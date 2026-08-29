use std::{
    panic::{AssertUnwindSafe, catch_unwind},
    sync::{Arc, RwLock},
};

use crate::{
    BackendCapabilities, BleBackend, BleEventSink, BleHubError, CommandDispatchError,
    ConnectCommand, StartScanCommand,
    ingress::{
        BackendEventIngressRegistration, BackendEventIngressRegistry, EventIngressState,
        global_backend_event_ingress_registry,
    },
};

pub struct BleHub {
    backend: RwLock<Option<Box<dyn BleBackend>>>,
    capabilities: BackendCapabilities,
    ingress: Arc<EventIngressState>,
    ingress_registration: BackendEventIngressRegistration,
}

impl BleHub {
    pub fn new(
        backend: Box<dyn BleBackend>,
        downstream: Box<dyn BleEventSink>,
    ) -> Result<Self, BleHubError> {
        Self::new_with_registry(
            backend,
            downstream,
            Arc::clone(global_backend_event_ingress_registry()),
        )
    }

    fn new_with_registry(
        backend: Box<dyn BleBackend>,
        downstream: Box<dyn BleEventSink>,
        registry: Arc<BackendEventIngressRegistry>,
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
        let token = match registry.allocate_token() {
            Ok(token) => token,
            Err(error) => {
                ingress.close_and_drain();
                let _ = backend.dispose();
                return Err(error);
            }
        };

        if let Err(error) = backend.bind_event_ingress(token) {
            ingress.close_and_drain();
            let _ = backend.dispose();
            return Err(error);
        }

        let registration = match registry.register(token, Arc::downgrade(&ingress)) {
            Ok(registration) => registration,
            Err(error) => {
                ingress.close_and_drain();
                let _ = backend.dispose();
                return Err(error);
            }
        };

        Ok(Self {
            backend: RwLock::new(Some(backend)),
            capabilities,
            ingress,
            ingress_registration: registration,
        })
    }

    pub fn capabilities(&self) -> BackendCapabilities {
        self.capabilities.clone()
    }

    pub fn start_scan(&self, command: StartScanCommand) -> Result<(), CommandDispatchError> {
        if !command.command_id.is_valid() || !command.scan_session_id.is_valid() {
            return Err(CommandDispatchError::InvalidRequest {
                detail: "command_id and scan_session_id must be nonzero".to_owned(),
            });
        }
        let backend =
            self.backend
                .read()
                .map_err(|_| CommandDispatchError::BackendUnavailable {
                    detail: "command gate is poisoned".to_owned(),
                    platform: None,
                })?;
        backend
            .as_ref()
            .ok_or(CommandDispatchError::Closed)?
            .start_scan(command)
    }

    pub fn connect(&self, command: ConnectCommand) -> Result<(), CommandDispatchError> {
        if !command.command_id.is_valid()
            || !command.connection_id.is_valid()
            || !command.peripheral_id.is_valid()
        {
            return Err(CommandDispatchError::InvalidRequest {
                detail: "command_id, connection_id and peripheral_id must be nonzero".to_owned(),
            });
        }
        let backend =
            self.backend
                .read()
                .map_err(|_| CommandDispatchError::BackendUnavailable {
                    detail: "command gate is poisoned".to_owned(),
                    platform: None,
                })?;
        backend
            .as_ref()
            .ok_or(CommandDispatchError::Closed)?
            .connect(command)
    }

    pub fn dispose(&self) -> Result<(), BleHubError> {
        let mut backend_slot = self
            .backend
            .write()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let Some(backend) = backend_slot.take() else {
            return Ok(());
        };

        self.ingress.close_and_drain();
        let backend_result =
            catch_unwind(AssertUnwindSafe(|| backend.dispose())).unwrap_or_else(|_| {
                Err(BleHubError::BackendCleanup {
                    detail: "backend panicked during dispose".to_owned(),
                    platform: None,
                })
            });
        self.ingress_registration.unregister();
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
        Arc, Mutex,
        atomic::{AtomicUsize, Ordering},
    };

    use crate::{
        BackendCapabilities, BackendEventIngressToken, BleBackend, BleEvent, BleEventSink,
        BleHubError, CommandDispatchError, ConnectCommand, EventSinkError, Platform,
        StartScanCommand,
        ingress::{BackendEventIngressRegistry, EventIngressState},
    };

    use super::BleHub;

    struct ConstructorBackend {
        state: Arc<ConstructorBackendState>,
        capabilities: Result<BackendCapabilities, BleHubError>,
        bind_result: Result<(), BleHubError>,
    }

    struct ConstructorBackendState {
        token: Mutex<Option<BackendEventIngressToken>>,
        dispose_count: AtomicUsize,
    }

    impl ConstructorBackend {
        fn new(
            capabilities: Result<BackendCapabilities, BleHubError>,
            bind_result: Result<(), BleHubError>,
        ) -> (Self, Arc<ConstructorBackendState>) {
            let state = Arc::new(ConstructorBackendState {
                token: Mutex::new(None),
                dispose_count: AtomicUsize::new(0),
            });
            (
                Self {
                    state: Arc::clone(&state),
                    capabilities,
                    bind_result,
                },
                state,
            )
        }
    }

    impl BleBackend for ConstructorBackend {
        fn capabilities(&self) -> Result<BackendCapabilities, BleHubError> {
            self.capabilities.clone()
        }

        fn bind_event_ingress(&self, token: BackendEventIngressToken) -> Result<(), BleHubError> {
            self.bind_result.clone()?;
            *self.state.token.lock().unwrap() = Some(token);
            Ok(())
        }

        fn start_scan(&self, _: StartScanCommand) -> Result<(), CommandDispatchError> {
            Err(CommandDispatchError::InvalidState {
                detail: "constructor test backend does not dispatch commands".to_owned(),
            })
        }

        fn connect(&self, _: ConnectCommand) -> Result<(), CommandDispatchError> {
            Err(CommandDispatchError::InvalidState {
                detail: "constructor test backend does not dispatch commands".to_owned(),
            })
        }

        fn dispose(&self) -> Result<(), BleHubError> {
            self.state.dispose_count.fetch_add(1, Ordering::AcqRel);
            self.state.token.lock().unwrap().take();
            Ok(())
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

    #[test]
    fn capabilities_failure_rolls_back_all_owned_resources() {
        let registry = Arc::new(BackendEventIngressRegistry::new());
        let (backend, state) = ConstructorBackend::new(
            Err(BleHubError::BackendAssembly {
                detail: "capabilities unavailable".to_owned(),
            }),
            Ok(()),
        );
        let (sink, sink_drop_count) = tracking_sink();

        let result = BleHub::new_with_registry(Box::new(backend), sink, Arc::clone(&registry));

        assert!(matches!(result, Err(BleHubError::BackendAssembly { .. })));
        assert_eq!(state.dispose_count.load(Ordering::Acquire), 1);
        assert_eq!(sink_drop_count.load(Ordering::Acquire), 1);
        assert!(state.token.lock().unwrap().is_none());
        assert_eq!(registry.len(), 0);
    }

    #[test]
    fn bind_failure_rolls_back_without_retaining_the_token() {
        let registry = Arc::new(BackendEventIngressRegistry::new());
        let (backend, state) = ConstructorBackend::new(
            Ok(BackendCapabilities::new(Platform::Test)),
            Err(BleHubError::BackendAssembly {
                detail: "bind rejected".to_owned(),
            }),
        );
        let (sink, sink_drop_count) = tracking_sink();

        let result = BleHub::new_with_registry(Box::new(backend), sink, Arc::clone(&registry));

        assert!(matches!(result, Err(BleHubError::BackendAssembly { .. })));
        assert_eq!(state.dispose_count.load(Ordering::Acquire), 1);
        assert_eq!(sink_drop_count.load(Ordering::Acquire), 1);
        assert!(state.token.lock().unwrap().is_none());
        assert_eq!(registry.len(), 0);
    }

    #[test]
    fn registration_failure_after_successful_bind_rolls_back_all_new_resources() {
        let registry = Arc::new(BackendEventIngressRegistry::new());
        let token = BackendEventIngressToken::new(1);
        let (existing_sink, _) = tracking_sink();
        let existing_state = Arc::new(EventIngressState::new(existing_sink));
        let existing_registration = registry
            .register(token, Arc::downgrade(&existing_state))
            .unwrap();
        registry.set_next_token(token.value);
        let (backend, state) =
            ConstructorBackend::new(Ok(BackendCapabilities::new(Platform::Test)), Ok(()));
        let (sink, sink_drop_count) = tracking_sink();

        let result = BleHub::new_with_registry(Box::new(backend), sink, Arc::clone(&registry));

        assert!(matches!(result, Err(BleHubError::FfiContract { .. })));
        assert_eq!(state.dispose_count.load(Ordering::Acquire), 1);
        assert_eq!(sink_drop_count.load(Ordering::Acquire), 1);
        assert!(state.token.lock().unwrap().is_none());
        assert_eq!(registry.len(), 1);
        assert!(
            registry
                .lookup(token)
                .unwrap()
                .ptr_eq(&Arc::downgrade(&existing_state))
        );
        existing_registration.unregister();
        assert_eq!(registry.len(), 0);
    }

    #[test]
    fn invalid_contract_version_rolls_back_before_binding() {
        let registry = Arc::new(BackendEventIngressRegistry::new());
        let (backend, state) = ConstructorBackend::new(
            Ok(BackendCapabilities {
                platform: Platform::Test,
                contract_version: 3,
            }),
            Ok(()),
        );
        let (sink, sink_drop_count) = tracking_sink();

        let result = BleHub::new_with_registry(Box::new(backend), sink, Arc::clone(&registry));

        assert!(matches!(result, Err(BleHubError::FfiContract { .. })));
        assert_eq!(state.dispose_count.load(Ordering::Acquire), 1);
        assert_eq!(sink_drop_count.load(Ordering::Acquire), 1);
        assert!(state.token.lock().unwrap().is_none());
        assert_eq!(registry.len(), 0);
    }
}
