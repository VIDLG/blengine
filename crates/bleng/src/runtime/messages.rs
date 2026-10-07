//! Actor mailbox protocol: command requests, asynchronous inputs, and waiter control.
//!
//! `Begin*` requests and `BeginReply` establish caller admission and handoff.
//! `IngressEvent` and `DispatchResolved` carry backend callbacks and synchronous
//! dispatch outcomes. `RetireWaiter` ends caller delivery, not native work.
//! Handlers use actor methods, never actor fields; state reconciliation stays
//! in actor.rs, while deadlines and caller waiting stay in owner.rs.
//!
//! `Begin` requests establish a command's caller-to-actor handoff, install its
//! reservation, and submit native dispatch. Their replies transfer the waiter
//! receiver and any newly allocated resource ID; they do not acknowledge backend
//! acceptance or BLE completion.
//!
//! Command handlers follow the same order: validate, allocate IDs/register the waiter,
//! then call `launch_dispatch` to claim the handoff, install the reservation,
//! and submit native dispatch. Operation-specific validation, waiter expectation,
//! reservation installation, and native command construction remain explicit.
//!
//! Success without a payload is `()`, not an invented acknowledgement object.
//! In particular, backend dispatch `Ok(())` means accepted for dispatch, not BLE
//! completion. `DispatchResolved` carries the full result to the actor; actual
//! operation data and completion arrive through correlated callback evidence.

use blehub::{
    AdapterSnapshot, BleEvent, CommandDispatchError, CommandId, ConnectCommand, ConnectionId,
    DisconnectCommand, DiscoverGattCommand, GattCharacteristicId, GattDatabase, GattSessionId,
    InitializeCommand, NotificationAction, NotificationMode, NotificationModeRequest, PeripheralId,
    ReadCharacteristicCommand, ReadRssiCommand, ScanOptions, ScanSessionId,
    SetNotificationsCommand, StartScanCommand, StopScanCommand, SubscriptionId,
    WriteCharacteristicCommand, WriteType,
};
use kameo::prelude::{Context, Message};

use crate::waiter::{WaiterError, WaiterExpectation, WaiterSuccess};

use super::{
    actor::{BlengActor, WaiterReceiver},
    owner::BeginHandoff,
    reservations::GattAccess,
};

// Command requests and admission replies.

/// Actor-to-owner handoff metadata, not a native dispatch result.
/// `Resource` is the newly allocated typed ID, or `()` for commands creating none.
/// The result receiver remains distinct from the registry's waiting expectation.
pub(super) struct BeginReply<Resource = ()> {
    pub(super) command_id: CommandId,
    pub(super) result_rx: WaiterReceiver,
    pub(super) resource: Resource,
}

/// Ties a Begin request to its admission resource and caller-visible success.
///
/// Correlation is a bounded snapshot of request IDs, not native state or an
/// operation result. The owner extracts it before moving the message into
/// `actor.ask`, without retaining or cloning the request or its payload.
/// Decoding has no lifecycle dependency: the owner handles a rejected success.
pub(super) trait OperationResult: Send + 'static {
    /// Newly allocated ID returned by `BeginReply`, or `()` if none is allocated.
    type Resource: Send + 'static;
    type Output;
    type Correlation: Send;

    fn correlation(&self) -> Self::Correlation;

    /// Checks the success variant and identities, then moves out its payload.
    fn decode_success(
        correlation: Self::Correlation,
        resource: Self::Resource,
        success: WaiterSuccess,
    ) -> Option<Self::Output>;
}

pub(super) struct BeginInitialize {
    pub(super) handoff: BeginHandoff,
}

pub(super) struct BeginScan {
    pub(super) handoff: BeginHandoff,
    pub(super) options: ScanOptions,
}

pub(super) struct BeginStop {
    pub(super) handoff: BeginHandoff,
    pub(super) session: ScanSessionId,
}

pub(super) struct BeginConnect {
    pub(super) handoff: BeginHandoff,
    pub(super) peripheral_id: PeripheralId,
}

pub(super) struct BeginDisconnect {
    pub(super) handoff: BeginHandoff,
    pub(super) connection: ConnectionId,
}

pub(super) struct BeginDiscoverGatt {
    pub(super) handoff: BeginHandoff,
    pub(super) connection: ConnectionId,
}

pub(super) struct BeginRssi {
    pub(super) handoff: BeginHandoff,
    pub(super) connection: ConnectionId,
}

pub(super) struct BeginRead {
    pub(super) handoff: BeginHandoff,
    pub(super) connection: ConnectionId,
    pub(super) session: GattSessionId,
    pub(super) characteristic: GattCharacteristicId,
}

pub(super) struct BeginWrite {
    pub(super) handoff: BeginHandoff,
    pub(super) connection: ConnectionId,
    pub(super) session: GattSessionId,
    pub(super) characteristic: GattCharacteristicId,
    pub(super) value: Vec<u8>,
    pub(super) write_type: WriteType,
}

pub(super) struct BeginEnableNotifications {
    pub(super) handoff: BeginHandoff,
    pub(super) connection: ConnectionId,
    pub(super) session: GattSessionId,
    pub(super) characteristic: GattCharacteristicId,
    pub(super) mode: NotificationModeRequest,
}

pub(super) struct BeginDisableNotifications {
    pub(super) handoff: BeginHandoff,
    pub(super) connection: ConnectionId,
    pub(super) session: GattSessionId,
    pub(super) subscription: SubscriptionId,
}

impl OperationResult for BeginInitialize {
    type Resource = ();
    type Output = AdapterSnapshot;
    type Correlation = ();

    fn correlation(&self) -> Self::Correlation {}

    fn decode_success(
        (): Self::Correlation,
        (): Self::Resource,
        success: WaiterSuccess,
    ) -> Option<Self::Output> {
        match success {
            WaiterSuccess::Initialized(snapshot) => Some(snapshot),
            _ => None,
        }
    }
}

impl OperationResult for BeginScan {
    type Resource = ScanSessionId;
    type Output = ScanSessionId;
    type Correlation = ();

    fn correlation(&self) -> Self::Correlation {}

    fn decode_success(
        (): Self::Correlation,
        resource: Self::Resource,
        success: WaiterSuccess,
    ) -> Option<Self::Output> {
        match success {
            WaiterSuccess::ScanStarted(scan) if scan == resource => Some(scan),
            _ => None,
        }
    }
}

impl OperationResult for BeginStop {
    type Resource = ();
    type Output = ();
    type Correlation = ScanSessionId;

    fn correlation(&self) -> Self::Correlation {
        self.session
    }

    fn decode_success(
        session: Self::Correlation,
        (): Self::Resource,
        success: WaiterSuccess,
    ) -> Option<Self::Output> {
        match success {
            WaiterSuccess::ScanStopped(stopped) if stopped == session => Some(()),
            _ => None,
        }
    }
}

impl OperationResult for BeginConnect {
    type Resource = ConnectionId;
    type Output = ConnectionId;
    type Correlation = ();

    fn correlation(&self) -> Self::Correlation {}

    fn decode_success(
        (): Self::Correlation,
        resource: Self::Resource,
        success: WaiterSuccess,
    ) -> Option<Self::Output> {
        match success {
            WaiterSuccess::Connected(found) if found == resource => Some(found),
            _ => None,
        }
    }
}

impl OperationResult for BeginDisconnect {
    type Resource = ();
    type Output = ();
    type Correlation = ConnectionId;

    fn correlation(&self) -> Self::Correlation {
        self.connection
    }

    fn decode_success(
        connection: Self::Correlation,
        (): Self::Resource,
        success: WaiterSuccess,
    ) -> Option<Self::Output> {
        match success {
            WaiterSuccess::Disconnected(found) if found == connection => Some(()),
            _ => None,
        }
    }
}

impl OperationResult for BeginDiscoverGatt {
    type Resource = GattSessionId;
    type Output = GattDatabase;
    type Correlation = ConnectionId;

    fn correlation(&self) -> Self::Correlation {
        self.connection
    }

    fn decode_success(
        connection: Self::Correlation,
        resource: Self::Resource,
        success: WaiterSuccess,
    ) -> Option<Self::Output> {
        match success {
            WaiterSuccess::GattDiscovered(database)
                if database.connection_id == connection && database.gatt_session_id == resource =>
            {
                Some(database)
            }
            _ => None,
        }
    }
}

impl OperationResult for BeginRssi {
    type Resource = ();
    type Output = i16;
    type Correlation = ConnectionId;

    fn correlation(&self) -> Self::Correlation {
        self.connection
    }

    fn decode_success(
        connection: Self::Correlation,
        (): Self::Resource,
        success: WaiterSuccess,
    ) -> Option<Self::Output> {
        match success {
            WaiterSuccess::RssiRead(found_connection, value) if found_connection == connection => {
                Some(value)
            }
            _ => None,
        }
    }
}

impl OperationResult for BeginRead {
    type Resource = ();
    type Output = Vec<u8>;
    type Correlation = (ConnectionId, GattSessionId, GattCharacteristicId);

    fn correlation(&self) -> Self::Correlation {
        (self.connection, self.session, self.characteristic)
    }

    fn decode_success(
        (connection, session, characteristic): Self::Correlation,
        (): Self::Resource,
        success: WaiterSuccess,
    ) -> Option<Self::Output> {
        match success {
            WaiterSuccess::CharacteristicRead {
                connection_id,
                gatt_session_id,
                characteristic_id,
                value,
            } if connection_id == connection
                && gatt_session_id == session
                && characteristic_id == characteristic =>
            {
                Some(value)
            }
            _ => None,
        }
    }
}

impl OperationResult for BeginWrite {
    type Resource = ();
    type Output = ();
    type Correlation = (ConnectionId, GattSessionId, GattCharacteristicId);

    fn correlation(&self) -> Self::Correlation {
        (self.connection, self.session, self.characteristic)
    }

    fn decode_success(
        (connection, session, characteristic): Self::Correlation,
        (): Self::Resource,
        success: WaiterSuccess,
    ) -> Option<Self::Output> {
        match success {
            WaiterSuccess::CharacteristicWritten(
                found_connection,
                found_session,
                found_characteristic,
            ) if found_connection == connection
                && found_session == session
                && found_characteristic == characteristic =>
            {
                Some(())
            }
            _ => None,
        }
    }
}

impl OperationResult for BeginEnableNotifications {
    type Resource = SubscriptionId;
    type Output = (SubscriptionId, Option<NotificationMode>);
    type Correlation = (ConnectionId, GattSessionId, GattCharacteristicId);

    fn correlation(&self) -> Self::Correlation {
        (self.connection, self.session, self.characteristic)
    }

    fn decode_success(
        (connection, session, characteristic): Self::Correlation,
        resource: Self::Resource,
        success: WaiterSuccess,
    ) -> Option<Self::Output> {
        match success {
            WaiterSuccess::NotificationsEnabled {
                connection_id,
                gatt_session_id,
                subscription_id,
                characteristic_id,
                actual_mode,
            } if connection_id == connection
                && gatt_session_id == session
                && subscription_id == resource
                && characteristic_id == characteristic =>
            {
                Some((resource, actual_mode))
            }
            _ => None,
        }
    }
}

impl OperationResult for BeginDisableNotifications {
    type Resource = ();
    type Output = ();
    type Correlation = (ConnectionId, GattSessionId, SubscriptionId);

    fn correlation(&self) -> Self::Correlation {
        (self.connection, self.session, self.subscription)
    }

    fn decode_success(
        (connection, session, subscription): Self::Correlation,
        (): Self::Resource,
        success: WaiterSuccess,
    ) -> Option<Self::Output> {
        match success {
            WaiterSuccess::NotificationsDisabled(
                found_connection,
                found_session,
                found_subscription,
                _,
            ) if found_connection == connection
                && found_session == session
                && found_subscription == subscription =>
            {
                Some(())
            }
            _ => None,
        }
    }
}

impl Message<BeginInitialize> for BlengActor {
    type Reply = Result<BeginReply, WaiterError>;

    async fn handle(
        &mut self,
        message: BeginInitialize,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        self.require_open()?;
        self.require_not_started()?;
        let (command_id, result_rx) = self.register_waiter(WaiterExpectation::Initialized)?;
        self.launch_dispatch(
            ctx,
            &message.handoff,
            command_id,
            |actor| {
                actor.reserve_initialize(command_id);
            },
            move |hub| hub.initialize(InitializeCommand { command_id }),
        )?;
        Ok(BeginReply {
            command_id,
            result_rx,
            resource: (),
        })
    }
}

impl Message<BeginScan> for BlengActor {
    type Reply = Result<BeginReply<ScanSessionId>, WaiterError>;

    async fn handle(
        &mut self,
        message: BeginScan,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        self.require_open()?;
        self.require_initialized()?;
        self.require_scan_available()?;
        let session_id: ScanSessionId = self.allocate_resource_id()?.into();
        let (command_id, result_rx) =
            self.register_waiter(WaiterExpectation::ScanStarted(session_id))?;
        self.launch_dispatch(
            ctx,
            &message.handoff,
            command_id,
            |actor| {
                actor.reserve_scan_start(command_id, session_id);
            },
            move |hub| {
                hub.start_scan(StartScanCommand {
                    command_id,
                    scan_session_id: session_id,
                    options: message.options,
                })
            },
        )?;
        Ok(BeginReply {
            command_id,
            result_rx,
            resource: session_id,
        })
    }
}

impl Message<BeginStop> for BlengActor {
    type Reply = Result<BeginReply, WaiterError>;

    async fn handle(
        &mut self,
        message: BeginStop,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        self.require_open()?;
        self.require_initialized()?;
        self.validate_scan_stop(message.session)?;
        let (command_id, result_rx) =
            self.register_waiter(WaiterExpectation::ScanStopped(message.session))?;
        self.launch_dispatch(
            ctx,
            &message.handoff,
            command_id,
            |actor| {
                actor.reserve_scan_stop(command_id);
            },
            move |hub| {
                hub.stop_scan(StopScanCommand {
                    command_id,
                    scan_session_id: message.session,
                })
            },
        )?;
        Ok(BeginReply {
            command_id,
            result_rx,
            resource: (),
        })
    }
}

impl Message<BeginConnect> for BlengActor {
    type Reply = Result<BeginReply<ConnectionId>, WaiterError>;

    async fn handle(
        &mut self,
        message: BeginConnect,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        self.require_open()?;
        self.require_initialized()?;
        self.require_peripheral_available(message.peripheral_id)?;
        let connection_id: ConnectionId = self.allocate_resource_id()?.into();
        let (command_id, result_rx) =
            self.register_waiter(WaiterExpectation::Connected(connection_id))?;
        self.launch_dispatch(
            ctx,
            &message.handoff,
            command_id,
            |actor| {
                actor.reserve_connection(message.peripheral_id, command_id, connection_id);
            },
            move |hub| {
                hub.connect(ConnectCommand {
                    command_id,
                    connection_id,
                    peripheral_id: message.peripheral_id,
                })
            },
        )?;
        Ok(BeginReply {
            command_id,
            result_rx,
            resource: connection_id,
        })
    }
}

impl Message<BeginDisconnect> for BlengActor {
    type Reply = Result<BeginReply, WaiterError>;

    async fn handle(
        &mut self,
        message: BeginDisconnect,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        self.require_open()?;
        self.require_initialized()?;
        let peripheral_id = self.validate_disconnect(message.connection)?;
        let (command_id, result_rx) =
            self.register_waiter(WaiterExpectation::Disconnected(message.connection))?;
        self.launch_dispatch(
            ctx,
            &message.handoff,
            command_id,
            |actor| {
                actor.reserve_disconnect(peripheral_id, command_id);
            },
            move |hub| {
                hub.disconnect(DisconnectCommand {
                    command_id,
                    connection_id: message.connection,
                })
            },
        )?;
        Ok(BeginReply {
            command_id,
            result_rx,
            resource: (),
        })
    }
}

impl Message<BeginDiscoverGatt> for BlengActor {
    type Reply = Result<BeginReply<GattSessionId>, WaiterError>;

    async fn handle(
        &mut self,
        message: BeginDiscoverGatt,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        self.require_open()?;
        self.require_initialized()?;
        let peripheral_id = self.validate_gatt_discovery(message.connection)?;
        let session_id: GattSessionId = self.allocate_resource_id()?.into();
        let (command_id, result_rx) = self.register_waiter(WaiterExpectation::GattDiscovered(
            message.connection,
            session_id,
        ))?;
        self.launch_dispatch(
            ctx,
            &message.handoff,
            command_id,
            |actor| {
                actor.reserve_gatt_discovery(peripheral_id, command_id, session_id);
            },
            move |hub| {
                hub.discover_gatt(DiscoverGattCommand {
                    command_id,
                    connection_id: message.connection,
                    gatt_session_id: session_id,
                })
            },
        )?;
        Ok(BeginReply {
            command_id,
            result_rx,
            resource: session_id,
        })
    }
}

impl Message<BeginRssi> for BlengActor {
    type Reply = Result<BeginReply, WaiterError>;

    async fn handle(
        &mut self,
        message: BeginRssi,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        self.require_open()?;
        self.require_initialized()?;
        let peripheral_id = self.validate_rssi(message.connection)?;
        let (command_id, result_rx) =
            self.register_waiter(WaiterExpectation::RssiRead(message.connection))?;
        self.launch_dispatch(
            ctx,
            &message.handoff,
            command_id,
            |actor| {
                actor.reserve_rssi(peripheral_id, command_id);
            },
            move |hub| {
                hub.read_rssi(ReadRssiCommand {
                    command_id,
                    connection_id: message.connection,
                })
            },
        )?;
        Ok(BeginReply {
            command_id,
            result_rx,
            resource: (),
        })
    }
}

impl Message<BeginRead> for BlengActor {
    type Reply = Result<BeginReply, WaiterError>;

    async fn handle(
        &mut self,
        message: BeginRead,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        self.require_open()?;
        self.require_initialized()?;
        let peripheral_id = self.validate_gatt_access(
            message.connection,
            message.session,
            message.characteristic,
            GattAccess::Read,
        )?;
        let (command_id, result_rx) =
            self.register_waiter(WaiterExpectation::CharacteristicRead(
                message.connection,
                message.session,
                message.characteristic,
            ))?;
        self.launch_dispatch(
            ctx,
            &message.handoff,
            command_id,
            |actor| {
                actor.reserve_read(
                    peripheral_id,
                    command_id,
                    message.session,
                    message.characteristic,
                );
            },
            move |hub| {
                hub.read_characteristic(ReadCharacteristicCommand {
                    command_id,
                    connection_id: message.connection,
                    gatt_session_id: message.session,
                    characteristic_id: message.characteristic,
                })
            },
        )?;
        Ok(BeginReply {
            command_id,
            result_rx,
            resource: (),
        })
    }
}

impl Message<BeginWrite> for BlengActor {
    type Reply = Result<BeginReply, WaiterError>;

    async fn handle(
        &mut self,
        message: BeginWrite,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        self.require_open()?;
        self.require_initialized()?;
        let peripheral_id = self.validate_gatt_access(
            message.connection,
            message.session,
            message.characteristic,
            GattAccess::Write,
        )?;
        let (command_id, result_rx) =
            self.register_waiter(WaiterExpectation::CharacteristicWritten(
                message.connection,
                message.session,
                message.characteristic,
            ))?;
        self.launch_dispatch(
            ctx,
            &message.handoff,
            command_id,
            |actor| {
                actor.reserve_write(
                    peripheral_id,
                    command_id,
                    message.session,
                    message.characteristic,
                );
            },
            move |hub| {
                hub.write_characteristic(WriteCharacteristicCommand {
                    command_id,
                    connection_id: message.connection,
                    gatt_session_id: message.session,
                    characteristic_id: message.characteristic,
                    value: message.value,
                    write_type: message.write_type,
                })
            },
        )?;
        Ok(BeginReply {
            command_id,
            result_rx,
            resource: (),
        })
    }
}

impl Message<BeginEnableNotifications> for BlengActor {
    type Reply = Result<BeginReply<SubscriptionId>, WaiterError>;

    async fn handle(
        &mut self,
        message: BeginEnableNotifications,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        self.require_open()?;
        self.require_initialized()?;
        let peripheral_id = self.validate_notification_enable(
            message.connection,
            message.session,
            message.characteristic,
        )?;
        let subscription: SubscriptionId = self.allocate_resource_id()?.into();
        let (command_id, result_rx) =
            self.register_waiter(WaiterExpectation::NotificationsEnabled(
                message.connection,
                message.session,
                subscription,
                message.characteristic,
            ))?;
        self.launch_dispatch(
            ctx,
            &message.handoff,
            command_id,
            |actor| {
                actor.reserve_notification_enable(
                    peripheral_id,
                    command_id,
                    subscription,
                    message.session,
                    message.characteristic,
                    message.mode,
                );
            },
            move |hub| {
                hub.set_notifications(SetNotificationsCommand {
                    command_id,
                    connection_id: message.connection,
                    gatt_session_id: message.session,
                    subscription_id: subscription,
                    characteristic_id: message.characteristic,
                    action: NotificationAction::Enable { mode: message.mode },
                })
            },
        )?;
        Ok(BeginReply {
            command_id,
            result_rx,
            resource: subscription,
        })
    }
}

impl Message<BeginDisableNotifications> for BlengActor {
    type Reply = Result<BeginReply, WaiterError>;

    async fn handle(
        &mut self,
        message: BeginDisableNotifications,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        self.require_open()?;
        self.require_initialized()?;
        let (peripheral_id, characteristic) = self.validate_notification_disable(
            message.connection,
            message.session,
            message.subscription,
        )?;
        let (command_id, result_rx) =
            self.register_waiter(WaiterExpectation::NotificationsDisabled(
                message.connection,
                message.session,
                message.subscription,
                characteristic,
            ))?;
        self.launch_dispatch(
            ctx,
            &message.handoff,
            command_id,
            |actor| {
                actor.reserve_notification_disable(peripheral_id, command_id, message.subscription);
            },
            move |hub| {
                hub.set_notifications(SetNotificationsCommand {
                    command_id,
                    connection_id: message.connection,
                    gatt_session_id: message.session,
                    subscription_id: message.subscription,
                    characteristic_id: characteristic,
                    action: NotificationAction::Disable,
                })
            },
        )?;
        Ok(BeginReply {
            command_id,
            result_rx,
            resource: (),
        })
    }
}

// Backend ingress and native dispatch outcomes.

/// One backend callback, distinct from native dispatch acknowledgement.
pub(super) struct IngressEvent(pub(super) BleEvent);

/// Native dispatch acknowledgement delivered back through the actor mailbox.
pub(super) struct DispatchResolved {
    pub(super) id: CommandId,
    pub(super) result: Result<(), CommandDispatchError>,
}

impl Message<DispatchResolved> for BlengActor {
    type Reply = ();

    async fn handle(&mut self, message: DispatchResolved, _: &mut Context<Self, Self::Reply>) {
        self.handle_dispatch_result(message.id, message.result);
    }
}

impl Message<IngressEvent> for BlengActor {
    type Reply = ();

    async fn handle(
        &mut self,
        IngressEvent(event): IngressEvent,
        _: &mut Context<Self, Self::Reply>,
    ) {
        self.handle_ingress_event(event);
    }
}

// Caller-side waiter retirement; native reservations keep their own lifetime.

pub(super) struct RetireWaiter {
    pub(super) id: CommandId,
    pub(super) timed_out: bool,
}

impl Message<RetireWaiter> for BlengActor {
    type Reply = ();

    async fn handle(&mut self, message: RetireWaiter, _: &mut Context<Self, Self::Reply>) {
        self.retire_waiter(message.id, message.timed_out);
    }
}

#[cfg(test)]
mod operation_result_tests {
    use super::{
        BeginConnect, BeginDisableNotifications, BeginDisconnect, BeginDiscoverGatt,
        BeginEnableNotifications, BeginInitialize, BeginRead, BeginRssi, BeginScan, BeginStop,
        BeginWrite, OperationResult,
    };
    use crate::waiter::WaiterSuccess;
    use blehub::{
        AdapterSnapshot, AdapterState, AuthorizationState, BluetoothUuid, ConnectionId,
        GattCharacteristicId, GattDatabase, GattService, GattServiceId, GattSessionId,
        NotificationMode, ScanSessionId, SubscriptionId,
    };

    const CONNECTION: ConnectionId = ConnectionId { value: 11 };
    const SESSION: GattSessionId = GattSessionId { value: 22 };
    const CHARACTERISTIC: GattCharacteristicId = GattCharacteristicId { value: 33 };
    const SUBSCRIPTION: SubscriptionId = SubscriptionId { value: 44 };
    const SCAN: ScanSessionId = ScanSessionId { value: 55 };

    #[test]
    fn initialization_moves_snapshot_and_allocating_operations_require_their_resource() {
        let snapshot = AdapterSnapshot {
            state: AdapterState::PoweredOn,
            scan_authorization: AuthorizationState::Authorized,
            connection_authorization: AuthorizationState::Authorized,
            name: Some("adapter payload".into()),
        };
        let name_ptr = snapshot.name.as_ref().unwrap().as_ptr();
        let decoded =
            BeginInitialize::decode_success((), (), WaiterSuccess::Initialized(snapshot)).unwrap();
        assert_eq!(decoded.state, AdapterState::PoweredOn);
        assert_eq!(decoded.scan_authorization, AuthorizationState::Authorized);
        assert_eq!(
            decoded.connection_authorization,
            AuthorizationState::Authorized
        );
        assert_eq!(decoded.name.as_deref(), Some("adapter payload"));
        assert_eq!(decoded.name.as_ref().unwrap().as_ptr(), name_ptr);
        assert_eq!(
            BeginInitialize::decode_success((), (), WaiterSuccess::ScanStarted(SCAN)),
            None
        );

        assert_eq!(
            BeginScan::decode_success((), SCAN, WaiterSuccess::ScanStarted(SCAN)),
            Some(SCAN)
        );
        assert_eq!(
            BeginScan::decode_success(
                (),
                ScanSessionId { value: 56 },
                WaiterSuccess::ScanStarted(SCAN),
            ),
            None
        );
        assert_eq!(
            BeginScan::decode_success((), SCAN, WaiterSuccess::ScanStopped(SCAN)),
            None
        );
        assert_eq!(
            BeginConnect::decode_success((), CONNECTION, WaiterSuccess::Connected(CONNECTION)),
            Some(CONNECTION)
        );
        assert_eq!(
            BeginConnect::decode_success(
                (),
                ConnectionId { value: 12 },
                WaiterSuccess::Connected(CONNECTION),
            ),
            None
        );
        assert_eq!(
            BeginConnect::decode_success((), CONNECTION, WaiterSuccess::Disconnected(CONNECTION)),
            None
        );
    }

    #[test]
    fn connection_and_scan_operations_reject_foreign_identities_and_rssi_keeps_its_value() {
        assert_eq!(
            BeginDisconnect::decode_success(
                ConnectionId { value: 12 },
                (),
                WaiterSuccess::Disconnected(CONNECTION),
            ),
            None
        );
        assert_eq!(
            BeginStop::decode_success(
                ScanSessionId { value: 56 },
                (),
                WaiterSuccess::ScanStopped(SCAN),
            ),
            None
        );
        assert_eq!(
            BeginRssi::decode_success(CONNECTION, (), WaiterSuccess::RssiRead(CONNECTION, -73)),
            Some(-73)
        );
        assert_eq!(
            BeginRssi::decode_success(
                ConnectionId { value: 12 },
                (),
                WaiterSuccess::RssiRead(CONNECTION, -73),
            ),
            None
        );
        assert_eq!(
            BeginRssi::decode_success(CONNECTION, (), WaiterSuccess::Disconnected(CONNECTION)),
            None
        );
    }

    #[test]
    fn discovery_requires_connection_and_allocated_session_and_moves_database() {
        for (connection_id, gatt_session_id) in [
            (ConnectionId { value: 12 }, SESSION),
            (CONNECTION, GattSessionId { value: 23 }),
        ] {
            assert_eq!(
                BeginDiscoverGatt::decode_success(
                    CONNECTION,
                    SESSION,
                    WaiterSuccess::GattDiscovered(GattDatabase {
                        connection_id,
                        gatt_session_id,
                        services: vec![],
                    }),
                ),
                None
            );
        }
        assert_eq!(
            BeginDiscoverGatt::decode_success(
                CONNECTION,
                SESSION,
                WaiterSuccess::Connected(CONNECTION),
            ),
            None
        );
        let database = GattDatabase {
            connection_id: CONNECTION,
            gatt_session_id: SESSION,
            services: vec![GattService {
                id: GattServiceId { value: 66 },
                uuid: BluetoothUuid::canonicalize("180f").unwrap(),
                primary: Some(true),
                characteristics: vec![],
            }],
        };
        let services_ptr = database.services.as_ptr();
        let decoded = BeginDiscoverGatt::decode_success(
            CONNECTION,
            SESSION,
            WaiterSuccess::GattDiscovered(database),
        )
        .unwrap();
        assert_eq!(decoded.connection_id, CONNECTION);
        assert_eq!(decoded.gatt_session_id, SESSION);
        assert_eq!(decoded.services.as_ptr(), services_ptr);
        assert_eq!(decoded.services.len(), 1);
        assert_eq!(decoded.services[0].id, GattServiceId { value: 66 });
        assert_eq!(
            decoded.services[0].uuid,
            BluetoothUuid::canonicalize("180f").unwrap()
        );
        assert_eq!(decoded.services[0].primary, Some(true));
    }

    #[test]
    fn characteristic_operations_check_each_identity_axis_and_move_read_payload() {
        let correlation = (CONNECTION, SESSION, CHARACTERISTIC);
        for (connection_id, gatt_session_id, characteristic_id) in [
            (ConnectionId { value: 12 }, SESSION, CHARACTERISTIC),
            (CONNECTION, GattSessionId { value: 23 }, CHARACTERISTIC),
            (CONNECTION, SESSION, GattCharacteristicId { value: 34 }),
        ] {
            assert_eq!(
                BeginRead::decode_success(
                    correlation,
                    (),
                    WaiterSuccess::CharacteristicRead {
                        connection_id,
                        gatt_session_id,
                        characteristic_id,
                        value: vec![0xde, 0xad],
                    },
                ),
                None
            );
            assert_eq!(
                BeginWrite::decode_success(
                    correlation,
                    (),
                    WaiterSuccess::CharacteristicWritten(
                        connection_id,
                        gatt_session_id,
                        characteristic_id,
                    ),
                ),
                None
            );
            assert_eq!(
                BeginEnableNotifications::decode_success(
                    correlation,
                    SUBSCRIPTION,
                    WaiterSuccess::NotificationsEnabled {
                        connection_id,
                        gatt_session_id,
                        subscription_id: SUBSCRIPTION,
                        characteristic_id,
                        actual_mode: Some(NotificationMode::Notification),
                    },
                ),
                None
            );
        }
        assert_eq!(
            BeginRead::decode_success(
                correlation,
                (),
                WaiterSuccess::CharacteristicWritten(CONNECTION, SESSION, CHARACTERISTIC),
            ),
            None
        );
        let value = vec![0xde, 0xad, 0xbe, 0xef];
        let value_ptr = value.as_ptr();
        let decoded = BeginRead::decode_success(
            correlation,
            (),
            WaiterSuccess::CharacteristicRead {
                connection_id: CONNECTION,
                gatt_session_id: SESSION,
                characteristic_id: CHARACTERISTIC,
                value,
            },
        )
        .unwrap();
        assert_eq!(decoded, [0xde, 0xad, 0xbe, 0xef]);
        assert_eq!(decoded.as_ptr(), value_ptr);
    }

    #[test]
    fn enable_requires_allocated_subscription_and_preserves_optional_actual_mode() {
        let correlation = (CONNECTION, SESSION, CHARACTERISTIC);
        for actual_mode in [
            None,
            Some(NotificationMode::Notification),
            Some(NotificationMode::Indication),
        ] {
            assert_eq!(
                BeginEnableNotifications::decode_success(
                    correlation,
                    SUBSCRIPTION,
                    WaiterSuccess::NotificationsEnabled {
                        connection_id: CONNECTION,
                        gatt_session_id: SESSION,
                        subscription_id: SUBSCRIPTION,
                        characteristic_id: CHARACTERISTIC,
                        actual_mode,
                    },
                ),
                Some((SUBSCRIPTION, actual_mode))
            );
        }
        assert_eq!(
            BeginEnableNotifications::decode_success(
                correlation,
                SubscriptionId { value: 45 },
                WaiterSuccess::NotificationsEnabled {
                    connection_id: CONNECTION,
                    gatt_session_id: SESSION,
                    subscription_id: SUBSCRIPTION,
                    characteristic_id: CHARACTERISTIC,
                    actual_mode: None,
                },
            ),
            None
        );
        assert_eq!(
            BeginEnableNotifications::decode_success(
                correlation,
                SUBSCRIPTION,
                WaiterSuccess::NotificationsDisabled(
                    CONNECTION,
                    SESSION,
                    SUBSCRIPTION,
                    CHARACTERISTIC
                ),
            ),
            None
        );
    }

    #[test]
    fn disable_checks_each_request_identity_but_does_not_require_characteristic() {
        let correlation = (CONNECTION, SESSION, SUBSCRIPTION);
        for (connection, session, subscription) in [
            (ConnectionId { value: 12 }, SESSION, SUBSCRIPTION),
            (CONNECTION, GattSessionId { value: 23 }, SUBSCRIPTION),
            (CONNECTION, SESSION, SubscriptionId { value: 45 }),
        ] {
            assert_eq!(
                BeginDisableNotifications::decode_success(
                    correlation,
                    (),
                    WaiterSuccess::NotificationsDisabled(
                        connection,
                        session,
                        subscription,
                        CHARACTERISTIC
                    ),
                ),
                None
            );
        }
        for characteristic in [CHARACTERISTIC, GattCharacteristicId { value: 34 }] {
            assert_eq!(
                BeginDisableNotifications::decode_success(
                    correlation,
                    (),
                    WaiterSuccess::NotificationsDisabled(
                        CONNECTION,
                        SESSION,
                        SUBSCRIPTION,
                        characteristic
                    ),
                ),
                Some(())
            );
        }
    }

    #[test]
    fn unit_outputs_do_not_make_distinct_operations_interchangeable() {
        for operation in 0..4 {
            for (variant, success) in [
                WaiterSuccess::ScanStopped(SCAN),
                WaiterSuccess::Disconnected(CONNECTION),
                WaiterSuccess::CharacteristicWritten(CONNECTION, SESSION, CHARACTERISTIC),
                WaiterSuccess::NotificationsDisabled(
                    CONNECTION,
                    SESSION,
                    SUBSCRIPTION,
                    CHARACTERISTIC,
                ),
            ]
            .into_iter()
            .enumerate()
            {
                let decoded = match operation {
                    0 => BeginStop::decode_success(SCAN, (), success),
                    1 => BeginDisconnect::decode_success(CONNECTION, (), success),
                    2 => BeginWrite::decode_success(
                        (CONNECTION, SESSION, CHARACTERISTIC),
                        (),
                        success,
                    ),
                    _ => BeginDisableNotifications::decode_success(
                        (CONNECTION, SESSION, SUBSCRIPTION),
                        (),
                        success,
                    ),
                };
                assert_eq!(
                    decoded,
                    (operation == variant).then_some(()),
                    "operation {operation}, variant {variant}"
                );
            }
        }
    }
}
