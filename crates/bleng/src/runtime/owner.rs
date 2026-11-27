//! Runtime owner, operation admission, and caller-side waiter tickets.
//!
//! Begin synchronously enqueues a bounded entry and progresses without wait().
//! Convenience submission/wait shares one absolute deadline; typed wait has an
//! independent deadline. Delivery retirement never requests native cancellation.
//!
//! Closing has two separate watch channels, not one stream of BLE events:
//! - `RuntimeLifecycle` publishes phase changes (Open -> Closed/Faulted). This
//!   wakes the coordinator; it does not mean teardown has finished.
//! - The coordinator, spawned once in `new()`, stops/joins the actor, drops its
//!   returned state, awaits `HubCleanup`, then publishes the retained
//!   `shutdown_result`. Each `close()` clones the receiver and sees that same
//!   final result, even if another close waiter is dropped or the result was
//!   published earlier. If the coordinator exits without publishing, its sole
//!   sender drops and close callers receive WorkerStopped.
//!
//! `HubCleanup` has its own wake-up signal for admission/disposal progress; its
//! locked state decides completion. Separately, the actor mailbox carries
//! commands and ingress, the bounded event FIFO feeds `next_event()`, and each
//! operation's oneshot settles only that operation's `wait()`.
//! Joining the actor task, not merely closing its mailbox, is the teardown
//! barrier. Owner Drop is a non-awaiting fallback, not a successful close result.

use std::{
    future::Future,
    io,
    panic::{AssertUnwindSafe, catch_unwind},
    pin::Pin,
    sync::Arc,
    task::{Context, Poll},
    time::Duration,
};

use crate::{
    cleanup::CleanupError,
    waiter::{WaiterError, WaiterSender},
};
use blehub::{BleBackend, BleEvent, CommandId};
use kameo::{
    actor::{ActorRef, PreparedActor},
    error::{ActorStopReason, PanicError, SendError},
    mailbox,
    message::Message,
};
use tokio::{
    runtime::{Builder, Runtime},
    sync::{Mutex, mpsc, watch},
    task::JoinHandle,
};
use tokio_util::task::AbortOnDropHandle;

use super::{
    AssemblyError, BackendPort, BlengActor, RuntimeLifecycle, RuntimePhase,
    messages::OperationSpec,
    operation::{OperationControl, OperationHandoff, OperationTicket, WaiterTicket},
    shared::{IdGenerator, OperationSubmission},
};

// Owner/flow tests live inside this privacy boundary; actor-only fixtures and
// assertions remain in actor.rs's test children. No production field is exposed.
#[cfg(test)]
#[path = "tests/mod.rs"]
mod tests;

/// Bounded mailbox for commands, ingress events, and lifecycle messages.
/// New submissions may be rejected; losing already-accepted ingress faults the runtime.
const ACTOR_MAILBOX_CAPACITY: usize = 64;
/// Bounded event FIFO; overflow faults the runtime instead of losing events.
const EVENT_CHANNEL_CAPACITY: usize = 64;

/// Errors produced while reading the bounded public event FIFO.
#[derive(Debug, PartialEq, Eq)]
pub(crate) enum EventReadError {
    Timeout,
    Fault,
}

/// Private task failure is distinct from the result returned by a BLE operation.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum TaskFailure {
    Panicked,
    Cancelled,
    WorkerStopped,
}

/// Faults the owner if a caller-bound future panics while being dropped.
/// Tokio may drop it after the caller abandoned its JoinHandle, so no waiter
/// remains to inspect a task result. The pinned allocation avoids unsafe projection.
/// A second panic during the future's own poll unwind can still abort the process
/// before the outer poll boundary has a chance to catch the first one.
struct FaultOnTaskDrop<F: Future> {
    future: Option<Pin<Box<F>>>,
    lifecycle: Arc<RuntimeLifecycle>,
}

impl<F: Future> FaultOnTaskDrop<F> {
    fn poll_catching_panic(
        &mut self,
        cx: &mut Context<'_>,
    ) -> Poll<Result<F::Output, TaskFailure>> {
        match catch_unwind(AssertUnwindSafe(|| {
            self.future.as_mut().expect("task future").as_mut().poll(cx)
        })) {
            Ok(Poll::Ready(output)) => Poll::Ready(Ok(output)),
            Ok(Poll::Pending) => Poll::Pending,
            Err(_) => Poll::Ready(Err(TaskFailure::Panicked)),
        }
    }

    /// Drops the retained future, faulting the owner and reporting a destructor panic.
    /// A destructor failure must override successful polling before caller delivery.
    fn drop_future_catching_panic(&mut self) -> Result<(), TaskFailure> {
        if catch_unwind(AssertUnwindSafe(|| drop(self.future.take()))).is_err() {
            self.lifecycle.fault();
            return Err(TaskFailure::Panicked);
        }
        Ok(())
    }
}

impl<F: Future> Drop for FaultOnTaskDrop<F> {
    fn drop(&mut self) {
        // The helper already faults the lifecycle on error; Drop cannot propagate it.
        let _ = self.drop_future_catching_panic();
    }
}

/// Coordinates actor teardown and native cleanup independently of close callers.
/// If its task is aborted or panics, even before the first poll, Drop faults the
/// lifecycle and kills the actor. The task's sole result sender then drops,
/// waking close callers with WorkerStopped instead of leaving them waiting.
struct ShutdownCoordinator {
    actor: ActorRef<BlengActor>,
    lifecycle: Arc<RuntimeLifecycle>,
    finished: bool,
}

impl ShutdownCoordinator {
    async fn coordinate_shutdown(
        self,
        mut actor_task: JoinHandle<Result<(BlengActor, ActorStopReason), PanicError>>,
        sender: watch::Sender<Option<Result<(), CleanupError>>>,
    ) {
        let mut phase_rx = self.lifecycle.subscribe_phase();
        let actor_join_outcome = tokio::select! {
            // Consume unexpected completion even while the gate is Open:
            // the returned actor's Drop faults it and retires waiters.
            outcome = &mut actor_task => outcome,
            _ = async {
                // Do not retain watch::Ref across shutdown awaits.
                let _ = phase_rx.wait_for(|phase| *phase != RuntimePhase::Open).await;
            } => {
                self.lifecycle.start_cleanup();
                let _ = self.actor.stop_gracefully().await;
                actor_task.await
            }
        };
        let actor_stopped = match actor_join_outcome {
            Ok(Ok((actor, reason))) => {
                // Joining covers on_stop/unregister, unlike mailbox closure.
                // The returned actor still owns state until explicitly dropped.
                drop(actor);
                // Kameo returns Ok((actor, reason)) even for a handler
                // panic, and records on_stop failures separately.
                !matches!(reason, ActorStopReason::Panicked(_))
                    && matches!(self.actor.get_shutdown_result(), Some(Ok(_)))
            }
            _ => {
                self.lifecycle.fault();
                false
            }
        };
        self.lifecycle.start_cleanup();
        // Even a failed actor join must wait for native cleanup before publishing.
        let cleanup_outcome = self.lifecycle.wait_for_cleanup().await;
        if !actor_stopped {
            self.finish(Err(CleanupError::WorkerStopped), sender);
            return;
        }
        self.finish(cleanup_outcome, sender);
    }

    fn finish(
        mut self,
        shutdown_outcome: Result<(), CleanupError>,
        sender: watch::Sender<Option<Result<(), CleanupError>>>,
    ) {
        self.finished = true;
        sender.send_replace(Some(shutdown_outcome));
    }
}

impl Drop for ShutdownCoordinator {
    fn drop(&mut self) {
        if !self.finished {
            self.lifecycle.fault();
            self.lifecycle.start_cleanup();
            self.actor.kill();
        }
    }
}

/// Owns the Tokio runtime and actor for one BLeng instance.
///
/// Each instance builds its own multithreaded runtime with timers enabled;
/// it neither borrows the caller's runtime nor uses a process-global executor.
/// Facade operations run through this owner, so another runtime can drive the
/// caller's async wait without becoming the executor for BLeng operations.
/// The public facade holds this owner behind `Arc`; foreign callers interact
/// through synchronous submission and async waiting, never Tokio or Kameo types.
pub(crate) struct RuntimeOwner {
    // Runtime ownership keeps the actor executor alive for this BLeng instance.
    runtime: Option<Runtime>,
    actor: ActorRef<BlengActor>,
    command_ids: Arc<IdGenerator>,
    lifecycle: Arc<RuntimeLifecycle>,
    // One bounded FIFO is intentionally single-consumer. Concurrent next_event
    // calls serialize on this guard and divide the stream; they do not broadcast.
    public_event_rx: Mutex<mpsc::Receiver<BleEvent>>,
    // Only the coordinator holds the sender: its unexpected exit closes this
    // channel and wakes every close caller with WorkerStopped.
    shutdown_result_rx: watch::Receiver<Option<Result<(), CleanupError>>>,
    #[cfg(test)]
    coordinator_abort: tokio::task::AbortHandle,
}

impl RuntimeOwner {
    pub(crate) fn new() -> io::Result<Self> {
        let runtime = Builder::new_multi_thread().enable_time().build()?;
        let (public_event_tx, public_event_rx) = mpsc::channel(EVENT_CHANNEL_CAPACITY);
        let (shutdown_result_tx, shutdown_result_rx) = watch::channel(None);
        let lifecycle = Arc::new(RuntimeLifecycle::new());
        let command_ids = Arc::new(IdGenerator::default());
        let prepared = PreparedActor::new(mailbox::bounded(ACTOR_MAILBOX_CAPACITY));
        let actor = prepared.actor_ref().clone();
        let actor_task = {
            let _runtime_guard = runtime.enter();
            prepared.spawn(BlengActor::with_ids(
                public_event_tx,
                Arc::clone(&lifecycle),
                Arc::clone(&command_ids),
            ))
        };
        // One coordinator owns teardown independently of all close callers.
        // It retains no RuntimeOwner, so it cannot keep the owner/runtime alive.
        let guard = ShutdownCoordinator {
            actor: actor.clone(),
            lifecycle: Arc::clone(&lifecycle),
            finished: false,
        };
        let coordinator_task = runtime
            .handle()
            .spawn(guard.coordinate_shutdown(actor_task, shutdown_result_tx));
        #[cfg(not(test))]
        drop(coordinator_task); // Detach: close callers observe the watch channel.
        Ok(Self {
            runtime: Some(runtime),
            actor,
            command_ids,
            lifecycle,
            public_event_rx: Mutex::new(public_event_rx),
            shutdown_result_rx,
            #[cfg(test)]
            coordinator_abort: coordinator_task.abort_handle(),
        })
    }

    /// Nonblocking lifecycle request; the existing coordinator owns all teardown.
    pub(crate) fn request_close(&self) {
        self.lifecycle.request_close();
    }

    pub(crate) async fn close(&self) -> Result<(), CleanupError> {
        let mut shutdown_result_rx = self.shutdown_result_rx.clone();
        self.request_close();

        // The watch receiver observes the coordinator's retained result, not
        // lifecycle phase changes. No close caller owns or cancels teardown.
        let outcome = shutdown_result_rx
            .wait_for(Option::is_some)
            .await
            .map_err(|_| CleanupError::WorkerStopped)?;
        outcome.clone().unwrap_or(Err(CleanupError::WorkerStopped))
    }

    /// Synchronous bounded enqueue. No task, native entry or actor admission runs
    /// on the caller. A full mailbox rejects only this submission, not the Hub.
    pub(crate) fn submit_operation<M: OperationSpec>(
        &self,
        preserve_completed_result_on_close: bool,
        make_message: impl FnOnce(OperationSubmission<M::Resource>) -> M,
    ) -> Result<OperationHandoff<M>, WaiterError>
    where
        BlengActor: Message<M>,
    {
        self.lifecycle.require_submission_ready()?;
        let id = self.command_ids.allocate()?.into();
        let (sender, receiver) = WaiterSender::new();
        let submission = OperationSubmission::new(id, sender);
        let message = make_message(submission.clone());
        let correlation = message.correlation();
        match self.actor.tell(message).try_send() {
            Ok(()) => {}
            Err(SendError::MailboxFull(_)) => {
                self.lifecycle.require_open()?;
                return Err(WaiterError::SubmissionQueueFull);
            }
            Err(_) => {
                self.lifecycle.require_open()?;
                self.lifecycle.fault();
                return Err(WaiterError::TransportFault);
            }
        }
        let waiter = WaiterTicket::new(
            id,
            receiver,
            self.actor.clone(),
            Arc::clone(&self.lifecycle),
        );
        Ok(OperationTicket::new(waiter, submission, correlation)
            .into_handoff(preserve_completed_result_on_close))
    }

    /// Linearizes cancellation on the actor. This control request has no
    /// independent waiter or timeout and does not imply native completion.
    pub(crate) async fn cancel_operation(&self, command_id: CommandId) -> Result<(), WaiterError> {
        OperationControl {
            actor: self.actor.clone(),
            lifecycle: Arc::clone(&self.lifecycle),
            submission: None,
        }
        .cancel(command_id)
        .await
    }

    fn backend_port(&self) -> Arc<BackendPort> {
        Arc::new(BackendPort::new(
            self.actor.clone(),
            Arc::clone(&self.lifecycle),
        ))
    }

    pub(crate) async fn assemble_backend(
        &self,
        backend: Arc<dyn BleBackend>,
    ) -> Result<(), AssemblyError> {
        let port = self.backend_port();
        let handle = self.runtime.as_ref().expect("runtime is open").handle();
        match handle
            .spawn_blocking(move || port.assemble_backend(backend))
            .await
        {
            Ok(result) => result,
            Err(_) => {
                self.lifecycle.fault();
                Err(AssemblyError::WorkerStopped)
            }
        }
    }

    pub(crate) async fn next_event(
        &self,
        timeout: Duration,
    ) -> Result<Option<BleEvent>, EventReadError> {
        tokio::time::timeout(timeout, async {
            let mut public_event_rx = self.public_event_rx.lock().await;
            let mut phase_rx = self.lifecycle.subscribe_phase();
            loop {
                match self.lifecycle.phase() {
                    RuntimePhase::Faulted => return Err(EventReadError::Fault),
                    RuntimePhase::Closed => return Ok(None),
                    RuntimePhase::Open => {}
                }
                tokio::select! {
                    event = public_event_rx.recv() => {
                        match self.lifecycle.phase() {
                            RuntimePhase::Faulted => return Err(EventReadError::Fault),
                            RuntimePhase::Closed => return Ok(None),
                            RuntimePhase::Open => {}
                        }
                        return Ok(event);
                    }
                    _ = phase_rx.changed() => {}
                }
            }
        })
        .await
        .map_err(|_| EventReadError::Timeout)?
    }

    /// Runs a caller-bound task on this instance's runtime. Dropping the wait
    /// aborts only this task, not an already-dispatched native BLE command.
    /// A single task panic in poll or drop faults the owner even if its caller
    /// already dropped the wait.
    pub(crate) async fn run_task<T: Send + 'static>(
        self: &Arc<Self>,
        future: impl Future<Output = T> + Send + 'static,
    ) -> Result<T, TaskFailure> {
        let owner = Arc::clone(self);
        let mut guarded = FaultOnTaskDrop {
            future: Some(Box::pin(future)),
            lifecycle: Arc::clone(&self.lifecycle),
        };
        let join_outcome = AbortOnDropHandle::new(owner.handle().spawn(async move {
            let task_outcome = std::future::poll_fn(|cx| guarded.poll_catching_panic(cx)).await;
            if task_outcome.is_err() {
                guarded.lifecycle.fault();
            }
            // Future destructors can panic after poll succeeds; that failure wins.
            guarded.drop_future_catching_panic()?;
            task_outcome
        }))
        .await;
        // Keep the runtime alive until the task has finished or been aborted.
        drop(owner);
        match join_outcome {
            Ok(task_outcome) => task_outcome,
            Err(error) => {
                self.lifecycle.fault();
                Err(if error.is_panic() {
                    TaskFailure::Panicked
                } else if error.is_cancelled() {
                    TaskFailure::Cancelled
                } else {
                    TaskFailure::WorkerStopped
                })
            }
        }
    }

    pub(crate) fn handle(&self) -> tokio::runtime::Handle {
        self.runtime
            .as_ref()
            .expect("runtime is open")
            .handle()
            .clone()
    }
}

impl Drop for RuntimeOwner {
    fn drop(&mut self) {
        self.request_close();
        // `Drop` cannot await Kameo shutdown; kill is the non-blocking final
        // fallback. It is harmless if the actor already stopped normally.
        self.actor.kill();
        if let Some(runtime) = self.runtime.take() {
            runtime.shutdown_background();
        }
    }
}
