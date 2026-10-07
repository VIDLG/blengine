//! Runtime lifecycle gate shared by commands, ingress, and cleanup.

use std::sync::{
    Arc,
    atomic::{AtomicU8, Ordering},
};

use crate::cleanup::HubCleanup;
use tokio::sync::watch;

/// Internal lifecycle phase for ingress, actor delivery, and backend cleanup.
///
/// The enum is the semantic representation. `RuntimeStatus` stores it in an
/// `AtomicU8` so phase transitions can use compare-and-exchange without a
/// mutex.
#[repr(u8)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
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

impl TryFrom<u8> for RuntimePhase {
    type Error = ();

    fn try_from(value: u8) -> Result<Self, Self::Error> {
        match value {
            value if value == u8::from(Self::Open) => Ok(Self::Open),
            value if value == u8::from(Self::Faulted) => Ok(Self::Faulted),
            value if value == u8::from(Self::Closed) => Ok(Self::Closed),
            _ => Err(()),
        }
    }
}

/// Shared lifecycle gate for ingress, actor delivery, and backend cleanup.
///
/// `RuntimePhase::Open` is the only dispatchable state. A fault or explicit
/// close is monotonic, so late callbacks cannot reopen the runtime or reuse
/// native state.
///
/// The `Arc` is intentional: the actor, owner facade, ingress bridge,
/// assembly port, and detached native-dispatch task can outlive one another.
/// They all observe the same gate, but none of them borrows the owner or actor.
pub(crate) struct RuntimeStatus {
    pub(super) phase: AtomicU8,
    pub(crate) changed: watch::Sender<RuntimePhase>,
    pub(crate) cleanup: Arc<HubCleanup>,
}

impl RuntimeStatus {
    pub(crate) fn phase(&self) -> RuntimePhase {
        RuntimePhase::try_from(self.phase.load(Ordering::Acquire)).unwrap_or(RuntimePhase::Faulted)
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
            self.changed.send_replace(RuntimePhase::Faulted);
            self.cleanup.start_cleanup();
        }
    }

    pub(crate) fn close(&self) {
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
            self.changed.send_replace(RuntimePhase::Closed);
        }
    }
}
