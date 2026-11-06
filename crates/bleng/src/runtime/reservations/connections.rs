use std::collections::HashMap;

use blehub::{
    BleEvent, BleEventError, CommandDispatchError, CommandId, ConnectionId, ConnectionState,
    ConnectionStateResult, GattCharacteristicId, GattSessionId, NotificationModeRequest,
    PeripheralId, SubscriptionId,
};

use crate::waiter::{WaiterError, WaiterOutcome};

use super::super::shared::CompletionObserver;
use super::{
    CancellationDecision, DispatchResolution, GattAccess, PublicationDecision, ReconciliationFault,
    connection::ConnectionReservation,
};

/// Actor-local ownership of connection lifecycles, keyed only by peripheral.
/// Command routes select non-state callbacks; state callbacks first select the
/// affected connection. Terminals without a route can use the retained command
/// inventory. Only this container inserts and removes connection roots.
#[derive(Default)]
pub(in crate::runtime) struct ConnectionReservations {
    by_peripheral: HashMap<PeripheralId, ConnectionReservation>,
}

impl ConnectionReservations {
    // Admission.

    pub(in crate::runtime) fn require_peripheral_available(
        &self,
        peripheral_id: PeripheralId,
    ) -> Result<(), WaiterError> {
        if self.by_peripheral.contains_key(&peripheral_id) {
            return Err(WaiterError::PeripheralBusy);
        }
        Ok(())
    }

    pub(in crate::runtime) fn validate_disconnect(
        &self,
        connection_id: ConnectionId,
    ) -> Result<PeripheralId, WaiterError> {
        let (peripheral_id, connection) = self.require_entry_for_conn(connection_id)?;
        connection.validate_disconnect()?;
        Ok(peripheral_id)
    }

    pub(in crate::runtime) fn validate_gatt_discovery(
        &self,
        connection_id: ConnectionId,
    ) -> Result<PeripheralId, WaiterError> {
        let (peripheral_id, connection) = self.require_entry_for_conn(connection_id)?;
        connection.validate_gatt_discovery()?;
        Ok(peripheral_id)
    }

    pub(in crate::runtime) fn validate_rssi(
        &self,
        connection_id: ConnectionId,
    ) -> Result<PeripheralId, WaiterError> {
        let (peripheral_id, connection) = self.require_entry_for_conn(connection_id)?;
        connection.validate_rssi()?;
        Ok(peripheral_id)
    }

    pub(in crate::runtime) fn validate_gatt_access(
        &self,
        connection_id: ConnectionId,
        session_id: GattSessionId,
        characteristic_id: GattCharacteristicId,
        access: GattAccess,
    ) -> Result<PeripheralId, WaiterError> {
        let (peripheral_id, connection) = self.require_entry_for_conn(connection_id)?;
        connection.validate_gatt_access(session_id, characteristic_id, access)?;
        Ok(peripheral_id)
    }

    pub(in crate::runtime) fn validate_notification_enable(
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

    pub(in crate::runtime) fn validate_notification_disable(
        &self,
        connection_id: ConnectionId,
        session_id: GattSessionId,
        subscription_id: SubscriptionId,
    ) -> Result<(PeripheralId, GattCharacteristicId), WaiterError> {
        let (peripheral_id, connection) = self.require_entry_for_conn(connection_id)?;
        let characteristic =
            connection.validate_notification_disable(session_id, subscription_id)?;
        Ok((peripheral_id, characteristic))
    }

    // Installation after admission and submission claim.

    pub(in crate::runtime) fn reserve_connection(
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

    delegate::delegate! {
        to |peripheral_id: PeripheralId| self.reserved_for_peripheral_mut(peripheral_id) {
            /// Registers a disconnect transition on an existing connection.
            pub(in crate::runtime) fn reserve_disconnect(&mut self, command_id: CommandId);

            /// Registers a GATT discovery transition while preserving any committed database.
            pub(in crate::runtime) fn reserve_gatt_discovery(
                &mut self,
                command_id: CommandId,
                session_id: GattSessionId,
            );

            /// Registers a pending RSSI read on an existing connection.
            pub(in crate::runtime) fn reserve_rssi(&mut self, command_id: CommandId);

            /// Registers a pending characteristic read.
            pub(in crate::runtime) fn reserve_read(
                &mut self,
                command_id: CommandId,
                session_id: GattSessionId,
                characteristic_id: GattCharacteristicId,
            );

            /// Registers a pending characteristic write.
            pub(in crate::runtime) fn reserve_write(
                &mut self,
                command_id: CommandId,
                session_id: GattSessionId,
                characteristic_id: GattCharacteristicId,
            );

            /// Registers a notification enable transition and its new subscription.
            pub(in crate::runtime) fn reserve_notification_enable(
                &mut self,
                command_id: CommandId,
                subscription_id: SubscriptionId,
                session_id: GattSessionId,
                characteristic_id: GattCharacteristicId,
                mode: NotificationModeRequest,
            );

            /// Registers a notification disable transition on an active subscription.
            pub(in crate::runtime) fn reserve_notification_disable(
                &mut self,
                command_id: CommandId,
                subscription_id: SubscriptionId,
            );
        }
    }

    // Command queries.

    pub(super) fn completion_observer_for_conn(
        &self,
        connection_id: ConnectionId,
        command_id: CommandId,
    ) -> Option<CompletionObserver> {
        let connection = self.reserved_for_conn(connection_id)?;
        let progress = connection.progress_for_cmd(command_id)?;
        Some(progress.completion_observer())
    }

    delegate::delegate! {
        to |connection_id: ConnectionId| self.reserved_for_conn(connection_id)? {
            pub(super) fn disconnect_command(&self) -> Option<CommandId>;
        }
    }

    /// Collects accepted commands for loss settlement without exposing connection records.
    pub(super) fn accepted_command_ids(&self, connection_id: ConnectionId) -> Vec<CommandId> {
        self.reserved_for_conn(connection_id)
            .map(ConnectionReservation::accepted_command_ids)
            .unwrap_or_default()
    }

    // Dispatch and evidence.

    pub(in crate::runtime) fn is_foreign_disconnect(&self, event: &BleEvent) -> bool {
        if let BleEvent::ConnectionStateChanged {
            command_id: Some(command_id),
            connection_id,
            result:
                ConnectionStateResult::Ok {
                    state: ConnectionState::Disconnected,
                },
        } = event
            && let Some(connection) = self.reserved_for_conn(*connection_id)
        {
            return connection.is_foreign_disconnect(*command_id);
        }
        false
    }

    /// Gates live callbacks before publication; this is not an ownership query.
    /// Some(event) passes through, including owned events needing no buffering.
    /// None retains the event pending dispatch resolution; overflow is an error.
    pub(in crate::runtime) fn gate_event(
        &mut self,
        event: BleEvent,
    ) -> Result<Option<BleEvent>, ReconciliationFault> {
        let connection = match &event {
            BleEvent::CommandCompleted { command_id, .. } => self.reserved_for_cmd_mut(*command_id),
            _ => Self::connection_id_for_event(&event)
                .and_then(|connection_id| self.reserved_for_conn_mut(connection_id)),
        };
        if let Some(connection) = connection
            && connection.try_buffer_dispatch_pending_event(&event)?
        {
            return Ok(None);
        }

        // A state callback may name a different connect by command ID. All gates
        // on the affected connection must precede this broad connect fallback.
        if let BleEvent::ConnectionStateChanged {
            command_id: Some(command_id),
            connection_id,
            ..
        } = &event
            && let Some(connection) = self.by_peripheral.values_mut().find(|connection| {
                !connection.matches_connection_id(*connection_id)
                    && connection.owns_root_command(*command_id)
            })
            && connection.try_buffer_dispatch_pending_event(&event)?
        {
            return Ok(None);
        }
        Ok(Some(event))
    }

    pub(super) fn gate_event_for_conn(
        &mut self,
        connection_id: ConnectionId,
        event: BleEvent,
    ) -> Result<Option<BleEvent>, ReconciliationFault> {
        let Some(connection) = self.reserved_for_conn_mut(connection_id) else {
            return Ok(Some(event));
        };
        if connection.try_buffer_dispatch_pending_event(&event)? {
            return Ok(None);
        }
        Ok(Some(event))
    }

    pub(in crate::runtime) fn record_event(
        &mut self,
        event: &BleEvent,
    ) -> Result<(), ReconciliationFault> {
        // Notification values belong to publication, not command-completion evidence.
        if matches!(event, BleEvent::NotificationReceived { .. }) {
            return Ok(());
        }
        let Some(connection_id) = Self::connection_id_for_event(event) else {
            return Ok(());
        };
        if let Some(connection) = self.reserved_for_conn_mut(connection_id) {
            connection.record_event(event)?;
        }
        Ok(())
    }

    /// Resolves a command through its actor-provided connection route.
    /// The route selects the connection; the connection still owns
    /// the operation-specific evidence and command progress.
    pub(in crate::runtime) fn resolve_dispatch_for_conn(
        &mut self,
        connection_id: ConnectionId,
        command_id: CommandId,
        result: &Result<(), CommandDispatchError>,
    ) -> Option<DispatchResolution> {
        let (peripheral_id, connection) = self.reserved_entry_for_conn_mut(connection_id)?;
        let root_rejected = result.is_err() && connection.owns_root_command(command_id);
        let resolution = connection.resolve_dispatch(command_id, result)?;
        if root_rejected {
            self.by_peripheral.remove(&peripheral_id);
        }
        Some(resolution)
    }

    pub(in crate::runtime) fn record_terminal_for_conn(
        &mut self,
        connection_id: ConnectionId,
        command_id: CommandId,
        result: &Result<(), BleEventError>,
    ) {
        if let Some(connection) = self.reserved_for_conn_mut(connection_id) {
            connection.try_record_terminal(command_id, result);
        }
    }

    // Cancellation.

    delegate::delegate! {
        to |connection_id: ConnectionId| self.reserved_for_conn_mut(connection_id)? {
            #[call(request_cancellation)]
            pub(super) fn request_cancellation_for_conn(
                &mut self,
                command_id: CommandId,
            ) -> Option<CancellationDecision>;
        }
    }

    pub(super) fn try_claim_cancellation_dispatch_for_conn(
        &mut self,
        connection_id: ConnectionId,
        command_id: CommandId,
    ) -> bool {
        self.reserved_for_conn_mut(connection_id)
            .is_some_and(|connection| connection.try_claim_cancellation_dispatch(command_id))
    }

    pub(super) fn try_resolve_cancellation_for_conn(
        &mut self,
        connection_id: ConnectionId,
        command_id: CommandId,
        result: Result<(), WaiterError>,
    ) -> bool {
        self.reserved_for_conn_mut(connection_id)
            .is_some_and(|connection| connection.try_resolve_cancellation(command_id, result))
    }

    pub(super) fn try_clear_cancellation_for_conn(
        &mut self,
        connection_id: ConnectionId,
        command_id: CommandId,
    ) -> bool {
        self.reserved_for_conn_mut(connection_id)
            .is_some_and(|connection| connection.try_clear_cancellation(command_id))
    }

    pub(super) fn is_cancellation_requested_for_conn(
        &self,
        connection_id: ConnectionId,
        command_id: CommandId,
    ) -> bool {
        self.reserved_for_conn(connection_id)
            .is_some_and(|connection| connection.is_cancellation_requested(command_id))
    }

    // Public event ordering.

    pub(super) fn decide_publication_for_conn(
        &mut self,
        connection_id: ConnectionId,
        event: &BleEvent,
    ) -> Result<PublicationDecision, ReconciliationFault> {
        match self.reserved_for_conn_mut(connection_id) {
            Some(connection) => connection.decide_publication(event),
            None => Ok(PublicationDecision::Publish),
        }
    }

    pub(super) fn decide_publication_for_cmd(
        &mut self,
        command_id: CommandId,
        event: &BleEvent,
    ) -> Result<PublicationDecision, ReconciliationFault> {
        match self.reserved_for_cmd_mut(command_id) {
            Some(connection) => connection.decide_publication(event),
            None => Ok(PublicationDecision::Publish),
        }
    }

    pub(super) fn take_cancellation_public_events_for_conn(
        &mut self,
        connection_id: ConnectionId,
        command_id: CommandId,
    ) -> Vec<BleEvent> {
        self.reserved_for_conn_mut(connection_id)
            .map(|connection| connection.take_cancellation_public_events(command_id))
            .unwrap_or_default()
    }

    // Caller results and reservation retirement.

    delegate::delegate! {
        to |connection_id: ConnectionId| self.reserved_for_conn(connection_id)? {
            #[call(waiter_result)]
            pub(in crate::runtime) fn waiter_result_for_conn(
                &self,
                command_id: CommandId,
            ) -> Option<WaiterOutcome>;
        }
    }

    /// Call only after settling affected waiters, including after late callbacks.
    /// Releases completed child operations and connection roots, returning every
    /// command route that can now be removed from the coordinator.
    pub(in crate::runtime) fn retire_completed(&mut self) -> Vec<CommandId> {
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

    // Private lookup; entry variants also retain the peripheral key.

    /// Admission must have validated this peripheral before installing a child command.
    fn reserved_for_peripheral_mut(
        &mut self,
        peripheral_id: PeripheralId,
    ) -> &mut ConnectionReservation {
        self.by_peripheral
            .get_mut(&peripheral_id)
            .expect("validated connection")
    }

    fn reserved_for_conn(&self, connection_id: ConnectionId) -> Option<&ConnectionReservation> {
        self.reserved_entry_for_conn(connection_id)
            .map(|(_, connection)| connection)
    }

    fn reserved_for_conn_mut(
        &mut self,
        connection_id: ConnectionId,
    ) -> Option<&mut ConnectionReservation> {
        self.reserved_entry_for_conn_mut(connection_id)
            .map(|(_, connection)| connection)
    }

    // Entry lookups retain the peripheral key for validation results or root removal.
    fn reserved_entry_for_conn(
        &self,
        connection_id: ConnectionId,
    ) -> Option<(PeripheralId, &ConnectionReservation)> {
        self.by_peripheral
            .iter()
            .find(|(_, connection)| connection.matches_connection_id(connection_id))
            .map(|(peripheral_id, connection)| (*peripheral_id, connection))
    }

    fn reserved_entry_for_conn_mut(
        &mut self,
        connection_id: ConnectionId,
    ) -> Option<(PeripheralId, &mut ConnectionReservation)> {
        self.by_peripheral
            .iter_mut()
            .find(|(_, connection)| connection.matches_connection_id(connection_id))
            .map(|(peripheral_id, connection)| (*peripheral_id, connection))
    }

    /// Only checks existence; operation-specific admission remains on the reservation.
    fn require_entry_for_conn(
        &self,
        connection_id: ConnectionId,
    ) -> Result<(PeripheralId, &ConnectionReservation), WaiterError> {
        self.reserved_entry_for_conn(connection_id)
            .ok_or(WaiterError::ConnectionNotActive)
    }

    fn reserved_for_cmd_mut(
        &mut self,
        command_id: CommandId,
    ) -> Option<&mut ConnectionReservation> {
        self.by_peripheral
            .values_mut()
            .find(|connection| connection.owns_command(command_id))
    }

    fn connection_id_for_event(event: &BleEvent) -> Option<ConnectionId> {
        match event {
            BleEvent::ConnectionStateChanged { connection_id, .. }
            | BleEvent::RssiRead { connection_id, .. }
            | BleEvent::CharacteristicRead { connection_id, .. }
            | BleEvent::CharacteristicWritten { connection_id, .. }
            | BleEvent::NotificationStateChanged { connection_id, .. }
            | BleEvent::NotificationReceived { connection_id, .. } => Some(*connection_id),
            BleEvent::GattDiscovered { database, .. } => Some(database.connection_id),
            _ => None,
        }
    }
}
