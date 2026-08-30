use std::collections::{HashMap, HashSet};

use blehub::{
    BleEvent, BleEventError, CommandDispatchError, CommandId, ConnectionId, ConnectionState,
    ConnectionStateResult, GattCharacteristicId, GattSessionId, NotificationState, ScanSessionId,
    SubscriptionId,
};

use crate::{
    RecordedCommand,
    conformance::{command_event, command_matches_event},
};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EventDisposition {
    Accepted,
    Duplicate,
    Stale,
    Orphan,
    Late,
    ContractViolation,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ProjectedCommandOutcome {
    DispatchRejected(CommandDispatchError),
    Succeeded,
    Failed(BleEventError),
    TimedOut,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
struct SubscriptionScope {
    connection_id: ConnectionId,
    gatt_session_id: GattSessionId,
    subscription_id: SubscriptionId,
    characteristic_id: GattCharacteristicId,
}

#[derive(Default)]
pub struct ReferenceProjection {
    pending: HashSet<CommandId>,
    commands: HashMap<CommandId, RecordedCommand>,
    outcomes: HashMap<CommandId, ProjectedCommandOutcome>,
    active_scans: HashSet<ScanSessionId>,
    active_connections: HashSet<ConnectionId>,
    active_gatt_sessions: HashSet<(ConnectionId, GattSessionId)>,
    active_subscriptions: HashSet<SubscriptionScope>,
}

impl ReferenceProjection {
    pub fn register_command(&mut self, command_id: CommandId) -> bool {
        !self.outcomes.contains_key(&command_id) && self.pending.insert(command_id)
    }

    pub fn register(&mut self, command: RecordedCommand) -> bool {
        let command_id = command.command_id();
        if !self.register_command(command_id) {
            return false;
        }
        self.commands.insert(command_id, command);
        true
    }

    pub fn dispatch_rejected(
        &mut self,
        command_id: CommandId,
        error: CommandDispatchError,
    ) -> bool {
        if !self.pending.remove(&command_id) {
            return false;
        }
        self.outcomes
            .insert(command_id, ProjectedCommandOutcome::DispatchRejected(error));
        true
    }

    pub fn apply(&mut self, event: &BleEvent) -> EventDisposition {
        if let BleEvent::CommandCompleted { command_id, error } = event {
            return self.apply_terminal(*command_id, error);
        }

        if let Some((command_id, _)) = command_event(event) {
            if let Some(outcome) = self.outcomes.get(&command_id) {
                return match outcome {
                    ProjectedCommandOutcome::TimedOut => EventDisposition::Late,
                    ProjectedCommandOutcome::DispatchRejected(_) => {
                        EventDisposition::ContractViolation
                    }
                    ProjectedCommandOutcome::Succeeded | ProjectedCommandOutcome::Failed(_) => {
                        EventDisposition::Duplicate
                    }
                };
            }
            if !self.pending.contains(&command_id) {
                return EventDisposition::Orphan;
            }
            if self
                .commands
                .get(&command_id)
                .is_some_and(|command| !command_matches_event(command, event))
            {
                return EventDisposition::ContractViolation;
            }
            if self.command_domain_is_stale(event) {
                return EventDisposition::Stale;
            }
            self.apply_domain_state(event);
            return EventDisposition::Accepted;
        }

        self.apply_passive_event(event)
    }

    pub fn timeout(&mut self, command_id: CommandId) -> bool {
        if !self.pending.remove(&command_id) {
            return false;
        }
        self.outcomes
            .insert(command_id, ProjectedCommandOutcome::TimedOut);
        true
    }

    pub fn activate_scan(&mut self, scan_session_id: ScanSessionId) {
        self.active_scans.insert(scan_session_id);
    }

    pub fn invalidate_scan(&mut self, scan_session_id: ScanSessionId) {
        self.active_scans.remove(&scan_session_id);
    }

    pub fn activate_connection(&mut self, connection_id: ConnectionId) {
        self.active_connections.insert(connection_id);
    }

    pub fn invalidate_connection(&mut self, connection_id: ConnectionId) {
        self.active_connections.remove(&connection_id);
        self.active_gatt_sessions
            .retain(|(candidate, _)| *candidate != connection_id);
        self.active_subscriptions
            .retain(|scope| scope.connection_id != connection_id);
    }

    pub fn activate_gatt_session(
        &mut self,
        connection_id: ConnectionId,
        gatt_session_id: GattSessionId,
    ) {
        self.active_gatt_sessions
            .retain(|(candidate, _)| *candidate != connection_id);
        self.active_subscriptions
            .retain(|scope| scope.connection_id != connection_id);
        self.active_gatt_sessions
            .insert((connection_id, gatt_session_id));
    }

    pub fn invalidate_gatt_session(
        &mut self,
        connection_id: ConnectionId,
        gatt_session_id: GattSessionId,
    ) {
        self.active_gatt_sessions
            .remove(&(connection_id, gatt_session_id));
        self.active_subscriptions.retain(|scope| {
            scope.connection_id != connection_id || scope.gatt_session_id != gatt_session_id
        });
    }

    pub fn activate_subscription(
        &mut self,
        connection_id: ConnectionId,
        gatt_session_id: GattSessionId,
        subscription_id: SubscriptionId,
        characteristic_id: GattCharacteristicId,
    ) {
        self.active_subscriptions.insert(SubscriptionScope {
            connection_id,
            gatt_session_id,
            subscription_id,
            characteristic_id,
        });
    }

    pub fn invalidate_subscription(
        &mut self,
        connection_id: ConnectionId,
        gatt_session_id: GattSessionId,
        subscription_id: SubscriptionId,
        characteristic_id: GattCharacteristicId,
    ) {
        self.active_subscriptions.remove(&SubscriptionScope {
            connection_id,
            gatt_session_id,
            subscription_id,
            characteristic_id,
        });
    }

    pub fn is_pending(&self, command_id: CommandId) -> bool {
        self.pending.contains(&command_id)
    }

    pub fn outcome(&self, command_id: CommandId) -> Option<ProjectedCommandOutcome> {
        self.outcomes.get(&command_id).cloned()
    }

    fn apply_terminal(
        &mut self,
        command_id: CommandId,
        error: &Option<BleEventError>,
    ) -> EventDisposition {
        if let Some(outcome) = self.outcomes.get(&command_id) {
            return match outcome {
                ProjectedCommandOutcome::TimedOut => EventDisposition::Late,
                ProjectedCommandOutcome::DispatchRejected(_) => EventDisposition::ContractViolation,
                ProjectedCommandOutcome::Succeeded | ProjectedCommandOutcome::Failed(_) => {
                    EventDisposition::Duplicate
                }
            };
        }
        if !self.pending.remove(&command_id) {
            return EventDisposition::Orphan;
        }
        let outcome = match error {
            Some(error) => ProjectedCommandOutcome::Failed(error.clone()),
            None => ProjectedCommandOutcome::Succeeded,
        };
        self.outcomes.insert(command_id, outcome);
        EventDisposition::Accepted
    }

    fn command_domain_is_stale(&self, event: &BleEvent) -> bool {
        match event {
            BleEvent::CharacteristicRead {
                connection_id,
                gatt_session_id,
                ..
            }
            | BleEvent::CharacteristicWritten {
                connection_id,
                gatt_session_id,
                ..
            }
            | BleEvent::NotificationStateChanged {
                connection_id,
                gatt_session_id,
                ..
            } => !self
                .active_gatt_sessions
                .contains(&(*connection_id, *gatt_session_id)),
            BleEvent::RssiRead { connection_id, .. } => {
                !self.active_connections.contains(connection_id)
            }
            _ => false,
        }
    }

    fn apply_domain_state(&mut self, event: &BleEvent) {
        match event {
            BleEvent::ScanStarted { scan_id, .. } => self.activate_scan(*scan_id),
            BleEvent::ScanStopped { scan_id, .. } => self.invalidate_scan(*scan_id),
            BleEvent::ConnectionStateChanged {
                connection_id,
                result:
                    ConnectionStateResult::Ok {
                        state: ConnectionState::Connected,
                    },
                ..
            } => self.activate_connection(*connection_id),
            BleEvent::ConnectionStateChanged {
                connection_id,
                result:
                    ConnectionStateResult::Ok {
                        state: ConnectionState::Disconnected,
                    },
                ..
            } => self.invalidate_connection(*connection_id),
            BleEvent::GattDiscovered { database, .. } => {
                self.activate_gatt_session(database.connection_id, database.gatt_session_id);
            }
            BleEvent::NotificationStateChanged {
                connection_id,
                gatt_session_id,
                subscription_id,
                characteristic_id,
                state: NotificationState::Enabled { .. },
                ..
            } => self.activate_subscription(
                *connection_id,
                *gatt_session_id,
                *subscription_id,
                *characteristic_id,
            ),
            BleEvent::NotificationStateChanged {
                connection_id,
                gatt_session_id,
                subscription_id,
                characteristic_id,
                state: NotificationState::Disabled,
                ..
            } => self.invalidate_subscription(
                *connection_id,
                *gatt_session_id,
                *subscription_id,
                *characteristic_id,
            ),
            _ => {}
        }
    }

    fn apply_passive_event(&mut self, event: &BleEvent) -> EventDisposition {
        match event {
            BleEvent::AdvertisementObserved { advertisement } => {
                if self.active_scans.contains(&advertisement.scan_session_id) {
                    EventDisposition::Accepted
                } else {
                    EventDisposition::Stale
                }
            }
            BleEvent::ScanStopped { scan_id, .. } => {
                if self.active_scans.remove(scan_id) {
                    EventDisposition::Accepted
                } else {
                    EventDisposition::Stale
                }
            }
            BleEvent::ConnectionStateChanged {
                command_id: None,
                connection_id,
                result,
            } => {
                if !self.active_connections.contains(connection_id) {
                    return EventDisposition::Stale;
                }
                if matches!(
                    result,
                    ConnectionStateResult::Ok {
                        state: ConnectionState::Disconnected
                    } | ConnectionStateResult::Err { .. }
                ) {
                    self.invalidate_connection(*connection_id);
                }
                EventDisposition::Accepted
            }
            BleEvent::NotificationReceived {
                connection_id,
                gatt_session_id,
                subscription_id,
                characteristic_id,
                ..
            } => {
                let scope = SubscriptionScope {
                    connection_id: *connection_id,
                    gatt_session_id: *gatt_session_id,
                    subscription_id: *subscription_id,
                    characteristic_id: *characteristic_id,
                };
                if self.active_subscriptions.contains(&scope) {
                    EventDisposition::Accepted
                } else {
                    EventDisposition::Stale
                }
            }
            BleEvent::AdapterSnapshotChanged {
                command_id: None, ..
            }
            | BleEvent::BackendError { .. } => EventDisposition::Accepted,
            _ => EventDisposition::Orphan,
        }
    }
}

#[cfg(test)]
mod tests {
    use blehub::{
        BleErrorCategory, GattCharacteristicId, GattSessionId, ReadCharacteristicCommand,
        SubscriptionId,
    };

    use super::*;
    use crate::advertisement_observation_fixture;

    fn completed(command_id: CommandId) -> BleEvent {
        BleEvent::CommandCompleted {
            command_id,
            error: None,
        }
    }

    fn notification(
        connection_id: ConnectionId,
        gatt_session_id: GattSessionId,
        subscription_id: SubscriptionId,
        characteristic_id: GattCharacteristicId,
    ) -> BleEvent {
        BleEvent::NotificationReceived {
            connection_id,
            gatt_session_id,
            subscription_id,
            characteristic_id,
            value: vec![1],
        }
    }

    #[test]
    fn terminal_is_accepted_once_then_classified_as_duplicate() {
        let command_id = CommandId { value: 1 };
        let mut projection = ReferenceProjection::default();
        assert!(projection.register_command(command_id));
        assert_eq!(
            projection.apply(&completed(command_id)),
            EventDisposition::Accepted
        );
        assert_eq!(
            projection.apply(&completed(command_id)),
            EventDisposition::Duplicate
        );
        assert_eq!(
            projection.outcome(command_id),
            Some(ProjectedCommandOutcome::Succeeded)
        );
    }

    #[test]
    fn unknown_terminal_is_orphan() {
        let mut projection = ReferenceProjection::default();
        assert_eq!(
            projection.apply(&completed(CommandId { value: 99 })),
            EventDisposition::Orphan
        );
    }

    #[test]
    fn terminal_after_caller_timeout_is_late_and_cannot_replace_local_outcome() {
        let command_id = CommandId { value: 1 };
        let mut projection = ReferenceProjection::default();
        assert!(projection.register_command(command_id));
        assert!(projection.timeout(command_id));
        assert!(!projection.is_pending(command_id));
        assert_eq!(
            projection.apply(&completed(command_id)),
            EventDisposition::Late
        );
        assert_eq!(
            projection.outcome(command_id),
            Some(ProjectedCommandOutcome::TimedOut)
        );
        assert!(!projection.timeout(command_id));
        assert!(!projection.register_command(command_id));
    }

    #[test]
    fn event_after_immediate_dispatch_rejection_is_a_contract_violation() {
        let command_id = CommandId { value: 1 };
        let error = CommandDispatchError::InvalidState {
            detail: "scripted rejection".to_owned(),
        };
        let mut projection = ReferenceProjection::default();
        assert!(projection.register_command(command_id));
        assert!(projection.dispatch_rejected(command_id, error.clone()));

        assert_eq!(
            projection.apply(&completed(command_id)),
            EventDisposition::ContractViolation
        );
        assert_eq!(
            projection.outcome(command_id),
            Some(ProjectedCommandOutcome::DispatchRejected(error))
        );
    }

    #[test]
    fn command_target_mismatch_does_not_update_the_projection() {
        let command = ReadCharacteristicCommand {
            command_id: CommandId { value: 1 },
            connection_id: ConnectionId { value: 2 },
            gatt_session_id: GattSessionId { value: 3 },
            characteristic_id: GattCharacteristicId { value: 4 },
        };
        let mut projection = ReferenceProjection::default();
        assert!(projection.register(RecordedCommand::ReadCharacteristic(command)));
        let mismatched = BleEvent::CharacteristicRead {
            command_id: command.command_id,
            connection_id: command.connection_id,
            gatt_session_id: GattSessionId { value: 99 },
            characteristic_id: command.characteristic_id,
            value: Vec::new(),
        };

        assert_eq!(
            projection.apply(&mismatched),
            EventDisposition::ContractViolation
        );
        assert!(projection.is_pending(command.command_id));
    }

    #[test]
    fn old_command_callback_is_stale_after_gatt_session_replacement() {
        let connection_id = ConnectionId { value: 1 };
        let old_gatt = GattSessionId { value: 2 };
        let new_gatt = GattSessionId { value: 3 };
        let characteristic_id = GattCharacteristicId { value: 4 };
        let old_command = ReadCharacteristicCommand {
            command_id: CommandId { value: 5 },
            connection_id,
            gatt_session_id: old_gatt,
            characteristic_id,
        };
        let new_command = ReadCharacteristicCommand {
            command_id: CommandId { value: 6 },
            gatt_session_id: new_gatt,
            ..old_command
        };
        let mut projection = ReferenceProjection::default();
        projection.activate_connection(connection_id);
        projection.activate_gatt_session(connection_id, old_gatt);
        assert!(projection.register(RecordedCommand::ReadCharacteristic(old_command)));
        projection.activate_gatt_session(connection_id, new_gatt);
        assert!(projection.register(RecordedCommand::ReadCharacteristic(new_command)));

        assert_eq!(
            projection.apply(&BleEvent::CharacteristicRead {
                command_id: old_command.command_id,
                connection_id,
                gatt_session_id: old_gatt,
                characteristic_id,
                value: vec![1],
            }),
            EventDisposition::Stale
        );
        assert!(projection.is_pending(old_command.command_id));
        assert_eq!(
            projection.apply(&BleEvent::CharacteristicRead {
                command_id: new_command.command_id,
                connection_id,
                gatt_session_id: new_gatt,
                characteristic_id,
                value: vec![2],
            }),
            EventDisposition::Accepted
        );
    }

    #[test]
    fn old_subscription_callback_is_stale_after_gatt_session_replacement() {
        let connection_id = ConnectionId { value: 1 };
        let old_gatt = GattSessionId { value: 2 };
        let new_gatt = GattSessionId { value: 3 };
        let old_subscription = SubscriptionId { value: 4 };
        let new_subscription = SubscriptionId { value: 5 };
        let characteristic_id = GattCharacteristicId { value: 6 };
        let mut projection = ReferenceProjection::default();
        projection.activate_connection(connection_id);
        projection.activate_gatt_session(connection_id, old_gatt);
        projection.activate_subscription(
            connection_id,
            old_gatt,
            old_subscription,
            characteristic_id,
        );
        projection.activate_gatt_session(connection_id, new_gatt);
        projection.activate_subscription(
            connection_id,
            new_gatt,
            new_subscription,
            characteristic_id,
        );

        assert_eq!(
            projection.apply(&notification(
                connection_id,
                old_gatt,
                old_subscription,
                characteristic_id,
            )),
            EventDisposition::Stale
        );
        assert_eq!(
            projection.apply(&notification(
                connection_id,
                new_gatt,
                new_subscription,
                characteristic_id,
            )),
            EventDisposition::Accepted
        );
    }

    #[test]
    fn old_scan_observation_is_stale_after_session_replacement() {
        let old_scan = ScanSessionId { value: 101 };
        let new_scan = ScanSessionId { value: 102 };
        let mut old_observation = advertisement_observation_fixture();
        old_observation.scan_session_id = old_scan;
        let mut new_observation = old_observation.clone();
        new_observation.scan_session_id = new_scan;
        let mut projection = ReferenceProjection::default();
        projection.activate_scan(old_scan);
        projection.invalidate_scan(old_scan);
        projection.activate_scan(new_scan);

        assert_eq!(
            projection.apply(&BleEvent::AdvertisementObserved {
                advertisement: old_observation,
            }),
            EventDisposition::Stale
        );
        assert_eq!(
            projection.apply(&BleEvent::AdvertisementObserved {
                advertisement: new_observation,
            }),
            EventDisposition::Accepted
        );
    }

    #[test]
    fn old_passive_disconnect_is_stale_after_connection_replacement() {
        let old_connection = ConnectionId { value: 1 };
        let new_connection = ConnectionId { value: 2 };
        let mut projection = ReferenceProjection::default();
        projection.activate_connection(old_connection);
        projection.invalidate_connection(old_connection);
        projection.activate_connection(new_connection);

        assert_eq!(
            projection.apply(&BleEvent::ConnectionStateChanged {
                command_id: None,
                connection_id: old_connection,
                result: ConnectionStateResult::Ok {
                    state: ConnectionState::Disconnected,
                },
            }),
            EventDisposition::Stale
        );
        assert_eq!(
            projection.apply(&BleEvent::ConnectionStateChanged {
                command_id: None,
                connection_id: new_connection,
                result: ConnectionStateResult::Ok {
                    state: ConnectionState::Disconnected,
                },
            }),
            EventDisposition::Accepted
        );
    }

    #[test]
    fn failed_terminal_preserves_the_structured_error() {
        let command_id = CommandId { value: 1 };
        let error = BleEventError {
            category: BleErrorCategory::Timeout,
            message: "platform timeout".to_owned(),
            platform_details: None,
        };
        let mut projection = ReferenceProjection::default();
        assert!(projection.register_command(command_id));

        assert_eq!(
            projection.apply(&BleEvent::CommandCompleted {
                command_id,
                error: Some(error.clone()),
            }),
            EventDisposition::Accepted
        );
        assert_eq!(
            projection.outcome(command_id),
            Some(ProjectedCommandOutcome::Failed(error))
        );
    }
}
