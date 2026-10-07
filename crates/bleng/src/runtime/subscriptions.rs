//! Notification subscription admission and the crate-private async facade.

use std::time::Duration;

use super::commands::validate_gatt_io;
use super::*;
use crate::waiter::WaiterExpectation;

struct BeginEnableNotifications {
    context: BeginContext,
    connection: ConnectionId,
    session: GattSessionId,
    characteristic: GattCharacteristicId,
    mode: NotificationModeRequest,
}

struct BeginDisableNotifications {
    context: BeginContext,
    connection: ConnectionId,
    session: GattSessionId,
    subscription: SubscriptionId,
}

impl Message<BeginEnableNotifications> for BlengActor {
    type Reply = Result<
        (
            CommandId,
            SubscriptionId,
            oneshot::Receiver<Result<WaiterSuccess, WaiterError>>,
        ),
        WaiterError,
    >;

    async fn handle(
        &mut self,
        message: BeginEnableNotifications,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        self.require_open()?;
        self.require_initialized()?;
        let key = validate_gatt_io(
            &self.connections,
            message.connection,
            message.session,
            message.characteristic,
            false,
        )?;
        if self.connections[&key]
            .subscriptions
            .values()
            .any(|route| route.characteristic_id == message.characteristic)
        {
            return Err(WaiterError::OperationBusy);
        }
        let (id, subscription, receiver) =
            self.register_waiter_with_following_id(|_, subscription| {
                WaiterExpectation::NotificationsEnabled(
                    message.connection,
                    message.session,
                    subscription,
                    message.characteristic,
                )
            })?;
        self.reserve_notification_enable(
            key,
            id,
            subscription,
            message.session,
            message.characteristic,
            message.mode,
        )
        .launch_dispatch(ctx, &message.context, move |hub| {
            hub.set_notifications(SetNotificationsCommand {
                command_id: id,
                connection_id: message.connection,
                gatt_session_id: message.session,
                subscription_id: subscription,
                characteristic_id: message.characteristic,
                action: NotificationAction::Enable { mode: message.mode },
            })
        })?;
        Ok((id, subscription, receiver))
    }
}

impl Message<BeginDisableNotifications> for BlengActor {
    type Reply = Result<
        (
            CommandId,
            oneshot::Receiver<Result<WaiterSuccess, WaiterError>>,
        ),
        WaiterError,
    >;

    async fn handle(
        &mut self,
        message: BeginDisableNotifications,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        self.require_open()?;
        self.require_initialized()?;
        let (key, route) = self
            .connections
            .iter()
            .find_map(|(key, connection)| {
                (connection.connection_id == message.connection)
                    .then(|| (*key, connection.subscriptions.get(&message.subscription)))
            })
            .ok_or(WaiterError::ConnectionNotActive)?;
        let route = route
            .filter(|route| route.session_id == message.session && route.active)
            .ok_or(WaiterError::SubscriptionNotActive)?;
        if route.transition.is_some() {
            return Err(WaiterError::OperationBusy);
        }
        let characteristic = route.characteristic_id;
        let connection = &self.connections[&key];
        if !connection.connected || connection.disconnected || connection.failed {
            return Err(WaiterError::ConnectionNotActive);
        }
        if connection.disconnect.is_some()
            || connection
                .rssi
                .as_ref()
                .is_some_and(|slot| slot.dispatch.is_pending())
            || connection
                .gatt
                .as_ref()
                .is_some_and(GattReservation::has_pending_discovery)
            || connection.read.is_some()
            || connection.write.is_some()
            || connection.subscriptions.values().any(|route| {
                route
                    .transition
                    .as_ref()
                    .is_some_and(|slot| slot.dispatch.is_pending())
            })
        {
            return Err(WaiterError::OperationBusy);
        }
        let (id, receiver) = self.register_waiter(WaiterExpectation::NotificationsDisabled(
            message.connection,
            message.session,
            message.subscription,
            characteristic,
        ))?;
        self.reserve_notification_disable(key, id, message.subscription)
            .launch_dispatch(ctx, &message.context, move |hub| {
                hub.set_notifications(SetNotificationsCommand {
                    command_id: id,
                    connection_id: message.connection,
                    gatt_session_id: message.session,
                    subscription_id: message.subscription,
                    characteristic_id: characteristic,
                    action: NotificationAction::Disable,
                })
            })?;
        Ok((id, receiver))
    }
}

impl BlengActor {
    pub(super) fn observe_subscription(&mut self, event: &BleEvent) {
        if let BleEvent::NotificationStateChanged {
            command_id,
            connection_id,
            gatt_session_id,
            subscription_id,
            characteristic_id,
            state,
        } = event
        {
            let Some(route) = self.connections.values_mut().find_map(|connection| {
                (connection.connection_id == *connection_id)
                    .then(|| connection.subscriptions.get_mut(subscription_id))
                    .flatten()
            }) else {
                return;
            };
            if route.session_id != *gatt_session_id || route.characteristic_id != *characteristic_id
            {
                return;
            }
            let Some(transition) = route
                .transition
                .as_mut()
                .filter(|transition| transition.dispatch.command_id == *command_id)
            else {
                return;
            };
            let expected = matches!(
                (transition.action, state),
                (
                    NotificationAction::Enable { .. },
                    NotificationState::Enabled { .. }
                ) | (NotificationAction::Disable, NotificationState::Disabled)
            );
            if !expected {
                return;
            }
            if let (
                NotificationAction::Enable { mode },
                NotificationState::Enabled { actual_mode },
            ) = (transition.action, state)
            {
                let valid = match mode {
                    NotificationModeRequest::PlatformDefault => true,
                    NotificationModeRequest::RequireNotification => {
                        *actual_mode == Some(NotificationMode::Notification)
                    }
                    NotificationModeRequest::RequireIndication => {
                        *actual_mode == Some(NotificationMode::Indication)
                    }
                };
                if !valid {
                    self.status.fault();
                    return;
                }
            }
            if transition.domain.is_none() {
                transition.domain = Some(*state);
            }
        }
    }

    pub(super) fn retire_subscriptions(&mut self) {
        for connection in self.connections.values_mut() {
            connection.subscriptions.retain(|_, route| {
                let Some(transition) = route.transition.as_ref() else {
                    return true;
                };
                if transition.dispatch.is_pending() {
                    return true;
                }
                if transition.terminal.as_ref().is_some_and(Result::is_err) {
                    if route.active {
                        route.transition = None;
                        return true;
                    }
                    return false;
                }
                if transition.terminal != Some(Ok(())) {
                    return true;
                }
                match (transition.action, transition.domain) {
                    (
                        NotificationAction::Enable { .. },
                        Some(NotificationState::Enabled { actual_mode }),
                    ) => {
                        route.active = true;
                        route.actual_mode = actual_mode;
                        route.transition = None;
                        true
                    }
                    (NotificationAction::Disable, Some(NotificationState::Disabled)) => false,
                    _ => true,
                }
            });
        }
    }
}

impl RuntimeOwner {
    pub(crate) async fn enable_notifications(
        &self,
        connection: ConnectionId,
        session: GattSessionId,
        characteristic: GattCharacteristicId,
        mode: NotificationModeRequest,
        deadline: Duration,
    ) -> Result<(SubscriptionId, Option<NotificationMode>), WaiterError> {
        let ((id, subscription, receiver), deadline) = self
            .begin_operation(deadline, move |actor, context| async move {
                actor
                    .ask(BeginEnableNotifications {
                        context,
                        connection,
                        session,
                        characteristic,
                        mode,
                    })
                    .await
            })
            .await?;
        self.wait_for_success(id, receiver, deadline, |success| match success {
            WaiterSuccess::NotificationsEnabled {
                connection_id,
                gatt_session_id,
                subscription_id,
                characteristic_id,
                actual_mode,
            } if connection_id == connection
                && gatt_session_id == session
                && subscription_id == subscription
                && characteristic_id == characteristic =>
            {
                Some((subscription, actual_mode))
            }
            _ => None,
        })
        .await
    }

    pub(crate) async fn disable_notifications(
        &self,
        connection: ConnectionId,
        session: GattSessionId,
        subscription: SubscriptionId,
        deadline: Duration,
    ) -> Result<(), WaiterError> {
        let ((id, receiver), deadline) = self
            .begin_operation(deadline, move |actor, context| async move {
                actor
                    .ask(BeginDisableNotifications {
                        context,
                        connection,
                        session,
                        subscription,
                    })
                    .await
            })
            .await?;
        self.wait_for_success(id, receiver, deadline, |success| match success {
            WaiterSuccess::NotificationsDisabled(
                found_connection,
                found_session,
                found_subscription,
                _,
            ) if found_connection == connection
                && found_session == session
                && found_subscription == subscription =>
            {
                Some(())
            }
            _ => None,
        })
        .await
    }
}
