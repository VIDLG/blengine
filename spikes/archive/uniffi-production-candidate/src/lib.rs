use std::{
    sync::{
        Arc, Condvar, Mutex,
        atomic::{AtomicU8, AtomicU64, Ordering},
    },
    time::Duration,
};

use blehub::{
    BLEHUB_CONTRACT_VERSION, BackendCapabilities, BleBackend, BleEvent, BleEventSink, BleHub,
    BleHubError, CommandDispatchError, ConnectCommand, DisconnectCommand, DiscoverGattCommand,
    EventSinkError, InitializeCommand, ReadCharacteristicCommand, ReadRssiCommand,
    SetNotificationsCommand, StartScanCommand, StopScanCommand, WriteCharacteristicCommand,
};
use blehub_testkit::unified_event_catalog_fixture;

static FIXTURE_CREATED: AtomicU64 = AtomicU64::new(0);
static FIXTURE_DROPPED: AtomicU64 = AtomicU64::new(0);
static SINK_CREATED: AtomicU64 = AtomicU64::new(0);
static SINK_DROPPED: AtomicU64 = AtomicU64::new(0);

#[derive(Clone, Copy, Debug, PartialEq, Eq, uniffi::Enum)]
pub enum FixtureSinkBehavior {
    Accept,
    Reject,
    Panic,
    Block,
}

#[derive(Clone, Debug, PartialEq, Eq, uniffi::Record)]
pub struct FixtureSnapshot {
    pub events: Vec<BleEvent>,
    pub active_callbacks: u64,
    pub maximum_active_callbacks: u64,
}

#[derive(Clone, Debug, PartialEq, Eq, uniffi::Record)]
pub struct FixtureLifecycleCounters {
    pub fixture_created: u64,
    pub fixture_dropped: u64,
    pub sink_created: u64,
    pub sink_dropped: u64,
}

#[uniffi::export]
pub fn fixture_contract_version() -> u32 {
    BLEHUB_CONTRACT_VERSION
}

#[uniffi::export]
pub fn fixture_event_catalog() -> Vec<BleEvent> {
    unified_event_catalog_fixture()
}

#[uniffi::export]
pub fn fixture_event_catalog_matches(events: Vec<BleEvent>) -> bool {
    events == unified_event_catalog_fixture()
}

#[uniffi::export]
pub fn fixture_lifecycle_counters() -> FixtureLifecycleCounters {
    FixtureLifecycleCounters {
        fixture_created: FIXTURE_CREATED.load(Ordering::Acquire),
        fixture_dropped: FIXTURE_DROPPED.load(Ordering::Acquire),
        sink_created: SINK_CREATED.load(Ordering::Acquire),
        sink_dropped: SINK_DROPPED.load(Ordering::Acquire),
    }
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
        SINK_CREATED.fetch_add(1, Ordering::AcqRel);
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
            *self
                .block_entered
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner) = false;
            *self
                .block_released
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner) = false;
        }
        self.behavior.store(behavior as u8, Ordering::Release);
    }

    fn wait_for_blocked_callback(&self, timeout: Duration) -> bool {
        let entered = self
            .block_entered
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let (entered, _) = self
            .block_entered_changed
            .wait_timeout_while(entered, timeout, |entered| !*entered)
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        *entered
    }

    fn release_blocked_callback(&self) {
        *self
            .block_released
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner) = true;
        self.block_released_changed.notify_all();
    }

    fn block_callback(&self) {
        *self
            .block_entered
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner) = true;
        self.block_entered_changed.notify_all();
        let released = self
            .block_released
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        drop(
            self.block_released_changed
                .wait_while(released, |released| !*released)
                .unwrap_or_else(std::sync::PoisonError::into_inner),
        );
    }

    fn snapshot(&self) -> FixtureSnapshot {
        FixtureSnapshot {
            events: self
                .events
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner)
                .clone(),
            active_callbacks: self.active_callbacks.load(Ordering::Acquire),
            maximum_active_callbacks: self.maximum_active_callbacks.load(Ordering::Acquire),
        }
    }
}

impl Drop for RecordingSinkState {
    fn drop(&mut self) {
        SINK_DROPPED.fetch_add(1, Ordering::AcqRel);
    }
}

struct RecordingSink {
    state: Arc<RecordingSinkState>,
}

struct ActiveCallbackGuard<'a> {
    active_callbacks: &'a AtomicU64,
}

impl Drop for ActiveCallbackGuard<'_> {
    fn drop(&mut self) {
        self.active_callbacks.fetch_sub(1, Ordering::AcqRel);
    }
}

impl BleEventSink for RecordingSink {
    fn on_event(&self, event: BleEvent) -> Result<(), EventSinkError> {
        let active = self.state.active_callbacks.fetch_add(1, Ordering::AcqRel) + 1;
        self.state
            .maximum_active_callbacks
            .fetch_max(active, Ordering::AcqRel);
        let _active_callback = ActiveCallbackGuard {
            active_callbacks: &self.state.active_callbacks,
        };

        match self.state.behavior.load(Ordering::Acquire) {
            value if value == FixtureSinkBehavior::Accept as u8 => {
                self.state
                    .events
                    .lock()
                    .unwrap_or_else(std::sync::PoisonError::into_inner)
                    .push(event);
                Ok(())
            }
            value if value == FixtureSinkBehavior::Reject as u8 => Err(EventSinkError {
                message: "production candidate sink rejected the event".to_owned(),
            }),
            value if value == FixtureSinkBehavior::Panic as u8 => {
                panic!("production candidate sink panic requested")
            }
            _ => {
                self.state.block_callback();
                self.state
                    .events
                    .lock()
                    .unwrap_or_else(std::sync::PoisonError::into_inner)
                    .push(event);
                Ok(())
            }
        }
    }
}

#[derive(uniffi::Object)]
pub struct ProductionCandidateFixture {
    hub: BleHub,
    sink: Arc<RecordingSinkState>,
}

#[uniffi::export]
impl ProductionCandidateFixture {
    #[uniffi::constructor]
    pub fn new(backend: Arc<dyn BleBackend>) -> Result<Arc<Self>, BleHubError> {
        let sink = Arc::new(RecordingSinkState::new());
        let hub = BleHub::new(
            backend,
            Box::new(RecordingSink {
                state: Arc::clone(&sink),
            }),
        )?;
        FIXTURE_CREATED.fetch_add(1, Ordering::AcqRel);
        Ok(Arc::new(Self { hub, sink }))
    }

    pub fn capabilities(&self) -> BackendCapabilities {
        self.hub.capabilities()
    }

    pub fn initialize(&self, command: InitializeCommand) -> Result<(), CommandDispatchError> {
        self.hub.initialize(command)
    }

    pub fn start_scan(&self, command: StartScanCommand) -> Result<(), CommandDispatchError> {
        self.hub.start_scan(command)
    }

    pub fn stop_scan(&self, command: StopScanCommand) -> Result<(), CommandDispatchError> {
        self.hub.stop_scan(command)
    }

    pub fn connect(&self, command: ConnectCommand) -> Result<(), CommandDispatchError> {
        self.hub.connect(command)
    }

    pub fn disconnect(&self, command: DisconnectCommand) -> Result<(), CommandDispatchError> {
        self.hub.disconnect(command)
    }

    pub fn discover_gatt(&self, command: DiscoverGattCommand) -> Result<(), CommandDispatchError> {
        self.hub.discover_gatt(command)
    }

    pub fn read_characteristic(
        &self,
        command: ReadCharacteristicCommand,
    ) -> Result<(), CommandDispatchError> {
        self.hub.read_characteristic(command)
    }

    pub fn write_characteristic(
        &self,
        command: WriteCharacteristicCommand,
    ) -> Result<(), CommandDispatchError> {
        self.hub.write_characteristic(command)
    }

    pub fn set_notifications(
        &self,
        command: SetNotificationsCommand,
    ) -> Result<(), CommandDispatchError> {
        self.hub.set_notifications(command)
    }

    pub fn read_rssi(&self, command: ReadRssiCommand) -> Result<(), CommandDispatchError> {
        self.hub.read_rssi(command)
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

    pub fn wait_for_blocked_callback(&self, timeout_millis: u64) -> bool {
        self.sink
            .wait_for_blocked_callback(Duration::from_millis(timeout_millis))
    }

    pub fn release_blocked_callback(&self) {
        self.sink.release_blocked_callback();
    }

    pub fn snapshot(&self) -> FixtureSnapshot {
        self.sink.snapshot()
    }

    pub fn dispose(&self) -> Result<(), BleHubError> {
        self.hub.dispose()
    }
}

impl Drop for ProductionCandidateFixture {
    fn drop(&mut self) {
        let _ = self.hub.dispose();
        FIXTURE_DROPPED.fetch_add(1, Ordering::AcqRel);
    }
}

uniffi::setup_scaffolding!();
