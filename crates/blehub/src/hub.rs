use std::sync::{Arc, Condvar, Mutex, RwLock};

use crate::{
    AssociateCompanionDeviceCommand, BackendCapabilities, BackendEventIngressHandle, BleBackend,
    BleEventSink, BleHubError, CancelOperationRequest, CommandDispatchError, ConnectCommand,
    CreateBondCommand, DisconnectCommand, DiscoverGattCommand, InitializeCommand, MtuCommand,
    QueryDevicesCommand, ReadCharacteristicCommand, ReadDescriptorCommand, ReadRssiCommand,
    RefreshGattCacheCommand, RemoveBondCommand, SetAdapterEnabledCommand,
    SetConnectionPriorityCommand, SetNotificationsCommand, StartScanCommand, StopScanCommand,
    WriteCharacteristicCommand, WriteDescriptorCommand, ingress::EventIngressState,
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

fn dispose_after_assembly_failure(
    backend: &dyn BleBackend,
    stage: &'static str,
    primary: BleHubError,
) -> BleHubError {
    match backend.dispose() {
        Ok(()) => primary,
        Err(cleanup) => {
            tracing::error!(
                target: "blehub::lifecycle",
                diagnostic = "assembly_rollback_failed",
                stage,
                error_class = "backend_cleanup",
                "backend cleanup failed while rolling back BleHub assembly"
            );
            BleHubError::BackendCleanup {
                detail: format!(
                    "assembly rollback at {stage} failed after `{primary}`: `{cleanup}`"
                ),
                platform_details: None,
            }
        }
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
                return Err(dispose_after_assembly_failure(
                    backend.as_ref(),
                    "capabilities",
                    error,
                ));
            }
        };

        let ingress = Arc::new(EventIngressState::new(downstream));
        let ingress_handle = Arc::new(BackendEventIngressHandle::new(Arc::downgrade(&ingress)));

        // Move the sole long-lived handle root into the backend. BleHub retains
        // only the separate ingress state; callback work may clone temporarily.
        if let Err(error) = backend.bind_event_ingress(ingress_handle) {
            ingress.close();
            return Err(dispose_after_assembly_failure(
                backend.as_ref(),
                "bind_event_ingress",
                error,
            ));
        }
        if let Err(detail) = ingress.activate() {
            ingress.close();
            return Err(dispose_after_assembly_failure(
                backend.as_ref(),
                "activate_ingress",
                BleHubError::FfiContract {
                    detail: detail.to_owned(),
                },
            ));
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

    pub fn set_adapter_enabled(
        &self,
        command: SetAdapterEnabledCommand,
    ) -> Result<(), CommandDispatchError> {
        self.with_backend(|backend| backend.set_adapter_enabled(command))
    }

    pub fn query_devices(&self, command: QueryDevicesCommand) -> Result<(), CommandDispatchError> {
        let command = command.validated()?;
        self.with_backend(|backend| backend.query_devices(command))
    }

    pub fn associate_companion_device(
        &self,
        command: AssociateCompanionDeviceCommand,
    ) -> Result<(), CommandDispatchError> {
        let command = command.validated()?;
        self.with_backend(|backend| backend.associate_companion_device(command))
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
        let command = command.validated()?;
        self.with_backend(|backend| backend.write_characteristic(command))
    }

    pub fn read_descriptor(
        &self,
        command: ReadDescriptorCommand,
    ) -> Result<(), CommandDispatchError> {
        self.with_backend(|backend| backend.read_descriptor(command))
    }

    pub fn write_descriptor(
        &self,
        command: WriteDescriptorCommand,
    ) -> Result<(), CommandDispatchError> {
        let command = command.validated()?;
        self.with_backend(|backend| backend.write_descriptor(command))
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

    pub fn mtu(&self, command: MtuCommand) -> Result<(), CommandDispatchError> {
        let command = command.validated()?;
        self.with_backend(|backend| backend.mtu(command))
    }

    pub fn set_connection_priority(
        &self,
        command: SetConnectionPriorityCommand,
    ) -> Result<(), CommandDispatchError> {
        self.with_backend(|backend| backend.set_connection_priority(command))
    }

    pub fn create_bond(&self, command: CreateBondCommand) -> Result<(), CommandDispatchError> {
        let command = command.validated()?;
        self.with_backend(|backend| backend.create_bond(command))
    }

    pub fn remove_bond(&self, command: RemoveBondCommand) -> Result<(), CommandDispatchError> {
        self.with_backend(|backend| backend.remove_bond(command))
    }

    pub fn refresh_gatt_cache(
        &self,
        command: RefreshGattCacheCommand,
    ) -> Result<(), CommandDispatchError> {
        self.with_backend(|backend| backend.refresh_gatt_cache(command))
    }

    /// Requests cancellation of an existing operation by its original ID.
    /// Success only means that the backend accepted the control request.
    pub fn cancel_operation(
        &self,
        target_command_id: crate::CommandId,
    ) -> Result<(), CommandDispatchError> {
        self.with_backend(|backend| {
            backend.cancel_operation(CancelOperationRequest { target_command_id })
        })
    }

    /// Stops dispatch, drains admitted sink calls, and performs backend cleanup once.
    /// Concurrent callers wait for the same cached result, including cleanup errors.
    ///
    /// Sink/backend Drop or deinit, backend cleanup, and their synchronous callbacks
    /// must not directly or indirectly wait for this same Hub's disposal. Doing so
    /// would wait for the outer shutdown that is itself waiting for them to return.
    /// Releasing internal locks before cleanup does not make recursive close safe.
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
#[path = "tests/hub_tests.rs"]
mod tests;
