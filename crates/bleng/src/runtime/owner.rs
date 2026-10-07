//! Runtime owner, operation admission, and caller-side waiter tickets.

use std::{
    collections::HashMap,
    future::Future,
    io,
    sync::{
        Arc,
        atomic::{AtomicBool, AtomicU8, Ordering},
    },
    time::Duration,
};

use crate::{
    cleanup::CleanupError,
    waiter::{WaiterError, WaiterRegistry, WaiterSuccess},
};
use blehub::{
    AdapterSnapshot, BleBackend, BleEvent, BleHub, CommandId, ConnectionId, GattCharacteristicId,
    GattDatabase, GattSessionId, PeripheralId, ScanOptions, ScanSessionId, WriteType,
};
use kameo::actor::Spawn;
use kameo::prelude::ActorRef;
use kameo::{error::SendError, mailbox};
use tokio::{
    runtime::{Builder, Runtime},
    sync::{Mutex, mpsc, oneshot, watch},
    time::{Instant, sleep_until, timeout_at},
};

use super::{
    AssemblyError, BackendPort, BeginConnect, BeginDisconnect, BeginDiscoverGatt, BeginInitialize,
    BeginRead, BeginRssi, BeginScan, BeginStop, BeginWrite, BlengActor, RetireWaiter,
    RuntimeLifecycle, RuntimePhase,
};

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

/// Atomic handoff phase for the interval between actor admission and caller
/// ownership. The integer representation is an implementation detail of the
/// atomic; callers use this enum and its transition methods instead.
#[repr(u8)]
#[derive(Clone, Copy, Debug, PartialEq, Eq, derive_more::TryFrom)]
#[try_from(repr)]
enum BeginPhase {
    /// The actor has not yet claimed the dispatch handoff.
    Pending = 0,
    /// The actor has entered the native dispatch boundary.
    Launched = 1,
    /// The caller received the dispatch acknowledgement and owns the waiter.
    Acknowledged = 2,
    /// The caller dropped or timed out before ownership was acknowledged.
    Cancelled = 3,
}

impl From<BeginPhase> for u8 {
    fn from(phase: BeginPhase) -> Self {
        phase as u8
    }
}

#[derive(Clone)]
struct BeginState(Arc<AtomicU8>);

impl BeginState {
    fn new() -> Self {
        Self(Arc::new(AtomicU8::new(BeginPhase::Pending.into())))
    }

    fn try_launch(&self) -> bool {
        self.0
            .compare_exchange(
                BeginPhase::Pending.into(),
                BeginPhase::Launched.into(),
                Ordering::AcqRel,
                Ordering::Acquire,
            )
            .is_ok()
    }

    fn acknowledge(&self) {
        self.0
            .store(BeginPhase::Acknowledged.into(), Ordering::Release);
    }

    fn cancel(&self) -> bool {
        let previous = self.0.swap(BeginPhase::Cancelled.into(), Ordering::AcqRel);
        // An impossible raw value fails closed as if native dispatch had begun.
        matches!(
            BeginPhase::try_from(previous).unwrap_or(BeginPhase::Launched),
            BeginPhase::Launched
        )
    }
}

/// Common caller-owned data carried by every begin message.
///
/// The actor receives the Hub root and the same atomic handoff state as one
/// value, while operation-specific messages only carry their own parameters.
pub(crate) struct BeginContext {
    pub(super) hub: Arc<BleHub>,
    state: BeginState,
}

impl BeginContext {
    fn new(hub: Arc<BleHub>, state: BeginState) -> Self {
        Self { hub, state }
    }

    pub(super) fn try_launch(&self) -> bool {
        self.state.try_launch()
    }
}

struct BeginOperationGuard {
    state: BeginState,
    lifecycle: Arc<RuntimeLifecycle>,
}

impl BeginOperationGuard {
    fn new(lifecycle: Arc<RuntimeLifecycle>) -> Self {
        Self {
            state: BeginState::new(),
            lifecycle,
        }
    }

    fn acknowledged(&self) {
        self.state.acknowledge();
    }

    fn context(&self, hub: Arc<BleHub>) -> BeginContext {
        BeginContext::new(hub, self.state.clone())
    }
}

impl Drop for BeginOperationGuard {
    fn drop(&mut self) {
        if self.state.cancel() {
            // Native dispatch may have begun but its ownership has not reached the caller.
            self.lifecycle.fault();
        }
    }
}

/// Caller-side handle for one actor-owned operation.
///
/// Dropping a ticket that still owns retirement responsibility retires the
/// waiter, but does not claim cancellation of a native command already dispatched.
pub(crate) struct WaiterTicket {
    id: CommandId,
    pub(crate) receiver: oneshot::Receiver<Result<WaiterSuccess, WaiterError>>,
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
            && self.lifecycle.phase() == RuntimePhase::Open
        {
            self.lifecycle.fault();
        }
    }

    fn disarm(&mut self) {
        self.retire_on_drop = false;
    }

    pub(crate) async fn wait_until(
        mut self,
        deadline: Instant,
    ) -> Result<WaiterSuccess, WaiterError> {
        let mut changed = self.lifecycle.subscribe_phase();
        let timer = sleep_until(deadline);
        tokio::pin!(timer);
        loop {
            match self.lifecycle.phase() {
                RuntimePhase::Closed => return Err(WaiterError::Closed),
                RuntimePhase::Faulted => return Err(WaiterError::TransportFault),
                _ => {}
            }
            if Instant::now() >= deadline {
                self.retire_waiter(true);
                self.disarm();
                return Err(WaiterError::Timeout);
            }
            tokio::select! {
                biased;
                result = &mut self.receiver => {
                    self.disarm();
                    return match self.lifecycle.phase() {
                        RuntimePhase::Closed => Err(WaiterError::Closed),
                        RuntimePhase::Faulted => Err(WaiterError::TransportFault),
                        _ => result.unwrap_or(Err(WaiterError::TransportFault)),
                    };
                }
                _ = &mut timer => {
                    self.retire_waiter(true);
                    self.disarm();
                    return match self.lifecycle.phase() {
                        RuntimePhase::Closed => Err(WaiterError::Closed),
                        RuntimePhase::Faulted => Err(WaiterError::TransportFault),
                        _ => Err(WaiterError::Timeout),
                    };
                }
                _ = changed.changed() => {
                    // Lifecycle transitions are wake-ups; the loop rechecks
                    // RuntimePhase at the top and returns the public error.
                }
            }
        }
    }
}

impl Drop for WaiterTicket {
    fn drop(&mut self) {
        if self.retire_on_drop && self.lifecycle.phase() == RuntimePhase::Open {
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
    pub(crate) actor: ActorRef<BlengActor>,
    pub(crate) lifecycle: Arc<RuntimeLifecycle>,
    // One bounded FIFO is intentionally single-consumer. Concurrent next_event
    // calls serialize on this guard and divide the stream; they do not broadcast.
    events: Mutex<mpsc::Receiver<BleEvent>>,
    shutdown_started: AtomicBool,
    pub(crate) shutdown_result: watch::Sender<Option<Result<(), CleanupError>>>,
}

/// Stop the actor after a lifecycle fault without borrowing `RuntimeOwner`.
/// The detached task owns exactly the actor handle and lifecycle receiver it needs.
fn spawn_fault_shutdown(
    handle: &tokio::runtime::Handle,
    actor: ActorRef<BlengActor>,
    mut phase: watch::Receiver<RuntimePhase>,
) {
    handle.spawn(async move {
        loop {
            let current_phase = *phase.borrow_and_update();
            match current_phase {
                RuntimePhase::Faulted => {
                    let _ = actor.stop_gracefully().await;
                    actor.wait_for_shutdown().await;
                    break;
                }
                RuntimePhase::Closed => break,
                RuntimePhase::Open => {}
            }
            if phase.changed().await.is_err() {
                break;
            }
        }
    });
}

/// Wait for the one shared shutdown result. It deliberately takes only the
/// receiver so cancellation of one caller cannot cancel the owner coordinator.
async fn wait_for_shutdown(
    mut result: watch::Receiver<Option<Result<(), CleanupError>>>,
) -> Result<(), CleanupError> {
    loop {
        if let Some(outcome) = result.borrow_and_update().clone() {
            return outcome;
        }
        if result.changed().await.is_err() {
            return Err(CleanupError::WorkerStopped);
        }
    }
}

impl RuntimeOwner {
    pub(crate) fn new() -> io::Result<Self> {
        let runtime = Builder::new_multi_thread().enable_time().build()?;
        let (sender, events) = mpsc::channel(EVENT_CHANNEL_CAPACITY);
        let (shutdown_result, _) = watch::channel(None);
        let lifecycle = Arc::new(RuntimeLifecycle::new());
        let actor = {
            let _runtime_guard = runtime.enter();
            BlengActor::spawn_with_mailbox(
                BlengActor {
                    events: sender,
                    lifecycle: Arc::clone(&lifecycle),
                    waiters: WaiterRegistry::new(),
                    next_id: 1,
                    initialized: false,
                    init: None,
                    scan: None,
                    connections: HashMap::new(),
                },
                mailbox::bounded(ACTOR_MAILBOX_CAPACITY),
            )
        };
        // A transport fault already starts native Hub cleanup, but the actor
        // must also stop without requiring a later explicit close. Otherwise
        // its reservations and waiter registry can remain alive indefinitely
        // while the foreign owner is still retained.
        spawn_fault_shutdown(runtime.handle(), actor.clone(), lifecycle.subscribe_phase());
        Ok(Self {
            runtime: Some(runtime),
            actor,
            lifecycle,
            events: Mutex::new(events),
            shutdown_started: AtomicBool::new(false),
            shutdown_result,
        })
    }

    pub(crate) async fn close(&self) -> Result<(), CleanupError> {
        let result = self.shutdown_result.subscribe();
        self.lifecycle.close();
        self.lifecycle.start_cleanup();
        if !self.shutdown_started.swap(true, Ordering::AcqRel) {
            self.spawn_shutdown_coordinator();
        }
        // Every caller awaits the same outcome; no caller owns the shutdown
        // future, and a later close can still observe cleanup errors.
        wait_for_shutdown(result).await
    }

    /// Spawn exactly one owner-held coordinator. It is deliberately detached
    /// from the caller's future so cancellation of one `close` call cannot
    /// strand actor shutdown or native Hub disposal.
    fn spawn_shutdown_coordinator(&self) {
        let actor = self.actor.clone();
        let lifecycle = Arc::clone(&self.lifecycle);
        let completed = self.shutdown_result.clone();
        let handle = self
            .runtime
            .as_ref()
            .expect("runtime is open")
            .handle()
            .clone();
        handle.spawn(async move {
            let _ = actor.stop_gracefully().await;
            actor.wait_for_shutdown().await;
            let cleanup_result = lifecycle.wait_for_cleanup().await;
            completed.send_replace(Some(cleanup_result));
        });
    }

    pub(super) async fn begin_operation<T, M, F, Fut>(
        &self,
        timeout: Duration,
        request: F,
    ) -> Result<(T, Instant), WaiterError>
    where
        F: FnOnce(ActorRef<BlengActor>, BeginContext) -> Fut,
        Fut: Future<Output = Result<T, SendError<M, WaiterError>>>,
    {
        match self.lifecycle.phase() {
            RuntimePhase::Closed => return Err(WaiterError::Closed),
            RuntimePhase::Faulted => return Err(WaiterError::TransportFault),
            _ => {}
        }
        let hub = self
            .lifecycle
            .hub_for_dispatch()
            .ok_or(WaiterError::BackendNotAssembled)?;
        let deadline = Instant::now() + timeout;
        let guard = BeginOperationGuard::new(Arc::clone(&self.lifecycle));
        let request = request(self.actor.clone(), guard.context(hub));
        let reply = match timeout_at(deadline, request).await {
            Ok(Ok(result)) => result,
            Ok(Err(SendError::HandlerError(error))) => return Err(error),
            Ok(Err(_)) => {
                self.lifecycle.fault();
                return Err(match self.lifecycle.phase() {
                    RuntimePhase::Closed => WaiterError::Closed,
                    _ => WaiterError::TransportFault,
                });
            }
            Err(_) => {
                drop(guard);
                return Err(match self.lifecycle.phase() {
                    RuntimePhase::Closed => WaiterError::Closed,
                    RuntimePhase::Faulted => WaiterError::TransportFault,
                    _ => WaiterError::Timeout,
                });
            }
        };
        guard.acknowledged();
        Ok((reply, deadline))
    }

    pub(crate) fn ticket(
        &self,
        id: CommandId,
        receiver: oneshot::Receiver<Result<WaiterSuccess, WaiterError>>,
    ) -> WaiterTicket {
        WaiterTicket {
            id,
            receiver,
            actor: self.actor.clone(),
            lifecycle: Arc::clone(&self.lifecycle),
            retire_on_drop: true,
        }
    }

    pub(super) async fn wait_for_success<T>(
        &self,
        id: CommandId,
        receiver: oneshot::Receiver<Result<WaiterSuccess, WaiterError>>,
        deadline: Instant,
        accept: impl FnOnce(WaiterSuccess) -> Option<T>,
    ) -> Result<T, WaiterError> {
        let success = self.ticket(id, receiver).wait_until(deadline).await?;
        accept(success).ok_or_else(|| {
            self.lifecycle.fault();
            WaiterError::TransportFault
        })
    }

    pub(crate) async fn initialize(
        &self,
        deadline: Duration,
    ) -> Result<AdapterSnapshot, WaiterError> {
        let ((id, receiver), deadline) = self
            .begin_operation(deadline, move |actor, context| async move {
                actor.ask(BeginInitialize { context }).await
            })
            .await?;
        self.wait_for_success(id, receiver, deadline, |success| match success {
            WaiterSuccess::Initialized(snapshot) => Some(snapshot),
            _ => None,
        })
        .await
    }

    pub(crate) async fn start_scan(
        &self,
        options: ScanOptions,
        deadline: Duration,
    ) -> Result<ScanSessionId, WaiterError> {
        let ((id, session, receiver), deadline) = self
            .begin_operation(deadline, move |actor, context| async move {
                actor.ask(BeginScan { context, options }).await
            })
            .await?;
        self.wait_for_success(id, receiver, deadline, |success| match success {
            WaiterSuccess::ScanStarted(scan) if scan == session => Some(scan),
            _ => None,
        })
        .await
    }

    pub(crate) async fn connect(
        &self,
        peripheral_id: PeripheralId,
        deadline: Duration,
    ) -> Result<ConnectionId, WaiterError> {
        let ((id, connection, receiver), deadline) = self
            .begin_operation(deadline, move |actor, context| async move {
                actor
                    .ask(BeginConnect {
                        context,
                        peripheral_id,
                    })
                    .await
            })
            .await?;
        self.wait_for_success(id, receiver, deadline, |success| match success {
            WaiterSuccess::Connected(found) if found == connection => Some(found),
            _ => None,
        })
        .await
    }

    pub(crate) async fn disconnect(
        &self,
        connection: ConnectionId,
        deadline: Duration,
    ) -> Result<(), WaiterError> {
        let ((id, receiver), deadline) = self
            .begin_operation(deadline, move |actor, context| async move {
                actor
                    .ask(BeginDisconnect {
                        context,
                        connection,
                    })
                    .await
            })
            .await?;
        self.wait_for_success(id, receiver, deadline, |success| match success {
            WaiterSuccess::Disconnected(found) if found == connection => Some(()),
            _ => None,
        })
        .await
    }

    pub(crate) async fn discover_gatt(
        &self,
        connection: ConnectionId,
        deadline: Duration,
    ) -> Result<GattDatabase, WaiterError> {
        let ((id, session, receiver), deadline) = self
            .begin_operation(deadline, move |actor, context| async move {
                actor
                    .ask(BeginDiscoverGatt {
                        context,
                        connection,
                    })
                    .await
            })
            .await?;
        self.wait_for_success(id, receiver, deadline, |success| match success {
            WaiterSuccess::GattDiscovered(database)
                if database.connection_id == connection && database.gatt_session_id == session =>
            {
                Some(database)
            }
            _ => None,
        })
        .await
    }

    pub(crate) async fn read_rssi(
        &self,
        connection: ConnectionId,
        deadline: Duration,
    ) -> Result<i16, WaiterError> {
        let ((id, receiver), deadline) = self
            .begin_operation(deadline, move |actor, context| async move {
                actor
                    .ask(BeginRssi {
                        context,
                        connection,
                    })
                    .await
            })
            .await?;
        self.wait_for_success(id, receiver, deadline, |success| match success {
            WaiterSuccess::RssiRead(found_connection, value) if found_connection == connection => {
                Some(value)
            }
            _ => None,
        })
        .await
    }

    pub(crate) async fn read_characteristic(
        &self,
        connection: ConnectionId,
        session: GattSessionId,
        characteristic: GattCharacteristicId,
        deadline: Duration,
    ) -> Result<Vec<u8>, WaiterError> {
        let ((id, receiver), deadline) = self
            .begin_operation(deadline, move |actor, context| async move {
                actor
                    .ask(BeginRead {
                        context,
                        connection,
                        session,
                        characteristic,
                    })
                    .await
            })
            .await?;
        self.wait_for_success(id, receiver, deadline, |success| match success {
            WaiterSuccess::CharacteristicRead {
                connection_id,
                gatt_session_id,
                characteristic_id,
                value,
            } if connection_id == connection
                && gatt_session_id == session
                && characteristic_id == characteristic =>
            {
                Some(value)
            }
            _ => None,
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
        deadline: Duration,
    ) -> Result<(), WaiterError> {
        let ((id, receiver), deadline) = self
            .begin_operation(deadline, move |actor, context| async move {
                actor
                    .ask(BeginWrite {
                        context,
                        connection,
                        session,
                        characteristic,
                        value,
                        write_type,
                    })
                    .await
            })
            .await?;
        self.wait_for_success(id, receiver, deadline, |success| match success {
            WaiterSuccess::CharacteristicWritten(
                found_connection,
                found_session,
                found_characteristic,
            ) if found_connection == connection
                && found_session == session
                && found_characteristic == characteristic =>
            {
                Some(())
            }
            _ => None,
        })
        .await
    }

    pub(crate) async fn stop_scan(
        &self,
        session: ScanSessionId,
        deadline: Duration,
    ) -> Result<(), WaiterError> {
        let ((id, receiver), deadline) = self
            .begin_operation(deadline, move |actor, context| async move {
                actor.ask(BeginStop { context, session }).await
            })
            .await?;
        self.wait_for_success(id, receiver, deadline, |success| match success {
            WaiterSuccess::ScanStopped(stopped) if stopped == session => Some(()),
            _ => None,
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
            let mut events = self.events.lock().await;
            let mut changed = self.lifecycle.subscribe_phase();
            loop {
                match self.lifecycle.phase() {
                    RuntimePhase::Faulted => return Err(EventReadError::Fault),
                    RuntimePhase::Closed => return Ok(None),
                    RuntimePhase::Open => {}
                }
                tokio::select! {
                    event = events.recv() => {
                        match self.lifecycle.phase() {
                            RuntimePhase::Faulted => return Err(EventReadError::Fault),
                            RuntimePhase::Closed => return Ok(None),
                            RuntimePhase::Open => {}
                        }
                        return Ok(event);
                    }
                    _ = changed.changed() => {}
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
