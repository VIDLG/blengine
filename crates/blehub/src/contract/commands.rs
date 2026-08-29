use super::{CommandId, ConnectionId, PeripheralId, ScanSessionId};

#[boltffi::data]
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum CommandKind {
    StartScan,
    Connect,
}

#[boltffi::data]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct StartScanCommand {
    pub command_id: CommandId,
    pub scan_session_id: ScanSessionId,
}

#[boltffi::data]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ConnectCommand {
    pub command_id: CommandId,
    pub connection_id: ConnectionId,
    pub peripheral_id: PeripheralId,
}
