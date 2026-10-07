//! Runtime owner, operation admission, and caller-side waiter tickets.

use std::{
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
    waiter::{WaiterError, WaiterSuccess},
};
use blehub::{
    AdapterSnapshot, BleBackend, BleEvent, CommandId, ConnectionId, GattCharacteristicId,
    GattDatabase, GattSessionId, NotificationMode, NotificationModeRequest, PeripheralId,
    ScanOptions, ScanSessionId, SubscriptionId, WriteType,
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
    AssemblyError, BackendPort, BlengActor, RuntimeLifecycle, RuntimePhase,
    commands::{
        BeginConnect, BeginDisableNotifications, BeginDisconnect, BeginDiscoverGatt,
        BeginEnableNotifications, BeginInitialize, BeginRead, BeginRssi, BeginScan, BeginStop,
        BeginWrite, RetireWaiter,
    },
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

    pub(super) fn try_launch(&self) -> bool {
        self.0
            .compare_exchange(
                BeginPhase::Pending.into(),
                BeginPhase::Claimed.into(),
                Ordering::AcqRel,
                Ordering::Acquire,
            )
            .is_ok()
    }

    /// Returns whether the actor claimed dispatch before cancellation.
    fn cancel(&self) -> bool {
        let previous = self.0.swap(BeginPhase::Cancelled.into(), Ordering::AcqRel);
        // An impossible raw value fails closed as if the actor had claimed dispatch.
        matches!(
            BeginPhase::try_from(previous).unwrap_or(BeginPhase::Claimed),
            BeginPhase::Claimed
        )
    }
}

/// Protects the caller until the owner receives the actor's begin reply.
/// `armed` is task-local Drop policy; only `handoff` arbitrates with the actor.
struct BeginHandoffGuard {
    handoff: BeginHandoff,
    lifecycle: Arc<RuntimeLifecycle>,
    armed: bool,
}

impl BeginHandoffGuard {
    fn new(lifecycle: Arc<RuntimeLifecycle>) -> Self {
        Self {
            handoff: BeginHandoff::new(),
            lifecycle,
            armed: true,
        }
    }

    /// Consumes the guard once the owner owns the reply; no atomic update is needed.
    fn acknowledge(mut self) {
        self.armed = false;
    }
}

impl Drop for BeginHandoffGuard {
    fn drop(&mut self) {
        if self.armed && self.handoff.cancel() {
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
    pub(crate) result_rx: oneshot::Receiver<Result<WaiterSuccess, WaiterError>>,
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
        let mut phase_rx = self.lifecycle.subscribe_phase();
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
                result = &mut self.result_rx => {
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
    pub(super) actor: ActorRef<BlengActor>,
    pub(crate) lifecycle: Arc<RuntimeLifecycle>,
    // One bounded FIFO is intentionally single-consumer. Concurrent next_event
    // calls serialize on this guard and divide the stream; they do not broadcast.
    event_rx: Mutex<mpsc::Receiver<BleEvent>>,
    shutdown_started: AtomicBool,
    pub(crate) shutdown_result_tx: watch::Sender<Option<Result<(), CleanupError>>>,
}

/// Stop the actor after a lifecycle fault without borrowing `RuntimeOwner`.
/// The detached task owns exactly the actor handle and lifecycle receiver it needs.
fn spawn_fault_shutdown(
    handle: &tokio::runtime::Handle,
    actor: ActorRef<BlengActor>,
    mut phase_rx: watch::Receiver<RuntimePhase>,
) {
    handle.spawn(async move {
        loop {
            let current_phase = *phase_rx.borrow_and_update();
            match current_phase {
                RuntimePhase::Faulted => {
                    let _ = actor.stop_gracefully().await;
                    actor.wait_for_shutdown().await;
                    break;
                }
                RuntimePhase::Closed => break,
                RuntimePhase::Open => {}
            }
            if phase_rx.changed().await.is_err() {
                break;
            }
        }
    });
}

/// Wait for the one shared shutdown result. It deliberately takes only the
/// receiver so cancellation of one caller cannot cancel the owner coordinator.
async fn wait_for_shutdown(
    mut shutdown_result_rx: watch::Receiver<Option<Result<(), CleanupError>>>,
) -> Result<(), CleanupError> {
    loop {
        if let Some(outcome) = shutdown_result_rx.borrow_and_update().clone() {
            return outcome;
        }
        if shutdown_result_rx.changed().await.is_err() {
            return Err(CleanupError::WorkerStopped);
        }
    }
}

impl RuntimeOwner {
    pub(crate) fn new() -> io::Result<Self> {
        let runtime = Builder::new_multi_thread().enable_time().build()?;
        let (event_tx, event_rx) = mpsc::channel(EVENT_CHANNEL_CAPACITY);
        let (shutdown_result_tx, _) = watch::channel(None);
        let lifecycle = Arc::new(RuntimeLifecycle::new());
        let actor = {
            let _runtime_guard = runtime.enter();
            BlengActor::spawn_with_mailbox(
                BlengActor::new(event_tx, Arc::clone(&lifecycle)),
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
            event_rx: Mutex::new(event_rx),
            shutdown_started: AtomicBool::new(false),
            shutdown_result_tx,
        })
    }

    pub(crate) async fn close(&self) -> Result<(), CleanupError> {
        let shutdown_result_rx = self.shutdown_result_tx.subscribe();
        self.lifecycle.close();
        self.lifecycle.start_cleanup();
        if !self.shutdown_started.swap(true, Ordering::AcqRel) {
            self.spawn_shutdown_coordinator();
        }
        // Every caller awaits the same outcome; no caller owns the shutdown
        // future, and a later close can still observe cleanup errors.
        wait_for_shutdown(shutdown_result_rx).await
    }

    /// Spawn exactly one owner-held coordinator. It is deliberately detached
    /// from the caller's future so cancellation of one `close` call cannot
    /// strand actor shutdown or native Hub disposal.
    fn spawn_shutdown_coordinator(&self) {
        let actor = self.actor.clone();
        let lifecycle = Arc::clone(&self.lifecycle);
        let shutdown_result_tx = self.shutdown_result_tx.clone();
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
            shutdown_result_tx.send_replace(Some(cleanup_result));
        });
    }

    pub(super) async fn begin_operation<T, M, F, Fut>(
        &self,
        timeout: Duration,
        request: F,
    ) -> Result<(T, Instant), WaiterError>
    where
        F: FnOnce(ActorRef<BlengActor>, BeginHandoff) -> Fut,
        Fut: Future<Output = Result<T, SendError<M, WaiterError>>>,
    {
        // Use the same dispatch gate as the actor, but retain no Hub across
        // mailbox waiting. The actor reacquires the root before handoff.
        drop(self.lifecycle.hub_for_dispatch()?);
        let deadline = Instant::now() + timeout;
        let guard = BeginHandoffGuard::new(Arc::clone(&self.lifecycle));
        let request = request(self.actor.clone(), guard.handoff.clone());
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
        guard.acknowledge();
        Ok((reply, deadline))
    }

    pub(crate) fn ticket(
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

    async fn wait_for_success<T>(
        &self,
        id: CommandId,
        result_rx: oneshot::Receiver<Result<WaiterSuccess, WaiterError>>,
        deadline: Instant,
        accept: impl FnOnce(WaiterSuccess) -> Option<T>,
    ) -> Result<T, WaiterError> {
        let success = self.ticket(id, result_rx).wait_until(deadline).await?;
        accept(success).ok_or_else(|| {
            self.lifecycle.fault();
            WaiterError::TransportFault
        })
    }

    pub(crate) async fn initialize(
        &self,
        deadline: Duration,
    ) -> Result<AdapterSnapshot, WaiterError> {
        let (reply, deadline) = self
            .begin_operation(deadline, move |actor, handoff| async move {
                actor.ask(BeginInitialize { handoff }).await
            })
            .await?;
        self.wait_for_success(
            reply.command_id,
            reply.result_rx,
            deadline,
            |success| match success {
                WaiterSuccess::Initialized(snapshot) => Some(snapshot),
                _ => None,
            },
        )
        .await
    }

    pub(crate) async fn start_scan(
        &self,
        options: ScanOptions,
        deadline: Duration,
    ) -> Result<ScanSessionId, WaiterError> {
        let (reply, deadline) = self
            .begin_operation(deadline, move |actor, handoff| async move {
                actor.ask(BeginScan { handoff, options }).await
            })
            .await?;
        self.wait_for_success(
            reply.command_id,
            reply.result_rx,
            deadline,
            |success| match success {
                WaiterSuccess::ScanStarted(scan) if scan == reply.resource => Some(scan),
                _ => None,
            },
        )
        .await
    }

    pub(crate) async fn connect(
        &self,
        peripheral_id: PeripheralId,
        deadline: Duration,
    ) -> Result<ConnectionId, WaiterError> {
        let (reply, deadline) = self
            .begin_operation(deadline, move |actor, handoff| async move {
                actor
                    .ask(BeginConnect {
                        handoff,
                        peripheral_id,
                    })
                    .await
            })
            .await?;
        self.wait_for_success(
            reply.command_id,
            reply.result_rx,
            deadline,
            |success| match success {
                WaiterSuccess::Connected(found) if found == reply.resource => Some(found),
                _ => None,
            },
        )
        .await
    }

    pub(crate) async fn disconnect(
        &self,
        connection: ConnectionId,
        deadline: Duration,
    ) -> Result<(), WaiterError> {
        let (reply, deadline) = self
            .begin_operation(deadline, move |actor, handoff| async move {
                actor
                    .ask(BeginDisconnect {
                        handoff,
                        connection,
                    })
                    .await
            })
            .await?;
        self.wait_for_success(
            reply.command_id,
            reply.result_rx,
            deadline,
            |success| match success {
                WaiterSuccess::Disconnected(found) if found == connection => Some(()),
                _ => None,
            },
        )
        .await
    }

    pub(crate) async fn discover_gatt(
        &self,
        connection: ConnectionId,
        deadline: Duration,
    ) -> Result<GattDatabase, WaiterError> {
        let (reply, deadline) = self
            .begin_operation(deadline, move |actor, handoff| async move {
                actor
                    .ask(BeginDiscoverGatt {
                        handoff,
                        connection,
                    })
                    .await
            })
            .await?;
        self.wait_for_success(
            reply.command_id,
            reply.result_rx,
            deadline,
            |success| match success {
                WaiterSuccess::GattDiscovered(database)
                    if database.connection_id == connection
                        && database.gatt_session_id == reply.resource =>
                {
                    Some(database)
                }
                _ => None,
            },
        )
        .await
    }

    pub(crate) async fn read_rssi(
        &self,
        connection: ConnectionId,
        deadline: Duration,
    ) -> Result<i16, WaiterError> {
        let (reply, deadline) = self
            .begin_operation(deadline, move |actor, handoff| async move {
                actor
                    .ask(BeginRssi {
                        handoff,
                        connection,
                    })
                    .await
            })
            .await?;
        self.wait_for_success(
            reply.command_id,
            reply.result_rx,
            deadline,
            |success| match success {
                WaiterSuccess::RssiRead(found_connection, value)
                    if found_connection == connection =>
                {
                    Some(value)
                }
                _ => None,
            },
        )
        .await
    }

    pub(crate) async fn read_characteristic(
        &self,
        connection: ConnectionId,
        session: GattSessionId,
        characteristic: GattCharacteristicId,
        deadline: Duration,
    ) -> Result<Vec<u8>, WaiterError> {
        let (reply, deadline) = self
            .begin_operation(deadline, move |actor, handoff| async move {
                actor
                    .ask(BeginRead {
                        handoff,
                        connection,
                        session,
                        characteristic,
                    })
                    .await
            })
            .await?;
        self.wait_for_success(
            reply.command_id,
            reply.result_rx,
            deadline,
            |success| match success {
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
            },
        )
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
        let (reply, deadline) = self
            .begin_operation(deadline, move |actor, handoff| async move {
                actor
                    .ask(BeginWrite {
                        handoff,
                        connection,
                        session,
                        characteristic,
                        value,
                        write_type,
                    })
                    .await
            })
            .await?;
        self.wait_for_success(
            reply.command_id,
            reply.result_rx,
            deadline,
            |success| match success {
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
            },
        )
        .await
    }

    pub(crate) async fn stop_scan(
        &self,
        session: ScanSessionId,
        deadline: Duration,
    ) -> Result<(), WaiterError> {
        let (reply, deadline) = self
            .begin_operation(deadline, move |actor, handoff| async move {
                actor.ask(BeginStop { handoff, session }).await
            })
            .await?;
        self.wait_for_success(
            reply.command_id,
            reply.result_rx,
            deadline,
            |success| match success {
                WaiterSuccess::ScanStopped(stopped) if stopped == session => Some(()),
                _ => None,
            },
        )
        .await
    }

    pub(crate) async fn enable_notifications(
        &self,
        connection: ConnectionId,
        session: GattSessionId,
        characteristic: GattCharacteristicId,
        mode: NotificationModeRequest,
        deadline: Duration,
    ) -> Result<(SubscriptionId, Option<NotificationMode>), WaiterError> {
        let (reply, deadline) = self
            .begin_operation(deadline, move |actor, handoff| async move {
                actor
                    .ask(BeginEnableNotifications {
                        handoff,
                        connection,
                        session,
                        characteristic,
                        mode,
                    })
                    .await
            })
            .await?;
        self.wait_for_success(
            reply.command_id,
            reply.result_rx,
            deadline,
            |success| match success {
                WaiterSuccess::NotificationsEnabled {
                    connection_id,
                    gatt_session_id,
                    subscription_id,
                    characteristic_id,
                    actual_mode,
                } if connection_id == connection
                    && gatt_session_id == session
                    && subscription_id == reply.resource
                    && characteristic_id == characteristic =>
                {
                    Some((reply.resource, actual_mode))
                }
                _ => None,
            },
        )
        .await
    }

    pub(crate) async fn disable_notifications(
        &self,
        connection: ConnectionId,
        session: GattSessionId,
        subscription: SubscriptionId,
        deadline: Duration,
    ) -> Result<(), WaiterError> {
        let (reply, deadline) = self
            .begin_operation(deadline, move |actor, handoff| async move {
                actor
                    .ask(BeginDisableNotifications {
                        handoff,
                        connection,
                        session,
                        subscription,
                    })
                    .await
            })
            .await?;
        self.wait_for_success(
            reply.command_id,
            reply.result_rx,
            deadline,
            |success| match success {
                WaiterSuccess::NotificationsDisabled(
                    found_connection,
                    found_session,
                    found_subscription,
                    _,
                ) if found_connection == connection
                    && found_session == session
                    && found_subscription == subscription =>
                {
                    Some(())
                }
                _ => None,
            },
        )
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
