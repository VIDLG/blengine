//! Runtime lifecycle gate shared by commands, ingress, and cleanup.

use std::sync::{
    Arc,
    atomic::{AtomicU8, Ordering},
};

use blehub::BleHub;

use crate::{
    cleanup::{AssemblyAdmission, AssemblyAdmissionError, CleanupError, HubCleanup},
    waiter::WaiterError,
};
use tokio::sync::watch;

/// Internal lifecycle phase for ingress, actor delivery, and backend cleanup.
///
/// The enum is the semantic representation. `RuntimeLifecycle` stores it in an
/// `AtomicU8` so phase transitions can use compare-and-exchange without a
/// mutex.
#[repr(u8)]
#[derive(Clone, Copy, Debug, PartialEq, Eq, derive_more::TryFrom)]
#[try_from(repr)]
pub(crate) enum RuntimePhase {
    /// Runtime accepts commands and ingress events.
    Open = 0,
    /// An unexpected transport or invariant failure occurred.
    Faulted = 1,
    /// The owner explicitly closed the runtime.
    Closed = 2,
}

impl From<RuntimePhase> for u8 {
    fn from(phase: RuntimePhase) -> Self {
        phase as u8
    }
}

/// Shared lifecycle controller for ingress, actor delivery, and backend cleanup.
///
/// This is not just a status snapshot: it controls phase transitions and starts
/// cleanup on faults even when the actor or its mailbox is unavailable.
/// HubCleanup owns disposal mechanics; callers use this controller's operations
/// rather than accessing or replacing its cleanup owner.
///
/// `RuntimePhase::Open` is the only dispatchable state. A fault or explicit
/// close is monotonic, so late callbacks cannot reopen the runtime or reuse
/// native state.
///
/// The `Arc` is intentional: the actor, owner facade, backend port, and
/// detached native-dispatch task can outlive one another.
/// They all observe the same gate, but none of them borrows the owner or actor.
pub(crate) struct RuntimeLifecycle {
    phase: AtomicU8,
    phase_tx: watch::Sender<RuntimePhase>,
    cleanup: Arc<HubCleanup>,
}

impl RuntimeLifecycle {
    pub(crate) fn new() -> Self {
        let (phase_tx, _) = watch::channel(RuntimePhase::Open);
        Self {
            phase: AtomicU8::new(RuntimePhase::Open.into()),
            phase_tx,
            cleanup: Arc::new(HubCleanup::new()),
        }
    }

    /// Reads the shared gate; this snapshot does not reserve admission or prevent closure.
    pub(super) fn is_open(&self) -> bool {
        self.phase() == RuntimePhase::Open
    }

    /// Maps the shared runtime gate to command admission without exposing phase storage.
    pub(super) fn require_open(&self) -> Result<(), WaiterError> {
        match self.phase() {
            RuntimePhase::Closed => Err(WaiterError::Closed),
            RuntimePhase::Faulted => Err(WaiterError::TransportFault),
            RuntimePhase::Open => Ok(()),
        }
    }

    /// Acquires a dispatchable root and maps lifecycle/cleanup gates to one result.
    /// Check phase after the cleanup lock so close/fault wins over a missing root.
    /// The returned root is not a lease against subsequent close or native disposal.
    pub(super) fn hub_for_dispatch(&self) -> Result<Arc<BleHub>, WaiterError> {
        let hub = self.cleanup.hub_for_dispatch();
        self.require_open()?;
        hub.ok_or(WaiterError::BackendNotAssembled)
    }

    /// Checks the open gate and assembled Hub before enqueueing a command.
    /// This snapshot does not reserve the Hub; actor dispatch checks again.
    pub(super) fn require_submission_ready(&self) -> Result<(), WaiterError> {
        self.hub_for_dispatch().map(|_| ())
    }

    /// Reserves assembly admission only while this lifecycle is open.
    /// Cleanup evaluates the phase under its lock, not before acquiring it.
    pub(super) fn with_admission<T, E>(
        &self,
        operation: impl FnOnce(AssemblyAdmission) -> Result<T, E>,
    ) -> Result<T, E>
    where
        E: From<AssemblyAdmissionError>,
    {
        self.cleanup.with_admission(|| self.is_open(), operation)
    }

    delegate::delegate! {
        to self.cleanup {

            pub(super) fn start_cleanup(&self);

            /// Waits for native Hub cleanup, not actor or executor shutdown.
            #[call(wait_for_completion)]
            pub(super) async fn wait_for_cleanup(&self) -> Result<(), CleanupError>;

            // Direct attachment bypasses production assembly only in fixtures.
            #[cfg(test)]
            #[call(attach)]
            pub(super) fn attach_hub(&self, hub: Arc<BleHub>) -> Result<(), AssemblyAdmissionError>;
        }
    }

    /// Subscribes to lifecycle changes, not native cleanup completion.
    /// The atomic phase remains authoritative; notifications wake async readers.
    pub(crate) fn subscribe_phase(&self) -> watch::Receiver<RuntimePhase> {
        self.phase_tx.subscribe()
    }

    pub(crate) fn phase(&self) -> RuntimePhase {
        self.phase
            .load(Ordering::Acquire)
            .try_into()
            .unwrap_or(RuntimePhase::Faulted)
    }

    pub(crate) fn fault(&self) {
        if self
            .phase
            .compare_exchange(
                RuntimePhase::Open.into(),
                RuntimePhase::Faulted.into(),
                Ordering::AcqRel,
                Ordering::Acquire,
            )
            .is_ok()
        {
            self.phase_tx.send_replace(RuntimePhase::Faulted);
            self.cleanup.start_cleanup();
        }
    }

    /// Marks the admission/ingress gate closed without starting cleanup.
    pub(super) fn mark_closed(&self) {
        if self
            .phase
            .compare_exchange(
                RuntimePhase::Open.into(),
                RuntimePhase::Closed.into(),
                Ordering::AcqRel,
                Ordering::Acquire,
            )
            .is_ok()
        {
            self.phase_tx.send_replace(RuntimePhase::Closed);
        }
    }

    /// Starts normal close without waiting for actor shutdown or native disposal.
    pub(super) fn request_close(&self) {
        self.mark_closed();
        self.cleanup.start_cleanup();
    }
}
