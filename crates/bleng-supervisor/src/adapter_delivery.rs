//! Bounded adapter-state observation, independent of any operation result or native control.
//!
//! The owner's existing single reader already publishes `AdapterSnapshotChanged` on the public
//! event FIFO; this module only stores what a consumer has not pulled yet. It owns no runtime,
//! timer or radio control: a pull deadline is the caller's bound, dropping the endpoint never
//! changes the adapter, and no last-known snapshot is replayed to a consumer that starts later.

use std::{
    collections::VecDeque,
    sync::{Arc, Mutex, MutexGuard, atomic::AtomicBool},
};

use blehub::AdapterSnapshot;
use bleng::Bleng;
use tokio::sync::Notify;

use crate::SupervisorOperationError;
use crate::pull_guard::PullGuard;

/// How many unread snapshots one observation may hold.
///
/// A snapshot is tiny — a state, two authorization words and an optional name — and the only way to
/// fill this is a consumer that stops pulling, so overflow ends observation instead of growing
/// without bound. The bound is the same one the other lanes use, so "bounded observation" reads the
/// same way everywhere.
pub const MAX_PENDING_SNAPSHOTS: usize = 64;

#[derive(Clone, Copy, Debug, PartialEq, Eq, thiserror::Error)]
pub enum AdapterDeliveryError {
    #[error("another adapter observation pull is pending")]
    PullBusy,
    #[error("adapter snapshot budget exhausted")]
    Overflow,
    #[error("adapter source owner retired")]
    OwnerRetired,
    #[error("adapter observation endpoint detached")]
    Detached,
    #[error("adapter observation synchronization failed")]
    Unavailable,
}

#[derive(Debug, thiserror::Error)]
pub enum AdapterSubmissionError {
    #[error("adapter observation rejected: {0}")]
    Owner(#[from] SupervisorOperationError),
    #[error("adapter source owner retired")]
    OwnerRetired,
    #[error("adapter observation synchronization failed")]
    Unavailable,
}

#[derive(Debug)]
struct DeliveryState {
    snapshots: VecDeque<AdapterSnapshot>,
    /// Retained: reading it twice returns it twice. Only the first failure is kept, so a later
    /// close can never conceal an earlier one.
    terminal: Option<AdapterDeliveryError>,
    detached: bool,
}

impl DeliveryState {
    fn fail(&mut self, error: AdapterDeliveryError) {
        self.snapshots.clear();
        if self.terminal.is_none() {
            self.terminal = Some(error);
        }
    }
}

/// Native Rust handoff: the lane, plus the owner whose runtime must poll it.
///
/// The lane itself owns no timer. A foreign caller may have no runtime at all, so the deadline is
/// applied on the owner's executor — the same runtime the operations already use.
pub struct AdapterHandoff {
    pub bleng: Arc<Bleng>,
    pub delivery: Arc<AdapterDelivery>,
}

/// One owner-bound, single-pending-pull adapter observation endpoint.
///
/// Several of these may coexist on one owner: each is an independent reader of the same stream, and
/// each receives every snapshot offered after it was registered. Registration happens under the
/// projection's lock, so a snapshot cannot slip in between "accepted" and "observable".
pub struct AdapterDelivery {
    state: Mutex<DeliveryState>,
    changed: Notify,
    pulling: AtomicBool,
}

impl std::fmt::Debug for AdapterDelivery {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("AdapterDelivery")
            .field("state", &self.state)
            .field("pulling", &self.pulling)
            .finish_non_exhaustive()
    }
}

impl AdapterDelivery {
    pub(crate) fn new() -> Self {
        Self {
            state: Mutex::new(DeliveryState {
                snapshots: VecDeque::new(),
                terminal: None,
                detached: false,
            }),
            changed: Notify::new(),
            pulling: AtomicBool::new(false),
        }
    }

    fn lock(&self) -> MutexGuard<'_, DeliveryState> {
        self.state.lock().unwrap_or_else(|poisoned| {
            let mut state = poisoned.into_inner();
            state.fail(AdapterDeliveryError::Unavailable);
            self.changed.notify_one();
            state
        })
    }

    pub(crate) fn offer(&self, snapshot: &AdapterSnapshot) {
        let mut state = self.lock();
        if state.terminal.is_some() {
            return;
        }
        if state.snapshots.len() == MAX_PENDING_SNAPSHOTS {
            state.fail(AdapterDeliveryError::Overflow);
        } else {
            state.snapshots.push_back(snapshot.clone());
        }
        drop(state);
        self.changed.notify_one();
    }

    /// Cancel-safe pending pull: dropping the future retires this pull only.
    ///
    /// The caller owns the deadline; this method never invents one. A pull that finds the retained
    /// terminal returns it again, because an observation's end is a fact and not a consumable
    /// receipt.
    pub async fn next_event(&self) -> Result<AdapterSnapshot, AdapterDeliveryError> {
        let Some(_pull) = PullGuard::acquire(&self.pulling) else {
            return Err(AdapterDeliveryError::PullBusy);
        };
        loop {
            // Take the notification before the state so a wake-up cannot be missed between them.
            let changed = self.changed.notified();
            {
                let mut state = self.lock();
                if let Some(snapshot) = state.snapshots.pop_front() {
                    return Ok(snapshot);
                }
                if let Some(error) = state.terminal {
                    return Err(error);
                }
            }
            changed.await;
        }
    }

    pub(crate) fn fail_unavailable(&self) {
        self.lock().fail(AdapterDeliveryError::Unavailable);
        self.changed.notify_one();
    }

    pub(crate) fn close(&self) {
        self.lock().fail(AdapterDeliveryError::OwnerRetired);
        self.changed.notify_one();
    }

    /// Ends observation only. The adapter, the engine and every operation stay untouched.
    pub fn detach(&self) {
        let mut state = self.lock();
        state.detached = true;
        state.fail(AdapterDeliveryError::Detached);
        drop(state);
        self.changed.notify_one();
    }
}

impl Default for AdapterDelivery {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
#[path = "tests/adapter_delivery.rs"]
mod tests;
