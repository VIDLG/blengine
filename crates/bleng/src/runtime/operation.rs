//! Caller-side operation ownership, typed decoding and native cancellation.
//!
//! The original actor oneshot is the only result delivery channel. A shared
//! observer lets cancellation see completion even while wait owns the ticket;
//! consuming or dropping delivery never implies native cancellation.
//!
//! # Operation ownership
//!
//! ```text
//! WaiterTicket
//!   └─ caller result delivery, lifecycle/error precedence, timeout
//!
//! OperationControl
//!   └─ independent predispatch/native cancellation capability
//!
//! OperationTicket<M>
//!   ├─ WaiterTicket
//!   ├─ SubmissionState completion observer
//!   ├─ actor-allocated resource
//!   └─ OperationSpec correlation + typed success decoding
//!         │
//!         ▼
//! ResultSlot<M>
//!   └─ Arc<Mutex<Option<OperationTicket<M>>>>; one result consumer
//!         │
//!         ▼
//! OperationHandoff<M>
//!   ├─ command_id
//!   ├─ OperationControl
//!   └─ ResultSlot<M>
//! ```
//!
//! `RuntimeOwner` creates `OperationHandoff`; the facade moves its three parts
//! into the foreign-facing operation handle. Cloning a `ResultSlot` shares the
//! single-consumer result claim but never removes independent cancellation.

use blehub::CommandId;
use kameo::{actor::ActorRef, error::SendError};

use std::{
    marker::PhantomData,
    sync::{Arc, Mutex as StdMutex, OnceLock},
};
use tokio::{
    sync::oneshot,
    time::{Instant, sleep_until},
};

use super::{
    BlengActor, RuntimeLifecycle, RuntimePhase,
    messages::{OperationSpec, RequestCancellation},
    shared::{OperationSubmission, SubmissionState},
};
use crate::waiter::{WaiterError, WaiterOutcome};

/// Caller-side handle for one actor-owned operation.
///
/// Dropping the oneshot receiver retires caller delivery, but does not claim
/// cancellation of a native command already dispatched.
pub(super) struct WaiterTicket {
    id: CommandId,
    result_rx: oneshot::Receiver<WaiterOutcome>,
    actor: ActorRef<BlengActor>,
    lifecycle: Arc<RuntimeLifecycle>,
    preserve_completed_result_on_close: bool,
    submission: Option<Arc<SubmissionState>>,
}

impl WaiterTicket {
    pub(super) fn new(
        id: CommandId,
        result_rx: oneshot::Receiver<WaiterOutcome>,
        actor: ActorRef<BlengActor>,
        lifecycle: Arc<RuntimeLifecycle>,
    ) -> Self {
        Self {
            id,
            result_rx,
            actor,
            lifecycle,
            preserve_completed_result_on_close: false,
            submission: None,
        }
    }

    /// Delivery retirement is receiver closure, independent of mailbox capacity.
    /// Native reservation and explicit cancellation remain actor-owned.
    fn finish_timeout(&self) -> WaiterOutcome {
        self.lifecycle.require_open()?;
        Err(WaiterError::Timeout)
    }

    pub(super) async fn wait_until(mut self, deadline: Instant) -> WaiterOutcome {
        let mut phase_rx = self.lifecycle.subscribe_phase();
        let timer = sleep_until(deadline);
        tokio::pin!(timer);
        loop {
            // A typed handle retains actor settlement independently of when its
            // caller claims it. Shutdown still fails an unsettled waiter Closed.
            if self.preserve_completed_result_on_close && Instant::now() < deadline {
                match self.result_rx.try_recv() {
                    Ok(result) => {
                        if self.lifecycle.phase() == RuntimePhase::Faulted {
                            return Err(WaiterError::TransportFault);
                        }
                        return result;
                    }
                    Err(oneshot::error::TryRecvError::Closed) => {
                        self.lifecycle.require_open()?;
                        return Err(WaiterError::TransportFault);
                    }
                    Err(oneshot::error::TryRecvError::Empty) => {}
                }
            }
            // The close notification can beat actor teardown's result send.
            // Locally settled or actor-confirmed completed typed operations can
            // await that send; unfinished work sees Closed, and faults always win.
            let completed_before_close = self.preserve_completed_result_on_close
                && self.lifecycle.phase() == RuntimePhase::Closed
                && self
                    .submission
                    .as_ref()
                    .is_some_and(|state| state.is_completed());
            if !completed_before_close {
                self.lifecycle.require_open()?;
            }
            if Instant::now() >= deadline {
                return self.finish_timeout();
            }
            tokio::select! {
                biased;
                result = &mut self.result_rx => {
                    if !self.preserve_completed_result_on_close || result.is_err()
                        || self.lifecycle.phase() == RuntimePhase::Faulted
                    {
                        self.lifecycle.require_open()?;
                    }
                    return result.unwrap_or(Err(WaiterError::TransportFault));
                }
                _ = &mut timer => {
                    return self.finish_timeout();
                }
                _ = phase_rx.changed() => {
                    // Lifecycle transitions are wake-ups; the loop rechecks
                    // RuntimePhase at the top and returns the public error.
                }
            }
        }
    }
}

/// Private control capability shared by typed operation handles.
/// It contains no result receiver. Local submission arbitration and actor-native
/// control can be accessed without taking the single-consumer wait result.
#[derive(Clone)]
pub(crate) struct OperationControl {
    pub(super) actor: ActorRef<BlengActor>,
    pub(super) lifecycle: Arc<RuntimeLifecycle>,
    pub(super) submission: Option<Arc<SubmissionState>>,
}

impl OperationControl {
    pub(crate) async fn cancel(&self, command_id: CommandId) -> Result<(), WaiterError> {
        self.lifecycle.require_open()?;
        if let Some(state) = &self.submission {
            if state.is_locally_settled() {
                return Err(WaiterError::OperationAlreadyCompleted(command_id));
            }
            if state.try_cancel_before_claim() {
                return Ok(());
            }
            // Rejection or another cancellation may win after the first check.
            // Both are local terminal outcomes, not an actor/native lookup.
            if state.is_locally_settled() {
                return Err(WaiterError::OperationAlreadyCompleted(command_id));
            }
        }
        let mut receiver = match self.actor.ask(RequestCancellation { command_id }).await {
            Ok(receiver) => receiver,
            Err(SendError::HandlerError(error)) => return Err(error),
            Err(_) => {
                self.lifecycle.require_open()?;
                self.lifecycle.fault();
                return Err(WaiterError::TransportFault);
            }
        };
        let mut phase_rx = self.lifecycle.subscribe_phase();
        loop {
            if let Some(result) = receiver.borrow_and_update().clone() {
                return result;
            }
            self.lifecycle.require_open()?;
            tokio::select! {
                result = receiver.changed() => {
                    if result.is_err() {
                        self.lifecycle.require_open()?;
                        return Err(WaiterError::TransportFault);
                    }
                }
                result = phase_rx.changed() => {
                    if result.is_err() {
                        return Err(WaiterError::TransportFault);
                    }
                }
            }
        }
    }
}

/// Internal typed ticket assembled after submission claim.
/// It owns caller delivery until `ResultSlot` takes the ticket, and decodes
/// the actor's `WaiterSuccess` with the operation's correlation and resource.
pub(super) struct OperationTicket<M: OperationSpec> {
    waiter: WaiterTicket,
    resource: Arc<OnceLock<M::Resource>>,
    correlation: M::Correlation,
    marker: PhantomData<fn() -> M>,
}

impl<M: OperationSpec> OperationTicket<M> {
    pub(super) fn new(
        mut waiter: WaiterTicket,
        submission: OperationSubmission<M::Resource>,
        correlation: M::Correlation,
    ) -> Self {
        waiter.submission = Some(Arc::clone(&submission.state));
        Self {
            waiter,
            resource: submission.resource,
            correlation,
            marker: PhantomData,
        }
    }

    pub(super) fn into_handoff(
        mut self,
        preserve_completed_result_on_close: bool,
    ) -> OperationHandoff<M> {
        self.waiter.preserve_completed_result_on_close = preserve_completed_result_on_close;
        let command_id = self.waiter.id;
        let submission_state = self
            .waiter
            .submission
            .as_ref()
            .expect("OperationTicket always has submission state")
            .clone();
        let control = OperationControl {
            actor: self.waiter.actor.clone(),
            lifecycle: Arc::clone(&self.waiter.lifecycle),
            submission: self.waiter.submission.clone(),
        };
        OperationHandoff {
            command_id,
            control,
            result_slot: ResultSlot {
                submission_state,
                operation: Arc::new(StdMutex::new(Some(self))),
                #[cfg(test)]
                wait_pause: Arc::new(StdMutex::new(None)),
            },
        }
    }

    async fn wait_until(self, deadline: Instant) -> Result<M::Output, WaiterError> {
        let lifecycle = Arc::clone(&self.waiter.lifecycle);
        let success = self.waiter.wait_until(deadline).await?;
        let Some(output) = M::decode_success(
            self.correlation,
            *self.resource.get().ok_or_else(|| {
                lifecycle.fault();
                WaiterError::TransportFault
            })?,
            success,
        ) else {
            lifecycle.fault();
            return Err(WaiterError::TransportFault);
        };
        Ok(output)
    }
}

/// Single-consumer result ownership for a typed operation. The cancellation
/// control is deliberately outside this slot so consuming or dropping the
/// result receiver never disables explicit native cancellation.
pub(crate) struct ResultSlot<M: OperationSpec> {
    operation: Arc<StdMutex<Option<OperationTicket<M>>>>,
    submission_state: Arc<SubmissionState>,
    #[cfg(test)]
    wait_pause: Arc<StdMutex<Option<tests::WaitPause>>>,
}

#[cfg(test)]
impl<M: OperationSpec> ResultSlot<M> {
    pub(super) async fn pause_wait(&self, after_result: bool) {
        let pause = {
            let mut pause = self.wait_pause.lock().unwrap();
            if pause
                .as_ref()
                .is_some_and(|pause| pause.after_result == after_result)
            {
                pause.take()
            } else {
                None
            }
        };
        if let Some(pause) = pause {
            let _ = pause.entered.send(());
            let _ = pause.release.await;
        }
    }
}

impl<M: OperationSpec> ResultSlot<M> {
    /// Result ownership and native completion are independent states. A timeout
    /// or caller-side cancellation consumes the waiter without proving that the
    /// native operation has ended, so cancellation must remain available.
    pub(crate) fn is_completed(&self) -> bool {
        self.submission_state.is_completed()
    }

    pub(crate) async fn wait_until(&self, deadline: Instant) -> Result<M::Output, WaiterError> {
        let operation = self
            .operation
            .lock()
            .map_err(|_| WaiterError::TransportFault)?
            .take()
            .ok_or(WaiterError::OperationResultConsumed)?;
        #[cfg(test)]
        self.pause_wait(false).await;
        let result = operation.wait_until(deadline).await;
        #[cfg(test)]
        self.pause_wait(true).await;
        result
    }
}

impl<M: OperationSpec> Clone for ResultSlot<M> {
    fn clone(&self) -> Self {
        Self {
            operation: Arc::clone(&self.operation),
            submission_state: self.submission_state.clone(),
            #[cfg(test)]
            wait_pause: Arc::clone(&self.wait_pause),
        }
    }
}

/// One-way runtime-to-facade handoff for a submitted operation.
///
/// The runtime groups the stable command identity, independent cancellation
/// control, and single-consumer result slot here so they cross the boundary as
/// one value. The facade consumes this handoff with `into_parts`; the runtime
/// does not retain a second owner of these facade-facing handles.
pub(crate) struct OperationHandoff<M: OperationSpec> {
    result_slot: ResultSlot<M>,
    control: OperationControl,
    command_id: CommandId,
}

impl<M: OperationSpec> OperationHandoff<M> {
    #[cfg(test)]
    pub(super) fn result_slot(&self) -> &ResultSlot<M> {
        &self.result_slot
    }

    pub(crate) fn into_parts(self) -> (CommandId, OperationControl, ResultSlot<M>) {
        (self.command_id, self.control, self.result_slot)
    }
}

#[cfg(test)]
#[path = "tests/operation.rs"]
mod tests;
