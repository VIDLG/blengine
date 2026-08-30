use std::sync::{Arc, RwLock};

use crate::{
    BackendCapabilities, BackendEventIngressHandle, BleBackend, BleEventSink, BleHubError,
    CommandDispatchError, ConnectCommand, DisconnectCommand, DiscoverGattCommand,
    InitializeCommand, ReadCharacteristicCommand, ReadRssiCommand, SetNotificationsCommand,
    StartScanCommand, StopScanCommand, WriteCharacteristicCommand, ingress::EventIngressState,
};

pub struct BleHub {
    backend: RwLock<Option<Arc<dyn BleBackend>>>,
    capabilities: BackendCapabilities,
    ingress: Arc<EventIngressState>,
}

fn dispose_after_assembly_failure(backend: &dyn BleBackend, stage: &'static str) {
    if let Err(error) = backend.dispose() {
        tracing::error!(
            target: "blehub::lifecycle",
            diagnostic = "assembly_rollback_failed",
            stage,
            error = %error,
            "backend cleanup failed while rolling back BleHub assembly"
        );
    }
}

impl BleHub {
    pub fn new(
        backend: Arc<dyn BleBackend>,
        downstream: Box<dyn BleEventSink>,
    ) -> Result<Self, BleHubError> {
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

        Ok(Self {
            backend: RwLock::new(Some(backend)),
            capabilities,
            ingress,
        })
    }

    pub fn capabilities(&self) -> BackendCapabilities {
        self.capabilities.clone()
    }

    /// Holds the command-gate read guard through the backend call so dispose's
    /// write guard cannot remove or dispose the backend during dispatch.
    fn with_backend(
        &self,
        dispatch: impl FnOnce(&dyn BleBackend) -> Result<(), CommandDispatchError>,
    ) -> Result<(), CommandDispatchError> {
        let backend = self.backend.read().map_err(|error| {
            tracing::error!(
                target: "blehub::lifecycle",
                diagnostic = "command_gate_poisoned",
                detail = %error,
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
        let mut backend_slot = match self.backend.write() {
            Ok(backend_slot) => backend_slot,
            Err(error) => {
                tracing::error!(
                    target: "blehub::lifecycle",
                    diagnostic = "command_gate_poisoned",
                    operation = "dispose",
                    detail = %error,
                    "recovered poisoned BleHub command gate"
                );
                error.into_inner()
            }
        };
        let Some(backend) = backend_slot.take() else {
            return Ok(());
        };

        self.ingress.close();
        let backend_result = backend.dispose();
        if let Err(error) = &backend_result {
            tracing::error!(
                target: "blehub::lifecycle",
                diagnostic = "backend_dispose_failed",
                error = %error,
                "backend cleanup failed during BleHub dispose"
            );
        }
        drop(backend);
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
    use std::sync::{
        Arc, Mutex, Weak,
        atomic::{AtomicUsize, Ordering},
    };

    use crate::{
        BackendCapabilities, BackendEventIngressHandle, BleBackend, BleEvent, BleEventSink,
        BleHubError, CommandDispatchError, CommandId, ConnectCommand, DisconnectCommand,
        DiscoverGattCommand, EventIngressStatus, EventSinkError, InitializeCommand, Platform,
        ReadCharacteristicCommand, ReadRssiCommand, ScanSessionId, SetNotificationsCommand,
        StartScanCommand, StopScanCommand, WriteCharacteristicCommand,
    };

    use super::BleHub;

    struct ConstructorBackend {
        state: Arc<ConstructorBackendState>,
        capabilities: Result<BackendCapabilities, BleHubError>,
        bind_result: Result<(), BleHubError>,
        dispose_result: Result<(), BleHubError>,
        push_during_bind: bool,
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

    fn tracking_sink() -> (Box<dyn BleEventSink>, Arc<AtomicUsize>) {
        let drop_count = Arc::new(AtomicUsize::new(0));
        (
            Box::new(DropTrackingSink {
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
