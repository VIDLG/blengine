use std::collections::HashMap;

use blehub::{
    BleEvent, BleEventError, CommandDispatchError, CommandId, ConnectionId, ConnectionState,
    ConnectionStateResult, GattCharacteristicId, GattSessionId, NotificationModeRequest,
    PeripheralId, SubscriptionId,
};

use crate::waiter::{WaiterError, WaiterSuccess};

use super::{
    connection::{ConnectionReservation, DispatchPendingRoute},
    dispatch::{DispatchResolution, ReconciliationFault},
    gatt::GattAccess,
};

/// Actor-local ownership of connection lifecycles, keyed only by peripheral.
/// Lookups by connection or command ID traverse the same records; there is no
/// second index to keep synchronized. Only this container inserts/removes roots.
#[derive(Default)]
pub struct ConnectionReservations {
    by_peripheral: HashMap<PeripheralId, ConnectionReservation>,
}

impl ConnectionReservations {
    /// Gates live callbacks before publication; this is not an ownership query.
    /// Some(event) passes through, including owned events needing no buffering.
    /// None retains the event pending dispatch resolution; overflow is an error.
    pub fn gate_event(&mut self, event: BleEvent) -> Result<Option<BleEvent>, ReconciliationFault> {
        // Category priority is global, not per connection. A malformed callback
        // can identify one connection by command and another by connection ID.
        for route in [
            DispatchPendingRoute::Disconnect,
            DispatchPendingRoute::Discovery,
            DispatchPendingRoute::Rssi,
            DispatchPendingRoute::Read,
            DispatchPendingRoute::Write,
            DispatchPendingRoute::Subscription,
            DispatchPendingRoute::Connect,
        ] {
            for connection in self.by_peripheral.values_mut() {
                if connection.buffer_pending_dispatch_event(route, &event)? {
                    return Ok(None);
                }
            }
        }
        Ok(Some(event))
    }

    pub fn record_event(&mut self, event: &BleEvent) -> Result<(), ReconciliationFault> {
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

    /// Returns None when no retained connection command owns this dispatch result.
    /// Otherwise updates reservations and returns ordered replay plus actor actions.
    pub fn resolve_dispatch(
        &mut self,
        command_id: CommandId,
        result: &Result<(), CommandDispatchError>,
    ) -> Option<DispatchResolution> {
        let (peripheral_id, connection) = self.find_command_mut(command_id)?;
        let root_rejected = result.is_err() && connection.owns_root_command(command_id);
        let resolution = connection.resolve_dispatch(command_id, result)?;
        if root_rejected {
            self.remove(peripheral_id);
        }
        Some(resolution)
    }

    pub fn is_foreign_disconnect(&self, event: &BleEvent) -> bool {
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

    pub fn disconnect_command(&self, connection_id: ConnectionId) -> Option<CommandId> {
        self.find(connection_id)?.1.disconnect_command()
    }

    pub fn require_peripheral_available(
        &self,
        peripheral_id: PeripheralId,
    ) -> Result<(), WaiterError> {
        if self.contains_peripheral(peripheral_id) {
            return Err(WaiterError::PeripheralBusy);
        }
        Ok(())
    }

    pub fn validate_disconnect(
        &self,
        connection_id: ConnectionId,
    ) -> Result<PeripheralId, WaiterError> {
        let (key, connection) = self
            .find(connection_id)
            .ok_or(WaiterError::ConnectionNotActive)?;
        connection.validate_disconnect()?;
        Ok(key)
    }

    pub fn validate_gatt_discovery(
        &self,
        connection_id: ConnectionId,
    ) -> Result<PeripheralId, WaiterError> {
        let (key, connection) = self
            .find(connection_id)
            .ok_or(WaiterError::ConnectionNotActive)?;
        connection.validate_gatt_discovery()?;
        Ok(key)
    }

    pub fn validate_rssi(&self, connection_id: ConnectionId) -> Result<PeripheralId, WaiterError> {
        let (key, connection) = self
            .find(connection_id)
            .ok_or(WaiterError::ConnectionNotActive)?;
        connection.validate_rssi()?;
        Ok(key)
    }

    pub fn validate_gatt_access(
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

    pub fn validate_notification_enable(
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

    pub fn validate_notification_disable(
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

    pub fn reserve_connection(
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
    pub fn reserve_disconnect(&mut self, peripheral_id: PeripheralId, command_id: CommandId) {
        self.connection_mut(peripheral_id)
            .reserve_disconnect(command_id);
    }

    /// Registers a GATT discovery transition while preserving any committed database.
    pub fn reserve_gatt_discovery(
        &mut self,
        peripheral_id: PeripheralId,
        command_id: CommandId,
        session_id: GattSessionId,
    ) {
        self.connection_mut(peripheral_id)
            .reserve_gatt_discovery(command_id, session_id);
    }

    /// Registers a pending RSSI read on an existing connection.
    pub fn reserve_rssi(&mut self, peripheral_id: PeripheralId, command_id: CommandId) {
        self.connection_mut(peripheral_id).reserve_rssi(command_id);
    }

    /// Registers a pending characteristic read.
    pub fn reserve_read(
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
    pub fn reserve_write(
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
    pub fn reserve_notification_enable(
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
    pub fn reserve_notification_disable(
        &mut self,
        peripheral_id: PeripheralId,
        command_id: CommandId,
        subscription_id: SubscriptionId,
    ) {
        self.connection_mut(peripheral_id)
            .reserve_notification_disable(command_id, subscription_id);
    }

    /// Collects accepted commands for loss settlement without exposing connection records.
    pub fn accepted_command_ids(&self, connection_id: ConnectionId) -> Vec<CommandId> {
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

    /// Records the first terminal in its owning command, ignoring unowned IDs.
    /// No ownership result is needed: this collection is the actor's final route.
    pub fn record_terminal(&mut self, command_id: CommandId, result: &Result<(), BleEventError>) {
        for connection in self.by_peripheral.values_mut() {
            if connection.record_terminal(command_id, result) {
                return;
            }
        }
    }

    /// Queries a deliverable result without advancing or retiring reservations.
    /// None means unowned or incomplete, not operation failure.
    pub fn waiter_result(
        &self,
        command_id: CommandId,
    ) -> Option<Result<WaiterSuccess, WaiterError>> {
        self.by_peripheral
            .values()
            .find_map(|connection| connection.waiter_result(command_id))
    }

    /// Call only after settling affected waiters, including after late callbacks.
    /// Retires completed child operations before removing retirable connection roots;
    /// caller timeout or cancellation alone permits neither kind of retirement.
    pub fn retire_completed(&mut self) {
        self.by_peripheral.retain(|_, connection| {
            connection.retire_completed_operations();
            !connection.is_retirable()
        });
    }
}
