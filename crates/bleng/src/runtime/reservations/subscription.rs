use blehub::{
    BleEvent, BleEventError, CommandId, ConnectionId, GattCharacteristicId, GattSessionId,
    NotificationAction, NotificationMode, NotificationModeRequest, NotificationState,
    SubscriptionId,
};

use crate::waiter::{WaiterError, WaiterSuccess};

use super::{
    connection::connection_lost,
    dispatch::{DispatchReservation, ReconcileFault},
};

/// A subscription is active only after the native CCCD transition and domain
/// event have been reconciled; `transition` represents that pending boundary.
pub(super) struct SubscriptionReservation {
    pub(super) session_id: GattSessionId,
    pub(super) characteristic_id: GattCharacteristicId,
    pub(super) active: bool,
    pub(super) transition: Option<NotificationTransition>,
}

impl SubscriptionReservation {
    pub(super) fn matches_event(
        &self,
        event: &BleEvent,
        expected: ConnectionId,
        subscription_id: SubscriptionId,
    ) -> bool {
        let Some(transition) = self.transition.as_ref() else {
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
                transition.dispatch.command_id == *command_id
                    && expected == *connection_id
                    && self.session_id == *gatt_session_id
                    && subscription_id == *event_subscription
                    && self.characteristic_id == *characteristic_id
            }
            BleEvent::CommandCompleted { command_id, .. } => {
                transition.dispatch.command_id == *command_id
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
        let transition = self
            .transition
            .as_mut()
            .expect("matching transition exists");
        match (transition.action, state) {
            (NotificationAction::Enable { mode }, NotificationState::Enabled { actual_mode }) => {
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
            }
            (NotificationAction::Disable, NotificationState::Disabled) => {}
            _ => return Ok(()),
        }
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
            active: false,
            transition: Some(NotificationTransition::new(
                command_id,
                NotificationAction::Enable { mode },
            )),
        }
    }

    pub(super) fn begin_disable(&mut self, command_id: CommandId) {
        self.transition = Some(NotificationTransition::new(
            command_id,
            NotificationAction::Disable,
        ));
    }

    /// Returns false only when this route is safe to remove. Failed disable
    /// preserves the active route; failed enable removes the provisional route.
    pub(super) fn retire_transition(&mut self) -> bool {
        let Some(transition) = self.transition.as_ref() else {
            return true;
        };
        if transition.dispatch.is_awaiting_dispatch_ack() {
            return true;
        }
        if transition.terminal.as_ref().is_some_and(Result::is_err) {
            if self.active {
                self.transition = None;
                return true;
            }
            return false;
        }
        if transition.terminal != Some(Ok(())) {
            return true;
        }
        match (transition.action, transition.domain) {
            (NotificationAction::Enable { .. }, Some(NotificationState::Enabled { .. })) => {
                self.active = true;
                self.transition = None;
                true
            }
            (NotificationAction::Disable, Some(NotificationState::Disabled)) => false,
            _ => true,
        }
    }
}

pub(super) struct NotificationTransition {
    pub(super) dispatch: DispatchReservation,
    action: NotificationAction,
    domain: Option<NotificationState>,
    pub(super) terminal: Option<Result<(), BleEventError>>,
}

impl NotificationTransition {
    fn new(command_id: CommandId, action: NotificationAction) -> Self {
        Self {
            dispatch: DispatchReservation::new(command_id),
            action,
            domain: None,
            terminal: None,
        }
    }

    pub(super) fn waiter_result(
        &self,
        connection_id: ConnectionId,
        subscription_id: SubscriptionId,
        session_id: GattSessionId,
        characteristic_id: GattCharacteristicId,
    ) -> Option<Result<WaiterSuccess, WaiterError>> {
        self.dispatch
            .waiter_result(&self.terminal, || match self.domain {
                Some(NotificationState::Enabled { actual_mode }) => {
                    Some(WaiterSuccess::NotificationsEnabled {
                        connection_id,
                        gatt_session_id: session_id,
                        subscription_id,
                        characteristic_id,
                        actual_mode,
                    })
                }
                Some(NotificationState::Disabled) => Some(WaiterSuccess::NotificationsDisabled(
                    connection_id,
                    session_id,
                    subscription_id,
                    characteristic_id,
                )),
                None => None,
            })
    }
}
