use super::*;

pub(crate) struct BeginEnableNotifications {
    pub(crate) submission: OperationSubmission<SubscriptionId>,
    pub(crate) connection: ConnectionId,
    pub(crate) session: GattSessionId,
    pub(crate) characteristic: GattCharacteristicId,
    pub(crate) mode: NotificationModeRequest,
}

pub(crate) struct BeginDisableNotifications {
    pub(crate) submission: OperationSubmission,
    pub(crate) connection: ConnectionId,
    pub(crate) session: GattSessionId,
    pub(crate) subscription: SubscriptionId,
}

impl OperationSpec for BeginEnableNotifications {
    type Resource = SubscriptionId;
    type Output = (SubscriptionId, Option<NotificationMode>);
    type Correlation = (ConnectionId, GattSessionId, GattCharacteristicId);

    fn correlation(&self) -> Self::Correlation {
        (self.connection, self.session, self.characteristic)
    }

    fn decode_success(
        (connection, session, characteristic): Self::Correlation,
        resource: Self::Resource,
        success: WaiterSuccess,
    ) -> Option<Self::Output> {
        match success {
            WaiterSuccess::NotificationsEnabled {
                connection_id,
                gatt_session_id,
                subscription_id,
                characteristic_id,
                actual_mode,
            } if connection_id == connection
                && gatt_session_id == session
                && subscription_id == resource
                && characteristic_id == characteristic =>
            {
                Some((resource, actual_mode))
            }
            _ => None,
        }
    }
}

impl OperationSpec for BeginDisableNotifications {
    type Resource = ();
    type Output = ();
    type Correlation = (ConnectionId, GattSessionId, SubscriptionId);

    fn correlation(&self) -> Self::Correlation {
        (self.connection, self.session, self.subscription)
    }

    fn decode_success(
        (connection, session, subscription): Self::Correlation,
        (): Self::Resource,
        success: WaiterSuccess,
    ) -> Option<Self::Output> {
        match success {
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
        }
    }
}

impl Message<BeginEnableNotifications> for BlengActor {
    type Reply = ();

    async fn handle(
        &mut self,
        message: BeginEnableNotifications,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        let submission = message.submission.clone();
        let result = (|| {
            self.require_open()?;
            self.require_initialized()?;
            let peripheral_id = self.validate_notification_enable(
                message.connection,
                message.session,
                message.characteristic,
            )?;
            let subscription: SubscriptionId = self.allocate_resource_id()?.into();
            let command_id = self.register_operation_waiter(
                &message.submission,
                WaiterExpectation::NotificationsEnabled(
                    message.connection,
                    message.session,
                    subscription,
                    message.characteristic,
                ),
            )?;
            self.launch_dispatch(
                ctx,
                &message.submission,
                command_id,
                |reservations| {
                    reservations.reserve_notification_enable(
                        peripheral_id,
                        command_id,
                        subscription,
                        message.session,
                        message.characteristic,
                        message.mode,
                    );
                },
                move |hub| {
                    hub.set_notifications(SetNotificationsCommand {
                        command_id,
                        connection_id: message.connection,
                        gatt_session_id: message.session,
                        subscription_id: subscription,
                        characteristic_id: message.characteristic,
                        action: NotificationAction::Enable { mode: message.mode },
                    })
                },
            )?;
            Ok(subscription)
        })();
        submission.resolve(result);
    }
}

impl Message<BeginDisableNotifications> for BlengActor {
    type Reply = ();

    async fn handle(
        &mut self,
        message: BeginDisableNotifications,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        let submission = message.submission.clone();
        let result = (|| {
            self.require_open()?;
            self.require_initialized()?;
            let (peripheral_id, characteristic) = self.validate_notification_disable(
                message.connection,
                message.session,
                message.subscription,
            )?;
            let command_id = self.register_operation_waiter(
                &message.submission,
                WaiterExpectation::NotificationsDisabled(
                    message.connection,
                    message.session,
                    message.subscription,
                    characteristic,
                ),
            )?;
            self.launch_dispatch(
                ctx,
                &message.submission,
                command_id,
                |reservations| {
                    reservations.reserve_notification_disable(
                        peripheral_id,
                        command_id,
                        message.subscription,
                    );
                },
                move |hub| {
                    hub.set_notifications(SetNotificationsCommand {
                        command_id,
                        connection_id: message.connection,
                        gatt_session_id: message.session,
                        subscription_id: message.subscription,
                        characteristic_id: characteristic,
                        action: NotificationAction::Disable,
                    })
                },
            )?;
            Ok(())
        })();
        submission.resolve(result);
    }
}
