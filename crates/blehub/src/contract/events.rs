use super::{BleEventError, CommandId, ConnectionId, ScanSessionId};

const MAX_DIAGNOSTIC_BYTES: usize = 1024;

#[boltffi::data]
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum ScanStopReason {
    Requested { command_id: CommandId },
    SystemSuspended,
}

#[boltffi::data]
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum ConnectionState {
    Connecting,
    Connected,
    Disconnecting,
    Disconnected,
}

#[boltffi::data]
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum BleEventPayload {
    ScanStarted {
        command_id: CommandId,
        scan_id: ScanSessionId,
    },
    ScanStopped {
        scan_id: ScanSessionId,
        reason: Result<ScanStopReason, BleEventError>,
    },
    ConnectionStateChanged {
        command_id: Option<CommandId>,
        connection_id: ConnectionId,
        state: Result<ConnectionState, BleEventError>,
    },
    CommandCompleted {
        command_id: CommandId,
        error: Option<BleEventError>,
    },
    BackendError {
        error: BleEventError,
    },
}

#[boltffi::data]
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct BleEvent {
    pub payload: BleEventPayload,
}

#[derive(Debug, derive_more::Display, derive_more::Error)]
pub(crate) enum EventValidationError {
    #[display("{field} must be nonzero")]
    ZeroId { field: &'static str },

    #[display("{field} exceeds {MAX_DIAGNOSTIC_BYTES} UTF-8 bytes")]
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
            BleEventPayload::ScanStopped { scan_id, reason } => {
                validate_id(scan_id.is_valid(), "scan_id")?;
                match reason {
                    Ok(ScanStopReason::Requested { command_id }) => {
                        validate_id(command_id.is_valid(), "command_id")
                    }
                    Ok(ScanStopReason::SystemSuspended) => Ok(()),
                    Err(error) => validate_event_error(error),
                }
            }
            BleEventPayload::ConnectionStateChanged {
                command_id,
                connection_id,
                state,
            } => {
                if let Some(command_id) = command_id {
                    validate_id(command_id.is_valid(), "command_id")?;
                }
                validate_id(connection_id.is_valid(), "connection_id")?;
                if let Err(error) = state {
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
