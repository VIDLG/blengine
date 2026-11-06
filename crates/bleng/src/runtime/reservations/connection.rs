use blehub::{
    BleEvent, BleEventError, CommandDispatchError, CommandId, ConnectionId, ConnectionState,
    ConnectionStateResult, GattCharacteristicId, GattSessionId, NotificationModeRequest,
    SubscriptionId,
};

use crate::waiter::{WaiterError, WaiterOutcome, WaiterSuccess};

use super::{
    BufferedEventDisposition, CancellationDecision, CommandProgress, DispatchAction,
    DispatchResolution, GattAccess, PublicationDecision, PublicationGate, PublicationRelease,
    ReconciliationFault, ScopedPublication, gatt::GattReservation, io::IoReservations,
    is_connection_lost, subscriptions::SubscriptionReservations,
};

/// Shared admission bound for retained RSSI/read/write commands and subscription transitions.
/// Stable routes, discovery, and connection lifecycle commands are not part of this budget.
const MAX_PENDING_NATIVE_IO_OPERATIONS_PER_CONNECTION: usize = 1_024;

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
    subscriptions: SubscriptionReservations,
}

impl ConnectionReservation {
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
            subscriptions: SubscriptionReservations::default(),
        }
    }

    // Identity and command queries.

    pub(super) fn matches_connection_id(&self, connection_id: ConnectionId) -> bool {
        self.connection_id == connection_id
    }

    delegate::delegate! {
        to self.progress {
            #[call(owns_command)]
            pub(super) fn owns_root_command(&self, command_id: CommandId) -> bool;
        }
    }

    pub(super) fn owns_command(&self, command_id: CommandId) -> bool {
        self.progress_for_cmd(command_id).is_some()
    }

    pub(super) fn is_foreign_disconnect(&self, command_id: CommandId) -> bool {
        !self.owns_root_command(command_id)
            && !self
                .disconnect
                .as_ref()
                .is_some_and(|slot| slot.owns_command(command_id))
    }

    /// Point lookups use the I/O command maps; inventory iteration is reserved for
    /// connection-wide admission, publication barriers, and lifecycle accounting.
    pub(super) fn progress_for_cmd(&self, command_id: CommandId) -> Option<&CommandProgress> {
        if self.progress.owns_command(command_id) {
            return Some(&self.progress);
        }
        if let Some(disconnect) = &self.disconnect
            && disconnect.owns_command(command_id)
        {
            return Some(&disconnect.progress);
        }
        if let Some(progress) = self.gatt.progress()
            && progress.owns_command(command_id)
        {
            return Some(progress);
        }
        if let Some(progress) = self.io.progress_for_cmd(command_id) {
            return Some(progress);
        }
        self.subscriptions.progress_for_cmd(command_id)
    }

    pub(super) fn disconnect_command(&self) -> Option<CommandId> {
        self.disconnect
            .as_ref()
            .map(DisconnectReservation::command_id)
    }

    pub(super) fn command_ids(&self) -> impl Iterator<Item = CommandId> + '_ {
        self.command_progresses().map(CommandProgress::command_id)
    }

    pub(super) fn accepted_command_ids(&self) -> Vec<CommandId> {
        self.command_progresses()
            .filter(|dispatch| !dispatch.is_awaiting_dispatch_result())
            .map(CommandProgress::command_id)
            .collect()
    }

    // Admission.

    pub(super) fn validate_disconnect(&self) -> Result<(), WaiterError> {
        // An accepted, unfinished connect can be disconnected before Connected arrives.
        // A failed root retained for cancellation/publication is no longer active.
        if self.disconnected_evidence
            || self.has_connect_error()
            || self.progress.has_failed_terminal()
            || self.progress.is_awaiting_dispatch_result()
        {
            return Err(WaiterError::ConnectionNotActive);
        }
        if self.disconnect.is_some() || self.has_child_awaiting_dispatch_result() {
            // Pre-dispatch siblings must learn acceptance before passive loss.
            return Err(WaiterError::OperationBusy);
        }
        Ok(())
    }

    pub(super) fn validate_gatt_discovery(&self) -> Result<(), WaiterError> {
        self.require_connected()?;
        if self.disconnect.is_some()
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
        if self.disconnect.is_some() || self.has_child_awaiting_dispatch_result() {
            return Err(WaiterError::OperationBusy);
        }
        self.require_native_io_operation_capacity()
    }

    /// Keep error precedence explicit: connection, submission, target, I/O, discovery.
    pub(super) fn validate_gatt_access(
        &self,
        session_id: GattSessionId,
        characteristic_id: GattCharacteristicId,
        access: GattAccess,
    ) -> Result<(), WaiterError> {
        self.require_connected()?;
        if self.disconnect.is_some() || self.has_child_awaiting_dispatch_result() {
            return Err(WaiterError::OperationBusy);
        }
        self.gatt
            .require_characteristic(session_id, characteristic_id)?;
        self.subscriptions
            .validate_characteristic_access(characteristic_id, access)?;
        if matches!(access, GattAccess::EnableNotifications)
            && self.io.has_characteristic_operation_for(characteristic_id)
        {
            return Err(WaiterError::OperationBusy);
        }
        if self.gatt.has_pending_discovery() {
            return Err(WaiterError::GattBusy);
        }
        // Enable keeps its existing precedence: pending discovery before duplicate route.
        if matches!(access, GattAccess::EnableNotifications)
            && self.subscriptions.has_subscription_for(characteristic_id)
        {
            return Err(WaiterError::OperationBusy);
        }
        self.require_native_io_operation_capacity()
    }

    pub(super) fn validate_notification_disable(
        &self,
        session_id: GattSessionId,
        subscription_id: SubscriptionId,
    ) -> Result<GattCharacteristicId, WaiterError> {
        // Subscription identity/activity precedes connection and sibling-busy checks.
        let characteristic_id = self
            .subscriptions
            .validate_disable(session_id, subscription_id)?;
        self.require_connected()?;
        if self.disconnect.is_some()
            || self.gatt.has_pending_discovery()
            || self.has_child_awaiting_dispatch_result()
        {
            return Err(WaiterError::OperationBusy);
        }
        self.require_native_io_operation_capacity()?;
        Ok(characteristic_id)
    }

    // Installation after admission and submission claim.

    pub(super) fn reserve_disconnect(&mut self, command_id: CommandId) {
        self.disconnect = Some(DisconnectReservation::new(command_id));
    }

    delegate::delegate! {
        to self.gatt {
            #[call(reserve_discovery)]
            pub(super) fn reserve_gatt_discovery(
                &mut self,
                command_id: CommandId,
                session_id: GattSessionId,
            );
        }
        to self.io {
            pub(super) fn reserve_rssi(&mut self, command_id: CommandId);
            pub(super) fn reserve_read(
                &mut self,
                command_id: CommandId,
                session_id: GattSessionId,
                characteristic_id: GattCharacteristicId,
            );
            pub(super) fn reserve_write(
                &mut self,
                command_id: CommandId,
                session_id: GattSessionId,
                characteristic_id: GattCharacteristicId,
            );
        }
        to self.subscriptions {
            #[call(reserve_enable)]
            pub(super) fn reserve_notification_enable(
                &mut self,
                command_id: CommandId,
                subscription_id: SubscriptionId,
                session_id: GattSessionId,
                characteristic_id: GattCharacteristicId,
                mode: NotificationModeRequest,
            );
            #[call(reserve_disable)]
            pub(super) fn reserve_notification_disable(
                &mut self,
                command_id: CommandId,
                subscription_id: SubscriptionId,
            );
        }
    }

    // Dispatch gating and evidence.

    /// Buffers only behind a dispatch gate relevant to this event.
    /// Ok(true) means retained once; Ok(false) means no applicable pending gate.
    /// A matching gate's overflow is an error, not permission to try another owner.
    pub(super) fn try_buffer_dispatch_pending_event(
        &mut self,
        event: &BleEvent,
    ) -> Result<bool, ReconciliationFault> {
        // State changes need ordered scope-wide gating rather than a single-owner lookup.
        if matches!(event, BleEvent::ConnectionStateChanged { .. }) {
            return self.try_buffer_connection_state_event(event);
        }

        let connection_id = self.connection_id;
        let progress = match event {
            // Terminals select one command; domain callbacks select their owner,
            // which validates full identity before returning a pending progress.
            BleEvent::CommandCompleted { command_id, .. } => self
                .progress_for_cmd_mut(*command_id)
                .filter(|progress| progress.is_awaiting_dispatch_result()),
            BleEvent::GattDiscovered { .. } => self
                .gatt
                .pending_progress_for_event_mut(event, connection_id),
            BleEvent::RssiRead { .. } => self
                .io
                .pending_rssi_progress_for_event_mut(event, connection_id),
            BleEvent::CharacteristicRead { .. } => self
                .io
                .pending_read_progress_for_event_mut(event, connection_id),
            BleEvent::CharacteristicWritten { .. } => self
                .io
                .pending_write_progress_for_event_mut(event, connection_id),
            BleEvent::NotificationStateChanged { .. } | BleEvent::NotificationReceived { .. } => {
                self.subscriptions
                    .pending_progress_for_event_mut(event, connection_id)
            }
            _ => None,
        };
        let Some(progress) = progress else {
            return Ok(false);
        };
        progress.buffer_dispatch_pending_event(event.clone())?;
        Ok(true)
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
                    ) if !self.is_foreign_disconnect(*command_id) => {
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
                self.gatt.record_event(event, connection_id);
            }
            BleEvent::RssiRead { .. }
            | BleEvent::CharacteristicRead { .. }
            | BleEvent::CharacteristicWritten { .. } => {
                self.io.record_event(event, connection_id);
            }
            BleEvent::NotificationStateChanged { .. } => {
                self.subscriptions.record_event(event, connection_id)?;
            }
            _ => {}
        }
        Ok(())
    }

    /// Returns command ownership, including duplicates, not operation success.
    /// The owning progress preserves the first terminal.
    pub(super) fn try_record_terminal(
        &mut self,
        command_id: CommandId,
        result: &Result<(), BleEventError>,
    ) -> bool {
        if let Some(progress) = self.progress_for_cmd_mut(command_id) {
            progress.record_terminal(result);
            return true;
        }
        false
    }

    pub(super) fn resolve_dispatch(
        &mut self,
        command_id: CommandId,
        result: &Result<(), CommandDispatchError>,
    ) -> Option<DispatchResolution> {
        let connection_id = self.connection_id;
        if let Some(resolution) = self.gatt.resolve_dispatch(command_id, result, |event| {
            classify_rejected_child_event(event, connection_id)
        }) {
            return Some(resolution);
        }
        if let Some(resolution) = self.io.resolve_dispatch(command_id, result, |event| {
            classify_rejected_child_event(event, connection_id)
        }) {
            return Some(resolution);
        }
        if let Some(resolution) =
            self.subscriptions
                .resolve_dispatch(command_id, result, connection_id)
        {
            return Some(resolution);
        }
        if let Some(disconnect) = self
            .disconnect
            .as_mut()
            .filter(|disconnect| disconnect.owns_command(command_id))
        {
            let resolution = disconnect.resolve_dispatch(result, connection_id);
            if result.is_err() {
                self.disconnect = None;
            }
            return Some(resolution);
        }
        if !self.owns_root_command(command_id) {
            return None;
        }
        if result.is_ok() {
            return Some(self.progress.resolve_accepted_dispatch());
        }
        Some(
            self.progress
                .resolve_rejected_dispatch(DispatchAction::NoAction, |_| {
                    BufferedEventDisposition::Conflict
                }),
        )
    }

    // Cancellation.

    /// Requests caller cancellation for a cancellable operation owned by this
    /// connection. Disconnect is deliberately control-plane-only here: it is
    /// the teardown authority and cannot be superseded by its own cancellation.
    pub(super) fn request_cancellation(
        &mut self,
        command_id: CommandId,
    ) -> Option<CancellationDecision> {
        if let Some(slot) = self
            .disconnect
            .as_ref()
            .filter(|slot| slot.owns_command(command_id))
        {
            return Some(slot.cancellation_decision());
        }
        self.progress_for_cmd_mut(command_id)
            .map(CommandProgress::request_cancellation)
    }

    pub(super) fn try_claim_cancellation_dispatch(&mut self, command_id: CommandId) -> bool {
        self.progress_for_cmd_mut(command_id)
            .is_some_and(CommandProgress::try_claim_cancellation_dispatch)
    }

    pub(super) fn try_resolve_cancellation(
        &mut self,
        command_id: CommandId,
        result: Result<(), WaiterError>,
    ) -> bool {
        let Some(progress) = self.progress_for_cmd_mut(command_id) else {
            return false;
        };
        progress.resolve_cancellation(result);
        true
    }

    pub(super) fn try_clear_cancellation(&mut self, command_id: CommandId) -> bool {
        let Some(progress) = self.progress_for_cmd_mut(command_id) else {
            return false;
        };
        progress.clear_cancellation();
        true
    }

    pub(super) fn is_cancellation_requested(&self, command_id: CommandId) -> bool {
        self.progress_for_cmd(command_id)
            .is_some_and(CommandProgress::is_cancellation_requested)
    }

    // Connection-scoped public event ordering.

    pub(super) fn decide_publication(
        &mut self,
        event: &BleEvent,
    ) -> Result<PublicationDecision, ReconciliationFault> {
        let cancellation_accepted = event
            .command_id()
            .and_then(|id| self.progress_for_cmd(id))
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
            .progress_for_cmd(command_id)
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

    // Caller results and reservation retirement.

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
        if let Some(result) = self.gatt.waiter_result(command_id) {
            return Some(result);
        }
        if let Some(result) = self.io.waiter_result(command_id, self.connection_id) {
            return Some(result);
        }
        self.subscriptions
            .waiter_result(command_id, self.connection_id)
    }

    /// Called after waiter settlement, even when the caller already retired.
    /// Returns every child command whose native reservation was released.
    pub(super) fn retire_completed_operations(&mut self) -> Vec<CommandId> {
        let mut retired = self.subscriptions.retire_completed();
        if self
            .disconnect
            .as_ref()
            .is_some_and(|disconnect| disconnect.progress.has_accepted_terminal_failure())
        {
            let disconnect = self
                .disconnect
                .take()
                .expect("disconnect reservation exists");
            retired.push(disconnect.command_id());
        }
        retired.extend(self.io.retire_completed());
        if let Some(command_id) = self.gatt.retire_completed_discovery() {
            retired.push(command_id);
        }
        retired
    }

    /// Only ended or failed connection lifecycles can retire. Publication, dispatch,
    /// cancellation, and an explicit disconnect's terminal/domain still gate removal.
    pub(super) fn is_retirable(&self) -> bool {
        let ended = self.disconnected_evidence
            || self.has_connect_error()
            || self.progress.has_failed_terminal();
        // Avoid walking retained commands while the connection lifecycle is still active.
        if !ended {
            return false;
        }
        if !self.publication.is_empty() {
            return false;
        }
        if self
            .disconnect
            .as_ref()
            .is_some_and(|disconnect| !disconnect.is_complete())
        {
            return false;
        }
        !self.command_progresses().any(|progress| {
            progress.is_awaiting_dispatch_result() || progress.is_awaiting_cancellation_result()
        })
    }

    // Private command lookup, admission helpers, and event predicates.

    fn progress_for_cmd_mut(&mut self, command_id: CommandId) -> Option<&mut CommandProgress> {
        if self.progress.owns_command(command_id) {
            return Some(&mut self.progress);
        }
        if let Some(disconnect) = &mut self.disconnect
            && disconnect.owns_command(command_id)
        {
            return Some(&mut disconnect.progress);
        }
        if let Some(progress) = self.gatt.progress_mut()
            && progress.owns_command(command_id)
        {
            return Some(progress);
        }
        if let Some(progress) = self.io.progress_for_cmd_mut(command_id) {
            return Some(progress);
        }
        self.subscriptions.progress_for_cmd_mut(command_id)
    }

    fn require_connected(&self) -> Result<(), WaiterError> {
        if !self.connected_evidence || self.disconnected_evidence || self.has_connect_error() {
            return Err(WaiterError::ConnectionNotActive);
        }
        Ok(())
    }

    fn has_child_awaiting_dispatch_result(&self) -> bool {
        self.child_progresses()
            .any(CommandProgress::is_awaiting_dispatch_result)
    }

    // This budget covers I/O and subscription transitions, not lifecycle or discovery commands.
    fn pending_native_io_operation_count(&self) -> usize {
        self.io.len() + self.subscriptions.transition_count()
    }

    fn require_native_io_operation_capacity(&self) -> Result<(), WaiterError> {
        if self.pending_native_io_operation_count()
            >= MAX_PENDING_NATIVE_IO_OPERATIONS_PER_CONNECTION
        {
            return Err(WaiterError::OperationBusy);
        }
        Ok(())
    }

    delegate::delegate! {
        to self.connect_error_evidence {
            #[call(is_some)]
            fn has_connect_error(&self) -> bool;
        }
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

    /// One inventory of child commands supports admission and lifecycle accounting.
    fn child_progresses(&self) -> impl Iterator<Item = &CommandProgress> {
        self.disconnect
            .iter()
            .map(|disconnect| &disconnect.progress)
            .chain(self.gatt.progress())
            .chain(self.io.progresses())
            .chain(self.subscriptions.progresses())
    }

    fn command_progresses(&self) -> impl Iterator<Item = &CommandProgress> {
        std::iter::once(&self.progress).chain(self.child_progresses())
    }

    /// Called only for connection-state events. The first matching child gate wins;
    /// connect is the fallback so connection loss cannot overtake child dispatch results.
    /// Buffering immediately preserves priority without probing subsequent owners.
    fn try_buffer_connection_state_event(
        &mut self,
        event: &BleEvent,
    ) -> Result<bool, ReconciliationFault> {
        let connection_id = self.connection_id;
        if let Some(disconnect) = self.disconnect.as_mut()
            && disconnect.should_buffer_event(event, connection_id)
        {
            disconnect
                .progress
                .buffer_dispatch_pending_event(event.clone())?;
            return Ok(true);
        }
        if let Some(progress) = self
            .gatt
            .pending_progress_for_event_mut(event, connection_id)
        {
            progress.buffer_dispatch_pending_event(event.clone())?;
            return Ok(true);
        }
        if let Some(progress) = self
            .io
            .pending_rssi_progress_for_event_mut(event, connection_id)
        {
            progress.buffer_dispatch_pending_event(event.clone())?;
            return Ok(true);
        }
        if let Some(progress) = self
            .io
            .pending_read_progress_for_event_mut(event, connection_id)
        {
            progress.buffer_dispatch_pending_event(event.clone())?;
            return Ok(true);
        }
        if let Some(progress) = self
            .io
            .pending_write_progress_for_event_mut(event, connection_id)
        {
            progress.buffer_dispatch_pending_event(event.clone())?;
            return Ok(true);
        }
        if let Some(progress) = self
            .subscriptions
            .pending_progress_for_event_mut(event, connection_id)
        {
            progress.buffer_dispatch_pending_event(event.clone())?;
            return Ok(true);
        }
        if !self.should_buffer_connect_state_event(event) {
            return Ok(false);
        }
        self.progress.buffer_dispatch_pending_event(event.clone())?;
        Ok(true)
    }

    /// Buffers related state callbacks before acceptance; terminals use the command gate.
    /// The affected connection's gates precede foreign connect fallback.
    fn should_buffer_connect_state_event(&self, event: &BleEvent) -> bool {
        if !self.progress.is_awaiting_dispatch_result() {
            return false;
        }
        matches!(
            event,
            BleEvent::ConnectionStateChanged {
                command_id,
                connection_id,
                ..
            } if *connection_id == self.connection_id
                || command_id.is_some_and(|id| self.progress.owns_command(id))
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

    delegate::delegate! {
        to self.progress {
            fn command_id(&self) -> CommandId;
            fn owns_command(&self, command_id: CommandId) -> bool;
        }
    }

    /// Disconnect is not cancellable; completed evidence only changes the reported decision.
    fn cancellation_decision(&self) -> CancellationDecision {
        if self.is_complete() {
            CancellationDecision::AlreadyCompleted
        } else {
            CancellationDecision::NotCancellable
        }
    }

    /// Dispatch timing applies only to buffering, not domain evidence recording.
    fn should_buffer_event(&self, event: &BleEvent, expected: ConnectionId) -> bool {
        self.progress.is_awaiting_dispatch_result() && self.matches_related_event(event, expected)
    }

    /// Resolves this disconnect's evidence; the parent owns slot removal on rejection.
    fn resolve_dispatch(
        &mut self,
        result: &Result<(), CommandDispatchError>,
        connection_id: ConnectionId,
    ) -> DispatchResolution {
        if result.is_ok() {
            return self.progress.resolve_accepted_dispatch();
        }
        self.progress
            .resolve_rejected_dispatch(DispatchAction::NoAction, |event| {
                classify_rejected_child_event(event, connection_id)
            })
    }

    /// Records correlated disconnect evidence for this explicit operation.
    /// Passive failure and command-correlated disconnect both prove the domain
    /// side of completion.
    fn record_event(&mut self, event: &BleEvent, expected: ConnectionId) {
        if self.matches_related_event(event, expected) {
            self.disconnect_evidence = true;
        }
    }

    fn waiter_result(&self, connection_id: ConnectionId) -> Option<WaiterOutcome> {
        if let Err(error) = self.progress.terminal_result()? {
            return Some(Err(error));
        }
        self.disconnect_evidence
            .then_some(Ok(WaiterSuccess::Disconnected(connection_id)))
    }

    /// A complete disconnect has accepted dispatch, terminal evidence, and
    /// matching native disconnect evidence.
    fn is_complete(&self) -> bool {
        !self.progress.is_awaiting_dispatch_result()
            && self.progress.has_terminal()
            && self.disconnect_evidence
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
}

/// Ordinary child rejection permits only independent, passive connection loss.
/// I/O maps and fixed discovery/disconnect slots share this connection-level policy;
/// subscriptions deliberately retain their broader commanded-loss policy.
fn classify_rejected_child_event(
    event: &BleEvent,
    connection_id: ConnectionId,
) -> BufferedEventDisposition {
    if matches!(
        event,
        BleEvent::ConnectionStateChanged {
            command_id: None,
            ..
        }
    ) && is_connection_lost(event, connection_id)
    {
        BufferedEventDisposition::Replay
    } else {
        BufferedEventDisposition::Conflict
    }
}
