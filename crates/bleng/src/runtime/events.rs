//! Ingress buffering and dispatch/event reconciliation against actor reservations.

use super::state::GattDiscoveryReservation;
use super::*;

pub(crate) struct DispatchResolved {
    pub(super) id: CommandId,
    pub(super) result: Result<(), CommandDispatchError>,
}

const PREDISPATCH_EVENTS: usize = 64;

fn connection_lost(event: &BleEvent, expected: ConnectionId) -> bool {
    matches!(
        event,
        BleEvent::ConnectionStateChanged {
            connection_id,
            result: blehub::ConnectionStateResult::Ok {
                state: blehub::ConnectionState::Disconnected,
            },
            ..
        } if *connection_id == expected
    ) || matches!(
        event,
        BleEvent::ConnectionStateChanged {
            command_id: None,
            connection_id,
            result: blehub::ConnectionStateResult::Err { .. },
        } if *connection_id == expected
    )
}

fn passive_connection_lost(event: &BleEvent, expected: ConnectionId) -> bool {
    matches!(
        event,
        BleEvent::ConnectionStateChanged {
            command_id: None,
            ..
        }
    ) && connection_lost(event, expected)
}

fn replay_after_rejection(
    lifecycle: &RuntimeLifecycle,
    buffered: Vec<BleEvent>,
    connection_id: ConnectionId,
) -> Vec<BleEvent> {
    if buffered
        .iter()
        .any(|event| !passive_connection_lost(event, connection_id))
    {
        lifecycle.fault();
    }
    buffered
        .into_iter()
        .filter(|event| passive_connection_lost(event, connection_id))
        .collect()
}

fn buffer_before_dispatch(
    lifecycle: &RuntimeLifecycle,
    buffered: &mut Vec<BleEvent>,
    event: BleEvent,
) {
    if buffered.len() >= PREDISPATCH_EVENTS {
        lifecycle.fault();
    } else {
        buffered.push(event);
    }
}

impl BlengActor {
    fn deliver(&mut self, event: BleEvent) {
        if self.events.try_send(event.clone()).is_err() {
            self.lifecycle.fault();
            return;
        }
        if self.lifecycle.phase() != RuntimePhase::Open {
            return;
        }
        // A commanded disconnect must belong to this connection's live
        // connect/disconnect lifecycle; a foreign command cannot retire it.
        if let BleEvent::ConnectionStateChanged {
            command_id: Some(command_id),
            connection_id,
            result:
                blehub::ConnectionStateResult::Ok {
                    state: blehub::ConnectionState::Disconnected,
                },
        } = &event
            && let Some(connection) = self
                .connections
                .values()
                .find(|connection| connection.connection_id == *connection_id)
            && *command_id != connection.dispatch.command_id
            && !connection
                .disconnect
                .as_ref()
                .is_some_and(|slot| slot.dispatch.command_id == *command_id)
        {
            return;
        }
        self.record_event_evidence(&event);
        if self.lifecycle.phase() != RuntimePhase::Open {
            return;
        }
        // Settle from the recorded evidence before releasing native/session
        // reservations. Late events still retire state after caller timeout.
        self.settle_event_waiters(&event);
        self.retire_completed_state(&event);
    }

    /// Records callback evidence in actor-owned reservations before any waiter
    /// is settled or short-lived reservation is retired.
    fn record_event_evidence(&mut self, event: &BleEvent) {
        if let BleEvent::AdapterSnapshotChanged {
            command_id: Some(command_id),
            snapshot,
        } = event
            && let Some(init) = self
                .init
                .as_mut()
                .filter(|init| init.dispatch.command_id == *command_id)
            && init.snapshot.is_none()
        {
            init.snapshot = Some(snapshot.clone());
        }

        if let BleEvent::ConnectionStateChanged {
            command_id,
            connection_id,
            result,
        } = &event
            && let Some(connection) = self
                .connections
                .values_mut()
                .find(|connection| connection.connection_id == *connection_id)
        {
            match (command_id, result) {
                (
                    Some(command_id),
                    blehub::ConnectionStateResult::Ok {
                        state: blehub::ConnectionState::Connected,
                    },
                ) if *command_id == connection.dispatch.command_id => {
                    connection.connected = true;
                }
                (Some(command_id), blehub::ConnectionStateResult::Err { error })
                    if *command_id == connection.dispatch.command_id =>
                {
                    connection.failed = true;
                    connection.connect_error = Some(error.clone());
                }
                (None, blehub::ConnectionStateResult::Err { .. })
                | (
                    None,
                    blehub::ConnectionStateResult::Ok {
                        state: blehub::ConnectionState::Disconnected,
                    },
                ) => {
                    connection.disconnected = true;
                }
                (
                    Some(command_id),
                    blehub::ConnectionStateResult::Ok {
                        state: blehub::ConnectionState::Disconnected,
                    },
                ) if *command_id == connection.dispatch.command_id
                    || connection
                        .disconnect
                        .as_ref()
                        .is_some_and(|slot| slot.dispatch.command_id == *command_id) =>
                {
                    connection.disconnected = true;
                }
                _ => {}
            }
        }
        if let BleEvent::ConnectionStateChanged {
            command_id,
            connection_id,
            result,
        } = &event
            && let Some(connection) = self
                .connections
                .values_mut()
                .find(|connection| connection.connection_id == *connection_id)
            && let Some(slot) = connection.disconnect.as_mut()
            && match (command_id, result) {
                (
                    None,
                    blehub::ConnectionStateResult::Err { .. }
                    | blehub::ConnectionStateResult::Ok {
                        state: blehub::ConnectionState::Disconnected,
                    },
                ) => true,
                (
                    Some(command_id),
                    blehub::ConnectionStateResult::Ok {
                        state: blehub::ConnectionState::Disconnected,
                    },
                ) => *command_id == slot.dispatch.command_id,
                _ => false,
            }
        {
            // A passive native error still proves that the disconnect domain
            // has completed. It must not fail an explicit disconnect waiter;
            // the command's own terminal event remains authoritative there.
            slot.domain = true;
        }

        if let BleEvent::GattDiscovered {
            command_id,
            database,
        } = &event
            && let Some(discovery) = self.connections.values_mut().find_map(|connection| {
                connection
                    .gatt
                    .as_mut()
                    .and_then(|gatt| gatt.discovery.as_mut())
                    .filter(|discovery| {
                        discovery.dispatch.command_id == *command_id
                            && discovery.session_id == database.gatt_session_id
                            && connection.connection_id == database.connection_id
                    })
            })
        {
            discovery.discovered = true;
            discovery.draft_database = Some(database.clone());
        }

        if let BleEvent::RssiRead {
            command_id,
            connection_id,
            rssi,
        } = &event
            && let Some(slot) = self.connections.values_mut().find_map(|connection| {
                (connection.connection_id == *connection_id)
                    .then_some(connection.rssi.as_mut())
                    .flatten()
                    .filter(|slot| slot.dispatch.command_id == *command_id)
            })
            && slot.value.is_none()
        {
            slot.value = Some(*rssi);
        }

        if let BleEvent::CharacteristicRead {
            command_id,
            connection_id,
            gatt_session_id,
            characteristic_id,
            value,
        } = &event
            && let Some(read) = self.connections.values_mut().find_map(|connection| {
                connection.read.as_mut().filter(|read| {
                    read.dispatch.command_id == *command_id
                        && read.session_id == *gatt_session_id
                        && read.characteristic_id == *characteristic_id
                        && connection.connection_id == *connection_id
                })
            })
            && read.value.is_none()
        {
            read.value = Some(value.clone());
        }

        if let BleEvent::CharacteristicWritten {
            command_id,
            connection_id,
            gatt_session_id,
            characteristic_id,
        } = &event
            && let Some(write) = self.connections.values_mut().find_map(|connection| {
                connection.write.as_mut().filter(|write| {
                    write.dispatch.command_id == *command_id
                        && write.session_id == *gatt_session_id
                        && write.characteristic_id == *characteristic_id
                        && connection.connection_id == *connection_id
                })
            })
        {
            write.written = true;
        }

        if let BleEvent::CommandCompleted { command_id, error } = &event {
            self.record_terminal(*command_id, error.clone());
        }
        self.observe_subscription(event);
        if self.lifecycle.phase() != RuntimePhase::Open {
            return;
        }
        if let Some(scan) = &mut self.scan {
            match &event {
                BleEvent::ScanStarted {
                    command_id,
                    scan_id,
                } if *command_id == scan.dispatch.command_id && *scan_id == scan.session_id => {
                    scan.started = true;
                }

                BleEvent::ScanStopped {
                    scan_id,
                    result: blehub::ScanStopResult::Err { error },
                } if *scan_id == scan.session_id => {
                    // Unlike a failed stop command, ScanStopped::Err reports
                    // that the native scan ended with an error (e.g. WinRT
                    // watcher failure). The session cannot be stopped again.
                    scan.stopped = true;
                    if let Some(stop) = scan.stop.as_mut() {
                        stop.stop_error = Some(error.clone());
                    }
                }
                BleEvent::ScanStopped {
                    scan_id,
                    result: blehub::ScanStopResult::Ok { reason },
                } if *scan_id == scan.session_id => {
                    scan.stopped |= match reason {
                        blehub::ScanStopReason::SystemSuspended => true,
                        blehub::ScanStopReason::Requested { command_id } => {
                            scan.stop.as_ref().is_some_and(|stop| {
                                stop.dispatch.command_id == *command_id
                                    && !stop.dispatch.is_pending()
                            })
                        }
                    };
                }
                _ => {}
            }
        }
    }

    /// Converts complete reservation evidence into caller-visible waiter results.
    fn settle_event_waiters(&mut self, event: &BleEvent) {
        if let BleEvent::ConnectionStateChanged { connection_id, .. } = event
            && connection_lost(event, *connection_id)
        {
            self.fail_connection_waiters(*connection_id);
            // Passive loss has no CommandId. It may supply the final domain
            // evidence for an explicit disconnect whose terminal arrived first.
            if let Some(command_id) = self
                .connections
                .values()
                .find(|connection| connection.connection_id == *connection_id)
                .and_then(|connection| connection.disconnect.as_ref())
                .map(|disconnect| disconnect.dispatch.command_id)
            {
                self.settle_waiter(command_id);
            }
        }
        if let Some(command_id) = event.command_id() {
            self.settle_waiter(command_id);
        }
        if matches!(event, BleEvent::ScanStopped { .. })
            && let Some(command_id) = self
                .scan
                .as_ref()
                .and_then(|scan| scan.stop.as_ref())
                .map(|stop| stop.dispatch.command_id)
        {
            self.settle_waiter(command_id);
        }
    }

    /// Removes reservations only after their native lifecycle is fully accounted
    /// for, including late callbacks after caller timeout or cancellation.
    fn retire_completed_state(&mut self, event: &BleEvent) {
        if self.init.as_ref().is_some_and(|init| {
            !init.dispatch.is_pending() && init.terminal == Some(Ok(())) && init.snapshot.is_some()
        }) {
            self.initialized = true;
        }
        if self.init.as_ref().is_some_and(|init| {
            !init.dispatch.is_pending() && init.terminal.as_ref().is_some_and(Result::is_err)
        }) {
            // Accepted setup failure invalidates the Hub even if its caller
            // already timed out or dropped the waiter.
            self.lifecycle.fault();
            return;
        }
        self.retire_subscriptions();
        for connection in self.connections.values_mut() {
            if connection.disconnect.as_ref().is_some_and(|slot| {
                !slot.dispatch.is_pending() && slot.terminal.as_ref().is_some_and(Result::is_err)
            }) {
                connection.disconnect = None;
            }
            let retire_rssi = connection.rssi.as_ref().is_some_and(|slot| {
                !slot.dispatch.is_pending()
                    && slot.terminal.is_some()
                    && (slot.terminal.as_ref().is_some_and(Result::is_err) || slot.value.is_some())
            });
            if retire_rssi {
                connection.rssi = None;
            }
            let retire_write = connection.write.as_ref().is_some_and(|write| {
                !write.dispatch.is_pending()
                    && write.terminal.is_some()
                    && (write.terminal.as_ref().is_some_and(Result::is_err) || write.written)
            });
            if retire_write {
                connection.write = None;
            }
            let retire_read = connection.read.as_ref().is_some_and(|read| {
                !read.dispatch.is_pending()
                    && read.terminal.is_some()
                    && (read.terminal.as_ref().is_some_and(Result::is_err) || read.value.is_some())
            });
            if retire_read {
                connection.read = None;
            }
            let retire_gatt = connection
                .gatt
                .as_ref()
                .and_then(|gatt| gatt.discovery.as_ref())
                .is_some_and(GattDiscoveryReservation::is_retirable);
            if retire_gatt {
                let mut remove_gatt = false;
                if let Some(gatt) = connection.gatt.as_mut()
                    && let Some(discovery) = gatt.discovery.take()
                {
                    if discovery.is_successful() {
                        gatt.database = discovery.draft_database;
                        connection.subscriptions.clear();
                    } else {
                        // A failed rediscovery must not erase a committed database.
                        remove_gatt = gatt.database.is_none();
                    }
                }
                if remove_gatt {
                    connection.gatt = None;
                }
            }
        }
        if let Some(peripheral_id) =
            self.connections
                .iter()
                .find_map(|(peripheral_id, connection)| {
                    let pending_dispatch = connection.disconnect.as_ref().is_some_and(|slot| {
                        slot.dispatch.is_pending() || slot.terminal.is_none() || !slot.domain
                    }) || connection
                        .gatt
                        .as_ref()
                        .and_then(|gatt| gatt.discovery.as_ref())
                        .is_some_and(|discovery| discovery.dispatch.is_pending())
                        || connection
                            .rssi
                            .as_ref()
                            .is_some_and(|slot| slot.dispatch.is_pending())
                        || connection
                            .read
                            .as_ref()
                            .is_some_and(|read| read.dispatch.is_pending())
                        || connection
                            .write
                            .as_ref()
                            .is_some_and(|write| write.dispatch.is_pending())
                        || connection.subscriptions.values().any(|route| {
                            route
                                .transition
                                .as_ref()
                                .is_some_and(|transition| transition.dispatch.is_pending())
                        });
                    (!connection.dispatch.is_pending()
                        && !pending_dispatch
                        && (connection.disconnected
                            || connection.failed
                            || connection.terminal.as_ref().is_some_and(Result::is_err)))
                    .then_some(*peripheral_id)
                })
        {
            self.connections.remove(&peripheral_id);
        }
        if let Some(scan) = &mut self.scan {
            match &scan.stop {
                // A native stop event (including Err) retires this scan.
                // Successful explicit stop still needs its terminal;
                // the error domain itself already proves native teardown.
                Some(stop)
                    if !stop.dispatch.is_pending()
                        && scan.stopped
                        && (stop.terminal.is_some()
                            || matches!(
                                event,
                                BleEvent::ScanStopped {
                                    result: blehub::ScanStopResult::Err { .. },
                                    ..
                                }
                            )) =>
                {
                    self.scan = None;
                    return;
                }
                Some(stop)
                    if !stop.dispatch.is_pending()
                        && stop.terminal.as_ref().is_some_and(Result::is_err) =>
                {
                    scan.stop = None;
                }
                _ => {}
            }
            if scan.stop.is_none() && !scan.dispatch.is_pending() && (scan.failed || scan.stopped) {
                self.scan = None;
            }
        }
    }
}

impl Message<DispatchResolved> for BlengActor {
    type Reply = ();

    async fn handle(&mut self, message: DispatchResolved, _: &mut Context<Self, Self::Reply>) {
        let buffered = if self
            .init
            .as_ref()
            .is_some_and(|init| init.dispatch.command_id == message.id)
        {
            let init = self.init.as_mut().expect("matching initialization exists");
            if message.result.is_err() {
                if init.dispatch.buffered.is_empty() {
                    // No native setup callback escaped. Preserve the exact
                    // rejection, but retire this one-attempt Hub.
                    self.lifecycle.start_cleanup();
                } else {
                    self.lifecycle.fault();
                }
                Vec::new()
            } else {
                init.dispatch.accept()
            }
        } else if self
            .scan
            .as_ref()
            .is_some_and(|scan| scan.dispatch.command_id == message.id)
        {
            if message.result.is_err() {
                self.scan = None;
                Vec::new()
            } else {
                let scan = self.scan.as_mut().expect("matching scan exists");
                scan.dispatch.accept()
            }
        } else if let Some(peripheral_id) =
            self.connections
                .iter()
                .find_map(|(peripheral_id, connection)| {
                    connection
                        .gatt
                        .as_ref()
                        .and_then(|gatt| gatt.discovery.as_ref())
                        .filter(|discovery| discovery.dispatch.command_id == message.id)
                        .map(|_| *peripheral_id)
                })
        {
            let connection = self
                .connections
                .get_mut(&peripheral_id)
                .expect("matching connection exists");
            let connection_id = connection.connection_id;
            if message.result.is_err() {
                let (buffered, remove_gatt) = {
                    let gatt = connection.gatt.as_mut().expect("matching GATT exists");
                    let mut discovery = gatt.discovery.take().expect("matching discovery exists");
                    (
                        std::mem::take(&mut discovery.dispatch.buffered),
                        gatt.database.is_none(),
                    )
                };
                if remove_gatt {
                    connection.gatt = None;
                }
                replay_after_rejection(&self.lifecycle, buffered, connection_id)
            } else {
                let gatt = connection.gatt.as_mut().expect("matching GATT exists");
                let discovery = gatt.discovery.as_mut().expect("matching discovery exists");
                discovery.dispatch.accept()
            }
        } else if let Some(peripheral_id) = self.connections.iter().find_map(|(key, connection)| {
            connection
                .rssi
                .as_ref()
                .filter(|slot| slot.dispatch.command_id == message.id)
                .map(|_| *key)
        }) {
            let connection = self
                .connections
                .get_mut(&peripheral_id)
                .expect("matching connection exists");
            let slot = connection.rssi.as_mut().expect("matching RSSI exists");
            if message.result.is_err() {
                let buffered = std::mem::take(&mut slot.dispatch.buffered);
                let connection_id = connection.connection_id;
                connection.rssi = None;
                replay_after_rejection(&self.lifecycle, buffered, connection_id)
            } else {
                slot.dispatch.accept()
            }
        } else if let Some(peripheral_id) =
            self.connections
                .iter()
                .find_map(|(peripheral_id, connection)| {
                    connection
                        .read
                        .as_ref()
                        .filter(|read| read.dispatch.command_id == message.id)
                        .map(|_| *peripheral_id)
                })
        {
            let connection = self
                .connections
                .get_mut(&peripheral_id)
                .expect("matching connection exists");
            let read = connection.read.as_mut().expect("matching read exists");
            if message.result.is_err() {
                let buffered = std::mem::take(&mut read.dispatch.buffered);
                let connection_id = connection.connection_id;
                connection.read = None;
                replay_after_rejection(&self.lifecycle, buffered, connection_id)
            } else {
                read.dispatch.accept()
            }
        } else if let Some(peripheral_id) =
            self.connections
                .iter()
                .find_map(|(peripheral_id, connection)| {
                    connection
                        .write
                        .as_ref()
                        .filter(|write| write.dispatch.command_id == message.id)
                        .map(|_| *peripheral_id)
                })
        {
            let connection = self
                .connections
                .get_mut(&peripheral_id)
                .expect("matching connection exists");
            let write = connection.write.as_mut().expect("matching write exists");
            if message.result.is_err() {
                let buffered = std::mem::take(&mut write.dispatch.buffered);
                let connection_id = connection.connection_id;
                connection.write = None;
                replay_after_rejection(&self.lifecycle, buffered, connection_id)
            } else {
                write.dispatch.accept()
            }
        } else if let Some((peripheral_id, subscription_id)) =
            self.connections
                .iter()
                .find_map(|(peripheral_id, connection)| {
                    connection
                        .subscriptions
                        .iter()
                        .find_map(|(subscription_id, route)| {
                            route
                                .transition
                                .as_ref()
                                .filter(|transition| transition.dispatch.command_id == message.id)
                                .map(|_| (*peripheral_id, *subscription_id))
                        })
                })
        {
            let connection_id = self.connections[&peripheral_id].connection_id;
            let route = self
                .connections
                .get_mut(&peripheral_id)
                .expect("matching connection exists")
                .subscriptions
                .get_mut(&subscription_id)
                .expect("matching subscription exists");
            let transition = route
                .transition
                .as_mut()
                .expect("matching transition exists");
            if message.result.is_err() {
                let ambiguous = transition
                    .dispatch
                    .buffered
                    .iter()
                    .any(|event| !connection_lost(event, connection_id));
                let disconnects = std::mem::take(&mut transition.dispatch.buffered);
                if route.active {
                    route.transition = None;
                } else {
                    self.connections
                        .get_mut(&peripheral_id)
                        .expect("matching connection exists")
                        .subscriptions
                        .remove(&subscription_id);
                }
                if ambiguous {
                    self.lifecycle.fault();
                }
                // The dispatch rejection wins for the command, but an unrelated
                // passive loss still retires the connection after that decision.
                disconnects
                    .into_iter()
                    .filter(|event| connection_lost(event, connection_id))
                    .collect()
            } else {
                transition.dispatch.accept()
            }
        } else if let Some(peripheral_id) = self.connections.iter().find_map(|(key, connection)| {
            connection
                .disconnect
                .as_ref()
                .filter(|slot| slot.dispatch.command_id == message.id)
                .map(|_| *key)
        }) {
            let connection_id = self.connections[&peripheral_id].connection_id;
            let connection = self
                .connections
                .get_mut(&peripheral_id)
                .expect("matching connection exists");
            let slot = connection
                .disconnect
                .as_mut()
                .expect("matching disconnect exists");
            if message.result.is_err() {
                let buffered = std::mem::take(&mut slot.dispatch.buffered);
                let ambiguous = buffered.iter().any(|event| !matches!(event,
                    BleEvent::ConnectionStateChanged { command_id: None, .. } if connection_lost(event, connection_id)));
                connection.disconnect = None;
                if ambiguous {
                    self.lifecycle.fault();
                }
                // Rejection wins, but a concurrent passive loss still retires
                // the old connection after the waiter receives the rejection.
                buffered
                    .into_iter()
                    .filter(|event| {
                        matches!(
                            event,
                            BleEvent::ConnectionStateChanged {
                                command_id: None,
                                ..
                            }
                        ) && connection_lost(event, connection_id)
                    })
                    .collect()
            } else {
                slot.dispatch.accept()
            }
        } else if let Some(peripheral_id) =
            self.connections
                .iter()
                .find_map(|(peripheral_id, connection)| {
                    (connection.dispatch.command_id == message.id).then_some(*peripheral_id)
                })
        {
            let connection = self
                .connections
                .get_mut(&peripheral_id)
                .expect("matching connection exists");
            if message.result.is_err() {
                let ambiguous = !connection.dispatch.buffered.is_empty();
                self.connections.remove(&peripheral_id);
                if ambiguous {
                    self.lifecycle.fault();
                }
                Vec::new()
            } else {
                connection.dispatch.accept()
            }
        } else if self
            .scan
            .as_ref()
            .and_then(|scan| scan.stop.as_ref())
            .is_some_and(|stop| stop.dispatch.command_id == message.id)
        {
            let scan = self.scan.as_mut().expect("matching scan exists");
            let stop = scan.stop.as_mut().expect("matching stop exists");
            if message.result.is_err() {
                if stop
                    .dispatch
                    .buffered
                    .iter()
                    .any(|event| matches!(event, BleEvent::ScanStopped { .. }))
                {
                    // A rejected stop with an already-observed stop callback
                    // leaves native scan ownership ambiguous; fail closed.
                    self.lifecycle.fault();
                }
                scan.stop = None;
                Vec::new()
            } else {
                stop.dispatch.accept()
            }
        } else {
            Vec::new()
        };
        if let Err(error) = message.result {
            self.waiters
                .finish(message.id, Err(WaiterError::DispatchRejected(error)));
        }
        for event in buffered {
            if self.lifecycle.phase() != RuntimePhase::Open {
                break;
            }
            self.deliver(event);
        }
    }
}

impl Message<IngressEvent> for BlengActor {
    type Reply = ();

    async fn handle(
        &mut self,
        IngressEvent(event): IngressEvent,
        _: &mut Context<Self, Self::Reply>,
    ) {
        if self.lifecycle.phase() != RuntimePhase::Open {
            return;
        }
        if let BleEvent::ConnectionStateChanged {
            command_id: Some(command_id),
            connection_id,
            result:
                blehub::ConnectionStateResult::Ok {
                    state: blehub::ConnectionState::Disconnected,
                },
        } = &event
            && let Some(connection) = self
                .connections
                .values()
                .find(|connection| connection.connection_id == *connection_id)
            && *command_id != connection.dispatch.command_id
            && !connection
                .disconnect
                .as_ref()
                .is_some_and(|slot| slot.dispatch.command_id == *command_id)
        {
            self.deliver(event);
            return;
        }
        if let Some(init) = &mut self.init
            && init.dispatch.is_pending()
            && match &event {
                BleEvent::AdapterSnapshotChanged {
                    command_id: Some(id),
                    ..
                }
                | BleEvent::CommandCompleted { command_id: id, .. } => {
                    *id == init.dispatch.command_id
                }
                _ => false,
            }
        {
            buffer_before_dispatch(&self.lifecycle, &mut init.dispatch.buffered, event);
            return;
        }
        if let Some(connection) = self.connections.values_mut().find(|connection| {
            connection.disconnect.as_ref().is_some_and(|slot| {
                slot.dispatch.is_pending()
                    && match &event {
                        BleEvent::ConnectionStateChanged {
                            command_id,
                            connection_id,
                            result:
                                blehub::ConnectionStateResult::Ok {
                                    state: blehub::ConnectionState::Disconnected,
                                },
                        } => {
                            *connection_id == connection.connection_id
                                && (command_id.is_none()
                                    || *command_id == Some(slot.dispatch.command_id))
                        }
                        BleEvent::ConnectionStateChanged {
                            command_id: None,
                            connection_id,
                            result: blehub::ConnectionStateResult::Err { .. },
                        } => *connection_id == connection.connection_id,
                        BleEvent::CommandCompleted { command_id, .. } => {
                            *command_id == slot.dispatch.command_id
                        }
                        _ => false,
                    }
            })
        }) {
            let slot = connection
                .disconnect
                .as_mut()
                .expect("matching disconnect exists");
            buffer_before_dispatch(&self.lifecycle, &mut slot.dispatch.buffered, event);
            return;
        }
        if let Some(connection) = self.connections.values_mut().find(|connection| {
            connection
                .gatt
                .as_ref()
                .and_then(|gatt| gatt.discovery.as_ref())
                .is_some_and(|discovery| {
                    discovery.dispatch.is_pending()
                        && match &event {
                            BleEvent::GattDiscovered {
                                command_id,
                                database,
                            } => {
                                discovery.dispatch.command_id == *command_id
                                    && discovery.session_id == database.gatt_session_id
                                    && connection.connection_id == database.connection_id
                            }
                            BleEvent::CommandCompleted { command_id, .. } => {
                                discovery.dispatch.command_id == *command_id
                            }
                            _ => connection_lost(&event, connection.connection_id),
                        }
                })
        }) {
            let gatt = connection.gatt.as_mut().expect("matching GATT exists");
            let discovery = gatt.discovery.as_mut().expect("matching discovery exists");
            buffer_before_dispatch(&self.lifecycle, &mut discovery.dispatch.buffered, event);
            return;
        }
        if let Some(connection) = self.connections.values_mut().find(|connection| {
            connection.rssi.as_ref().is_some_and(|slot| {
                slot.dispatch.is_pending()
                    && match &event {
                        BleEvent::RssiRead {
                            command_id,
                            connection_id,
                            ..
                        } => {
                            slot.dispatch.command_id == *command_id
                                && connection.connection_id == *connection_id
                        }
                        BleEvent::CommandCompleted { command_id, .. } => {
                            slot.dispatch.command_id == *command_id
                        }
                        _ => connection_lost(&event, connection.connection_id),
                    }
            })
        }) {
            let slot = connection.rssi.as_mut().expect("matching RSSI exists");
            buffer_before_dispatch(&self.lifecycle, &mut slot.dispatch.buffered, event);
            return;
        }
        if let Some(connection) = self.connections.values_mut().find(|connection| {
            connection.read.as_ref().is_some_and(|read| {
                read.dispatch.is_pending()
                    && match &event {
                        BleEvent::CharacteristicRead {
                            command_id,
                            connection_id,
                            gatt_session_id,
                            characteristic_id,
                            ..
                        } => {
                            read.dispatch.command_id == *command_id
                                && read.session_id == *gatt_session_id
                                && read.characteristic_id == *characteristic_id
                                && connection.connection_id == *connection_id
                        }
                        BleEvent::CommandCompleted { command_id, .. } => {
                            read.dispatch.command_id == *command_id
                        }
                        _ => connection_lost(&event, connection.connection_id),
                    }
            })
        }) {
            let read = connection.read.as_mut().expect("matching read exists");
            buffer_before_dispatch(&self.lifecycle, &mut read.dispatch.buffered, event);
            return;
        }
        if let Some(connection) = self.connections.values_mut().find(|connection| {
            connection.write.as_ref().is_some_and(|write| {
                write.dispatch.is_pending()
                    && match &event {
                        BleEvent::CharacteristicWritten {
                            command_id,
                            connection_id,
                            gatt_session_id,
                            characteristic_id,
                        } => {
                            write.dispatch.command_id == *command_id
                                && write.session_id == *gatt_session_id
                                && write.characteristic_id == *characteristic_id
                                && connection.connection_id == *connection_id
                        }
                        BleEvent::CommandCompleted { command_id, .. } => {
                            write.dispatch.command_id == *command_id
                        }
                        _ => connection_lost(&event, connection.connection_id),
                    }
            })
        }) {
            let write = connection.write.as_mut().expect("matching write exists");
            buffer_before_dispatch(&self.lifecycle, &mut write.dispatch.buffered, event);
            return;
        }
        if let Some((connection, subscription)) =
            self.connections.values_mut().find_map(|connection| {
                let connection_id = connection.connection_id;
                connection
                    .subscriptions
                    .iter_mut()
                    .find_map(|(subscription_id, route)| {
                        let matches = route.transition.as_ref().is_some_and(|transition| {
                            transition.dispatch.is_pending()
                                && match &event {
                                    BleEvent::NotificationStateChanged {
                                        command_id,
                                        connection_id: event_connection,
                                        gatt_session_id,
                                        subscription_id: event_subscription,
                                        characteristic_id,
                                        ..
                                    } => {
                                        transition.dispatch.command_id == *command_id
                                            && connection_id == *event_connection
                                            && route.session_id == *gatt_session_id
                                            && *subscription_id == *event_subscription
                                            && route.characteristic_id == *characteristic_id
                                    }
                                    BleEvent::CommandCompleted { command_id, .. } => {
                                        transition.dispatch.command_id == *command_id
                                    }
                                    _ => connection_lost(&event, connection_id),
                                }
                        });
                        matches.then_some((connection_id, route))
                    })
            })
        {
            let _ = connection;
            buffer_before_dispatch(
                &self.lifecycle,
                &mut subscription
                    .transition
                    .as_mut()
                    .expect("matching transition exists")
                    .dispatch
                    .buffered,
                event,
            );
            return;
        }
        if let Some(connection) = self.connections.values_mut().find(|connection| {
            connection.dispatch.is_pending()
                && match &event {
                    BleEvent::ConnectionStateChanged {
                        command_id,
                        connection_id,
                        ..
                    } => {
                        *connection_id == connection.connection_id
                            || *command_id == Some(connection.dispatch.command_id)
                    }
                    BleEvent::CommandCompleted { command_id, .. } => {
                        *command_id == connection.dispatch.command_id
                    }
                    _ => false,
                }
        }) {
            buffer_before_dispatch(&self.lifecycle, &mut connection.dispatch.buffered, event);
            return;
        }
        if let Some(scan) = &mut self.scan
            && scan.dispatch.is_pending()
            && match &event {
                BleEvent::ScanStarted { command_id, .. }
                | BleEvent::CommandCompleted { command_id, .. } => {
                    *command_id == scan.dispatch.command_id
                }
                BleEvent::AdvertisementObserved { advertisement } => {
                    advertisement.scan_session_id == scan.session_id
                }
                BleEvent::ScanStopped { scan_id, .. } => *scan_id == scan.session_id,
                _ => false,
            }
        {
            buffer_before_dispatch(&self.lifecycle, &mut scan.dispatch.buffered, event);
            return;
        }
        if let Some(scan) = &mut self.scan
            && let Some(stop) = &mut scan.stop
            && stop.dispatch.is_pending()
            && match &event {
                BleEvent::CommandCompleted { command_id, .. } => {
                    *command_id == stop.dispatch.command_id
                }
                BleEvent::ScanStopped { scan_id, .. } => *scan_id == scan.session_id,
                _ => false,
            }
        {
            buffer_before_dispatch(&self.lifecycle, &mut stop.dispatch.buffered, event);
            return;
        }
        self.deliver(event);
    }
}
