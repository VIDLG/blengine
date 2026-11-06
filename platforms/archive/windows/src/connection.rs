use std::sync::{Arc, Mutex};

use blehub::{
    BleErrorCategory, BleEvent, BleEventError, BleHubError, CommandDispatchError, CommandId,
    ConnectCommand, ConnectionId, ConnectionState, ConnectionStateResult, DisconnectCommand,
    PeripheralId, Platform, PlatformErrorDetails, ReadRssiCommand,
};
use windows::{
    Devices::Bluetooth::{
        BluetoothAddressType, BluetoothCacheMode, BluetoothConnectionStatus, BluetoothLEDevice,
        BluetoothLEPreferredConnectionParameters,
        GenericAttributeProfile::{GattCommunicationStatus, GattDeviceServicesResult},
    },
    Foundation::TypedEventHandler,
    core::{IInspectable, Interface},
};
use windows_future::{AsyncOperationCompletedHandler, AsyncStatus, IAsyncOperation};

use crate::{
    backend::{
        BackendState, Lifecycle, WindowsBackend, delivery_for_state, ensure_ready, lock_delivery,
        lock_state, log_cleanup_failure, push_events, record_connection_cleanup, track_callback,
    },
    error::{dispatch_error, event_error, simple_event_error},
    gatt,
    resource_accounting::CallbackResource,
};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) struct ConnectionCorrelation {
    pub(crate) generation: u64,
    pub(crate) connect_command_id: CommandId,
    pub(crate) connection_id: ConnectionId,
    pub(crate) peripheral_id: PeripheralId,
}

enum PendingConnectOperation {
    Device(IAsyncOperation<BluetoothLEDevice>),
    GattProbe(IAsyncOperation<GattDeviceServicesResult>),
}

impl PendingConnectOperation {
    fn cancel(&self) -> windows::core::Result<()> {
        match self {
            Self::Device(operation) => operation.Cancel(),
            Self::GattProbe(operation) => operation.Cancel(),
        }
    }
}

pub(crate) enum ConnectionPhase {
    CreatingDevice {
        operation: IAsyncOperation<BluetoothLEDevice>,
    },
    ProbingGatt {
        device: BluetoothLEDevice,
        connection_token: i64,
        gatt_services_token: i64,
        operation: IAsyncOperation<GattDeviceServicesResult>,
    },
    Connected {
        device: BluetoothLEDevice,
        connection_token: i64,
        gatt_services_token: Option<i64>,
        gatt: Box<gatt::ConnectionGattState>,
    },
}

pub(crate) struct ConnectionContext {
    pub(crate) correlation: ConnectionCorrelation,
    pub(crate) phase: ConnectionPhase,
}

enum CancellationAction {
    Connect(PendingConnectOperation),
    Gatt {
        connection: ConnectionCorrelation,
        operation: gatt::IoCancellation,
    },
    Discovery {
        connection: ConnectionCorrelation,
        operation: gatt::DiscoveryCancellation,
    },
}

pub(crate) fn cancel_operation(
    backend: &WindowsBackend,
    target_command_id: CommandId,
) -> Result<(), CommandDispatchError> {
    let action = {
        let mut state = lock_state(&backend.state);
        let mut found = None;
        for context in state.connections.values_mut() {
            let correlation = context.correlation;
            if correlation.connect_command_id == target_command_id {
                found = match &context.phase {
                    ConnectionPhase::CreatingDevice { operation } => {
                        Some(CancellationAction::Connect(
                            PendingConnectOperation::Device(operation.clone()),
                        ))
                    }
                    ConnectionPhase::ProbingGatt { operation, .. } => {
                        Some(CancellationAction::Connect(
                            PendingConnectOperation::GattProbe(operation.clone()),
                        ))
                    }
                    ConnectionPhase::Connected { .. } => None,
                };
                if found.is_some() {
                    break;
                }
            }
            if let ConnectionPhase::Connected { gatt, .. } = &mut context.phase {
                if let Some(operation) = gatt::prepare_io_cancellation(gatt, target_command_id) {
                    found = Some(CancellationAction::Gatt {
                        connection: correlation,
                        operation,
                    });
                    break;
                }
                if let Some(operation) =
                    gatt::prepare_discovery_cancellation(gatt, target_command_id)
                {
                    found = Some(CancellationAction::Discovery {
                        connection: correlation,
                        operation,
                    });
                    break;
                }
            }
        }
        found.ok_or(CommandDispatchError::OperationNotFound { target_command_id })?
    };

    match action {
        CancellationAction::Connect(operation) => operation
            .cancel()
            .map_err(|error| dispatch_error("cancel Windows connection operation", error)),
        CancellationAction::Gatt {
            connection,
            operation,
        } => match gatt::cancel_prepared_io(operation)
            .map_err(|error| dispatch_error("cancel Windows GATT operation", error))?
        {
            gatt::IoCancellationDisposition::Queued => {
                gatt::drive_after_io_cancellation(&backend.state, connection);
                Ok(())
            }
            gatt::IoCancellationDisposition::ActiveAccepted => Ok(()),
            gatt::IoCancellationDisposition::AlreadyCompleting => {
                Err(CommandDispatchError::OperationAlreadyCompleted { target_command_id })
            }
        },
        CancellationAction::Discovery {
            connection,
            operation,
        } => match gatt::cancel_prepared_discovery(operation)
            .map_err(|error| dispatch_error("cancel Windows GATT discovery", error))?
        {
            gatt::DiscoveryCancellationDisposition::Queued => {
                gatt::drive_after_discovery_cancellation(&backend.state, connection);
                Ok(())
            }
            gatt::DiscoveryCancellationDisposition::ActiveAccepted => Ok(()),
            gatt::DiscoveryCancellationDisposition::AlreadyCompleting => {
                Err(CommandDispatchError::OperationAlreadyCompleted { target_command_id })
            }
        },
    }
}

pub(crate) fn begin_connect(
    backend: &WindowsBackend,
    command: ConnectCommand,
) -> Result<(), CommandDispatchError> {
    let key = {
        let state = lock_state(&backend.state);
        ensure_ready(&state)?;
        let key = state
            .connection_peripheral_key(command.peripheral_id)
            .ok_or(CommandDispatchError::InvalidRequest {
                detail: "the peripheral ID was not discovered by this Windows backend".to_owned(),
            })?;
        ensure_connection_available(&state, command)?;
        key
    };

    let operation = BluetoothLEDevice::FromBluetoothAddressWithBluetoothAddressTypeAsync(
        key.0,
        BluetoothAddressType(key.1),
    )
    .map_err(|error| dispatch_error("create Bluetooth LE device operation", error))?;

    let delivery_guard = lock_delivery(&backend.delivery);
    let reservation = (|| {
        let mut state = lock_state(&backend.state);
        ensure_ready(&state)?;
        ensure_connection_available(&state, command)?;
        if state.connection_peripheral_key(command.peripheral_id) != Some(key) {
            return Err(CommandDispatchError::InvalidState {
                detail: "the Windows peripheral identity changed during connect dispatch"
                    .to_owned(),
            });
        }
        let generation = state.allocate_connection_generation().ok_or_else(|| {
            CommandDispatchError::BackendUnavailable {
                detail: "Windows connection generation space is exhausted".to_owned(),
                platform_details: None,
            }
        })?;
        let correlation = ConnectionCorrelation {
            generation,
            connect_command_id: command.command_id,
            connection_id: command.connection_id,
            peripheral_id: command.peripheral_id,
        };
        state.connections.insert(
            generation,
            ConnectionContext {
                correlation,
                phase: ConnectionPhase::CreatingDevice {
                    operation: operation.clone(),
                },
            },
        );
        state.insert_connection_indexes(correlation);
        let ingress = state
            .ingress()
            .ok_or_else(|| CommandDispatchError::BackendUnavailable {
                detail: "Windows event ingress disappeared during connect dispatch".to_owned(),
                platform_details: None,
            })?;
        Ok((correlation, ingress))
    })();
    let (correlation, ingress) = match reservation {
        Ok(value) => value,
        Err(error) => {
            drop(delivery_guard);
            let _ = operation.Cancel();
            return Err(error);
        }
    };
    push_events(
        &ingress,
        vec![BleEvent::ConnectionStateChanged {
            command_id: Some(command.command_id),
            connection_id: command.connection_id,
            result: ConnectionStateResult::Ok {
                state: ConnectionState::Connecting,
            },
        }],
        "begin Windows connection",
    );
    drop(delivery_guard);

    let weak_state = Arc::downgrade(&backend.state);
    let callback_resource = track_callback(&backend.state, CallbackResource::DeviceLookup);
    let handler = AsyncOperationCompletedHandler::new(move |operation, status| {
        let _callback_resource = &callback_resource;
        if let Some(state) = weak_state.upgrade() {
            handle_device_completed(&state, correlation, operation.as_ref(), status);
        }
        Ok(())
    });
    if let Err(error) = operation.SetCompleted(&handler) {
        fail_connect_from_state(
            &backend.state,
            correlation,
            event_error(
                BleErrorCategory::Connection,
                "register Bluetooth LE device completion",
                error,
            ),
        );
    }
    Ok(())
}

pub(crate) fn begin_disconnect(
    backend: &WindowsBackend,
    command: DisconnectCommand,
) -> Result<(), CommandDispatchError> {
    let delivery_guard = lock_delivery(&backend.delivery);
    let (correlation, connection, pending_connect_operation, ingress, events) = {
        let mut state = lock_state(&backend.state);
        ensure_ready(&state)?;
        let Some(generation) = state
            .active_connections
            .get(&command.connection_id)
            .copied()
        else {
            return Err(CommandDispatchError::InvalidState {
                detail: "connection lifecycle is not active".to_owned(),
            });
        };
        let Some(connection) = state.connections.get(&generation) else {
            return Err(CommandDispatchError::BackendUnavailable {
                detail: "Windows connection ownership is transitioning".to_owned(),
                platform_details: None,
            });
        };
        let correlation = connection.correlation;
        if let ConnectionPhase::Connected { gatt, .. } = &connection.phase {
            gatt::ensure_gatt_cleanup_idle(gatt)?;
        }
        let ingress = state
            .ingress()
            .ok_or_else(|| CommandDispatchError::BackendUnavailable {
                detail: "Windows event ingress disappeared during disconnect dispatch".to_owned(),
                platform_details: None,
            })?;
        let pending_connect_operation = match &connection.phase {
            ConnectionPhase::CreatingDevice { operation } => {
                Some(PendingConnectOperation::Device(operation.clone()))
            }
            ConnectionPhase::ProbingGatt { operation, .. } => {
                Some(PendingConnectOperation::GattProbe(operation.clone()))
            }
            ConnectionPhase::Connected { .. } => None,
        };
        let events = gatt::connection_teardown_events(
            match &connection.phase {
                ConnectionPhase::Connected { gatt, .. } => Some(gatt.as_ref()),
                _ => None,
            },
            "connection was disconnected while GATT discovery was pending",
            BleEvent::ConnectionStateChanged {
                command_id: Some(command.command_id),
                connection_id: command.connection_id,
                result: ConnectionStateResult::Ok {
                    state: ConnectionState::Disconnected,
                },
            },
            Some(BleEvent::CommandCompleted {
                command_id: command.command_id,
                error: None,
            }),
        );
        state.begin_connection_cleanup(correlation);
        state.remove_connection_indexes(correlation);
        let connection = pending_connect_operation
            .is_none()
            .then(|| state.connections.remove(&generation))
            .flatten();
        (
            correlation,
            connection,
            pending_connect_operation,
            ingress,
            events,
        )
    };

    let release_cleanup_after_disconnect = pending_connect_operation.is_none();
    drop(delivery_guard);
    if let Some(connection) = connection {
        cleanup_connection_context(connection, None);
    }
    if let Some(operation) = pending_connect_operation {
        log_cleanup_failure(
            operation.cancel(),
            "request cancellation of retired Windows connection attempt",
        );
    }

    let _delivery_guard = lock_delivery(&backend.delivery);
    if release_cleanup_after_disconnect {
        lock_state(&backend.state).finish_connection_cleanup(correlation);
    }
    push_events(&ingress, events, "complete Windows disconnect");
    Ok(())
}

pub(crate) fn complete_unsupported_read_rssi(
    backend: &WindowsBackend,
    command: ReadRssiCommand,
) -> Result<(), CommandDispatchError> {
    let _delivery_guard = lock_delivery(&backend.delivery);
    let ingress = {
        let state = lock_state(&backend.state);
        ensure_ready(&state)?;
        if let Some(connection) = state
            .active_connections
            .get(&command.connection_id)
            .and_then(|generation| state.connections.get(generation))
            && let ConnectionPhase::Connected { gatt, .. } = &connection.phase
        {
            gatt::ensure_gatt_cleanup_idle(gatt)?;
        }
        state
            .ingress()
            .ok_or_else(|| CommandDispatchError::BackendUnavailable {
                detail: "Windows event ingress disappeared during RSSI dispatch".to_owned(),
                platform_details: None,
            })?
    };
    push_events(
        &ingress,
        vec![BleEvent::CommandCompleted {
            command_id: command.command_id,
            error: Some(simple_event_error(
                BleErrorCategory::NotSupported,
                "Windows does not support connected one-shot RSSI reads",
            )),
        }],
        "complete unsupported Windows RSSI read",
    );
    Ok(())
}

fn ensure_connection_available(
    state: &BackendState,
    command: ConnectCommand,
) -> Result<(), CommandDispatchError> {
    if state.peripheral_connection_is_active_or_retiring(command.peripheral_id) {
        return Err(CommandDispatchError::InvalidState {
            detail: "the peripheral already has an active connection lifecycle".to_owned(),
        });
    }
    if state
        .active_connections
        .contains_key(&command.connection_id)
        || state.connection_id_is_retiring(command.connection_id)
        || state
            .connections
            .values()
            .any(|connection| connection.correlation.connection_id == command.connection_id)
    {
        return Err(CommandDispatchError::InvalidRequest {
            detail: "the connection ID is already in use by this Windows backend".to_owned(),
        });
    }
    Ok(())
}

fn handle_device_completed(
    state: &Arc<Mutex<BackendState>>,
    correlation: ConnectionCorrelation,
    operation: Option<&IAsyncOperation<BluetoothLEDevice>>,
    status: AsyncStatus,
) {
    let (phase, active) = {
        let mut current = lock_state(state);
        let Some(connection) = current.connections.remove(&correlation.generation) else {
            return;
        };
        if connection.correlation != correlation {
            current
                .connections
                .insert(connection.correlation.generation, connection);
            return;
        }
        let active = current.connection_is_active(correlation);
        (connection.phase, active)
    };
    match phase {
        ConnectionPhase::CreatingDevice { operation: _ } => {}
        phase => {
            lock_state(state).connections.insert(
                correlation.generation,
                ConnectionContext { correlation, phase },
            );
            return;
        }
    }

    if status != AsyncStatus::Completed {
        finish_removed_connect_failure(
            state,
            correlation,
            None,
            simple_event_error(
                BleErrorCategory::Connection,
                format!("Bluetooth LE device lookup ended with {status:?}"),
            ),
        );
        return;
    }
    let Some(operation) = operation else {
        finish_removed_connect_failure(
            state,
            correlation,
            None,
            simple_event_error(
                BleErrorCategory::Internal,
                "Bluetooth LE device completion omitted the operation",
            ),
        );
        return;
    };
    let device = match operation.GetResults() {
        Ok(device) => device,
        Err(error) => {
            finish_removed_connect_failure(
                state,
                correlation,
                None,
                event_error(
                    BleErrorCategory::Connection,
                    "complete Bluetooth LE device lookup",
                    error,
                ),
            );
            return;
        }
    };
    if Interface::as_raw(&device).is_null() {
        finish_removed_connect_failure(
            state,
            correlation,
            None,
            simple_event_error(
                BleErrorCategory::NotFound,
                "Windows could not create a Bluetooth LE device for the peripheral",
            ),
        );
        return;
    }
    if !active {
        log_cleanup_failure(
            device.Close(),
            "close Bluetooth LE device after retired lookup completed",
        );
        finish_removed_connect_failure(
            state,
            correlation,
            None,
            simple_event_error(
                BleErrorCategory::Connection,
                "connection lifecycle ended before the Windows GATT probe could establish connection evidence",
            ),
        );
        return;
    }

    let weak_state = Arc::downgrade(state);
    let status_resource = track_callback(state, CallbackResource::ConnectionStatusChanged);
    let status_handler =
        TypedEventHandler::<BluetoothLEDevice, IInspectable>::new(move |sender, _args| {
            let _status_resource = &status_resource;
            if let (Some(state), Some(device)) = (weak_state.upgrade(), sender.as_ref()) {
                handle_connection_status_changed(&state, correlation, device);
            }
            Ok(())
        });
    let connection_token = match device.ConnectionStatusChanged(&status_handler) {
        Ok(token) => token,
        Err(error) => {
            finish_removed_connect_failure(
                state,
                correlation,
                Some(ConnectionPhase::Connected {
                    device,
                    connection_token: 0,
                    gatt_services_token: None,
                    gatt: Box::new(gatt::ConnectionGattState::new()),
                }),
                event_error(
                    BleErrorCategory::Connection,
                    "register connection-status callback",
                    error,
                ),
            );
            return;
        }
    };
    let weak_state = Arc::downgrade(state);
    let gatt_services_resource = track_callback(state, CallbackResource::GattServicesChanged);
    let gatt_services_handler =
        TypedEventHandler::<BluetoothLEDevice, IInspectable>::new(move |_sender, _args| {
            let _gatt_services_resource = &gatt_services_resource;
            if let Some(state) = weak_state.upgrade() {
                gatt::handle_services_changed(&state, correlation);
            }
            Ok(())
        });
    let gatt_services_token = match device.GattServicesChanged(&gatt_services_handler) {
        Ok(token) => token,
        Err(error) => {
            finish_removed_connect_failure(
                state,
                correlation,
                Some(ConnectionPhase::Connected {
                    device,
                    connection_token,
                    gatt_services_token: None,
                    gatt: Box::new(gatt::ConnectionGattState::new()),
                }),
                event_error(
                    BleErrorCategory::Connection,
                    "register GATT-services-changed callback",
                    error,
                ),
            );
            return;
        }
    };
    let services_operation =
        match device.GetGattServicesWithCacheModeAsync(BluetoothCacheMode::Uncached) {
            Ok(operation) => operation,
            Err(error) => {
                finish_removed_connect_failure(
                    state,
                    correlation,
                    Some(ConnectionPhase::Connected {
                        device,
                        connection_token,
                        gatt_services_token: Some(gatt_services_token),
                        gatt: Box::new(gatt::ConnectionGattState::new()),
                    }),
                    event_error(
                        BleErrorCategory::Connection,
                        "start uncached GATT service probe",
                        error,
                    ),
                );
                return;
            }
        };

    let should_continue = {
        let mut current = lock_state(state);
        if current.lifecycle == Lifecycle::Ready && current.connection_is_active(correlation) {
            current.connections.insert(
                correlation.generation,
                ConnectionContext {
                    correlation,
                    phase: ConnectionPhase::ProbingGatt {
                        device: device.clone(),
                        connection_token,
                        gatt_services_token,
                        operation: services_operation.clone(),
                    },
                },
            );
            true
        } else {
            false
        }
    };
    if !should_continue {
        let connection = ConnectionContext {
            correlation,
            phase: ConnectionPhase::ProbingGatt {
                device,
                connection_token,
                gatt_services_token,
                operation: services_operation,
            },
        };
        cleanup_connection_context(connection, None);
        return;
    }

    let weak_state = Arc::downgrade(state);
    let callback_resource = track_callback(state, CallbackResource::GattProbe);
    let handler = AsyncOperationCompletedHandler::new(move |operation, status| {
        let _callback_resource = &callback_resource;
        if let Some(state) = weak_state.upgrade() {
            handle_service_probe_completed(&state, correlation, operation.as_ref(), status);
        }
        Ok(())
    });
    if let Err(error) = services_operation.SetCompleted(&handler) {
        fail_connect_from_state(
            state,
            correlation,
            event_error(
                BleErrorCategory::Connection,
                "register uncached GATT service probe completion",
                error,
            ),
        );
    }
}

fn handle_service_probe_completed(
    state: &Arc<Mutex<BackendState>>,
    correlation: ConnectionCorrelation,
    operation: Option<&IAsyncOperation<GattDeviceServicesResult>>,
    status: AsyncStatus,
) {
    let (device, connection_token, gatt_services_token) = {
        let current = lock_state(state);
        let Some(connection) = current.connections.get(&correlation.generation) else {
            return;
        };
        if connection.correlation != correlation {
            return;
        }
        match &connection.phase {
            ConnectionPhase::ProbingGatt {
                device,
                connection_token,
                gatt_services_token,
                operation: _,
            } => (device.clone(), *connection_token, *gatt_services_token),
            _ => return,
        }
    };

    if status != AsyncStatus::Completed {
        fail_connect_from_state(
            state,
            correlation,
            simple_event_error(
                BleErrorCategory::Connection,
                format!("uncached GATT service probe ended with {status:?}"),
            ),
        );
        return;
    }
    let Some(operation) = operation else {
        fail_connect_from_state(
            state,
            correlation,
            simple_event_error(
                BleErrorCategory::Internal,
                "uncached GATT service probe completion omitted the operation",
            ),
        );
        return;
    };
    let result = match operation.GetResults() {
        Ok(result) => result,
        Err(error) => {
            fail_connect_from_state(
                state,
                correlation,
                event_error(
                    BleErrorCategory::Connection,
                    "complete uncached GATT service probe",
                    error,
                ),
            );
            return;
        }
    };
    let communication_status = match result.Status() {
        Ok(status) => status,
        Err(error) => {
            fail_connect_from_state(
                state,
                correlation,
                event_error(
                    BleErrorCategory::Connection,
                    "read uncached GATT service probe status",
                    error,
                ),
            );
            return;
        }
    };
    if communication_status != GattCommunicationStatus::Success {
        fail_connect_from_state(
            state,
            correlation,
            gatt_connection_error(communication_status),
        );
        return;
    }
    let services = match result.Services() {
        Ok(services) => services,
        Err(error) => {
            fail_connect_from_state(
                state,
                correlation,
                event_error(
                    BleErrorCategory::Connection,
                    "read services from uncached GATT connection probe",
                    error,
                ),
            );
            return;
        }
    };
    let service_count = match services.Size() {
        Ok(count) => count,
        Err(error) => {
            fail_connect_from_state(
                state,
                correlation,
                event_error(
                    BleErrorCategory::Connection,
                    "read service count from uncached GATT connection probe",
                    error,
                ),
            );
            return;
        }
    };
    let mut temporary_services = Vec::with_capacity(service_count as usize);
    for index in 0..service_count {
        match services.GetAt(index) {
            Ok(service) => temporary_services.push(service),
            Err(error) => {
                for service in temporary_services {
                    log_cleanup_failure(
                        service.Close(),
                        "close temporary GATT service from failed connection probe",
                    );
                }
                fail_connect_from_state(
                    state,
                    correlation,
                    event_error(
                        BleErrorCategory::Connection,
                        "read service from uncached GATT connection probe",
                        error,
                    ),
                );
                return;
            }
        }
    }
    for service in temporary_services {
        log_cleanup_failure(
            service.Close(),
            "close temporary GATT service from connection probe",
        );
    }
    match device.ConnectionStatus() {
        Ok(BluetoothConnectionStatus::Connected) => {}
        Ok(status) => {
            fail_connect_from_state(
                state,
                correlation,
                simple_event_error(
                    BleErrorCategory::Connection,
                    format!("uncached GATT probe succeeded but connection status was {status:?}"),
                ),
            );
            return;
        }
        Err(error) => {
            fail_connect_from_state(
                state,
                correlation,
                event_error(
                    BleErrorCategory::Connection,
                    "read Bluetooth LE connection status after GATT probe",
                    error,
                ),
            );
            return;
        }
    }

    request_throughput_connection_parameters(&device);

    let delivery_gate = delivery_for_state(state);
    let delivery_guard = lock_delivery(&delivery_gate);
    let (retired_connection, ingress) = {
        let mut current = lock_state(state);
        if current.lifecycle != Lifecycle::Ready {
            return;
        }
        let Some(connection) = current.connections.get(&correlation.generation) else {
            return;
        };
        if connection.correlation != correlation
            || !matches!(connection.phase, ConnectionPhase::ProbingGatt { .. })
        {
            return;
        }
        let active = current.connection_is_active(correlation);
        let ingress = current.ingress();
        let retired_connection = if active {
            let Some(connection) = current.connections.get_mut(&correlation.generation) else {
                return;
            };
            connection.phase = ConnectionPhase::Connected {
                device: device.clone(),
                connection_token,
                gatt_services_token: Some(gatt_services_token),
                gatt: Box::new(gatt::ConnectionGattState::new()),
            };
            None
        } else {
            current.begin_connection_cleanup(correlation);
            current.connections.remove(&correlation.generation)
        };
        (retired_connection, ingress)
    };
    if let Some(connection) = retired_connection {
        drop(delivery_guard);
        cleanup_connection_context(connection, None);
        let _delivery_guard = lock_delivery(&delivery_gate);
        lock_state(state).finish_connection_cleanup(correlation);
        if let Some(ingress) = ingress {
            // Windows cannot prove cancellation: a successful retired probe may report the
            // original Connected domain event and connect terminal after logical disconnect.
            // Active indexes stay absent; under Plan 8.4 the caller classifies these IDs as stale.
            push_events(
                &ingress,
                connected_events(correlation),
                "deliver retired Windows connection completion",
            );
        }
        return;
    }
    if let Some(ingress) = ingress {
        push_events(
            &ingress,
            connected_events(correlation),
            "complete Windows connection",
        );
    }
}

fn handle_connection_status_changed(
    state: &Arc<Mutex<BackendState>>,
    correlation: ConnectionCorrelation,
    device: &BluetoothLEDevice,
) {
    let status = match device.ConnectionStatus() {
        Ok(status) => status,
        Err(error) => {
            report_connection_callback_error(
                state,
                correlation,
                event_error(
                    BleErrorCategory::Connection,
                    "read Bluetooth LE connection status in callback",
                    error,
                ),
            );
            return;
        }
    };
    if status != BluetoothConnectionStatus::Disconnected {
        return;
    }

    if gatt::defer_connection_disconnect_if_cleaning(state, correlation) {
        return;
    }

    {
        let current = lock_state(state);
        if !current.connection_is_active(correlation)
            || !current
                .connections
                .get(&correlation.generation)
                .is_some_and(|connection| {
                    connection.correlation == correlation
                        && matches!(
                            connection.phase,
                            ConnectionPhase::ProbingGatt { .. } | ConnectionPhase::Connected { .. }
                        )
                })
        {
            return;
        }
    }

    let delivery_gate = delivery_for_state(state);
    let delivery_guard = lock_delivery(&delivery_gate);
    let (connection, ingress, events) = {
        let mut current = lock_state(state);
        if !current.connection_is_active(correlation) {
            return;
        }
        let Some(connection) = current.connections.get(&correlation.generation) else {
            return;
        };
        if connection.correlation != correlation {
            return;
        }
        if let Some(gatt) = gatt::connected_gatt_mut(&mut current, correlation)
            && gatt::defer_connection_disconnect_if_cleaning_locked(gatt)
        {
            return;
        }
        let events = match &connection.phase {
            ConnectionPhase::ProbingGatt { .. } => connect_failure_events(
                correlation,
                true,
                simple_event_error(
                    BleErrorCategory::Connection,
                    "Bluetooth LE device disconnected while establishing the GATT connection",
                ),
            ),
            ConnectionPhase::Connected { gatt, .. } => gatt::connection_teardown_events(
                Some(gatt.as_ref()),
                "connection was lost while GATT discovery was pending",
                BleEvent::ConnectionStateChanged {
                    command_id: None,
                    connection_id: correlation.connection_id,
                    result: ConnectionStateResult::Ok {
                        state: ConnectionState::Disconnected,
                    },
                },
                None,
            ),
            ConnectionPhase::CreatingDevice { .. } => return,
        };
        current.begin_connection_cleanup(correlation);
        let connection = current.remove_connection(correlation.generation);
        (connection, current.ingress(), events)
    };
    drop(delivery_guard);
    if let Some(connection) = connection {
        cleanup_connection_context(connection, None);
    }
    let _delivery_guard = lock_delivery(&delivery_gate);
    lock_state(state).finish_connection_cleanup(correlation);
    if let Some(ingress) = ingress {
        push_events(&ingress, events, "deliver passive Windows disconnect");
    }
}

pub(crate) fn drain_deferred_connection_disconnect(
    state: &Arc<Mutex<BackendState>>,
    correlation: ConnectionCorrelation,
) {
    let device = {
        let current = lock_state(state);
        if !current.connection_is_active(correlation) {
            return;
        }
        current
            .connections
            .get(&correlation.generation)
            .filter(|connection| connection.correlation == correlation)
            .and_then(|connection| match &connection.phase {
                ConnectionPhase::ProbingGatt { device, .. }
                | ConnectionPhase::Connected { device, .. } => Some(device.clone()),
                ConnectionPhase::CreatingDevice { .. } => None,
            })
    };
    if let Some(device) = device {
        handle_connection_status_changed(state, correlation, &device);
    }
}

fn report_connection_callback_error(
    state: &Arc<Mutex<BackendState>>,
    correlation: ConnectionCorrelation,
    error: BleEventError,
) {
    let delivery_gate = delivery_for_state(state);
    let _delivery_guard = lock_delivery(&delivery_gate);
    let ingress = {
        let current = lock_state(state);
        current
            .connection_is_active(correlation)
            .then(|| current.ingress())
            .flatten()
    };
    if let Some(ingress) = ingress {
        push_events(
            &ingress,
            vec![BleEvent::BackendError { error }],
            "report Windows connection callback failure",
        );
    }
}

fn fail_connect_from_state(
    state: &Arc<Mutex<BackendState>>,
    correlation: ConnectionCorrelation,
    error: BleEventError,
) {
    let delivery_gate = delivery_for_state(state);
    let delivery_guard = lock_delivery(&delivery_gate);
    let (connection, ingress, events) = {
        let mut current = lock_state(state);
        let Some(connection) = current.connections.remove(&correlation.generation) else {
            return;
        };
        if connection.correlation != correlation {
            current
                .connections
                .insert(connection.correlation.generation, connection);
            return;
        }
        let active = current.connection_is_active(correlation);
        current.begin_connection_cleanup(correlation);
        if active {
            current.remove_connection_indexes(correlation);
        }
        let ingress = current.ingress();
        let events = connect_failure_events(correlation, active, error);
        (connection, ingress, events)
    };
    drop(delivery_guard);
    cleanup_connection_context(connection, None);
    let _delivery_guard = lock_delivery(&delivery_gate);
    lock_state(state).finish_connection_cleanup(correlation);
    if let Some(ingress) = ingress {
        push_events(&ingress, events, "fail Windows connection");
    }
}

fn finish_removed_connect_failure(
    state: &Arc<Mutex<BackendState>>,
    correlation: ConnectionCorrelation,
    cleanup_phase: Option<ConnectionPhase>,
    error: BleEventError,
) {
    let delivery_gate = delivery_for_state(state);
    let delivery_guard = lock_delivery(&delivery_gate);
    let (ingress, events) = {
        let mut current = lock_state(state);
        let active = current.connection_is_active(correlation);
        current.begin_connection_cleanup(correlation);
        if active {
            current.remove_connection_indexes(correlation);
        }
        (
            current.ingress(),
            connect_failure_events(correlation, active, error),
        )
    };
    drop(delivery_guard);
    if let Some(phase) = cleanup_phase {
        cleanup_connection_context(ConnectionContext { correlation, phase }, None);
    }
    let _delivery_guard = lock_delivery(&delivery_gate);
    lock_state(state).finish_connection_cleanup(correlation);
    if let Some(ingress) = ingress {
        push_events(&ingress, events, "fail Windows connection completion");
    }
}

fn request_throughput_connection_parameters(device: &BluetoothLEDevice) {
    let Ok(parameters) = BluetoothLEPreferredConnectionParameters::ThroughputOptimized() else {
        tracing::debug!(
            target: "blehub_windows::connection",
            "Windows throughput-optimized connection parameters are unavailable"
        );
        return;
    };
    match device.RequestPreferredConnectionParameters(&parameters) {
        Ok(result) => match result.Status() {
            Ok(status) if status.0 == 1 => {
                tracing::debug!(
                    target: "blehub_windows::connection",
                    "Windows accepted throughput-optimized connection parameters"
                );
            }
            Ok(status) => {
                tracing::debug!(
                    target: "blehub_windows::connection",
                    ?status,
                    "Windows did not accept throughput-optimized connection parameters"
                );
            }
            Err(error) => {
                tracing::debug!(
                    target: "blehub_windows::connection",
                    detail = %error,
                    "Windows connection-parameter request returned no status"
                );
            }
        },
        Err(error) => {
            tracing::debug!(
                target: "blehub_windows::connection",
                detail = %error,
                "Windows connection-parameter request is unsupported"
            );
        }
    }
}

fn connected_events(correlation: ConnectionCorrelation) -> Vec<BleEvent> {
    vec![
        BleEvent::ConnectionStateChanged {
            command_id: Some(correlation.connect_command_id),
            connection_id: correlation.connection_id,
            result: ConnectionStateResult::Ok {
                state: ConnectionState::Connected,
            },
        },
        BleEvent::CommandCompleted {
            command_id: correlation.connect_command_id,
            error: None,
        },
    ]
}

fn connect_failure_events(
    correlation: ConnectionCorrelation,
    active: bool,
    error: BleEventError,
) -> Vec<BleEvent> {
    let mut events = Vec::with_capacity(if active { 2 } else { 1 });
    if active {
        events.push(BleEvent::ConnectionStateChanged {
            command_id: Some(correlation.connect_command_id),
            connection_id: correlation.connection_id,
            result: ConnectionStateResult::Err {
                error: error.clone(),
            },
        });
    }
    events.push(BleEvent::CommandCompleted {
        command_id: correlation.connect_command_id,
        error: Some(error),
    });
    events
}

fn gatt_connection_error(status: GattCommunicationStatus) -> BleEventError {
    let category = match status {
        GattCommunicationStatus::AccessDenied => BleErrorCategory::Permission,
        GattCommunicationStatus::ProtocolError => BleErrorCategory::Protocol,
        GattCommunicationStatus::Unreachable => BleErrorCategory::Connection,
        _ => BleErrorCategory::Backend,
    };
    BleEventError {
        category,
        message: format!("uncached GATT service probe failed with {status:?}"),
        platform_details: Some(PlatformErrorDetails {
            platform: Platform::Windows,
            code: Some(status.0.to_string()),
            name: Some(format!("{status:?}")),
            message: None,
        }),
    }
}

pub(crate) fn cleanup_connection_context(
    connection: ConnectionContext,
    mut first_error: Option<&mut Option<BleHubError>>,
) {
    match connection.phase {
        ConnectionPhase::CreatingDevice { operation } => {
            record_connection_cleanup(
                operation.Cancel(),
                "cancel Bluetooth LE device lookup",
                &mut first_error,
            );
        }
        ConnectionPhase::ProbingGatt {
            device,
            connection_token,
            gatt_services_token,
            operation,
        } => {
            record_connection_cleanup(
                operation.Cancel(),
                "cancel GATT connection probe",
                &mut first_error,
            );
            record_connection_cleanup(
                device.RemoveGattServicesChanged(gatt_services_token),
                "remove GATT-services-changed callback",
                &mut first_error,
            );
            record_connection_cleanup(
                device.RemoveConnectionStatusChanged(connection_token),
                "remove connection-status callback",
                &mut first_error,
            );
            record_connection_cleanup(
                device.Close(),
                "close Bluetooth LE device",
                &mut first_error,
            );
        }
        ConnectionPhase::Connected {
            device,
            connection_token,
            gatt_services_token,
            gatt,
        } => {
            gatt::cancel_pending_operation(&gatt, &mut first_error);
            if let Some(token) = gatt_services_token {
                record_connection_cleanup(
                    device.RemoveGattServicesChanged(token),
                    "remove GATT-services-changed callback",
                    &mut first_error,
                );
            }
            if connection_token != 0 {
                record_connection_cleanup(
                    device.RemoveConnectionStatusChanged(connection_token),
                    "remove connection-status callback",
                    &mut first_error,
                );
            }
            gatt::cleanup_connection_gatt(*gatt, first_error.as_deref_mut());
            record_connection_cleanup(
                device.Close(),
                "close Bluetooth LE device",
                &mut first_error,
            );
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use blehub::{
        AdapterSnapshot, AdapterState, AuthorizationState, BleEventSink, BleHub, EventSinkError,
    };

    use crate::initialization::InitializationCallbackBarrier;

    struct RecordingSink {
        events: Arc<Mutex<Vec<BleEvent>>>,
    }

    impl BleEventSink for RecordingSink {
        fn on_event(&self, event: BleEvent) -> Result<(), EventSinkError> {
            self.events
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner)
                .push(event);
            Ok(())
        }
    }

    fn test_connection_correlation() -> ConnectionCorrelation {
        ConnectionCorrelation {
            generation: 7,
            connect_command_id: CommandId { value: 11 },
            connection_id: ConnectionId { value: 12 },
            peripheral_id: PeripheralId { value: 13 },
        }
    }

    #[test]
    fn pending_disconnect_cleanup_reservation_is_idempotent_until_callback_release() {
        let mut state = BackendState::new_without_process_lease_for_test(Arc::new(Mutex::new(())));
        let correlation = test_connection_correlation();
        state.begin_connection_cleanup(correlation);
        state.begin_connection_cleanup(correlation);

        assert!(matches!(
            ensure_connection_available(
                &state,
                ConnectCommand {
                    command_id: CommandId { value: 20 },
                    connection_id: ConnectionId { value: 21 },
                    peripheral_id: correlation.peripheral_id,
                },
            ),
            Err(CommandDispatchError::InvalidState { .. })
        ));
        assert!(matches!(
            ensure_connection_available(
                &state,
                ConnectCommand {
                    command_id: CommandId { value: 22 },
                    connection_id: correlation.connection_id,
                    peripheral_id: PeripheralId { value: 23 },
                },
            ),
            Err(CommandDispatchError::InvalidRequest { .. })
        ));

        // The outer pending-disconnect path deliberately does not release. Repeating the
        // callback reservation models synchronous Cancel completion and remains one owner.
        assert!(state.connection_id_is_retiring(correlation.connection_id));
        assert!(state.peripheral_connection_is_active_or_retiring(correlation.peripheral_id));

        // The eventual retired callback releases both identities exactly once.
        state.finish_connection_cleanup(correlation);
        assert_eq!(
            ensure_connection_available(
                &state,
                ConnectCommand {
                    command_id: CommandId { value: 24 },
                    connection_id: correlation.connection_id,
                    peripheral_id: correlation.peripheral_id,
                },
            ),
            Ok(())
        );

        // A synchronous or duplicate completion cannot unbalance cleanup ownership.
        state.finish_connection_cleanup(correlation);
        assert_eq!(
            ensure_connection_available(
                &state,
                ConnectCommand {
                    command_id: CommandId { value: 25 },
                    connection_id: correlation.connection_id,
                    peripheral_id: correlation.peripheral_id,
                },
            ),
            Ok(())
        );
    }

    #[test]
    fn retired_probe_success_preserves_original_ids_after_cleanup_releases_identity() {
        let mut state = BackendState::new_without_process_lease_for_test(Arc::new(Mutex::new(())));
        let correlation = test_connection_correlation();
        state.insert_connection_indexes(correlation);
        state.begin_connection_cleanup(correlation);
        state.remove_connection_indexes(correlation);
        state.finish_connection_cleanup(correlation);

        assert!(!state.connection_is_active(correlation));
        assert!(!state.connection_id_is_retiring(correlation.connection_id));
        assert!(!state.peripheral_connection_is_active_or_retiring(correlation.peripheral_id));
        assert_eq!(
            ensure_connection_available(
                &state,
                ConnectCommand {
                    command_id: CommandId { value: 30 },
                    connection_id: correlation.connection_id,
                    peripheral_id: correlation.peripheral_id,
                },
            ),
            Ok(())
        );
        assert_eq!(
            connected_events(correlation),
            vec![
                BleEvent::ConnectionStateChanged {
                    command_id: Some(correlation.connect_command_id),
                    connection_id: correlation.connection_id,
                    result: ConnectionStateResult::Ok {
                        state: ConnectionState::Connected,
                    },
                },
                BleEvent::CommandCompleted {
                    command_id: correlation.connect_command_id,
                    error: None,
                },
            ]
        );
    }

    #[test]
    fn active_connect_failure_reuses_the_same_error_for_state_and_terminal() {
        let correlation = test_connection_correlation();
        let error = simple_event_error(BleErrorCategory::Connection, "probe failed");

        assert_eq!(
            connect_failure_events(correlation, true, error.clone()),
            vec![
                BleEvent::ConnectionStateChanged {
                    command_id: Some(correlation.connect_command_id),
                    connection_id: correlation.connection_id,
                    result: ConnectionStateResult::Err {
                        error: error.clone(),
                    },
                },
                BleEvent::CommandCompleted {
                    command_id: correlation.connect_command_id,
                    error: Some(error),
                },
            ]
        );
    }

    #[test]
    fn retired_connect_failure_only_terminates_the_original_command() {
        let correlation = test_connection_correlation();
        let error = simple_event_error(BleErrorCategory::Connection, "late probe failed");

        assert_eq!(
            connect_failure_events(correlation, false, error.clone()),
            vec![BleEvent::CommandCompleted {
                command_id: correlation.connect_command_id,
                error: Some(error),
            }]
        );
    }

    #[test]
    fn unsupported_connected_rssi_is_accepted_and_completes_with_one_terminal() {
        let delivery = Arc::new(Mutex::new(()));
        let state = Arc::new(Mutex::new(
            BackendState::new_without_process_lease_for_test(Arc::clone(&delivery)),
        ));
        let backend = Arc::new(WindowsBackend {
            state: Arc::clone(&state),
            delivery,
            initialization_setup: Mutex::new(()),
            initialization_callbacks: Arc::new(InitializationCallbackBarrier::new()),
        });
        let events = Arc::new(Mutex::new(Vec::new()));
        let hub = BleHub::new(
            backend,
            Arc::new(RecordingSink {
                events: Arc::clone(&events),
            }),
        )
        .unwrap();
        {
            let mut state = lock_state(&state);
            state.lifecycle = Lifecycle::Ready;
            state.adapter_snapshot = Some(AdapterSnapshot {
                state: AdapterState::PoweredOn,
                scan_authorization: AuthorizationState::NotRequired,
                connection_authorization: AuthorizationState::NotRequired,
                name: None,
            });
        }
        let command = ReadRssiCommand {
            command_id: CommandId { value: 31 },
            connection_id: ConnectionId { value: 32 },
        };

        assert!(!hub.capabilities().supports_read_rssi);
        assert_eq!(hub.read_rssi(command), Ok(()));
        assert_eq!(
            *events
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner),
            vec![BleEvent::CommandCompleted {
                command_id: command.command_id,
                error: Some(simple_event_error(
                    BleErrorCategory::NotSupported,
                    "Windows does not support connected one-shot RSSI reads",
                )),
            }]
        );
    }
}
