use std::{
    sync::{
        Arc, Condvar, Mutex,
        atomic::{AtomicU64, Ordering},
    },
    time::Duration,
};

#[derive(Clone, Debug, PartialEq, Eq, uniffi::Record)]
pub struct StartScanCommand {
    pub command_id: u64,
    pub scan_session_id: u64,
}

#[derive(Clone, Debug, PartialEq, Eq, uniffi::Record)]
pub struct BleEvent {
    pub sequence: u64,
    pub command_id: Option<u64>,
    pub payload: Vec<u8>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, uniffi::Enum)]
pub enum EventIngressStatus {
    Delivered,
    Closed,
}

#[derive(Debug, thiserror::Error, uniffi::Error)]
pub enum BleHubError {
    #[error("backend rejected setup: {reason}")]
    SetupRejected { reason: String },
    #[error("unexpected foreign callback failure: {reason}")]
    UnexpectedCallback { reason: String },
}

impl From<uniffi::UnexpectedUniFFICallbackError> for BleHubError {
    fn from(error: uniffi::UnexpectedUniFFICallbackError) -> Self {
        Self::UnexpectedCallback {
            reason: error.reason,
        }
    }
}

#[derive(Debug, thiserror::Error, uniffi::Error)]
pub enum CommandDispatchError {
    #[error("command was rejected: {reason}")]
    Rejected { reason: String },
    #[error("BleHub is closed")]
    Closed,
    #[error("unexpected foreign callback failure: {reason}")]
    UnexpectedCallback { reason: String },
}

impl From<uniffi::UnexpectedUniFFICallbackError> for CommandDispatchError {
    fn from(error: uniffi::UnexpectedUniFFICallbackError) -> Self {
        Self::UnexpectedCallback {
            reason: error.reason,
        }
    }
}

static INGRESS_CREATED: AtomicU64 = AtomicU64::new(0);
static INGRESS_DROPPED: AtomicU64 = AtomicU64::new(0);

#[derive(Clone, Debug, PartialEq, Eq, uniffi::Record)]
pub struct LifecycleCounters {
    pub ingress_created: u64,
    pub ingress_dropped: u64,
}

#[uniffi::export]
pub fn lifecycle_counters() -> LifecycleCounters {
    LifecycleCounters {
        ingress_created: INGRESS_CREATED.load(Ordering::Acquire),
        ingress_dropped: INGRESS_DROPPED.load(Ordering::Acquire),
    }
}

#[derive(Default)]
struct CallbackGate {
    active_callbacks: u64,
    blocked_callbacks: u64,
    block_callbacks: bool,
    closed: bool,
}

#[derive(uniffi::Object)]
pub struct BackendEventIngressHandle {
    callback_gate: Mutex<CallbackGate>,
    callback_gate_changed: Condvar,
    events: Mutex<Vec<BleEvent>>,
}

impl BackendEventIngressHandle {
    fn new() -> Self {
        INGRESS_CREATED.fetch_add(1, Ordering::AcqRel);
        Self {
            callback_gate: Mutex::new(CallbackGate::default()),
            callback_gate_changed: Condvar::new(),
            events: Mutex::new(Vec::new()),
        }
    }

    fn close(&self) {
        let mut gate = self
            .callback_gate
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        gate.closed = true;
        gate.block_callbacks = false;
        self.callback_gate_changed.notify_all();
    }

    fn close_and_wait(&self) {
        let mut gate = self
            .callback_gate
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        gate.closed = true;
        while gate.active_callbacks != 0 {
            gate = self
                .callback_gate_changed
                .wait(gate)
                .unwrap_or_else(std::sync::PoisonError::into_inner);
        }
    }

    fn finish_callback(&self) {
        let mut gate = self
            .callback_gate
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        gate.active_callbacks -= 1;
        self.callback_gate_changed.notify_all();
    }
}

impl Drop for BackendEventIngressHandle {
    fn drop(&mut self) {
        INGRESS_DROPPED.fetch_add(1, Ordering::AcqRel);
    }
}

#[uniffi::export]
impl BackendEventIngressHandle {
    pub fn push(&self, event: BleEvent) -> EventIngressStatus {
        let mut gate = self
            .callback_gate
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if gate.closed {
            return EventIngressStatus::Closed;
        }
        gate.active_callbacks += 1;
        if gate.block_callbacks {
            gate.blocked_callbacks += 1;
            self.callback_gate_changed.notify_all();
            while gate.block_callbacks {
                gate = self
                    .callback_gate_changed
                    .wait(gate)
                    .unwrap_or_else(std::sync::PoisonError::into_inner);
            }
            gate.blocked_callbacks -= 1;
        }
        drop(gate);

        self.events
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .push(event);
        self.finish_callback();
        EventIngressStatus::Delivered
    }

    pub fn snapshot(&self) -> Vec<BleEvent> {
        self.events
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .clone()
    }
}

#[uniffi::export(foreign)]
pub trait BleBackend: Send + Sync {
    /// Stable UniFFI objects cross the FFI as `Arc`-backed handles. Production
    /// moves the sole long-lived root into the backend; only queued or in-flight
    /// callbacks may retain temporary references.
    fn bind_event_ingress(
        &self,
        ingress: Arc<BackendEventIngressHandle>,
    ) -> Result<(), BleHubError>;

    fn start_scan(&self, command: StartScanCommand) -> Result<(), CommandDispatchError>;

    fn dispose(&self) -> Result<(), BleHubError>;
}

#[derive(uniffi::Object)]
pub struct Phase0Fixture {
    backend: Mutex<Option<Arc<dyn BleBackend>>>,
    ingress: Arc<BackendEventIngressHandle>,
}

#[uniffi::export]
impl Phase0Fixture {
    #[uniffi::constructor]
    pub fn new(backend: Arc<dyn BleBackend>) -> Result<Arc<Self>, BleHubError> {
        let ingress = Arc::new(BackendEventIngressHandle::new());
        // The fixture retains this clone for event inspection and callback-gate
        // controls. Production must instead move its sole root into the backend
        // and retain only the separate `Arc<EventIngressState>`.
        backend.bind_event_ingress(Arc::clone(&ingress))?;
        Ok(Arc::new(Self {
            backend: Mutex::new(Some(backend)),
            ingress,
        }))
    }

    pub fn start_scan(&self, command: StartScanCommand) -> Result<(), CommandDispatchError> {
        self.backend()?.start_scan(command)
    }

    pub fn start_scan_from_rust_thread(
        &self,
        command: StartScanCommand,
    ) -> Result<(), CommandDispatchError> {
        let backend = self.backend()?;
        std::thread::spawn(move || backend.start_scan(command))
            .join()
            .unwrap_or_else(|_| {
                Err(CommandDispatchError::UnexpectedCallback {
                    reason: "Rust callback thread panicked".to_owned(),
                })
            })
    }

    pub fn events(&self) -> Vec<BleEvent> {
        self.ingress.snapshot()
    }

    pub fn set_callback_blocking(&self, blocked: bool) {
        let mut gate = self
            .ingress
            .callback_gate
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        gate.block_callbacks = blocked;
        if !blocked {
            self.ingress.callback_gate_changed.notify_all();
        }
    }

    pub fn wait_for_blocked_callback(&self, timeout_millis: u64) -> bool {
        let gate = self
            .ingress
            .callback_gate
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let (gate, _) = self
            .ingress
            .callback_gate_changed
            .wait_timeout_while(gate, Duration::from_millis(timeout_millis), |gate| {
                gate.blocked_callbacks == 0
            })
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        gate.blocked_callbacks != 0
    }

    pub fn release_blocked_callbacks(&self) {
        self.set_callback_blocking(false);
    }

    pub fn dispose(&self) -> Result<(), BleHubError> {
        let backend = self
            .backend
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .take();
        let Some(backend) = backend else {
            return Ok(());
        };

        self.ingress.close_and_wait();
        backend.dispose()
    }
}

impl Phase0Fixture {
    fn backend(&self) -> Result<Arc<dyn BleBackend>, CommandDispatchError> {
        self.backend
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .clone()
            .ok_or(CommandDispatchError::Closed)
    }
}

impl Drop for Phase0Fixture {
    fn drop(&mut self) {
        self.ingress.close();
    }
}

uniffi::setup_scaffolding!();
