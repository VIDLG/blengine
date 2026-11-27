//! One pending pull per lane, enforced structurally.
//!
//! Every observation lane allows at most one pending pull and reports a second one as a contract
//! violation rather than queueing it. The rule is the same in all of them, so it is written once:
//! a lane that copies this flag reset can forget the `Release` store or forget to reset at all, and
//! then a single cancelled pull wedges the stream forever.

use std::sync::atomic::{AtomicBool, Ordering};

/// Holds the lane's single pull slot until dropped, including when the future is cancelled.
pub(crate) struct PullGuard<'a>(&'a AtomicBool);

impl<'a> PullGuard<'a> {
    /// Claims the slot, or returns `None` when a pull is already pending.
    pub(crate) fn acquire(pulling: &'a AtomicBool) -> Option<Self> {
        pulling
            .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .ok()
            .map(|_| Self(pulling))
    }
}

impl Drop for PullGuard<'_> {
    fn drop(&mut self) {
        self.0.store(false, Ordering::Release);
    }
}
