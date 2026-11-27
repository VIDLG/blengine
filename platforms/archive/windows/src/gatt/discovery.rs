use super::*;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) struct DiscoveryCorrelation {
    pub(super) connection: ConnectionCorrelation,
    pub(super) generation: u64,
    pub(super) command: DiscoverGattCommand,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum DiscoveryStage {
    Services,
    Characteristics {
        service_index: usize,
    },
    Descriptors {
        service_index: usize,
        characteristic_index: usize,
    },
}

pub(super) struct PendingDiscovery {
    pub(super) correlation: DiscoveryCorrelation,
    pub(super) stage: DiscoveryStage,
    pub(super) operation: PendingOperationState,
    pub(super) completion: Option<DiscoveryCompletion>,
    pub(super) driver_running: bool,
    pub(super) draft: DiscoveryDraft,
}

pub(super) struct DiscoveryDraft {
    services: Vec<DraftService>,
}

impl DiscoveryDraft {
    pub(super) fn new() -> Self {
        Self {
            services: Vec::new(),
        }
    }

    fn finish(self, command: DiscoverGattCommand) -> (GattDatabase, NativeGattMap) {
        let mut public_services = Vec::with_capacity(self.services.len());
        let mut native_services = HashMap::with_capacity(self.services.len());
        let mut native_characteristics = HashMap::new();
        let mut native_descriptors = HashMap::new();

        for service in self.services {
            let mut public_characteristics = Vec::with_capacity(service.characteristics.len());
            for characteristic in service.characteristics {
                let mut public_descriptors = Vec::with_capacity(characteristic.descriptors.len());
                for descriptor in characteristic.descriptors {
                    public_descriptors.push(GattDescriptor {
                        id: descriptor.id,
                        uuid: descriptor.uuid,
                    });
                    native_descriptors.insert(descriptor.id, descriptor.native);
                }
                public_characteristics.push(GattCharacteristic {
                    id: characteristic.id,
                    uuid: characteristic.uuid,
                    properties: characteristic.properties,
                    descriptors: public_descriptors,
                });
                native_characteristics.insert(
                    characteristic.id,
                    RetainedGattCharacteristic {
                        properties: characteristic.properties,
                        native: characteristic.native,
                    },
                );
            }
            public_services.push(GattService {
                id: service.id,
                uuid: service.uuid,
                primary: Some(true),
                characteristics: public_characteristics,
            });
            native_services.insert(service.id, service.native);
        }

        (
            GattDatabase {
                connection_id: command.connection_id,
                gatt_session_id: command.gatt_session_id,
                services: public_services,
            },
            NativeGattMap {
                gatt_session_id: command.gatt_session_id,
                services: native_services,
                characteristics: native_characteristics,
                descriptors: native_descriptors,
            },
        )
    }
}

struct DraftService {
    id: GattServiceId,
    uuid: blehub::BluetoothUuid,
    native: GattDeviceService,
    characteristics: Vec<DraftCharacteristic>,
}

struct DraftCharacteristic {
    id: GattCharacteristicId,
    uuid: blehub::BluetoothUuid,
    properties: blehub::CharacteristicProperties,
    native: WinrtGattCharacteristic,
    descriptors: Vec<DraftDescriptor>,
}

struct DraftDescriptor {
    id: GattDescriptorId,
    uuid: blehub::BluetoothUuid,
    native: NativeGattDescriptor,
}

pub(super) enum PendingOperationState {
    Reserved,
    Running(PendingOperation),
}

impl PendingOperationState {
    pub(super) fn cancel(&self) -> windows::core::Result<()> {
        match self {
            Self::Reserved => Ok(()),
            Self::Running(operation) => operation.cancel(),
        }
    }
}

pub(super) enum PendingOperation {
    Services(IAsyncOperation<GattDeviceServicesResult>),
    Characteristics(IAsyncOperation<GattCharacteristicsResult>),
    Descriptors(IAsyncOperation<GattDescriptorsResult>),
}

pub(super) enum DiscoveryCancellation {
    Queued,
    Active(PendingOperation),
    AlreadyCompleting,
}

pub(super) fn prepare_cancellation(
    gatt: &mut ConnectionGattState,
    command_id: CommandId,
) -> Option<DiscoveryCancellation> {
    let pending = gatt
        .pending
        .as_mut()
        .filter(|pending| pending.correlation.command.command_id == command_id)?;
    if pending.completion.is_some() {
        return Some(DiscoveryCancellation::AlreadyCompleting);
    }
    match &pending.operation {
        PendingOperationState::Reserved => {
            pending.completion = Some(DiscoveryCompletion {
                stage: pending.stage,
                result: cancellation_result(pending.stage),
            });
            Some(DiscoveryCancellation::Queued)
        }
        PendingOperationState::Running(operation) => {
            Some(DiscoveryCancellation::Active(clone_operation(operation)))
        }
    }
}

fn cancellation_result(stage: DiscoveryStage) -> CompletionResult {
    let error = simple_event_error(
        BleErrorCategory::Cancelled,
        "Windows GATT discovery cancelled before native start",
    );
    match stage {
        DiscoveryStage::Services => CompletionResult::Services(Err(error)),
        DiscoveryStage::Characteristics { .. } => CompletionResult::Characteristics(Err(error)),
        DiscoveryStage::Descriptors { .. } => CompletionResult::Descriptors(Err(error)),
    }
}

impl PendingOperation {
    pub(super) fn cancel(&self) -> windows::core::Result<()> {
        match self {
            Self::Services(operation) => operation.Cancel(),
            Self::Characteristics(operation) => operation.Cancel(),
            Self::Descriptors(operation) => operation.Cancel(),
        }
    }
}

pub(super) struct DiscoveryCompletion {
    stage: DiscoveryStage,
    result: CompletionResult,
}

enum CompletionResult {
    Services(Result<GattDeviceServicesResult, BleEventError>),
    Characteristics(Result<GattCharacteristicsResult, BleEventError>),
    Descriptors(Result<GattDescriptorsResult, BleEventError>),
}

struct ServiceSeed {
    uuid: blehub::BluetoothUuid,
    native: GattDeviceService,
}

struct CharacteristicSeed {
    uuid: blehub::BluetoothUuid,
    properties: blehub::CharacteristicProperties,
    native: WinrtGattCharacteristic,
}

struct DescriptorSeed {
    uuid: blehub::BluetoothUuid,
    native: NativeGattDescriptor,
}

enum ProcessedCompletion {
    Services(Vec<ServiceSeed>),
    Characteristics {
        service_index: usize,
        characteristics: Vec<CharacteristicSeed>,
    },
    Descriptors {
        service_index: usize,
        characteristic_index: usize,
        descriptors: Vec<DescriptorSeed>,
    },
}

enum NextOperation {
    Characteristics {
        stage: DiscoveryStage,
        service: GattDeviceService,
    },
    Descriptors {
        stage: DiscoveryStage,
        characteristic: WinrtGattCharacteristic,
    },
}

impl NextOperation {
    fn stage(&self) -> DiscoveryStage {
        match self {
            Self::Characteristics { stage, .. } | Self::Descriptors { stage, .. } => *stage,
        }
    }

    fn start(&self) -> windows::core::Result<PendingOperation> {
        match self {
            Self::Characteristics { service, .. } => service
                .GetCharacteristicsWithCacheModeAsync(BluetoothCacheMode::Uncached)
                .map(PendingOperation::Characteristics),
            Self::Descriptors { characteristic, .. } => characteristic
                .GetDescriptorsWithCacheModeAsync(BluetoothCacheMode::Uncached)
                .map(PendingOperation::Descriptors),
        }
    }
}

enum DriverAction {
    Continue(NextOperation),
    Complete,
    Fail {
        error: BleEventError,
        orphan_services: Vec<GattDeviceService>,
    },
    Gone {
        orphan_services: Vec<GattDeviceService>,
    },
}

struct DiscoveryReservation {
    correlation: DiscoveryCorrelation,
    device: BluetoothLEDevice,
}

pub(super) fn begin_discovery(
    backend: &WindowsBackend,
    command: DiscoverGattCommand,
) -> Result<(), CommandDispatchError> {
    let delivery_guard = lock_delivery(&backend.delivery);
    let (reservation, operation) = reserve_then_start(
        || reserve_discovery(backend, command),
        |reservation| {
            reservation
                .device
                .GetGattServicesWithCacheModeAsync(BluetoothCacheMode::Uncached)
        },
    )?;
    let correlation = reservation.correlation;
    let operation = match operation {
        Ok(operation) => operation,
        Err(error) => {
            drop(delivery_guard);
            fail_discovery(
                &backend.state,
                correlation,
                event_error(
                    BleErrorCategory::Gatt,
                    "start uncached GATT service discovery",
                    error,
                ),
            );
            drain_deferred_services_changed(&backend.state, correlation.connection);
            return Ok(());
        }
    };

    let pending_operation = PendingOperation::Services(operation);
    if !store_pending_operation(
        &backend.state,
        correlation,
        DiscoveryStage::Services,
        &pending_operation,
    ) {
        drop(delivery_guard);
        log_cleanup_failure(
            pending_operation.cancel(),
            "cancel retired GATT service discovery",
        );
        drain_deferred_services_changed(&backend.state, correlation.connection);
        return Ok(());
    }
    let completion_registration = install_completion_handler(
        &backend.state,
        correlation,
        DiscoveryStage::Services,
        &pending_operation,
    );
    drop(delivery_guard);
    if let Err(error) = completion_registration {
        fail_discovery(
            &backend.state,
            correlation,
            event_error(
                BleErrorCategory::Gatt,
                "register uncached GATT service discovery completion",
                error,
            ),
        );
    }
    drain_deferred_services_changed(&backend.state, correlation.connection);
    run_discovery_driver(&backend.state, correlation);
    Ok(())
}

fn reserve_discovery(
    backend: &WindowsBackend,
    command: DiscoverGattCommand,
) -> Result<DiscoveryReservation, CommandDispatchError> {
    let reservation = {
        let mut state = lock_state(&backend.state);
        let (connection, device) = validate_discovery(&state, command)?;
        let generation = state
            .gatt_discovery_generations
            .allocate_one()
            .ok_or_else(|| CommandDispatchError::BackendUnavailable {
                detail: "Windows GATT discovery generation space is exhausted".to_owned(),
                platform_details: None,
            })?;
        let correlation = DiscoveryCorrelation {
            connection,
            generation,
            command,
        };
        let Some(gatt) = connected_gatt_mut(&mut state, connection) else {
            return Err(CommandDispatchError::InvalidState {
                detail: "connection is no longer available for GATT discovery".to_owned(),
            });
        };
        gatt.pending = Some(PendingDiscovery {
            correlation,
            stage: DiscoveryStage::Services,
            operation: PendingOperationState::Reserved,
            completion: None,
            driver_running: true,
            draft: DiscoveryDraft::new(),
        });
        DiscoveryReservation {
            correlation,
            device,
        }
    };
    Ok(reservation)
}

fn store_pending_operation(
    state: &std::sync::Arc<std::sync::Mutex<BackendState>>,
    correlation: DiscoveryCorrelation,
    stage: DiscoveryStage,
    operation: &PendingOperation,
) -> bool {
    let mut current = lock_state(state);
    let Some(gatt) = connected_gatt_mut(&mut current, correlation.connection) else {
        return false;
    };
    let Some(pending) = gatt.pending.as_mut() else {
        return false;
    };
    if pending.correlation != correlation
        || pending.stage != stage
        || !pending.driver_running
        || !matches!(pending.operation, PendingOperationState::Reserved)
    {
        return false;
    }
    pending.operation = PendingOperationState::Running(clone_operation(operation));
    true
}

fn validate_discovery(
    state: &BackendState,
    command: DiscoverGattCommand,
) -> Result<(ConnectionCorrelation, BluetoothLEDevice), CommandDispatchError> {
    ensure_ready(state)?;
    let generation = state
        .active_connections
        .get(&command.connection_id)
        .copied()
        .ok_or_else(|| CommandDispatchError::InvalidState {
            detail: "connection lifecycle is not active".to_owned(),
        })?;
    let connection = state.connections.get(&generation).ok_or_else(|| {
        CommandDispatchError::BackendUnavailable {
            detail: "Windows connection ownership is transitioning".to_owned(),
            platform_details: None,
        }
    })?;
    if connection.correlation.connection_id != command.connection_id {
        return Err(CommandDispatchError::InvalidState {
            detail: "connection correlation no longer matches the discovery request".to_owned(),
        });
    }
    let ConnectionPhase::Connected { device, gatt, .. } = &connection.phase else {
        return Err(CommandDispatchError::InvalidState {
            detail: "connection is not ready for GATT discovery".to_owned(),
        });
    };
    ensure_gatt_cleanup_idle(gatt)?;
    if gatt.pending.is_some() {
        return Err(CommandDispatchError::InvalidState {
            detail: "a GATT discovery is already pending for this connection".to_owned(),
        });
    }
    if !gatt.io_queue.is_empty() {
        return Err(CommandDispatchError::InvalidState {
            detail: "GATT I/O is active or queued for this connection".to_owned(),
        });
    }
    if gatt_session_id_is_active_or_pending(gatt, command.gatt_session_id) {
        return Err(CommandDispatchError::InvalidRequest {
            detail: "the GATT session ID is currently active or pending for this connection"
                .to_owned(),
        });
    }
    Ok((connection.correlation, device.clone()))
}

pub(super) fn gatt_session_id_is_active_or_pending(
    gatt: &ConnectionGattState,
    gatt_session_id: GattSessionId,
) -> bool {
    gatt.active
        .as_ref()
        .is_some_and(|active| active.gatt_session_id == gatt_session_id)
        || gatt
            .pending
            .as_ref()
            .is_some_and(|pending| pending.correlation.command.gatt_session_id == gatt_session_id)
}

fn pending_matches(state: &BackendState, correlation: DiscoveryCorrelation) -> bool {
    connected_gatt(state, correlation.connection)
        .and_then(|gatt| gatt.pending.as_ref())
        .is_some_and(|pending| pending.correlation == correlation)
}

fn operation_is_current(
    state: &std::sync::Arc<std::sync::Mutex<BackendState>>,
    correlation: DiscoveryCorrelation,
    stage: DiscoveryStage,
) -> bool {
    let state = lock_state(state);
    connected_gatt(&state, correlation.connection)
        .and_then(|gatt| gatt.pending.as_ref())
        .is_some_and(|pending| {
            pending.correlation == correlation
                && pending.stage == stage
                && matches!(pending.operation, PendingOperationState::Running(_))
        })
}

fn install_completion_handler(
    state: &std::sync::Arc<std::sync::Mutex<BackendState>>,
    correlation: DiscoveryCorrelation,
    stage: DiscoveryStage,
    operation: &PendingOperation,
) -> windows::core::Result<()> {
    match operation {
        PendingOperation::Services(operation) => {
            let weak_state = std::sync::Arc::downgrade(state);
            let callback_resource = track_callback(state, CallbackResource::ServiceDiscovery);
            let handler = AsyncOperationCompletedHandler::new(move |operation, status| {
                let _callback_resource = &callback_resource;
                if let Some(state) = weak_state.upgrade()
                    && operation_is_current(&state, correlation, stage)
                {
                    let result = completion_result(
                        operation.as_ref(),
                        status,
                        "uncached GATT service discovery",
                        |operation| operation.GetResults(),
                    );
                    submit_completion(
                        &state,
                        correlation,
                        DiscoveryCompletion {
                            stage,
                            result: CompletionResult::Services(result),
                        },
                    );
                }
                Ok(())
            });
            operation.SetCompleted(&handler)
        }
        PendingOperation::Characteristics(operation) => {
            let weak_state = std::sync::Arc::downgrade(state);
            let callback_resource =
                track_callback(state, CallbackResource::CharacteristicDiscovery);
            let handler = AsyncOperationCompletedHandler::new(move |operation, status| {
                let _callback_resource = &callback_resource;
                if let Some(state) = weak_state.upgrade()
                    && operation_is_current(&state, correlation, stage)
                {
                    let result = completion_result(
                        operation.as_ref(),
                        status,
                        "uncached GATT characteristic discovery",
                        |operation| operation.GetResults(),
                    );
                    submit_completion(
                        &state,
                        correlation,
                        DiscoveryCompletion {
                            stage,
                            result: CompletionResult::Characteristics(result),
                        },
                    );
                }
                Ok(())
            });
            operation.SetCompleted(&handler)
        }
        PendingOperation::Descriptors(operation) => {
            let weak_state = std::sync::Arc::downgrade(state);
            let callback_resource = track_callback(state, CallbackResource::DescriptorDiscovery);
            let handler = AsyncOperationCompletedHandler::new(move |operation, status| {
                let _callback_resource = &callback_resource;
                if let Some(state) = weak_state.upgrade()
                    && operation_is_current(&state, correlation, stage)
                {
                    let result = completion_result(
                        operation.as_ref(),
                        status,
                        "uncached GATT descriptor discovery",
                        |operation| operation.GetResults(),
                    );
                    submit_completion(
                        &state,
                        correlation,
                        DiscoveryCompletion {
                            stage,
                            result: CompletionResult::Descriptors(result),
                        },
                    );
                }
                Ok(())
            });
            operation.SetCompleted(&handler)
        }
    }
}

fn completion_result<T, O>(
    operation: Option<&IAsyncOperation<T>>,
    status: AsyncStatus,
    operation_name: &'static str,
    get_results: impl FnOnce(&IAsyncOperation<T>) -> windows::core::Result<O>,
) -> Result<O, BleEventError>
where
    T: windows::core::RuntimeType,
{
    if status != AsyncStatus::Completed {
        let category = if status == AsyncStatus::Canceled {
            BleErrorCategory::Cancelled
        } else {
            BleErrorCategory::Gatt
        };
        return Err(simple_event_error(
            category,
            format!("{operation_name} ended with {status:?}"),
        ));
    }
    let Some(operation) = operation else {
        return Err(simple_event_error(
            BleErrorCategory::Internal,
            format!("{operation_name} completion omitted the operation"),
        ));
    };
    get_results(operation).map_err(|error| {
        event_error(
            BleErrorCategory::Gatt,
            "complete uncached Windows GATT discovery operation",
            error,
        )
    })
}

fn submit_completion(
    state: &std::sync::Arc<std::sync::Mutex<BackendState>>,
    correlation: DiscoveryCorrelation,
    completion: DiscoveryCompletion,
) {
    let should_run = {
        let mut current = lock_state(state);
        let Some(gatt) = connected_gatt_mut(&mut current, correlation.connection) else {
            return;
        };
        let Some(pending) = gatt.pending.as_mut() else {
            return;
        };
        if pending.correlation != correlation
            || pending.stage != completion.stage
            || pending.completion.is_some()
        {
            return;
        }
        pending.completion = Some(completion);
        if pending.driver_running {
            false
        } else {
            pending.driver_running = true;
            true
        }
    };
    if should_run {
        run_discovery_driver(state, correlation);
    }
}

fn run_discovery_driver(
    state: &std::sync::Arc<std::sync::Mutex<BackendState>>,
    correlation: DiscoveryCorrelation,
) {
    loop {
        let completion = {
            let mut current = lock_state(state);
            let Some(gatt) = connected_gatt_mut(&mut current, correlation.connection) else {
                return;
            };
            let Some(pending) = gatt.pending.as_mut() else {
                return;
            };
            if pending.correlation != correlation || !pending.driver_running {
                return;
            }
            let Some(completion) = pending.completion.take() else {
                pending.driver_running = false;
                return;
            };
            completion
        };

        let processed = match process_completion(completion) {
            Ok(processed) => processed,
            Err(error) => {
                fail_discovery(state, correlation, error);
                return;
            }
        };
        let delivery = delivery_for_state(state);
        let delivery_guard = lock_delivery(&delivery);
        match apply_processed_completion(state, correlation, processed) {
            DriverAction::Continue(next) => {
                let stage = next.stage();
                let operation = match next.start() {
                    Ok(operation) => operation,
                    Err(error) => {
                        drop(delivery_guard);
                        fail_discovery(
                            state,
                            correlation,
                            event_error(
                                BleErrorCategory::Gatt,
                                match stage {
                                    DiscoveryStage::Characteristics { .. } => {
                                        "start uncached GATT characteristic discovery"
                                    }
                                    DiscoveryStage::Descriptors { .. } => {
                                        "start uncached GATT descriptor discovery"
                                    }
                                    DiscoveryStage::Services => "start uncached GATT discovery",
                                },
                                error,
                            ),
                        );
                        drain_deferred_services_changed(state, correlation.connection);
                        return;
                    }
                };
                let stored = store_pending_operation(state, correlation, stage, &operation);
                if !stored {
                    drop(delivery_guard);
                    log_cleanup_failure(
                        operation.cancel(),
                        "cancel stale GATT discovery continuation",
                    );
                    drain_deferred_services_changed(state, correlation.connection);
                    return;
                }
                let completion_registration =
                    install_completion_handler(state, correlation, stage, &operation);
                drop(delivery_guard);
                if let Err(error) = completion_registration {
                    fail_discovery(
                        state,
                        correlation,
                        event_error(
                            BleErrorCategory::Gatt,
                            match stage {
                                DiscoveryStage::Characteristics { .. } => {
                                    "register uncached GATT characteristic discovery completion"
                                }
                                DiscoveryStage::Descriptors { .. } => {
                                    "register uncached GATT descriptor discovery completion"
                                }
                                DiscoveryStage::Services => {
                                    "register uncached GATT discovery completion"
                                }
                            },
                            error,
                        ),
                    );
                }
                drain_deferred_services_changed(state, correlation.connection);
            }
            DriverAction::Complete => {
                drop(delivery_guard);
                complete_discovery(state, correlation);
                return;
            }
            DriverAction::Fail {
                error,
                orphan_services,
            } => {
                drop(delivery_guard);
                close_service_objects(orphan_services, None);
                fail_discovery(state, correlation, error);
                return;
            }
            DriverAction::Gone { orphan_services } => {
                drop(delivery_guard);
                close_service_objects(orphan_services, None);
                return;
            }
        }
    }
}

fn clone_operation(operation: &PendingOperation) -> PendingOperation {
    match operation {
        PendingOperation::Services(operation) => PendingOperation::Services(operation.clone()),
        PendingOperation::Characteristics(operation) => {
            PendingOperation::Characteristics(operation.clone())
        }
        PendingOperation::Descriptors(operation) => {
            PendingOperation::Descriptors(operation.clone())
        }
    }
}

fn process_completion(
    completion: DiscoveryCompletion,
) -> Result<ProcessedCompletion, BleEventError> {
    match (completion.stage, completion.result) {
        (DiscoveryStage::Services, CompletionResult::Services(result)) => {
            collect_services(result?).map(ProcessedCompletion::Services)
        }
        (
            DiscoveryStage::Characteristics { service_index },
            CompletionResult::Characteristics(result),
        ) => collect_characteristics(result?).map(|characteristics| {
            ProcessedCompletion::Characteristics {
                service_index,
                characteristics,
            }
        }),
        (
            DiscoveryStage::Descriptors {
                service_index,
                characteristic_index,
            },
            CompletionResult::Descriptors(result),
        ) => collect_descriptors(result?).map(|descriptors| ProcessedCompletion::Descriptors {
            service_index,
            characteristic_index,
            descriptors,
        }),
        _ => Err(simple_event_error(
            BleErrorCategory::Internal,
            "GATT discovery completion did not match its registered stage",
        )),
    }
}

fn collect_services(result: GattDeviceServicesResult) -> Result<Vec<ServiceSeed>, BleEventError> {
    ensure_communication_success(
        result.Status().map_err(|error| {
            event_error(
                BleErrorCategory::Gatt,
                "read uncached GATT service discovery status",
                error,
            )
        })?,
        "uncached GATT service discovery",
    )?;
    let services = result.Services().map_err(|error| {
        event_error(
            BleErrorCategory::Gatt,
            "read uncached GATT service discovery results",
            error,
        )
    })?;
    let count = services.Size().map_err(|error| {
        event_error(
            BleErrorCategory::Gatt,
            "read uncached GATT service result count",
            error,
        )
    })?;
    let mut native_services = Vec::with_capacity(count as usize);
    for index in 0..count {
        match services.GetAt(index) {
            Ok(service) => native_services.push(service),
            Err(error) => {
                close_service_objects(native_services, None);
                return Err(event_error(
                    BleErrorCategory::Gatt,
                    "read uncached GATT service result",
                    error,
                ));
            }
        }
    }
    let mut uuids = Vec::with_capacity(native_services.len());
    for service in &native_services {
        match service.Uuid() {
            Ok(uuid) => uuids.push(map_guid(uuid)),
            Err(error) => {
                close_service_objects(native_services, None);
                return Err(event_error(
                    BleErrorCategory::Gatt,
                    "read GATT service UUID",
                    error,
                ));
            }
        }
    }
    Ok(uuids
        .into_iter()
        .zip(native_services)
        .map(|(uuid, native)| ServiceSeed { uuid, native })
        .collect())
}

fn collect_characteristics(
    result: GattCharacteristicsResult,
) -> Result<Vec<CharacteristicSeed>, BleEventError> {
    ensure_communication_success(
        result.Status().map_err(|error| {
            event_error(
                BleErrorCategory::Gatt,
                "read uncached GATT characteristic discovery status",
                error,
            )
        })?,
        "uncached GATT characteristic discovery",
    )?;
    let characteristics = result.Characteristics().map_err(|error| {
        event_error(
            BleErrorCategory::Gatt,
            "read uncached GATT characteristic discovery results",
            error,
        )
    })?;
    let count = characteristics.Size().map_err(|error| {
        event_error(
            BleErrorCategory::Gatt,
            "read uncached GATT characteristic result count",
            error,
        )
    })?;
    let mut seeds = Vec::with_capacity(count as usize);
    for index in 0..count {
        let characteristic = characteristics.GetAt(index).map_err(|error| {
            event_error(
                BleErrorCategory::Gatt,
                "read uncached GATT characteristic result",
                error,
            )
        })?;
        let uuid = characteristic.Uuid().map(map_guid).map_err(|error| {
            event_error(
                BleErrorCategory::Gatt,
                "read GATT characteristic UUID",
                error,
            )
        })?;
        let properties = characteristic
            .CharacteristicProperties()
            .map(map_characteristic_properties)
            .map_err(|error| {
                event_error(
                    BleErrorCategory::Gatt,
                    "read GATT characteristic properties",
                    error,
                )
            })?;
        seeds.push(CharacteristicSeed {
            uuid,
            properties,
            native: characteristic,
        });
    }
    Ok(seeds)
}

fn collect_descriptors(
    result: GattDescriptorsResult,
) -> Result<Vec<DescriptorSeed>, BleEventError> {
    ensure_communication_success(
        result.Status().map_err(|error| {
            event_error(
                BleErrorCategory::Gatt,
                "read uncached GATT descriptor discovery status",
                error,
            )
        })?,
        "uncached GATT descriptor discovery",
    )?;
    let descriptors = result.Descriptors().map_err(|error| {
        event_error(
            BleErrorCategory::Gatt,
            "read uncached GATT descriptor discovery results",
            error,
        )
    })?;
    let count = descriptors.Size().map_err(|error| {
        event_error(
            BleErrorCategory::Gatt,
            "read uncached GATT descriptor result count",
            error,
        )
    })?;
    let mut seeds = Vec::with_capacity(count as usize);
    for index in 0..count {
        let descriptor = descriptors.GetAt(index).map_err(|error| {
            event_error(
                BleErrorCategory::Gatt,
                "read uncached GATT descriptor result",
                error,
            )
        })?;
        let uuid = descriptor.Uuid().map(map_guid).map_err(|error| {
            event_error(BleErrorCategory::Gatt, "read GATT descriptor UUID", error)
        })?;
        seeds.push(DescriptorSeed {
            uuid,
            native: descriptor,
        });
    }
    Ok(seeds)
}

fn ensure_communication_success(
    status: GattCommunicationStatus,
    operation: &'static str,
) -> Result<(), BleEventError> {
    ensure_communication_success_with_protocol(status, None, operation)
}

fn apply_processed_completion(
    state: &std::sync::Arc<std::sync::Mutex<BackendState>>,
    correlation: DiscoveryCorrelation,
    processed: ProcessedCompletion,
) -> DriverAction {
    let orphan_services = || match &processed {
        ProcessedCompletion::Services(services) => services
            .iter()
            .map(|service| service.native.clone())
            .collect(),
        _ => Vec::new(),
    };
    let mut current = lock_state(state);
    if !pending_matches(&current, correlation) {
        return DriverAction::Gone {
            orphan_services: orphan_services(),
        };
    }

    let count = match &processed {
        ProcessedCompletion::Services(services) => services.len(),
        ProcessedCompletion::Characteristics {
            characteristics, ..
        } => characteristics.len(),
        ProcessedCompletion::Descriptors { descriptors, .. } => descriptors.len(),
    };
    let Some(ids) = current.gatt_attribute_ids.allocate_many(count) else {
        return DriverAction::Fail {
            error: simple_event_error(
                BleErrorCategory::Internal,
                "Windows GATT attribute ID space is exhausted",
            ),
            orphan_services: orphan_services(),
        };
    };

    let Some(gatt) = connected_gatt_mut(&mut current, correlation.connection) else {
        return DriverAction::Gone {
            orphan_services: orphan_services(),
        };
    };
    let Some(pending) = gatt.pending.as_mut() else {
        return DriverAction::Gone {
            orphan_services: orphan_services(),
        };
    };
    if pending.correlation != correlation {
        return DriverAction::Gone {
            orphan_services: orphan_services(),
        };
    }

    match processed {
        ProcessedCompletion::Services(services) => {
            for (value, service) in ids.zip(services) {
                pending.draft.services.push(DraftService {
                    id: GattServiceId { value },
                    uuid: service.uuid,
                    native: service.native,
                    characteristics: Vec::new(),
                });
            }
        }
        ProcessedCompletion::Characteristics {
            service_index,
            characteristics,
        } => {
            let Some(service) = pending.draft.services.get_mut(service_index) else {
                return DriverAction::Fail {
                    error: simple_event_error(
                        BleErrorCategory::Internal,
                        "GATT characteristic result referenced an unknown service",
                    ),
                    orphan_services: Vec::new(),
                };
            };
            for (value, characteristic) in ids.zip(characteristics) {
                service.characteristics.push(DraftCharacteristic {
                    id: GattCharacteristicId { value },
                    uuid: characteristic.uuid,
                    properties: characteristic.properties,
                    native: characteristic.native,
                    descriptors: Vec::new(),
                });
            }
        }
        ProcessedCompletion::Descriptors {
            service_index,
            characteristic_index,
            descriptors,
        } => {
            let Some(characteristic) = pending
                .draft
                .services
                .get_mut(service_index)
                .and_then(|service| service.characteristics.get_mut(characteristic_index))
            else {
                return DriverAction::Fail {
                    error: simple_event_error(
                        BleErrorCategory::Internal,
                        "GATT descriptor result referenced an unknown characteristic",
                    ),
                    orphan_services: Vec::new(),
                };
            };
            for (value, descriptor) in ids.zip(descriptors) {
                characteristic.descriptors.push(DraftDescriptor {
                    id: GattDescriptorId { value },
                    uuid: descriptor.uuid,
                    native: descriptor.native,
                });
            }
        }
    }

    match next_operation(&pending.draft, pending.stage) {
        Some(next) => {
            pending.stage = next.stage();
            pending.operation = PendingOperationState::Reserved;
            DriverAction::Continue(next)
        }
        None => DriverAction::Complete,
    }
}

fn next_operation(
    draft: &DiscoveryDraft,
    completed_stage: DiscoveryStage,
) -> Option<NextOperation> {
    match completed_stage {
        DiscoveryStage::Services => {
            draft
                .services
                .first()
                .map(|service| NextOperation::Characteristics {
                    stage: DiscoveryStage::Characteristics { service_index: 0 },
                    service: service.native.clone(),
                })
        }
        DiscoveryStage::Characteristics { service_index } => {
            let service = draft.services.get(service_index)?;
            if let Some(characteristic) = service.characteristics.first() {
                Some(NextOperation::Descriptors {
                    stage: DiscoveryStage::Descriptors {
                        service_index,
                        characteristic_index: 0,
                    },
                    characteristic: characteristic.native.clone(),
                })
            } else {
                draft.services.get(service_index + 1).map(|service| {
                    NextOperation::Characteristics {
                        stage: DiscoveryStage::Characteristics {
                            service_index: service_index + 1,
                        },
                        service: service.native.clone(),
                    }
                })
            }
        }
        DiscoveryStage::Descriptors {
            service_index,
            characteristic_index,
        } => {
            let service = draft.services.get(service_index)?;
            if let Some(characteristic) = service.characteristics.get(characteristic_index + 1) {
                Some(NextOperation::Descriptors {
                    stage: DiscoveryStage::Descriptors {
                        service_index,
                        characteristic_index: characteristic_index + 1,
                    },
                    characteristic: characteristic.native.clone(),
                })
            } else {
                draft.services.get(service_index + 1).map(|service| {
                    NextOperation::Characteristics {
                        stage: DiscoveryStage::Characteristics {
                            service_index: service_index + 1,
                        },
                        service: service.native.clone(),
                    }
                })
            }
        }
    }
}

fn complete_discovery(
    state: &std::sync::Arc<std::sync::Mutex<BackendState>>,
    correlation: DiscoveryCorrelation,
) {
    let delivery = delivery_for_state(state);
    let _delivery_guard = lock_delivery(&delivery);
    let (old_active, old_subscriptions, ingress, database) = {
        let mut current = lock_state(state);
        let Some(gatt) = connected_gatt_mut(&mut current, correlation.connection) else {
            return;
        };
        let Some(pending) = gatt.pending.take() else {
            return;
        };
        if pending.correlation != correlation {
            gatt.pending = Some(pending);
            return;
        }
        let PendingDiscovery {
            operation,
            completion,
            draft,
            ..
        } = pending;
        let (database, active) = draft.finish(correlation.command);
        let old_subscriptions = take_subscriptions(gatt);
        let old_active = gatt.active.replace(active);
        gatt.cleanup_in_progress = true;
        drop(completion);
        drop(operation);
        (old_active, old_subscriptions, current.ingress(), database)
    };
    drop(_delivery_guard);
    cleanup_subscription_routes(old_subscriptions, None);
    if let Some(old_active) = old_active {
        cleanup_native_map(old_active, None);
    }
    let _delivery_guard = lock_delivery(&delivery);
    if let Some(ingress) = ingress {
        push_events(
            &ingress,
            discovery_success_events(correlation.command, database),
            "complete Windows GATT discovery",
        );
    }
    let deferred = finish_cleanup_suppression(state, correlation.connection);
    drop(_delivery_guard);
    drain_deferred_cleanup(state, correlation.connection, deferred);
}

fn fail_discovery(
    state: &std::sync::Arc<std::sync::Mutex<BackendState>>,
    correlation: DiscoveryCorrelation,
    error: BleEventError,
) {
    let delivery = delivery_for_state(state);
    let _delivery_guard = lock_delivery(&delivery);
    let (pending, ingress) = {
        let mut current = lock_state(state);
        let Some(gatt) = connected_gatt_mut(&mut current, correlation.connection) else {
            return;
        };
        let Some(pending) = gatt.pending.take() else {
            return;
        };
        if pending.correlation != correlation {
            gatt.pending = Some(pending);
            return;
        }
        gatt.cleanup_in_progress = true;
        (pending, current.ingress())
    };
    drop(_delivery_guard);
    cleanup_pending(pending, None);
    let _delivery_guard = lock_delivery(&delivery);
    if let Some(ingress) = ingress {
        push_events(
            &ingress,
            vec![discovery_failure_event(correlation.command, error)],
            "fail Windows GATT discovery",
        );
    }
    let deferred = finish_cleanup_suppression(state, correlation.connection);
    drop(_delivery_guard);
    drain_deferred_cleanup(state, correlation.connection, deferred);
}

pub(super) fn discovery_success_events(
    command: DiscoverGattCommand,
    database: GattDatabase,
) -> Vec<BleEvent> {
    vec![
        BleEvent::GattDiscovered {
            command_id: command.command_id,
            database,
        },
        BleEvent::CommandCompleted {
            command_id: command.command_id,
            error: None,
        },
    ]
}

pub(super) fn discovery_failure_event(
    command: DiscoverGattCommand,
    error: BleEventError,
) -> BleEvent {
    BleEvent::CommandCompleted {
        command_id: command.command_id,
        error: Some(error),
    }
}

pub(super) fn cleanup_pending(
    pending: PendingDiscovery,
    mut first_error: Option<&mut Option<blehub::BleHubError>>,
) {
    record_connection_cleanup(
        pending.operation.cancel(),
        "cancel pending GATT discovery",
        &mut first_error,
    );
    cleanup_draft(pending.draft, first_error);
}

pub(super) fn cleanup_draft(
    draft: DiscoveryDraft,
    first_error: Option<&mut Option<blehub::BleHubError>>,
) {
    let mut services = Vec::with_capacity(draft.services.len());
    let mut characteristics = Vec::new();
    let mut descriptors = Vec::new();
    for service in draft.services {
        services.push(service.native);
        for characteristic in service.characteristics {
            characteristics.push(characteristic.native);
            descriptors.extend(
                characteristic
                    .descriptors
                    .into_iter()
                    .map(|descriptor| descriptor.native),
            );
        }
    }
    drop(descriptors);
    drop(characteristics);
    close_service_objects(services, first_error);
}
