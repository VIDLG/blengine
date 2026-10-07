//! Command messages and actor-side admission; the actor owns dispatch coordination.
//!
//! `Begin` requests establish a command's caller-to-actor handoff, install its
//! reservation, and submit native dispatch. Their replies transfer the waiter
//! receiver and any newly allocated resource ID; they do not acknowledge backend
//! acceptance or BLE completion.
//!
//! Handlers follow the same order: validate, allocate IDs/register the waiter,
//! then call `launch_dispatch` to claim the handoff, install the reservation,
//! and submit native dispatch. Validation remains command-specific; caller
//! deadlines/waiting live in owner.rs, evidence reconciliation in actor/events.rs.

use blehub::{
    CommandId, ConnectCommand, ConnectionId, DisconnectCommand, DiscoverGattCommand,
    GattCharacteristicId, GattSessionId, InitializeCommand, NotificationAction,
    NotificationModeRequest, PeripheralId, ReadCharacteristicCommand, ReadRssiCommand, ScanOptions,
    ScanSessionId, SetNotificationsCommand, StartScanCommand, StopScanCommand, SubscriptionId,
    WriteCharacteristicCommand, WriteType,
};
use kameo::prelude::{Context, Message};

use crate::waiter::{WaiterError, WaiterExpectation};

use super::{
    actor::{BlengActor, WaiterReceiver},
    owner::BeginHandoff,
};

/// Actor-to-owner handoff metadata, not a native dispatch result.
/// `Resource` is the newly allocated typed ID, or `()` for commands creating none.
/// The result receiver remains distinct from the registry's waiting expectation.
pub(super) struct BeginReply<Resource = ()> {
    pub(super) command_id: CommandId,
    pub(super) result_rx: WaiterReceiver,
    pub(super) resource: Resource,
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

pub(super) struct RetireWaiter {
    pub(super) id: CommandId,
    pub(super) timed_out: bool,
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
        let peripheral_id = self.validate_gatt_io(
            message.connection,
            message.session,
            message.characteristic,
            true,
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
        let peripheral_id = self.validate_gatt_io(
            message.connection,
            message.session,
            message.characteristic,
            false,
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

impl Message<RetireWaiter> for BlengActor {
    type Reply = ();

    async fn handle(&mut self, message: RetireWaiter, _: &mut Context<Self, Self::Reply>) {
        if message.timed_out {
            self.timeout_waiter(message.id);
        } else {
            self.cancel_waiter(message.id);
        }
    }
}
