use super::{BleFailure, BleOperationError, CommandId, ConnectionId, ErrorCategory, ScanSessionId};

const MAX_DIAGNOSTIC_BYTES: usize = 1024;

#[boltffi::data]
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum ConnectionPhase {
    Connecting,
    Connected,
    Disconnecting,
    Disconnected,
    Failed,
}

#[boltffi::data]
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum CommandOutcome {
    Succeeded,
    Failed,
    TimedOut,
    Cancelled,
}

#[boltffi::data]
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum BleEventPayload {
    ScanStarted {
        command_id: CommandId,
        scan_session_id: ScanSessionId,
    },
    ConnectionStateChanged {
        command_id: Option<CommandId>,
        connection_id: ConnectionId,
        phase: ConnectionPhase,
        error: Option<BleFailure>,
    },
    CommandCompleted {
        command_id: CommandId,
        outcome: CommandOutcome,
        error: Option<BleOperationError>,
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
    #[display("invalid error shape for {context}")]
    InvalidErrorShape { context: &'static str },
    #[display("{field} exceeds {MAX_DIAGNOSTIC_BYTES} UTF-8 bytes")]
    DiagnosticTooLong { field: &'static str },
}

impl BleEvent {
    pub(crate) fn validate_shape(&self) -> Result<(), EventValidationError> {
        match &self.payload {
            BleEventPayload::ScanStarted {
                command_id,
                scan_session_id,
            } => {
                validate_id(command_id.is_valid(), "command_id")?;
                validate_id(scan_session_id.is_valid(), "scan_session_id")
            }
            BleEventPayload::ConnectionStateChanged {
                command_id,
                connection_id,
                phase,
                error,
            } => {
                if let Some(command_id) = command_id {
                    validate_id(command_id.is_valid(), "command_id")?;
                }
                validate_id(connection_id.is_valid(), "connection_id")?;
                match phase {
                    ConnectionPhase::Failed if error.is_none() => {
                        return Err(EventValidationError::InvalidErrorShape {
                            context: "ConnectionStateChanged(Failed)",
                        });
                    }
                    ConnectionPhase::Connecting
                    | ConnectionPhase::Connected
                    | ConnectionPhase::Disconnecting
                        if error.is_some() =>
                    {
                        return Err(EventValidationError::InvalidErrorShape {
                            context: "non-failing ConnectionStateChanged",
                        });
                    }
                    _ => {}
                }
                if let Some(error) = error {
                    validate_failure(error)?;
                }
                Ok(())
            }
            BleEventPayload::CommandCompleted {
                command_id,
                outcome,
                error,
            } => {
                validate_id(command_id.is_valid(), "command_id")?;
                let valid = match (outcome, error.as_ref()) {
                    (CommandOutcome::Succeeded, None) => true,
                    (CommandOutcome::Failed, Some(error)) => !matches!(
                        error.failure.category,
                        ErrorCategory::Timeout | ErrorCategory::Cancelled
                    ),
                    (CommandOutcome::TimedOut, Some(error)) => {
                        error.failure.category == ErrorCategory::Timeout
                    }
                    (CommandOutcome::Cancelled, Some(error)) => {
                        error.failure.category == ErrorCategory::Cancelled
                    }
                    _ => false,
                };
                if !valid {
                    return Err(EventValidationError::InvalidErrorShape {
                        context: "CommandCompleted",
                    });
                }
                if let Some(error) = error {
                    validate_failure(&error.failure)?;
                }
                Ok(())
            }
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

fn validate_failure(failure: &BleFailure) -> Result<(), EventValidationError> {
    validate_text(&failure.message, "failure.message")?;
    if let Some(platform) = &failure.platform {
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
