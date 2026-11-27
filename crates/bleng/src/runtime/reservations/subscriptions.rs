use std::collections::HashMap;

use blehub::{
    BleEvent, CommandDispatchError, CommandId, ConnectionId, GattCharacteristicId, GattSessionId,
    NotificationModeRequest, SubscriptionId,
};

use crate::waiter::{WaiterError, WaiterOutcome};

use super::{
    CommandProgress, DispatchResolution, GattAccess, ReconciliationFault,
    subscription::{SubscriptionDisposition, SubscriptionReservation},
};

/// Owns one connection's subscription routes; each route owns its transition policy.
/// A command transition can retire while its route remains. Only transitions count
/// toward the connection's shared pending-I/O budget, not stable active routes.
#[derive(Default)]
pub(super) struct SubscriptionReservations {
    subscriptions: HashMap<SubscriptionId, SubscriptionReservation>,
}

impl SubscriptionReservations {
    delegate::delegate! {
        to self.subscriptions {
            pub(super) fn is_empty(&self) -> bool;
        }
    }

    pub(super) fn transition_count(&self) -> usize {
        self.subscriptions
            .values()
            .filter(|route| route.is_transitioning())
            .count()
    }

    /// Checks subscription-local conflicts before the parent's discovery check.
    /// A stable route blocks reads, but duplicate enable is checked separately
    /// after discovery so this method must not reject it early.
    pub(super) fn validate_characteristic_access(
        &self,
        characteristic_id: GattCharacteristicId,
        access: GattAccess,
    ) -> Result<(), WaiterError> {
        if self.subscriptions.values().any(|route| {
            route.characteristic_id == characteristic_id
                && (route.is_transitioning() || matches!(access, GattAccess::Read))
        }) {
            return Err(WaiterError::OperationBusy);
        }
        Ok(())
    }

    pub(super) fn has_subscription_for(&self, characteristic_id: GattCharacteristicId) -> bool {
        self.subscriptions
            .values()
            .any(|route| route.characteristic_id == characteristic_id)
    }

    /// Only subscription-local checks; the parent preserves connection-check precedence.
    pub(super) fn validate_disable(
        &self,
        session_id: GattSessionId,
        subscription_id: SubscriptionId,
    ) -> Result<GattCharacteristicId, WaiterError> {
        self.subscriptions
            .get(&subscription_id)
            .ok_or(WaiterError::SubscriptionNotActive)?
            .validate_disable(session_id)
    }

    pub(super) fn reserve_enable(
        &mut self,
        command_id: CommandId,
        subscription_id: SubscriptionId,
        session_id: GattSessionId,
        characteristic_id: GattCharacteristicId,
        mode: NotificationModeRequest,
    ) {
        self.subscriptions.insert(
            subscription_id,
            SubscriptionReservation::enabling(command_id, session_id, characteristic_id, mode),
        );
    }

    pub(super) fn reserve_disable(
        &mut self,
        command_id: CommandId,
        subscription_id: SubscriptionId,
    ) {
        self.subscriptions
            .get_mut(&subscription_id)
            .expect("validated subscription")
            .begin_disable(command_id);
    }

    pub(super) fn progress_for_cmd(&self, command_id: CommandId) -> Option<&CommandProgress> {
        self.progresses()
            .find(|progress| progress.owns_command(command_id))
    }

    pub(super) fn progress_for_cmd_mut(
        &mut self,
        command_id: CommandId,
    ) -> Option<&mut CommandProgress> {
        self.subscriptions
            .values_mut()
            .filter_map(SubscriptionReservation::progress_mut)
            .find(|progress| progress.owns_command(command_id))
    }

    pub(super) fn progresses(&self) -> impl Iterator<Item = &CommandProgress> {
        self.subscriptions
            .values()
            .filter_map(SubscriptionReservation::progress)
    }

    pub(super) fn pending_progress_for_event_mut(
        &mut self,
        event: &BleEvent,
        connection_id: ConnectionId,
    ) -> Option<&mut CommandProgress> {
        match event {
            BleEvent::NotificationStateChanged {
                subscription_id, ..
            }
            | BleEvent::NotificationReceived {
                subscription_id, ..
            } => self
                .subscriptions
                .get_mut(subscription_id)
                .filter(|route| route.should_buffer_event(event, connection_id, *subscription_id))
                .and_then(SubscriptionReservation::progress_mut),
            // Loss belongs to the connection scope, even when it names another command.
            // Keep the related-event search for connection observations and terminals.
            _ => {
                for (subscription_id, route) in &mut self.subscriptions {
                    if route.should_buffer_event(event, connection_id, *subscription_id)
                        && let Some(progress) = route.progress_mut()
                    {
                        return Some(progress);
                    }
                }
                None
            }
        }
    }

    pub(super) fn record_event(
        &mut self,
        event: &BleEvent,
        connection_id: ConnectionId,
    ) -> Result<(), ReconciliationFault> {
        if let BleEvent::NotificationStateChanged {
            subscription_id, ..
        } = event
            && let Some(route) = self.subscriptions.get_mut(subscription_id)
        {
            route.record_event(event, connection_id, *subscription_id)?;
        }
        Ok(())
    }

    pub(super) fn resolve_dispatch(
        &mut self,
        command_id: CommandId,
        result: &Result<(), CommandDispatchError>,
        connection_id: ConnectionId,
    ) -> Option<DispatchResolution> {
        for (subscription_id, route) in &mut self.subscriptions {
            if let Some((resolution, disposition)) =
                route.resolve_dispatch(command_id, result, connection_id)
            {
                let subscription_id = *subscription_id;
                if matches!(disposition, SubscriptionDisposition::Remove) {
                    self.subscriptions.remove(&subscription_id);
                }
                return Some(resolution);
            }
        }
        None
    }

    pub(super) fn waiter_result(
        &self,
        command_id: CommandId,
        connection_id: ConnectionId,
    ) -> Option<WaiterOutcome> {
        self.subscriptions
            .iter()
            .find_map(|(subscription_id, route)| {
                route.waiter_result(command_id, connection_id, *subscription_id)
            })
    }

    /// Called after waiter settlement; route retention does not imply native continuity.
    pub(super) fn retire_completed(&mut self) -> Vec<CommandId> {
        let mut retired = Vec::new();
        self.subscriptions.retain(|_, route| {
            let Some(retirement) = route.retire_transition() else {
                return true;
            };
            retired.push(retirement.command_id);
            matches!(retirement.disposition, SubscriptionDisposition::Keep)
        });
        retired
    }
}
