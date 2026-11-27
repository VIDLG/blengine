//! Rust-only, non-consuming observation of one operation's outcome.
//!
//! Typed `wait()` delivers the full result to one caller. This optional summary
//! lets a separate consumer end its own waiting after confirmed operation failure,
//! even when the result caller never waits, times out, or drops its future.
//! Enable it before submission reaches the actor so late listeners can replay
//! failure. Currently only scan start enables it, for supervisor scan delivery.
//! Submission/actor settlement writes the outcome; a weak listener observes only
//! failure. This module owns no BLE commands, resource retirement, or result payload.

use std::sync::{
    Arc, Mutex, Weak,
    atomic::{AtomicBool, Ordering},
};

/// A synchronous failure notification, independent of the typed operation waiter.
/// Implementations must be short, nonblocking and must not panic. No result payload
/// is copied here; the original typed result and canonical events remain authoritative.
/// Failure includes accepted cancellation before a final successful outcome, not
/// proof of remote rollback. Owner close/fault and reservation destruction remain
/// separate lifecycle signals; integrations must observe owner retirement too.
pub trait OperationFailureObserver: Send + Sync {
    fn operation_failed(&self);
}

/// Keeps one failure registration active without retaining its receiver or Hub.
/// Drop prevents new notification admission without waiting for callbacks to drain.
/// A notification that already observed the active flag may still call its receiver.
pub struct OperationFailureRegistration {
    listener: Arc<Listener>,
}

impl Drop for OperationFailureRegistration {
    fn drop(&mut self) {
        self.listener.revoke();
    }
}

/// One operation's retained outcome and optional failure listener, independent
/// of caller result ownership. Outcome selection and registration share a lock;
/// callbacks always run after releasing it.
#[derive(Default)]
pub(crate) struct OperationOutcomeObservation(Mutex<State>);

impl OperationOutcomeObservation {
    /// Installs one revocable listener; confirmed failure is replayed immediately
    /// outside the lock. An existing active registration keeps its slot.
    pub(crate) fn register(
        &self,
        receiver: Weak<dyn OperationFailureObserver>,
    ) -> Option<OperationFailureRegistration> {
        let (token, replay_failure) = self
            .0
            .lock()
            .expect("operation outcome observation lock")
            .register(receiver)?;
        if replay_failure {
            token.listener.notify();
        }
        Some(token)
    }

    /// Retains the first completion outcome, or defers it while cancellation
    /// control still owns the final decision. Local rejection uses this path too.
    pub(crate) fn record_completion(&self, succeeded: bool, cancellation_pending: bool) {
        let notify = self
            .0
            .lock()
            .expect("operation outcome observation lock")
            .record_completion(succeeded, cancellation_pending);
        if let Some(listener) = notify {
            listener.notify();
        }
    }

    /// Accepted cancellation means operation failure; rejected cancellation releases
    /// any deferred native outcome. Neither can overwrite an already final result.
    pub(crate) fn resolve_cancellation(&self, accepted: bool) {
        let notify = self
            .0
            .lock()
            .expect("operation outcome observation lock")
            .resolve_cancellation(accepted);
        if let Some(listener) = notify {
            listener.notify();
        }
    }
}

#[derive(Default)]
struct State {
    // None: unresolved; Some(true): succeeded; Some(false): failed.
    // Only the first authoritative outcome is retained, never the BLE payload.
    succeeded: Option<bool>,
    // Native completion may precede the independent cancellation control receipt.
    deferred_native: Option<bool>,
    listener: Weak<Listener>,
}

impl State {
    /// The returned flag requests failure replay after the caller releases the lock.
    fn register(
        &mut self,
        receiver: Weak<dyn OperationFailureObserver>,
    ) -> Option<(OperationFailureRegistration, bool)> {
        if self.listener.upgrade().is_some_and(|old| old.is_active()) {
            return None;
        }
        let listener = Arc::new(Listener::new(receiver));
        self.listener = Arc::downgrade(&listener);
        Some((
            OperationFailureRegistration { listener },
            self.succeeded == Some(false),
        ))
    }

    fn record_completion(
        &mut self,
        succeeded: bool,
        cancellation_pending: bool,
    ) -> Option<Arc<Listener>> {
        if self.succeeded.is_some() {
            return None;
        }
        if cancellation_pending {
            self.deferred_native.get_or_insert(succeeded);
            return None;
        }
        self.finish(self.deferred_native.unwrap_or(succeeded))
    }

    fn resolve_cancellation(&mut self, accepted: bool) -> Option<Arc<Listener>> {
        if accepted {
            return self.finish(false);
        }
        self.finish(self.deferred_native?)
    }

    /// Latches the outcome once and returns a listener for lock-free notification.
    fn finish(&mut self, succeeded: bool) -> Option<Arc<Listener>> {
        if self.succeeded.is_some() {
            return None;
        }
        self.succeeded = Some(succeeded);
        self.deferred_native = None;
        if succeeded {
            None
        } else {
            self.listener.upgrade()
        }
    }
}

struct Listener {
    active: AtomicBool,
    receiver: Weak<dyn OperationFailureObserver>,
}

impl Listener {
    fn new(receiver: Weak<dyn OperationFailureObserver>) -> Self {
        Self {
            active: AtomicBool::new(true),
            receiver,
        }
    }

    fn is_active(&self) -> bool {
        self.active.load(Ordering::Acquire)
    }

    fn revoke(&self) {
        self.active.store(false, Ordering::Release);
    }

    fn notify(&self) {
        if self.is_active()
            && let Some(receiver) = self.receiver.upgrade()
        {
            receiver.operation_failed();
        }
    }
}

#[cfg(test)]
#[path = "tests/operation_observation.rs"]
mod tests;
