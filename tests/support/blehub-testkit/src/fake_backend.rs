use std::sync::{
    Arc, Mutex,
    atomic::{AtomicBool, AtomicUsize, Ordering},
};

use blehub::{
    BackendCapabilities, BackendEventIngressHandle, BleBackend, BleEvent, BleHubError,
    CancelOperationRequest, CommandDispatchError, ConnectCommand, DisconnectCommand,
    DiscoverGattCommand, EventIngressStatus, InitializeCommand, Platform,
    ReadCharacteristicCommand, ReadRssiCommand, SetNotificationsCommand, StartScanCommand,
    StopScanCommand, WriteCharacteristicCommand,
};

use crate::{
    BackendContractViolation, CompletionExpectation, EventScenario, RecordedCommand,
    runner::ScenarioRunner,
};

struct FakeBackendState {
    capabilities: BackendCapabilities,
    ingress: Mutex<Option<Arc<BackendEventIngressHandle>>>,
    runner: ScenarioRunner,
    cancellation_policy: CancellationPolicy,
    disposed: AtomicBool,
    dispose_count: AtomicUsize,
}

pub struct FakeBackend {
    state: Arc<FakeBackendState>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CancellationPolicy {
    Reject,
    Accept,
}

#[derive(Clone)]
pub struct FakeBackendHandle {
    state: Arc<FakeBackendState>,
}

impl FakeBackend {
    pub fn scripted(
        scenarios: impl IntoIterator<Item = EventScenario>,
    ) -> (Self, FakeBackendHandle) {
        Self::scripted_with_cancellation(scenarios, CancellationPolicy::Reject)
    }

    pub fn scripted_with_cancellation(
        scenarios: impl IntoIterator<Item = EventScenario>,
        cancellation_policy: CancellationPolicy,
    ) -> (Self, FakeBackendHandle) {
        Self::scripted_with_capabilities_and_cancellation(
            BackendCapabilities {
                supports_duplicate_scan_results: true,
                supports_connectable_flag: true,
                supports_read_rssi: true,
                supports_required_notification: true,
                supports_required_indication: true,
                reports_actual_notification_mode: true,
                ..BackendCapabilities::new(Platform::Test)
            },
            scenarios,
            cancellation_policy,
        )
    }

    pub fn scripted_with_capabilities(
        capabilities: BackendCapabilities,
        scenarios: impl IntoIterator<Item = EventScenario>,
    ) -> (Self, FakeBackendHandle) {
        Self::scripted_with_capabilities_and_cancellation(
            capabilities,
            scenarios,
            CancellationPolicy::Reject,
        )
    }

    pub fn scripted_with_capabilities_and_cancellation(
        capabilities: BackendCapabilities,
        scenarios: impl IntoIterator<Item = EventScenario>,
        cancellation_policy: CancellationPolicy,
    ) -> (Self, FakeBackendHandle) {
        let state = Arc::new(FakeBackendState {
            capabilities,
            ingress: Mutex::new(None),
            runner: ScenarioRunner::new(scenarios),
            cancellation_policy,
            disposed: AtomicBool::new(false),
            dispose_count: AtomicUsize::new(0),
        });
        (
            Self {
                state: Arc::clone(&state),
            },
            FakeBackendHandle { state },
        )
    }

    fn dispatch(&self, command: RecordedCommand) -> Result<(), CommandDispatchError> {
        self.state.runner.dispatch(
            command,
            self.state.disposed.load(Ordering::Acquire),
            |event| {
                let ingress = self.state.ingress.lock().unwrap().clone().ok_or_else(|| {
                    CommandDispatchError::InvalidState {
                        detail: "event ingress is not bound".to_owned(),
                    }
                })?;
                Ok(ingress.push(event))
            },
        )
    }
}

impl BleBackend for FakeBackend {
    fn capabilities(&self) -> Result<BackendCapabilities, BleHubError> {
        Ok(self.state.capabilities.clone())
    }

    fn bind_event_ingress(
        &self,
        ingress: Arc<BackendEventIngressHandle>,
    ) -> Result<(), BleHubError> {
        let mut current = self.state.ingress.lock().unwrap();
        if current.is_some() {
            return Err(BleHubError::BackendAssembly {
                detail: "event ingress is already bound".to_owned(),
            });
        }
        *current = Some(ingress);
        Ok(())
    }

    fn initialize(&self, command: InitializeCommand) -> Result<(), CommandDispatchError> {
        self.dispatch(RecordedCommand::Initialize(command))
    }

    fn start_scan(&self, command: StartScanCommand) -> Result<(), CommandDispatchError> {
        self.dispatch(RecordedCommand::StartScan(command))
    }

    fn stop_scan(&self, command: StopScanCommand) -> Result<(), CommandDispatchError> {
        self.dispatch(RecordedCommand::StopScan(command))
    }

    fn connect(&self, command: ConnectCommand) -> Result<(), CommandDispatchError> {
        self.dispatch(RecordedCommand::Connect(command))
    }

    fn disconnect(&self, command: DisconnectCommand) -> Result<(), CommandDispatchError> {
        self.dispatch(RecordedCommand::Disconnect(command))
    }

    fn discover_gatt(&self, command: DiscoverGattCommand) -> Result<(), CommandDispatchError> {
        self.dispatch(RecordedCommand::DiscoverGatt(command))
    }

    fn read_characteristic(
        &self,
        command: ReadCharacteristicCommand,
    ) -> Result<(), CommandDispatchError> {
        self.dispatch(RecordedCommand::ReadCharacteristic(command))
    }

    fn write_characteristic(
        &self,
        command: WriteCharacteristicCommand,
    ) -> Result<(), CommandDispatchError> {
        self.dispatch(RecordedCommand::WriteCharacteristic(command))
    }

    fn set_notifications(
        &self,
        command: SetNotificationsCommand,
    ) -> Result<(), CommandDispatchError> {
        self.dispatch(RecordedCommand::SetNotifications(command))
    }

    fn read_rssi(&self, command: ReadRssiCommand) -> Result<(), CommandDispatchError> {
        self.dispatch(RecordedCommand::ReadRssi(command))
    }

    fn cancel_operation(
        &self,
        request: CancelOperationRequest,
    ) -> Result<(), CommandDispatchError> {
        match self.state.cancellation_policy {
            CancellationPolicy::Accept => Ok(()),
            CancellationPolicy::Reject => Err(CommandDispatchError::OperationNotCancellable {
                target_command_id: request.target_command_id,
            }),
        }
    }

    fn dispose(&self) -> Result<(), BleHubError> {
        self.state.ingress.lock().unwrap().take();
        if !self.state.disposed.swap(true, Ordering::AcqRel) {
            self.state.dispose_count.fetch_add(1, Ordering::AcqRel);
        }
        Ok(())
    }
}

impl FakeBackendHandle {
    pub fn advance(&self) -> Option<EventIngressStatus> {
        self.state.runner.advance(|event| {
            self.state
                .ingress
                .lock()
                .unwrap()
                .clone()
                .map_or(EventIngressStatus::Closed, |ingress| ingress.push(event))
        })
    }

    pub fn advance_all(&self) -> Vec<EventIngressStatus> {
        std::iter::from_fn(|| self.advance()).collect()
    }

    pub fn pending_event_count(&self) -> usize {
        self.state.runner.pending_event_count()
    }

    pub fn emit(&self, event: BleEvent) -> EventIngressStatus {
        self.state.runner.emit(event, |event| {
            self.state
                .ingress
                .lock()
                .unwrap()
                .clone()
                .map_or(EventIngressStatus::Closed, |ingress| ingress.push(event))
        })
    }

    /// Captures a temporary callback reference. Tests must call this before
    /// dispose when they need to model queued native work completing late.
    pub fn ingress_for_callback(&self) -> Option<Arc<BackendEventIngressHandle>> {
        self.state.ingress.lock().unwrap().clone()
    }

    pub fn push_statuses(&self) -> Vec<EventIngressStatus> {
        self.state.runner.push_statuses()
    }

    pub fn commands(&self) -> Vec<RecordedCommand> {
        self.state.runner.commands()
    }

    pub fn dispose_count(&self) -> usize {
        self.state.dispose_count.load(Ordering::Acquire)
    }

    pub fn finish_completion(
        &self,
        command_id: blehub::CommandId,
        expected: CompletionExpectation,
    ) {
        self.state.runner.finish_completion(command_id, expected);
    }

    pub fn terminal_attempted(&self, command_id: blehub::CommandId) -> bool {
        self.state.runner.terminal_attempted(command_id)
    }

    pub fn contract_violations(&self) -> Vec<BackendContractViolation> {
        self.state.runner.contract_violations()
    }
}
