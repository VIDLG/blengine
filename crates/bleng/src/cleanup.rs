//! Hub ownership admission and asynchronous disposal coordination.
//!
//! # Five-step execution model
//!
//! 1. Register under the lock: establish identity, ownership, and a result slot.
//! 2. Schedule outside the lock: hand a disposal target to its worker.
//! 3. Execute in the background: run native dispose/drop without the state lock.
//! 4. Account under the lock: store success/failure before notifying waiters.
//! 5. Complete from state: require cleanup started, admission released, and all
//!    slots settled. A notification wakes a check; it never proves completion.
//!
//! These rules separate short state transitions from potentially blocking or
//! reentrant work; asynchronous execution must not mean untracked ownership.
//!
//! # Core invariants
//!
//! - Only the primary Hub is dispatchable. Rejected/late Hubs are registered
//!   solely for identity checking and mandatory cleanup, never as extra backends.
//! - Admission is reserved before foreign assembly and released by its RAII
//!   guard. Cleanup can start during assembly, but cannot finish until that
//!   guard is released and any resulting Hub's disposal is accounted for.
//! - `CleanupState` owns the decisions and results; `CleanupCoordinator` owns
//!   locking, wakeups, and disposal scheduling. Foreign disposal runs outside
//!   the state lock, on an OS thread independent of the owner Tokio runtime.
//! - Dropping a completion waiter does not cancel cleanup. `watch` signals are
//!   wakeups/diagnostics, not an event log: waiters recheck authoritative state.
//!
//! # Locking discipline
//!
//! - Lock only to reserve admission, register ownership, transfer a disposal
//!   target, record a result, clone the dispatch root, or inspect completion.
//! - Evaluate the runtime-open predicate after locking; pass the resulting
//!   bool to CleanupState. This avoids a pre-lock snapshot, but does not make
//!   the separate runtime atomic and cleanup state one atomic transaction.
//! - Release the state lock BEFORE foreign assembly, thread creation, native
//!   dispose/root destruction, or awaiting a change. Never join a worker here.
//! - Register a result slot BEFORE scheduling its worker, so a concurrent close
//!   cannot overlook already-owned cleanup even if the worker has not started.
//! - Result/admission notifications may be sent under the lock; registration
//!   and start notifications are sent after unlocking. Neither means completion:
//!   a woken waiter acquires the lock and rechecks the full state conjunction.
//!
//! # Why an independent disposal thread
//!
//! Native cleanup may be scheduled after the owner Tokio runtime shuts down,
//! so it cannot rely on that runtime accepting another blocking-pool job.
//! Already-running spawn_blocking work is not generally cancelled by shutdown;
//! the issue here is scheduling late work, not interrupting a running task.
//! One OS thread per disposal costs more than pool reuse, but is appropriate
//! for this lifecycle-level work. Completion is tracked by slots, not a join.
//! Neither execution choice can forcibly interrupt a blocked native dispose.
//!
//! # Assembly and ownership flow
//!
//! `[lock]` / `[unlock]` below refer to the CleanupCoordinator state mutex.
//! Indentation denotes calls; the outcome branches run after the state call
//! returns. Registering a rejected Hub does not call back into the coordinator.
//!
//! ```text
//! begin_admission()
//!   [lock] read runtime_open; reserve the single assembly slot or reject
//!   [unlock] on acceptance: notify; return the admission guard
//!   |
//!   v
//! foreign backend assembly [no state lock]
//!   | failure: guard Drop releases admission (see below)
//!   v
//! AssemblyAdmission::commit(hub)
//!   CleanupCoordinator::register_owned_hub()
//!     [lock] read runtime_open
//!       CleanupState::register_owned_hub()
//!         duplicate identity -> Err(DuplicateOwnedHub); no new slot
//!         accepted primary   -> retain Hub; return Primary
//!         rejected/late      -> register_rejected_hub()
//!                               record identity + result slot
//!                               return Rejected { disposal, reason }
//!     [unlock] BEFORE handling the outcome
//!       duplicate -> return error; existing registration owns cleanup duty
//!       Primary   -> notify; return Ok; DO NOT schedule disposal yet
//!       Rejected  -> notify; schedule_disposal(disposal); return Err(reason)
//!                    (schedule now, but do not wait for native disposal)
//!   |
//!   v
//! admission guard Drop, on success or error
//!   [lock] release admission; notify completion waiters
//!   [unlock]
//! ```
//!
//! # Cleanup, shared disposal path, and completion
//!
//! ```text
//! start_cleanup()
//!   [lock] if already started: no change
//!          otherwise mark started; take the primary Hub's DisposalTarget
//!   [unlock] if newly started: notify; schedule_disposal(primary) if present
//!   |
//!   | primary is scheduled here; rejected/late Hubs are scheduled at commit
//!   v
//! schedule_disposal(target) [no state lock; shared by both paths]
//!   create independent OS thread with target.into_worker()
//!   | creation fails: preserve Hub root; record WorkerStopped (see below)
//!   | succeeds: caller continues without joining the worker
//!   v
//! worker executes [no state lock]
//!   dispose_owned_hub() -- synchronous dispose + root drop, with panic guards
//!   |
//!   v
//! CleanupCoordinator::record_disposal_result(slot, result)
//!   [lock] store result in primary/rejected slot; notify completion waiters
//!   [unlock]
//!   (also used by the scheduling caller if thread creation fails)
//!
//! wait_for_completion()
//!   subscribe to changes BEFORE checking state
//!   [lock] check: cleanup started AND admission released AND all slots settled
//!          complete -> read retained result; unlock and return
//!   [unlock] if incomplete: await a change, then lock and recheck
//!   result priority: primary error first, then rejected errors, otherwise Ok
//! ```
//!
//! Admission and cleanup overlap; they are not mutually exclusive phases.
//! A worker that cannot be created, or a panic that leaves native disposal in
//! an unsafe state, deliberately preserves a Hub root rather than triggering
//! potentially blocking/reentrant destruction on the caller. These exceptional
//! paths report `WorkerStopped`; they do not claim successful resource release.
//!
//! Detailed rationale, ownership tables, race cases, and test evidence are in
//! [`docs/research/bleng-cleanup-design-2026-10-07.md`](../../../docs/research/bleng-cleanup-design-2026-10-07.md).
//! A [Chinese version](../../../docs/research/bleng-cleanup-design-2026-10-07.zh-CN.md) is also available.

use std::{
    mem::ManuallyDrop,
    panic::{AssertUnwindSafe, catch_unwind},
    sync::{Arc, Mutex, PoisonError, Weak},
};

use blehub::{BleHub, BleHubError};
use tokio::sync::watch;

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum CleanupError {
    Backend(BleHubError),
    WorkerStopped,
}

/// Failure to enter or commit the single Hub ownership/assembly slot.
///
/// Admission reserves the slot before foreign backend assembly starts and
/// remains held until the resulting Hub is attached or the attempt rolls back.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum AssemblyAdmissionError {
    /// The same canonical Hub allocation was already accepted or rejected.
    DuplicateOwnedHub,
    /// Another primary Hub or assembly attempt already occupies the slot.
    AdditionalHubRejected,
    /// Cleanup has started or the runtime no longer accepts assembly.
    Closed,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum DisposalSlot {
    /// The result belonging to the one active primary registration.
    Primary,
    /// The result belonging to one rejected or late registration.
    Rejected(usize),
}

/// One append-only cleanup ledger entry for a rejected or late Hub.
///
/// Rejected Hubs never become dispatchable; they only need identity tracking
/// and a disposal result until close completes.
struct RejectedHub {
    identity: Weak<BleHub>,
    result: Option<Result<(), CleanupError>>,
}

/// The separate primary registration.
///
/// The Hub root is present while the runtime is open and is moved to a
/// `DisposalTarget` when cleanup starts. The registration itself remains so
/// its identity and primary disposal result survive that ownership transfer.
struct PrimaryRegistration {
    identity: Weak<BleHub>,
    hub: Option<Arc<BleHub>>,
    result: Option<Result<(), CleanupError>>,
}

/// The Hub and ledger slot associated with one cleanup-owned registration.
///
/// While the runtime is open, the primary target remains in `CleanupState` for
/// dispatch. `start_cleanup()` moves it to a disposal worker. Rejected/late
/// targets go directly to that worker after registration.
struct DisposalTarget {
    hub: Arc<BleHub>,
    slot: DisposalSlot,
}

#[derive(Default)]
struct CleanupState {
    /// The only Hub that can receive commands before cleanup starts.
    primary: Option<PrimaryRegistration>,
    /// Append-only ledger for rejected/late Hubs only. Slots remain stable
    /// because entries are never removed or reused during this lifecycle.
    rejected_hubs: Vec<RejectedHub>,
    /// Whether cleanup has been started. This transition is monotonic.
    started: bool,
    /// Whether a foreign assembly currently holds the admission reservation.
    admission_reserved: bool,
}

#[derive(Clone, Copy)]
enum HubRegistration {
    /// The admission guard reserved the primary ownership slot, so this Hub
    /// may claim the one primary slot even while admission is reserved.
    Admitted,
    /// A test-only direct registration has no admission guard and therefore
    /// must not bypass an active assembly.
    #[cfg(test)]
    Unadmitted,
}

/// Result of changing registration state while holding the cleanup lock.
///
/// A rejected Hub and its disposal slot stay together until `CleanupCoordinator`
/// transfers them to a background disposal job. They are deliberately not
/// placed in `AssemblyAdmissionError`, which is a caller-facing reason rather
/// than an ownership carrier.
enum HubRegistrationOutcome {
    Primary,
    Rejected {
        disposal: DisposalTarget,
        reason: AssemblyAdmissionError,
    },
}

enum CleanupStartResult {
    AlreadyStarted,
    Started { primary: Option<DisposalTarget> },
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum CleanupSignal {
    Initialized,
    AdmissionReserved,
    AdmissionReleased,
    PrimaryHubRegistered,
    RejectedHubRegistered,
    CleanupStarted,
    DisposalCompleted,
}

impl PrimaryRegistration {
    fn new(identity: Weak<BleHub>, hub: Arc<BleHub>) -> Self {
        Self {
            identity,
            hub: Some(hub),
            result: None,
        }
    }

    fn matches_identity(&self, identity: &Weak<BleHub>) -> bool {
        self.identity.ptr_eq(identity)
    }

    fn dispatch_hub(&self) -> Option<Arc<BleHub>> {
        self.hub.as_ref().map(Arc::clone)
    }

    fn take_disposal_target(&mut self) -> Option<DisposalTarget> {
        self.hub.take().map(|hub| DisposalTarget {
            hub,
            slot: DisposalSlot::Primary,
        })
    }

    fn is_settled(&self) -> bool {
        self.result.is_some()
    }

    fn record_disposal_result(&mut self, result: Result<(), CleanupError>) {
        self.result = Some(result);
    }

    fn error(&self) -> Option<CleanupError> {
        self.result
            .as_ref()
            .and_then(|result| result.as_ref().err().cloned())
    }
}

impl RejectedHub {
    fn new(identity: Weak<BleHub>) -> Self {
        Self {
            identity,
            result: None,
        }
    }

    fn matches_identity(&self, identity: &Weak<BleHub>) -> bool {
        self.identity.ptr_eq(identity)
    }

    fn is_settled(&self) -> bool {
        self.result.is_some()
    }

    fn record_disposal_result(&mut self, result: Result<(), CleanupError>) {
        self.result = Some(result);
    }

    fn error(&self) -> Option<CleanupError> {
        self.result
            .as_ref()
            .and_then(|result| result.as_ref().err().cloned())
    }
}

impl DisposalTarget {
    fn into_worker(self, shared: Arc<CleanupCoordinator>) -> impl FnOnce() + Send + 'static {
        // If thread creation fails, dropping its closure must not run BleHub's
        // blocking Drop on the caller. Only an executing worker unwraps this
        // root; an unstarted worker deliberately leaks it instead.
        let hub = ManuallyDrop::new(self.hub);
        let slot = self.slot;
        move || {
            let result = dispose_owned_hub(ManuallyDrop::into_inner(hub));
            shared.record_disposal_result(slot, result);
        }
    }
}

impl CleanupState {
    fn reserve_admission(&mut self, runtime_open: bool) -> Result<(), AssemblyAdmissionError> {
        if self.started || !runtime_open {
            return Err(AssemblyAdmissionError::Closed);
        }
        if self.admission_reserved || self.primary.is_some() {
            return Err(AssemblyAdmissionError::AdditionalHubRejected);
        }
        self.admission_reserved = true;
        Ok(())
    }

    /// Registers a Hub or records a rejected Hub for mandatory cleanup.
    ///
    /// `Err` is reserved for duplicate identity, where this call did not take
    /// ownership of a new cleanup slot. A closed or secondary assembly returns
    /// `Ok(Rejected { .. })` so the Hub and its slot can be transferred to the
    /// disposal scheduler before the caller receives the rejection reason.
    fn register_owned_hub(
        &mut self,
        hub: Arc<BleHub>,
        registration: HubRegistration,
        runtime_open: bool,
    ) -> Result<HubRegistrationOutcome, AssemblyAdmissionError> {
        let identity = Arc::downgrade(&hub);
        if self
            .primary
            .as_ref()
            .is_some_and(|primary| primary.matches_identity(&identity))
            || self
                .rejected_hubs
                .iter()
                .any(|rejected| rejected.matches_identity(&identity))
        {
            // Do not schedule an alias of any Hub already under cleanup.
            return Err(AssemblyAdmissionError::DuplicateOwnedHub);
        }

        let can_claim_primary =
            matches!(registration, HubRegistration::Admitted) || !self.admission_reserved;
        if !self.started && runtime_open && self.primary.is_none() && can_claim_primary {
            self.primary = Some(PrimaryRegistration::new(identity, hub));
            return Ok(HubRegistrationOutcome::Primary);
        }

        Ok(self.register_rejected_hub(hub, identity, runtime_open))
    }

    fn register_rejected_hub(
        &mut self,
        hub: Arc<BleHub>,
        identity: Weak<BleHub>,
        runtime_open: bool,
    ) -> HubRegistrationOutcome {
        let reason = if self.started || !runtime_open {
            AssemblyAdmissionError::Closed
        } else {
            AssemblyAdmissionError::AdditionalHubRejected
        };
        let slot = DisposalSlot::Rejected(self.rejected_hubs.len());
        self.rejected_hubs.push(RejectedHub::new(identity));
        HubRegistrationOutcome::Rejected {
            disposal: DisposalTarget { hub, slot },
            reason,
        }
    }

    fn release_admission(&mut self) {
        self.admission_reserved = false;
    }

    fn hub_for_dispatch(&self) -> Option<Arc<BleHub>> {
        if self.started {
            None
        } else {
            self.primary
                .as_ref()
                .and_then(PrimaryRegistration::dispatch_hub)
        }
    }

    fn start_cleanup(&mut self) -> CleanupStartResult {
        if self.started {
            return CleanupStartResult::AlreadyStarted;
        }
        self.started = true;
        CleanupStartResult::Started {
            primary: self
                .primary
                .as_mut()
                .and_then(PrimaryRegistration::take_disposal_target),
        }
    }

    /// Returns whether the terminal cleanup condition has been reached.
    ///
    /// Every clause matters: disposal must have started, admission must no
    /// longer allow a foreign assembly to add ownership, and every registered
    /// Hub must have a settled disposal result.
    fn is_cleanup_complete(&self) -> bool {
        let primary_complete = self
            .primary
            .as_ref()
            .is_none_or(PrimaryRegistration::is_settled);
        self.started
            && !self.admission_reserved
            && primary_complete
            && self.rejected_hubs.iter().all(RejectedHub::is_settled)
    }

    fn record_disposal_result(&mut self, slot: DisposalSlot, result: Result<(), CleanupError>) {
        match slot {
            DisposalSlot::Primary => {
                self.primary
                    .as_mut()
                    .expect("primary disposal result has a registration")
                    .record_disposal_result(result);
            }
            DisposalSlot::Rejected(index) => {
                self.rejected_hubs[index].record_disposal_result(result);
            }
        }
    }

    /// Selects the terminal cleanup error after every disposal slot settled.
    ///
    /// The primary Hub's error has precedence because it represents the
    /// runtime's canonical ownership; rejected/late Hub errors are fallback
    /// diagnostics only.
    fn completed_result(&self) -> Result<(), CleanupError> {
        let primary_error = self.primary.as_ref().and_then(PrimaryRegistration::error);
        primary_error
            .or_else(|| self.rejected_hubs.iter().find_map(RejectedHub::error))
            .map_or(Ok(()), Err)
    }
}

/// Shared cleanup state that may outlive the public cleanup façade.
///
/// Disposal workers and admission guards retain this coordinator independently
/// of the façade and owner runtime. The state and notifier remain available
/// until those owners finish registration, disposal, and result publication.
struct CleanupCoordinator {
    state: Mutex<CleanupState>,
    changed: watch::Sender<CleanupSignal>,
}

impl CleanupCoordinator {
    fn signal(&self, signal: CleanupSignal) {
        self.changed.send_replace(signal);
    }

    fn record_disposal_result(&self, slot: DisposalSlot, result: Result<(), CleanupError>) {
        let mut state = self.state.lock().unwrap_or_else(PoisonError::into_inner);
        state.record_disposal_result(slot, result);
        self.signal(CleanupSignal::DisposalCompleted);
    }

    /// Releases the admission reservation and wakes close waiters.
    fn release_admission(&self) {
        let mut state = self.state.lock().unwrap_or_else(PoisonError::into_inner);
        state.release_admission();
        self.signal(CleanupSignal::AdmissionReleased);
    }

    fn register_owned_hub(
        self: &Arc<Self>,
        hub: Arc<BleHub>,
        registration: HubRegistration,
        is_open: impl FnOnce() -> bool,
    ) -> Result<(), AssemblyAdmissionError> {
        let outcome = {
            let mut state = self.state.lock().unwrap_or_else(PoisonError::into_inner);
            // Read runtime state after acquiring the lock, not before waiting for it.
            let runtime_open = is_open();
            state.register_owned_hub(hub, registration, runtime_open)?
        };
        match outcome {
            HubRegistrationOutcome::Primary => {
                self.signal(CleanupSignal::PrimaryHubRegistered);
                Ok(())
            }
            HubRegistrationOutcome::Rejected { disposal, reason } => {
                self.signal(CleanupSignal::RejectedHubRegistered);
                self.schedule_disposal(disposal);
                Err(reason)
            }
        }
    }

    /// Fire-and-forget native disposal on an executor-independent thread.
    ///
    /// Cleanup may outlive the Tokio runtime when a foreign assembly waiter is
    /// cancelled. Using a dedicated blocking thread keeps late rejected Hubs
    /// disposable after `RuntimeOwner::Drop` has shut that runtime down. The
    /// result ledger, rather than the detached thread, is the completion
    /// boundary observed by `HubCleanup::wait_for_completion()`.
    fn schedule_disposal(self: &Arc<Self>, disposal: DisposalTarget) {
        let slot = disposal.slot;
        let result = std::thread::Builder::new()
            .name("blehub-disposal".to_owned())
            .spawn(disposal.into_worker(Arc::clone(self)));
        if result.is_err() {
            // The discarded worker preserves its Hub root rather than running
            // native cleanup here. Settle the slot as failed, not as disposed.
            self.record_disposal_result(slot, Err(CleanupError::WorkerStopped));
        }
    }
}

fn dispose_owned_hub(hub: Arc<BleHub>) -> Result<(), CleanupError> {
    match catch_unwind(AssertUnwindSafe(|| hub.dispose())) {
        Ok(result) => {
            // A downstream destructor can panic even after native disposal
            // completed. Always settle the cleanup slot for close waiters.
            if catch_unwind(AssertUnwindSafe(|| drop(hub))).is_err() {
                return Err(CleanupError::WorkerStopped);
            }
            result.map_err(CleanupError::Backend)
        }
        Err(_) => {
            // A panic can leave BleHub's lifecycle stuck in Closing. Its
            // Drop would retry dispose and wait forever; preserve the root
            // rather than hang every close waiter on this unrecoverable bug.
            std::mem::forget(hub);
            Err(CleanupError::WorkerStopped)
        }
    }
}

// Admission is acquired before the foreign attach call and held through
// rollback or registration of the resulting Hub. Close cannot finish while
// this guard exists, even if no Hub has been constructed yet.
pub(crate) struct AssemblyAdmission {
    shared: Arc<CleanupCoordinator>,
}

impl AssemblyAdmission {
    pub(crate) fn commit(
        self,
        hub: Arc<BleHub>,
        is_open: impl FnOnce() -> bool,
    ) -> Result<(), AssemblyAdmissionError> {
        self.shared
            .register_owned_hub(hub, HubRegistration::Admitted, is_open)
    }
}

impl Drop for AssemblyAdmission {
    fn drop(&mut self) {
        self.shared.release_admission();
    }
}

pub(crate) struct HubCleanup {
    shared: Arc<CleanupCoordinator>,
}

impl HubCleanup {
    pub(crate) fn new() -> Self {
        let (changed, _) = watch::channel(CleanupSignal::Initialized);
        Self {
            shared: Arc::new(CleanupCoordinator {
                state: Mutex::new(CleanupState::default()),
                changed,
            }),
        }
    }

    /// Runs an operation while holding the admission reservation for assembly.
    ///
    /// The reservation is released when `operation` returns, whether the
    /// operation commits a Hub, rejects it, or exits through an error. The
    /// generic error conversion keeps cleanup independent from runtime assembly
    /// error types while allowing callers to combine admission and backend
    /// failures in one result.
    pub(crate) fn with_admission<T, E>(
        self: &Arc<Self>,
        is_open: impl FnOnce() -> bool,
        operation: impl FnOnce(AssemblyAdmission) -> Result<T, E>,
    ) -> Result<T, E>
    where
        E: From<AssemblyAdmissionError>,
    {
        let admission = self.begin_admission(is_open).map_err(E::from)?;
        operation(admission)
    }

    pub(crate) fn begin_admission(
        self: &Arc<Self>,
        is_open: impl FnOnce() -> bool,
    ) -> Result<AssemblyAdmission, AssemblyAdmissionError> {
        let mut state = self
            .shared
            .state
            .lock()
            .unwrap_or_else(PoisonError::into_inner);
        // Read runtime state after acquiring the lock, not before waiting for it.
        let runtime_open = is_open();
        state.reserve_admission(runtime_open)?;
        drop(state);
        self.shared.signal(CleanupSignal::AdmissionReserved);
        Ok(AssemblyAdmission {
            shared: Arc::clone(&self.shared),
        })
    }

    pub(crate) fn hub_for_dispatch(&self) -> Option<Arc<BleHub>> {
        let state = self
            .shared
            .state
            .lock()
            .unwrap_or_else(PoisonError::into_inner);
        state.hub_for_dispatch()
    }

    pub(crate) fn start_cleanup(&self) {
        let primary = {
            let mut state = self
                .shared
                .state
                .lock()
                .unwrap_or_else(PoisonError::into_inner);
            state.start_cleanup()
        };
        if let CleanupStartResult::Started { primary } = primary {
            self.shared.signal(CleanupSignal::CleanupStarted);
            if let Some(disposal) = primary {
                self.shared.schedule_disposal(disposal);
            }
        }
    }

    /// Waits for the cleanup lifecycle to settle after `start_cleanup()`.
    ///
    /// This waits for the admission reservation to be released and every
    /// registered Hub disposal slot to receive a result, including rejected
    /// or late-committed Hubs. It does not wait for BLE commands, scan,
    /// connection, GATT, or waiter operations; those belong to the runtime
    /// owner and waiter registry. The owner normally calls this after
    /// `HubCleanup::start_cleanup()` during `RuntimeOwner::close()`.
    pub(crate) async fn wait_for_completion(&self) -> Result<(), CleanupError> {
        let mut changes = self.shared.changed.subscribe();
        loop {
            {
                let state = self
                    .shared
                    .state
                    .lock()
                    .unwrap_or_else(PoisonError::into_inner);
                // `CleanupSignal` only wakes this loop. The state conjunction
                // below is the actual terminal condition for cleanup.
                if state.is_cleanup_complete() {
                    return state.completed_result();
                }
            }
            // Signals only wake this loop. `watch` may coalesce multiple
            // signals, so CleanupState remains the authoritative completion
            // source instead of matching a particular signal here.
            match changes.changed().await {
                Ok(()) => {
                    let signal = *changes.borrow_and_update();
                    tracing::trace!(
                        target: "bleng::cleanup",
                        ?signal,
                        "cleanup state changed"
                    );
                }
                Err(_) => return Err(CleanupError::WorkerStopped),
            }
        }
    }
}

impl Drop for HubCleanup {
    fn drop(&mut self) {
        self.start_cleanup();
    }
}

#[cfg(test)]
#[path = "tests/cleanup_tests.rs"]
mod tests;
