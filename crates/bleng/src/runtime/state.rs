//! Actor-owned runtime state and correlation reservations.

use std::{collections::HashMap, sync::Arc};

use blehub::{
    AdapterSnapshot, BleEvent, BleEventError, CommandId, ConnectionId, GattCharacteristicId,
    GattDatabase, GattSessionId, NotificationAction, NotificationMode, NotificationModeRequest,
    NotificationState, PeripheralId, ScanSessionId, SubscriptionId,
};
use kameo::prelude::*;
use tokio::sync::{mpsc, oneshot};

use crate::waiter::{
    WaiterCloseReason, WaiterError, WaiterExpectation, WaiterRegistry, WaiterSuccess,
};

use super::{RuntimePhase, RuntimeStatus};

/// Single serialized owner of BLeng orchestration state.
///
/// Native callbacks arrive through the actor mailbox, while every reservation
/// below records the correlation state needed to reconcile dispatch, domain,
/// terminal, and late events without exposing runtime state through UniFFI.
#[derive(Actor)]
pub(crate) struct BlengActor {
    pub(super) events: mpsc::Sender<BleEvent>,
    pub(super) status: Arc<RuntimeStatus>,
    pub(super) waiters: WaiterRegistry,
    pub(super) next_id: u64,
    pub(super) initialized: bool,
    pub(super) init: Option<InitReservation>,
    pub(super) scan: Option<ScanReservation>,
    pub(super) connections: HashMap<PeripheralId, ConnectionReservation>,
}

impl Drop for BlengActor {
    fn drop(&mut self) {
        if self.status.phase() == RuntimePhase::Open {
            // An unexpected actor stop must retire the Hub, not restart it
            // with stale native state or leave accepted waiters hanging.
            self.status.fault();
        }
        let reason = if self.status.phase() == RuntimePhase::Faulted {
            WaiterCloseReason::TransportFault
        } else {
            WaiterCloseReason::Closed
        };
        self.waiters.close(reason);
    }
}

#[derive(Clone, Copy)]
enum ConnectionRollback {
    RemoveConnection,
    Disconnect,
    GattDiscovery,
    Rssi,
    Read,
    Write,
    Notification {
        subscription: SubscriptionId,
        remove_subscription: bool,
    },
}

/// Actor-side RAII guard for the short handoff window before native dispatch.
/// Long-lived reservations remain owned by the actor event state machine after
/// this guard is committed; they are not cleaned up by `Drop`.
pub(crate) struct ReservationRollback<'actor> {
    actor: &'actor mut BlengActor,
    command_id: CommandId,
    armed: bool,
}

impl<'actor> ReservationRollback<'actor> {
    pub(crate) fn new(actor: &'actor mut BlengActor, command_id: CommandId) -> Self {
        Self {
            actor,
            command_id,
            armed: true,
        }
    }

    pub(crate) fn command_id(&self) -> CommandId {
        self.command_id
    }

    /// Commits the reservation and returns the shared lifecycle status needed
    /// by the detached native-dispatch task.
    pub(crate) fn commit(mut self) -> Arc<RuntimeStatus> {
        self.armed = false;
        Arc::clone(&self.actor.status)
    }
}

// The state-side guard and rollback semantics live here. Its dispatch-side
// `launch_dispatch` extension is intentionally implemented in commands.rs,
// where Kameo and native transport concerns are already owned.
impl Drop for ReservationRollback<'_> {
    fn drop(&mut self) {
        if self.armed {
            self.actor.rollback_reservation(self.command_id);
            self.actor.waiters.cancel(self.command_id);
        }
    }
}

pub(crate) type WaiterReceiver = oneshot::Receiver<Result<WaiterSuccess, WaiterError>>;

impl BlengActor {
    pub(super) fn require_open(&self) -> Result<(), WaiterError> {
        match self.status.phase() {
            RuntimePhase::Closed => Err(WaiterError::Closed),
            RuntimePhase::Faulted => Err(WaiterError::TransportFault),
            RuntimePhase::Open => Ok(()),
        }
    }

    /// Settles a caller waiter from the authoritative reservation state.
    ///
    /// The reservation may outlive the waiter after timeout or cancellation;
    /// this method is therefore intentionally a no-op when no result is ready
    /// or when the waiter has already retired.
    pub(super) fn settle_waiter(&mut self, command_id: CommandId) {
        if let Some(result) = self.waiter_result(command_id) {
            self.waiters.finish(command_id, result);
        }
    }

    /// Fails accepted connection-bound waiters after a passive loss. Commands
    /// still awaiting dispatch acknowledgement are excluded; their buffered
    /// callback is replayed only after dispatch resolves.
    pub(super) fn fail_connection_waiters(&mut self, connection_id: ConnectionId) {
        let Some(connection) = self
            .connections
            .values()
            .find(|connection| connection.connection_id == connection_id)
        else {
            return;
        };
        let mut command_ids = Vec::new();
        if !connection.dispatch.is_pending() {
            command_ids.push(connection.dispatch.command_id);
        }
        for slot in [
            connection.disconnect.as_ref().map(|slot| &slot.dispatch),
            connection.rssi.as_ref().map(|slot| &slot.dispatch),
            connection.read.as_ref().map(|slot| &slot.dispatch),
            connection.write.as_ref().map(|slot| &slot.dispatch),
        ]
        .into_iter()
        .flatten()
        .filter(|dispatch| !dispatch.is_pending())
        {
            command_ids.push(slot.command_id);
        }
        if let Some(discovery) = connection
            .gatt
            .as_ref()
            .and_then(|gatt| gatt.discovery.as_ref())
            .map(|discovery| &discovery.dispatch)
            .filter(|dispatch| !dispatch.is_pending())
        {
            command_ids.push(discovery.command_id);
        }
        command_ids.extend(
            connection
                .subscriptions
                .values()
                .filter_map(|route| route.transition.as_ref())
                .map(|transition| &transition.dispatch)
                .filter(|dispatch| !dispatch.is_pending())
                .map(|dispatch| dispatch.command_id),
        );
        self.waiters.disconnect_commands(command_ids);
    }

    pub(super) fn require_initialized(&self) -> Result<(), WaiterError> {
        if self.initialized {
            Ok(())
        } else {
            Err(WaiterError::NotInitialized)
        }
    }

    pub(super) fn register_waiter(
        &mut self,
        expectation: WaiterExpectation,
    ) -> Result<(CommandId, WaiterReceiver), WaiterError> {
        let next_id = self
            .next_id
            .checked_add(1)
            .ok_or(WaiterError::IdExhausted)?;
        let id: CommandId = self.next_id.into();
        let receiver = self.waiters.register(id, expectation)?;
        self.next_id = next_id;
        Ok((id, receiver))
    }

    /// Registers a waiter whose operation also needs the immediately following
    /// monotonic ID, such as a scan session, connection, or subscription.
    ///
    /// The two IDs and the waiter are committed together: if waiter admission
    /// fails, the actor counter is left untouched. The secondary value is
    /// converted once into the operation's typed ID, shared by its expectation
    /// and reservation.
    pub(super) fn register_waiter_with_following_id<Id: From<u64> + Copy>(
        &mut self,
        expectation: impl FnOnce(CommandId, Id) -> WaiterExpectation,
    ) -> Result<(CommandId, Id, WaiterReceiver), WaiterError> {
        let secondary_value = self
            .next_id
            .checked_add(1)
            .ok_or(WaiterError::IdExhausted)?;
        let next_id = secondary_value
            .checked_add(1)
            .ok_or(WaiterError::IdExhausted)?;
        let command_id: CommandId = self.next_id.into();
        let secondary_id = secondary_value.into();
        let receiver = self
            .waiters
            .register(command_id, expectation(command_id, secondary_id))?;
        self.next_id = next_id;
        Ok((command_id, secondary_id, receiver))
    }

    /// Registers an initialization operation before its native dispatch.
    pub(super) fn reserve_initialize(&mut self, command_id: CommandId) -> ReservationRollback<'_> {
        self.init = Some(InitReservation {
            dispatch: DispatchReservation::new(command_id),
            snapshot: None,
            terminal: None,
        });
        ReservationRollback::new(self, command_id)
    }

    /// Registers a scan session before its native start dispatch.
    pub(super) fn reserve_scan_start(
        &mut self,
        command_id: CommandId,
        session_id: ScanSessionId,
    ) -> ReservationRollback<'_> {
        self.scan = Some(ScanReservation {
            dispatch: DispatchReservation::new(command_id),
            session_id,
            failed: false,
            stopped: false,
            started: false,
            start_terminal: None,
            stop: None,
        });
        ReservationRollback::new(self, command_id)
    }

    /// Registers a connection before its native connect dispatch.
    pub(super) fn reserve_connection(
        &mut self,
        peripheral_id: PeripheralId,
        command_id: CommandId,
        connection_id: ConnectionId,
    ) -> ReservationRollback<'_> {
        self.connections.insert(
            peripheral_id,
            ConnectionReservation {
                dispatch: DispatchReservation::new(command_id),
                connection_id,
                connected: false,
                failed: false,
                disconnected: false,
                terminal: None,
                connect_error: None,
                disconnect: None,
                gatt: None,
                read: None,
                rssi: None,
                write: None,
                subscriptions: HashMap::new(),
            },
        );
        ReservationRollback::new(self, command_id)
    }

    /// Registers a disconnect transition on an existing connection.
    pub(super) fn reserve_disconnect(
        &mut self,
        peripheral_id: PeripheralId,
        command_id: CommandId,
    ) -> ReservationRollback<'_> {
        self.connection_mut(peripheral_id).disconnect = Some(DisconnectReservation {
            dispatch: DispatchReservation::new(command_id),
            domain: false,
            terminal: None,
        });
        ReservationRollback::new(self, command_id)
    }

    /// Registers a GATT discovery transition while preserving any committed database.
    pub(super) fn reserve_gatt_discovery(
        &mut self,
        peripheral_id: PeripheralId,
        command_id: CommandId,
        session_id: GattSessionId,
    ) -> ReservationRollback<'_> {
        self.connection_mut(peripheral_id)
            .gatt
            .get_or_insert_with(GattReservation::new)
            .begin_discovery(command_id, session_id);
        ReservationRollback::new(self, command_id)
    }

    /// Registers a pending RSSI read on an existing connection.
    pub(super) fn reserve_rssi(
        &mut self,
        peripheral_id: PeripheralId,
        command_id: CommandId,
    ) -> ReservationRollback<'_> {
        self.connection_mut(peripheral_id).rssi = Some(RssiReservation {
            dispatch: DispatchReservation::new(command_id),
            value: None,
            terminal: None,
        });
        ReservationRollback::new(self, command_id)
    }

    /// Registers a pending characteristic read.
    pub(super) fn reserve_read(
        &mut self,
        peripheral_id: PeripheralId,
        command_id: CommandId,
        session_id: GattSessionId,
        characteristic_id: GattCharacteristicId,
    ) -> ReservationRollback<'_> {
        self.connection_mut(peripheral_id).read = Some(ReadReservation {
            dispatch: DispatchReservation::new(command_id),
            session_id,
            characteristic_id,
            value: None,
            terminal: None,
        });
        ReservationRollback::new(self, command_id)
    }

    /// Registers a pending characteristic write.
    pub(super) fn reserve_write(
        &mut self,
        peripheral_id: PeripheralId,
        command_id: CommandId,
        session_id: GattSessionId,
        characteristic_id: GattCharacteristicId,
    ) -> ReservationRollback<'_> {
        self.connection_mut(peripheral_id).write = Some(WriteReservation {
            dispatch: DispatchReservation::new(command_id),
            session_id,
            characteristic_id,
            written: false,
            terminal: None,
        });
        ReservationRollback::new(self, command_id)
    }

    /// Registers the stop transition for the active scan session.
    pub(super) fn reserve_scan_stop(&mut self, command_id: CommandId) -> ReservationRollback<'_> {
        self.scan.as_mut().expect("validated active scan").stop = Some(StopReservation {
            dispatch: DispatchReservation::new(command_id),
            terminal: None,
            stop_error: None,
        });
        ReservationRollback::new(self, command_id)
    }

    /// Registers a GATT discovery transition
    /// Registers a notification enable transition and its new subscription.
    pub(super) fn reserve_notification_enable(
        &mut self,
        peripheral_id: PeripheralId,
        command_id: CommandId,
        subscription_id: SubscriptionId,
        session_id: GattSessionId,
        characteristic_id: GattCharacteristicId,
        mode: NotificationModeRequest,
    ) -> ReservationRollback<'_> {
        self.connection_mut(peripheral_id).subscriptions.insert(
            subscription_id,
            SubscriptionReservation {
                session_id,
                characteristic_id,
                actual_mode: None,
                active: false,
                transition: Some(NotificationTransition {
                    dispatch: DispatchReservation::new(command_id),
                    action: NotificationAction::Enable { mode },
                    domain: None,
                    terminal: None,
                }),
            },
        );
        ReservationRollback::new(self, command_id)
    }

    /// Registers a notification disable transition on an active subscription.
    pub(super) fn reserve_notification_disable(
        &mut self,
        peripheral_id: PeripheralId,
        command_id: CommandId,
        subscription_id: SubscriptionId,
    ) -> ReservationRollback<'_> {
        self.connection_mut(peripheral_id)
            .subscriptions
            .get_mut(&subscription_id)
            .expect("validated subscription")
            .transition = Some(NotificationTransition {
            dispatch: DispatchReservation::new(command_id),
            action: NotificationAction::Disable,
            domain: None,
            terminal: None,
        });
        ReservationRollback::new(self, command_id)
    }

    fn connection_mut(&mut self, peripheral_id: PeripheralId) -> &mut ConnectionReservation {
        self.connections
            .get_mut(&peripheral_id)
            .expect("validated connection")
    }

    /// Records the one terminal callback identified by `command_id`.
    /// Command IDs are globally unique within this actor, so terminal routing
    /// belongs here rather than being duplicated across every event branch.
    pub(super) fn record_terminal(&mut self, command_id: CommandId, error: Option<BleEventError>) {
        let result = error.map_or(Ok(()), Err);
        if let Some(init) = self
            .init
            .as_mut()
            .filter(|init| init.dispatch.command_id == command_id)
            && init.terminal.is_none()
        {
            init.terminal = Some(result);
            return;
        }
        if let Some(scan) = self.scan.as_mut() {
            if scan.dispatch.command_id == command_id {
                if scan.start_terminal.is_none() {
                    scan.failed = result.is_err();
                    scan.start_terminal = Some(result);
                }
                return;
            }
            if let Some(stop) = scan
                .stop
                .as_mut()
                .filter(|stop| stop.dispatch.command_id == command_id)
                && stop.terminal.is_none()
            {
                stop.terminal = Some(result);
                return;
            }
        }
        for connection in self.connections.values_mut() {
            if connection.dispatch.command_id == command_id {
                if connection.terminal.is_none() {
                    connection.terminal = Some(result);
                }
                return;
            }
            if let Some(disconnect) = connection
                .disconnect
                .as_mut()
                .filter(|disconnect| disconnect.dispatch.command_id == command_id)
                && disconnect.terminal.is_none()
            {
                disconnect.terminal = Some(result);
                return;
            }
            if let Some(discovery) = connection
                .gatt
                .as_mut()
                .and_then(|gatt| gatt.discovery.as_mut())
                .filter(|discovery| discovery.dispatch.command_id == command_id)
                && discovery.terminal.is_none()
            {
                discovery.failed = result.is_err();
                discovery.terminal = Some(result);
                return;
            }
            if let Some(rssi) = connection
                .rssi
                .as_mut()
                .filter(|rssi| rssi.dispatch.command_id == command_id)
                && rssi.terminal.is_none()
            {
                rssi.terminal = Some(result);
                return;
            }
            if let Some(read) = connection
                .read
                .as_mut()
                .filter(|read| read.dispatch.command_id == command_id)
                && read.terminal.is_none()
            {
                read.terminal = Some(result);
                return;
            }
            if let Some(write) = connection
                .write
                .as_mut()
                .filter(|write| write.dispatch.command_id == command_id)
                && write.terminal.is_none()
            {
                write.terminal = Some(result);
                return;
            }
            if let Some(transition) = connection
                .subscriptions
                .values_mut()
                .filter_map(|route| route.transition.as_mut())
                .find(|transition| transition.dispatch.command_id == command_id)
                && transition.terminal.is_none()
            {
                transition.terminal = Some(result);
                return;
            }
        }
    }

    fn waiter_result(&self, command_id: CommandId) -> Option<Result<WaiterSuccess, WaiterError>> {
        if let Some(init) = self
            .init
            .as_ref()
            .filter(|init| init.dispatch.command_id == command_id)
        {
            return init.waiter_result();
        }
        if let Some(scan) = self.scan.as_ref() {
            if scan.dispatch.command_id == command_id {
                return scan.start_result();
            }
            if let Some(stop) = scan
                .stop
                .as_ref()
                .filter(|stop| stop.dispatch.command_id == command_id)
            {
                return stop.waiter_result(scan.session_id, scan.stopped);
            }
        }
        for connection in self.connections.values() {
            if connection.dispatch.command_id == command_id {
                return connection.connect_result();
            }
            if let Some(disconnect) = connection
                .disconnect
                .as_ref()
                .filter(|disconnect| disconnect.dispatch.command_id == command_id)
            {
                return disconnect.waiter_result(connection.connection_id);
            }
            if let Some(gatt) = connection
                .gatt
                .as_ref()
                .and_then(|gatt| gatt.discovery.as_ref())
                .filter(|gatt| gatt.dispatch.command_id == command_id)
            {
                return gatt.waiter_result();
            }
            if let Some(rssi) = connection
                .rssi
                .as_ref()
                .filter(|rssi| rssi.dispatch.command_id == command_id)
            {
                return rssi.waiter_result(connection.connection_id);
            }
            if let Some(read) = connection
                .read
                .as_ref()
                .filter(|read| read.dispatch.command_id == command_id)
            {
                return read.waiter_result(connection.connection_id);
            }
            if let Some(write) = connection
                .write
                .as_ref()
                .filter(|write| write.dispatch.command_id == command_id)
            {
                return write.waiter_result(connection.connection_id);
            }
            if let Some((subscription_id, route, transition)) = connection
                .subscriptions
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
                    connection.connection_id,
                    subscription_id,
                    route.session_id,
                    route.characteristic_id,
                );
            }
        }
        None
    }

    /// Removes the short-lived reservation created for `command_id` when the
    /// caller drops the operation before native dispatch is launched. The
    /// command ID is unique across all actor reservations, so this state-level
    /// rollback keeps command handlers free of container-specific cleanup.
    pub(super) fn rollback_reservation(&mut self, command_id: CommandId) {
        if self
            .init
            .as_ref()
            .is_some_and(|reservation| reservation.dispatch.command_id == command_id)
        {
            self.init = None;
            return;
        }
        if let Some(scan) = self.scan.as_mut() {
            if scan.dispatch.command_id == command_id {
                self.scan = None;
                return;
            }
            if scan
                .stop
                .as_ref()
                .is_some_and(|stop| stop.dispatch.command_id == command_id)
            {
                scan.stop = None;
                return;
            }
        }

        let target = self
            .connections
            .iter()
            .find_map(|(peripheral_id, connection)| {
                let rollback = if connection.dispatch.command_id == command_id {
                    Some(ConnectionRollback::RemoveConnection)
                } else if connection
                    .disconnect
                    .as_ref()
                    .is_some_and(|slot| slot.dispatch.command_id == command_id)
                {
                    Some(ConnectionRollback::Disconnect)
                } else if connection
                    .gatt
                    .as_ref()
                    .and_then(|gatt| gatt.discovery.as_ref())
                    .is_some_and(|discovery| discovery.dispatch.command_id == command_id)
                {
                    Some(ConnectionRollback::GattDiscovery)
                } else if connection
                    .rssi
                    .as_ref()
                    .is_some_and(|slot| slot.dispatch.command_id == command_id)
                {
                    Some(ConnectionRollback::Rssi)
                } else if connection
                    .read
                    .as_ref()
                    .is_some_and(|slot| slot.dispatch.command_id == command_id)
                {
                    Some(ConnectionRollback::Read)
                } else if connection
                    .write
                    .as_ref()
                    .is_some_and(|slot| slot.dispatch.command_id == command_id)
                {
                    Some(ConnectionRollback::Write)
                } else {
                    connection
                        .subscriptions
                        .iter()
                        .find_map(|(subscription, route)| {
                            route
                                .transition
                                .as_ref()
                                .filter(|transition| transition.dispatch.command_id == command_id)
                                .map(|transition| ConnectionRollback::Notification {
                                    subscription: *subscription,
                                    remove_subscription: matches!(
                                        &transition.action,
                                        NotificationAction::Enable { .. }
                                    ),
                                })
                        })
                };
                rollback.map(|rollback| (*peripheral_id, rollback))
            });

        let Some((peripheral_id, rollback)) = target else {
            return;
        };
        match rollback {
            ConnectionRollback::RemoveConnection => {
                self.connections.remove(&peripheral_id);
            }
            ConnectionRollback::Disconnect => {
                self.connections
                    .get_mut(&peripheral_id)
                    .expect("matching connection exists")
                    .disconnect = None;
            }
            ConnectionRollback::GattDiscovery => {
                let connection = self
                    .connections
                    .get_mut(&peripheral_id)
                    .expect("matching connection exists");
                let remove_gatt = if let Some(gatt) = connection.gatt.as_mut() {
                    gatt.discovery = None;
                    gatt.database.is_none()
                } else {
                    false
                };
                if remove_gatt {
                    connection.gatt = None;
                }
            }
            ConnectionRollback::Rssi => {
                self.connections
                    .get_mut(&peripheral_id)
                    .expect("matching connection exists")
                    .rssi = None;
            }
            ConnectionRollback::Read => {
                self.connections
                    .get_mut(&peripheral_id)
                    .expect("matching connection exists")
                    .read = None;
            }
            ConnectionRollback::Write => {
                self.connections
                    .get_mut(&peripheral_id)
                    .expect("matching connection exists")
                    .write = None;
            }
            ConnectionRollback::Notification {
                subscription,
                remove_subscription,
            } => {
                let connection = self
                    .connections
                    .get_mut(&peripheral_id)
                    .expect("matching connection exists");
                if remove_subscription {
                    connection.subscriptions.remove(&subscription);
                } else if let Some(route) = connection.subscriptions.get_mut(&subscription) {
                    route.transition = None;
                }
            }
        }
    }
}

/// Shared command-dispatch state embedded by reservations that can receive
/// native events before dispatch acknowledgement.
pub(crate) struct DispatchReservation {
    pub(super) command_id: CommandId,
    pub(super) dispatch_accepted: bool,
    pub(super) buffered: Vec<BleEvent>,
}

impl DispatchReservation {
    pub(super) fn new(command_id: CommandId) -> Self {
        Self {
            command_id,
            dispatch_accepted: false,
            buffered: Vec::new(),
        }
    }

    pub(super) fn is_pending(&self) -> bool {
        !self.dispatch_accepted
    }

    pub(super) fn accept(&mut self) -> Vec<BleEvent> {
        self.dispatch_accepted = true;
        std::mem::take(&mut self.buffered)
    }
}

/// In-flight command records retain events that can arrive before dispatch
/// acknowledgement; the waiter is settled only after its required evidence is complete.
pub(crate) struct InitReservation {
    pub(super) dispatch: DispatchReservation,
    pub(super) snapshot: Option<AdapterSnapshot>,
    pub(super) terminal: Option<Result<(), BleEventError>>,
}

/// Scan state is session-scoped and remains retained until start/stop cleanup
/// has accounted for both accepted and late native callbacks.
pub(crate) struct ScanReservation {
    pub(super) dispatch: DispatchReservation,
    pub(super) session_id: ScanSessionId,
    pub(super) failed: bool,
    pub(super) stopped: bool,
    pub(super) started: bool,
    pub(super) start_terminal: Option<Result<(), BleEventError>>,
    pub(super) stop: Option<StopReservation>,
}

pub(crate) struct StopReservation {
    pub(super) dispatch: DispatchReservation,
    pub(super) terminal: Option<Result<(), BleEventError>>,
    pub(super) stop_error: Option<BleEventError>,
}

/// A connection owns its GATT session, I/O slots, and subscriptions.
/// Keeping these under one peripheral key prevents cross-device callback routing.
pub(crate) struct ConnectionReservation {
    pub(super) dispatch: DispatchReservation,
    pub(super) connection_id: ConnectionId,
    pub(super) connected: bool,
    pub(super) failed: bool,
    pub(super) disconnected: bool,
    pub(super) terminal: Option<Result<(), BleEventError>>,
    pub(super) connect_error: Option<BleEventError>,
    pub(super) disconnect: Option<DisconnectReservation>,
    pub(super) gatt: Option<GattReservation>,
    pub(super) read: Option<ReadReservation>,
    pub(super) rssi: Option<RssiReservation>,
    pub(super) write: Option<WriteReservation>,
    pub(super) subscriptions: HashMap<SubscriptionId, SubscriptionReservation>,
}

pub(crate) struct DisconnectReservation {
    pub(super) dispatch: DispatchReservation,
    pub(super) domain: bool,
    pub(super) terminal: Option<Result<(), BleEventError>>,
}

/// GATT state under one connection. A committed database survives while a
/// later discovery transition is in flight, so failed rediscovery cannot erase
/// the last usable session.
pub(crate) struct GattReservation {
    pub(super) database: Option<GattDatabase>,
    pub(super) discovery: Option<GattDiscoveryReservation>,
}

impl GattReservation {
    pub(super) fn new() -> Self {
        Self {
            database: None,
            discovery: None,
        }
    }

    pub(super) fn committed_database(&self, session_id: GattSessionId) -> Option<&GattDatabase> {
        self.database
            .as_ref()
            .filter(|database| database.gatt_session_id == session_id)
    }

    pub(super) fn has_pending_discovery(&self) -> bool {
        self.discovery.is_some()
    }

    pub(super) fn begin_discovery(&mut self, command_id: CommandId, session_id: GattSessionId) {
        self.discovery = Some(GattDiscoveryReservation::new(command_id, session_id));
    }
}

/// One in-flight GATT discovery/rediscovery transition.
pub(crate) struct GattDiscoveryReservation {
    pub(super) dispatch: DispatchReservation,
    pub(super) session_id: GattSessionId,
    pub(super) discovered: bool,
    pub(super) failed: bool,
    pub(super) terminal: Option<Result<(), BleEventError>>,
    /// Database drafted by this discovery; it becomes committed only after
    /// the discovery terminal succeeds.
    pub(super) draft_database: Option<GattDatabase>,
}

impl GattDiscoveryReservation {
    fn new(command_id: CommandId, session_id: GattSessionId) -> Self {
        Self {
            dispatch: DispatchReservation::new(command_id),
            session_id,
            discovered: false,
            failed: false,
            terminal: None,
            draft_database: None,
        }
    }

    pub(super) fn is_retirable(&self) -> bool {
        self.dispatch.dispatch_accepted
            && (self.failed
                || self.terminal.as_ref().is_some_and(Result::is_err)
                || self.is_successful())
    }

    pub(super) fn is_successful(&self) -> bool {
        self.discovered && self.terminal == Some(Ok(()))
    }
}

pub(crate) struct RssiReservation {
    pub(super) dispatch: DispatchReservation,
    pub(super) value: Option<i16>,
    pub(super) terminal: Option<Result<(), BleEventError>>,
}

pub(crate) struct ReadReservation {
    pub(super) dispatch: DispatchReservation,
    pub(super) session_id: GattSessionId,
    pub(super) characteristic_id: GattCharacteristicId,
    pub(super) value: Option<Vec<u8>>,
    pub(super) terminal: Option<Result<(), BleEventError>>,
}

pub(crate) struct WriteReservation {
    pub(super) dispatch: DispatchReservation,
    pub(super) session_id: GattSessionId,
    pub(super) characteristic_id: GattCharacteristicId,
    pub(super) written: bool,
    pub(super) terminal: Option<Result<(), BleEventError>>,
}

/// A subscription is active only after the native CCCD transition and domain
/// event have been reconciled; `transition` represents that pending boundary.
pub(crate) struct SubscriptionReservation {
    pub(super) session_id: GattSessionId,
    pub(super) characteristic_id: GattCharacteristicId,
    pub(super) actual_mode: Option<NotificationMode>,
    pub(super) active: bool,
    pub(super) transition: Option<NotificationTransition>,
}

pub(crate) struct NotificationTransition {
    pub(super) dispatch: DispatchReservation,
    pub(super) action: NotificationAction,
    pub(super) domain: Option<NotificationState>,
    pub(super) terminal: Option<Result<(), BleEventError>>,
}

fn terminal_error(
    terminal: &Option<Result<(), BleEventError>>,
) -> Option<Result<WaiterSuccess, WaiterError>> {
    match terminal {
        Some(Err(error)) => Some(Err(WaiterError::Operation(error.clone()))),
        _ => None,
    }
}

impl InitReservation {
    fn waiter_result(&self) -> Option<Result<WaiterSuccess, WaiterError>> {
        if self.dispatch.is_pending() {
            return None;
        }
        if let Some(result) = terminal_error(&self.terminal) {
            return Some(result);
        }
        (self.terminal == Some(Ok(())))
            .then(|| {
                self.snapshot
                    .clone()
                    .map(|snapshot| Ok(WaiterSuccess::Initialized(snapshot)))
            })
            .flatten()
    }
}

impl ScanReservation {
    fn start_result(&self) -> Option<Result<WaiterSuccess, WaiterError>> {
        if self.dispatch.is_pending() {
            return None;
        }
        if let Some(result) = terminal_error(&self.start_terminal) {
            return Some(result);
        }
        (self.start_terminal == Some(Ok(())) && self.started)
            .then_some(Ok(WaiterSuccess::ScanStarted(self.session_id)))
    }
}

impl StopReservation {
    fn waiter_result(
        &self,
        session_id: ScanSessionId,
        stopped: bool,
    ) -> Option<Result<WaiterSuccess, WaiterError>> {
        if self.dispatch.is_pending() {
            return None;
        }
        if let Some(error) = &self.stop_error {
            return Some(Err(WaiterError::Operation(error.clone())));
        }
        if let Some(result) = terminal_error(&self.terminal) {
            return Some(result);
        }
        (self.terminal == Some(Ok(())) && stopped)
            .then_some(Ok(WaiterSuccess::ScanStopped(session_id)))
    }
}

impl ConnectionReservation {
    fn connect_result(&self) -> Option<Result<WaiterSuccess, WaiterError>> {
        if self.dispatch.is_pending() {
            return None;
        }
        if let Some(error) = &self.connect_error {
            return Some(Err(WaiterError::Operation(error.clone())));
        }
        if self.disconnected {
            return Some(Err(WaiterError::Disconnected));
        }
        if let Some(result) = terminal_error(&self.terminal) {
            return Some(result);
        }
        (self.terminal == Some(Ok(())) && self.connected)
            .then_some(Ok(WaiterSuccess::Connected(self.connection_id)))
    }
}

impl DisconnectReservation {
    fn waiter_result(
        &self,
        connection_id: ConnectionId,
    ) -> Option<Result<WaiterSuccess, WaiterError>> {
        if self.dispatch.is_pending() {
            return None;
        }
        if let Some(result) = terminal_error(&self.terminal) {
            return Some(result);
        }
        (self.terminal == Some(Ok(())) && self.domain)
            .then_some(Ok(WaiterSuccess::Disconnected(connection_id)))
    }
}

impl GattDiscoveryReservation {
    fn waiter_result(&self) -> Option<Result<WaiterSuccess, WaiterError>> {
        if self.dispatch.is_pending() {
            return None;
        }
        if let Some(result) = terminal_error(&self.terminal) {
            return Some(result);
        }
        (self.terminal == Some(Ok(())) && self.discovered)
            .then(|| {
                self.draft_database
                    .clone()
                    .map(|database| Ok(WaiterSuccess::GattDiscovered(database)))
            })
            .flatten()
    }
}

impl RssiReservation {
    fn waiter_result(
        &self,
        connection_id: ConnectionId,
    ) -> Option<Result<WaiterSuccess, WaiterError>> {
        if self.dispatch.is_pending() {
            return None;
        }
        if let Some(result) = terminal_error(&self.terminal) {
            return Some(result);
        }
        (self.terminal == Some(Ok(())))
            .then(|| {
                self.value
                    .map(|value| Ok(WaiterSuccess::RssiRead(connection_id, value)))
            })
            .flatten()
    }
}

impl ReadReservation {
    fn waiter_result(
        &self,
        connection_id: ConnectionId,
    ) -> Option<Result<WaiterSuccess, WaiterError>> {
        if self.dispatch.is_pending() {
            return None;
        }
        if let Some(result) = terminal_error(&self.terminal) {
            return Some(result);
        }
        (self.terminal == Some(Ok(())))
            .then(|| {
                self.value.clone().map(|value| {
                    Ok(WaiterSuccess::CharacteristicRead {
                        connection_id,
                        gatt_session_id: self.session_id,
                        characteristic_id: self.characteristic_id,
                        value,
                    })
                })
            })
            .flatten()
    }
}

impl WriteReservation {
    fn waiter_result(
        &self,
        connection_id: ConnectionId,
    ) -> Option<Result<WaiterSuccess, WaiterError>> {
        if self.dispatch.is_pending() {
            return None;
        }
        if let Some(result) = terminal_error(&self.terminal) {
            return Some(result);
        }
        (self.terminal == Some(Ok(())) && self.written).then_some(Ok(
            WaiterSuccess::CharacteristicWritten(
                connection_id,
                self.session_id,
                self.characteristic_id,
            ),
        ))
    }
}

impl NotificationTransition {
    fn waiter_result(
        &self,
        connection_id: ConnectionId,
        subscription_id: SubscriptionId,
        session_id: GattSessionId,
        characteristic_id: GattCharacteristicId,
    ) -> Option<Result<WaiterSuccess, WaiterError>> {
        if self.dispatch.is_pending() {
            return None;
        }
        if let Some(result) = terminal_error(&self.terminal) {
            return Some(result);
        }
        if self.terminal != Some(Ok(())) {
            return None;
        }
        match self.domain {
            Some(NotificationState::Enabled { actual_mode }) => {
                Some(Ok(WaiterSuccess::NotificationsEnabled {
                    connection_id,
                    gatt_session_id: session_id,
                    subscription_id,
                    characteristic_id,
                    actual_mode,
                }))
            }
            Some(NotificationState::Disabled) => Some(Ok(WaiterSuccess::NotificationsDisabled(
                connection_id,
                session_id,
                subscription_id,
                characteristic_id,
            ))),
            None => None,
        }
    }
}
