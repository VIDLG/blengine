use std::collections::HashMap;

use blehub::{
    BleEvent, BleEventError, CommandDispatchError, CommandId, ConnectionId, ConnectionState,
    ConnectionStateResult, GattCharacteristicId, GattSessionId, NotificationModeRequest,
    PeripheralId, SubscriptionId,
};

use crate::waiter::{WaiterError, WaiterSuccess};

use super::{
    connection::{ConnectionReservation, DisconnectReservation, PreAckRoute},
    dispatch::{DispatchEffect, DispatchReservation, DispatchResolution, ReconcileFault},
    gatt::GattReservation,
    io::{ReadReservation, RssiReservation, WriteReservation},
    subscription::SubscriptionReservation,
};

/// Actor-local ownership of connection lifecycles, keyed only by peripheral.
/// Lookups by connection or command ID traverse the same records; there is no
/// second index to keep synchronized. Only this container inserts/removes roots.
#[derive(Default)]
pub struct ConnectionReservations {
    by_peripheral: HashMap<PeripheralId, ConnectionReservation>,
}

impl ConnectionReservations {
    pub fn buffer_before_dispatch(
        &mut self,
        event: BleEvent,
    ) -> Result<Option<BleEvent>, ReconcileFault> {
        // Category priority is global, not per connection. A malformed callback
        // can identify one connection by command and another by connection ID.
        for route in [
            PreAckRoute::Disconnect,
            PreAckRoute::Discovery,
            PreAckRoute::Rssi,
            PreAckRoute::Read,
            PreAckRoute::Write,
            PreAckRoute::Subscription,
            PreAckRoute::Connect,
        ] {
            for connection in self.by_peripheral.values_mut() {
                if let Some(dispatch) = connection.pre_ack_route(route, &event) {
                    dispatch.buffer(event)?;
                    return Ok(None);
                }
            }
        }
        Ok(Some(event))
    }

    pub fn record_event(&mut self, event: &BleEvent) -> Result<(), ReconcileFault> {
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

    pub fn resolve_dispatch(
        &mut self,
        command_id: CommandId,
        result: &Result<(), CommandDispatchError>,
    ) -> Option<DispatchResolution> {
        let (peripheral_id, connection) = self.find_command_mut(command_id)?;
        if let Some(resolution) = connection.resolve_operation_dispatch(command_id, result) {
            return Some(resolution);
        }
        debug_assert_eq!(connection.dispatch.command_id, command_id);
        Some(if result.is_err() {
            let contradictory = !connection.dispatch.buffered.is_empty();
            self.remove(peripheral_id);
            DispatchResolution {
                effect: if contradictory {
                    DispatchEffect::Fault(ReconcileFault::ContradictoryDispatch)
                } else {
                    DispatchEffect::None
                },
                ..Default::default()
            }
        } else {
            connection.dispatch.accepted_resolution()
        })
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
            return *command_id != connection.dispatch.command_id
                && !connection
                    .disconnect
                    .as_ref()
                    .is_some_and(|slot| slot.dispatch.command_id == *command_id);
        }
        false
    }

    pub fn disconnect_command(&self, connection_id: ConnectionId) -> Option<CommandId> {
        self.find(connection_id)?
            .1
            .disconnect
            .as_ref()
            .map(|slot| slot.dispatch.command_id)
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
        // An accepted connect owns a native lifecycle even before Connected arrives.
        if connection.disconnected
            || connection.has_failed()
            || connection.dispatch.is_awaiting_dispatch_ack()
        {
            return Err(WaiterError::ConnectionNotActive);
        }
        if connection.disconnect.is_some()
            || connection
                .gatt
                .as_ref()
                .and_then(|gatt| gatt.discovery.as_ref())
                .is_some_and(|discovery| discovery.dispatch.is_awaiting_dispatch_ack())
            || connection
                .rssi
                .as_ref()
                .is_some_and(|slot| slot.dispatch.is_awaiting_dispatch_ack())
            || connection
                .read
                .as_ref()
                .is_some_and(|slot| slot.dispatch.is_awaiting_dispatch_ack())
            || connection
                .write
                .as_ref()
                .is_some_and(|slot| slot.dispatch.is_awaiting_dispatch_ack())
            || connection.subscriptions.values().any(|route| {
                route
                    .transition
                    .as_ref()
                    .is_some_and(|slot| slot.dispatch.is_awaiting_dispatch_ack())
            })
        {
            // Pre-dispatch siblings must learn acceptance before passive loss.
            return Err(WaiterError::OperationBusy);
        }
        Ok(key)
    }

    pub fn validate_gatt_discovery(
        &self,
        connection_id: ConnectionId,
    ) -> Result<PeripheralId, WaiterError> {
        let (key, connection) = self
            .find(connection_id)
            .ok_or(WaiterError::ConnectionNotActive)?;
        if !connection.connected || connection.disconnected || connection.has_failed() {
            return Err(WaiterError::ConnectionNotActive);
        }
        if connection.disconnect.is_some()
            || connection
                .rssi
                .as_ref()
                .is_some_and(|slot| slot.dispatch.is_awaiting_dispatch_ack())
            || connection
                .gatt
                .as_ref()
                .is_some_and(GattReservation::has_pending_discovery)
            || connection.read.is_some()
            || connection.write.is_some()
            || connection
                .subscriptions
                .values()
                .any(|subscription| subscription.active || subscription.transition.is_some())
        {
            return Err(WaiterError::GattBusy);
        }
        Ok(key)
    }

    pub fn validate_rssi(&self, connection_id: ConnectionId) -> Result<PeripheralId, WaiterError> {
        let (key, connection) = self
            .find(connection_id)
            .ok_or(WaiterError::ConnectionNotActive)?;
        if !connection.connected || connection.disconnected || connection.has_failed() {
            return Err(WaiterError::ConnectionNotActive);
        }
        if connection.rssi.is_some()
            || connection.disconnect.is_some()
            || connection
                .gatt
                .as_ref()
                .and_then(|gatt| gatt.discovery.as_ref())
                .is_some_and(|discovery| discovery.dispatch.is_awaiting_dispatch_ack())
            || connection
                .read
                .as_ref()
                .is_some_and(|slot| slot.dispatch.is_awaiting_dispatch_ack())
            || connection
                .write
                .as_ref()
                .is_some_and(|slot| slot.dispatch.is_awaiting_dispatch_ack())
            || connection.subscriptions.values().any(|route| {
                route
                    .transition
                    .as_ref()
                    .is_some_and(|slot| slot.dispatch.is_awaiting_dispatch_ack())
            })
        {
            return Err(WaiterError::OperationBusy);
        }
        Ok(key)
    }

    pub fn validate_gatt_io(
        &self,
        connection_id: ConnectionId,
        session_id: GattSessionId,
        characteristic_id: GattCharacteristicId,
        read: bool,
    ) -> Result<PeripheralId, WaiterError> {
        let (key, connection) = self
            .find(connection_id)
            .ok_or(WaiterError::ConnectionNotActive)?;
        connection.validate_gatt_io(session_id, characteristic_id, read)?;
        Ok(key)
    }

    pub fn validate_notification_enable(
        &self,
        connection_id: ConnectionId,
        session_id: GattSessionId,
        characteristic_id: GattCharacteristicId,
    ) -> Result<PeripheralId, WaiterError> {
        let (key, connection) = self
            .find(connection_id)
            .ok_or(WaiterError::ConnectionNotActive)?;
        connection.validate_gatt_io(session_id, characteristic_id, false)?;
        if connection
            .subscriptions
            .values()
            .any(|route| route.characteristic_id == characteristic_id)
        {
            return Err(WaiterError::OperationBusy);
        }
        Ok(key)
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
        let route = connection
            .subscriptions
            .get(&subscription_id)
            .filter(|route| route.session_id == session_id && route.active)
            .ok_or(WaiterError::SubscriptionNotActive)?;
        if route.transition.is_some() {
            return Err(WaiterError::OperationBusy);
        }
        let characteristic = route.characteristic_id;

        if !connection.connected || connection.disconnected || connection.has_failed() {
            return Err(WaiterError::ConnectionNotActive);
        }
        if connection.disconnect.is_some()
            || connection
                .rssi
                .as_ref()
                .is_some_and(|slot| slot.dispatch.is_awaiting_dispatch_ack())
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
                    .is_some_and(|slot| slot.dispatch.is_awaiting_dispatch_ack())
            })
        {
            return Err(WaiterError::OperationBusy);
        }
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
        self.connection_mut(peripheral_id).disconnect = Some(DisconnectReservation {
            dispatch: DispatchReservation::new(command_id),
            domain: false,
            terminal: None,
        });
    }

    /// Registers a GATT discovery transition while preserving any committed database.
    pub fn reserve_gatt_discovery(
        &mut self,
        peripheral_id: PeripheralId,
        command_id: CommandId,
        session_id: GattSessionId,
    ) {
        self.connection_mut(peripheral_id)
            .gatt
            .get_or_insert_with(GattReservation::default)
            .begin_discovery(command_id, session_id);
    }

    /// Registers a pending RSSI read on an existing connection.
    pub fn reserve_rssi(&mut self, peripheral_id: PeripheralId, command_id: CommandId) {
        self.connection_mut(peripheral_id).rssi = Some(RssiReservation {
            dispatch: DispatchReservation::new(command_id),
            value: None,
            terminal: None,
        });
    }

    /// Registers a pending characteristic read.
    pub fn reserve_read(
        &mut self,
        peripheral_id: PeripheralId,
        command_id: CommandId,
        session_id: GattSessionId,
        characteristic_id: GattCharacteristicId,
    ) {
        self.connection_mut(peripheral_id).read = Some(ReadReservation::new(
            command_id,
            session_id,
            characteristic_id,
        ));
    }

    /// Registers a pending characteristic write.
    pub fn reserve_write(
        &mut self,
        peripheral_id: PeripheralId,
        command_id: CommandId,
        session_id: GattSessionId,
        characteristic_id: GattCharacteristicId,
    ) {
        self.connection_mut(peripheral_id).write = Some(WriteReservation::new(
            command_id,
            session_id,
            characteristic_id,
        ));
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
        self.connection_mut(peripheral_id).subscriptions.insert(
            subscription_id,
            SubscriptionReservation::enabling(command_id, session_id, characteristic_id, mode),
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
            .subscriptions
            .get_mut(&subscription_id)
            .expect("validated subscription")
            .begin_disable(command_id);
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
                (connection.connection_id == connection_id).then_some((*peripheral_id, connection))
            })
    }

    fn find_mut(&mut self, connection_id: ConnectionId) -> Option<&mut ConnectionReservation> {
        self.by_peripheral
            .values_mut()
            .find(|connection| connection.connection_id == connection_id)
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

    pub fn record_terminal(&mut self, command_id: CommandId, result: &Result<(), BleEventError>) {
        if let Some((_, connection)) = self.find_command_mut(command_id) {
            connection.record_terminal(command_id, result);
        }
    }

    pub fn waiter_result(
        &self,
        command_id: CommandId,
    ) -> Option<Result<WaiterSuccess, WaiterError>> {
        self.by_peripheral
            .values()
            .find_map(|connection| connection.waiter_result(command_id))
    }

    /// Call only after settling affected waiters, including after late callbacks.
    pub fn retire_completed(&mut self) {
        self.by_peripheral.retain(|_, connection| {
            connection.retire_completed_operations();
            !connection.is_retirable()
        });
    }
}
