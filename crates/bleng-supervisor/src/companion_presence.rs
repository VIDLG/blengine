//! Bounded companion-device presence observation.
//!
//! A platform association can opt in to proximity delivery (`notifyOnDeviceNearby`). When it does,
//! the platform reports one transition each time the associated peer appears or disappears; the
//! backend turns that into `CompanionPresenceChanged` on the owner's single event reader, and this
//! module stores what a consumer has not pulled yet.
//!
//! Presence is an **observation**, not a link: it is edge-triggered while the lane is live, so the
//! first event a consumer sees is a transition and not a replay of current state. Nothing here can
//! connect, associate, or change the adapter, and a pull deadline retires the pull — not the stream.

use std::{
    collections::VecDeque,
    sync::{Arc, Mutex, MutexGuard, atomic::AtomicBool},
};

use bleng::Bleng;
use tokio::sync::Notify;

use crate::SupervisorOperationError;
use crate::pull_guard::PullGuard;

/// How many unread presence transitions one observation may hold.
///
/// A transition is tiny and edge-triggered, so the only way to fill this is a consumer that stops
/// pulling; overflow ends observation instead of growing without bound. The bound matches the other
/// lanes so "bounded observation" reads the same way everywhere.
pub const MAX_PENDING_PRESENCE: usize = 64;

#[derive(Clone, Copy, Debug, PartialEq, Eq, thiserror::Error)]
pub enum CompanionPresenceDeliveryError {
    #[error("another companion presence pull is pending")]
    PullBusy,
    #[error("companion presence budget exhausted")]
    Overflow,
    #[error("companion presence source owner retired")]
    OwnerRetired,
    #[error("companion presence observation endpoint detached")]
    Detached,
    #[error("companion presence observation synchronization failed")]
    Unavailable,
}

#[derive(Debug, thiserror::Error)]
pub enum CompanionPresenceSubmissionError {
    #[error("companion presence observation rejected: {0}")]
    Owner(#[from] SupervisorOperationError),
    #[error("companion presence source owner retired")]
    OwnerRetired,
    #[error("companion presence observation synchronization failed")]
    Unavailable,
}

/// One presence transition. `key` reuses the owner-scoped inventory key the backend already
/// allocated for that address, so it is comparable with `peripheral.key()` for the same peer.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CompanionPresence {
    /// Whether the associated peer is now nearby.
    pub present: bool,
    /// Owner-scoped inventory key; never the platform address.
    pub key: String,
    /// The platform's association label when it has one.
    pub local_name: Option<String>,
}

#[derive(Debug)]
struct PresenceState {
    events: VecDeque<CompanionPresence>,
    /// Retained: reading it twice returns it twice. Only the first failure is kept, so a later
    /// close can never conceal an earlier one.
    terminal: Option<CompanionPresenceDeliveryError>,
    detached: bool,
}

impl PresenceState {
    fn fail(&mut self, error: CompanionPresenceDeliveryError) {
        self.events.clear();
        if self.terminal.is_none() {
            self.terminal = Some(error);
        }
    }
}

/// Native Rust handoff: the lane, plus the owner whose runtime must poll it.
///
/// The lane itself owns no timer. A foreign caller may have no runtime at all, so the deadline is
/// applied on the owner's executor — the same runtime the operations already use.
pub struct CompanionPresenceHandoff {
    pub bleng: Arc<Bleng>,
    pub delivery: Arc<CompanionPresenceDelivery>,
}

/// One owner-bound, single-pending-pull companion-presence endpoint.
///
/// Several of these may coexist on one owner: each is an independent reader of the same stream and
/// receives every transition offered after it was registered. Registration happens under the
/// projection's lock, so a transition cannot slip in between "accepted" and "observable".
pub struct CompanionPresenceDelivery {
    state: Mutex<PresenceState>,
    changed: Notify,
    pulling: AtomicBool,
}

impl std::fmt::Debug for CompanionPresenceDelivery {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("CompanionPresenceDelivery")
            .field("state", &self.state)
            .field("pulling", &self.pulling)
            .finish_non_exhaustive()
    }
}

impl CompanionPresenceDelivery {
    pub(crate) fn new() -> Self {
        Self {
            state: Mutex::new(PresenceState {
                events: VecDeque::new(),
                terminal: None,
                detached: false,
            }),
            changed: Notify::new(),
            pulling: AtomicBool::new(false),
        }
    }

    fn lock(&self) -> MutexGuard<'_, PresenceState> {
        self.state.lock().unwrap_or_else(|poisoned| {
            let mut state = poisoned.into_inner();
            state.fail(CompanionPresenceDeliveryError::Unavailable);
            self.changed.notify_one();
            state
        })
    }

    pub(crate) fn offer(&self, presence: CompanionPresence) {
        let mut state = self.lock();
        if state.terminal.is_some() {
            return;
        }
        if state.events.len() == MAX_PENDING_PRESENCE {
            state.fail(CompanionPresenceDeliveryError::Overflow);
        } else {
            state.events.push_back(presence);
        }
        drop(state);
        self.changed.notify_one();
    }

    /// Cancel-safe pending pull: dropping the future retires this pull only.
    ///
    /// The caller owns the deadline; this method never invents one. A pull that finds the retained
    /// terminal returns it again, because an observation's end is a fact and not a consumable
    /// receipt.
    pub async fn next_event(&self) -> Result<CompanionPresence, CompanionPresenceDeliveryError> {
        let Some(_pull) = PullGuard::acquire(&self.pulling) else {
            return Err(CompanionPresenceDeliveryError::PullBusy);
        };
        loop {
            // Take the notification before the state so a wake-up cannot be missed between them.
            let changed = self.changed.notified();
            {
                let mut state = self.lock();
                if let Some(presence) = state.events.pop_front() {
                    return Ok(presence);
                }
                if let Some(error) = state.terminal {
                    return Err(error);
                }
            }
            changed.await;
        }
    }

    pub(crate) fn fail_unavailable(&self) {
        self.lock()
            .fail(CompanionPresenceDeliveryError::Unavailable);
        self.changed.notify_one();
    }

    pub(crate) fn close(&self) {
        self.lock()
            .fail(CompanionPresenceDeliveryError::OwnerRetired);
        self.changed.notify_one();
    }

    /// Ends observation only. The association, the engine and every operation stay untouched.
    pub fn detach(&self) {
        let mut state = self.lock();
        state.detached = true;
        state.fail(CompanionPresenceDeliveryError::Detached);
        drop(state);
        self.changed.notify_one();
    }
}

impl Default for CompanionPresenceDelivery {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
#[path = "tests/companion_presence.rs"]
mod tests;
