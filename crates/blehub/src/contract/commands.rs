use super::{CommandDispatchError, CommandId, ConnectionId, PeripheralId, ScanSessionId};

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, uniffi::Enum)]
pub enum CommandKind {
    StartScan,
    Connect,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, uniffi::Record)]
pub struct StartScanCommand {
    pub command_id: CommandId,
    pub scan_session_id: ScanSessionId,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, uniffi::Record)]
pub struct ConnectCommand {
    pub command_id: CommandId,
    pub connection_id: ConnectionId,
    pub peripheral_id: PeripheralId,
}

impl StartScanCommand {
    pub(crate) fn validate(&self) -> Result<(), CommandDispatchError> {
        validate_id(self.command_id.is_valid(), "command_id")?;
        validate_id(self.scan_session_id.is_valid(), "scan_session_id")
    }
}

impl ConnectCommand {
    pub(crate) fn validate(&self) -> Result<(), CommandDispatchError> {
        validate_id(self.command_id.is_valid(), "command_id")?;
        validate_id(self.connection_id.is_valid(), "connection_id")?;
        validate_id(self.peripheral_id.is_valid(), "peripheral_id")
    }
}

fn validate_id(valid: bool, field: &'static str) -> Result<(), CommandDispatchError> {
    if valid {
        Ok(())
    } else {
        Err(CommandDispatchError::InvalidRequest {
            detail: format!("{field} must be nonzero"),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn valid_commands_pass_validation() {
        assert_eq!(
            StartScanCommand {
                command_id: CommandId::new(1),
                scan_session_id: ScanSessionId::new(2),
            }
            .validate(),
            Ok(())
        );
        assert_eq!(
            ConnectCommand {
                command_id: CommandId::new(1),
                connection_id: ConnectionId::new(2),
                peripheral_id: PeripheralId::new(3),
            }
            .validate(),
            Ok(())
        );
    }

    #[test]
    fn validation_identifies_each_zero_id() {
        let valid_start_scan = StartScanCommand {
            command_id: CommandId::new(1),
            scan_session_id: ScanSessionId::new(2),
        };
        for (command, field) in [
            (
                StartScanCommand {
                    command_id: CommandId::new(0),
                    ..valid_start_scan
                },
                "command_id",
            ),
            (
                StartScanCommand {
                    scan_session_id: ScanSessionId::new(0),
                    ..valid_start_scan
                },
                "scan_session_id",
            ),
        ] {
            assert_eq!(
                command.validate(),
                Err(CommandDispatchError::InvalidRequest {
                    detail: format!("{field} must be nonzero"),
                })
            );
        }

        let valid_connect = ConnectCommand {
            command_id: CommandId::new(1),
            connection_id: ConnectionId::new(2),
            peripheral_id: PeripheralId::new(3),
        };
        for (command, field) in [
            (
                ConnectCommand {
                    command_id: CommandId::new(0),
                    ..valid_connect
                },
                "command_id",
            ),
            (
                ConnectCommand {
                    connection_id: ConnectionId::new(0),
                    ..valid_connect
                },
                "connection_id",
            ),
            (
                ConnectCommand {
                    peripheral_id: PeripheralId::new(0),
                    ..valid_connect
                },
                "peripheral_id",
            ),
        ] {
            assert_eq!(
                command.validate(),
                Err(CommandDispatchError::InvalidRequest {
                    detail: format!("{field} must be nonzero"),
                })
            );
        }
    }
}
