use std::collections::HashMap;

use blehub::{
    BleEvent, BleEventError, CommandDispatchError, CommandId, ConnectionId, ConnectionState,
    ConnectionStateResult, GattCharacteristicId, GattSessionId, NotificationModeRequest,
    PeripheralId, SubscriptionId,
};

use crate::waiter::{WaiterError, WaiterOutcome};

use super::super::{command::ConnectionOperationKind, shared::CompletionObserver};
use super::{
    CancellationDecision, DispatchResolution, GattAccess, PublicationDecision, ReconciliationFault,
    connection::ConnectionReservation,
};

/// Actor-local ownership of connection lifecycles, keyed only by peripheral.
/// Production command routing supplies the connection identity whenever the event
/// carries a command route. Terminal callbacks may carry only a command ID, so this
/// container also resolves those through its command inventory. Only this container
/// inserts and removes connection roots.
#[derive(Default)]
pub(crate) struct ConnectionReservations {
    by_peripheral: HashMap<PeripheralId, ConnectionReservation>,
}

impl ConnectionReservations {
    /// Gates live callbacks before publication; this is not an ownership query.
    /// Some(event) passes through, including owned events needing no buffering.
    /// None retains the event pending dispatch resolution; overflow is an error.
    pub(crate) fn gate_event(
        &mut self,
        event: BleEvent,
    ) -> Result<Option<BleEvent>, ReconciliationFault> {
        // Category priority is global, not per connection. A malformed callback
        // can identify one connection by command and another by connection ID.
        for kind in [
            ConnectionOperationKind::Disconnect,
            ConnectionOperationKind::DiscoverGatt,
            ConnectionOperationKind::ReadRssi,
            ConnectionOperationKind::Read,
            ConnectionOperationKind::Write,
            ConnectionOperationKind::EnableNotifications,
            ConnectionOperationKind::Connect,
        ] {
            for connection in self.by_peripheral.values_mut() {
                if connection.buffer_pending_dispatch_event(kind, &event)? {
                    return Ok(None);
                }
            }
        }
        Ok(Some(event))
    }

    pub(crate) fn gate_event_for_conn(
        &mut self,
        connection_id: ConnectionId,
        kind: ConnectionOperationKind,
        event: BleEvent,
    ) -> Result<Option<BleEvent>, ReconciliationFault> {
        let Some(connection) = self.find_mut(connection_id) else {
            return Ok(Some(event));
        };
        if connection.buffer_pending_dispatch_event(kind, &event)? {
            return Ok(None);
        }
        Ok(Some(event))
    }

    pub(crate) fn record_event(&mut self, event: &BleEvent) -> Result<(), ReconciliationFault> {
        let connection_id = match event {
            BleEvent::ConnectionStateChanged { connection_id, .. }
            | BleEvent::RssiRead { connection_id, .. }
            | BleEvent::CharacteristicRead { connection_id, .. }
            | BleEvent::CharacteristicWritten { connection_id, .. }
            | BleEvent::NotificationStateChanged { connection_id, .. } => *connection_id,
            BleEvent::GattDiscovered { database, .. } => database.connection_id,
            _ => return Ok(()),
        };
        if let Some(connection) = self.find_mut(connection_id) {
            connection.record_event(event)?;
        }
        Ok(())
    }

    /// Resolves a command through its actor-provided connection route.
    /// The route avoids scanning every peripheral; the connection still owns
    /// the operation-specific evidence and command progress.
    pub(crate) fn resolve_dispatch_for_conn(
        &mut self,
        connection_id: ConnectionId,
        command_id: CommandId,
        result: &Result<(), CommandDispatchError>,
    ) -> Option<DispatchResolution> {
        let (peripheral_id, root_rejected, resolution) = {
            let (peripheral_id, connection) = self.find_mut_with_key(connection_id)?;
            let root_rejected = result.is_err() && connection.owns_root_command(command_id);
            let resolution = connection.resolve_dispatch(command_id, result)?;
            (peripheral_id, root_rejected, resolution)
        };
        if root_rejected {
            self.remove(peripheral_id);
        }
        Some(resolution)
    }

    pub(crate) fn completion_observer_for_conn(
        &self,
        connection_id: ConnectionId,
        command_id: CommandId,
    ) -> Option<CompletionObserver> {
        self.find(connection_id)
            .and_then(|(_, connection)| connection.progress(command_id))
            .map(|progress| progress.completion_observer())
    }

    pub(crate) fn decide_publication_for_conn(
        &mut self,
        connection_id: ConnectionId,
        event: &BleEvent,
    ) -> Result<PublicationDecision, ReconciliationFault> {
        match self.find_mut(connection_id) {
            Some(connection) => connection.decide_publication(event),
            None => Ok(PublicationDecision::Publish),
        }
    }

    pub(crate) fn decide_publication_for_cmd(
        &mut self,
        command_id: CommandId,
        event: &BleEvent,
    ) -> Result<PublicationDecision, ReconciliationFault> {
        match self.find_command_mut(command_id) {
            Some((_, connection)) => connection.decide_publication(event),
            None => Ok(PublicationDecision::Publish),
        }
    }

    pub(crate) fn take_cancellation_public_events_for_conn(
        &mut self,
        connection_id: ConnectionId,
        command_id: CommandId,
    ) -> Vec<BleEvent> {
        self.find_mut(connection_id)
            .map(|connection| connection.take_cancellation_public_events(command_id))
            .unwrap_or_default()
    }

    pub(crate) fn is_foreign_disconnect(&self, event: &BleEvent) -> bool {
        if let BleEvent::ConnectionStateChanged {
            command_id: Some(command_id),
            connection_id,
            result:
                ConnectionStateResult::Ok {
                    state: ConnectionState::Disconnected,
                },
        } = event
            && let Some((_, connection)) = self.find(*connection_id)
        {
            return connection.is_foreign_disconnect(*command_id);
        }
        false
    }

    pub(crate) fn disconnect_command(&self, connection_id: ConnectionId) -> Option<CommandId> {
        self.find(connection_id)?.1.disconnect_command()
    }

    pub(crate) fn require_peripheral_available(
        &self,
        peripheral_id: PeripheralId,
    ) -> Result<(), WaiterError> {
        if self.contains_peripheral(peripheral_id) {
            return Err(WaiterError::PeripheralBusy);
        }
        Ok(())
    }

    pub(crate) fn validate_disconnect(
        &self,
        connection_id: ConnectionId,
    ) -> Result<PeripheralId, WaiterError> {
        let (key, connection) = self
            .find(connection_id)
            .ok_or(WaiterError::ConnectionNotActive)?;
        connection.validate_disconnect()?;
        Ok(key)
    }

    pub(crate) fn validate_gatt_discovery(
        &self,
        connection_id: ConnectionId,
    ) -> Result<PeripheralId, WaiterError> {
        let (key, connection) = self
            .find(connection_id)
            .ok_or(WaiterError::ConnectionNotActive)?;
        connection.validate_gatt_discovery()?;
        Ok(key)
    }

    pub(crate) fn validate_rssi(
        &self,
        connection_id: ConnectionId,
    ) -> Result<PeripheralId, WaiterError> {
        let (key, connection) = self
            .find(connection_id)
            .ok_or(WaiterError::ConnectionNotActive)?;
        connection.validate_rssi()?;
        Ok(key)
    }

    pub(crate) fn validate_gatt_access(
        &self,
        connection_id: ConnectionId,
        session_id: GattSessionId,
        characteristic_id: GattCharacteristicId,
        access: GattAccess,
    ) -> Result<PeripheralId, WaiterError> {
        let (key, connection) = self
            .find(connection_id)
            .ok_or(WaiterError::ConnectionNotActive)?;
        connection.validate_gatt_access(session_id, characteristic_id, access)?;
        Ok(key)
    }

    pub(crate) fn validate_notification_enable(
        &self,
        connection_id: ConnectionId,
        session_id: GattSessionId,
        characteristic_id: GattCharacteristicId,
    ) -> Result<PeripheralId, WaiterError> {
        self.validate_gatt_access(
            connection_id,
            session_id,
            characteristic_id,
            GattAccess::EnableNotifications,
        )
    }

    pub(crate) fn validate_notification_disable(
        &self,
        connection_id: ConnectionId,
        session_id: GattSessionId,
        subscription_id: SubscriptionId,
    ) -> Result<(PeripheralId, GattCharacteristicId), WaiterError> {
        let (key, connection) = self
            .find(connection_id)
            .ok_or(WaiterError::ConnectionNotActive)?;
        let characteristic =
            connection.validate_notification_disable(session_id, subscription_id)?;
        Ok((key, characteristic))
    }

    pub(crate) fn reserve_connection(
        &mut self,
        peripheral_id: PeripheralId,
        command_id: CommandId,
        connection_id: ConnectionId,
    ) {
        let std::collections::hash_map::Entry::Vacant(entry) =
            self.by_peripheral.entry(peripheral_id)
        else {
            panic!("validated peripheral already has a connection reservation");
        };
        entry.insert(ConnectionReservation::new(command_id, connection_id));
    }

    /// Registers a disconnect transition on an existing connection.
    pub(crate) fn reserve_disconnect(
        &mut self,
        peripheral_id: PeripheralId,
        command_id: CommandId,
    ) {
        self.connection_mut(peripheral_id)
            .reserve_disconnect(command_id);
    }

    /// Registers a GATT discovery transition while preserving any committed database.
    pub(crate) fn reserve_gatt_discovery(
        &mut self,
        peripheral_id: PeripheralId,
        command_id: CommandId,
        session_id: GattSessionId,
    ) {
        self.connection_mut(peripheral_id)
            .reserve_gatt_discovery(command_id, session_id);
    }

    /// Registers a pending RSSI read on an existing connection.
    pub(crate) fn reserve_rssi(&mut self, peripheral_id: PeripheralId, command_id: CommandId) {
        self.connection_mut(peripheral_id).reserve_rssi(command_id);
    }

    /// Registers a pending characteristic read.
    pub(crate) fn reserve_read(
        &mut self,
        peripheral_id: PeripheralId,
        command_id: CommandId,
        session_id: GattSessionId,
        characteristic_id: GattCharacteristicId,
    ) {
        self.connection_mut(peripheral_id)
            .reserve_read(command_id, session_id, characteristic_id);
    }

    /// Registers a pending characteristic write.
    pub(crate) fn reserve_write(
        &mut self,
        peripheral_id: PeripheralId,
        command_id: CommandId,
        session_id: GattSessionId,
        characteristic_id: GattCharacteristicId,
    ) {
        self.connection_mut(peripheral_id)
            .reserve_write(command_id, session_id, characteristic_id);
    }

    /// Registers a notification enable transition and its new subscription.
    pub(crate) fn reserve_notification_enable(
        &mut self,
        peripheral_id: PeripheralId,
        command_id: CommandId,
        subscription_id: SubscriptionId,
        session_id: GattSessionId,
        characteristic_id: GattCharacteristicId,
        mode: NotificationModeRequest,
    ) {
        self.connection_mut(peripheral_id)
            .reserve_notification_enable(
                command_id,
                subscription_id,
                session_id,
                characteristic_id,
                mode,
            );
    }

    /// Registers a notification disable transition on an active subscription.
    pub(crate) fn reserve_notification_disable(
        &mut self,
        peripheral_id: PeripheralId,
        command_id: CommandId,
        subscription_id: SubscriptionId,
    ) {
        self.connection_mut(peripheral_id)
            .reserve_notification_disable(command_id, subscription_id);
    }

    /// Collects accepted commands for loss settlement without exposing connection records.
    pub(crate) fn accepted_command_ids(&self, connection_id: ConnectionId) -> Vec<CommandId> {
        self.find(connection_id)
            .map(|(_, connection)| connection.accepted_command_ids())
            .unwrap_or_default()
    }

    fn contains_peripheral(&self, peripheral_id: PeripheralId) -> bool {
        self.by_peripheral.contains_key(&peripheral_id)
    }

    fn connection_mut(&mut self, peripheral_id: PeripheralId) -> &mut ConnectionReservation {
        self.by_peripheral
            .get_mut(&peripheral_id)
            .expect("validated connection")
    }

    fn find(&self, connection_id: ConnectionId) -> Option<(PeripheralId, &ConnectionReservation)> {
        self.by_peripheral
            .iter()
            .find_map(|(peripheral_id, connection)| {
                connection
                    .matches_connection_id(connection_id)
                    .then_some((*peripheral_id, connection))
            })
    }

    fn find_mut(&mut self, connection_id: ConnectionId) -> Option<&mut ConnectionReservation> {
        self.by_peripheral
            .values_mut()
            .find(|connection| connection.matches_connection_id(connection_id))
    }

    fn find_mut_with_key(
        &mut self,
        connection_id: ConnectionId,
    ) -> Option<(PeripheralId, &mut ConnectionReservation)> {
        self.by_peripheral
            .iter_mut()
            .find(|(_, connection)| connection.matches_connection_id(connection_id))
            .map(|(peripheral_id, connection)| (*peripheral_id, connection))
    }

    fn find_command_mut(
        &mut self,
        command_id: CommandId,
    ) -> Option<(PeripheralId, &mut ConnectionReservation)> {
        self.by_peripheral
            .iter_mut()
            .find_map(|(peripheral_id, connection)| {
                connection
                    .contains_command(command_id)
                    .then_some((*peripheral_id, connection))
            })
    }

    fn remove(&mut self, peripheral_id: PeripheralId) -> Option<ConnectionReservation> {
        self.by_peripheral.remove(&peripheral_id)
    }

    pub(crate) fn record_terminal_for_conn(
        &mut self,
        connection_id: ConnectionId,
        command_id: CommandId,
        result: &Result<(), BleEventError>,
    ) {
        if let Some(connection) = self.find_mut(connection_id) {
            connection.record_terminal(command_id, result);
        }
    }

    pub(crate) fn waiter_result_for_conn(
        &self,
        connection_id: ConnectionId,
        command_id: CommandId,
    ) -> Option<WaiterOutcome> {
        self.find(connection_id)
            .and_then(|(_, connection)| connection.waiter_result(command_id))
    }

    pub(crate) fn request_cancellation_for_conn(
        &mut self,
        connection_id: ConnectionId,
        command_id: CommandId,
    ) -> Option<CancellationDecision> {
        self.find_mut(connection_id)
            .and_then(|connection| connection.request_cancellation(command_id))
    }

    pub(crate) fn try_claim_cancellation_dispatch_for_conn(
        &mut self,
        connection_id: ConnectionId,
        command_id: CommandId,
    ) -> bool {
        self.find_mut(connection_id)
            .is_some_and(|connection| connection.try_claim_cancellation_dispatch(command_id))
    }

    pub(crate) fn try_resolve_cancellation_for_conn(
        &mut self,
        connection_id: ConnectionId,
        command_id: CommandId,
        result: Result<(), WaiterError>,
    ) -> bool {
        self.find_mut(connection_id)
            .is_some_and(|connection| connection.try_resolve_cancellation(command_id, result))
    }

    pub(crate) fn try_clear_cancellation_for_conn(
        &mut self,
        connection_id: ConnectionId,
        command_id: CommandId,
    ) -> bool {
        self.find_mut(connection_id)
            .is_some_and(|connection| connection.try_clear_cancellation(command_id))
    }

    pub(crate) fn is_cancellation_requested_for_conn(
        &self,
        connection_id: ConnectionId,
        command_id: CommandId,
    ) -> bool {
        self.find(connection_id)
            .is_some_and(|(_, connection)| connection.is_cancellation_requested(command_id))
    }

    /// Call only after settling affected waiters, including after late callbacks.
    /// Releases completed child operations and connection roots, returning every
    /// command route that can now be removed from the coordinator.
    pub(crate) fn retire_completed(&mut self) -> Vec<CommandId> {
        let mut retired = Vec::new();
        self.by_peripheral.retain(|_, connection| {
            retired.extend(connection.retire_completed_operations());
            if connection.is_retirable() {
                retired.extend(connection.command_ids());
                false
            } else {
                true
            }
        });
        retired
    }
}
