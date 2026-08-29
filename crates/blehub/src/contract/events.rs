use super::{BleEventError, CommandId, ConnectionId, ScanSessionId};

const MAX_DIAGNOSTIC_BYTES: usize = 1024;

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
pub enum BleEventPayload {
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

#[derive(Clone, Debug, PartialEq, Eq, uniffi::Record)]
pub struct BleEvent {
    pub payload: BleEventPayload,
}

#[derive(Debug, thiserror::Error)]
pub(crate) enum EventValidationError {
    #[error("{field} must be nonzero")]
    ZeroId { field: &'static str },

    #[error("{field} exceeds {MAX_DIAGNOSTIC_BYTES} UTF-8 bytes")]
    DiagnosticTooLong { field: &'static str },
}

impl BleEvent {
    pub(crate) fn validate_shape(&self) -> Result<(), EventValidationError> {
        match &self.payload {
            BleEventPayload::ScanStarted {
                command_id,
                scan_id,
            } => {
                validate_id(command_id.is_valid(), "command_id")?;
                validate_id(scan_id.is_valid(), "scan_id")
            }
            BleEventPayload::ScanStopped { scan_id, result } => {
                validate_id(scan_id.is_valid(), "scan_id")?;
                match result {
                    ScanStopResult::Ok {
                        reason: ScanStopReason::Requested { command_id },
                    } => validate_id(command_id.is_valid(), "command_id"),
                    ScanStopResult::Ok {
                        reason: ScanStopReason::SystemSuspended,
                    } => Ok(()),
                    ScanStopResult::Err { error } => validate_event_error(error),
                }
            }
            BleEventPayload::ConnectionStateChanged {
                command_id,
                connection_id,
                result,
            } => {
                if let Some(command_id) = command_id {
                    validate_id(command_id.is_valid(), "command_id")?;
                }
                validate_id(connection_id.is_valid(), "connection_id")?;
                if let ConnectionStateResult::Err { error } = result {
                    validate_event_error(error)?;
                }
                Ok(())
            }
            BleEventPayload::CommandCompleted { command_id, error } => {
                validate_id(command_id.is_valid(), "command_id")?;
                if let Some(error) = error {
                    validate_event_error(error)?;
                }
                Ok(())
            }
            BleEventPayload::BackendError { error } => validate_event_error(error),
        }
    }
}

fn validate_id(valid: bool, field: &'static str) -> Result<(), EventValidationError> {
    if valid {
        Ok(())
    } else {
        Err(EventValidationError::ZeroId { field })
    }
}

fn validate_event_error(error: &BleEventError) -> Result<(), EventValidationError> {
    validate_text(&error.message, "error.message")?;
    if let Some(platform) = &error.platform {
        for (value, field) in [
            (platform.code.as_ref(), "platform.code"),
            (platform.name.as_ref(), "platform.name"),
            (platform.message.as_ref(), "platform.message"),
        ] {
            if let Some(value) = value {
                validate_text(value, field)?;
            }
        }
    }
    Ok(())
}

fn validate_text(value: &str, field: &'static str) -> Result<(), EventValidationError> {
    if value.len() <= MAX_DIAGNOSTIC_BYTES {
        Ok(())
    } else {
        Err(EventValidationError::DiagnosticTooLong { field })
    }
}
