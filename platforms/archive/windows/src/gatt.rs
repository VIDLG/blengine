use std::collections::{HashMap, VecDeque};

use blehub::{
    BleErrorCategory, BleEvent, BleEventError, CharacteristicProperties, CommandDispatchError,
    CommandId, DiscoverGattCommand, GattCharacteristic, GattCharacteristicId, GattDatabase,
    GattDescriptor, GattDescriptorId, GattService, GattServiceId, GattSessionId,
    MAX_WRITE_VALUE_BYTES, NotificationAction, NotificationMode, NotificationModeRequest,
    NotificationState, Platform, PlatformErrorDetails, ReadCharacteristicCommand,
    SetNotificationsCommand, SubscriptionId, WriteCharacteristicCommand, WriteType,
};
use windows::Devices::Bluetooth::{
    BluetoothCacheMode, BluetoothLEDevice,
    GenericAttributeProfile::{
        GattCharacteristic as WinrtGattCharacteristic, GattCharacteristicsResult,
        GattClientCharacteristicConfigurationDescriptorValue, GattCommunicationStatus,
        GattDescriptor as NativeGattDescriptor, GattDescriptorsResult, GattDeviceService,
        GattDeviceServicesResult, GattReadResult, GattValueChangedEventArgs, GattWriteOption,
        GattWriteResult,
    },
};
use windows::{Foundation::TypedEventHandler, Storage::Streams::DataWriter, core::Interface};
use windows_future::{AsyncOperationCompletedHandler, AsyncStatus, IAsyncOperation};

use crate::{
    backend::{
        BackendState, Lifecycle, WindowsBackend, delivery_for_state, ensure_ready, lock_delivery,
        lock_state, push_events, record_connection_cleanup, track_callback,
    },
    connection::{ConnectionCorrelation, ConnectionPhase, drain_deferred_connection_disconnect},
    error::{dispatch_error, event_error, simple_event_error},
    mapping::{map_characteristic_properties, map_guid, read_buffer},
    resource_accounting::CallbackResource,
};

mod discovery;
mod io;

pub(super) use discovery::DiscoveryCancellation;
use discovery::{
    PendingDiscovery, PendingOperationState, cleanup_draft, cleanup_pending,
    discovery_failure_event,
};
pub(super) use io::IoCancellation;
use io::{
    QueuedIo, SubscriptionRoute, cleanup_io_queue, cleanup_subscription_routes, io_failure_events,
    take_subscriptions,
};

#[cfg(test)]
mod tests;

pub(super) fn begin_discovery(
    backend: &WindowsBackend,
    command: DiscoverGattCommand,
) -> Result<(), CommandDispatchError> {
    discovery::begin_discovery(backend, command)
}

pub(super) fn begin_read(
    backend: &WindowsBackend,
    command: ReadCharacteristicCommand,
) -> Result<(), CommandDispatchError> {
    io::begin_read(backend, command)
}

pub(super) fn begin_write(
    backend: &WindowsBackend,
    command: WriteCharacteristicCommand,
) -> Result<(), CommandDispatchError> {
    io::begin_write(backend, command)
}

pub(super) fn begin_set_notifications(
    backend: &WindowsBackend,
    command: SetNotificationsCommand,
) -> Result<(), CommandDispatchError> {
    io::begin_set_notifications(backend, command)
}

/// Cancels a queued or active characteristic/notification operation through
/// the same per-connection FIFO that owns its native callback correlation.
/// Connect and discovery operations remain outside this GATT-only path.
pub(super) fn prepare_io_cancellation(
    gatt: &mut ConnectionGattState,
    command_id: CommandId,
) -> Option<IoCancellation> {
    io::prepare_cancellation(gatt, command_id)
}

pub(super) enum IoCancellationDisposition {
    Queued,
    ActiveAccepted,
    AlreadyCompleting,
}

pub(super) fn cancel_prepared_io(
    action: IoCancellation,
) -> Result<IoCancellationDisposition, windows::core::Error> {
    match action {
        IoCancellation::Queued => Ok(IoCancellationDisposition::Queued),
        IoCancellation::Active(operation) => {
            operation.cancel()?;
            Ok(IoCancellationDisposition::ActiveAccepted)
        }
        IoCancellation::AlreadyCompleting => Ok(IoCancellationDisposition::AlreadyCompleting),
    }
}

pub(super) fn drive_after_io_cancellation(
    state: &std::sync::Arc<std::sync::Mutex<BackendState>>,
    connection: ConnectionCorrelation,
) {
    if drive_io_queue(state, connection) {
        drain_deferred_services_changed(state, connection);
    }
}

pub(super) fn prepare_discovery_cancellation(
    gatt: &mut ConnectionGattState,
    command_id: CommandId,
) -> Option<DiscoveryCancellation> {
    discovery::prepare_cancellation(gatt, command_id)
}

pub(super) enum DiscoveryCancellationDisposition {
    Queued,
    ActiveAccepted,
    AlreadyCompleting,
}

pub(super) fn cancel_prepared_discovery(
    action: DiscoveryCancellation,
) -> Result<DiscoveryCancellationDisposition, windows::core::Error> {
    match action {
        DiscoveryCancellation::Queued => Ok(DiscoveryCancellationDisposition::Queued),
        DiscoveryCancellation::Active(operation) => {
            operation.cancel()?;
            Ok(DiscoveryCancellationDisposition::ActiveAccepted)
        }
        DiscoveryCancellation::AlreadyCompleting => {
            Ok(DiscoveryCancellationDisposition::AlreadyCompleting)
        }
    }
}

pub(super) fn drive_after_discovery_cancellation(
    state: &std::sync::Arc<std::sync::Mutex<BackendState>>,
    correlation: ConnectionCorrelation,
) {
    discovery::run_discovery_driver(state, correlation);
}

pub(super) struct MonotonicIdAllocator {
    next: u64,
}

impl MonotonicIdAllocator {
    pub(super) fn new() -> Self {
        Self { next: 1 }
    }

    fn allocate_one(&mut self) -> Option<u64> {
        self.allocate_many(1).map(|values| values.start)
    }

    fn allocate_many(&mut self, count: usize) -> Option<std::ops::Range<u64>> {
        let count = u64::try_from(count).ok()?;
        let start = self.next;
        let end = start.checked_add(count)?;
        self.next = end;
        Some(start..end)
    }
}

pub(super) struct ConnectionGattState {
    cache_revision: u64,
    cleanup_in_progress: bool,
    services_changed_pending: bool,
    disconnect_pending: bool,

    subscriptions: HashMap<SubscriptionId, io::SubscriptionRoute>,
    subscription_by_characteristic: HashMap<GattCharacteristicId, SubscriptionId>,
    pending: Option<discovery::PendingDiscovery>,
    io_queue: VecDeque<io::QueuedIo>,
    io_driver_running: bool,
    io_transition_in_progress: bool,
    active: Option<NativeGattMap>,
}

impl ConnectionGattState {
    pub(super) fn new() -> Self {
        Self {
            cache_revision: 0,
            cleanup_in_progress: false,
            services_changed_pending: false,
            disconnect_pending: false,

            subscriptions: HashMap::new(),
            subscription_by_characteristic: HashMap::new(),
            pending: None,
            io_queue: VecDeque::new(),
            io_driver_running: false,
            io_transition_in_progress: false,
            active: None,
        }
    }

    fn finish_cleanup(&mut self) -> DeferredCleanupCallbacks {
        self.cleanup_in_progress = false;
        DeferredCleanupCallbacks {
            services_changed: std::mem::take(&mut self.services_changed_pending),
            disconnected: std::mem::take(&mut self.disconnect_pending),
        }
    }

    #[allow(dead_code)]
    pub(super) fn lookup_characteristic(
        &self,
        gatt_session_id: GattSessionId,
        characteristic_id: GattCharacteristicId,
    ) -> Option<&WinrtGattCharacteristic> {
        let active = self.active.as_ref()?;
        (active.gatt_session_id == gatt_session_id)
            .then(|| active.characteristics.get(&characteristic_id))
            .flatten()
            .map(|characteristic| &characteristic.native)
    }
}

struct NativeGattMap {
    gatt_session_id: GattSessionId,
    services: HashMap<GattServiceId, GattDeviceService>,
    characteristics: HashMap<GattCharacteristicId, RetainedGattCharacteristic>,
    descriptors: HashMap<GattDescriptorId, NativeGattDescriptor>,
}

#[derive(Clone)]
struct RetainedGattCharacteristic {
    properties: CharacteristicProperties,
    native: WinrtGattCharacteristic,
}

fn reserve_then_start<R, O, E>(
    reserve: impl FnOnce() -> Result<R, CommandDispatchError>,
    start: impl FnOnce(&R) -> Result<O, E>,
) -> Result<(R, Result<O, E>), CommandDispatchError> {
    let reservation = reserve()?;
    let operation = start(&reservation);
    Ok((reservation, operation))
}

fn ensure_communication_success_with_protocol(
    status: GattCommunicationStatus,
    protocol_error: Option<u8>,
    operation: &'static str,
) -> Result<(), BleEventError> {
    if status == GattCommunicationStatus::Success {
        return Ok(());
    }
    let category = match status {
        GattCommunicationStatus::AccessDenied => BleErrorCategory::Permission,
        GattCommunicationStatus::ProtocolError => BleErrorCategory::Protocol,
        GattCommunicationStatus::Unreachable => BleErrorCategory::Connection,
        _ => BleErrorCategory::Gatt,
    };
    let protocol_detail = protocol_error.map(|value| format!("ATT protocol error 0x{value:02X}"));
    let message = protocol_detail.as_ref().map_or_else(
        || format!("{operation} failed with {status:?}"),
        |detail| format!("{operation} failed with {status:?} ({detail})"),
    );
    Err(BleEventError {
        category,
        message,
        platform_details: Some(PlatformErrorDetails {
            platform: Platform::Windows,
            code: Some(status.0.to_string()),
            name: Some(format!("{status:?}")),
            message: protocol_detail,
        }),
    })
}

pub(super) fn ensure_gatt_cleanup_idle(
    gatt: &ConnectionGattState,
) -> Result<(), CommandDispatchError> {
    if gatt.cleanup_in_progress {
        return Err(CommandDispatchError::BackendUnavailable {
            detail: "Windows GATT cleanup is in progress for this connection".to_owned(),
            platform_details: None,
        });
    }
    Ok(())
}

pub(super) fn connected_gatt_mut(
    state: &mut BackendState,
    connection: ConnectionCorrelation,
) -> Option<&mut ConnectionGattState> {
    if state.lifecycle != Lifecycle::Ready || !state.connection_is_active(connection) {
        return None;
    }
    let context = state.connections.get_mut(&connection.generation)?;
    if context.correlation != connection {
        return None;
    }
    match &mut context.phase {
        ConnectionPhase::Connected { gatt, .. } => Some(gatt.as_mut()),
        _ => None,
    }
}

fn connected_gatt(
    state: &BackendState,
    connection: ConnectionCorrelation,
) -> Option<&ConnectionGattState> {
    if state.lifecycle != Lifecycle::Ready || !state.connection_is_active(connection) {
        return None;
    }
    let context = state.connections.get(&connection.generation)?;
    if context.correlation != connection {
        return None;
    }
    match &context.phase {
        ConnectionPhase::Connected { gatt, .. } => Some(gatt.as_ref()),
        _ => None,
    }
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
struct DeferredCleanupCallbacks {
    services_changed: bool,
    disconnected: bool,
}

fn finish_cleanup_suppression(
    state: &std::sync::Arc<std::sync::Mutex<BackendState>>,
    connection: ConnectionCorrelation,
) -> DeferredCleanupCallbacks {
    let mut current = lock_state(state);
    connected_gatt_mut(&mut current, connection)
        .map(ConnectionGattState::finish_cleanup)
        .unwrap_or_default()
}

fn drain_deferred_cleanup(
    state: &std::sync::Arc<std::sync::Mutex<BackendState>>,
    connection: ConnectionCorrelation,
    deferred: DeferredCleanupCallbacks,
) {
    if deferred.disconnected {
        drain_deferred_connection_disconnect(state, connection);
    }
    if deferred.services_changed {
        handle_services_changed(state, connection);
    }
}

pub(super) fn pending_connection_failure_event(
    gatt: &ConnectionGattState,
    message: &'static str,
) -> Option<BleEvent> {
    let pending = gatt.pending.as_ref()?;
    Some(discovery_failure_event(
        pending.correlation.command,
        simple_event_error(BleErrorCategory::Connection, message),
    ))
}

pub(super) fn connection_teardown_events(
    gatt: Option<&ConnectionGattState>,
    pending_message: &'static str,
    disconnected: BleEvent,
    disconnect_terminal: Option<BleEvent>,
) -> Vec<BleEvent> {
    let mut events = Vec::new();
    if let Some(gatt) = gatt {
        events.extend(io_failure_events(
            gatt.io_queue
                .iter()
                .map(|queued| queued.correlation.command.clone()),
            BleErrorCategory::Connection,
            "connection was disconnected while GATT I/O was active or queued",
        ));
        if let Some(event) = pending_connection_failure_event(gatt, pending_message) {
            events.push(event);
        }
    }
    events.push(disconnected);
    if let Some(terminal) = disconnect_terminal {
        events.push(terminal);
    }
    events
}

pub(super) fn defer_connection_disconnect_if_cleaning_locked(
    gatt: &mut ConnectionGattState,
) -> bool {
    if gatt.cleanup_in_progress {
        gatt.disconnect_pending = true;
        true
    } else {
        false
    }
}

pub(super) fn defer_connection_disconnect_if_cleaning(
    state: &std::sync::Arc<std::sync::Mutex<BackendState>>,
    connection: ConnectionCorrelation,
) -> bool {
    let mut current = lock_state(state);
    connected_gatt_mut(&mut current, connection)
        .is_some_and(defer_connection_disconnect_if_cleaning_locked)
}

fn services_changed_should_process(
    state: &mut BackendState,
    connection: ConnectionCorrelation,
) -> bool {
    connected_gatt_mut(state, connection).is_some_and(|gatt| {
        let discovery_transition_in_progress = gatt
            .pending
            .as_ref()
            .is_some_and(|pending| matches!(pending.operation, PendingOperationState::Reserved));
        let native_transition_in_progress =
            discovery_transition_in_progress || gatt.io_transition_in_progress;
        if gatt.cleanup_in_progress || native_transition_in_progress {
            gatt.services_changed_pending = true;
            false
        } else {
            gatt.pending.is_some() || !gatt.io_queue.is_empty() || gatt.active.is_some()
        }
    })
}

fn drain_deferred_services_changed(
    state: &std::sync::Arc<std::sync::Mutex<BackendState>>,
    connection: ConnectionCorrelation,
) {
    let should_process = {
        let mut current = lock_state(state);
        connected_gatt_mut(&mut current, connection)
            .is_some_and(|gatt| std::mem::take(&mut gatt.services_changed_pending))
    };
    if should_process {
        handle_services_changed(state, connection);
    }
}

pub(super) fn handle_services_changed(
    state: &std::sync::Arc<std::sync::Mutex<BackendState>>,
    connection: ConnectionCorrelation,
) {
    {
        let mut current = lock_state(state);
        if !services_changed_should_process(&mut current, connection) {
            return;
        }
    }

    let delivery = delivery_for_state(state);
    let _delivery_guard = lock_delivery(&delivery);
    let (invalidated, ingress, events) = {
        let mut current = lock_state(state);
        if !services_changed_should_process(&mut current, connection) {
            return;
        }
        let Some(gatt) = connected_gatt_mut(&mut current, connection) else {
            return;
        };
        gatt.cache_revision = gatt.cache_revision.saturating_add(1);
        let pending = gatt.pending.take();
        let io_queue = std::mem::take(&mut gatt.io_queue);
        gatt.io_driver_running = false;
        gatt.io_transition_in_progress = false;
        let subscriptions = take_subscriptions(gatt);
        let active = gatt.active.take();
        let mut events = io_failure_events(
            io_queue
                .iter()
                .map(|queued| queued.correlation.command.clone()),
            BleErrorCategory::Gatt,
            "GATT services changed while characteristic I/O was active or queued",
        );
        if let Some(pending) = &pending {
            events.push(discovery_failure_event(
                pending.correlation.command,
                simple_event_error(
                    BleErrorCategory::Gatt,
                    "GATT services changed while discovery was pending",
                ),
            ));
        }
        gatt.cleanup_in_progress = true;
        (
            InvalidatedGatt {
                pending,
                io_queue,
                subscriptions,
                active,
            },
            current.ingress(),
            events,
        )
    };
    drop(_delivery_guard);
    cleanup_invalidated(invalidated, None);
    let _delivery_guard = lock_delivery(&delivery);
    if let Some(ingress) = ingress {
        push_events(&ingress, events, "fail invalidated Windows GATT operations");
    }
    let deferred = finish_cleanup_suppression(state, connection);
    drop(_delivery_guard);
    drain_deferred_cleanup(state, connection, deferred);
}

struct InvalidatedGatt {
    pending: Option<PendingDiscovery>,
    io_queue: VecDeque<QueuedIo>,
    subscriptions: Vec<SubscriptionRoute>,
    active: Option<NativeGattMap>,
}

fn cleanup_invalidated(
    invalidated: InvalidatedGatt,
    mut first_error: Option<&mut Option<blehub::BleHubError>>,
) {
    if let Some(pending) = invalidated.pending {
        cleanup_pending(pending, first_error.as_deref_mut());
    }
    cleanup_io_queue(invalidated.io_queue, first_error.as_deref_mut());
    cleanup_subscription_routes(invalidated.subscriptions, first_error.as_deref_mut());
    if let Some(active) = invalidated.active {
        cleanup_native_map(active, first_error);
    }
}

pub(super) fn cancel_pending_operation(
    gatt: &ConnectionGattState,
    first_error: &mut Option<&mut Option<blehub::BleHubError>>,
) {
    if let Some(pending) = &gatt.pending {
        record_connection_cleanup(
            pending.operation.cancel(),
            "cancel pending GATT discovery",
            first_error,
        );
    }
    if let Some(queued) = gatt.io_queue.front() {
        record_connection_cleanup(
            queued.operation.cancel(),
            "cancel active GATT I/O",
            first_error,
        );
    }
}

pub(super) fn cleanup_connection_gatt(
    mut gatt: ConnectionGattState,
    mut first_error: Option<&mut Option<blehub::BleHubError>>,
) {
    if let Some(pending) = gatt.pending.take() {
        cleanup_draft(pending.draft, first_error.as_deref_mut());
    }
    let subscriptions = take_subscriptions(&mut gatt);
    drop(gatt.io_queue);
    cleanup_subscription_routes(subscriptions, first_error.as_deref_mut());
    if let Some(active) = gatt.active.take() {
        cleanup_native_map(active, first_error);
    }
}

fn cleanup_native_map(
    active: NativeGattMap,
    first_error: Option<&mut Option<blehub::BleHubError>>,
) {
    let NativeGattMap {
        gatt_session_id: _,
        services,
        characteristics,
        descriptors,
    } = active;
    drop(descriptors);
    drop(characteristics);
    close_service_objects(services.into_values().collect(), first_error);
}

fn close_service_objects(
    services: Vec<GattDeviceService>,
    mut first_error: Option<&mut Option<blehub::BleHubError>>,
) {
    for service in services {
        record_connection_cleanup(
            service.Close(),
            "close retained GATT service",
            &mut first_error,
        );
    }
}

fn log_cleanup_failure(result: windows::core::Result<()>, operation: &'static str) {
    if let Err(error) = result {
        tracing::warn!(
            target: "blehub_windows::gatt",
            operation,
            detail = %error,
            "Windows GATT cleanup failed"
        );
    }
}
