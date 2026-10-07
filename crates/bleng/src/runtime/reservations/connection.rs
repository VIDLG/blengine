use std::collections::HashMap;

use blehub::{
    BleEvent, BleEventError, CommandDispatchError, CommandId, ConnectionId, ConnectionState,
    ConnectionStateResult, GattCharacteristicId, GattSessionId, SubscriptionId,
};

use crate::waiter::{WaiterError, WaiterSuccess};

use super::{
    dispatch::{
        DispatchEffect, DispatchReservation, DispatchResolution, ReconcileFault, record_terminal,
    },
    gatt::{GattDiscoveryReservation, GattReservation},
    io::{ReadReservation, RssiReservation, WriteReservation},
    subscription::{NotificationTransition, SubscriptionReservation},
};

#[derive(Clone, Copy)]
pub(super) enum PreAckRoute {
    Disconnect,
    Discovery,
    Rssi,
    Read,
    Write,
    Subscription,
    Connect,
}

/// A connection owns its GATT session, I/O slots, and subscriptions.
/// Keeping these under one peripheral key prevents cross-device callback routing.
pub(super) struct ConnectionReservation {
    pub(super) dispatch: DispatchReservation,
    pub(super) connection_id: ConnectionId,
    pub(super) connected: bool,
    pub(super) disconnected: bool,
    pub(super) terminal: Option<Result<(), BleEventError>>,
    connect_error: Option<BleEventError>,
    pub(super) disconnect: Option<DisconnectReservation>,
    pub(super) gatt: Option<GattReservation>,
    pub(super) read: Option<ReadReservation>,
    pub(super) rssi: Option<RssiReservation>,
    pub(super) write: Option<WriteReservation>,
    pub(super) subscriptions: HashMap<SubscriptionId, SubscriptionReservation>,
}

impl ConnectionReservation {
    pub(super) fn pre_ack_route(
        &mut self,
        route: PreAckRoute,
        event: &BleEvent,
    ) -> Option<&mut DispatchReservation> {
        let connection_id = self.connection_id;
        let dispatch = match route {
            PreAckRoute::Disconnect => self
                .disconnect
                .as_mut()
                .filter(|slot| slot.matches_event(event, connection_id))
                .map(|slot| &mut slot.dispatch),
            PreAckRoute::Discovery => self
                .gatt
                .as_mut()
                .and_then(|gatt| gatt.discovery.as_mut())
                .filter(|slot| slot.matches_event(event, connection_id))
                .map(|slot| &mut slot.dispatch),
            PreAckRoute::Rssi => self
                .rssi
                .as_mut()
                .filter(|slot| slot.matches_event(event, connection_id))
                .map(|slot| &mut slot.dispatch),
            PreAckRoute::Read => self
                .read
                .as_mut()
                .filter(|slot| slot.matches_event(event, connection_id))
                .map(|slot| &mut slot.dispatch),
            PreAckRoute::Write => self
                .write
                .as_mut()
                .filter(|slot| slot.matches_event(event, connection_id))
                .map(|slot| &mut slot.dispatch),
            PreAckRoute::Subscription => {
                self.subscriptions
                    .iter_mut()
                    .find_map(|(subscription_id, route)| {
                        if route.matches_event(event, connection_id, *subscription_id)
                            && route.transition.as_ref().is_some_and(|transition| {
                                transition.dispatch.is_awaiting_dispatch_ack()
                            })
                        {
                            route.transition.as_mut().map(|slot| &mut slot.dispatch)
                        } else {
                            None
                        }
                    })
            }
            PreAckRoute::Connect => {
                // Buffer broadly before acceptance, but record only exact
                // connection/command evidence after replay.
                let matches = match event {
                    BleEvent::ConnectionStateChanged {
                        command_id,
                        connection_id,
                        ..
                    } => {
                        *connection_id == self.connection_id
                            || *command_id == Some(self.dispatch.command_id)
                    }
                    BleEvent::CommandCompleted { command_id, .. } => {
                        *command_id == self.dispatch.command_id
                    }
                    _ => false,
                };
                matches.then_some(&mut self.dispatch)
            }
        };
        dispatch.filter(|dispatch| dispatch.is_awaiting_dispatch_ack())
    }

    pub(super) fn record_event(&mut self, event: &BleEvent) -> Result<(), ReconcileFault> {
        let connection_id = self.connection_id;
        match event {
            BleEvent::ConnectionStateChanged {
                command_id, result, ..
            } => {
                match (command_id, result) {
                    (
                        Some(command_id),
                        ConnectionStateResult::Ok {
                            state: ConnectionState::Connected,
                        },
                    ) if *command_id == self.dispatch.command_id => {
                        self.connected = true;
                    }
                    (Some(command_id), ConnectionStateResult::Err { error })
                        if *command_id == self.dispatch.command_id =>
                    {
                        self.connect_error = Some(error.clone());
                    }
                    (None, ConnectionStateResult::Err { .. })
                    | (
                        None,
                        ConnectionStateResult::Ok {
                            state: ConnectionState::Disconnected,
                        },
                    ) => {
                        self.disconnected = true;
                    }
                    (
                        Some(command_id),
                        ConnectionStateResult::Ok {
                            state: ConnectionState::Disconnected,
                        },
                    ) if *command_id == self.dispatch.command_id
                        || self
                            .disconnect
                            .as_ref()
                            .is_some_and(|slot| slot.dispatch.command_id == *command_id) =>
                    {
                        self.disconnected = true;
                    }
                    _ => {}
                }
                if let Some(slot) = self.disconnect.as_mut()
                    && slot.matches_event(event, connection_id)
                {
                    // Passive errors prove teardown but do not replace the
                    // explicit disconnect command's authoritative terminal.
                    slot.domain = true;
                }
            }
            BleEvent::GattDiscovered { database, .. } => {
                if let Some(discovery) = self.gatt.as_mut().and_then(|gatt| gatt.discovery.as_mut())
                    && discovery.matches_event(event, connection_id)
                {
                    // Discovery deliberately keeps the latest correlated draft.
                    discovery.draft_database = Some(database.clone());
                }
            }
            BleEvent::RssiRead { rssi, .. } => {
                if let Some(slot) = self.rssi.as_mut()
                    && slot.matches_event(event, connection_id)
                    && slot.value.is_none()
                {
                    slot.value = Some(*rssi);
                }
            }
            BleEvent::CharacteristicRead { value, .. } => {
                if let Some(read) = self.read.as_mut()
                    && read.matches_event(event, connection_id)
                    && read.value.is_none()
                {
                    read.value = Some(value.clone());
                }
            }
            BleEvent::CharacteristicWritten { .. } => {
                if let Some(write) = self.write.as_mut()
                    && write.matches_event(event, connection_id)
                {
                    write.written = true;
                }
            }
            BleEvent::NotificationStateChanged {
                subscription_id, ..
            } => {
                if let Some(route) = self.subscriptions.get_mut(subscription_id) {
                    route.record_event(event, connection_id, *subscription_id)?;
                }
            }
            _ => {}
        }
        Ok(())
    }

    pub(super) fn resolve_operation_dispatch(
        &mut self,
        command_id: CommandId,
        result: &Result<(), CommandDispatchError>,
    ) -> Option<DispatchResolution> {
        let connection_id = self.connection_id;
        if let Some(discovery) = self
            .gatt
            .as_mut()
            .and_then(|gatt| gatt.discovery.as_mut())
            .filter(|slot| slot.dispatch.command_id == command_id)
        {
            return Some(if result.is_err() {
                let discovery = self.take_discovery().expect("matching discovery exists");
                replay_after_rejection(
                    discovery.dispatch.buffered,
                    connection_id,
                    passive_connection_lost,
                )
            } else {
                discovery.dispatch.accepted_resolution()
            });
        }
        if let Some(slot) = self
            .rssi
            .as_mut()
            .filter(|slot| slot.dispatch.command_id == command_id)
        {
            return Some(if result.is_err() {
                let buffered = std::mem::take(&mut slot.dispatch.buffered);
                self.rssi = None;
                replay_after_rejection(buffered, connection_id, passive_connection_lost)
            } else {
                slot.dispatch.accepted_resolution()
            });
        }
        if let Some(read) = self
            .read
            .as_mut()
            .filter(|slot| slot.dispatch.command_id == command_id)
        {
            return Some(if result.is_err() {
                let buffered = std::mem::take(&mut read.dispatch.buffered);
                self.read = None;
                replay_after_rejection(buffered, connection_id, passive_connection_lost)
            } else {
                read.dispatch.accepted_resolution()
            });
        }
        if let Some(write) = self
            .write
            .as_mut()
            .filter(|slot| slot.dispatch.command_id == command_id)
        {
            return Some(if result.is_err() {
                let buffered = std::mem::take(&mut write.dispatch.buffered);
                self.write = None;
                replay_after_rejection(buffered, connection_id, passive_connection_lost)
            } else {
                write.dispatch.accepted_resolution()
            });
        }
        if let Some(subscription_id) =
            self.subscriptions
                .iter()
                .find_map(|(subscription_id, route)| {
                    route
                        .transition
                        .as_ref()
                        .filter(|transition| transition.dispatch.command_id == command_id)
                        .map(|_| *subscription_id)
                })
        {
            let transition = self
                .subscriptions
                .get_mut(&subscription_id)
                .and_then(|route| route.transition.as_mut())
                .expect("matching transition exists");
            return Some(if result.is_err() {
                let buffered = std::mem::take(&mut transition.dispatch.buffered);
                self.take_subscription_transition(subscription_id);
                // Subscription rejection also permits commanded connection loss.
                replay_after_rejection(buffered, connection_id, connection_lost)
            } else {
                transition.dispatch.accepted_resolution()
            });
        }
        if let Some(slot) = self
            .disconnect
            .as_mut()
            .filter(|slot| slot.dispatch.command_id == command_id)
        {
            return Some(if result.is_err() {
                let buffered = std::mem::take(&mut slot.dispatch.buffered);
                self.disconnect = None;
                replay_after_rejection(buffered, connection_id, passive_connection_lost)
            } else {
                slot.dispatch.accepted_resolution()
            });
        }
        None
    }

    pub(super) fn contains_command(&self, command_id: CommandId) -> bool {
        self.dispatches()
            .any(|dispatch| dispatch.command_id == command_id)
    }

    /// Validates operation-local admission after the container resolves its connection.
    /// Keep error precedence explicit: connection, handoff, target, I/O, discovery.
    pub(super) fn validate_gatt_io(
        &self,
        session_id: GattSessionId,
        characteristic_id: GattCharacteristicId,
        read: bool,
    ) -> Result<(), WaiterError> {
        if !self.connected || self.disconnected || self.has_failed() {
            return Err(WaiterError::ConnectionNotActive);
        }
        if self.disconnect.is_some()
            || self
                .rssi
                .as_ref()
                .is_some_and(|slot| slot.dispatch.is_awaiting_dispatch_ack())
        {
            return Err(WaiterError::OperationBusy);
        }
        let database = self
            .gatt
            .as_ref()
            .and_then(|gatt| gatt.committed_database(session_id))
            .ok_or(WaiterError::GattNotReady)?;
        if !database.services.iter().any(|service| {
            service
                .characteristics
                .iter()
                .any(|characteristic| characteristic.id == characteristic_id)
        }) {
            return Err(WaiterError::GattNotReady);
        }
        if self.read.is_some()
            || self.write.is_some()
            || self
                .subscriptions
                .values()
                .any(|subscription| subscription.transition.is_some())
            || (read
                && self
                    .subscriptions
                    .values()
                    .any(|subscription| subscription.characteristic_id == characteristic_id))
        {
            return Err(WaiterError::OperationBusy);
        }
        if self
            .gatt
            .as_ref()
            .is_some_and(GattReservation::has_pending_discovery)
        {
            return Err(WaiterError::GattBusy);
        }
        Ok(())
    }

    pub(super) fn new(command_id: CommandId, connection_id: ConnectionId) -> Self {
        Self {
            dispatch: DispatchReservation::new(command_id),
            connection_id,
            connected: false,
            disconnected: false,
            terminal: None,
            connect_error: None,
            disconnect: None,
            gatt: None,
            read: None,
            rssi: None,
            write: None,
            subscriptions: HashMap::new(),
        }
    }

    /// Remove a provisional discovery without touching the committed database.
    /// Dispatch rejection preserves the last committed GATT session.
    fn take_discovery(&mut self) -> Option<GattDiscoveryReservation> {
        let gatt = self.gatt.as_mut()?;
        let discovery = gatt.discovery.take();
        if gatt.database.is_none() {
            self.gatt = None;
        }
        discovery
    }

    /// Preserve active routes when a disable transition is rejected;
    /// a provisional enable has no active route to retain.
    fn take_subscription_transition(
        &mut self,
        subscription_id: SubscriptionId,
    ) -> Option<NotificationTransition> {
        let route = self.subscriptions.get_mut(&subscription_id)?;
        let transition = route.transition.take();
        if !route.active {
            self.subscriptions.remove(&subscription_id);
        }
        transition
    }

    pub(super) fn has_failed(&self) -> bool {
        self.connect_error.is_some()
    }

    /// Called after waiter settlement, even when the caller already retired.
    pub(super) fn retire_completed_operations(&mut self) {
        self.subscriptions
            .retain(|_, route| route.retire_transition());
        if self.disconnect.as_ref().is_some_and(|slot| {
            !slot.dispatch.is_awaiting_dispatch_ack()
                && slot.terminal.as_ref().is_some_and(Result::is_err)
        }) {
            self.disconnect = None;
        }
        if self
            .rssi
            .as_ref()
            .is_some_and(RssiReservation::is_retirable)
        {
            self.rssi = None;
        }
        if self
            .read
            .as_ref()
            .is_some_and(ReadReservation::is_retirable)
        {
            self.read = None;
        }
        if self
            .write
            .as_ref()
            .is_some_and(WriteReservation::is_retirable)
        {
            self.write = None;
        }
        if let Some(gatt) = self.gatt.as_mut() {
            if gatt.commit_completed_discovery() {
                // Successful rediscovery invalidates routes for the old GATT session.
                self.subscriptions.clear();
            }
            if gatt.database.is_none() && gatt.discovery.is_none() {
                self.gatt = None;
            }
        }
    }

    /// A disconnected connection cannot be removed until all dispatch handoffs
    /// have resolved; an explicit disconnect also needs its own terminal/domain.
    pub(super) fn is_retirable(&self) -> bool {
        let pending_disconnect = self.disconnect.as_ref().is_some_and(|slot| {
            slot.dispatch.is_awaiting_dispatch_ack() || slot.terminal.is_none() || !slot.domain
        });
        !pending_disconnect
            && !self
                .dispatches()
                .any(DispatchReservation::is_awaiting_dispatch_ack)
            && (self.disconnected
                || self.has_failed()
                || self.terminal.as_ref().is_some_and(Result::is_err))
    }

    fn connect_result(&self) -> Option<Result<WaiterSuccess, WaiterError>> {
        if self.dispatch.is_awaiting_dispatch_ack() {
            return None;
        }
        if let Some(error) = &self.connect_error {
            return Some(Err(WaiterError::Operation(error.clone())));
        }
        if self.disconnected {
            return Some(Err(WaiterError::Disconnected));
        }
        self.dispatch.waiter_result(&self.terminal, || {
            self.connected
                .then_some(WaiterSuccess::Connected(self.connection_id))
        })
    }

    pub(super) fn waiter_result(
        &self,
        command_id: CommandId,
    ) -> Option<Result<WaiterSuccess, WaiterError>> {
        if self.dispatch.command_id == command_id {
            return self.connect_result();
        }
        if let Some(disconnect) = self
            .disconnect
            .as_ref()
            .filter(|disconnect| disconnect.dispatch.command_id == command_id)
        {
            return disconnect.waiter_result(self.connection_id);
        }
        if let Some(gatt) = self
            .gatt
            .as_ref()
            .and_then(|gatt| gatt.discovery.as_ref())
            .filter(|gatt| gatt.dispatch.command_id == command_id)
        {
            return gatt.waiter_result();
        }
        if let Some(rssi) = self
            .rssi
            .as_ref()
            .filter(|rssi| rssi.dispatch.command_id == command_id)
        {
            return rssi.waiter_result(self.connection_id);
        }
        if let Some(read) = self
            .read
            .as_ref()
            .filter(|read| read.dispatch.command_id == command_id)
        {
            return read.waiter_result(self.connection_id);
        }
        if let Some(write) = self
            .write
            .as_ref()
            .filter(|write| write.dispatch.command_id == command_id)
        {
            return write.waiter_result(self.connection_id);
        }
        if let Some((subscription_id, route, transition)) =
            self.subscriptions
                .iter()
                .find_map(|(subscription_id, route)| {
                    route
                        .transition
                        .as_ref()
                        .filter(|transition| transition.dispatch.command_id == command_id)
                        .map(|transition| (*subscription_id, route, transition))
                })
        {
            return transition.waiter_result(
                self.connection_id,
                subscription_id,
                route.session_id,
                route.characteristic_id,
            );
        }
        None
    }

    pub(super) fn record_terminal(
        &mut self,
        command_id: CommandId,
        result: &Result<(), BleEventError>,
    ) -> bool {
        let terminals = [
            Some((&self.dispatch, &mut self.terminal)),
            self.disconnect
                .as_mut()
                .map(|slot| (&slot.dispatch, &mut slot.terminal)),
            self.gatt
                .as_mut()
                .and_then(|gatt| gatt.discovery.as_mut())
                .map(|slot| (&slot.dispatch, &mut slot.terminal)),
            self.rssi
                .as_mut()
                .map(|slot| (&slot.dispatch, &mut slot.terminal)),
            self.read
                .as_mut()
                .map(|slot| (&slot.dispatch, &mut slot.terminal)),
            self.write
                .as_mut()
                .map(|slot| (&slot.dispatch, &mut slot.terminal)),
        ];
        for (dispatch, terminal) in terminals.into_iter().flatten().chain(
            self.subscriptions
                .values_mut()
                .filter_map(|route| route.transition.as_mut())
                .map(|slot| (&slot.dispatch, &mut slot.terminal)),
        ) {
            if dispatch.command_id == command_id {
                record_terminal(terminal, result);
                return true;
            }
        }
        false
    }

    pub(super) fn accepted_command_ids(&self) -> Vec<CommandId> {
        self.dispatches()
            .filter(|dispatch| !dispatch.is_awaiting_dispatch_ack())
            .map(|dispatch| dispatch.command_id)
            .collect()
    }

    /// Shared traversal of dispatch metadata, not a second ownership/index map.
    fn dispatches(&self) -> impl Iterator<Item = &DispatchReservation> {
        [
            Some(&self.dispatch),
            self.disconnect.as_ref().map(|slot| &slot.dispatch),
            self.gatt
                .as_ref()
                .and_then(|gatt| gatt.discovery.as_ref())
                .map(|slot| &slot.dispatch),
            self.rssi.as_ref().map(|slot| &slot.dispatch),
            self.read.as_ref().map(|slot| &slot.dispatch),
            self.write.as_ref().map(|slot| &slot.dispatch),
        ]
        .into_iter()
        .flatten()
        .chain(
            self.subscriptions
                .values()
                .filter_map(|route| route.transition.as_ref())
                .map(|slot| &slot.dispatch),
        )
    }
}

pub(super) struct DisconnectReservation {
    pub(super) dispatch: DispatchReservation,
    pub(super) domain: bool,
    pub(super) terminal: Option<Result<(), BleEventError>>,
}

impl DisconnectReservation {
    fn matches_event(&self, event: &BleEvent, expected: ConnectionId) -> bool {
        match event {
            BleEvent::ConnectionStateChanged {
                command_id,
                connection_id,
                result:
                    ConnectionStateResult::Ok {
                        state: ConnectionState::Disconnected,
                    },
            } => {
                *connection_id == expected
                    && (command_id.is_none() || *command_id == Some(self.dispatch.command_id))
            }
            BleEvent::ConnectionStateChanged {
                command_id: None,
                connection_id,
                result: ConnectionStateResult::Err { .. },
            } => *connection_id == expected,
            BleEvent::CommandCompleted { command_id, .. } => {
                *command_id == self.dispatch.command_id
            }
            _ => false,
        }
    }

    fn waiter_result(
        &self,
        connection_id: ConnectionId,
    ) -> Option<Result<WaiterSuccess, WaiterError>> {
        self.dispatch.waiter_result(&self.terminal, || {
            self.domain
                .then_some(WaiterSuccess::Disconnected(connection_id))
        })
    }
}

pub fn connection_lost(event: &BleEvent, expected: ConnectionId) -> bool {
    matches!(
        event,
        BleEvent::ConnectionStateChanged {
            connection_id,
            result: ConnectionStateResult::Ok { state: ConnectionState::Disconnected },
            ..
        } if *connection_id == expected
    ) || matches!(
        event,
        BleEvent::ConnectionStateChanged {
            command_id: None,
            connection_id,
            result: ConnectionStateResult::Err { .. },
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
    buffered: Vec<BleEvent>,
    connection_id: ConnectionId,
    permits: fn(&BleEvent, ConnectionId) -> bool,
) -> DispatchResolution {
    let contradictory = buffered.iter().any(|event| !permits(event, connection_id));
    DispatchResolution {
        replay: buffered
            .into_iter()
            .filter(|event| permits(event, connection_id))
            .collect(),
        effect: if contradictory {
            DispatchEffect::Fault(ReconcileFault::ContradictoryDispatch)
        } else {
            DispatchEffect::None
        },
    }
}
