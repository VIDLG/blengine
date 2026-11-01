//! Actor mailbox protocol: command requests, asynchronous inputs, and waiter control.
//!
//! `Begin*` messages own bounded eager submissions. Handlers validate BLE state,
//! register the submission's original sender, claim dispatch, and install native
//! reservations. Admission errors settle that same result channel; there is no
//! admission reply channel or caller-side begin task. Resource metadata is written
//! once by admission, before the actor can reconcile backend evidence.
//! `IngressEvent` and `DispatchResolved` carry callbacks and native receipts;
//! cancellation after claim and reservation retirement remain actor-owned.
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

use super::{actor::BlengActor, reservations::GattAccess, shared::OperationSubmission};

// Command requests and typed result decoding.

/// Ties a submitted request to its actor-allocated resource and typed success.
///
/// Correlation is a bounded snapshot of request IDs, not native state or an
/// operation result. The owner extracts it before moving the message into
/// bounded enqueue, without retaining or cloning the request payload.
/// Decoding has no lifecycle dependency: the owner handles a rejected success.
pub(crate) trait OperationSpec: Send + 'static {
    /// Actor-allocated ID, or `()` for commands creating no resource.
    type Resource: Send + Sync + Copy + 'static;
    type Output: Send + 'static;
    type Correlation: Send + 'static;

    fn correlation(&self) -> Self::Correlation;

    /// Checks the success variant and identities, then moves out its payload.
    fn decode_success(
        correlation: Self::Correlation,
        resource: Self::Resource,
        success: WaiterSuccess,
    ) -> Option<Self::Output>;
}

pub(crate) struct BeginInitialize {
    pub(crate) submission: OperationSubmission,
}

pub(crate) struct BeginScan {
    pub(crate) submission: OperationSubmission<ScanSessionId>,
    pub(crate) options: ScanOptions,
}

pub(crate) struct BeginStop {
    pub(crate) submission: OperationSubmission,
    pub(crate) session: ScanSessionId,
}

pub(crate) struct BeginConnect {
    pub(crate) submission: OperationSubmission<ConnectionId>,
    pub(crate) peripheral_id: PeripheralId,
}

pub(crate) struct BeginDisconnect {
    pub(crate) submission: OperationSubmission,
    pub(crate) connection: ConnectionId,
}

pub(crate) struct BeginDiscoverGatt {
    pub(crate) submission: OperationSubmission<GattSessionId>,
    pub(crate) connection: ConnectionId,
}

pub(crate) struct BeginRssi {
    pub(crate) submission: OperationSubmission,
    pub(crate) connection: ConnectionId,
}

pub(crate) struct BeginRead {
    pub(crate) submission: OperationSubmission,
    pub(crate) connection: ConnectionId,
    pub(crate) session: GattSessionId,
    pub(crate) characteristic: GattCharacteristicId,
}

pub(crate) struct BeginWrite {
    pub(crate) submission: OperationSubmission,
    pub(crate) connection: ConnectionId,
    pub(crate) session: GattSessionId,
    pub(crate) characteristic: GattCharacteristicId,
    pub(crate) value: Vec<u8>,
    pub(crate) write_type: WriteType,
}

pub(crate) struct BeginEnableNotifications {
    pub(crate) submission: OperationSubmission<SubscriptionId>,
    pub(crate) connection: ConnectionId,
    pub(crate) session: GattSessionId,
    pub(crate) characteristic: GattCharacteristicId,
    pub(crate) mode: NotificationModeRequest,
}

pub(crate) struct BeginDisableNotifications {
    pub(crate) submission: OperationSubmission,
    pub(crate) connection: ConnectionId,
    pub(crate) session: GattSessionId,
    pub(crate) subscription: SubscriptionId,
}

impl OperationSpec for BeginInitialize {
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

impl OperationSpec for BeginScan {
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

impl OperationSpec for BeginStop {
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

impl OperationSpec for BeginConnect {
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

impl OperationSpec for BeginDisconnect {
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

impl OperationSpec for BeginDiscoverGatt {
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

impl OperationSpec for BeginRssi {
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

impl OperationSpec for BeginRead {
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

impl OperationSpec for BeginWrite {
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

impl OperationSpec for BeginEnableNotifications {
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

impl OperationSpec for BeginDisableNotifications {
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
    type Reply = ();

    async fn handle(
        &mut self,
        message: BeginInitialize,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        let submission = message.submission.clone();
        let result = (|| {
            self.require_open()?;
            self.require_not_started()?;
            let command_id = self
                .register_operation_waiter(&message.submission, WaiterExpectation::Initialized)?;
            self.launch_dispatch(
                ctx,
                &message.submission,
                command_id,
                |reservations| {
                    reservations.reserve_initialize(command_id);
                },
                move |hub| hub.initialize(InitializeCommand { command_id }),
            )?;
            Ok(())
        })();
        submission.resolve(result);
    }
}

impl Message<BeginScan> for BlengActor {
    type Reply = ();

    async fn handle(
        &mut self,
        message: BeginScan,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        let submission = message.submission.clone();
        let result = (|| {
            self.require_open()?;
            self.require_initialized()?;
            self.require_scan_available()?;
            let session_id: ScanSessionId = self.allocate_resource_id()?.into();
            let command_id = self.register_operation_waiter(
                &message.submission,
                WaiterExpectation::ScanStarted(session_id),
            )?;
            self.launch_dispatch(
                ctx,
                &message.submission,
                command_id,
                |reservations| {
                    reservations.reserve_scan_start(command_id, session_id);
                },
                move |hub| {
                    hub.start_scan(StartScanCommand {
                        command_id,
                        scan_session_id: session_id,
                        options: message.options,
                    })
                },
            )?;
            Ok(session_id)
        })();
        submission.resolve(result);
    }
}

impl Message<BeginStop> for BlengActor {
    type Reply = ();

    async fn handle(
        &mut self,
        message: BeginStop,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        let submission = message.submission.clone();
        let result = (|| {
            self.require_open()?;
            self.require_initialized()?;
            self.validate_scan_stop(message.session)?;
            let command_id = self.register_operation_waiter(
                &message.submission,
                WaiterExpectation::ScanStopped(message.session),
            )?;
            self.launch_dispatch(
                ctx,
                &message.submission,
                command_id,
                |reservations| {
                    reservations.reserve_scan_stop(command_id);
                },
                move |hub| {
                    hub.stop_scan(StopScanCommand {
                        command_id,
                        scan_session_id: message.session,
                    })
                },
            )?;
            Ok(())
        })();
        submission.resolve(result);
    }
}

impl Message<BeginConnect> for BlengActor {
    type Reply = ();

    async fn handle(
        &mut self,
        message: BeginConnect,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        let submission = message.submission.clone();
        let result = (|| {
            self.require_open()?;
            self.require_initialized()?;
            self.require_peripheral_available(message.peripheral_id)?;
            let connection_id: ConnectionId = self.allocate_resource_id()?.into();
            let command_id = self.register_operation_waiter(
                &message.submission,
                WaiterExpectation::Connected(connection_id),
            )?;
            self.launch_dispatch(
                ctx,
                &message.submission,
                command_id,
                |reservations| {
                    reservations.reserve_connection(
                        message.peripheral_id,
                        command_id,
                        connection_id,
                    );
                },
                move |hub| {
                    hub.connect(ConnectCommand {
                        command_id,
                        connection_id,
                        peripheral_id: message.peripheral_id,
                    })
                },
            )?;
            Ok(connection_id)
        })();
        submission.resolve(result);
    }
}

impl Message<BeginDisconnect> for BlengActor {
    type Reply = ();

    async fn handle(
        &mut self,
        message: BeginDisconnect,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        let submission = message.submission.clone();
        let result = (|| {
            self.require_open()?;
            self.require_initialized()?;
            let peripheral_id = self.validate_disconnect(message.connection)?;
            let command_id = self.register_operation_waiter(
                &message.submission,
                WaiterExpectation::Disconnected(message.connection),
            )?;
            self.launch_dispatch(
                ctx,
                &message.submission,
                command_id,
                |reservations| {
                    reservations.reserve_disconnect(peripheral_id, command_id);
                },
                move |hub| {
                    hub.disconnect(DisconnectCommand {
                        command_id,
                        connection_id: message.connection,
                    })
                },
            )?;
            Ok(())
        })();
        submission.resolve(result);
    }
}

impl Message<BeginDiscoverGatt> for BlengActor {
    type Reply = ();

    async fn handle(
        &mut self,
        message: BeginDiscoverGatt,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        let submission = message.submission.clone();
        let result = (|| {
            self.require_open()?;
            self.require_initialized()?;
            let peripheral_id = self.validate_gatt_discovery(message.connection)?;
            let session_id: GattSessionId = self.allocate_resource_id()?.into();
            let command_id = self.register_operation_waiter(
                &message.submission,
                WaiterExpectation::GattDiscovered(message.connection, session_id),
            )?;
            self.launch_dispatch(
                ctx,
                &message.submission,
                command_id,
                |reservations| {
                    reservations.reserve_gatt_discovery(peripheral_id, command_id, session_id);
                },
                move |hub| {
                    hub.discover_gatt(DiscoverGattCommand {
                        command_id,
                        connection_id: message.connection,
                        gatt_session_id: session_id,
                    })
                },
            )?;
            Ok(session_id)
        })();
        submission.resolve(result);
    }
}

impl Message<BeginRssi> for BlengActor {
    type Reply = ();

    async fn handle(
        &mut self,
        message: BeginRssi,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        let submission = message.submission.clone();
        let result = (|| {
            self.require_open()?;
            self.require_initialized()?;
            let peripheral_id = self.validate_rssi(message.connection)?;
            let command_id = self.register_operation_waiter(
                &message.submission,
                WaiterExpectation::RssiRead(message.connection),
            )?;
            self.launch_dispatch(
                ctx,
                &message.submission,
                command_id,
                |reservations| {
                    reservations.reserve_rssi(peripheral_id, command_id);
                },
                move |hub| {
                    hub.read_rssi(ReadRssiCommand {
                        command_id,
                        connection_id: message.connection,
                    })
                },
            )?;
            Ok(())
        })();
        submission.resolve(result);
    }
}

impl Message<BeginRead> for BlengActor {
    type Reply = ();

    async fn handle(
        &mut self,
        message: BeginRead,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        let submission = message.submission.clone();
        let result = (|| {
            self.require_open()?;
            self.require_initialized()?;
            let peripheral_id = self.validate_gatt_access(
                message.connection,
                message.session,
                message.characteristic,
                GattAccess::Read,
            )?;
            let command_id = self.register_operation_waiter(
                &message.submission,
                WaiterExpectation::CharacteristicRead(
                    message.connection,
                    message.session,
                    message.characteristic,
                ),
            )?;
            self.launch_dispatch(
                ctx,
                &message.submission,
                command_id,
                |reservations| {
                    reservations.reserve_read(
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
            Ok(())
        })();
        submission.resolve(result);
    }
}

impl Message<BeginWrite> for BlengActor {
    type Reply = ();

    async fn handle(
        &mut self,
        message: BeginWrite,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        let submission = message.submission.clone();
        let result = (|| {
            self.require_open()?;
            self.require_initialized()?;
            let peripheral_id = self.validate_gatt_access(
                message.connection,
                message.session,
                message.characteristic,
                GattAccess::Write,
            )?;
            let command_id = self.register_operation_waiter(
                &message.submission,
                WaiterExpectation::CharacteristicWritten(
                    message.connection,
                    message.session,
                    message.characteristic,
                ),
            )?;
            self.launch_dispatch(
                ctx,
                &message.submission,
                command_id,
                |reservations| {
                    reservations.reserve_write(
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
            Ok(())
        })();
        submission.resolve(result);
    }
}

impl Message<BeginEnableNotifications> for BlengActor {
    type Reply = ();

    async fn handle(
        &mut self,
        message: BeginEnableNotifications,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        let submission = message.submission.clone();
        let result = (|| {
            self.require_open()?;
            self.require_initialized()?;
            let peripheral_id = self.validate_notification_enable(
                message.connection,
                message.session,
                message.characteristic,
            )?;
            let subscription: SubscriptionId = self.allocate_resource_id()?.into();
            let command_id = self.register_operation_waiter(
                &message.submission,
                WaiterExpectation::NotificationsEnabled(
                    message.connection,
                    message.session,
                    subscription,
                    message.characteristic,
                ),
            )?;
            self.launch_dispatch(
                ctx,
                &message.submission,
                command_id,
                |reservations| {
                    reservations.reserve_notification_enable(
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
            Ok(subscription)
        })();
        submission.resolve(result);
    }
}

impl Message<BeginDisableNotifications> for BlengActor {
    type Reply = ();

    async fn handle(
        &mut self,
        message: BeginDisableNotifications,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        let submission = message.submission.clone();
        let result = (|| {
            self.require_open()?;
            self.require_initialized()?;
            let (peripheral_id, characteristic) = self.validate_notification_disable(
                message.connection,
                message.session,
                message.subscription,
            )?;
            let command_id = self.register_operation_waiter(
                &message.submission,
                WaiterExpectation::NotificationsDisabled(
                    message.connection,
                    message.session,
                    message.subscription,
                    characteristic,
                ),
            )?;
            self.launch_dispatch(
                ctx,
                &message.submission,
                command_id,
                |reservations| {
                    reservations.reserve_notification_disable(
                        peripheral_id,
                        command_id,
                        message.subscription,
                    );
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
            Ok(())
        })();
        submission.resolve(result);
    }
}

// Backend ingress.

/// One backend callback, distinct from native dispatch and cancellation acknowledgements.
pub(super) struct IngressEvent(pub(super) BleEvent);

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

// Native dispatch acknowledgement.

/// Native dispatch acknowledgement delivered back through the actor mailbox.
pub(super) struct DispatchResolved {
    pub(super) id: CommandId,
    pub(super) result: Result<(), CommandDispatchError>,
}

impl Message<DispatchResolved> for BlengActor {
    type Reply = ();

    async fn handle(&mut self, message: DispatchResolved, ctx: &mut Context<Self, Self::Reply>) {
        self.handle_dispatch_result(message.id, message.result, ctx.actor_ref().clone());
    }
}

// Native cancellation acknowledgement.

/// Native cancellation acknowledgement returned through the actor mailbox.
/// The result covers backend acceptance of the control request, not BLE teardown.
pub(super) struct CancellationResolved {
    pub(super) id: CommandId,
    pub(super) result: Result<(), WaiterError>,
}

impl Message<CancellationResolved> for BlengActor {
    type Reply = ();

    async fn handle(&mut self, message: CancellationResolved, _: &mut Context<Self, Self::Reply>) {
        self.handle_cancellation_result(message.id, message.result);
    }
}

// Caller-side explicit operation control.

/// Requests cancellation of an existing command without allocating a new ID.
/// The actor reply is a receipt channel for native control acceptance; native
/// stop and recovery remain asynchronous and belong to the original reservation.
pub(super) struct RequestCancellation {
    pub(super) command_id: CommandId,
}

impl Message<RequestCancellation> for BlengActor {
    type Reply = Result<super::reservations::CancellationReceiver, WaiterError>;

    async fn handle(
        &mut self,
        message: RequestCancellation,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        self.request_operation_cancellation(message.command_id, ctx.actor_ref().clone())
    }
}

#[cfg(test)]
#[path = "tests/operation_results.rs"]
mod operation_result_tests;
