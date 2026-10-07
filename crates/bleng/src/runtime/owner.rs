//! Runtime owner, operation admission, and caller-side waiter tickets.
//!
//! Operations share one absolute deadline: begin handoff -> wait for evidence ->
//! validate the typed result. Timeout/cancellation retires caller delivery only.
//!
//! One coordinator is spawned at construction, independently of close callers:
//! gate leaves Open (or actor exits) -> stop/join actor -> drop actor state ->
//! await native cleanup -> publish the retained shutdown result. Joining the
//! actual actor task, not merely waiting for mailbox closure, is the teardown
//! barrier. Drop remains a non-awaiting fallback, not a successful close result.

use std::{
    io,
    sync::{
        Arc,
        atomic::{AtomicU8, Ordering},
    },
    time::Duration,
};

use crate::{
    cleanup::CleanupError,
    waiter::{WaiterError, WaiterSuccess},
};
use blehub::{
    AdapterSnapshot, BleBackend, BleEvent, CommandId, ConnectionId, GattCharacteristicId,
    GattDatabase, GattSessionId, NotificationMode, NotificationModeRequest, PeripheralId,
    ScanOptions, ScanSessionId, SubscriptionId, WriteType,
};
use kameo::{
    actor::{ActorRef, PreparedActor},
    error::SendError,
    mailbox,
    message::Message,
};
use scopeguard::ScopeGuard;
use tokio::{
    runtime::{Builder, Runtime},
    sync::{Mutex, mpsc, oneshot, watch},
    time::{Instant, sleep_until, timeout_at},
};

use super::{
    AssemblyError, BackendPort, BlengActor, RuntimeLifecycle, RuntimePhase,
    messages::{
        BeginConnect, BeginDisableNotifications, BeginDisconnect, BeginDiscoverGatt,
        BeginEnableNotifications, BeginInitialize, BeginRead, BeginReply, BeginRssi, BeginScan,
        BeginStop, BeginWrite, OperationResult, RetireWaiter,
    },
};

// Owner/flow tests live inside this privacy boundary; actor-only fixtures and
// assertions remain in actor.rs's test children. No production field is exposed.
#[cfg(test)]
#[path = "tests/mod.rs"]
mod tests;

/// Bounded mailbox for commands, ingress events, and lifecycle messages.
/// A full mailbox is a transport fault rather than permission to lose work.
const ACTOR_MAILBOX_CAPACITY: usize = 64;
/// Bounded event FIFO; overflow faults the runtime instead of losing events.
const EVENT_CHANNEL_CAPACITY: usize = 64;

/// Errors produced while reading the bounded public event FIFO.
#[derive(Debug, PartialEq, Eq)]
pub(crate) enum EventReadError {
    Timeout,
    Fault,
}

/// Cross-thread competition to begin dispatch. Owner acknowledgement is local
/// to the guard, not another shared phase. The integer representation remains
/// an implementation detail of the atomic.
#[repr(u8)]
#[derive(Clone, Copy, Debug, PartialEq, Eq, derive_more::TryFrom)]
#[try_from(repr)]
enum BeginPhase {
    /// The actor has not yet claimed the dispatch handoff.
    Pending = 0,
    /// The actor claimed dispatch ownership; native entry is not guaranteed.
    Claimed = 1,
    /// The caller dropped or timed out before ownership was acknowledged.
    Cancelled = 2,
}

impl From<BeginPhase> for u8 {
    fn from(phase: BeginPhase) -> Self {
        phase as u8
    }
}

/// Shared caller-to-actor permission to begin dispatch, not a native acknowledgement.
/// The token carries no Hub; the actor acquires its dispatch root before claiming it.
#[derive(Clone)]
pub(super) struct BeginHandoff(Arc<AtomicU8>);

impl BeginHandoff {
    fn new() -> Self {
        Self(Arc::new(AtomicU8::new(BeginPhase::Pending.into())))
    }

    /// Atomically claims the handoff unless the caller has abandoned it.
    /// Success grants dispatch ownership; it does not start native work.
    pub(super) fn try_claim(&self) -> bool {
        self.0
            .compare_exchange(
                BeginPhase::Pending.into(),
                BeginPhase::Claimed.into(),
                Ordering::AcqRel,
                Ordering::Acquire,
            )
            .is_ok()
    }

    /// Abandons the handoff, preventing an unclaimed dispatch from starting.
    /// Returns whether the actor had already claimed it; native work is not cancelled.
    fn abandon(&self) -> bool {
        let previous = self.0.swap(BeginPhase::Cancelled.into(), Ordering::AcqRel);
        // An impossible raw value fails closed as if the actor had claimed dispatch.
        matches!(
            BeginPhase::try_from(previous).unwrap_or(BeginPhase::Claimed),
            BeginPhase::Claimed
        )
    }
}

/// Caller-side handle for one actor-owned operation.
///
/// Dropping a ticket that still owns retirement responsibility retires the
/// waiter, but does not claim cancellation of a native command already dispatched.
struct WaiterTicket {
    id: CommandId,
    result_rx: oneshot::Receiver<Result<WaiterSuccess, WaiterError>>,
    actor: ActorRef<BlengActor>,
    lifecycle: Arc<RuntimeLifecycle>,
    retire_on_drop: bool,
}

impl WaiterTicket {
    fn retire_waiter(&self, timed_out: bool) {
        if self
            .actor
            .tell(RetireWaiter {
                id: self.id,
                timed_out,
            })
            .try_send()
            .is_err()
            && self.lifecycle.is_open()
        {
            self.lifecycle.fault();
        }
    }

    fn disarm(&mut self) {
        self.retire_on_drop = false;
    }

    /// Retirement can itself fault a full/closed mailbox. Recheck the gate so
    /// both an already-expired deadline and the timer wake report that failure.
    fn finish_timeout(&mut self) -> Result<WaiterSuccess, WaiterError> {
        self.retire_waiter(true);
        self.disarm();
        self.lifecycle.require_open()?;
        Err(WaiterError::Timeout)
    }

    async fn wait_until(mut self, deadline: Instant) -> Result<WaiterSuccess, WaiterError> {
        let mut phase_rx = self.lifecycle.subscribe_phase();
        let timer = sleep_until(deadline);
        tokio::pin!(timer);
        loop {
            self.lifecycle.require_open()?;
            if Instant::now() >= deadline {
                return self.finish_timeout();
            }
            tokio::select! {
                biased;
                result = &mut self.result_rx => {
                    self.disarm();
                    self.lifecycle.require_open()?;
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

impl Drop for WaiterTicket {
    fn drop(&mut self) {
        if self.retire_on_drop && self.lifecycle.is_open() {
            self.retire_waiter(false);
        }
    }
}

/// Owns the Tokio runtime and actor for one BLeng instance.
///
/// Each instance builds its own multithreaded runtime with timers enabled;
/// it neither borrows the caller's runtime nor uses a process-global executor.
/// Facade operations use its explicit handle, so another runtime can drive the
/// caller's async wait without becoming the executor for BLeng operations.
/// The public facade holds this owner behind `Arc`; foreign callers interact
/// only through async methods and never receive Tokio or Kameo types.
pub(crate) struct RuntimeOwner {
    // Runtime ownership keeps the actor executor alive for this BLeng instance.
    runtime: Option<Runtime>,
    actor: ActorRef<BlengActor>,
    lifecycle: Arc<RuntimeLifecycle>,
    // One bounded FIFO is intentionally single-consumer. Concurrent next_event
    // calls serialize on this guard and divide the stream; they do not broadcast.
    event_rx: Mutex<mpsc::Receiver<BleEvent>>,
    // Retained outcome from the single coordinator; all close callers share it.
    shutdown_result_tx: watch::Sender<Option<Result<(), CleanupError>>>,
}

impl RuntimeOwner {
    pub(crate) fn new() -> io::Result<Self> {
        let runtime = Builder::new_multi_thread().enable_time().build()?;
        let (event_tx, event_rx) = mpsc::channel(EVENT_CHANNEL_CAPACITY);
        let (shutdown_result_tx, _) = watch::channel(None);
        let lifecycle = Arc::new(RuntimeLifecycle::new());
        let prepared = PreparedActor::new(mailbox::bounded(ACTOR_MAILBOX_CAPACITY));
        let actor = prepared.actor_ref().clone();
        let mut actor_task = {
            let _runtime_guard = runtime.enter();
            prepared.spawn(BlengActor::new(event_tx, Arc::clone(&lifecycle)))
        };
        // One coordinator owns teardown independently of all close callers.
        // It retains no RuntimeOwner, so it cannot keep the owner/runtime alive.
        runtime.handle().spawn({
            let actor = actor.clone();
            let lifecycle = Arc::clone(&lifecycle);
            let shutdown_result_tx = shutdown_result_tx.clone();
            async move {
                let mut phase_rx = lifecycle.subscribe_phase();
                let actor_result = tokio::select! {
                    // Consume unexpected completion even while the gate is Open:
                    // the returned actor's Drop faults it and retires waiters.
                    result = &mut actor_task => result,
                    _ = async {
                        // Do not retain watch::Ref across shutdown awaits.
                        let _ = phase_rx.wait_for(|phase| *phase != RuntimePhase::Open).await;
                    } => {
                        lifecycle.start_cleanup();
                        let _ = actor.stop_gracefully().await;
                        actor_task.await
                    }
                };
                let actor_stopped = match actor_result {
                    Ok(Ok((actor, _reason))) => {
                        // Joining covers on_stop/unregister, unlike mailbox closure.
                        // The returned actor still owns state until explicitly dropped.
                        drop(actor);
                        true
                    }
                    _ => {
                        lifecycle.fault();
                        false
                    }
                };
                lifecycle.start_cleanup();
                let cleanup_result = lifecycle.wait_for_cleanup().await;
                let outcome = if actor_stopped {
                    cleanup_result
                } else {
                    Err(CleanupError::WorkerStopped)
                };
                shutdown_result_tx.send_replace(Some(outcome));
            }
        });
        Ok(Self {
            runtime: Some(runtime),
            actor,
            lifecycle,
            event_rx: Mutex::new(event_rx),
            shutdown_result_tx,
        })
    }

    pub(crate) async fn close(&self) -> Result<(), CleanupError> {
        let mut shutdown_result_rx = self.shutdown_result_tx.subscribe();
        self.lifecycle.close();
        self.lifecycle.start_cleanup();

        // Every caller awaits the same outcome; no caller owns the shutdown
        // future, and a later close can still observe cleanup errors.
        let outcome = shutdown_result_rx
            .wait_for(Option::is_some)
            .await
            .map_err(|_| CleanupError::WorkerStopped)?;
        outcome.clone().unwrap_or(Err(CleanupError::WorkerStopped))
    }

    fn ticket(
        &self,
        id: CommandId,
        result_rx: oneshot::Receiver<Result<WaiterSuccess, WaiterError>>,
    ) -> WaiterTicket {
        WaiterTicket {
            id,
            result_rx,
            actor: self.actor.clone(),
            lifecycle: Arc::clone(&self.lifecycle),
            retire_on_drop: true,
        }
    }

    /// Runs admission and evidence waiting under one absolute deadline.
    /// `make_message` builds a Begin message; its actor reply transfers handoff metadata,
    /// not BLE completion. `OperationResult` validates and extracts the typed success.
    async fn run_operation<M: OperationResult>(
        &self,
        timeout: Duration,
        make_message: impl FnOnce(BeginHandoff) -> M,
    ) -> Result<M::Output, WaiterError>
    where
        BlengActor: Message<M, Reply = Result<BeginReply<M::Resource>, WaiterError>>,
    {
        // Use the same dispatch gate as the actor, but retain no Hub across
        // mailbox waiting. The actor reacquires the root before handoff.
        drop(self.lifecycle.hub_for_dispatch()?);
        let deadline = Instant::now() + timeout;
        let lifecycle = Arc::clone(&self.lifecycle);
        // Generic RAII protects the reply handoff; the token still owns atomic
        // arbitration. The guard captures lifecycle only, never RuntimeOwner.
        let handoff = scopeguard::guard(BeginHandoff::new(), move |handoff| {
            let was_claimed = handoff.abandon();
            if was_claimed {
                // Native dispatch may have begun before caller ownership arrived.
                lifecycle.fault();
            }
        });
        let message = make_message(handoff.clone());
        // Retain only correlation IDs while Kameo owns the request and its payload.
        let correlation = message.correlation();
        let reply = match timeout_at(deadline, async { self.actor.ask(message).await }).await {
            Ok(Ok(reply)) => reply,
            Ok(Err(SendError::HandlerError(error))) => return Err(error),
            Ok(Err(_)) => {
                self.lifecycle.fault();
                return Err(match self.lifecycle.phase() {
                    RuntimePhase::Closed => WaiterError::Closed,
                    _ => WaiterError::TransportFault,
                });
            }
            Err(_) => {
                // Abandonment can fault the lifecycle; classify the error afterwards.
                drop(handoff);
                return Err(match self.lifecycle.phase() {
                    RuntimePhase::Closed => WaiterError::Closed,
                    RuntimePhase::Faulted => WaiterError::TransportFault,
                    _ => WaiterError::Timeout,
                });
            }
        };
        // Receiving the reply transfers ownership. Defuse the guard and release
        // its token/lifecycle roots before waiting for operation evidence.
        drop(ScopeGuard::into_inner(handoff));
        let success = self
            .ticket(reply.command_id, reply.result_rx)
            .wait_until(deadline)
            .await?;
        M::decode_success(correlation, reply.resource, success).ok_or_else(|| {
            self.lifecycle.fault();
            WaiterError::TransportFault
        })
    }

    pub(crate) async fn initialize(
        &self,
        timeout: Duration,
    ) -> Result<AdapterSnapshot, WaiterError> {
        self.run_operation(timeout, |handoff| BeginInitialize { handoff })
            .await
    }

    pub(crate) async fn start_scan(
        &self,
        options: ScanOptions,
        timeout: Duration,
    ) -> Result<ScanSessionId, WaiterError> {
        self.run_operation(timeout, |handoff| BeginScan { handoff, options })
            .await
    }

    pub(crate) async fn connect(
        &self,
        peripheral_id: PeripheralId,
        timeout: Duration,
    ) -> Result<ConnectionId, WaiterError> {
        self.run_operation(timeout, |handoff| BeginConnect {
            handoff,
            peripheral_id,
        })
        .await
    }

    pub(crate) async fn disconnect(
        &self,
        connection: ConnectionId,
        timeout: Duration,
    ) -> Result<(), WaiterError> {
        self.run_operation(timeout, |handoff| BeginDisconnect {
            handoff,
            connection,
        })
        .await
    }

    pub(crate) async fn discover_gatt(
        &self,
        connection: ConnectionId,
        timeout: Duration,
    ) -> Result<GattDatabase, WaiterError> {
        self.run_operation(timeout, |handoff| BeginDiscoverGatt {
            handoff,
            connection,
        })
        .await
    }

    pub(crate) async fn read_rssi(
        &self,
        connection: ConnectionId,
        timeout: Duration,
    ) -> Result<i16, WaiterError> {
        self.run_operation(timeout, |handoff| BeginRssi {
            handoff,
            connection,
        })
        .await
    }

    pub(crate) async fn read_characteristic(
        &self,
        connection: ConnectionId,
        session: GattSessionId,
        characteristic: GattCharacteristicId,
        timeout: Duration,
    ) -> Result<Vec<u8>, WaiterError> {
        self.run_operation(timeout, |handoff| BeginRead {
            handoff,
            connection,
            session,
            characteristic,
        })
        .await
    }

    pub(crate) async fn write_characteristic(
        &self,
        connection: ConnectionId,
        session: GattSessionId,
        characteristic: GattCharacteristicId,
        value: Vec<u8>,
        write_type: WriteType,
        timeout: Duration,
    ) -> Result<(), WaiterError> {
        self.run_operation(timeout, |handoff| BeginWrite {
            handoff,
            connection,
            session,
            characteristic,
            value,
            write_type,
        })
        .await
    }

    pub(crate) async fn stop_scan(
        &self,
        session: ScanSessionId,
        timeout: Duration,
    ) -> Result<(), WaiterError> {
        self.run_operation(timeout, |handoff| BeginStop { handoff, session })
            .await
    }

    pub(crate) async fn enable_notifications(
        &self,
        connection: ConnectionId,
        session: GattSessionId,
        characteristic: GattCharacteristicId,
        mode: NotificationModeRequest,
        timeout: Duration,
    ) -> Result<(SubscriptionId, Option<NotificationMode>), WaiterError> {
        self.run_operation(timeout, |handoff| BeginEnableNotifications {
            handoff,
            connection,
            session,
            characteristic,
            mode,
        })
        .await
    }

    pub(crate) async fn disable_notifications(
        &self,
        connection: ConnectionId,
        session: GattSessionId,
        subscription: SubscriptionId,
        timeout: Duration,
    ) -> Result<(), WaiterError> {
        self.run_operation(timeout, |handoff| BeginDisableNotifications {
            handoff,
            connection,
            session,
            subscription,
        })
        .await
    }

    pub(crate) fn backend_port(&self) -> Arc<BackendPort> {
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
            let mut event_rx = self.event_rx.lock().await;
            let mut phase_rx = self.lifecycle.subscribe_phase();
            loop {
                match self.lifecycle.phase() {
                    RuntimePhase::Faulted => return Err(EventReadError::Fault),
                    RuntimePhase::Closed => return Ok(None),
                    RuntimePhase::Open => {}
                }
                tokio::select! {
                    event = event_rx.recv() => {
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
        self.lifecycle.close();
        self.lifecycle.start_cleanup();
        // `Drop` cannot await Kameo shutdown; kill is the non-blocking final
        // fallback. It is harmless if the actor already stopped normally.
        self.actor.kill();
        if let Some(runtime) = self.runtime.take() {
            runtime.shutdown_background();
        }
    }
}
