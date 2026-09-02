use std::{
    collections::HashMap,
    sync::{
        Arc, Mutex, MutexGuard,
        atomic::{AtomicBool, Ordering},
    },
};

use blehub::{
    AdapterSnapshot, AdapterState, BackendCapabilities, BackendEventIngressHandle, BleBackend,
    BleEvent, BleHubError, CommandDispatchError, CommandId, ConnectCommand, ConnectionId,
    DisconnectCommand, DiscoverGattCommand, EventIngressStatus, InitializeCommand, PeripheralId,
    Platform, ReadCharacteristicCommand, ReadRssiCommand, SetNotificationsCommand,
    StartScanCommand, StopScanCommand, WriteCharacteristicCommand,
};
use windows::Devices::{Bluetooth::BluetoothAdapter, Radios::Radio};
use windows_future::IAsyncOperation;

#[cfg(feature = "hil-callback-gates")]
use crate::callback_gates::{CallbackGateBarrier, WindowsHilCallback, WindowsHilCallbackGate};
use crate::{
    connection::{self, ConnectionContext, ConnectionCorrelation},
    error::cleanup_error,
    gatt,
    initialization::{self, InitializationCallbackBarrier, RetiredInitializationOperation},
    resource_accounting::{CallbackResource, CallbackResourceGuard, ResourceLedger},
    scan,
};

static WINDOWS_BACKEND_LEASED: AtomicBool = AtomicBool::new(false);

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Lifecycle {
    Inert,
    Initializing { command_id: CommandId },
    Ready,
    Faulted,
    Closed,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
struct PeripheralKey {
    address: u64,
    address_type: i32,
}

pub(crate) struct BackendState {
    delivery: Arc<Mutex<()>>,
    resource_ledger: ResourceLedger,
    #[cfg(feature = "hil-callback-gates")]
    pub(crate) hil_callback_gates: Arc<CallbackGateBarrier>,
    pub(crate) lifecycle: Lifecycle,
    ingress: Option<Arc<BackendEventIngressHandle>>,
    pub(crate) adapter_operation: Option<IAsyncOperation<BluetoothAdapter>>,
    pub(crate) radio_operation: Option<IAsyncOperation<Radio>>,
    pub(crate) retired_initialization_operations: Vec<RetiredInitializationOperation>,
    pub(crate) adapter: Option<BluetoothAdapter>,
    pub(crate) radio: Option<Radio>,
    pub(crate) radio_state_token: Option<i64>,
    pub(crate) adapter_snapshot: Option<AdapterSnapshot>,
    pub(crate) scan: Option<scan::ScanState>,
    pub(crate) last_stopped_scan: Option<blehub::ScanSessionId>,
    peripheral_ids: HashMap<PeripheralKey, PeripheralId>,
    peripheral_keys: HashMap<PeripheralId, PeripheralKey>,
    pub(crate) connections: HashMap<u64, ConnectionContext>,
    pub(crate) active_connections: HashMap<ConnectionId, u64>,
    peripheral_connections: HashMap<PeripheralId, u64>,
    retiring_connection_ids: HashMap<ConnectionId, u64>,
    retiring_peripherals: HashMap<PeripheralId, u64>,
    next_peripheral_id: u64,
    pub(crate) next_scan_generation: u64,
    next_connection_generation: u64,
    pub(crate) gatt_discovery_generations: gatt::MonotonicIdAllocator,
    pub(crate) gatt_io_generations: gatt::MonotonicIdAllocator,
    pub(crate) gatt_attribute_ids: gatt::MonotonicIdAllocator,
    owns_lease: bool,
}

impl BackendState {
    fn new(delivery: Arc<Mutex<()>>, resource_ledger: ResourceLedger) -> Self {
        Self {
            delivery,
            resource_ledger,
            #[cfg(feature = "hil-callback-gates")]
            hil_callback_gates: Arc::new(CallbackGateBarrier::new()),
            lifecycle: Lifecycle::Inert,
            ingress: None,
            adapter_operation: None,
            radio_operation: None,
            retired_initialization_operations: Vec::new(),
            adapter: None,
            radio: None,
            radio_state_token: None,
            adapter_snapshot: None,
            scan: None,
            last_stopped_scan: None,
            peripheral_ids: HashMap::new(),
            peripheral_keys: HashMap::new(),
            connections: HashMap::new(),
            active_connections: HashMap::new(),
            peripheral_connections: HashMap::new(),
            retiring_connection_ids: HashMap::new(),
            retiring_peripherals: HashMap::new(),
            next_peripheral_id: 1,
            next_scan_generation: 1,
            next_connection_generation: 1,
            gatt_discovery_generations: gatt::MonotonicIdAllocator::new(),
            gatt_io_generations: gatt::MonotonicIdAllocator::new(),
            gatt_attribute_ids: gatt::MonotonicIdAllocator::new(),
            owns_lease: true,
        }
    }

    pub(crate) fn ingress(&self) -> Option<Arc<BackendEventIngressHandle>> {
        self.ingress.clone()
    }

    #[cfg(test)]
    pub(crate) fn new_without_process_lease_for_test(delivery: Arc<Mutex<()>>) -> Self {
        let mut state = Self::new(delivery, ResourceLedger::default());
        state.owns_lease = false;
        state
    }

    fn allocate_peripheral_id(&mut self, key: PeripheralKey) -> Option<PeripheralId> {
        if let Some(id) = self.peripheral_ids.get(&key) {
            return Some(*id);
        }
        let value = self.next_peripheral_id;
        self.next_peripheral_id = self.next_peripheral_id.checked_add(1)?;
        let id = PeripheralId { value };
        self.peripheral_ids.insert(key, id);
        self.peripheral_keys.insert(id, key);
        Some(id)
    }

    pub(crate) fn allocate_scan_peripheral_id(
        &mut self,
        address: u64,
        address_type: i32,
    ) -> Option<PeripheralId> {
        self.allocate_peripheral_id(PeripheralKey {
            address,
            address_type,
        })
    }

    pub(crate) fn connection_peripheral_key(
        &self,
        peripheral_id: PeripheralId,
    ) -> Option<(u64, i32)> {
        self.peripheral_keys
            .get(&peripheral_id)
            .map(|key| (key.address, key.address_type))
    }

    pub(crate) fn allocate_connection_generation(&mut self) -> Option<u64> {
        let generation = self.next_connection_generation;
        self.next_connection_generation = generation.checked_add(1)?;
        Some(generation)
    }

    pub(crate) fn insert_connection_indexes(&mut self, correlation: ConnectionCorrelation) {
        self.active_connections
            .insert(correlation.connection_id, correlation.generation);
        self.peripheral_connections
            .insert(correlation.peripheral_id, correlation.generation);
    }

    pub(crate) fn peripheral_connection_is_active_or_retiring(
        &self,
        peripheral_id: PeripheralId,
    ) -> bool {
        self.peripheral_connections.contains_key(&peripheral_id)
            || self.retiring_peripherals.contains_key(&peripheral_id)
    }

    pub(crate) fn connection_id_is_retiring(&self, connection_id: ConnectionId) -> bool {
        self.retiring_connection_ids.contains_key(&connection_id)
    }

    pub(crate) fn connection_is_active(&self, correlation: ConnectionCorrelation) -> bool {
        self.active_connections.get(&correlation.connection_id) == Some(&correlation.generation)
            && self.peripheral_connections.get(&correlation.peripheral_id)
                == Some(&correlation.generation)
    }

    pub(crate) fn remove_connection_indexes(&mut self, correlation: ConnectionCorrelation) {
        if self.active_connections.get(&correlation.connection_id) == Some(&correlation.generation)
        {
            self.active_connections.remove(&correlation.connection_id);
        }
        if self.peripheral_connections.get(&correlation.peripheral_id)
            == Some(&correlation.generation)
        {
            self.peripheral_connections
                .remove(&correlation.peripheral_id);
        }
    }

    pub(crate) fn remove_connection(&mut self, generation: u64) -> Option<ConnectionContext> {
        let connection = self.connections.remove(&generation)?;
        self.remove_connection_indexes(connection.correlation);
        Some(connection)
    }

    pub(crate) fn begin_connection_cleanup(&mut self, correlation: ConnectionCorrelation) {
        self.retiring_connection_ids
            .entry(correlation.connection_id)
            .or_insert(correlation.generation);
        self.retiring_peripherals
            .entry(correlation.peripheral_id)
            .or_insert(correlation.generation);
    }

    pub(crate) fn finish_connection_cleanup(&mut self, correlation: ConnectionCorrelation) {
        Self::release_cleanup_reservation(
            &mut self.retiring_connection_ids,
            correlation.connection_id,
            correlation.generation,
        );
        Self::release_cleanup_reservation(
            &mut self.retiring_peripherals,
            correlation.peripheral_id,
            correlation.generation,
        );
    }

    fn release_cleanup_reservation<K: Eq + std::hash::Hash + Copy>(
        reservations: &mut HashMap<K, u64>,
        key: K,
        generation: u64,
    ) {
        if reservations.get(&key) == Some(&generation) {
            reservations.remove(&key);
        }
    }
}

pub struct WindowsBackend {
    pub(crate) state: Arc<Mutex<BackendState>>,
    pub(crate) delivery: Arc<Mutex<()>>,
    pub(crate) initialization_setup: Mutex<()>,
    pub(crate) initialization_callbacks: Arc<InitializationCallbackBarrier>,
}

impl WindowsBackend {
    pub fn new() -> Result<Self, BleHubError> {
        Self::new_with_resource_ledger(ResourceLedger::default())
    }

    #[cfg(feature = "resource-accounting")]
    #[doc(hidden)]
    pub fn new_accounted() -> Result<(Self, crate::WindowsResourceProbe), BleHubError> {
        let resource_ledger = ResourceLedger::default();
        let probe = resource_ledger.probe();
        Self::new_with_resource_ledger(resource_ledger).map(|backend| (backend, probe))
    }

    fn new_with_resource_ledger(resource_ledger: ResourceLedger) -> Result<Self, BleHubError> {
        WINDOWS_BACKEND_LEASED
            .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .map_err(|_| BleHubError::ResourceBusy {
                detail: "a Windows backend already owns the process Bluetooth adapter".to_owned(),
            })?;
        let delivery = Arc::new(Mutex::new(()));
        Ok(Self {
            state: Arc::new(Mutex::new(BackendState::new(
                Arc::clone(&delivery),
                resource_ledger,
            ))),
            delivery,
            initialization_setup: Mutex::new(()),
            initialization_callbacks: Arc::new(InitializationCallbackBarrier::new()),
        })
    }

    #[cfg(feature = "hil-callback-gates")]
    #[doc(hidden)]
    pub fn arm_hil_callback_gate(&self, callback: WindowsHilCallback) -> WindowsHilCallbackGate {
        hil_callback_gates_for_state(&self.state).arm(callback)
    }

    #[cfg(feature = "resource-accounting")]
    #[doc(hidden)]
    pub fn resource_snapshot(&self) -> crate::WindowsLogicalResourceSnapshot {
        let state = lock_state(&self.state);
        crate::WindowsLogicalResourceSnapshot {
            ingress_roots: usize::from(state.ingress.is_some()),
            pending_initialization_operations: usize::from(state.adapter_operation.is_some())
                + usize::from(state.radio_operation.is_some())
                + state.retired_initialization_operations.len(),
            adapter_roots: usize::from(state.adapter.is_some()),
            radio_roots: usize::from(state.radio.is_some()),
            radio_event_tokens: usize::from(state.radio_state_token.is_some()),
            scan_roots: usize::from(state.scan.is_some()),
            connection_roots: state.connections.len(),
            owns_adapter_lease: state.owns_lease,
        }
    }
}

impl BleBackend for WindowsBackend {
    fn capabilities(&self) -> Result<BackendCapabilities, BleHubError> {
        Ok(BackendCapabilities {
            supports_duplicate_scan_results: true,
            supports_connectable_flag: true,
            supports_read_rssi: false,
            supports_required_notification: true,
            supports_required_indication: true,
            reports_actual_notification_mode: true,
            ..BackendCapabilities::new(Platform::Windows)
        })
    }

    fn bind_event_ingress(
        &self,
        ingress: Arc<BackendEventIngressHandle>,
    ) -> Result<(), BleHubError> {
        let mut state = lock_state(&self.state);
        if state.lifecycle != Lifecycle::Inert || state.ingress.is_some() {
            return Err(BleHubError::BackendAssembly {
                detail: "Windows event ingress must be bound exactly once while inert".to_owned(),
            });
        }
        state.ingress = Some(ingress);
        Ok(())
    }

    fn initialize(&self, command: InitializeCommand) -> Result<(), CommandDispatchError> {
        initialization::begin(self, command)
    }

    fn start_scan(&self, command: StartScanCommand) -> Result<(), CommandDispatchError> {
        scan::begin(self, command)
    }

    fn stop_scan(&self, command: StopScanCommand) -> Result<(), CommandDispatchError> {
        scan::stop(self, command)
    }

    fn connect(&self, command: ConnectCommand) -> Result<(), CommandDispatchError> {
        connection::begin_connect(self, command)
    }

    fn disconnect(&self, command: DisconnectCommand) -> Result<(), CommandDispatchError> {
        connection::begin_disconnect(self, command)
    }

    fn discover_gatt(&self, command: DiscoverGattCommand) -> Result<(), CommandDispatchError> {
        gatt::begin_discovery(self, command)
    }

    fn read_characteristic(
        &self,
        command: ReadCharacteristicCommand,
    ) -> Result<(), CommandDispatchError> {
        gatt::begin_read(self, command)
    }

    fn write_characteristic(
        &self,
        command: WriteCharacteristicCommand,
    ) -> Result<(), CommandDispatchError> {
        gatt::begin_write(self, command)
    }

    fn set_notifications(
        &self,
        command: SetNotificationsCommand,
    ) -> Result<(), CommandDispatchError> {
        gatt::begin_set_notifications(self, command)
    }

    fn read_rssi(&self, command: ReadRssiCommand) -> Result<(), CommandDispatchError> {
        connection::complete_unsupported_read_rssi(self, command)
    }

    fn dispose(&self) -> Result<(), BleHubError> {
        #[cfg(feature = "hil-callback-gates")]
        hil_callback_gates_for_state(&self.state).close();
        let _setup_guard = self
            .initialization_setup
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        self.initialization_callbacks.close_and_wait();
        let delivery_guard = lock_delivery(&self.delivery);
        let (
            adapter_operation,
            radio_operation,
            retired_initialization_operations,
            adapter,
            radio,
            radio_token,
            scan,
            connections,
            ingress,
            release_lease,
        ) = {
            let mut state = lock_state(&self.state);
            if state.lifecycle == Lifecycle::Closed {
                return Ok(());
            }
            state.lifecycle = Lifecycle::Closed;
            state.peripheral_ids.clear();
            state.peripheral_keys.clear();
            state.active_connections.clear();
            state.peripheral_connections.clear();
            state.retiring_connection_ids.clear();
            state.retiring_peripherals.clear();
            let connections = std::mem::take(&mut state.connections);
            let release_lease = std::mem::take(&mut state.owns_lease);
            (
                state.adapter_operation.take(),
                state.radio_operation.take(),
                std::mem::take(&mut state.retired_initialization_operations),
                state.adapter.take(),
                state.radio.take(),
                state.radio_state_token.take(),
                state.scan.take(),
                connections,
                state.ingress.take(),
                release_lease,
            )
        };
        drop(delivery_guard);

        drop(ingress);
        let mut first_error = None;
        if let Some(operation) = adapter_operation
            && let Err(error) = operation.Cancel()
        {
            first_error.get_or_insert_with(|| cleanup_error("cancel adapter lookup", error));
        }
        if let Some(operation) = radio_operation
            && let Err(error) = operation.Cancel()
        {
            first_error.get_or_insert_with(|| cleanup_error("cancel radio lookup", error));
        }
        for operation in retired_initialization_operations {
            if let Err(error) = operation.cancel() {
                first_error.get_or_insert_with(|| cleanup_error(operation.cleanup_name(), error));
            }
        }
        if let Some(scan) = scan {
            scan::cleanup_for_dispose(scan, &mut first_error);
        }
        if let (Some(radio), Some(token)) = (radio, radio_token)
            && let Err(error) = radio.RemoveStateChanged(token)
        {
            first_error.get_or_insert_with(|| cleanup_error("remove radio-state callback", error));
        }
        for (_, connection) in connections {
            connection::cleanup_connection_context(connection, Some(&mut first_error));
        }

        drop(adapter);
        if release_lease {
            WINDOWS_BACKEND_LEASED.store(false, Ordering::Release);
        }
        first_error.map_or(Ok(()), Err)
    }
}

impl Drop for WindowsBackend {
    fn drop(&mut self) {
        let _ = <Self as BleBackend>::dispose(self);
    }
}

pub(crate) fn lock_state(state: &Arc<Mutex<BackendState>>) -> MutexGuard<'_, BackendState> {
    state.lock().unwrap_or_else(|error| {
        tracing::error!(
            target: "blehub_windows::lifecycle",
            detail = %error,
            "recovered poisoned Windows backend state"
        );
        error.into_inner()
    })
}

pub(crate) fn lock_delivery(delivery: &Arc<Mutex<()>>) -> MutexGuard<'_, ()> {
    delivery.lock().unwrap_or_else(|error| {
        tracing::error!(
            target: "blehub_windows::delivery",
            detail = %error,
            "recovered poisoned Windows event-delivery gate"
        );
        error.into_inner()
    })
}

pub(crate) fn delivery_for_state(state: &Arc<Mutex<BackendState>>) -> Arc<Mutex<()>> {
    Arc::clone(&lock_state(state).delivery)
}

#[cfg(feature = "hil-callback-gates")]
pub(crate) fn hil_callback_gates_for_state(
    state: &Arc<Mutex<BackendState>>,
) -> Arc<CallbackGateBarrier> {
    Arc::clone(&lock_state(state).hil_callback_gates)
}

pub(crate) fn track_callback(
    state: &Arc<Mutex<BackendState>>,
    resource: CallbackResource,
) -> CallbackResourceGuard {
    let ledger = lock_state(state).resource_ledger.clone();
    ledger.track(resource)
}

pub(crate) fn ensure_ingress_bound(state: &BackendState) -> Result<(), CommandDispatchError> {
    if state.lifecycle == Lifecycle::Closed {
        return Err(CommandDispatchError::Closed);
    }
    if state.ingress.is_none() {
        return Err(CommandDispatchError::InvalidState {
            detail: "Windows event ingress is not bound".to_owned(),
        });
    }
    Ok(())
}

pub(crate) fn ensure_ready(state: &BackendState) -> Result<(), CommandDispatchError> {
    ensure_ingress_bound(state)?;
    if state.lifecycle != Lifecycle::Ready {
        return Err(CommandDispatchError::InvalidState {
            detail: "Windows backend is not initialized".to_owned(),
        });
    }
    match state
        .adapter_snapshot
        .as_ref()
        .map(|snapshot| snapshot.state)
    {
        Some(AdapterState::PoweredOn) => Ok(()),
        Some(AdapterState::PoweredOff) => Err(CommandDispatchError::InvalidState {
            detail: "Windows Bluetooth radio is powered off".to_owned(),
        }),
        Some(AdapterState::Unavailable | AdapterState::Unknown) | None => {
            Err(CommandDispatchError::BackendUnavailable {
                detail: "Windows Bluetooth LE Central adapter is unavailable".to_owned(),
                platform_details: None,
            })
        }
    }
}

pub(crate) fn record_connection_cleanup(
    result: windows::core::Result<()>,
    operation: &'static str,
    first_error: &mut Option<&mut Option<BleHubError>>,
) {
    let Err(error) = result else {
        return;
    };
    if let Some(first_error) = first_error.as_mut() {
        (**first_error).get_or_insert_with(|| cleanup_error(operation, error));
    } else {
        tracing::warn!(
            target: "blehub_windows::lifecycle",
            operation,
            detail = %error,
            "Windows connection cleanup failed"
        );
    }
}

pub(crate) fn log_cleanup_failure(result: windows::core::Result<()>, operation: &'static str) {
    if let Err(error) = result {
        tracing::warn!(
            target: "blehub_windows::lifecycle",
            operation,
            detail = %error,
            "Windows best-effort callback cleanup failed"
        );
    }
}

pub(crate) fn push_events(
    ingress: &BackendEventIngressHandle,
    events: impl IntoIterator<Item = BleEvent>,
    operation: &'static str,
) {
    for event in events {
        match ingress.push(event) {
            EventIngressStatus::Delivered => {}
            EventIngressStatus::Closed => {
                tracing::debug!(target: "blehub_windows::ingress", operation, "event ingress is closed");
                break;
            }
            EventIngressStatus::InvalidEvent { message }
            | EventIngressStatus::DownstreamFailed { message } => {
                tracing::warn!(
                    target: "blehub_windows::ingress",
                    operation,
                    detail = %message,
                    "Windows event delivery was not accepted"
                );
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use blehub::{BleEventSink, BleHub, EventSinkError};
    use windows::Devices::Bluetooth::BluetoothAddressType;

    struct AcceptingSink;

    impl BleEventSink for AcceptingSink {
        fn on_event(&self, _: BleEvent) -> Result<(), EventSinkError> {
            Ok(())
        }
    }

    #[test]
    fn peripheral_identity_allocation_is_stable_and_bidirectional() {
        let mut state = BackendState::new(Arc::new(Mutex::new(())), ResourceLedger::default());
        let key = PeripheralKey {
            address: 0x1122_3344_5566,
            address_type: BluetoothAddressType::Random.0,
        };

        let first = state.allocate_peripheral_id(key).unwrap();
        let repeated = state.allocate_peripheral_id(key).unwrap();

        assert_eq!(first, repeated);
        assert_eq!(state.peripheral_keys.get(&first), Some(&key));
    }

    #[test]
    fn dispose_releases_process_lease_while_an_external_backend_arc_remains() {
        let backend = Arc::new(WindowsBackend::new().unwrap());
        let retained = Arc::clone(&backend);
        let hub = BleHub::new(backend, Box::new(AcceptingSink)).unwrap();

        hub.dispose().unwrap();
        assert!(WindowsBackend::new().is_ok());
        assert!(matches!(
            retained.initialize(InitializeCommand {
                command_id: CommandId { value: 9 },
            }),
            Err(CommandDispatchError::Closed)
        ));
    }
}
