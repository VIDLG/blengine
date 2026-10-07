//! Actor root: serialized orchestration, reservation admission, and caller settlement.
//!
//! # Ownership and responsibilities
//!
//! ```text
//! RuntimeOwner (private Tokio runtime, actor handle, event_rx, shutdown_result_tx)
//! +-- BlengActor (serialized orchestration state)
//! |   +-- event_tx -> bounded BLE event FIFO -> RuntimeOwner::next_event()
//! |   +-- waiters: CommandId -> expectation + oneshot result_tx
//! |   +-- command_id_gen: monotonic command ID allocation
//! |   +-- resource_id_gen: independent scan/connection/GATT/subscription ID allocation
//! |   +-- initialization: NotStarted -> Pending(InitReservation) -> Ready
//! |   +-- scan: Idle -> Reserved(ScanReservation) -> Idle
//! |   +-- connections: ConnectionReservations (private peripheral-keyed map)
//! |       +-- connect/disconnect correlation
//! |       +-- GATT: committed database + pending discovery
//! |       +-- read/write/RSSI operation slots
//! |       +-- subscriptions: SubscriptionId -> route + pending transition
//! +-- RuntimeLifecycle (also shared by actor, backend port, and dispatch tasks)
//!     +-- atomic phase + watch phase_tx
//!     +-- HubCleanup: admission, Hub ownership, disposal, completion accounting
//! ```
//!
//! The actor owns mutable operation state; shared lifecycle controls admission
//! and fault/close transitions. Waiters stay actor-local so registration and
//! settlement are serialized without another lock. Hub disposal belongs to
//! cleanup, not to a reservation or waiter destructor.
//!
//! # Event ingress is not the public event queue
//!
//! ```text
//! native backend -> BleHub ingress -> BackendPort (BleEventSink)
//!                -> IngressEvent in actor mailbox -> BlengActor
//! ```
//!
//! BackendPort implements the sink; BlengActor processes its mailbox messages.
//! The actor publishes BLE events through event_tx, records correlated evidence,
//! settles eligible waiters, then retires completed native reservations.
//! RuntimeOwner owns event_rx; concurrent next_event() calls serialize on it
//! and divide one FIFO stream rather than receiving broadcast copies. Mailbox
//! or event-FIFO overflow faults the lifecycle instead of silently losing work.
//!
//! # Channel roles
//!
//! - event_tx / event_rx: the shared bounded BleEvent stream exposed by next_event().
//! - result_tx / result_rx: one command's exactly-once waiter result, not an event stream.
//! - phase_tx / phase_rx: lifecycle wake-ups; the atomic RuntimePhase is authoritative.
//! - cleanup_signal_tx / cleanup_signal_rx: cleanup wake-ups; locked cleanup state
//!   decides completion, not a particular signal value.
//! - shutdown_result_tx / shutdown_result_rx: the retained shared outcome of actor
//!   shutdown plus native cleanup, independent of cancellation of one close caller.
//!
//! Channel endpoint variables use a semantic name plus _tx / _rx. Payloads,
//! actor handles, Notify values, and receiver type aliases are not endpoints to
//! rename by this rule. Watch notifications may coalesce; subscribers recheck
//! the authoritative phase or cleanup state instead of treating them as a log.
//!
//! # ID allocation
//!
//! Command and resource sequences are independent and start at 1. Resources
//! share a sequence across scan, connection, GATT session, and subscription IDs;
//! typed command/resource IDs may have equal numeric values. Allocation is
//! actor-confined, allows gaps after failure/cancellation, and never rolls back,
//! reuses an issued value, or wraps. Each sequence exhausts independently.
//!
//! # Cancellation before mutation
//!
//! Commands finish fallible checks and waiter registration before launch_dispatch.
//! The atomic caller handoff is checked before invoking a synchronous reservation
//! closure. Losing that race cancels only the waiter; no native reservation exists
//! to roll back. The winning path registers state before spawning native dispatch,
//! with no intervening await or normal-error return.
//!
//! # Waiters and reservations have different lifetimes
//!
//! A waiter owns caller delivery; its reservation owns native callback evidence.
//! Local evidence and transitions live together in reservations/. The actor
//! keeps its lifecycle and reservation roots private. messages.rs uses admission,
//! registration, and input-processing methods, never mutable state getters.
//! This root owns dispatch handoff, event reconciliation, and coordination between
//! reservations and caller delivery. The mailbox protocol defines requests,
//! asynchronous inputs, and waiter control without a second command index.
//! Imports are explicit rather than parent globs.
//! Timeout/cancellation retires only the waiter, not already-dispatched BLE work.
//! Late callbacks must still reconcile and retire the retained reservation.
//! Initialization becomes Ready only after dispatch acceptance, a correlated
//! snapshot, and a successful terminal; success is settled before its evidence
//! is released. Readiness is distinct from adapter power and runtime phase.

use std::{
    panic::{AssertUnwindSafe, catch_unwind},
    sync::Arc,
};

use blehub::{
    BleEvent, BleEventError, BleHub, CommandDispatchError, CommandId, ConnectionId,
    GattCharacteristicId, GattSessionId, NotificationModeRequest, PeripheralId, ScanSessionId,
    SubscriptionId,
};
use kameo::prelude::{Actor, Context};
use tokio::sync::{mpsc, oneshot};

use crate::waiter::{
    WaiterCloseReason, WaiterError, WaiterExpectation, WaiterRegistry, WaiterSuccess,
};

use super::{
    RuntimeLifecycle, RuntimePhase,
    messages::DispatchResolved,
    owner::BeginHandoff,
    reservations::{
        ConnectionReservations, DispatchEffect, DispatchResolution, GattAccess,
        InitializationState, ReconcileFault, ScanState, connection_lost,
    },
};

#[cfg(test)]
#[path = "tests/actor.rs"]
mod tests;

#[cfg(test)]
#[path = "tests/actor_support.rs"]
mod test_support;

#[cfg(test)]
pub(super) use test_support::{
    AssumeInitialized, BeginInitializeAfterCleanup, BeginInitializeWithBlockedReservation,
    CancelledConnectionLeaf, CancelledRediscovery, CheckConsumedIds, HoldActor, RegisterWaiter,
};

pub(super) type WaiterReceiver = oneshot::Receiver<Result<WaiterSuccess, WaiterError>>;

/// Actor-local, nonzero IDs. Exhaustion never wraps or reuses a value.
/// Allocation is not rolled back when subsequent admission or dispatch fails.
#[derive(Default)]
struct IdGenerator {
    last_issued: u64,
}

impl IdGenerator {
    fn allocate(&mut self) -> Result<u64, WaiterError> {
        let value = self
            .last_issued
            .checked_add(1)
            .ok_or(WaiterError::IdExhausted)?;
        self.last_issued = value;
        Ok(value)
    }
}

/// Single serialized owner of BLeng orchestration state.
///
/// Native callbacks arrive through the actor mailbox. Reservations record the
/// correlation state needed to reconcile dispatch, domain, terminal, and late
/// events without exposing runtime state through UniFFI.
#[derive(Actor)]
pub(super) struct BlengActor {
    // Accessed only by publish_event(); process_event() uses that policy.
    // RuntimeOwner owns event_rx; public Bleng::next_event() reads through it.
    // This is neither backend ingress nor the per-command waiter result channel.
    // try_send is non-blocking; a full or closed queue faults the lifecycle.
    event_tx: mpsc::Sender<BleEvent>,
    // Runtime-internal shared capability, not exposed lifecycle/cleanup storage.
    // Dispatch tasks must clone it; actor failure closes this same gate and
    // starts cleanup even if the actor mailbox is unusable.
    lifecycle: Arc<RuntimeLifecycle>,
    // Caller delivery only; actor confinement linearizes admission and settlement
    // without a lock. Retiring a waiter does not retire its native reservation.
    waiters: WaiterRegistry,
    // Independent namespaces: equal command/resource values are valid.
    // Gaps are allowed; issued IDs are never rolled back, reused, or wrapped.
    command_id_gen: IdGenerator,
    resource_id_gen: IdGenerator,
    // Only actor methods mutate these roots. Mailbox handlers use admission
    // and input-processing methods, never state getters.
    // Readiness is independent of adapter power; Pending survives waiter retirement.
    initialization: InitializationState,
    // At most one scan session, retained while late start/stop evidence is unresolved.
    scan: ScanState,
    // One connection lifecycle per peripheral; each owns its GATT/I/O/subscriptions.
    connections: ConnectionReservations,
}

impl BlengActor {
    pub(super) fn new(event_tx: mpsc::Sender<BleEvent>, lifecycle: Arc<RuntimeLifecycle>) -> Self {
        Self {
            event_tx,
            lifecycle,
            waiters: WaiterRegistry::new(),
            command_id_gen: IdGenerator::default(),
            resource_id_gen: IdGenerator::default(),
            initialization: InitializationState::default(),
            scan: ScanState::default(),
            connections: ConnectionReservations::default(),
        }
    }

    // Pure forwarding, grouped by owner; policy and coordination stay explicit below.
    delegate::delegate! {
        to self.lifecycle {
            pub(super) fn require_open(&self) -> Result<(), WaiterError>;
        }

        to self.resource_id_gen {
            #[call(allocate)]
            pub(super) fn allocate_resource_id(&mut self) -> Result<u64, WaiterError>;
        }


        to self.initialization {
            pub(super) fn require_initialized(&self) -> Result<(), WaiterError>;

            pub(super) fn require_not_started(&self) -> Result<(), WaiterError>;

            pub(super) fn reserve_initialize(&mut self, command_id: CommandId);
        }

        to self.connections {
            pub(super) fn require_peripheral_available(&self, peripheral_id: PeripheralId) -> Result<(), WaiterError>;

            pub(super) fn validate_disconnect(&self, connection_id: ConnectionId) -> Result<PeripheralId, WaiterError>;

            pub(super) fn validate_gatt_discovery(&self, connection_id: ConnectionId) -> Result<PeripheralId, WaiterError>;

            pub(super) fn validate_rssi(&self, connection_id: ConnectionId) -> Result<PeripheralId, WaiterError>;

            pub(super) fn validate_gatt_access(
                &self,
                connection_id: ConnectionId,
                session_id: GattSessionId,
                characteristic_id: GattCharacteristicId,
                access: GattAccess,
            ) -> Result<PeripheralId, WaiterError>;

            pub(super) fn validate_notification_enable(
                &self,
                connection_id: ConnectionId,
                session_id: GattSessionId,
                characteristic_id: GattCharacteristicId,
            ) -> Result<PeripheralId, WaiterError>;

            pub(super) fn validate_notification_disable(
                &self,
                connection_id: ConnectionId,
                session_id: GattSessionId,
                subscription_id: SubscriptionId,
            ) -> Result<(PeripheralId, GattCharacteristicId), WaiterError>;

            pub(super) fn reserve_connection(
                &mut self,
                peripheral_id: PeripheralId,
                command_id: CommandId,
                connection_id: ConnectionId,
            );

            pub(super) fn reserve_disconnect(&mut self, peripheral_id: PeripheralId, command_id: CommandId);

            pub(super) fn reserve_gatt_discovery(
                &mut self,
                peripheral_id: PeripheralId,
                command_id: CommandId,
                session_id: GattSessionId,
            );

            pub(super) fn reserve_rssi(&mut self, peripheral_id: PeripheralId, command_id: CommandId);

            pub(super) fn reserve_read(
                &mut self,
                peripheral_id: PeripheralId,
                command_id: CommandId,
                session_id: GattSessionId,
                characteristic_id: GattCharacteristicId,
            );

            pub(super) fn reserve_write(
                &mut self,
                peripheral_id: PeripheralId,
                command_id: CommandId,
                session_id: GattSessionId,
                characteristic_id: GattCharacteristicId,
            );

            pub(super) fn reserve_notification_enable(
                &mut self,
                peripheral_id: PeripheralId,
                command_id: CommandId,
                subscription_id: SubscriptionId,
                session_id: GattSessionId,
                characteristic_id: GattCharacteristicId,
                mode: NotificationModeRequest,
            );

            pub(super) fn reserve_notification_disable(
                &mut self,
                peripheral_id: PeripheralId,
                command_id: CommandId,
                subscription_id: SubscriptionId,
            );
        }

        to self.scan {
            pub(super) fn require_scan_available(&self) -> Result<(), WaiterError>;

            pub(super) fn validate_scan_stop(&self, session_id: ScanSessionId) -> Result<(), WaiterError>;

            pub(super) fn reserve_scan_start(&mut self, command_id: CommandId, session_id: ScanSessionId);

            pub(super) fn reserve_scan_stop(&mut self, command_id: CommandId);
        }
    }

    /// Allocates a command ID and registers caller delivery. Commands creating
    /// resources allocate their typed resource ID separately, before this call.
    /// Registration failure may leave a gap; no issued ID is reused.
    pub(super) fn register_waiter(
        &mut self,
        expectation: WaiterExpectation,
    ) -> Result<(CommandId, WaiterReceiver), WaiterError> {
        let id = self.command_id_gen.allocate()?.into();
        let result_rx = self.waiters.register(id, expectation)?;
        Ok((id, result_rx))
    }

    /// Acquires the dispatch root before claiming the atomic caller handoff.
    /// Preparation failure settles the waiter without installing a reservation.
    /// After handoff, registration is synchronous/infallible and precedes dispatch;
    /// no await or normal-error path may intervene. Detached work owns lifecycle.
    pub(super) fn launch_dispatch<R: kameo::Reply>(
        &mut self,
        actor_context: &Context<Self, R>,
        handoff: &BeginHandoff,
        command_id: CommandId,
        reserve: impl FnOnce(&mut Self),
        dispatch: impl FnOnce(Arc<BleHub>) -> Result<(), CommandDispatchError> + Send + 'static,
    ) -> Result<(), WaiterError> {
        let hub = match self.lifecycle.hub_for_dispatch() {
            Ok(hub) => hub,
            Err(error) => {
                self.waiters.finish(command_id, Err(error.clone()));
                return Err(error);
            }
        };
        // Claim only after fallible preparation. Claiming first would make a
        // missing Hub look like an abandoned native operation to the caller guard.
        if !handoff.try_claim() {
            self.waiters.cancel(command_id);
            return Err(WaiterError::Cancelled);
        }
        reserve(self);
        let lifecycle = Arc::clone(&self.lifecycle);
        let actor = actor_context.actor_ref().clone();
        tokio::task::spawn_blocking(move || {
            match catch_unwind(AssertUnwindSafe(|| dispatch(hub))) {
                Ok(result) => {
                    if actor
                        .tell(DispatchResolved {
                            id: command_id,
                            result,
                        })
                        .try_send()
                        .is_err()
                        && lifecycle.is_open()
                    {
                        lifecycle.fault();
                    }
                }
                Err(_) => lifecycle.fault(),
            }
        });
        Ok(())
    }

    /// Retires caller delivery only; already-dispatched native work stays reserved.
    pub(super) fn retire_waiter(&mut self, command_id: CommandId, timed_out: bool) {
        if timed_out {
            self.waiters.timeout(command_id);
        } else {
            self.waiters.cancel(command_id);
        }
    }

    /// Reconciles one backend callback without exposing reservation state to handlers.
    pub(super) fn handle_ingress_event(&mut self, event: BleEvent) {
        if !self.lifecycle.is_open() {
            return;
        }
        // Foreign-command disconnects remain visible publicly, but must neither
        // enter another command's buffer nor mutate its native lifecycle.
        if self.connections.is_foreign_disconnect(&event) {
            self.process_event(event);
            return;
        }
        match self.buffer_before_dispatch(event) {
            Ok(Some(event)) => self.process_event(event),
            Ok(None) => {}
            Err(fault) => {
                tracing::error!(
                    ?fault,
                    "BLE event could not be buffered before dispatch acknowledgement; closing runtime"
                );
                self.lifecycle.fault();
            }
        }
    }

    /// Applies native dispatch acknowledgement before replaying its buffered callbacks.
    pub(super) fn handle_dispatch_result(
        &mut self,
        command_id: CommandId,
        result: Result<(), CommandDispatchError>,
    ) {
        let resolution = self.resolve_dispatch(command_id, &result);
        match resolution.effect {
            DispatchEffect::None => {}
            DispatchEffect::Cleanup => self.lifecycle.start_cleanup(),
            DispatchEffect::Fault(fault) => {
                tracing::error!(
                    ?fault,
                    command_id = command_id.value,
                    "BLE dispatch result contradicted reserved event evidence; closing runtime"
                );
                self.lifecycle.fault();
            }
        }
        if let Err(error) = result {
            // Rejection wins before replayed passive loss can affect this waiter.
            self.waiters
                .finish(command_id, Err(WaiterError::DispatchRejected(error)));
        }
        for event in resolution.replay {
            if !self.lifecycle.is_open() {
                break;
            }
            self.process_event(event);
        }
    }

    fn process_event(&mut self, event: BleEvent) {
        if self.publish_event(&event).is_err() {
            return;
        }
        // Publication cannot be retracted if admission raced concurrent shutdown.
        if !self.lifecycle.is_open() || self.connections.is_foreign_disconnect(&event) {
            return;
        }
        if let Err(fault) = self.record_event_evidence(&event) {
            tracing::error!(
                ?fault,
                "BLE event evidence conflicted with reserved state; closing runtime"
            );
            self.lifecycle.fault();
            return;
        }
        if !self.lifecycle.is_open() {
            return;
        }
        self.settle_event_waiters(&event);
        self.advance_and_retire_state();
    }

    fn record_event_evidence(&mut self, event: &BleEvent) -> Result<(), ReconcileFault> {
        match event {
            BleEvent::AdapterSnapshotChanged { .. } => self.initialization.record_event(event),
            BleEvent::ScanStarted { .. } | BleEvent::ScanStopped { .. } => {
                self.scan.record_event(event)
            }
            BleEvent::CommandCompleted { command_id, error } => {
                self.record_terminal(*command_id, error.clone());
            }
            _ => return self.connections.record_event(event),
        }
        Ok(())
    }

    /// Settle caller delivery before retiring native reservations, including for late callbacks.
    fn settle_event_waiters(&mut self, event: &BleEvent) {
        if let BleEvent::ConnectionStateChanged { connection_id, .. } = event
            && connection_lost(event, *connection_id)
        {
            // Only accepted connection-bound commands lose their waiters here;
            // pre-ack callbacks stay buffered until dispatch resolves.
            self.waiters
                .disconnect_commands(self.connections.accepted_command_ids(*connection_id));
            // Passive loss may complete an explicit disconnect's domain evidence,
            // but its own terminal is still required for successful settlement.
            if let Some(command_id) = self.connections.disconnect_command(*connection_id) {
                self.try_settle_waiter(command_id);
            }
        }
        if let Some(command_id) = event.command_id() {
            self.try_settle_waiter(command_id);
        }
        if matches!(event, BleEvent::ScanStopped { .. })
            && let Some(command_id) = self.scan.stop_command()
        {
            self.try_settle_waiter(command_id);
        }
    }

    /// Advance initialization and retire completed native reservations after waiter settlement.
    fn advance_and_retire_state(&mut self) {
        if self.initialization.has_failed() {
            // Accepted initialization failure invalidates even a caller-retired Hub.
            self.lifecycle.fault();
            return;
        }
        self.initialization.complete_if_ready();
        self.connections.retire_completed();
        self.scan.retire_completed();
    }

    fn buffer_before_dispatch(
        &mut self,
        event: BleEvent,
    ) -> Result<Option<BleEvent>, ReconcileFault> {
        // These are ownership probes, not broadcasts. Each connection owner also
        // preserves global operation-category priority within its collection.
        let Some(event) = self.initialization.buffer_before_dispatch(event)? else {
            return Ok(None);
        };
        let Some(event) = self.connections.buffer_before_dispatch(event)? else {
            return Ok(None);
        };
        self.scan.buffer_before_dispatch(event)
    }

    fn resolve_dispatch(
        &mut self,
        command_id: CommandId,
        result: &Result<(), CommandDispatchError>,
    ) -> DispatchResolution {
        // Unique command IDs permit stopping at the first owning reservation.
        if let Some(resolution) = self.initialization.resolve_dispatch(command_id, result) {
            return resolution;
        }
        if let Some(resolution) = self.scan.resolve_dispatch(command_id, result) {
            return resolution;
        }
        self.connections
            .resolve_dispatch(command_id, result)
            .unwrap_or_default()
    }

    /// Checks admission and enqueues one outward event before evidence processing.
    /// Success means local enqueue only; process_event() rechecks the lifecycle before
    /// processing evidence. Queue failure faults the lifecycle.
    fn publish_event(&self, event: &BleEvent) -> Result<(), WaiterError> {
        self.require_open()?;
        if self.event_tx.try_send(event.clone()).is_err() {
            self.lifecycle.fault();
            return Err(WaiterError::TransportFault);
        }
        Ok(())
    }

    /// Settles a caller waiter from the authoritative reservation state.
    ///
    /// The reservation may outlive the waiter after timeout or cancellation;
    /// this method is therefore intentionally a no-op when no result is ready
    /// or when the waiter has already retired.
    fn try_settle_waiter(&mut self, command_id: CommandId) {
        // Avoid materializing reservation results after caller delivery has retired.
        if !self.waiters.contains(command_id) {
            return;
        }
        if let Some(result) = self.waiter_result(command_id) {
            self.waiters.finish(command_id, result);
        }
    }

    /// Routes a terminal callback to the reservation owning `command_id`.
    /// Command IDs are unique within this actor. Stop at the first matching owner,
    /// including for duplicate terminals; a match does not mean operation success.
    fn record_terminal(&mut self, command_id: CommandId, error: Option<BleEventError>) {
        let result = error.map_or(Ok(()), Err);
        if self.initialization.record_terminal(command_id, &result) {
            return;
        }
        if self.scan.record_terminal(command_id, &result) {
            return;
        }
        self.connections.record_terminal(command_id, &result);
    }

    /// Returns the first ready outcome, whether success or failure. `None` means
    /// no outcome is ready, either because the ID is absent or evidence is incomplete.
    fn waiter_result(&self, command_id: CommandId) -> Option<Result<WaiterSuccess, WaiterError>> {
        if let Some(result) = self.initialization.waiter_result(command_id) {
            return Some(result);
        }
        if let Some(result) = self.scan.waiter_result(command_id) {
            return Some(result);
        }
        self.connections.waiter_result(command_id)
    }
}

impl Drop for BlengActor {
    fn drop(&mut self) {
        // Runs for every actor teardown, not only failures. RuntimeOwner::close()
        // closes the gate before stop_gracefully() and waits for actor shutdown
        // plus cleanup. RuntimeOwner::Drop also closes it before the kill fallback.
        // Fault-driven teardown already has a Faulted gate; preserve that reason.
        if self.lifecycle.is_open() {
            // Only an actor that stops while admission is still open is unexpected.
            // Retire its Hub rather than leave live native state or stranded waiters.
            self.lifecycle.fault();
        }
        // Normal teardown also releases/fails every remaining caller waiter.
        let reason = if self.lifecycle.phase() == RuntimePhase::Faulted {
            WaiterCloseReason::TransportFault
        } else {
            WaiterCloseReason::Closed
        };
        self.waiters.close(reason);
    }
}
