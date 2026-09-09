use std::sync::{Arc, Condvar, Mutex, RwLock};

use crate::{
    BackendCapabilities, BackendEventIngressHandle, BleBackend, BleEventSink, BleHubError,
    CommandDispatchError, ConnectCommand, DisconnectCommand, DiscoverGattCommand,
    InitializeCommand, ReadCharacteristicCommand, ReadRssiCommand, SetNotificationsCommand,
    StartScanCommand, StopScanCommand, WriteCharacteristicCommand, ingress::EventIngressState,
};

#[derive(uniffi::Object)]
pub struct BleHub {
    backend: RwLock<Option<Arc<dyn BleBackend>>>,
    capabilities: BackendCapabilities,
    ingress: Arc<EventIngressState>,
    lifecycle: (Mutex<LifecycleState>, Condvar),
}

#[derive(Clone)]
enum LifecycleState {
    Open,
    Closing,
    Closed(Option<BleHubError>),
}

fn dispose_after_assembly_failure(backend: &dyn BleBackend, stage: &'static str) {
    if let Err(_error) = backend.dispose() {
        tracing::error!(
            target: "blehub::lifecycle",
            diagnostic = "assembly_rollback_failed",
            stage,
            error_class = "backend_cleanup",
            "backend cleanup failed while rolling back BleHub assembly"
        );
    }
}

impl BleHub {
    /// Holds the command-gate read guard through the backend call so dispose's
    /// write guard cannot remove or dispose the backend during dispatch.
    fn with_backend(
        &self,
        dispatch: impl FnOnce(&dyn BleBackend) -> Result<(), CommandDispatchError>,
    ) -> Result<(), CommandDispatchError> {
        let backend = self.backend.read().map_err(|_error| {
            tracing::error!(
                target: "blehub::lifecycle",
                diagnostic = "command_gate_poisoned",
                error_class = "mutex_poisoned",
                "failed to acquire BleHub command gate"
            );
            CommandDispatchError::BackendUnavailable {
                detail: "command gate is poisoned".to_owned(),
                platform_details: None,
            }
        })?;
        let backend = backend.as_deref().ok_or(CommandDispatchError::Closed)?;
        dispatch(backend)
    }
}

#[uniffi::export]
impl BleHub {
    #[uniffi::constructor]
    pub fn new(
        backend: Arc<dyn BleBackend>,
        downstream: Arc<dyn BleEventSink>,
    ) -> Result<Arc<Self>, BleHubError> {
        let capabilities = match backend
            .capabilities()
            .and_then(BackendCapabilities::validated)
        {
            Ok(capabilities) => capabilities,
            Err(error) => {
                dispose_after_assembly_failure(backend.as_ref(), "capabilities");
                return Err(error);
            }
        };

        let ingress = Arc::new(EventIngressState::new(downstream));
        let ingress_handle = Arc::new(BackendEventIngressHandle::new(Arc::downgrade(&ingress)));

        // Move the sole long-lived handle root into the backend. BleHub retains
        // only the separate ingress state; callback work may clone temporarily.
        if let Err(error) = backend.bind_event_ingress(ingress_handle) {
            ingress.close();
            dispose_after_assembly_failure(backend.as_ref(), "bind_event_ingress");
            return Err(error);
        }
        if let Err(detail) = ingress.activate() {
            ingress.close();
            dispose_after_assembly_failure(backend.as_ref(), "activate_ingress");
            return Err(BleHubError::FfiContract {
                detail: detail.to_owned(),
            });
        }

        Ok(Arc::new(Self {
            backend: RwLock::new(Some(backend)),
            capabilities,
            ingress,
            lifecycle: (Mutex::new(LifecycleState::Open), Condvar::new()),
        }))
    }

    pub fn capabilities(&self) -> BackendCapabilities {
        self.capabilities.clone()
    }

    pub fn initialize(&self, command: InitializeCommand) -> Result<(), CommandDispatchError> {
        self.with_backend(|backend| backend.initialize(command))
    }

    pub fn start_scan(&self, command: StartScanCommand) -> Result<(), CommandDispatchError> {
        let command = command.validated()?;
        self.with_backend(|backend| backend.start_scan(command))
    }

    pub fn stop_scan(&self, command: StopScanCommand) -> Result<(), CommandDispatchError> {
        self.with_backend(|backend| backend.stop_scan(command))
    }

    pub fn connect(&self, command: ConnectCommand) -> Result<(), CommandDispatchError> {
        self.with_backend(|backend| backend.connect(command))
    }

    pub fn disconnect(&self, command: DisconnectCommand) -> Result<(), CommandDispatchError> {
        self.with_backend(|backend| backend.disconnect(command))
    }

    pub fn discover_gatt(&self, command: DiscoverGattCommand) -> Result<(), CommandDispatchError> {
        self.with_backend(|backend| backend.discover_gatt(command))
    }

    pub fn read_characteristic(
        &self,
        command: ReadCharacteristicCommand,
    ) -> Result<(), CommandDispatchError> {
        self.with_backend(|backend| backend.read_characteristic(command))
    }

    pub fn write_characteristic(
        &self,
        command: WriteCharacteristicCommand,
    ) -> Result<(), CommandDispatchError> {
        command.validate()?;
        self.with_backend(|backend| backend.write_characteristic(command))
    }

    pub fn set_notifications(
        &self,
        command: SetNotificationsCommand,
    ) -> Result<(), CommandDispatchError> {
        self.with_backend(|backend| backend.set_notifications(command))
    }

    pub fn read_rssi(&self, command: ReadRssiCommand) -> Result<(), CommandDispatchError> {
        self.with_backend(|backend| backend.read_rssi(command))
    }

    pub fn dispose(&self) -> Result<(), BleHubError> {
        let mut lifecycle = self
            .lifecycle
            .0
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        loop {
            match &*lifecycle {
                LifecycleState::Open => {
                    *lifecycle = LifecycleState::Closing;
                    break;
                }
                LifecycleState::Closing => {
                    lifecycle = self
                        .lifecycle
                        .1
                        .wait(lifecycle)
                        .unwrap_or_else(|error| error.into_inner());
                }
                LifecycleState::Closed(result) => return result.clone().map_or(Ok(()), Err),
            }
        }
        drop(lifecycle);

        let backend = {
            let mut backend_slot = match self.backend.write() {
                Ok(backend_slot) => backend_slot,
                Err(error) => {
                    tracing::error!(
                        target: "blehub::lifecycle",
                        diagnostic = "command_gate_poisoned",
                        operation = "dispose",
                        error_class = "mutex_poisoned",
                        "recovered poisoned BleHub command gate"
                    );
                    error.into_inner()
                }
            };
            backend_slot
                .take()
                .expect("open lifecycle must own the backend")
        };

        self.ingress.close();
        let backend_result = backend.dispose();
        if let Err(_error) = &backend_result {
            tracing::error!(
                target: "blehub::lifecycle",
                diagnostic = "backend_dispose_failed",
                error_class = "backend_cleanup",
                "backend cleanup failed during BleHub dispose"
            );
        }
        drop(backend);
        let mut lifecycle = self
            .lifecycle
            .0
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        *lifecycle = LifecycleState::Closed(backend_result.clone().err());
        self.lifecycle.1.notify_all();
        backend_result
    }
}

impl Drop for BleHub {
    fn drop(&mut self) {
        let _ = self.dispose();
    }
}

#[cfg(test)]
mod tests {
    use std::{
        sync::{
            Arc, Barrier, Mutex, Weak,
            atomic::{AtomicUsize, Ordering},
            mpsc,
        },
        thread,
        time::{Duration, Instant},
    };

    use crate::{
        BackendCapabilities, BackendEventIngressHandle, BleBackend, BleEvent, BleEventSink,
        BleHubError, CommandDispatchError, CommandId, ConnectCommand, DisconnectCommand,
        DiscoverGattCommand, EventIngressStatus, EventSinkError, InitializeCommand, Platform,
        ReadCharacteristicCommand, ReadRssiCommand, ScanOptions, ScanSessionId,
        SetNotificationsCommand, StartScanCommand, StopScanCommand, WriteCharacteristicCommand,
    };

    use super::BleHub;

    struct ConstructorBackend {
        state: Arc<ConstructorBackendState>,
        capabilities: Result<BackendCapabilities, BleHubError>,
        bind_result: Result<(), BleHubError>,
        dispose_result: Result<(), BleHubError>,
        push_during_bind: bool,
        dispose_barriers: Option<(Arc<Barrier>, Arc<Barrier>)>,
    }

    struct ConstructorBackendState {
        ingress: Mutex<Option<Arc<BackendEventIngressHandle>>>,
        observed_ingress: Mutex<Option<Weak<BackendEventIngressHandle>>>,
        bind_count: AtomicUsize,
        dispatch_count: AtomicUsize,
        dispose_count: AtomicUsize,
        bind_push_status: Mutex<Option<EventIngressStatus>>,
    }

    impl ConstructorBackend {
        fn new(
            capabilities: Result<BackendCapabilities, BleHubError>,
            bind_result: Result<(), BleHubError>,
            dispose_result: Result<(), BleHubError>,
            push_during_bind: bool,
        ) -> (Self, Arc<ConstructorBackendState>) {
            let state = Arc::new(ConstructorBackendState {
                ingress: Mutex::new(None),
                observed_ingress: Mutex::new(None),
                bind_count: AtomicUsize::new(0),
                dispatch_count: AtomicUsize::new(0),
                dispose_count: AtomicUsize::new(0),
                bind_push_status: Mutex::new(None),
            });
            (
                Self {
                    state: Arc::clone(&state),
                    capabilities,
                    bind_result,
                    dispose_result,
                    push_during_bind,
                    dispose_barriers: None,
                },
                state,
            )
        }

        fn reject_dispatch(&self) -> Result<(), CommandDispatchError> {
            self.state.dispatch_count.fetch_add(1, Ordering::AcqRel);
            Err(CommandDispatchError::InvalidState {
                detail: "constructor test backend does not dispatch commands".to_owned(),
            })
        }
    }

    impl BleBackend for ConstructorBackend {
        fn capabilities(&self) -> Result<BackendCapabilities, BleHubError> {
            self.capabilities.clone()
        }

        fn bind_event_ingress(
            &self,
            ingress: Arc<BackendEventIngressHandle>,
        ) -> Result<(), BleHubError> {
            self.state.bind_count.fetch_add(1, Ordering::AcqRel);
            *self.state.observed_ingress.lock().unwrap() = Some(Arc::downgrade(&ingress));
            self.bind_result.clone()?;
            if self.push_during_bind {
                *self.state.bind_push_status.lock().unwrap() = Some(ingress.push(scan_started()));
            }
            *self.state.ingress.lock().unwrap() = Some(ingress);
            Ok(())
        }

        fn initialize(&self, _: InitializeCommand) -> Result<(), CommandDispatchError> {
            self.reject_dispatch()
        }

        fn start_scan(&self, _: StartScanCommand) -> Result<(), CommandDispatchError> {
            self.reject_dispatch()
        }

        fn stop_scan(&self, _: StopScanCommand) -> Result<(), CommandDispatchError> {
            self.reject_dispatch()
        }

        fn connect(&self, _: ConnectCommand) -> Result<(), CommandDispatchError> {
            self.reject_dispatch()
        }

        fn disconnect(&self, _: DisconnectCommand) -> Result<(), CommandDispatchError> {
            self.reject_dispatch()
        }

        fn discover_gatt(&self, _: DiscoverGattCommand) -> Result<(), CommandDispatchError> {
            self.reject_dispatch()
        }

        fn read_characteristic(
            &self,
            _: ReadCharacteristicCommand,
        ) -> Result<(), CommandDispatchError> {
            self.reject_dispatch()
        }

        fn write_characteristic(
            &self,
            _: WriteCharacteristicCommand,
        ) -> Result<(), CommandDispatchError> {
            self.reject_dispatch()
        }

        fn set_notifications(
            &self,
            _: SetNotificationsCommand,
        ) -> Result<(), CommandDispatchError> {
            self.reject_dispatch()
        }

        fn read_rssi(&self, _: ReadRssiCommand) -> Result<(), CommandDispatchError> {
            self.reject_dispatch()
        }

        fn dispose(&self) -> Result<(), BleHubError> {
            self.state.dispose_count.fetch_add(1, Ordering::AcqRel);
            if let Some((entered, release)) = &self.dispose_barriers {
                entered.wait();
                release.wait();
            }
            self.state.ingress.lock().unwrap().take();
            self.dispose_result.clone()
        }
    }

    struct DropTrackingSink {
        drop_count: Arc<AtomicUsize>,
    }

    impl BleEventSink for DropTrackingSink {
        fn on_event(&self, _: BleEvent) -> Result<(), EventSinkError> {
            Ok(())
        }
    }

    impl Drop for DropTrackingSink {
        fn drop(&mut self) {
            self.drop_count.fetch_add(1, Ordering::AcqRel);
        }
    }

    struct BlockingSink {
        entered: Arc<Barrier>,
        release: Arc<Barrier>,
    }

    impl BleEventSink for BlockingSink {
        fn on_event(&self, _: BleEvent) -> Result<(), EventSinkError> {
            self.entered.wait();
            self.release.wait();
            Ok(())
        }
    }

    fn tracking_sink() -> (Arc<dyn BleEventSink>, Arc<AtomicUsize>) {
        let drop_count = Arc::new(AtomicUsize::new(0));
        (
            Arc::new(DropTrackingSink {
                drop_count: Arc::clone(&drop_count),
            }),
            drop_count,
        )
    }

    fn backend(
        capabilities: Result<BackendCapabilities, BleHubError>,
        bind_result: Result<(), BleHubError>,
        dispose_result: Result<(), BleHubError>,
    ) -> (Arc<ConstructorBackend>, Arc<ConstructorBackendState>) {
        let (backend, state) =
            ConstructorBackend::new(capabilities, bind_result, dispose_result, false);
        (Arc::new(backend), state)
    }

    fn observed_ingress(state: &ConstructorBackendState) -> Weak<BackendEventIngressHandle> {
        state.observed_ingress.lock().unwrap().clone().unwrap()
    }

    fn blocking_dispose_backend(
        entered: Arc<Barrier>,
        release: Arc<Barrier>,
    ) -> (Arc<ConstructorBackend>, Arc<ConstructorBackendState>) {
        let (mut backend, state) =
            backend(Ok(BackendCapabilities::new(Platform::Test)), Ok(()), Ok(()));
        Arc::get_mut(&mut backend).unwrap().dispose_barriers = Some((entered, release));
        (backend, state)
    }

    fn scan_started() -> BleEvent {
        BleEvent::ScanStarted {
            command_id: CommandId { value: 1 },
            scan_id: ScanSessionId { value: 2 },
        }
    }

    #[test]
    fn capabilities_failure_disposes_before_binding() {
        let (backend, state) = backend(
            Err(BleHubError::BackendAssembly {
                detail: "capabilities unavailable".to_owned(),
            }),
            Ok(()),
            Ok(()),
        );
        let (sink, sink_drop_count) = tracking_sink();

        let result = BleHub::new(backend, sink);

        assert!(matches!(result, Err(BleHubError::BackendAssembly { .. })));
        assert_eq!(state.bind_count.load(Ordering::Acquire), 0);
        assert_eq!(state.dispose_count.load(Ordering::Acquire), 1);
        assert_eq!(sink_drop_count.load(Ordering::Acquire), 1);
    }

    #[test]
    fn bind_failure_does_not_retain_ingress_root() {
        let (backend, state) = backend(
            Ok(BackendCapabilities::new(Platform::Test)),
            Err(BleHubError::BackendAssembly {
                detail: "bind rejected".to_owned(),
            }),
            Ok(()),
        );
        let (sink, sink_drop_count) = tracking_sink();

        let result = BleHub::new(backend, sink);

        assert!(matches!(result, Err(BleHubError::BackendAssembly { .. })));
        assert!(observed_ingress(&state).upgrade().is_none());
        assert_eq!(state.dispose_count.load(Ordering::Acquire), 1);
        assert_eq!(sink_drop_count.load(Ordering::Acquire), 1);
    }

    #[test]
    fn invalid_contract_version_rolls_back_before_binding() {
        let (backend, state) = backend(
            Ok(BackendCapabilities {
                contract_version: 6,
                ..BackendCapabilities::new(Platform::Test)
            }),
            Ok(()),
            Ok(()),
        );
        let (sink, sink_drop_count) = tracking_sink();

        let result = BleHub::new(backend, sink);

        assert!(matches!(result, Err(BleHubError::FfiContract { .. })));
        assert_eq!(state.bind_count.load(Ordering::Acquire), 0);
        assert_eq!(state.dispose_count.load(Ordering::Acquire), 1);
        assert_eq!(sink_drop_count.load(Ordering::Acquire), 1);
    }

    #[test]
    fn push_during_bind_is_closed_and_rejects_construction() {
        let (backend, state) = ConstructorBackend::new(
            Ok(BackendCapabilities::new(Platform::Test)),
            Ok(()),
            Ok(()),
            true,
        );
        let (sink, sink_drop_count) = tracking_sink();

        let result = BleHub::new(Arc::new(backend), sink);

        assert!(matches!(result, Err(BleHubError::FfiContract { .. })));
        assert_eq!(
            *state.bind_push_status.lock().unwrap(),
            Some(EventIngressStatus::Closed)
        );
        assert!(observed_ingress(&state).upgrade().is_none());
        assert_eq!(state.dispose_count.load(Ordering::Acquire), 1);
        assert_eq!(sink_drop_count.load(Ordering::Acquire), 1);
    }

    #[test]
    fn successful_dispose_releases_backend_ingress_root() {
        let (backend, state) =
            backend(Ok(BackendCapabilities::new(Platform::Test)), Ok(()), Ok(()));
        let (sink, sink_drop_count) = tracking_sink();
        let hub = BleHub::new(backend, sink).unwrap();
        let ingress = observed_ingress(&state);
        assert!(ingress.upgrade().is_some());

        hub.dispose().unwrap();
        hub.dispose().unwrap();

        assert!(ingress.upgrade().is_none());
        assert_eq!(state.dispose_count.load(Ordering::Acquire), 1);
        assert_eq!(sink_drop_count.load(Ordering::Acquire), 1);
    }

    #[test]
    fn failing_dispose_still_releases_backend_ingress_root() {
        let cleanup_error = BleHubError::BackendCleanup {
            detail: "scripted cleanup failure".to_owned(),
            platform_details: None,
        };
        let (backend, state) = backend(
            Ok(BackendCapabilities::new(Platform::Test)),
            Ok(()),
            Err(cleanup_error.clone()),
        );
        let (sink, sink_drop_count) = tracking_sink();
        let hub = BleHub::new(backend, sink).unwrap();
        let ingress = observed_ingress(&state);

        assert_eq!(hub.dispose(), Err(cleanup_error));

        assert!(ingress.upgrade().is_none());
        assert_eq!(state.dispose_count.load(Ordering::Acquire), 1);
        assert_eq!(sink_drop_count.load(Ordering::Acquire), 1);
    }

    #[test]
    fn late_temporary_callback_is_closed_after_dispose() {
        let (backend, state) =
            backend(Ok(BackendCapabilities::new(Platform::Test)), Ok(()), Ok(()));
        let (sink, sink_drop_count) = tracking_sink();
        let hub = BleHub::new(backend, sink).unwrap();
        let callback = state.ingress.lock().unwrap().clone().unwrap();

        hub.dispose().unwrap();

        assert_eq!(callback.push(scan_started()), EventIngressStatus::Closed);
        assert_eq!(sink_drop_count.load(Ordering::Acquire), 1);
    }

    #[test]
    fn dispose_releases_command_gate_before_waiting_for_ingress_drain() {
        let (backend, state) =
            backend(Ok(BackendCapabilities::new(Platform::Test)), Ok(()), Ok(()));
        let entered = Arc::new(Barrier::new(2));
        let release = Arc::new(Barrier::new(2));
        let hub = BleHub::new(
            backend,
            Arc::new(BlockingSink {
                entered: Arc::clone(&entered),
                release: Arc::clone(&release),
            }),
        )
        .unwrap();
        let callback = state.ingress.lock().unwrap().clone().unwrap();
        let push = thread::spawn(move || callback.push(scan_started()));
        entered.wait();

        let dispose_hub = Arc::clone(&hub);
        let dispose = thread::spawn(move || dispose_hub.dispose());
        let deadline = Instant::now() + Duration::from_secs(1);
        loop {
            if let Ok(backend_slot) = hub.backend.try_read()
                && backend_slot.is_none()
            {
                break;
            }
            assert!(
                Instant::now() < deadline,
                "dispose did not release the command gate after closing the backend slot"
            );
            thread::yield_now();
        }

        let command = StartScanCommand {
            command_id: CommandId { value: 3 },
            scan_session_id: ScanSessionId { value: 4 },
            options: ScanOptions {
                service_uuids: Vec::new(),
                name_prefix: None,
                company_id: None,
                allow_duplicates: false,
            },
        };
        assert_eq!(hub.start_scan(command), Err(CommandDispatchError::Closed));
        assert_eq!(state.dispose_count.load(Ordering::Acquire), 0);

        release.wait();
        assert_eq!(push.join().unwrap(), EventIngressStatus::Delivered);
        assert_eq!(dispose.join().unwrap(), Ok(()));
        assert_eq!(state.dispose_count.load(Ordering::Acquire), 1);
    }

    #[test]
    fn concurrent_dispose_waits_for_the_first_cleanup() {
        let (backend, state) =
            backend(Ok(BackendCapabilities::new(Platform::Test)), Ok(()), Ok(()));
        let entered = Arc::new(Barrier::new(2));
        let release = Arc::new(Barrier::new(2));
        let hub = BleHub::new(
            backend,
            Arc::new(BlockingSink {
                entered: Arc::clone(&entered),
                release: Arc::clone(&release),
            }),
        )
        .unwrap();
        let callback = state.ingress.lock().unwrap().clone().unwrap();
        let push = thread::spawn(move || callback.push(scan_started()));
        entered.wait();

        let first_hub = Arc::clone(&hub);
        let first = thread::spawn(move || first_hub.dispose());
        let second_hub = Arc::clone(&hub);
        let (done_tx, done_rx) = mpsc::channel();
        let second = thread::spawn(move || {
            let result = second_hub.dispose();
            done_tx.send(result).unwrap();
        });

        assert!(done_rx.recv_timeout(Duration::from_millis(50)).is_err());
        assert_eq!(state.dispose_count.load(Ordering::Acquire), 0);

        release.wait();
        assert_eq!(push.join().unwrap(), EventIngressStatus::Delivered);
        assert_eq!(first.join().unwrap(), Ok(()));
        assert_eq!(
            done_rx.recv_timeout(Duration::from_secs(1)).unwrap(),
            Ok(())
        );
        second.join().unwrap();
        assert_eq!(state.dispose_count.load(Ordering::Acquire), 1);
    }

    #[test]
    fn concurrent_dispose_waits_during_backend_cleanup() {
        let entered = Arc::new(Barrier::new(2));
        let release = Arc::new(Barrier::new(2));
        let (backend, state) = blocking_dispose_backend(Arc::clone(&entered), Arc::clone(&release));
        let hub = BleHub::new(
            backend,
            Arc::new(DropTrackingSink {
                drop_count: Arc::new(AtomicUsize::new(0)),
            }),
        )
        .unwrap();
        let first_hub = Arc::clone(&hub);
        let first = thread::spawn(move || first_hub.dispose());
        entered.wait();
        let second_hub = Arc::clone(&hub);
        let (done_tx, done_rx) = mpsc::channel();
        let second = thread::spawn(move || done_tx.send(second_hub.dispose()).unwrap());
        assert!(done_rx.recv_timeout(Duration::from_millis(50)).is_err());
        assert_eq!(state.dispose_count.load(Ordering::Acquire), 1);
        release.wait();
        assert_eq!(first.join().unwrap(), Ok(()));
        assert_eq!(
            done_rx.recv_timeout(Duration::from_secs(1)).unwrap(),
            Ok(())
        );
        second.join().unwrap();
    }

    #[test]
    fn multiple_close_observers_receive_the_first_cleanup_error() {
        let cleanup_error = BleHubError::BackendCleanup {
            detail: "first cleanup failure".to_owned(),
            platform_details: None,
        };
        let (backend, _) = backend(
            Ok(BackendCapabilities::new(Platform::Test)),
            Ok(()),
            Err(cleanup_error.clone()),
        );
        let hub = BleHub::new(
            backend,
            Arc::new(DropTrackingSink {
                drop_count: Arc::new(AtomicUsize::new(0)),
            }),
        )
        .unwrap();
        assert_eq!(hub.dispose(), Err(cleanup_error.clone()));
        let observers: Vec<_> = (0..4)
            .map(|_| {
                let hub = Arc::clone(&hub);
                thread::spawn(move || hub.dispose())
            })
            .collect();
        for observer in observers {
            assert_eq!(observer.join().unwrap(), Err(cleanup_error.clone()));
        }
    }

    #[test]
    fn extra_backend_arc_does_not_keep_ingress_root_after_dispose() {
        let (backend, state) =
            backend(Ok(BackendCapabilities::new(Platform::Test)), Ok(()), Ok(()));
        let extra_backend = Arc::clone(&backend);
        let (sink, _) = tracking_sink();
        let hub = BleHub::new(backend, sink).unwrap();
        let ingress = observed_ingress(&state);

        hub.dispose().unwrap();

        assert!(ingress.upgrade().is_none());
        assert_eq!(Arc::strong_count(&extra_backend), 1);
    }
}
