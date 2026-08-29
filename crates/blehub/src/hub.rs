use std::sync::{Arc, RwLock};

use crate::{
    BackendCapabilities, BackendEventIngressHandle, BleBackend, BleEventSink, BleHubError,
    CommandDispatchError, ConnectCommand, StartScanCommand, ingress::EventIngressState,
};

pub struct BleHub {
    backend: RwLock<Option<Arc<dyn BleBackend>>>,
    capabilities: BackendCapabilities,
    ingress: Arc<EventIngressState>,
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
                let _ = backend.dispose();
                return Err(error);
            }
        };

        let ingress = Arc::new(EventIngressState::new(downstream));
        let ingress_handle = Arc::new(BackendEventIngressHandle::new(Arc::downgrade(&ingress)));

        // Move the sole long-lived handle root into the backend. BleHub retains
        // only the separate ingress state; callback work may clone temporarily.
        if let Err(error) = backend.bind_event_ingress(ingress_handle) {
            ingress.close();
            let _ = backend.dispose();
            return Err(error);
        }
        if let Err(error) = ingress.activate() {
            ingress.close();
            let _ = backend.dispose();
            return Err(BleHubError::FfiContract {
                detail: error.to_string(),
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
        let backend =
            self.backend
                .read()
                .map_err(|_| CommandDispatchError::BackendUnavailable {
                    detail: "command gate is poisoned".to_owned(),
                    platform: None,
                })?;
        let backend = backend.as_deref().ok_or(CommandDispatchError::Closed)?;
        dispatch(backend)
    }

    pub fn start_scan(&self, command: StartScanCommand) -> Result<(), CommandDispatchError> {
        command.validate()?;
        self.with_backend(|backend| backend.start_scan(command))
    }

    pub fn connect(&self, command: ConnectCommand) -> Result<(), CommandDispatchError> {
        command.validate()?;
        self.with_backend(|backend| backend.connect(command))
    }

    pub fn dispose(&self) -> Result<(), BleHubError> {
        let mut backend_slot = self
            .backend
            .write()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let Some(backend) = backend_slot.take() else {
            return Ok(());
        };

        self.ingress.close();
        let backend_result = backend.dispose();
        drop(backend);
        backend_result
    }
}

impl Drop for BleHub {
    fn drop(&mut self) {
        if let Err(error) = self.dispose() {
            eprintln!("BleHub dispose failed during Drop: {error}");
        }
    }
}

#[cfg(test)]
mod tests {
    use std::sync::{
        Arc, Mutex, Weak,
        atomic::{AtomicUsize, Ordering},
    };

    use crate::{
        BackendCapabilities, BackendEventIngressHandle, BleBackend, BleEvent, BleEventPayload,
        BleEventSink, BleHubError, CommandDispatchError, CommandId, ConnectCommand, ConnectionId,
        EventIngressStatus, EventSinkError, PeripheralId, Platform, ScanSessionId,
        StartScanCommand,
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

        fn start_scan(&self, _: StartScanCommand) -> Result<(), CommandDispatchError> {
            self.state.dispatch_count.fetch_add(1, Ordering::AcqRel);
            Err(CommandDispatchError::InvalidState {
                detail: "constructor test backend does not dispatch commands".to_owned(),
            })
        }

        fn connect(&self, _: ConnectCommand) -> Result<(), CommandDispatchError> {
            self.state.dispatch_count.fetch_add(1, Ordering::AcqRel);
            Err(CommandDispatchError::InvalidState {
                detail: "constructor test backend does not dispatch commands".to_owned(),
            })
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
        BleEvent {
            payload: BleEventPayload::ScanStarted {
                command_id: CommandId::new(1),
                scan_id: ScanSessionId::new(2),
            },
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
                platform: Platform::Test,
                contract_version: 4,
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
    fn invalid_commands_are_rejected_before_backend_dispatch() {
        let (backend, state) =
            backend(Ok(BackendCapabilities::new(Platform::Test)), Ok(()), Ok(()));
        let (sink, _) = tracking_sink();
        let hub = BleHub::new(backend, sink).unwrap();

        assert_eq!(
            hub.start_scan(StartScanCommand {
                command_id: CommandId::new(0),
                scan_session_id: ScanSessionId::new(1),
            }),
            Err(CommandDispatchError::InvalidRequest {
                detail: "command_id must be nonzero".to_owned(),
            })
        );
        assert_eq!(
            hub.connect(ConnectCommand {
                command_id: CommandId::new(1),
                connection_id: ConnectionId::new(0),
                peripheral_id: PeripheralId::new(2),
            }),
            Err(CommandDispatchError::InvalidRequest {
                detail: "connection_id must be nonzero".to_owned(),
            })
        );
        assert_eq!(state.dispatch_count.load(Ordering::Acquire), 0);
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
            platform: None,
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
