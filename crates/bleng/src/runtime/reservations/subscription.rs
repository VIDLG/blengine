use blehub::{
    BleEvent, CommandId, ConnectionId, GattCharacteristicId, GattSessionId, NotificationMode,
    NotificationModeRequest, NotificationState, SubscriptionId,
};

use crate::waiter::{WaiterError, WaiterSuccess};

use super::{
    connection::connection_lost,
    dispatch::{CommandProgress, ReconcileFault},
};

/// A subscription becomes active only after dispatch acknowledgement, a successful
/// terminal, and matching domain evidence. Disabling preserves its active route.
pub(super) struct SubscriptionReservation {
    pub(super) session_id: GattSessionId,
    pub(super) characteristic_id: GattCharacteristicId,
    state: SubscriptionState,
}

enum SubscriptionState {
    Enabling {
        mode: NotificationModeRequest,
        transition: NotificationTransition,
    },
    Active,
    Disabling(NotificationTransition),
}

pub(super) enum SubscriptionDisposition {
    Keep,
    Remove,
}

impl SubscriptionReservation {
    pub(super) fn is_active(&self) -> bool {
        matches!(
            self.state,
            SubscriptionState::Active | SubscriptionState::Disabling(_)
        )
    }

    pub(super) fn is_transitioning(&self) -> bool {
        self.transition().is_some()
    }

    fn transition(&self) -> Option<&NotificationTransition> {
        match &self.state {
            SubscriptionState::Enabling { transition, .. }
            | SubscriptionState::Disabling(transition) => Some(transition),
            SubscriptionState::Active => None,
        }
    }

    pub(super) fn progress(&self) -> Option<&CommandProgress> {
        self.transition().map(|transition| &transition.progress)
    }

    pub(super) fn progress_mut(&mut self) -> Option<&mut CommandProgress> {
        match &mut self.state {
            SubscriptionState::Enabling { transition, .. }
            | SubscriptionState::Disabling(transition) => Some(&mut transition.progress),
            SubscriptionState::Active => None,
        }
    }

    pub(super) fn waiter_result(
        &self,
        command_id: CommandId,
        connection_id: ConnectionId,
        subscription_id: SubscriptionId,
    ) -> Option<Result<WaiterSuccess, WaiterError>> {
        let transition = self.transition()?;
        if transition.progress.command_id != command_id {
            return None;
        }
        transition
            .progress
            .waiter_result(|| match transition.domain {
                Some(NotificationState::Enabled { actual_mode }) => {
                    Some(WaiterSuccess::NotificationsEnabled {
                        connection_id,
                        gatt_session_id: self.session_id,
                        subscription_id,
                        characteristic_id: self.characteristic_id,
                        actual_mode,
                    })
                }
                Some(NotificationState::Disabled) => Some(WaiterSuccess::NotificationsDisabled(
                    connection_id,
                    self.session_id,
                    subscription_id,
                    self.characteristic_id,
                )),
                None => None,
            })
    }

    pub(super) fn matches_event(
        &self,
        event: &BleEvent,
        expected: ConnectionId,
        subscription_id: SubscriptionId,
    ) -> bool {
        let Some(transition) = self.transition() else {
            return false;
        };
        match event {
            BleEvent::NotificationStateChanged {
                command_id,
                connection_id,
                gatt_session_id,
                subscription_id: event_subscription,
                characteristic_id,
                ..
            } => {
                transition.progress.command_id == *command_id
                    && expected == *connection_id
                    && self.session_id == *gatt_session_id
                    && subscription_id == *event_subscription
                    && self.characteristic_id == *characteristic_id
            }
            BleEvent::CommandCompleted { command_id, .. } => {
                transition.progress.command_id == *command_id
            }
            _ => connection_lost(event, expected),
        }
    }

    pub(super) fn record_event(
        &mut self,
        event: &BleEvent,
        connection_id: ConnectionId,
        subscription_id: SubscriptionId,
    ) -> Result<(), ReconcileFault> {
        if !self.matches_event(event, connection_id, subscription_id) {
            return Ok(());
        }
        let BleEvent::NotificationStateChanged { state, .. } = event else {
            return Ok(());
        };
        let transition = match (&mut self.state, state) {
            (
                SubscriptionState::Enabling { mode, transition },
                NotificationState::Enabled { actual_mode },
            ) => {
                let valid = match mode {
                    NotificationModeRequest::PlatformDefault => true,
                    NotificationModeRequest::RequireNotification => {
                        *actual_mode == Some(NotificationMode::Notification)
                    }
                    NotificationModeRequest::RequireIndication => {
                        *actual_mode == Some(NotificationMode::Indication)
                    }
                };
                // Validate even a duplicate; first evidence does not excuse a
                // later correlated callback that contradicts the required mode.
                if !valid {
                    return Err(ReconcileFault::UnexpectedNotificationMode);
                }
                transition
            }
            (SubscriptionState::Disabling(transition), NotificationState::Disabled) => transition,
            _ => return Ok(()),
        };
        if transition.domain.is_none() {
            transition.domain = Some(*state);
        }
        Ok(())
    }

    /// Creates the provisional route and its enable transition together.
    pub(super) fn enabling(
        command_id: CommandId,
        session_id: GattSessionId,
        characteristic_id: GattCharacteristicId,
        mode: NotificationModeRequest,
    ) -> Self {
        Self {
            session_id,
            characteristic_id,
            state: SubscriptionState::Enabling {
                mode,
                transition: NotificationTransition::new(command_id),
            },
        }
    }

    pub(super) fn begin_disable(&mut self, command_id: CommandId) {
        self.state = SubscriptionState::Disabling(NotificationTransition::new(command_id));
    }

    /// Failed disable preserves the active route; failed enable removes the
    /// provisional route. Successful transitions also require domain evidence.
    pub(super) fn retire_transition(&mut self) -> SubscriptionDisposition {
        let Some(transition) = self.transition() else {
            return SubscriptionDisposition::Keep;
        };
        if !transition.progress.is_complete(transition.domain.is_some()) {
            return SubscriptionDisposition::Keep;
        }
        if transition.progress.terminal_failed() {
            return self.reject_transition();
        }
        match self.state {
            SubscriptionState::Enabling { .. } => {
                self.state = SubscriptionState::Active;
                SubscriptionDisposition::Keep
            }
            SubscriptionState::Disabling(_) => SubscriptionDisposition::Remove,
            SubscriptionState::Active => SubscriptionDisposition::Keep,
        }
    }

    /// The caller takes buffered events before rejection and removes the route
    /// immediately when requested; only a rejected disable has an active fallback.
    pub(super) fn reject_transition(&mut self) -> SubscriptionDisposition {
        match self.state {
            SubscriptionState::Enabling { .. } => SubscriptionDisposition::Remove,
            SubscriptionState::Disabling(_) => {
                self.state = SubscriptionState::Active;
                SubscriptionDisposition::Keep
            }
            SubscriptionState::Active => SubscriptionDisposition::Keep,
        }
    }
}

struct NotificationTransition {
    progress: CommandProgress,
    domain: Option<NotificationState>,
}

impl NotificationTransition {
    fn new(command_id: CommandId) -> Self {
        Self {
            progress: CommandProgress::new(command_id),
            domain: None,
        }
    }
}

#[cfg(test)]
#[path = "tests/subscription.rs"]
mod tests;
