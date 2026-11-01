//! Shared ID allocation and predispatch submission mechanics for one runtime.

use std::sync::{
    Arc, OnceLock,
    atomic::{AtomicBool, AtomicU8, AtomicU64, Ordering},
};

use blehub::CommandId;

use crate::waiter::{WaiterError, WaiterSender};

/// Cloneable observation of actor-confirmed native completion.
///
/// The actor/reservation retirement path is the only writer. Readers may outlive
/// caller delivery, so this latch is separate from waiter/result ownership.
#[derive(Clone, Default)]
pub(crate) struct CompletionObserver(Arc<AtomicBool>);

impl CompletionObserver {
    pub(crate) fn is_completed(&self) -> bool {
        self.0.load(Ordering::Acquire)
    }

    pub(in crate::runtime) fn confirm_completed(&self) {
        self.0.store(true, Ordering::Release);
    }
}

/// Nonzero IDs. Separate command and actor-owned resource allocators never
/// wrap or reuse IDs; allocation is not rolled back after failed admission.
#[derive(Default)]
pub(super) struct IdGenerator {
    last_issued: AtomicU64,
}

impl IdGenerator {
    pub(super) fn allocate(&self) -> Result<u64, WaiterError> {
        self.last_issued
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |last| {
                last.checked_add(1)
            })
            .map(|last| last + 1)
            .map_err(|_| WaiterError::IdExhausted)
    }
}

/// One atomic linearization point for explicit predispatch cancellation versus claim.
/// Dropping caller delivery never changes this phase.
#[repr(u8)]
#[derive(Clone, Copy, Debug, PartialEq, Eq, derive_more::TryFrom)]
#[try_from(repr)]
enum SubmissionPhase {
    Pending = 0,
    Claimed = 1,
    Cancelled = 2,
    LocallySettled = 3,
}

impl From<SubmissionPhase> for u8 {
    fn from(phase: SubmissionPhase) -> Self {
        phase as u8
    }
}

pub(super) struct SubmissionState {
    phase: AtomicU8,
    pub(super) result_tx: WaiterSender,
    completion: OnceLock<CompletionObserver>,
}

impl SubmissionState {
    fn phase(&self) -> Option<SubmissionPhase> {
        SubmissionPhase::try_from(self.phase.load(Ordering::Acquire)).ok()
    }

    pub(super) fn is_locally_settled(&self) -> bool {
        matches!(
            self.phase(),
            Some(SubmissionPhase::Cancelled | SubmissionPhase::LocallySettled)
        )
    }

    pub(super) fn is_cancelled(&self) -> bool {
        self.phase() == Some(SubmissionPhase::Cancelled)
    }

    pub(super) fn is_completed(&self) -> bool {
        self.is_locally_settled()
            || self
                .completion
                .get()
                .is_some_and(CompletionObserver::is_completed)
    }

    pub(super) fn cancel_before_claim(&self) -> bool {
        if self
            .phase
            .compare_exchange(
                SubmissionPhase::Pending.into(),
                SubmissionPhase::Cancelled.into(),
                Ordering::AcqRel,
                Ordering::Acquire,
            )
            .is_ok()
        {
            self.result_tx.send(Err(WaiterError::Cancelled));
            true
        } else {
            false
        }
    }

    /// Admission rejection competes at the same CAS as cancellation/claim. It
    /// cannot publish first and then incorrectly allow a cancellation receipt.
    pub(super) fn reject(&self, error: WaiterError) {
        match self.phase.compare_exchange(
            SubmissionPhase::Pending.into(),
            SubmissionPhase::LocallySettled.into(),
            Ordering::AcqRel,
            Ordering::Acquire,
        ) {
            Ok(_) => self.result_tx.send(Err(error)),
            Err(previous)
                if matches!(
                    SubmissionPhase::try_from(previous),
                    Ok(SubmissionPhase::Cancelled | SubmissionPhase::LocallySettled)
                ) => {}
            Err(_) => debug_assert!(false, "admission cannot reject after dispatch claim"),
        }
    }
}

/// Queued entry and handle share identity and access to the sole result sender. Resource
/// metadata is written only by actor admission, before it processes BLE evidence.
pub(crate) struct OperationSubmission<Resource = ()> {
    pub(super) id: CommandId,
    pub(super) state: Arc<SubmissionState>,
    pub(super) resource: Arc<OnceLock<Resource>>,
}

impl<R> Clone for OperationSubmission<R> {
    fn clone(&self) -> Self {
        Self {
            id: self.id,
            state: Arc::clone(&self.state),
            resource: Arc::clone(&self.resource),
        }
    }
}

impl<R> OperationSubmission<R> {
    /// Keeps the initial phase and result/resource slots private to the submission protocol.
    pub(super) fn new(id: CommandId, result_tx: WaiterSender) -> Self {
        Self {
            id,
            state: Arc::new(SubmissionState {
                phase: AtomicU8::new(SubmissionPhase::Pending.into()),
                result_tx,
                completion: OnceLock::new(),
            }),
            resource: Arc::new(OnceLock::new()),
        }
    }

    pub(super) fn try_claim(&self) -> bool {
        self.state
            .phase
            .compare_exchange(
                SubmissionPhase::Pending.into(),
                SubmissionPhase::Claimed.into(),
                Ordering::AcqRel,
                Ordering::Acquire,
            )
            .is_ok()
    }

    pub(super) fn resolve(&self, result: Result<R, WaiterError>) {
        match result {
            Ok(resource) => {
                let _ = self.resource.set(resource);
            }
            Err(error) => self.state.reject(error),
        }
    }

    pub(super) fn observe_completion(&self, completion: CompletionObserver) {
        let _ = self.state.completion.set(completion);
    }
}

#[cfg(test)]
#[path = "tests/shared.rs"]
mod tests;
