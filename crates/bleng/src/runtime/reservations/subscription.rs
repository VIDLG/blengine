use blehub::{
    BleEvent, CommandId, ConnectionId, GattCharacteristicId, GattSessionId, NotificationMode,
    NotificationModeRequest, NotificationState, SubscriptionId,
};

use crate::waiter::{WaiterError, WaiterSuccess};

use super::{
    connection::connection_lost,
    dispatch::{CommandProgress, ReconciliationFault},
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

    /// Queries a deliverable transition result without changing the route.
    /// None means no matching transition or incomplete evidence, not failure.
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
        if let Err(error) = transition.progress.terminal_result()? {
            return Some(Err(error));
        }
        let success = match transition.domain? {
            NotificationState::Enabled { actual_mode } => WaiterSuccess::NotificationsEnabled {
                connection_id,
                gatt_session_id: self.session_id,
                subscription_id,
                characteristic_id: self.characteristic_id,
                actual_mode,
            },
            NotificationState::Disabled => WaiterSuccess::NotificationsDisabled(
                connection_id,
                self.session_id,
                subscription_id,
                self.characteristic_id,
            ),
        };
        Some(Ok(success))
    }

    /// Only an unresolved transition can buffer; an active route has no target.
    pub(super) fn should_buffer_event(
        &self,
        event: &BleEvent,
        expected: ConnectionId,
        subscription_id: SubscriptionId,
    ) -> bool {
        let Some(progress) = self.progress() else {
            return false;
        };
        progress.is_awaiting_dispatch_result()
            && self.matches_related_event(event, expected, subscription_id)
    }

    /// Correlates transition state, terminal, or connection loss, not enable success.
    /// record_event() separately checks the domain variant and required mode.
    fn matches_related_event(
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
    ) -> Result<(), ReconciliationFault> {
        if !self.matches_related_event(event, connection_id, subscription_id) {
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
                    return Err(ReconciliationFault::UnexpectedNotificationMode);
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

    /// Call after waiter settlement, even when the caller already retired.
    /// Failed disable preserves the active route; failed enable removes the
    /// provisional route. Successful transitions also require domain evidence.
    /// Keep can mean incomplete, active, or restored; it does not imply success.
    /// The connection owner must apply Remove to its route collection.
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

    /// Restores Active for a failed disable, or requests removal of a failed enable.
    /// Dispatch rejection drains buffered events before calling this; accepted
    /// terminal failure reaches it after waiter settlement through retire_transition().
    /// The connection owner must remove the route when Remove is returned.
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
