use std::{
    collections::HashMap,
    sync::{
        Arc, Condvar, LazyLock, Mutex,
        atomic::{AtomicU8, AtomicU64, Ordering},
    },
    time::Duration,
};

use blehub::{
    BackendCapabilities, BleBackend, BleEvent, BleEventSink, BleHub, BleHubError,
    CommandDispatchError, ConnectCommand, EventSinkError, StartScanCommand,
};
use boltffi::*;

#[export]
pub const FIXTURE_CONTRACT_VERSION: u32 = blehub::BLEHUB_CONTRACT_VERSION;

#[data]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ExceptionProbeAck {
    pub accepted: bool,
}

#[data]
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub struct ForeignCallAckToken {
    pub value: u64,
}

static NEXT_FOREIGN_CALL_ACK_TOKEN: AtomicU64 = AtomicU64::new(1);
static FOREIGN_CALL_ACKS: LazyLock<Mutex<HashMap<ForeignCallAckToken, bool>>> =
    LazyLock::new(|| Mutex::new(HashMap::new()));

#[export]
pub trait ForeignResultExceptionProbe: Send + Sync {
    fn unit_result(&self) -> Result<(), CommandDispatchError>;

    fn success_sentinel_result(&self) -> Result<bool, CommandDispatchError>;

    fn record_ack_result(&self) -> Result<ExceptionProbeAck, CommandDispatchError>;

    fn acknowledged_unit_result(
        &self,
        token: ForeignCallAckToken,
    ) -> Result<(), CommandDispatchError>;
}

pub struct ForeignResultExceptionProbeFixture {
    probe: Box<dyn ForeignResultExceptionProbe>,
}

#[export]
impl ForeignResultExceptionProbeFixture {
    pub fn new(probe: Box<dyn ForeignResultExceptionProbe>) -> Self {
        Self { probe }
    }

    pub fn call_unit_result(&self) -> Result<(), CommandDispatchError> {
        self.probe.unit_result()
    }

    pub fn call_checked_success_sentinel(&self) -> Result<(), CommandDispatchError> {
        validate_exception_probe_ack(self.probe.success_sentinel_result()?)
    }

    pub fn call_checked_record_ack(&self) -> Result<(), CommandDispatchError> {
        validate_exception_probe_ack(self.probe.record_ack_result()?.accepted)
    }

    pub fn call_explicitly_acknowledged_unit_result(&self) -> Result<(), CommandDispatchError> {
        let token = begin_foreign_call_ack();
        let result = self.probe.acknowledged_unit_result(token);
        let acknowledged = finish_foreign_call_ack(token);

        match (result, acknowledged) {
            (Ok(()), true) => Ok(()),
            (Ok(()), false) => Err(missing_foreign_call_ack()),
            (Err(error), false) => Err(error),
            (Err(_), true) => Err(CommandDispatchError::FfiContract {
                detail: "foreign callback both acknowledged and rejected the same call".to_owned(),
            }),
        }
    }
}

#[export]
pub fn acknowledge_foreign_call(token: ForeignCallAckToken) -> bool {
    let mut acknowledgements = FOREIGN_CALL_ACKS.lock().unwrap();
    let Some(acknowledged) = acknowledgements.get_mut(&token) else {
        return false;
    };
    if *acknowledged {
        return false;
    }
    *acknowledged = true;
    true
}

fn begin_foreign_call_ack() -> ForeignCallAckToken {
    let token = ForeignCallAckToken {
        value: NEXT_FOREIGN_CALL_ACK_TOKEN.fetch_add(1, Ordering::Relaxed),
    };
    assert_ne!(token.value, 0, "foreign call ACK token space exhausted");
    assert!(
        FOREIGN_CALL_ACKS
            .lock()
            .unwrap()
            .insert(token, false)
            .is_none(),
        "foreign call ACK token collision"
    );
    token
}

fn finish_foreign_call_ack(token: ForeignCallAckToken) -> bool {
    FOREIGN_CALL_ACKS
        .lock()
        .unwrap()
        .remove(&token)
        .unwrap_or(false)
}

fn missing_foreign_call_ack() -> CommandDispatchError {
    CommandDispatchError::FfiContract {
        detail: "foreign callback returned without the required explicit acknowledgement"
            .to_owned(),
    }
}

fn validate_exception_probe_ack(accepted: bool) -> Result<(), CommandDispatchError> {
    if accepted {
        Ok(())
    } else {
        Err(CommandDispatchError::FfiContract {
            detail: "foreign callback did not return the required success sentinel".to_owned(),
        })
    }
}

static SINK_CREATE_COUNT: AtomicU64 = AtomicU64::new(0);
static SINK_DROP_COUNT: AtomicU64 = AtomicU64::new(0);

#[data]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FixtureSinkBehavior {
    Accept,
    Reject,
    Panic,
    Block,
}

#[data]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Phase0FixtureSnapshot {
    pub event_count: u64,
    pub active_callbacks: u64,
    pub maximum_active_callbacks: u64,
    pub sink_create_count: u64,
    pub sink_drop_count: u64,
}

struct RecordingSinkState {
    events: Mutex<Vec<BleEvent>>,
    behavior: AtomicU8,
    active_callbacks: AtomicU64,
    maximum_active_callbacks: AtomicU64,
    block_entered: Mutex<bool>,
    block_entered_changed: Condvar,
    block_released: Mutex<bool>,
    block_released_changed: Condvar,
}

impl RecordingSinkState {
    fn new() -> Self {
        SINK_CREATE_COUNT.fetch_add(1, Ordering::AcqRel);
        Self {
            events: Mutex::new(Vec::new()),
            behavior: AtomicU8::new(FixtureSinkBehavior::Accept as u8),
            active_callbacks: AtomicU64::new(0),
            maximum_active_callbacks: AtomicU64::new(0),
            block_entered: Mutex::new(false),
            block_entered_changed: Condvar::new(),
            block_released: Mutex::new(false),
            block_released_changed: Condvar::new(),
        }
    }

    fn set_behavior(&self, behavior: FixtureSinkBehavior) {
        if behavior == FixtureSinkBehavior::Block {
            *self.block_entered.lock().unwrap() = false;
            *self.block_released.lock().unwrap() = false;
        }
        self.behavior.store(behavior as u8, Ordering::Release);
    }

    fn wait_for_blocked_callback(&self, timeout: Duration) -> bool {
        let entered = self.block_entered.lock().unwrap();
        let (entered, _) = self
            .block_entered_changed
            .wait_timeout_while(entered, timeout, |entered| !*entered)
            .unwrap();
        *entered
    }

    fn release_blocked_callback(&self) {
        *self.block_released.lock().unwrap() = true;
        self.block_released_changed.notify_all();
    }

    fn block_callback(&self) {
        *self.block_entered.lock().unwrap() = true;
        self.block_entered_changed.notify_all();
        let released = self.block_released.lock().unwrap();
        drop(
            self.block_released_changed
                .wait_while(released, |released| !*released)
                .unwrap(),
        );
    }

    fn snapshot(&self) -> Phase0FixtureSnapshot {
        Phase0FixtureSnapshot {
            event_count: self.events.lock().unwrap().len() as u64,
            active_callbacks: self.active_callbacks.load(Ordering::Acquire),
            maximum_active_callbacks: self.maximum_active_callbacks.load(Ordering::Acquire),
            sink_create_count: SINK_CREATE_COUNT.load(Ordering::Acquire),
            sink_drop_count: SINK_DROP_COUNT.load(Ordering::Acquire),
        }
    }
}

struct RecordingSink {
    state: Arc<RecordingSinkState>,
}

struct ActiveCallbackGuard<'a> {
    count: &'a AtomicU64,
}

impl Drop for ActiveCallbackGuard<'_> {
    fn drop(&mut self) {
        self.count.fetch_sub(1, Ordering::AcqRel);
    }
}

impl BleEventSink for RecordingSink {
    fn on_event(&self, event: BleEvent) -> Result<(), EventSinkError> {
        let active = self.state.active_callbacks.fetch_add(1, Ordering::AcqRel) + 1;
        self.state
            .maximum_active_callbacks
            .fetch_max(active, Ordering::AcqRel);
        let _active_callback = ActiveCallbackGuard {
            count: &self.state.active_callbacks,
        };

        match self.state.behavior.load(Ordering::Acquire) {
            value if value == FixtureSinkBehavior::Accept as u8 => {
                self.state.events.lock().unwrap().push(event);
                Ok(())
            }
            value if value == FixtureSinkBehavior::Reject as u8 => Err(EventSinkError {
                message: "phase0 fixture sink rejected the event".to_owned(),
            }),
            value if value == FixtureSinkBehavior::Panic as u8 => {
                panic!("phase0 fixture sink panic requested")
            }
            _ => {
                self.state.block_callback();
                self.state.events.lock().unwrap().push(event);
                Ok(())
            }
        }
    }
}

impl Drop for RecordingSink {
    fn drop(&mut self) {
        SINK_DROP_COUNT.fetch_add(1, Ordering::AcqRel);
    }
}

pub struct Phase0Fixture {
    hub: BleHub,
    sink: Arc<RecordingSinkState>,
}

#[export]
impl Phase0Fixture {
    pub fn new(backend: Box<dyn BleBackend>) -> Result<Self, BleHubError> {
        let sink = Arc::new(RecordingSinkState::new());
        let hub = BleHub::new(
            backend,
            Box::new(RecordingSink {
                state: Arc::clone(&sink),
            }),
        )?;
        Ok(Self { hub, sink })
    }

    pub fn capabilities(&self) -> BackendCapabilities {
        self.hub.capabilities()
    }

    pub fn start_scan(&self, command: StartScanCommand) -> Result<(), CommandDispatchError> {
        self.hub.start_scan(command)
    }

    pub fn connect(&self, command: ConnectCommand) -> Result<(), CommandDispatchError> {
        self.hub.connect(command)
    }

    pub fn start_scan_from_rust_thread(
        &self,
        command: StartScanCommand,
    ) -> Result<(), CommandDispatchError> {
        std::thread::scope(|scope| {
            scope
                .spawn(|| self.hub.start_scan(command))
                .join()
                .unwrap_or_else(|_| {
                    Err(CommandDispatchError::FfiContract {
                        detail: "Rust-created backend caller thread panicked".to_owned(),
                    })
                })
        })
    }

    pub fn set_sink_behavior(&self, behavior: FixtureSinkBehavior) {
        self.sink.set_behavior(behavior);
    }

    pub fn snapshot(&self) -> Phase0FixtureSnapshot {
        self.sink.snapshot()
    }

    pub fn wait_for_blocked_callback(&self, timeout_millis: u64) -> bool {
        self.sink
            .wait_for_blocked_callback(Duration::from_millis(timeout_millis))
    }

    pub fn release_blocked_callback(&self) {
        self.sink.release_blocked_callback();
    }

    pub fn events(&self) -> Vec<BleEvent> {
        self.sink.events.lock().unwrap().clone()
    }

    pub fn dispose(&self) -> Result<(), BleHubError> {
        self.hub.dispose()
    }
}
