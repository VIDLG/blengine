use std::collections::HashMap;

use blehub::{
    BleEvent, BleEventError, BondState, CommandId, CommandKind, ConnectionState,
    ConnectionStateResult, NotificationAction, NotificationMode, NotificationModeRequest,
    NotificationState, ScanStopReason, ScanStopResult,
};

use crate::RecordedCommand;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CommandEventKind {
    AdapterSnapshotChanged,
    ScanStarted,
    ScanStopped,
    ConnectionStateChanged,
    GattDiscovered,
    CharacteristicRead,
    CharacteristicWritten,
    NotificationStateChanged,
    RssiRead,
    MtuRead,
    DevicesListed,
    BondStateChanged,
    DescriptorRead,
    DescriptorWritten,
    CommandCompleted,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CompletionExpectation {
    SucceededWithDomain,
    SucceededWithoutDomain,
    Failed,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum BackendContractViolation {
    DuplicateCommandId {
        command_id: CommandId,
    },
    UnknownCommandEvent {
        command_id: CommandId,
        event: CommandEventKind,
    },
    CommandEventMismatch {
        command_id: CommandId,
        operation: CommandKind,
        event: CommandEventKind,
    },
    MissingDomainEventAttempt {
        command_id: CommandId,
        operation: CommandKind,
    },
    DuplicateDomainEventAttempt {
        command_id: CommandId,
        operation: CommandKind,
        attempt_count: usize,
    },
    UnexpectedDomainEventAttempt {
        command_id: CommandId,
        operation: CommandKind,
    },
    UnexpectedDomainEventBeforeFailure {
        command_id: CommandId,
        operation: CommandKind,
    },
    FailureEventErrorMismatch {
        command_id: CommandId,
    },
    FailureDomainWithSuccessfulTerminal {
        command_id: CommandId,
    },
    MissingTerminalAttempt {
        command_id: CommandId,
    },
    TerminalOutcomeMismatch {
        command_id: CommandId,
        expected: CompletionExpectation,
    },
    EventEmissionBeforeDispatchRejection {
        operation: CommandKind,
        emitted_event_count: usize,
    },
    EventAfterDispatchRejection {
        command_id: CommandId,
    },
    DuplicateTerminalAttempt {
        command_id: CommandId,
    },
    EventAfterTerminalAttempt {
        command_id: CommandId,
    },
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum DispatchState {
    Dispatching,
    Accepted,
    Rejected,
}

struct TrackedCommand {
    command: RecordedCommand,
    dispatch_state: DispatchState,
    event_attempt_count: usize,
    domain_event_attempt_count: usize,
    domain_error: Option<BleEventError>,
    terminal_attempted: bool,
    terminal_error: Option<BleEventError>,
}

#[derive(Default)]
pub struct BackendConformanceTracker {
    commands: HashMap<CommandId, TrackedCommand>,
    violations: Vec<BackendContractViolation>,
}

impl BackendConformanceTracker {
    pub fn begin_dispatch(&mut self, command: RecordedCommand) {
        let command_id = command.command_id();
        if self.commands.contains_key(&command_id) {
            self.violations
                .push(BackendContractViolation::DuplicateCommandId { command_id });
            return;
        }
        self.commands.insert(
            command_id,
            TrackedCommand {
                command,
                dispatch_state: DispatchState::Dispatching,
                event_attempt_count: 0,
                domain_event_attempt_count: 0,
                domain_error: None,
                terminal_attempted: false,
                terminal_error: None,
            },
        );
    }

    pub fn finish_dispatch(&mut self, command_id: CommandId, accepted: bool) {
        let Some(command) = self.commands.get_mut(&command_id) else {
            return;
        };
        command.dispatch_state = if accepted {
            DispatchState::Accepted
        } else {
            DispatchState::Rejected
        };
        if !accepted && command.event_attempt_count != 0 {
            self.violations.push(
                BackendContractViolation::EventEmissionBeforeDispatchRejection {
                    operation: command.command.operation(),
                    emitted_event_count: command.event_attempt_count,
                },
            );
        }
    }

    pub fn record_event_attempt(&mut self, event: &BleEvent) {
        let Some((command_id, event_kind)) = command_event(event) else {
            return;
        };
        let Some(command) = self.commands.get_mut(&command_id) else {
            self.violations
                .push(BackendContractViolation::UnknownCommandEvent {
                    command_id,
                    event: event_kind,
                });
            return;
        };

        command.event_attempt_count += 1;
        let operation = command.command.operation();
        let mut violations = Vec::new();
        if command.dispatch_state == DispatchState::Rejected {
            violations.push(BackendContractViolation::EventAfterDispatchRejection { command_id });
        }
        if event_kind == CommandEventKind::CommandCompleted {
            if command.terminal_attempted {
                violations.push(BackendContractViolation::DuplicateTerminalAttempt { command_id });
            } else {
                command.terminal_attempted = true;
                let BleEvent::CommandCompleted { error, .. } = event else {
                    unreachable!();
                };
                command.terminal_error = error.clone();
            }
        } else {
            if command.terminal_attempted {
                violations.push(BackendContractViolation::EventAfterTerminalAttempt { command_id });
            }
            if command_matches_event(&command.command, event) {
                if !command_event_is_progress(&command.command, event) {
                    command.domain_event_attempt_count += 1;
                    command.domain_error = command_domain_error(event).cloned();
                }
            } else {
                violations.push(BackendContractViolation::CommandEventMismatch {
                    command_id,
                    operation,
                    event: event_kind,
                });
            }
        }
        self.violations.extend(violations);
    }

    pub fn finish_completion(&mut self, command_id: CommandId, expected: CompletionExpectation) {
        let Some(command) = self.commands.get(&command_id) else {
            return;
        };
        let operation = command.command.operation();
        match expected {
            CompletionExpectation::SucceededWithDomain => {
                match command.domain_event_attempt_count {
                    0 => {
                        self.violations
                            .push(BackendContractViolation::MissingDomainEventAttempt {
                                command_id,
                                operation,
                            })
                    }
                    1 => {}
                    attempt_count => self.violations.push(
                        BackendContractViolation::DuplicateDomainEventAttempt {
                            command_id,
                            operation,
                            attempt_count,
                        },
                    ),
                }
                if command.domain_error.is_some() {
                    self.violations.push(
                        BackendContractViolation::FailureDomainWithSuccessfulTerminal {
                            command_id,
                        },
                    );
                }
            }
            CompletionExpectation::SucceededWithoutDomain => {
                if command.domain_event_attempt_count != 0 {
                    self.violations
                        .push(BackendContractViolation::UnexpectedDomainEventAttempt {
                            command_id,
                            operation,
                        });
                }
            }
            CompletionExpectation::Failed => {
                if command.domain_event_attempt_count > 1 {
                    self.violations
                        .push(BackendContractViolation::DuplicateDomainEventAttempt {
                            command_id,
                            operation,
                            attempt_count: command.domain_event_attempt_count,
                        });
                }
                if command.domain_event_attempt_count == 1 {
                    match &command.domain_error {
                        Some(error) if command.terminal_error.as_ref() != Some(error) => {
                            self.violations.push(
                                BackendContractViolation::FailureEventErrorMismatch { command_id },
                            )
                        }
                        None => self.violations.push(
                            BackendContractViolation::UnexpectedDomainEventBeforeFailure {
                                command_id,
                                operation,
                            },
                        ),
                        Some(_) => {}
                    }
                }
            }
        }
        if !command.terminal_attempted {
            self.violations
                .push(BackendContractViolation::MissingTerminalAttempt { command_id });
            return;
        }
        let terminal_failed = command.terminal_error.is_some();
        let outcome_matches = match expected {
            CompletionExpectation::SucceededWithDomain
            | CompletionExpectation::SucceededWithoutDomain => !terminal_failed,
            CompletionExpectation::Failed => terminal_failed,
        };
        if !outcome_matches {
            self.violations
                .push(BackendContractViolation::TerminalOutcomeMismatch {
                    command_id,
                    expected,
                });
        }
    }

    pub fn terminal_attempted(&self, command_id: CommandId) -> bool {
        self.commands
            .get(&command_id)
            .is_some_and(|command| command.terminal_attempted)
    }

    pub fn violations(&self) -> &[BackendContractViolation] {
        &self.violations
    }
}

pub(crate) fn command_event(event: &BleEvent) -> Option<(CommandId, CommandEventKind)> {
    match event {
        BleEvent::AdapterSnapshotChanged {
            command_id: Some(command_id),
            ..
        } => Some((*command_id, CommandEventKind::AdapterSnapshotChanged)),
        BleEvent::ScanStarted { command_id, .. } => {
            Some((*command_id, CommandEventKind::ScanStarted))
        }
        BleEvent::ScanStopped {
            result:
                ScanStopResult::Ok {
                    reason: ScanStopReason::Requested { command_id },
                },
            ..
        } => Some((*command_id, CommandEventKind::ScanStopped)),
        BleEvent::ConnectionStateChanged {
            command_id: Some(command_id),
            ..
        } => Some((*command_id, CommandEventKind::ConnectionStateChanged)),
        BleEvent::GattDiscovered { command_id, .. } => {
            Some((*command_id, CommandEventKind::GattDiscovered))
        }
        BleEvent::CharacteristicRead { command_id, .. } => {
            Some((*command_id, CommandEventKind::CharacteristicRead))
        }
        BleEvent::CharacteristicWritten { command_id, .. } => {
            Some((*command_id, CommandEventKind::CharacteristicWritten))
        }
        BleEvent::DescriptorRead { command_id, .. } => {
            Some((*command_id, CommandEventKind::DescriptorRead))
        }
        BleEvent::DescriptorWritten { command_id, .. } => {
            Some((*command_id, CommandEventKind::DescriptorWritten))
        }
        BleEvent::NotificationStateChanged { command_id, .. } => {
            Some((*command_id, CommandEventKind::NotificationStateChanged))
        }
        BleEvent::RssiRead { command_id, .. } => Some((*command_id, CommandEventKind::RssiRead)),
        BleEvent::MtuRead { command_id, .. } => Some((*command_id, CommandEventKind::MtuRead)),
        BleEvent::DevicesListed { command_id, .. } => {
            Some((*command_id, CommandEventKind::DevicesListed))
        }
        BleEvent::BondStateChanged {
            command_id: Some(command_id),
            ..
        } => Some((*command_id, CommandEventKind::BondStateChanged)),
        BleEvent::CommandCompleted { command_id, .. } => {
            Some((*command_id, CommandEventKind::CommandCompleted))
        }
        BleEvent::AdapterSnapshotChanged {
            command_id: None, ..
        }
        | BleEvent::AdvertisementObserved { .. }
        | BleEvent::ScanStopped { .. }
        | BleEvent::ConnectionStateChanged {
            command_id: None, ..
        }
        | BleEvent::BondStateChanged {
            command_id: None, ..
        }
        | BleEvent::NotificationReceived { .. }
        | BleEvent::CompanionPresenceChanged { .. }
        | BleEvent::BackendError { .. } => None,
    }
}

fn command_domain_error(event: &BleEvent) -> Option<&BleEventError> {
    match event {
        BleEvent::ConnectionStateChanged {
            result: ConnectionStateResult::Err { error },
            ..
        } => Some(error),
        _ => None,
    }
}

fn command_event_is_progress(command: &RecordedCommand, event: &BleEvent) -> bool {
    matches!(
        (command, event),
        (
            RecordedCommand::Connect(_),
            BleEvent::ConnectionStateChanged {
                result: ConnectionStateResult::Ok {
                    state: ConnectionState::Connecting
                },
                ..
            },
        ) | (
            RecordedCommand::Disconnect(_),
            BleEvent::ConnectionStateChanged {
                result: ConnectionStateResult::Ok {
                    state: ConnectionState::Disconnecting
                },
                ..
            },
        ) | (
            RecordedCommand::CreateBond(_),
            BleEvent::BondStateChanged {
                state: BondState::Bonding,
                ..
            },
        ) | (
            RecordedCommand::RemoveBond(_),
            BleEvent::BondStateChanged {
                state: BondState::Bonding,
                ..
            },
        )
    )
}

pub(crate) fn command_matches_event(command: &RecordedCommand, event: &BleEvent) -> bool {
    match (command, event) {
        (
            RecordedCommand::Initialize(command),
            BleEvent::AdapterSnapshotChanged {
                command_id: Some(command_id),
                ..
            },
        ) => command.command_id == *command_id,
        (
            RecordedCommand::StartScan(command),
            BleEvent::ScanStarted {
                command_id,
                scan_id,
            },
        ) => command.command_id == *command_id && command.scan_session_id == *scan_id,
        (
            RecordedCommand::StopScan(command),
            BleEvent::ScanStopped {
                scan_id,
                result:
                    ScanStopResult::Ok {
                        reason: ScanStopReason::Requested { command_id },
                    },
            },
        ) => command.command_id == *command_id && command.scan_session_id == *scan_id,
        (
            RecordedCommand::Connect(command),
            BleEvent::ConnectionStateChanged {
                command_id: Some(command_id),
                connection_id,
                result,
            },
        ) => {
            command.command_id == *command_id
                && command.connection_id == *connection_id
                && matches!(
                    result,
                    ConnectionStateResult::Ok {
                        state: ConnectionState::Connecting | ConnectionState::Connected,
                    } | ConnectionStateResult::Err { .. }
                )
        }
        (
            RecordedCommand::Disconnect(command),
            BleEvent::ConnectionStateChanged {
                command_id: Some(command_id),
                connection_id,
                result,
            },
        ) => {
            command.command_id == *command_id
                && command.connection_id == *connection_id
                && matches!(
                    result,
                    ConnectionStateResult::Ok {
                        state: ConnectionState::Disconnecting | ConnectionState::Disconnected,
                    } | ConnectionStateResult::Err { .. }
                )
        }
        (
            RecordedCommand::DiscoverGatt(command),
            BleEvent::GattDiscovered {
                command_id,
                database,
            },
        ) => {
            command.command_id == *command_id
                && command.connection_id == database.connection_id
                && command.gatt_session_id == database.gatt_session_id
        }
        (
            RecordedCommand::ReadCharacteristic(command),
            BleEvent::CharacteristicRead {
                command_id,
                connection_id,
                gatt_session_id,
                characteristic_id,
                ..
            },
        ) => {
            command.command_id == *command_id
                && command.connection_id == *connection_id
                && command.gatt_session_id == *gatt_session_id
                && command.characteristic_id == *characteristic_id
        }
        (
            RecordedCommand::WriteCharacteristic(command),
            BleEvent::CharacteristicWritten {
                command_id,
                connection_id,
                gatt_session_id,
                characteristic_id,
            },
        ) => {
            command.command_id == *command_id
                && command.connection_id == *connection_id
                && command.gatt_session_id == *gatt_session_id
                && command.characteristic_id == *characteristic_id
        }
        (
            RecordedCommand::ReadDescriptor(command),
            BleEvent::DescriptorRead {
                command_id,
                connection_id,
                gatt_session_id,
                characteristic_id,
                descriptor_id,
                ..
            },
        ) => {
            command.command_id == *command_id
                && command.connection_id == *connection_id
                && command.gatt_session_id == *gatt_session_id
                && command.characteristic_id == *characteristic_id
                && command.descriptor_id == *descriptor_id
        }
        (
            RecordedCommand::WriteDescriptor(command),
            BleEvent::DescriptorWritten {
                command_id,
                connection_id,
                gatt_session_id,
                characteristic_id,
                descriptor_id,
            },
        ) => {
            command.command_id == *command_id
                && command.connection_id == *connection_id
                && command.gatt_session_id == *gatt_session_id
                && command.characteristic_id == *characteristic_id
                && command.descriptor_id == *descriptor_id
        }
        (
            RecordedCommand::SetNotifications(command),
            BleEvent::NotificationStateChanged {
                command_id,
                connection_id,
                gatt_session_id,
                subscription_id,
                characteristic_id,
                state,
            },
        ) => {
            let action_matches = matches!(
                (command.action, state),
                (
                    NotificationAction::Enable {
                        mode: NotificationModeRequest::PlatformDefault,
                    },
                    NotificationState::Enabled { .. },
                ) | (
                    NotificationAction::Enable {
                        mode: NotificationModeRequest::RequireNotification,
                    },
                    NotificationState::Enabled {
                        actual_mode: Some(NotificationMode::Notification),
                    },
                ) | (
                    NotificationAction::Enable {
                        mode: NotificationModeRequest::RequireIndication,
                    },
                    NotificationState::Enabled {
                        actual_mode: Some(NotificationMode::Indication),
                    },
                ) | (NotificationAction::Disable, NotificationState::Disabled)
            );
            command.command_id == *command_id
                && command.connection_id == *connection_id
                && command.gatt_session_id == *gatt_session_id
                && command.subscription_id == *subscription_id
                && command.characteristic_id == *characteristic_id
                && action_matches
        }
        (
            RecordedCommand::ReadRssi(command),
            BleEvent::RssiRead {
                command_id,
                connection_id,
                ..
            },
        ) => command.command_id == *command_id && command.connection_id == *connection_id,
        (
            RecordedCommand::Mtu(command),
            BleEvent::MtuRead {
                command_id,
                connection_id,
                ..
            },
        ) => command.command_id == *command_id && command.connection_id == *connection_id,
        (
            RecordedCommand::CreateBond(command),
            BleEvent::BondStateChanged {
                command_id: Some(command_id),
                connection_id,
                ..
            },
        ) => command.command_id == *command_id && command.connection_id == *connection_id,
        (
            RecordedCommand::RemoveBond(command),
            BleEvent::BondStateChanged {
                command_id: Some(command_id),
                connection_id,
                ..
            },
        ) => command.command_id == *command_id && command.connection_id == *connection_id,
        (_, BleEvent::CommandCompleted { command_id, .. }) => command.command_id() == *command_id,
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use blehub::{
        AdapterSnapshot, AdapterState, AuthorizationState, BleErrorCategory, ConnectCommand,
        ConnectionId, ConnectionState, ConnectionStateResult, DisconnectCommand,
        DiscoverGattCommand, GattCharacteristicId, GattSessionId, InitializeCommand,
        NotificationAction, NotificationMode, NotificationModeRequest, NotificationState,
        PeripheralId, ReadCharacteristicCommand, ReadRssiCommand, ScanOptions, ScanSessionId,
        SetNotificationsCommand, StartScanCommand, StopScanCommand, SubscriptionId,
        WriteCharacteristicCommand, WriteType,
    };

    use super::*;
    use crate::duplicate_gatt_database_fixture;

    fn assert_successful_completion(command: RecordedCommand, domain_event: BleEvent) {
        let command_id = command.command_id();
        let mut tracker = BackendConformanceTracker::default();
        tracker.begin_dispatch(command);
        tracker.finish_dispatch(command_id, true);
        tracker.record_event_attempt(&domain_event);
        tracker.record_event_attempt(&BleEvent::CommandCompleted {
            command_id,
            error: None,
        });
        tracker.finish_completion(command_id, CompletionExpectation::SucceededWithDomain);
        assert_eq!(tracker.violations(), &[]);
    }

    fn connection_commands() -> [RecordedCommand; 2] {
        let command_id = CommandId { value: 1 };
        let connection_id = ConnectionId { value: 2 };
        [
            RecordedCommand::Connect(ConnectCommand {
                auto_connect: false,
                command_id,
                connection_id,
                peripheral_id: PeripheralId { value: 3 },
            }),
            RecordedCommand::Disconnect(DisconnectCommand {
                command_id,
                connection_id,
            }),
        ]
    }

    fn connection_event(command: &RecordedCommand, state: ConnectionState) -> BleEvent {
        let connection_id = match command {
            RecordedCommand::Connect(command) => command.connection_id,
            RecordedCommand::Disconnect(command) => command.connection_id,
            _ => unreachable!(),
        };
        BleEvent::ConnectionStateChanged {
            command_id: Some(command.command_id()),
            connection_id,
            result: ConnectionStateResult::Ok { state },
        }
    }

    #[test]
    fn connection_progress_before_success_preserves_one_final_domain() {
        for (command, progress, final_state) in [
            (
                connection_commands()[0].clone(),
                ConnectionState::Connecting,
                ConnectionState::Connected,
            ),
            (
                connection_commands()[1].clone(),
                ConnectionState::Disconnecting,
                ConnectionState::Disconnected,
            ),
        ] {
            let command_id = command.command_id();
            let mut tracker = BackendConformanceTracker::default();
            tracker.begin_dispatch(command.clone());
            tracker.record_event_attempt(&connection_event(&command, progress));
            tracker.finish_dispatch(command_id, true);
            tracker.record_event_attempt(&connection_event(&command, final_state));
            tracker.record_event_attempt(&BleEvent::CommandCompleted {
                command_id,
                error: None,
            });
            tracker.finish_completion(command_id, CompletionExpectation::SucceededWithDomain);
            assert_eq!(tracker.violations(), &[], "{command:?}");
        }
    }

    fn assert_connection_success_rejects_states(
        command: RecordedCommand,
        states: &[ConnectionState],
    ) {
        for &state in states {
            let command_id = command.command_id();
            let mut tracker = BackendConformanceTracker::default();
            tracker.begin_dispatch(command.clone());
            tracker.finish_dispatch(command_id, true);
            tracker.record_event_attempt(&connection_event(&command, state));
            tracker.record_event_attempt(&BleEvent::CommandCompleted {
                command_id,
                error: None,
            });
            tracker.finish_completion(command_id, CompletionExpectation::SucceededWithDomain);
            assert!(
                tracker.violations().contains(
                    &BackendContractViolation::MissingDomainEventAttempt {
                        command_id,
                        operation: command.operation(),
                    }
                ),
                "{command:?} must not complete successfully with only {state:?}: {:?}",
                tracker.violations()
            );
        }
    }

    #[test]
    fn connect_success_requires_connected_final_state() {
        assert_connection_success_rejects_states(
            connection_commands()[0].clone(),
            &[
                ConnectionState::Connecting,
                ConnectionState::Disconnected,
                ConnectionState::Disconnecting,
            ],
        );
    }

    #[test]
    fn disconnect_success_requires_disconnected_final_state() {
        assert_connection_success_rejects_states(
            connection_commands()[1].clone(),
            &[
                ConnectionState::Disconnecting,
                ConnectionState::Connected,
                ConnectionState::Connecting,
            ],
        );
    }

    #[test]
    fn connection_progress_before_failure_preserves_failure_state_and_error_checks() {
        for (command, progress) in [
            (
                connection_commands()[0].clone(),
                ConnectionState::Connecting,
            ),
            (
                connection_commands()[1].clone(),
                ConnectionState::Disconnecting,
            ),
        ] {
            for mismatch in [false, true] {
                let command_id = command.command_id();
                let error = BleEventError {
                    category: BleErrorCategory::Connection,
                    message: "connection failed".to_owned(),
                    platform_details: None,
                };
                let mut tracker = BackendConformanceTracker::default();
                tracker.begin_dispatch(command.clone());
                tracker.finish_dispatch(command_id, true);
                tracker.record_event_attempt(&connection_event(&command, progress));
                let mut failure = connection_event(&command, progress);
                let BleEvent::ConnectionStateChanged { result, .. } = &mut failure else {
                    unreachable!();
                };
                *result = ConnectionStateResult::Err {
                    error: error.clone(),
                };
                tracker.record_event_attempt(&failure);
                tracker.record_event_attempt(&BleEvent::CommandCompleted {
                    command_id,
                    error: Some(if mismatch {
                        BleEventError {
                            message: "different error".to_owned(),
                            ..error
                        }
                    } else {
                        error
                    }),
                });
                tracker.finish_completion(command_id, CompletionExpectation::Failed);
                let expected = if mismatch {
                    vec![BackendContractViolation::FailureEventErrorMismatch { command_id }]
                } else {
                    Vec::new()
                };
                assert_eq!(tracker.violations(), expected, "{command:?}");
            }
        }
    }

    #[test]
    fn connection_progress_still_violates_dispatch_rejection_and_terminal_order() {
        for (command, progress) in [
            (
                connection_commands()[0].clone(),
                ConnectionState::Connecting,
            ),
            (
                connection_commands()[1].clone(),
                ConnectionState::Disconnecting,
            ),
        ] {
            let command_id = command.command_id();
            let event = connection_event(&command, progress);
            let mut rejected = BackendConformanceTracker::default();
            rejected.begin_dispatch(command.clone());
            rejected.record_event_attempt(&event);
            rejected.finish_dispatch(command_id, false);
            rejected.record_event_attempt(&event);
            assert_eq!(
                rejected.violations(),
                &[
                    BackendContractViolation::EventEmissionBeforeDispatchRejection {
                        operation: command.operation(),
                        emitted_event_count: 1,
                    },
                    BackendContractViolation::EventAfterDispatchRejection { command_id },
                ]
            );

            let mut completed = BackendConformanceTracker::default();
            completed.begin_dispatch(command);
            completed.finish_dispatch(command_id, true);
            completed.record_event_attempt(&BleEvent::CommandCompleted {
                command_id,
                error: None,
            });
            completed.record_event_attempt(&event);
            assert_eq!(
                completed.violations(),
                &[BackendContractViolation::EventAfterTerminalAttempt { command_id }]
            );
        }
    }

    #[test]
    fn every_command_kind_has_a_typed_success_domain_event_mapping() {
        let connection_id = ConnectionId { value: 20 };
        let gatt_session_id = GattSessionId { value: 30 };
        let characteristic_id = GattCharacteristicId { value: 40 };
        let subscription_id = SubscriptionId { value: 50 };

        assert_successful_completion(
            RecordedCommand::Initialize(InitializeCommand {
                command_id: CommandId { value: 1 },
            }),
            BleEvent::AdapterSnapshotChanged {
                command_id: Some(CommandId { value: 1 }),
                snapshot: AdapterSnapshot {
                    state: AdapterState::PoweredOn,
                    scan_authorization: AuthorizationState::Authorized,
                    connection_authorization: AuthorizationState::Authorized,
                    name: None,
                },
            },
        );
        assert_successful_completion(
            RecordedCommand::StartScan(StartScanCommand {
                command_id: CommandId { value: 2 },
                scan_session_id: ScanSessionId { value: 10 },
                options: ScanOptions::default(),
            }),
            BleEvent::ScanStarted {
                command_id: CommandId { value: 2 },
                scan_id: ScanSessionId { value: 10 },
            },
        );
        assert_successful_completion(
            RecordedCommand::StopScan(StopScanCommand {
                command_id: CommandId { value: 3 },
                scan_session_id: ScanSessionId { value: 10 },
            }),
            BleEvent::ScanStopped {
                scan_id: ScanSessionId { value: 10 },
                result: ScanStopResult::Ok {
                    reason: ScanStopReason::Requested {
                        command_id: CommandId { value: 3 },
                    },
                },
            },
        );
        assert_successful_completion(
            RecordedCommand::Connect(ConnectCommand {
                auto_connect: false,
                command_id: CommandId { value: 4 },
                connection_id,
                peripheral_id: PeripheralId { value: 21 },
            }),
            BleEvent::ConnectionStateChanged {
                command_id: Some(CommandId { value: 4 }),
                connection_id,
                result: ConnectionStateResult::Ok {
                    state: ConnectionState::Connected,
                },
            },
        );
        assert_successful_completion(
            RecordedCommand::Disconnect(DisconnectCommand {
                command_id: CommandId { value: 5 },
                connection_id,
            }),
            BleEvent::ConnectionStateChanged {
                command_id: Some(CommandId { value: 5 }),
                connection_id,
                result: ConnectionStateResult::Ok {
                    state: ConnectionState::Disconnected,
                },
            },
        );

        let mut database = duplicate_gatt_database_fixture();
        database.connection_id = connection_id;
        database.gatt_session_id = gatt_session_id;
        assert_successful_completion(
            RecordedCommand::DiscoverGatt(DiscoverGattCommand {
                command_id: CommandId { value: 6 },
                connection_id,
                gatt_session_id,
            }),
            BleEvent::GattDiscovered {
                command_id: CommandId { value: 6 },
                database,
            },
        );
        assert_successful_completion(
            RecordedCommand::ReadCharacteristic(ReadCharacteristicCommand {
                command_id: CommandId { value: 7 },
                connection_id,
                gatt_session_id,
                characteristic_id,
            }),
            BleEvent::CharacteristicRead {
                command_id: CommandId { value: 7 },
                connection_id,
                gatt_session_id,
                characteristic_id,
                value: vec![1],
            },
        );
        assert_successful_completion(
            RecordedCommand::WriteCharacteristic(WriteCharacteristicCommand {
                command_id: CommandId { value: 8 },
                connection_id,
                gatt_session_id,
                characteristic_id,
                value: vec![2],
                write_type: WriteType::WithoutResponse,
            }),
            BleEvent::CharacteristicWritten {
                command_id: CommandId { value: 8 },
                connection_id,
                gatt_session_id,
                characteristic_id,
            },
        );
        assert_successful_completion(
            RecordedCommand::SetNotifications(SetNotificationsCommand {
                command_id: CommandId { value: 9 },
                connection_id,
                gatt_session_id,
                subscription_id,
                characteristic_id,
                action: NotificationAction::Enable {
                    mode: NotificationModeRequest::RequireNotification,
                },
            }),
            BleEvent::NotificationStateChanged {
                command_id: CommandId { value: 9 },
                connection_id,
                gatt_session_id,
                subscription_id,
                characteristic_id,
                state: NotificationState::Enabled {
                    actual_mode: Some(NotificationMode::Notification),
                },
            },
        );
        assert_successful_completion(
            RecordedCommand::ReadRssi(ReadRssiCommand {
                command_id: CommandId { value: 10 },
                connection_id,
            }),
            BleEvent::RssiRead {
                command_id: CommandId { value: 10 },
                connection_id,
                rssi: -42,
            },
        );
    }

    #[test]
    fn duplicate_domain_attempt_is_rejected_even_with_one_terminal() {
        let command_id = CommandId { value: 1 };
        let scan_id = ScanSessionId { value: 2 };
        let command = RecordedCommand::StartScan(StartScanCommand {
            command_id,
            scan_session_id: scan_id,
            options: ScanOptions::default(),
        });
        let domain = BleEvent::ScanStarted {
            command_id,
            scan_id,
        };
        let mut tracker = BackendConformanceTracker::default();
        tracker.begin_dispatch(command);
        tracker.finish_dispatch(command_id, true);
        tracker.record_event_attempt(&domain);
        tracker.record_event_attempt(&domain);
        tracker.record_event_attempt(&BleEvent::CommandCompleted {
            command_id,
            error: None,
        });

        tracker.finish_completion(command_id, CompletionExpectation::SucceededWithDomain);

        assert_eq!(
            tracker.violations(),
            &[BackendContractViolation::DuplicateDomainEventAttempt {
                command_id,
                operation: CommandKind::StartScan,
                attempt_count: 2,
            }]
        );
    }

    #[test]
    fn successful_domain_data_cannot_precede_a_failed_terminal() {
        let command_id = CommandId { value: 1 };
        let connection_id = ConnectionId { value: 2 };
        let gatt_session_id = GattSessionId { value: 3 };
        let characteristic_id = GattCharacteristicId { value: 4 };
        let command = RecordedCommand::ReadCharacteristic(ReadCharacteristicCommand {
            command_id,
            connection_id,
            gatt_session_id,
            characteristic_id,
        });
        let mut tracker = BackendConformanceTracker::default();
        tracker.begin_dispatch(command);
        tracker.finish_dispatch(command_id, true);
        tracker.record_event_attempt(&BleEvent::CharacteristicRead {
            command_id,
            connection_id,
            gatt_session_id,
            characteristic_id,
            value: vec![1],
        });
        tracker.record_event_attempt(&BleEvent::CommandCompleted {
            command_id,
            error: Some(BleEventError {
                category: BleErrorCategory::Gatt,
                message: "late failure".to_owned(),
                platform_details: None,
            }),
        });

        tracker.finish_completion(command_id, CompletionExpectation::Failed);

        assert_eq!(
            tracker.violations(),
            &[
                BackendContractViolation::UnexpectedDomainEventBeforeFailure {
                    command_id,
                    operation: CommandKind::ReadCharacteristic,
                }
            ]
        );
    }

    #[test]
    fn failure_state_and_terminal_must_reuse_the_same_error() {
        let command_id = CommandId { value: 1 };
        let connection_id = ConnectionId { value: 2 };
        let state_error = BleEventError {
            category: BleErrorCategory::Connection,
            message: "state failure".to_owned(),
            platform_details: None,
        };
        let terminal_error = BleEventError {
            message: "terminal failure".to_owned(),
            ..state_error.clone()
        };
        let command = RecordedCommand::Connect(ConnectCommand {
            auto_connect: false,
            command_id,
            connection_id,
            peripheral_id: PeripheralId { value: 3 },
        });
        let mut tracker = BackendConformanceTracker::default();
        tracker.begin_dispatch(command);
        tracker.finish_dispatch(command_id, true);
        tracker.record_event_attempt(&BleEvent::ConnectionStateChanged {
            command_id: Some(command_id),
            connection_id,
            result: ConnectionStateResult::Err { error: state_error },
        });
        tracker.record_event_attempt(&BleEvent::CommandCompleted {
            command_id,
            error: Some(terminal_error),
        });

        tracker.finish_completion(command_id, CompletionExpectation::Failed);

        assert_eq!(
            tracker.violations(),
            &[BackendContractViolation::FailureEventErrorMismatch { command_id }]
        );
    }

    #[test]
    fn strict_notification_request_rejects_a_different_actual_mode() {
        let command_id = CommandId { value: 1 };
        let connection_id = ConnectionId { value: 2 };
        let gatt_session_id = GattSessionId { value: 3 };
        let subscription_id = SubscriptionId { value: 4 };
        let characteristic_id = GattCharacteristicId { value: 5 };
        let command = RecordedCommand::SetNotifications(SetNotificationsCommand {
            command_id,
            connection_id,
            gatt_session_id,
            subscription_id,
            characteristic_id,
            action: NotificationAction::Enable {
                mode: NotificationModeRequest::RequireIndication,
            },
        });
        let mut tracker = BackendConformanceTracker::default();
        tracker.begin_dispatch(command);
        tracker.finish_dispatch(command_id, true);
        tracker.record_event_attempt(&BleEvent::NotificationStateChanged {
            command_id,
            connection_id,
            gatt_session_id,
            subscription_id,
            characteristic_id,
            state: NotificationState::Enabled {
                actual_mode: Some(NotificationMode::Notification),
            },
        });

        assert_eq!(
            tracker.violations(),
            &[BackendContractViolation::CommandEventMismatch {
                command_id,
                operation: CommandKind::SetNotifications,
                event: CommandEventKind::NotificationStateChanged,
            }]
        );
    }
}
