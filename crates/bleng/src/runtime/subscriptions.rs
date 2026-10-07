//! Notification subscription admission and the crate-private async facade.

use std::time::Duration;

use blehub::{
    CommandId, ConnectionId, GattCharacteristicId, GattSessionId, NotificationAction,
    NotificationMode, NotificationModeRequest, SetNotificationsCommand, SubscriptionId,
};
use kameo::prelude::{Context, Message};

use crate::waiter::{WaiterError, WaiterExpectation, WaiterSuccess};

use super::{BeginHandoff, BlengActor, RuntimeOwner, WaiterReceiver};

struct BeginEnableNotifications {
    handoff: BeginHandoff,
    connection: ConnectionId,
    session: GattSessionId,
    characteristic: GattCharacteristicId,
    mode: NotificationModeRequest,
}

struct BeginDisableNotifications {
    handoff: BeginHandoff,
    connection: ConnectionId,
    session: GattSessionId,
    subscription: SubscriptionId,
}

impl Message<BeginEnableNotifications> for BlengActor {
    type Reply = Result<(CommandId, SubscriptionId, WaiterReceiver), WaiterError>;

    async fn handle(
        &mut self,
        message: BeginEnableNotifications,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        self.require_open()?;
        self.require_initialized()?;
        let key = self.validate_notification_enable(
            message.connection,
            message.session,
            message.characteristic,
        )?;
        let subscription: SubscriptionId = self.allocate_resource_id()?.into();
        let (id, result_rx) = self.register_waiter(WaiterExpectation::NotificationsEnabled(
            message.connection,
            message.session,
            subscription,
            message.characteristic,
        ))?;
        self.launch_dispatch(
            ctx,
            &message.handoff,
            id,
            |actor| {
                actor.reserve_notification_enable(
                    key,
                    id,
                    subscription,
                    message.session,
                    message.characteristic,
                    message.mode,
                );
            },
            move |hub| {
                hub.set_notifications(SetNotificationsCommand {
                    command_id: id,
                    connection_id: message.connection,
                    gatt_session_id: message.session,
                    subscription_id: subscription,
                    characteristic_id: message.characteristic,
                    action: NotificationAction::Enable { mode: message.mode },
                })
            },
        )?;
        Ok((id, subscription, result_rx))
    }
}

impl Message<BeginDisableNotifications> for BlengActor {
    type Reply = Result<(CommandId, WaiterReceiver), WaiterError>;

    async fn handle(
        &mut self,
        message: BeginDisableNotifications,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        self.require_open()?;
        self.require_initialized()?;
        let (key, characteristic) = self.validate_notification_disable(
            message.connection,
            message.session,
            message.subscription,
        )?;
        let (id, result_rx) = self.register_waiter(WaiterExpectation::NotificationsDisabled(
            message.connection,
            message.session,
            message.subscription,
            characteristic,
        ))?;
        self.launch_dispatch(
            ctx,
            &message.handoff,
            id,
            |actor| {
                actor.reserve_notification_disable(key, id, message.subscription);
            },
            move |hub| {
                hub.set_notifications(SetNotificationsCommand {
                    command_id: id,
                    connection_id: message.connection,
                    gatt_session_id: message.session,
                    subscription_id: message.subscription,
                    characteristic_id: characteristic,
                    action: NotificationAction::Disable,
                })
            },
        )?;
        Ok((id, result_rx))
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
        let ((id, subscription, result_rx), deadline) = self
            .begin_operation(deadline, move |actor, handoff| async move {
                actor
                    .ask(BeginEnableNotifications {
                        handoff,
                        connection,
                        session,
                        characteristic,
                        mode,
                    })
                    .await
            })
            .await?;
        self.wait_for_success(id, result_rx, deadline, |success| match success {
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
        let ((id, result_rx), deadline) = self
            .begin_operation(deadline, move |actor, handoff| async move {
                actor
                    .ask(BeginDisableNotifications {
                        handoff,
                        connection,
                        session,
                        subscription,
                    })
                    .await
            })
            .await?;
        self.wait_for_success(id, result_rx, deadline, |success| match success {
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
