//! Actor-side command admission, ID allocation, and isolated synchronous dispatch.

use super::*;
use blehub::BleHub;

use crate::waiter::WaiterExpectation;

type CommandReply = Result<(CommandId, WaiterReceiver), WaiterError>;
type ScanReply = Result<(CommandId, ScanSessionId, WaiterReceiver), WaiterError>;
type ConnectionReply = Result<(CommandId, ConnectionId, WaiterReceiver), WaiterError>;
type GattReply = Result<(CommandId, GattSessionId, WaiterReceiver), WaiterError>;

pub(crate) struct BeginInitialize {
    pub(super) context: BeginContext,
}

pub(crate) struct BeginScan {
    pub(super) context: BeginContext,
    pub(super) options: ScanOptions,
}

pub(crate) struct BeginStop {
    pub(super) context: BeginContext,
    pub(super) session: ScanSessionId,
}

pub(crate) struct BeginConnect {
    pub(super) context: BeginContext,
    pub(super) peripheral_id: PeripheralId,
}

pub(crate) struct BeginDisconnect {
    pub(super) context: BeginContext,
    pub(super) connection: ConnectionId,
}

pub(crate) struct BeginDiscoverGatt {
    pub(super) context: BeginContext,
    pub(super) connection: ConnectionId,
}

pub(crate) struct BeginRssi {
    pub(super) context: BeginContext,
    pub(super) connection: ConnectionId,
}

pub(crate) struct BeginRead {
    pub(super) context: BeginContext,
    pub(super) connection: ConnectionId,
    pub(super) session: GattSessionId,
    pub(super) characteristic: GattCharacteristicId,
}

pub(crate) struct BeginWrite {
    pub(super) context: BeginContext,
    pub(super) connection: ConnectionId,
    pub(super) session: GattSessionId,
    pub(super) characteristic: GattCharacteristicId,
    pub(super) value: Vec<u8>,
    pub(super) write_type: WriteType,
}

pub(crate) struct RetireWaiter {
    pub(super) id: CommandId,
    pub(super) timed_out: bool,
}

impl Message<RetireWaiter> for BlengActor {
    type Reply = ();

    async fn handle(&mut self, message: RetireWaiter, _: &mut Context<Self, Self::Reply>) {
        if message.timed_out {
            self.waiters.timeout(message.id);
        } else {
            self.waiters.cancel(message.id);
        }
    }
}

// The rollback guard itself lives in state.rs. This dispatch-side extension
// stays here because it owns the Kameo context, blocking pool, and dispatch
// acknowledgement path; moving it to state.rs would mix state mutation with
// native command transport.
impl ReservationRollback<'_> {
    /// Commits the reservation handoff and starts the synchronous Hub call on
    /// Tokio's blocking pool. The guard remains armed until `try_launch` wins;
    /// a failed handoff therefore drops the reservation and waiter together.
    pub(super) fn launch_dispatch<R: kameo::Reply>(
        self,
        actor_context: &Context<BlengActor, R>,
        begin_context: &BeginContext,
        dispatch: impl FnOnce(Arc<BleHub>) -> Result<(), CommandDispatchError> + Send + 'static,
    ) -> Result<(), WaiterError> {
        let hub = Arc::clone(&begin_context.hub);
        if !begin_context.try_launch() {
            return Err(WaiterError::Cancelled);
        }
        let command_id = self.command_id();
        let status = self.commit();
        let actor = actor_context.actor_ref().clone();
        tokio::task::spawn_blocking(move || {
            match catch_unwind(AssertUnwindSafe(|| dispatch(hub))) {
                Ok(result) => {
                    if actor
                        .tell(DispatchResolved {
                            id: command_id,
                            result,
                        })
                        .try_send()
                        .is_err()
                        && status.phase() == RuntimePhase::Open
                    {
                        status.fault();
                    }
                }
                Err(_) => status.fault(),
            }
        });
        Ok(())
    }
}

/// Validate GATT I/O admission against actor-owned connection state.
///
/// This is a pure actor-side predicate: callers pass the serialized connection
/// map explicitly, so the owner facade never reads actor reservations directly.
pub(super) fn validate_gatt_io(
    connections: &HashMap<PeripheralId, ConnectionReservation>,
    connection_id: ConnectionId,
    session_id: GattSessionId,
    characteristic_id: GattCharacteristicId,
    read: bool,
) -> Result<PeripheralId, WaiterError> {
    let (peripheral_id, connection) = connections
        .iter()
        .find(|(_, connection)| connection.connection_id == connection_id)
        .ok_or(WaiterError::ConnectionNotActive)?;
    if !connection.connected || connection.disconnected || connection.failed {
        return Err(WaiterError::ConnectionNotActive);
    }
    if connection.disconnect.is_some()
        || connection
            .rssi
            .as_ref()
            .is_some_and(|slot| slot.dispatch.is_pending())
    {
        return Err(WaiterError::OperationBusy);
    }
    let database = connection
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
    if connection.read.is_some()
        || connection.write.is_some()
        || connection
            .subscriptions
            .values()
            .any(|subscription| subscription.transition.is_some())
        || (read
            && connection
                .subscriptions
                .values()
                .any(|subscription| subscription.characteristic_id == characteristic_id))
    {
        return Err(WaiterError::OperationBusy);
    }
    if connection
        .gatt
        .as_ref()
        .is_some_and(GattReservation::has_pending_discovery)
    {
        return Err(WaiterError::GattBusy);
    }
    Ok(*peripheral_id)
}

impl Message<BeginInitialize> for BlengActor {
    type Reply = CommandReply;

    async fn handle(
        &mut self,
        message: BeginInitialize,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        self.require_open()?;
        if self.initialized || self.init.is_some() {
            return Err(WaiterError::OperationBusy);
        }
        let (id, receiver) = self.register_waiter(WaiterExpectation::Initialized)?;
        self.reserve_initialize(id)
            .launch_dispatch(ctx, &message.context, move |hub| {
                hub.initialize(InitializeCommand { command_id: id })
            })?;
        Ok((id, receiver))
    }
}

impl Message<BeginScan> for BlengActor {
    type Reply = ScanReply;

    async fn handle(
        &mut self,
        message: BeginScan,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        self.require_open()?;
        self.require_initialized()?;
        if self.scan.is_some() {
            return Err(WaiterError::ScanBusy);
        }
        let (id, session, receiver) = self.register_waiter_with_following_id(|_, session| {
            WaiterExpectation::ScanStarted(session)
        })?;
        self.reserve_scan_start(id, session).launch_dispatch(
            ctx,
            &message.context,
            move |hub| {
                let command = StartScanCommand {
                    command_id: id,
                    scan_session_id: session,
                    options: message.options,
                };
                hub.start_scan(command)
            },
        )?;
        Ok((id, session, receiver))
    }
}

impl Message<BeginConnect> for BlengActor {
    type Reply = ConnectionReply;

    async fn handle(
        &mut self,
        message: BeginConnect,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        self.require_open()?;
        self.require_initialized()?;
        if self.connections.contains_key(&message.peripheral_id) {
            return Err(WaiterError::PeripheralBusy);
        }
        let (command_id, connection_id, receiver) =
            self.register_waiter_with_following_id(|_, connection_id| {
                WaiterExpectation::Connected(connection_id)
            })?;
        self.reserve_connection(message.peripheral_id, command_id, connection_id)
            .launch_dispatch(ctx, &message.context, move |hub| {
                let command = ConnectCommand {
                    command_id,
                    connection_id,
                    peripheral_id: message.peripheral_id,
                };
                hub.connect(command)
            })?;
        Ok((command_id, connection_id, receiver))
    }
}

impl Message<BeginDisconnect> for BlengActor {
    type Reply = CommandReply;

    async fn handle(
        &mut self,
        message: BeginDisconnect,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        self.require_open()?;
        self.require_initialized()?;
        let key = self
            .connections
            .iter()
            .find_map(|(key, connection)| {
                (connection.connection_id == message.connection).then_some(*key)
            })
            .ok_or(WaiterError::ConnectionNotActive)?;
        let connection = &self.connections[&key];
        // An accepted connect may still be awaiting its Connected domain event;
        // backend logical ownership already exists and can be disconnected.
        if connection.disconnected || connection.failed || connection.dispatch.is_pending() {
            return Err(WaiterError::ConnectionNotActive);
        }
        if connection.disconnect.is_some()
            || connection
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
                .is_some_and(|slot| slot.dispatch.is_pending())
            || connection
                .write
                .as_ref()
                .is_some_and(|slot| slot.dispatch.is_pending())
            || connection.subscriptions.values().any(|route| {
                route
                    .transition
                    .as_ref()
                    .is_some_and(|slot| slot.dispatch.is_pending())
            })
        {
            // A pre-dispatch sibling must first learn whether its own command
            // was accepted, so a passive loss cannot strand its waiter.
            return Err(WaiterError::OperationBusy);
        }
        let (id, receiver) =
            self.register_waiter(WaiterExpectation::Disconnected(message.connection))?;
        self.reserve_disconnect(key, id)
            .launch_dispatch(ctx, &message.context, move |hub| {
                hub.disconnect(DisconnectCommand {
                    command_id: id,
                    connection_id: message.connection,
                })
            })?;
        Ok((id, receiver))
    }
}

impl Message<BeginDiscoverGatt> for BlengActor {
    type Reply = GattReply;

    async fn handle(
        &mut self,
        message: BeginDiscoverGatt,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        self.require_open()?;
        self.require_initialized()?;
        let connection_key = self
            .connections
            .iter()
            .find(|(_, connection)| connection.connection_id == message.connection)
            .map(|(peripheral_id, _)| *peripheral_id)
            .ok_or(WaiterError::ConnectionNotActive)?;
        {
            let connection = self
                .connections
                .get(&connection_key)
                .expect("matching connection exists");
            if !connection.connected || connection.disconnected || connection.failed {
                return Err(WaiterError::ConnectionNotActive);
            }
            if connection.disconnect.is_some()
                || connection
                    .rssi
                    .as_ref()
                    .is_some_and(|slot| slot.dispatch.is_pending())
                || connection
                    .gatt
                    .as_ref()
                    .is_some_and(GattReservation::has_pending_discovery)
                || connection.read.is_some()
                || connection.write.is_some()
                || connection
                    .subscriptions
                    .values()
                    .any(|subscription| subscription.transition.is_some())
            {
                return Err(WaiterError::GattBusy);
            }
        }
        let (command_id, session_id, receiver) =
            self.register_waiter_with_following_id(|_, session_id| {
                WaiterExpectation::GattDiscovered(message.connection, session_id)
            })?;
        self.reserve_gatt_discovery(connection_key, command_id, session_id)
            .launch_dispatch(ctx, &message.context, move |hub| {
                let command = DiscoverGattCommand {
                    command_id,
                    connection_id: message.connection,
                    gatt_session_id: session_id,
                };
                hub.discover_gatt(command)
            })?;
        Ok((command_id, session_id, receiver))
    }
}

impl Message<BeginRssi> for BlengActor {
    type Reply = CommandReply;

    async fn handle(
        &mut self,
        message: BeginRssi,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        self.require_open()?;
        self.require_initialized()?;
        let key = self
            .connections
            .iter()
            .find_map(|(key, connection)| {
                (connection.connection_id == message.connection).then_some(*key)
            })
            .ok_or(WaiterError::ConnectionNotActive)?;
        let connection = &self.connections[&key];
        if !connection.connected || connection.disconnected || connection.failed {
            return Err(WaiterError::ConnectionNotActive);
        }
        if connection.rssi.is_some()
            || connection.disconnect.is_some()
            || connection
                .gatt
                .as_ref()
                .and_then(|gatt| gatt.discovery.as_ref())
                .is_some_and(|discovery| discovery.dispatch.is_pending())
            || connection
                .read
                .as_ref()
                .is_some_and(|slot| slot.dispatch.is_pending())
            || connection
                .write
                .as_ref()
                .is_some_and(|slot| slot.dispatch.is_pending())
            || connection.subscriptions.values().any(|route| {
                route
                    .transition
                    .as_ref()
                    .is_some_and(|slot| slot.dispatch.is_pending())
            })
        {
            return Err(WaiterError::OperationBusy);
        }
        let (id, receiver) =
            self.register_waiter(WaiterExpectation::RssiRead(message.connection))?;
        self.reserve_rssi(key, id)
            .launch_dispatch(ctx, &message.context, move |hub| {
                hub.read_rssi(ReadRssiCommand {
                    command_id: id,
                    connection_id: message.connection,
                })
            })?;
        Ok((id, receiver))
    }
}

impl Message<BeginRead> for BlengActor {
    type Reply = CommandReply;

    async fn handle(
        &mut self,
        message: BeginRead,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        self.require_open()?;
        self.require_initialized()?;
        let connection_key = validate_gatt_io(
            &self.connections,
            message.connection,
            message.session,
            message.characteristic,
            true,
        )?;
        let (command_id, receiver) =
            self.register_waiter(WaiterExpectation::CharacteristicRead(
                message.connection,
                message.session,
                message.characteristic,
            ))?;
        self.reserve_read(
            connection_key,
            command_id,
            message.session,
            message.characteristic,
        )
        .launch_dispatch(ctx, &message.context, move |hub| {
            let command = ReadCharacteristicCommand {
                command_id,
                connection_id: message.connection,
                gatt_session_id: message.session,
                characteristic_id: message.characteristic,
            };
            hub.read_characteristic(command)
        })?;
        Ok((command_id, receiver))
    }
}

impl Message<BeginWrite> for BlengActor {
    type Reply = CommandReply;

    async fn handle(
        &mut self,
        message: BeginWrite,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        self.require_open()?;
        self.require_initialized()?;
        let connection_key = validate_gatt_io(
            &self.connections,
            message.connection,
            message.session,
            message.characteristic,
            false,
        )?;
        let (command_id, receiver) =
            self.register_waiter(WaiterExpectation::CharacteristicWritten(
                message.connection,
                message.session,
                message.characteristic,
            ))?;
        self.reserve_write(
            connection_key,
            command_id,
            message.session,
            message.characteristic,
        )
        .launch_dispatch(ctx, &message.context, move |hub| {
            let command = WriteCharacteristicCommand {
                command_id,
                connection_id: message.connection,
                gatt_session_id: message.session,
                characteristic_id: message.characteristic,
                value: message.value,
                write_type: message.write_type,
            };
            hub.write_characteristic(command)
        })?;
        Ok((command_id, receiver))
    }
}

impl Message<BeginStop> for BlengActor {
    type Reply = CommandReply;

    async fn handle(
        &mut self,
        message: BeginStop,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        self.require_open()?;
        self.require_initialized()?;
        let scan = self.scan.as_ref().ok_or(WaiterError::ScanNotActive)?;
        if scan.session_id != message.session || scan.failed || scan.stopped {
            return Err(WaiterError::ScanNotActive);
        }
        // Do not issue a native stop while start dispatch or its domain/terminal
        // completion is still unresolved. A future slice can model deferred stop.
        if scan.dispatch.is_pending()
            || scan.start_terminal.is_none()
            || !scan.started
            || scan.stop.is_some()
        {
            return Err(WaiterError::ScanBusy);
        }
        let (id, receiver) =
            self.register_waiter(WaiterExpectation::ScanStopped(message.session))?;
        self.reserve_scan_stop(id)
            .launch_dispatch(ctx, &message.context, move |hub| {
                let command = StopScanCommand {
                    command_id: id,
                    scan_session_id: message.session,
                };
                hub.stop_scan(command)
            })?;
        Ok((id, receiver))
    }
}
