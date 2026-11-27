//! Actor root: serialized orchestration, reservation admission, and caller settlement.
//!
//! # Ownership and responsibilities
//!
//! ```text
//! RuntimeOwner (private Tokio runtime, actor handle, public_event_rx, shutdown_result_rx)
//! +-- BlengActor (serialized orchestration state)
//! |   +-- public_event_tx -> bounded BLE event FIFO -> RuntimeOwner::next_event()
//! |   +-- waiters: CommandId -> expectation + oneshot result_tx
//! |   +-- test-only direct-waiter allocator (release builds omit it)
//! |   +-- resource_id_gen: independent scan/connection/GATT/subscription ID allocation
//! |   +-- reservations: ReservationCoordinator (route-directed reservation coordination)
//! |       +-- initialization: private NotStarted / Pending / Ready / Rejected phases
//! |       +-- scan: private Idle / Reserved phases (one reusable scan slot)
//! |       +-- connections: ConnectionReservations (private peripheral-keyed map)
//! |           +-- connect/disconnect correlation
//! |           +-- GATT: committed database + pending discovery
//! |           +-- read/write/RSSI operation slots
//! |           +-- subscriptions: SubscriptionId -> route + pending transition
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
//! The actor publishes BLE events through public_event_tx, records correlated evidence,
//! settles eligible waiters, then retires completed native reservations.
//! RuntimeOwner owns public_event_rx; concurrent next_event() calls serialize on it
//! and divide one FIFO stream rather than receiving broadcast copies. Mailbox
//! or event-FIFO overflow faults the lifecycle instead of silently losing work.
//!
//! # Channel roles
//!
//! - public_event_tx / public_event_rx: the shared bounded BleEvent stream exposed by next_event().
//! - result_tx / result_rx: one command's exactly-once waiter result, not an event stream.
//! - phase_tx / phase_rx: lifecycle wake-ups; the atomic RuntimePhase is authoritative.
//! - cleanup_signal_tx / cleanup_signal_rx: cleanup wake-ups; locked cleanup state
//!   decides completion, not a particular signal value.
//! - shutdown_result_tx / shutdown_result_rx: the coordinator alone publishes the
//!   retained actor-shutdown/cleanup outcome; if it exits early, receiver closure
//!   wakes close callers with WorkerStopped instead of leaving them pending.
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
//! The atomic caller submission is checked before invoking a synchronous reservation
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
//! This root owns dispatch submission, event reconciliation, and coordination between
//! reservations and caller delivery. The mailbox protocol defines requests,
//! asynchronous inputs, and waiter control without a second command index.
//! Imports are explicit rather than parent globs.
//! Timeout retires only the waiter, not already-dispatched BLE work.
//! Explicit cancellation first latches the original command, waits for native control
//! acceptance, and only then settles its waiter as cancelled. A rejected control
//! request clears the latch so the original operation can continue. Late callbacks
//! still reconcile and retire the retained reservation; caller cancellation is
//! never used as a native-retirement signal.
//! Initialization becomes Ready only after dispatch acceptance, a correlated
//! snapshot, and a successful terminal; success is settled before its evidence
//! is released. Readiness is distinct from adapter power and runtime phase.

use std::{
    panic::{AssertUnwindSafe, catch_unwind},
    sync::Arc,
};

use blehub::{
    BleEvent, BleHub, CommandDispatchError, CommandId, ConnectionId, GattCharacteristicId,
    GattDescriptorId, GattSessionId, PeripheralId, ScanSessionId, SubscriptionId,
};
use kameo::prelude::{Actor, ActorRef, Context};
use tokio::sync::mpsc;

use crate::waiter::{WaiterCloseReason, WaiterError, WaiterExpectation, WaiterRegistry};

use super::{
    CommandRoute, RuntimeLifecycle, RuntimePhase,
    messages::{CancellationResolved, DispatchResolved},
    reservations::{
        CancellationDecision, CancellationReceiver, DispatchAction, GattAccess,
        PublicationDecision, ReservationCoordinator, is_connection_lost,
    },
    shared::{IdGenerator, OperationSubmission},
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

/// Single serialized owner of BLeng orchestration state.
///
/// Native callbacks arrive through the actor mailbox. Reservations record the
/// correlation state needed to reconcile dispatch, domain, terminal, and late
/// events without exposing runtime state through UniFFI.
#[derive(Actor)]
pub(crate) struct BlengActor {
    // Accessed only by publish_event(); process_event() uses that policy.
    // RuntimeOwner owns public_event_rx; public Bleng::next_event() reads through it.
    // This is neither backend ingress nor the per-command waiter result channel.
    // try_send is non-blocking; a full or closed queue faults the lifecycle.
    public_event_tx: mpsc::Sender<BleEvent>,
    // Runtime-internal shared capability, not exposed lifecycle/cleanup storage.
    // Dispatch tasks must clone it; actor failure closes this same gate and
    // starts cleanup even if the actor mailbox is unusable.
    lifecycle: Arc<RuntimeLifecycle>,
    // Caller delivery only; actor confinement linearizes admission and settlement
    // without a lock. Retiring a waiter does not retire its native reservation.
    waiters: WaiterRegistry,
    // Direct actor tests register waiters without RuntimeOwner submission.
    // This per-instance allocator is compiled out of production builds.
    #[cfg(test)]
    test_command_id_gen: Arc<IdGenerator>,
    resource_id_gen: IdGenerator,
    // ReservationCoordinator owns the domain roots and route-directed coordination;
    // waiter settlement remains here because it combines reservations with waiters.
    reservations: ReservationCoordinator,
}

impl BlengActor {
    pub(super) fn with_ids(
        public_event_tx: mpsc::Sender<BleEvent>,
        lifecycle: Arc<RuntimeLifecycle>,
        _command_id_gen: Arc<IdGenerator>,
    ) -> Self {
        Self {
            public_event_tx,
            lifecycle,
            waiters: WaiterRegistry::new(),
            #[cfg(test)]
            test_command_id_gen: _command_id_gen,
            resource_id_gen: IdGenerator::default(),
            reservations: ReservationCoordinator::default(),
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


        to self.reservations {
            pub(super) fn require_initialized(&self) -> Result<(), WaiterError>;

            pub(super) fn require_not_started(&self) -> Result<(), WaiterError>;

            pub(super) fn require_adapter_available(&self) -> Result<(), WaiterError>;

            pub(super) fn require_devices_query_available(&self) -> Result<(), WaiterError>;

            pub(super) fn require_companion_association_available(&self) -> Result<(), WaiterError>;


            pub(super) fn require_peripheral_available(&self, peripheral_id: PeripheralId) -> Result<(), WaiterError>;

            pub(super) fn validate_disconnect(&self, connection_id: ConnectionId) -> Result<PeripheralId, WaiterError>;

            pub(super) fn validate_gatt_discovery(&self, connection_id: ConnectionId) -> Result<PeripheralId, WaiterError>;

            pub(super) fn validate_rssi(&self, connection_id: ConnectionId) -> Result<PeripheralId, WaiterError>;

            pub(super) fn validate_mtu(&self, connection_id: ConnectionId) -> Result<PeripheralId, WaiterError>;

            pub(super) fn validate_set_connection_priority(&self, connection_id: ConnectionId) -> Result<PeripheralId, WaiterError>;

            pub(super) fn validate_create_bond(&self, connection_id: ConnectionId) -> Result<PeripheralId, WaiterError>;

            pub(super) fn validate_remove_bond(&self, connection_id: ConnectionId) -> Result<PeripheralId, WaiterError>;

            pub(super) fn validate_refresh_gatt_cache(&self, connection_id: ConnectionId) -> Result<PeripheralId, WaiterError>;

            pub(super) fn validate_descriptor_access(
                &self,
                connection_id: ConnectionId,
                session_id: GattSessionId,
                characteristic_id: GattCharacteristicId,
                descriptor_id: GattDescriptorId,
            ) -> Result<PeripheralId, WaiterError>;

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


            pub(super) fn require_scan_available(&self) -> Result<(), WaiterError>;

            pub(super) fn validate_scan_stop(&self, session_id: ScanSessionId) -> Result<(), WaiterError>;


        }
    }

    pub(super) fn register_operation_waiter<R>(
        &mut self,
        submission: &OperationSubmission<R>,
        expectation: WaiterExpectation,
    ) -> Result<CommandId, WaiterError> {
        if !submission.state.is_cancelled() {
            self.waiters.register_sender(
                submission.id,
                expectation,
                submission.state.result_tx.clone(),
            )?;
            self.reservations
                .register_command(submission.id, CommandRoute::from(expectation));
        }
        Ok(submission.id)
    }

    /// Acquires the dispatch root before the atomic cancel-versus-claim point.
    /// Preparation failure settles the waiter without installing a reservation.
    /// After claim, reservation installation is synchronous/infallible and precedes
    /// native dispatch; no await or normal-error path may intervene.
    pub(super) fn launch_dispatch<R: kameo::Reply, Resource>(
        &mut self,
        actor_context: &Context<Self, R>,
        submission: &OperationSubmission<Resource>,
        command_id: CommandId,
        reserve: impl FnOnce(&mut ReservationCoordinator),
        dispatch: impl FnOnce(Arc<BleHub>) -> Result<(), CommandDispatchError> + Send + 'static,
    ) -> Result<(), WaiterError> {
        let hub = match self.lifecycle.hub_for_dispatch() {
            Ok(hub) => hub,
            Err(error) => {
                // Do not publish before rejection wins the submission CAS.
                // The shared sender remains available to cancellation/resolve.
                self.waiters.discard(command_id);
                self.reservations.remove_commands([command_id]);
                return Err(error);
            }
        };
        // CAS is the cancellation-versus-dispatch linearization point. Only
        // explicit cancellation can revoke an unclaimed eager request.
        if !submission.try_claim() {
            self.waiters.cancel(command_id);
            self.reservations.remove_commands([command_id]);
            return Err(WaiterError::Cancelled);
        }
        reserve(&mut self.reservations);
        let completion = self.reservations.attach_completion_observer(command_id);
        submission.observe_completion(completion);
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

    /// Starts native cancellation without conflating it with caller waiter
    /// retirement. The caller waits for the native control receipt; the waiter is
    /// settled only after that receipt succeeds, while the reservation remains
    /// until native terminal evidence permits retirement.
    pub(super) fn request_operation_cancellation(
        &mut self,
        command_id: CommandId,
        actor: ActorRef<Self>,
    ) -> Result<CancellationReceiver, WaiterError> {
        self.require_open()?;
        let decision = self
            .reservations
            .request_cancellation(command_id)
            .ok_or(WaiterError::OperationNotFound(command_id))?;
        match decision {
            CancellationDecision::Accepted(receiver)
            | CancellationDecision::AlreadyRequested(receiver) => {
                if self
                    .reservations
                    .try_claim_cancellation_dispatch(command_id)
                {
                    self.dispatch_native_cancellation(command_id, actor);
                }
                Ok(receiver)
            }
            CancellationDecision::AlreadyCompleted => {
                Err(WaiterError::OperationAlreadyCompleted(command_id))
            }
            CancellationDecision::NotCancellable => {
                Err(WaiterError::OperationNotCancellable(command_id))
            }
        }
    }

    /// Native cancellation is a detached control-plane call. Its return value
    /// confirms only that the backend accepted the control request; the original
    /// reservation still waits for its own callback/recovery evidence.
    fn dispatch_native_cancellation(&mut self, command_id: CommandId, actor: ActorRef<Self>) {
        let hub = match self.lifecycle.hub_for_dispatch() {
            Ok(hub) => hub,
            Err(error) => {
                self.handle_cancellation_result(command_id, Err(error));
                self.lifecycle.fault();
                return;
            }
        };
        let lifecycle = Arc::clone(&self.lifecycle);
        tokio::task::spawn_blocking(move || {
            let result = match catch_unwind(AssertUnwindSafe(|| hub.cancel_operation(command_id))) {
                Ok(result) => result.map_err(WaiterError::from_cancellation_dispatch),
                Err(_) => {
                    lifecycle.fault();
                    Err(WaiterError::TransportFault)
                }
            };
            if actor
                .tell(CancellationResolved {
                    id: command_id,
                    result,
                })
                .try_send()
                .is_err()
                && lifecycle.is_open()
            {
                lifecycle.fault();
            }
        });
    }

    pub(super) fn handle_cancellation_result(
        &mut self,
        command_id: CommandId,
        result: Result<(), WaiterError>,
    ) {
        let public_events = self
            .reservations
            .resolve_cancellation_and_release_public_events(command_id, result.clone());
        if let Some(completion) = self.reservations.command_completion(command_id) {
            completion.resolve_cancellation_outcome(result.is_ok());
        }
        // Evidence was already recorded. Publish only, preserving arrival order;
        // replaying process_event here would settle/reconcile the same callback twice.
        for event in public_events {
            if self.publish_event(&event).is_err() {
                return;
            }
        }
        if result.is_ok() {
            self.waiters.cancel(command_id);
        } else {
            self.reservations.try_clear_cancellation(command_id);
            self.settle_waiter_if_ready(command_id);
        }
        // Native completion may already be recorded while its control receipt
        // was pending. Settle caller delivery before releasing that evidence.
        self.advance_and_retire_reservations();
    }

    /// Reconciles one backend callback without exposing reservation state to handlers.
    pub(super) fn handle_ingress_event(&mut self, event: BleEvent) {
        if !self.lifecycle.is_open() {
            return;
        }
        // Foreign-command disconnects remain visible publicly, but must neither
        // enter another command's dispatch buffer nor mutate its native lifecycle.
        // Its own connection's publication order still applies.
        if self.reservations.is_foreign_disconnect(&event) {
            self.process_event(event);
            return;
        }
        match self.reservations.gate_event(event) {
            Ok(Some(event)) => self.process_event(event),
            Ok(None) => {}
            Err(fault) => {
                tracing::error!(
                    ?fault,
                    "BLE event failed dispatch evidence reconciliation; closing runtime"
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
        actor: ActorRef<Self>,
    ) {
        // Dispatch reconciliation may release the reservation; retain only its
        // existing completion link long enough to publish authoritative rejection.
        let completion = self.reservations.command_completion(command_id);
        let cancellation_requested =
            result.is_err() && self.reservations.is_cancellation_requested(command_id);
        if cancellation_requested {
            // A rejected native dispatch never became cancellable. Complete the
            // cancellation receipt before resolution can remove the reservation.
            self.handle_cancellation_result(command_id, Ok(()));
        }
        let resolution = self.reservations.resolve_dispatch(command_id, &result);
        if result.is_ok()
            && self
                .reservations
                .try_claim_cancellation_dispatch(command_id)
        {
            self.dispatch_native_cancellation(command_id, actor);
        }
        match resolution.action {
            DispatchAction::NoAction => {}
            DispatchAction::StartCleanup => self.lifecycle.start_cleanup(),
            DispatchAction::FaultRuntime(fault) => {
                tracing::error!(
                    ?fault,
                    command_id = command_id.value,
                    "BLE dispatch result contradicted reserved event evidence; closing runtime"
                );
                self.lifecycle.fault();
            }
        }
        if let Err(error) = result
            && !cancellation_requested
        {
            // Rejection wins before replayed passive loss can affect this waiter.
            if let Some(completion) = completion {
                completion.record_outcome(false, false);
            }
            self.waiters
                .finish(command_id, Err(WaiterError::DispatchRejected(error)));
        }
        for event in resolution.events_to_replay {
            if !self.lifecycle.is_open() {
                break;
            }
            self.process_event(event);
        }
        self.advance_and_retire_reservations();
    }

    fn process_event(&mut self, event: BleEvent) {
        match self.reservations.decide_publication(&event) {
            Ok(PublicationDecision::Publish) => {
                if self.publish_event(&event).is_err() {
                    return;
                }
            }
            Ok(PublicationDecision::Defer | PublicationDecision::Suppress) => {}
            Err(fault) => {
                tracing::error!(
                    ?fault,
                    "BLE public evidence overflowed pending cancellation; closing runtime"
                );
                self.lifecycle.fault();
                return;
            }
        }
        // Publication cannot be retracted if admission raced concurrent shutdown.
        if !self.lifecycle.is_open() || self.reservations.is_foreign_disconnect(&event) {
            return;
        }
        if let Err(fault) = self.reservations.record_event_evidence(&event) {
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
        self.advance_and_retire_reservations();
    }

    /// Settle caller delivery before retiring native reservations, including for late callbacks.
    fn settle_event_waiters(&mut self, event: &BleEvent) {
        if let BleEvent::ConnectionStateChanged { connection_id, .. } = event
            && is_connection_lost(event, *connection_id)
        {
            // Dispatch-pending callbacks stay buffered until dispatch resolves.
            // Completed results survive in the waiter owner; all unfinished
            // connection-bound waiters still fail immediately, even during cancel.
            let command_ids = self.reservations.accepted_command_ids(*connection_id);
            let disconnect_command = self.reservations.disconnect_command(*connection_id);
            for command_id in &command_ids {
                // Explicit disconnect still requires its own terminal. A completed
                // native result awaiting cancellation must keep its deferred outcome.
                if Some(*command_id) == disconnect_command {
                    continue;
                }
                if let Some(completion) = self.reservations.command_completion(*command_id)
                    && !completion.is_completed()
                {
                    completion.record_outcome(false, false);
                }
            }
            self.waiters.disconnect_commands(command_ids);
            // Passive loss may complete an explicit disconnect's domain evidence,
            // but its own terminal is still required for successful settlement.
            if let Some(command_id) = self.reservations.disconnect_command(*connection_id) {
                self.settle_waiter_if_ready(command_id);
            }
        }
        if let Some(command_id) = event.command_id() {
            self.settle_waiter_if_ready(command_id);
        }
        if matches!(event, BleEvent::ScanStopped { .. })
            && let Some(command_id) = self.reservations.stop_command()
        {
            self.settle_waiter_if_ready(command_id);
        }
    }

    /// Advances and retires reservations after waiter settlement.
    fn advance_and_retire_reservations(&mut self) {
        if self.reservations.advance_and_retire().is_err() {
            // Accepted initialization failure invalidates even a caller-retired Hub.
            self.lifecycle.fault();
        }
    }

    /// Checks admission and enqueues one outward event before evidence processing.
    /// Success means local enqueue only; process_event() rechecks the lifecycle before
    /// processing evidence. Queue failure faults the lifecycle.
    fn publish_event(&self, event: &BleEvent) -> Result<(), WaiterError> {
        self.require_open()?;
        if self.public_event_tx.try_send(event.clone()).is_err() {
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
    fn settle_waiter_if_ready(&mut self, command_id: CommandId) {
        let has_waiter = self.waiters.contains(command_id);
        let cancellation_requested = self.reservations.is_cancellation_requested(command_id);
        if !cancellation_requested && self.waiters.try_finish_deferred(command_id) {
            return;
        }
        // Direct actor tests may install reservations without launch_dispatch;
        // production submissions always have the directory-owned observer.
        let completion = self
            .reservations
            .command_completion(command_id)
            .or_else(|| self.reservations.completion_observer(command_id));
        if !has_waiter
            && completion
                .as_ref()
                .is_none_or(|observer| observer.is_completed())
        {
            return;
        }
        if let Some(result) = self.reservations.waiter_result(command_id) {
            if let Some(completion) = completion {
                completion.confirm_completed();
                // This summary survives caller pruning but must not freeze a
                // native success while cancellation still owns the final result.
                completion.record_outcome(result.is_ok(), cancellation_requested);
            }
            // Control owns delivery while pending or accepted, but not the
            // authoritative native completion observation retained by the handle.
            if has_waiter {
                if cancellation_requested {
                    self.waiters.defer_result(command_id, result);
                } else {
                    self.waiters.finish(command_id, result);
                }
            }
        }
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
