use std::collections::HashMap;

use blehub::{
    BleEvent, BleEventError, CommandDispatchError, CommandId, ConnectionId, ConnectionState,
    ConnectionStateResult, GattCharacteristicId, GattSessionId, NotificationModeRequest,
    SubscriptionId,
};

use crate::waiter::{WaiterError, WaiterOutcome, WaiterSuccess};

use super::super::command::ConnectionOperationKind;

use super::{
    BufferedEventDisposition, CancellationDecision, CommandProgress, DispatchAction,
    DispatchResolution, GattAccess, PublicationDecision, PublicationGate, PublicationRelease,
    ReconciliationFault, ScopedPublication,
    gatt::GattReservation,
    io::{IoReservations, MAX_PENDING_NATIVE_OPERATIONS_PER_CONNECTION},
    subscription::{SubscriptionDisposition, SubscriptionReservation},
};

/// A connection owns its GATT session, I/O slots, and subscriptions.
/// Keeping these under one peripheral key prevents cross-device callback routing.
pub(super) struct ConnectionReservation {
    progress: CommandProgress,
    // Publication order and its 64-event cap belong to the whole connection.
    publication: ScopedPublication,
    connection_id: ConnectionId,
    connected_evidence: bool,
    disconnected_evidence: bool,
    // Command-correlated connection failure; this is domain evidence for connect,
    // not the terminal result and not passive disconnect evidence.
    connect_error_evidence: Option<BleEventError>,
    disconnect: Option<DisconnectReservation>,
    gatt: GattReservation,
    io: IoReservations,
    subscriptions: HashMap<SubscriptionId, SubscriptionReservation>,
}

impl ConnectionReservation {
    /// Selects a command awaiting dispatch resolution in the requested category.
    /// Related events include terminals and connection loss, not just success data;
    /// recording checks the domain variant separately. Categories can overlap.
    /// Each reservation combines dispatch timing and correlation; this method
    /// selects a target within the category chosen by the collection's global order.
    /// None means no pending buffer target, not that this connection is unrelated.
    fn pending_dispatch_target(
        &mut self,
        kind: ConnectionOperationKind,
        event: &BleEvent,
    ) -> Option<&mut CommandProgress> {
        let connection_id = self.connection_id;
        match kind {
            ConnectionOperationKind::Disconnect => self
                .disconnect
                .as_mut()
                .filter(|slot| slot.should_buffer_event(event, connection_id))
                .map(|slot| &mut slot.progress),
            ConnectionOperationKind::DiscoverGatt => self
                .gatt
                .discovery
                .as_mut()
                .filter(|slot| slot.should_buffer_event(event, connection_id))
                .map(|slot| &mut slot.progress),
            ConnectionOperationKind::ReadRssi => self
                .io
                .pending_rssi_mut(event, connection_id)
                .map(|slot| &mut slot.progress),
            ConnectionOperationKind::Read => self
                .io
                .pending_read_mut(event, connection_id)
                .map(|slot| &mut slot.progress),
            ConnectionOperationKind::Write => self
                .io
                .pending_write_mut(event, connection_id)
                .map(|slot| &mut slot.progress),
            ConnectionOperationKind::EnableNotifications
            | ConnectionOperationKind::DisableNotifications => self
                .subscriptions
                .iter_mut()
                .find_map(|(subscription_id, route)| {
                    if route.should_buffer_event(event, connection_id, *subscription_id) {
                        route.progress_mut()
                    } else {
                        None
                    }
                }),
            ConnectionOperationKind::Connect => {
                if self.should_buffer_connect_event(event) {
                    Some(&mut self.progress)
                } else {
                    None
                }
            }
        }
    }

    /// Buffers one event while dispatch is pending when this connection owns its route.
    /// The collection chooses category priority; this object owns the actual
    /// progress mutation and therefore does not expose it to the collection.
    pub(super) fn buffer_pending_dispatch_event(
        &mut self,
        kind: ConnectionOperationKind,
        event: &BleEvent,
    ) -> Result<bool, ReconciliationFault> {
        let Some(progress) = self.pending_dispatch_target(kind, event) else {
            return Ok(false);
        };
        progress.buffer_pre_dispatch_event(event.clone())?;
        Ok(true)
    }

    /// Buffer broadly before acceptance, but record only exact connection/command
    /// evidence after replay. Collection-wide operation priority stays with the caller.
    fn should_buffer_connect_event(&self, event: &BleEvent) -> bool {
        if !self.progress.is_awaiting_dispatch_result() {
            return false;
        }
        match event {
            BleEvent::ConnectionStateChanged {
                command_id,
                connection_id,
                ..
            } => {
                *connection_id == self.connection_id
                    || command_id.is_some_and(|id| self.progress.owns_command(id))
            }
            BleEvent::CommandCompleted { command_id, .. } => {
                self.progress.owns_command(*command_id)
            }
            _ => false,
        }
    }

    pub(super) fn record_event(&mut self, event: &BleEvent) -> Result<(), ReconciliationFault> {
        let connection_id = self.connection_id;
        match event {
            BleEvent::ConnectionStateChanged {
                command_id, result, ..
            } => {
                match (command_id, result) {
                    (
                        Some(command_id),
                        ConnectionStateResult::Ok {
                            state: ConnectionState::Connected,
                        },
                    ) if self.progress.owns_command(*command_id) => {
                        self.connected_evidence = true;
                    }
                    (Some(command_id), ConnectionStateResult::Err { error })
                        if self.progress.owns_command(*command_id) =>
                    {
                        self.connect_error_evidence = Some(error.clone());
                    }
                    (None, ConnectionStateResult::Err { .. })
                    | (
                        None,
                        ConnectionStateResult::Ok {
                            state: ConnectionState::Disconnected,
                        },
                    ) => {
                        self.disconnected_evidence = true;
                    }
                    (
                        Some(command_id),
                        ConnectionStateResult::Ok {
                            state: ConnectionState::Disconnected,
                        },
                    ) if self.progress.owns_command(*command_id)
                        || self
                            .disconnect
                            .as_ref()
                            .is_some_and(|slot| slot.owns_command(*command_id)) =>
                    {
                        self.disconnected_evidence = true;
                    }
                    _ => {}
                }
                if let Some(slot) = self.disconnect.as_mut() {
                    // Passive errors prove teardown but do not replace the
                    // explicit disconnect command's authoritative terminal.
                    slot.record_event(event, connection_id);
                }
            }
            BleEvent::GattDiscovered { .. } => {
                if let Some(discovery) = self.gatt.discovery.as_mut() {
                    // Discovery deliberately keeps the latest correlated draft.
                    discovery.record_event(event, connection_id);
                }
            }
            BleEvent::RssiRead { .. }
            | BleEvent::CharacteristicRead { .. }
            | BleEvent::CharacteristicWritten { .. } => {
                self.io.record_event(event, connection_id);
            }
            BleEvent::NotificationStateChanged {
                subscription_id, ..
            } => {
                if let Some(route) = self.subscriptions.get_mut(subscription_id) {
                    route.record_event(event, connection_id, *subscription_id)?;
                }
            }
            _ => {}
        }
        Ok(())
    }

    pub(super) fn resolve_dispatch(
        &mut self,
        command_id: CommandId,
        result: &Result<(), CommandDispatchError>,
    ) -> Option<DispatchResolution> {
        let connection_id = self.connection_id;
        // Taking the discovery slot never removes its separately committed database.
        if let Some(resolution) = resolve_pending_slot(
            &mut self.gatt.discovery,
            command_id,
            result,
            connection_id,
            |slot| &mut slot.progress,
        ) {
            return Some(resolution);
        }
        if self.io.rssi_mut(command_id).is_some() {
            let resolution = resolve_pending_reservation(
                self.io
                    .rssi_mut(command_id)
                    .expect("matching RSSI reservation"),
                command_id,
                result,
                connection_id,
                |slot| &mut slot.progress,
            );
            if result.is_err() {
                self.io.remove_rssi(command_id);
            }
            return resolution;
        }
        if self.io.read_mut(command_id).is_some() {
            let resolution = resolve_pending_reservation(
                self.io
                    .read_mut(command_id)
                    .expect("matching read reservation"),
                command_id,
                result,
                connection_id,
                |slot| &mut slot.progress,
            );
            if result.is_err() {
                self.io.remove_read(command_id);
            }
            return resolution;
        }
        if self.io.write_mut(command_id).is_some() {
            let resolution = resolve_pending_reservation(
                self.io
                    .write_mut(command_id)
                    .expect("matching write reservation"),
                command_id,
                result,
                connection_id,
                |slot| &mut slot.progress,
            );
            if result.is_err() {
                self.io.remove_write(command_id);
            }
            return resolution;
        }
        if let Some(subscription_id) =
            self.subscriptions
                .iter()
                .find_map(|(subscription_id, route)| {
                    route
                        .progress()
                        .filter(|progress| progress.owns_command(command_id))
                        .map(|_| *subscription_id)
                })
        {
            let progress = self
                .subscriptions
                .get_mut(&subscription_id)
                .and_then(SubscriptionReservation::progress_mut)
                .expect("matching transition exists");
            return Some(if result.is_err() {
                // Subscription rejection also permits commanded connection loss.
                let resolution = progress.rejected_resolution(DispatchAction::NoAction, |event| {
                    if connection_lost(event, connection_id) {
                        BufferedEventDisposition::Replay
                    } else {
                        BufferedEventDisposition::Conflict
                    }
                });
                self.reject_subscription_transition(subscription_id);
                resolution
            } else {
                progress.accepted_resolution()
            });
        }
        if let Some(resolution) = resolve_pending_slot(
            &mut self.disconnect,
            command_id,
            result,
            connection_id,
            |slot| &mut slot.progress,
        ) {
            return Some(resolution);
        }
        if !self.owns_root_command(command_id) {
            return None;
        }
        Some(if result.is_err() {
            self.progress
                .rejected_resolution(DispatchAction::NoAction, |_| {
                    BufferedEventDisposition::Conflict
                })
        } else {
            self.progress.accepted_resolution()
        })
    }

    pub(super) fn owns_root_command(&self, command_id: CommandId) -> bool {
        self.progress.owns_command(command_id)
    }

    pub(super) fn progress(&self, command_id: CommandId) -> Option<&CommandProgress> {
        self.command_progresses()
            .find(|progress| progress.owns_command(command_id))
    }

    pub(super) fn decide_publication(
        &mut self,
        event: &BleEvent,
    ) -> Result<PublicationDecision, ReconciliationFault> {
        let cancellation_accepted = event
            .command_id()
            .and_then(|id| self.progress(id))
            .is_some_and(CommandProgress::is_cancellation_accepted);
        let gate = PublicationGate {
            cancellation_accepted_for_event: cancellation_accepted,
            cancellation_pending: self
                .command_progresses()
                .any(CommandProgress::is_awaiting_cancellation_result),
        };
        self.publication.decide(event, gate)
    }

    pub(super) fn take_cancellation_public_events(
        &mut self,
        command_id: CommandId,
    ) -> Vec<BleEvent> {
        let Some(accepted) = self
            .progress(command_id)
            .map(CommandProgress::is_cancellation_accepted)
        else {
            return Vec::new();
        };
        let pending = self
            .command_progresses()
            .any(CommandProgress::is_awaiting_cancellation_result);
        self.publication.resolve(
            command_id,
            PublicationRelease {
                cancellation_accepted: accepted,
                another_cancellation_pending: pending,
            },
        )
    }

    pub(super) fn contains_command(&self, command_id: CommandId) -> bool {
        self.command_progresses()
            .any(|dispatch| dispatch.owns_command(command_id))
    }

    /// Requests caller cancellation for a cancellable operation owned by this
    /// connection. Disconnect is deliberately control-plane-only here: it is
    /// the teardown authority and cannot be superseded by its own cancellation.
    pub(super) fn request_cancellation(
        &mut self,
        command_id: CommandId,
    ) -> Option<CancellationDecision> {
        if self.progress.owns_command(command_id) {
            return Some(self.progress.request_cancellation());
        }
        if let Some(slot) = self
            .disconnect
            .as_ref()
            .filter(|slot| slot.owns_command(command_id))
        {
            return Some(if slot.is_complete() {
                CancellationDecision::AlreadyCompleted
            } else {
                CancellationDecision::NotCancellable
            });
        }
        self.cancellable_progresses_mut()
            .find(|progress| progress.owns_command(command_id))
            .map(CommandProgress::request_cancellation)
    }

    pub(super) fn try_claim_cancellation_dispatch(&mut self, command_id: CommandId) -> bool {
        self.command_progresses_mut()
            .find(|progress| progress.owns_command(command_id))
            .is_some_and(CommandProgress::try_claim_cancellation_dispatch)
    }

    pub(super) fn try_resolve_cancellation(
        &mut self,
        command_id: CommandId,
        result: Result<(), WaiterError>,
    ) -> bool {
        let Some(progress) = self
            .command_progresses_mut()
            .find(|progress| progress.owns_command(command_id))
        else {
            return false;
        };
        progress.resolve_cancellation(result);
        true
    }

    pub(super) fn try_clear_cancellation(&mut self, command_id: CommandId) -> bool {
        let Some(progress) = self
            .command_progresses_mut()
            .find(|progress| progress.owns_command(command_id))
        else {
            return false;
        };
        progress.clear_cancellation();
        true
    }

    pub(super) fn is_cancellation_requested(&self, command_id: CommandId) -> bool {
        self.command_progresses()
            .find(|progress| progress.owns_command(command_id))
            .is_some_and(CommandProgress::is_cancellation_requested)
    }

    fn require_connected(&self) -> Result<(), WaiterError> {
        if !self.connected_evidence || self.disconnected_evidence || self.has_failed() {
            return Err(WaiterError::ConnectionNotActive);
        }
        Ok(())
    }

    fn has_pending_child_handoff(&self) -> bool {
        self.child_progresses()
            .any(CommandProgress::is_awaiting_dispatch_result)
    }

    fn pending_native_operation_count(&self) -> usize {
        self.io.len()
            + self
                .subscriptions
                .values()
                .filter(|route| route.is_transitioning())
                .count()
    }

    fn require_native_operation_capacity(&self) -> Result<(), WaiterError> {
        if self.pending_native_operation_count() >= MAX_PENDING_NATIVE_OPERATIONS_PER_CONNECTION {
            return Err(WaiterError::OperationBusy);
        }
        Ok(())
    }

    fn has_subscription_transition_for(&self, characteristic_id: GattCharacteristicId) -> bool {
        self.subscriptions
            .values()
            .any(|route| route.is_transitioning() && route.characteristic_id == characteristic_id)
    }

    pub(super) fn validate_disconnect(&self) -> Result<(), WaiterError> {
        // An accepted connect owns a native lifecycle even before Connected arrives.
        if self.disconnected_evidence
            || self.has_failed()
            || self.progress.is_awaiting_dispatch_result()
        {
            return Err(WaiterError::ConnectionNotActive);
        }
        if self.disconnect.is_some() || self.has_pending_child_handoff() {
            // Pre-dispatch siblings must learn acceptance before passive loss.
            return Err(WaiterError::OperationBusy);
        }
        Ok(())
    }

    pub(super) fn validate_gatt_discovery(&self) -> Result<(), WaiterError> {
        self.require_connected()?;
        if self.disconnect.is_some()
            || self.io.is_rssi_awaiting_dispatch()
            || self.gatt.has_pending_discovery()
            || self.io.len() != 0
            || !self.subscriptions.is_empty()
        {
            return Err(WaiterError::GattBusy);
        }
        Ok(())
    }

    pub(super) fn validate_rssi(&self) -> Result<(), WaiterError> {
        self.require_connected()?;
        if self.disconnect.is_some() || self.has_pending_child_handoff() {
            return Err(WaiterError::OperationBusy);
        }
        self.require_native_operation_capacity()
    }

    /// Keep error precedence explicit: connection, submission, target, I/O, discovery.
    pub(super) fn validate_gatt_access(
        &self,
        session_id: GattSessionId,
        characteristic_id: GattCharacteristicId,
        access: GattAccess,
    ) -> Result<(), WaiterError> {
        self.require_connected()?;
        if self.disconnect.is_some() || self.has_pending_child_handoff() {
            return Err(WaiterError::OperationBusy);
        }
        let database = self
            .gatt
            .committed_database(session_id)
            .ok_or(WaiterError::GattNotReady)?;
        if !database.services.iter().any(|service| {
            service
                .characteristics
                .iter()
                .any(|characteristic| characteristic.id == characteristic_id)
        }) {
            return Err(WaiterError::GattNotReady);
        }
        if self.has_subscription_transition_for(characteristic_id)
            || (matches!(access, GattAccess::Read)
                && self
                    .subscriptions
                    .values()
                    .any(|route| route.characteristic_id == characteristic_id))
            || (matches!(access, GattAccess::EnableNotifications)
                && self.io.has_characteristic_operation_for(characteristic_id))
        {
            return Err(WaiterError::OperationBusy);
        }
        if self.gatt.has_pending_discovery() {
            return Err(WaiterError::GattBusy);
        }
        // Enable keeps its existing precedence: pending discovery before duplicate route.
        if matches!(access, GattAccess::EnableNotifications)
            && self
                .subscriptions
                .values()
                .any(|route| route.characteristic_id == characteristic_id)
        {
            return Err(WaiterError::OperationBusy);
        }
        self.require_native_operation_capacity()
    }

    pub(super) fn validate_notification_disable(
        &self,
        session_id: GattSessionId,
        subscription_id: SubscriptionId,
    ) -> Result<GattCharacteristicId, WaiterError> {
        // Subscription identity/activity precedes connection and sibling-busy checks.
        let route = self
            .subscriptions
            .get(&subscription_id)
            .filter(|route| route.session_id == session_id && route.is_active())
            .ok_or(WaiterError::SubscriptionNotActive)?;
        if route.is_transitioning() {
            return Err(WaiterError::OperationBusy);
        }
        self.require_connected()?;
        if self.disconnect.is_some()
            || self.gatt.has_pending_discovery()
            || self.has_pending_child_handoff()
        {
            return Err(WaiterError::OperationBusy);
        }
        self.require_native_operation_capacity()?;
        Ok(route.characteristic_id)
    }

    pub(super) fn new(command_id: CommandId, connection_id: ConnectionId) -> Self {
        Self {
            progress: CommandProgress::new(command_id),
            publication: ScopedPublication::default(),
            connection_id,
            connected_evidence: false,
            disconnected_evidence: false,
            connect_error_evidence: None,
            disconnect: None,
            gatt: GattReservation::default(),
            io: IoReservations::default(),
            subscriptions: HashMap::new(),
        }
    }

    pub(super) fn matches_connection_id(&self, connection_id: ConnectionId) -> bool {
        self.connection_id == connection_id
    }

    pub(super) fn reserve_disconnect(&mut self, command_id: CommandId) {
        self.disconnect = Some(DisconnectReservation::new(command_id));
    }

    pub(super) fn reserve_gatt_discovery(
        &mut self,
        command_id: CommandId,
        session_id: GattSessionId,
    ) {
        self.gatt.begin_discovery(command_id, session_id);
    }

    pub(super) fn reserve_rssi(&mut self, command_id: CommandId) {
        self.io.reserve_rssi(command_id);
    }

    pub(super) fn reserve_read(
        &mut self,
        command_id: CommandId,
        session_id: GattSessionId,
        characteristic_id: GattCharacteristicId,
    ) {
        self.io
            .reserve_read(command_id, session_id, characteristic_id);
    }

    pub(super) fn reserve_write(
        &mut self,
        command_id: CommandId,
        session_id: GattSessionId,
        characteristic_id: GattCharacteristicId,
    ) {
        self.io
            .reserve_write(command_id, session_id, characteristic_id);
    }

    pub(super) fn reserve_notification_enable(
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

    pub(super) fn reserve_notification_disable(
        &mut self,
        command_id: CommandId,
        subscription_id: SubscriptionId,
    ) {
        self.subscriptions
            .get_mut(&subscription_id)
            .expect("validated subscription")
            .begin_disable(command_id);
    }

    pub(super) fn is_foreign_disconnect(&self, command_id: CommandId) -> bool {
        !self.owns_root_command(command_id)
            && !self
                .disconnect
                .as_ref()
                .is_some_and(|slot| slot.owns_command(command_id))
    }

    pub(super) fn disconnect_command(&self) -> Option<CommandId> {
        self.disconnect
            .as_ref()
            .map(DisconnectReservation::command_id)
    }

    /// Rejected disable restores Active; rejected enable removes its provisional route.
    fn reject_subscription_transition(&mut self, subscription_id: SubscriptionId) {
        let route = self
            .subscriptions
            .get_mut(&subscription_id)
            .expect("matching subscription exists");
        if matches!(route.reject_transition(), SubscriptionDisposition::Remove) {
            self.subscriptions.remove(&subscription_id);
        }
    }

    fn has_failed(&self) -> bool {
        self.connect_error_evidence.is_some()
    }

    /// Called after waiter settlement, even when the caller already retired.
    /// Returns every child command whose native reservation was released.
    pub(super) fn retire_completed_operations(&mut self) -> Vec<CommandId> {
        let mut retired = Vec::new();
        self.subscriptions.retain(|_, route| {
            let command_id = route.progress().map(CommandProgress::command_id);
            let keep = matches!(route.retire_transition(), SubscriptionDisposition::Keep);
            if !keep && let Some(command_id) = command_id {
                retired.push(command_id);
            }
            keep
        });
        if self
            .disconnect
            .as_ref()
            .is_some_and(DisconnectReservation::can_retire_after_failure)
        {
            retired.push(
                self.disconnect
                    .as_ref()
                    .expect("disconnect reservation exists")
                    .progress
                    .command_id(),
            );
            self.disconnect = None;
        }
        retired.extend(self.io.retire_completed());
        if let Some(command_id) = self.gatt.retire_completed_discovery() {
            retired.push(command_id);
        }
        retired
    }

    /// A disconnected connection cannot be removed until dispatch handoffs and
    /// cancellation receipts resolve; an explicit disconnect needs its terminal/domain.
    pub(super) fn is_retirable(&self) -> bool {
        let disconnect_complete = self
            .disconnect
            .as_ref()
            .is_none_or(DisconnectReservation::is_complete);
        disconnect_complete
            && self.publication.is_empty()
            && !self.command_progresses().any(|progress| {
                progress.is_awaiting_dispatch_result() || progress.is_awaiting_cancellation_result()
            })
            && (self.disconnected_evidence
                || self.has_failed()
                || self.progress.is_terminal_failed())
    }

    fn connect_result(&self) -> Option<WaiterOutcome> {
        if self.progress.is_awaiting_dispatch_result() {
            return None;
        }
        if let Some(error) = &self.connect_error_evidence {
            return Some(Err(WaiterError::Operation(error.clone())));
        }
        if self.disconnected_evidence {
            return Some(Err(WaiterError::Disconnected));
        }
        if let Err(error) = self.progress.terminal_result()? {
            return Some(Err(error));
        }
        self.connected_evidence
            .then_some(Ok(WaiterSuccess::Connected(self.connection_id)))
    }

    /// Queries without consuming evidence; None means unowned or incomplete.
    /// Waiter settlement must precede any retirement that releases this evidence.
    pub(super) fn waiter_result(&self, command_id: CommandId) -> Option<WaiterOutcome> {
        if self.progress.owns_command(command_id) {
            return self.connect_result();
        }
        if let Some(disconnect) = self
            .disconnect
            .as_ref()
            .filter(|disconnect| disconnect.owns_command(command_id))
        {
            return disconnect.waiter_result(self.connection_id);
        }
        if let Some(gatt) = self
            .gatt
            .discovery
            .as_ref()
            .filter(|gatt| gatt.progress.owns_command(command_id))
        {
            return gatt.waiter_result();
        }
        if let Some(result) = self.io.waiter_result(command_id, self.connection_id) {
            return Some(result);
        }
        self.subscriptions
            .iter()
            .find_map(|(subscription_id, route)| {
                route.waiter_result(command_id, self.connection_id, *subscription_id)
            })
    }

    /// Returns command ownership, including duplicates, not operation success.
    /// The owning progress preserves the first terminal.
    pub(super) fn record_terminal(
        &mut self,
        command_id: CommandId,
        result: &Result<(), BleEventError>,
    ) -> bool {
        if let Some(progress) = self
            .command_progresses_mut()
            .find(|progress| progress.owns_command(command_id))
        {
            progress.record_terminal(result);
            return true;
        }
        false
    }

    pub(super) fn accepted_command_ids(&self) -> Vec<CommandId> {
        self.command_progresses()
            .filter(|dispatch| !dispatch.is_awaiting_dispatch_result())
            .map(CommandProgress::command_id)
            .collect()
    }

    /// One inventory of child commands supports admission and lifecycle accounting.
    fn child_progresses(&self) -> impl Iterator<Item = &CommandProgress> {
        [
            self.disconnect.as_ref().map(|slot| &slot.progress),
            self.gatt.discovery.as_ref().map(|slot| &slot.progress),
        ]
        .into_iter()
        .flatten()
        .chain(self.io.progresses())
        .chain(
            self.subscriptions
                .values()
                .filter_map(SubscriptionReservation::progress),
        )
    }

    pub(super) fn command_ids(&self) -> impl Iterator<Item = CommandId> + '_ {
        self.command_progresses().map(CommandProgress::command_id)
    }

    fn command_progresses(&self) -> impl Iterator<Item = &CommandProgress> {
        std::iter::once(&self.progress).chain(self.child_progresses())
    }

    fn cancellable_progresses_mut(&mut self) -> impl Iterator<Item = &mut CommandProgress> {
        [self.gatt.discovery.as_mut().map(|slot| &mut slot.progress)]
            .into_iter()
            .flatten()
            .chain(self.io.progresses_mut())
            .chain(
                self.subscriptions
                    .values_mut()
                    .filter_map(SubscriptionReservation::progress_mut),
            )
    }

    fn command_progresses_mut(&mut self) -> impl Iterator<Item = &mut CommandProgress> {
        [
            Some(&mut self.progress),
            self.disconnect.as_mut().map(|slot| &mut slot.progress),
            self.gatt.discovery.as_mut().map(|slot| &mut slot.progress),
        ]
        .into_iter()
        .flatten()
        .chain(self.io.progresses_mut())
        .chain(
            self.subscriptions
                .values_mut()
                .filter_map(SubscriptionReservation::progress_mut),
        )
    }
}

struct DisconnectReservation {
    progress: CommandProgress,
    disconnect_evidence: bool,
}

impl DisconnectReservation {
    fn new(command_id: CommandId) -> Self {
        Self {
            progress: CommandProgress::new(command_id),
            disconnect_evidence: false,
        }
    }

    fn command_id(&self) -> CommandId {
        self.progress.command_id()
    }

    fn owns_command(&self, command_id: CommandId) -> bool {
        self.progress.owns_command(command_id)
    }

    /// Records correlated disconnect evidence for this explicit operation.
    /// Passive failure and command-correlated disconnect both prove the domain
    /// side of completion.
    fn record_event(&mut self, event: &BleEvent, expected: ConnectionId) {
        if self.matches_related_event(event, expected) {
            self.disconnect_evidence = true;
        }
    }

    /// A complete disconnect has accepted dispatch, terminal evidence, and
    /// matching native disconnect evidence.
    fn is_complete(&self) -> bool {
        !self.progress.is_awaiting_dispatch_result()
            && self.progress.has_terminal()
            && self.disconnect_evidence
    }

    /// A failed accepted terminal releases only this child operation; the
    /// connection root may still be alive and allow a retry.
    fn can_retire_after_failure(&self) -> bool {
        !self.progress.is_awaiting_dispatch_result() && self.progress.is_terminal_failed()
    }

    /// Dispatch timing applies only to buffering, not domain evidence recording.
    fn should_buffer_event(&self, event: &BleEvent, expected: ConnectionId) -> bool {
        self.progress.is_awaiting_dispatch_result() && self.matches_related_event(event, expected)
    }

    /// Matches related disconnect, terminal, and passive failure events, not success.
    fn matches_related_event(&self, event: &BleEvent, expected: ConnectionId) -> bool {
        match event {
            BleEvent::ConnectionStateChanged {
                command_id,
                connection_id,
                result:
                    ConnectionStateResult::Ok {
                        state: ConnectionState::Disconnected,
                    },
            } => {
                *connection_id == expected
                    && command_id.is_none_or(|id| self.progress.owns_command(id))
            }
            BleEvent::ConnectionStateChanged {
                command_id: None,
                connection_id,
                result: ConnectionStateResult::Err { .. },
            } => *connection_id == expected,
            BleEvent::CommandCompleted { command_id, .. } => {
                self.progress.owns_command(*command_id)
            }
            _ => false,
        }
    }

    fn waiter_result(&self, connection_id: ConnectionId) -> Option<WaiterOutcome> {
        if let Err(error) = self.progress.terminal_result()? {
            return Some(Err(error));
        }
        self.disconnect_evidence
            .then_some(Ok(WaiterSuccess::Disconnected(connection_id)))
    }
}

pub fn connection_lost(event: &BleEvent, expected: ConnectionId) -> bool {
    matches!(
        event,
        BleEvent::ConnectionStateChanged {
            connection_id,
            result: ConnectionStateResult::Ok { state: ConnectionState::Disconnected },
            ..
        } if *connection_id == expected
    ) || matches!(
        event,
        BleEvent::ConnectionStateChanged {
            command_id: None,
            connection_id,
            result: ConnectionStateResult::Err { .. },
        } if *connection_id == expected
    )
}

fn passive_connection_lost(event: &BleEvent, expected: ConnectionId) -> bool {
    matches!(
        event,
        BleEvent::ConnectionStateChanged {
            command_id: None,
            ..
        }
    ) && connection_lost(event, expected)
}

/// Resolves a keyed child reservation without owning its storage removal.
/// The caller removes rejected map entries after this borrow ends.
fn resolve_pending_reservation<T>(
    slot: &mut T,
    command_id: CommandId,
    result: &Result<(), CommandDispatchError>,
    connection_id: ConnectionId,
    progress: impl FnOnce(&mut T) -> &mut CommandProgress,
) -> Option<DispatchResolution> {
    let command = progress(slot);
    if !command.owns_command(command_id) {
        return None;
    }
    Some(if result.is_err() {
        command.rejected_resolution(DispatchAction::NoAction, |event| {
            if passive_connection_lost(event, connection_id) {
                BufferedEventDisposition::Replay
            } else {
                BufferedEventDisposition::Conflict
            }
        })
    } else {
        command.accepted_resolution()
    })
}

/// Ordinary child commands discard only their pending slot on rejection and
/// permit passive loss replay. Subscriptions deliberately use a different policy.
fn resolve_pending_slot<T>(
    slot: &mut Option<T>,
    command_id: CommandId,
    result: &Result<(), CommandDispatchError>,
    connection_id: ConnectionId,
    progress: impl FnOnce(&mut T) -> &mut CommandProgress,
) -> Option<DispatchResolution> {
    let command = progress(slot.as_mut()?);
    if !command.owns_command(command_id) {
        return None;
    }
    Some(if result.is_err() {
        let resolution = command.rejected_resolution(DispatchAction::NoAction, |event| {
            if passive_connection_lost(event, connection_id) {
                BufferedEventDisposition::Replay
            } else {
                BufferedEventDisposition::Conflict
            }
        });
        *slot = None;
        resolution
    } else {
        command.accepted_resolution()
    })
}
