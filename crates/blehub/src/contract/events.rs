use super::{BleEventError, CommandId, ConnectionId, ScanSessionId};

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, uniffi::Enum)]
pub enum ScanStopReason {
    Requested { command_id: CommandId },
    SystemSuspended,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, uniffi::Enum)]
pub enum ConnectionState {
    Connecting,
    Connected,
    Disconnecting,
    Disconnected,
}

#[derive(Clone, Debug, PartialEq, Eq, uniffi::Enum)]
pub enum ScanStopResult {
    Ok { reason: ScanStopReason },
    Err { error: BleEventError },
}

#[derive(Clone, Debug, PartialEq, Eq, uniffi::Enum)]
pub enum ConnectionStateResult {
    Ok { state: ConnectionState },
    Err { error: BleEventError },
}

#[derive(Clone, Debug, PartialEq, Eq, uniffi::Enum)]
pub enum BleEvent {
    ScanStarted {
        command_id: CommandId,
        scan_id: ScanSessionId,
    },
    ScanStopped {
        scan_id: ScanSessionId,
        result: ScanStopResult,
    },
    ConnectionStateChanged {
        command_id: Option<CommandId>,
        connection_id: ConnectionId,
        result: ConnectionStateResult,
    },
    CommandCompleted {
        command_id: CommandId,
        error: Option<BleEventError>,
    },
    BackendError {
        error: BleEventError,
    },
}
