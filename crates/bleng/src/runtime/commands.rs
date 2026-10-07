//! Command messages, actor-side admission, and isolated synchronous native dispatch.

use blehub::{
    CommandId, ConnectCommand, ConnectionId, DisconnectCommand, DiscoverGattCommand,
    GattCharacteristicId, GattSessionId, InitializeCommand, PeripheralId,
    ReadCharacteristicCommand, ReadRssiCommand, ScanOptions, ScanSessionId, StartScanCommand,
    StopScanCommand, WriteCharacteristicCommand, WriteType,
};
use kameo::prelude::{Context, Message};

use crate::waiter::{WaiterError, WaiterExpectation};

use super::{BeginHandoff, BlengActor, WaiterReceiver};

type CommandReply = Result<(CommandId, WaiterReceiver), WaiterError>;
type ScanReply = Result<(CommandId, ScanSessionId, WaiterReceiver), WaiterError>;
type ConnectionReply = Result<(CommandId, ConnectionId, WaiterReceiver), WaiterError>;
type GattReply = Result<(CommandId, GattSessionId, WaiterReceiver), WaiterError>;

pub(crate) struct BeginInitialize {
    pub(super) handoff: BeginHandoff,
}

pub(crate) struct BeginScan {
    pub(super) handoff: BeginHandoff,
    pub(super) options: ScanOptions,
}

pub(crate) struct BeginStop {
    pub(super) handoff: BeginHandoff,
    pub(super) session: ScanSessionId,
}

pub(crate) struct BeginConnect {
    pub(super) handoff: BeginHandoff,
    pub(super) peripheral_id: PeripheralId,
}

pub(crate) struct BeginDisconnect {
    pub(super) handoff: BeginHandoff,
    pub(super) connection: ConnectionId,
}

pub(crate) struct BeginDiscoverGatt {
    pub(super) handoff: BeginHandoff,
    pub(super) connection: ConnectionId,
}

pub(crate) struct BeginRssi {
    pub(super) handoff: BeginHandoff,
    pub(super) connection: ConnectionId,
}

pub(crate) struct BeginRead {
    pub(super) handoff: BeginHandoff,
    pub(super) connection: ConnectionId,
    pub(super) session: GattSessionId,
    pub(super) characteristic: GattCharacteristicId,
}

pub(crate) struct BeginWrite {
    pub(super) handoff: BeginHandoff,
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
            self.timeout_waiter(message.id);
        } else {
            self.cancel_waiter(message.id);
        }
    }
}

impl Message<BeginInitialize> for BlengActor {
    type Reply = CommandReply;

    async fn handle(
        &mut self,
        message: BeginInitialize,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        self.require_open()?;
        self.require_not_started()?;
        let (id, result_rx) = self.register_waiter(WaiterExpectation::Initialized)?;
        self.launch_dispatch(
            ctx,
            &message.handoff,
            id,
            |actor| {
                actor.reserve_initialize(id);
            },
            move |hub| hub.initialize(InitializeCommand { command_id: id }),
        )?;
        Ok((id, result_rx))
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
        self.require_scan_available()?;
        let session: ScanSessionId = self.allocate_resource_id()?.into();
        let (id, result_rx) = self.register_waiter(WaiterExpectation::ScanStarted(session))?;
        self.launch_dispatch(
            ctx,
            &message.handoff,
            id,
            |actor| {
                actor.reserve_scan_start(id, session);
            },
            move |hub| {
                let command = StartScanCommand {
                    command_id: id,
                    scan_session_id: session,
                    options: message.options,
                };
                hub.start_scan(command)
            },
        )?;
        Ok((id, session, result_rx))
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
                let command = ConnectCommand {
                    command_id,
                    connection_id,
                    peripheral_id: message.peripheral_id,
                };
                hub.connect(command)
            },
        )?;
        Ok((command_id, connection_id, result_rx))
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
        let key = self.validate_disconnect(message.connection)?;
        let (id, result_rx) =
            self.register_waiter(WaiterExpectation::Disconnected(message.connection))?;
        self.launch_dispatch(
            ctx,
            &message.handoff,
            id,
            |actor| {
                actor.reserve_disconnect(key, id);
            },
            move |hub| {
                hub.disconnect(DisconnectCommand {
                    command_id: id,
                    connection_id: message.connection,
                })
            },
        )?;
        Ok((id, result_rx))
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
        let connection_key = self.validate_gatt_discovery(message.connection)?;
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
                actor.reserve_gatt_discovery(connection_key, command_id, session_id);
            },
            move |hub| {
                let command = DiscoverGattCommand {
                    command_id,
                    connection_id: message.connection,
                    gatt_session_id: session_id,
                };
                hub.discover_gatt(command)
            },
        )?;
        Ok((command_id, session_id, result_rx))
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
        let key = self.validate_rssi(message.connection)?;
        let (id, result_rx) =
            self.register_waiter(WaiterExpectation::RssiRead(message.connection))?;
        self.launch_dispatch(
            ctx,
            &message.handoff,
            id,
            |actor| {
                actor.reserve_rssi(key, id);
            },
            move |hub| {
                hub.read_rssi(ReadRssiCommand {
                    command_id: id,
                    connection_id: message.connection,
                })
            },
        )?;
        Ok((id, result_rx))
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
        let connection_key = self.validate_gatt_io(
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
                    connection_key,
                    command_id,
                    message.session,
                    message.characteristic,
                );
            },
            move |hub| {
                let command = ReadCharacteristicCommand {
                    command_id,
                    connection_id: message.connection,
                    gatt_session_id: message.session,
                    characteristic_id: message.characteristic,
                };
                hub.read_characteristic(command)
            },
        )?;
        Ok((command_id, result_rx))
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
        let connection_key = self.validate_gatt_io(
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
                    connection_key,
                    command_id,
                    message.session,
                    message.characteristic,
                );
            },
            move |hub| {
                let command = WriteCharacteristicCommand {
                    command_id,
                    connection_id: message.connection,
                    gatt_session_id: message.session,
                    characteristic_id: message.characteristic,
                    value: message.value,
                    write_type: message.write_type,
                };
                hub.write_characteristic(command)
            },
        )?;
        Ok((command_id, result_rx))
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
        self.validate_scan_stop(message.session)?;
        let (id, result_rx) =
            self.register_waiter(WaiterExpectation::ScanStopped(message.session))?;
        self.launch_dispatch(
            ctx,
            &message.handoff,
            id,
            |actor| {
                actor.reserve_scan_stop(id);
            },
            move |hub| {
                let command = StopScanCommand {
                    command_id: id,
                    scan_session_id: message.session,
                };
                hub.stop_scan(command)
            },
        )?;
        Ok((id, result_rx))
    }
}
