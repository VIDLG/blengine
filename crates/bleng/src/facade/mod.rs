//! Caller-facing BLeng facade and private-runtime boundary.
//!
//! Typed operation handles live in operations.rs; error types live in error.rs.
//! Dropping a handle retires caller delivery, while cancel() explicitly requests
//! native control under the operation policy and backend's cancellation support.

use std::{future::Future, sync::Arc, time::Duration};

use blehub::{
    AdapterSnapshot, BleBackend, BleEvent, BluetoothUuid, BondState, CommandId, ConnectionId,
    ConnectionPriority, DeviceQueryKind, DeviceRecord, GattCharacteristicId, GattDatabase,
    GattDescriptorId, GattSessionId, MtuInfo, NotificationModeRequest, PeripheralId, ScanOptions,
    ScanSessionId, SubscriptionId, WriteType,
};
use tokio::time::Instant;

use super::{error::BlengError, runtime};

mod operations;

use operations::OperationAdapter;
pub use operations::{
    AssociateCompanionDeviceOperation, BondOperation, ConnectOperation,
    DisableNotificationsOperation, DisconnectOperation, DiscoverGattOperation,
    EnableNotificationsOperation, InitializeOperation, MtuOperation, NotificationSubscription,
    QueryDevicesOperation, ReadDescriptorOperation, ReadOperation, ReadRssiOperation,
    RefreshGattCacheOperation, SetAdapterEnabledOperation, SetConnectionPriorityOperation,
    StartScanOperation, StopScanOperation, WriteDescriptorOperation, WriteOperation,
};

const MAX_TIMEOUT_MS: u64 = 86_400_000;

fn validate_timeout(milliseconds: u64) -> Result<Duration, BlengError> {
    if !(1..=MAX_TIMEOUT_MS).contains(&milliseconds) {
        return Err(BlengError::InvalidTimeout);
    }
    Ok(Duration::from_millis(milliseconds))
}

fn completion_deadline(timeout_ms: u64) -> Result<Instant, BlengError> {
    let timeout = validate_timeout(timeout_ms)?;
    Ok(Instant::now() + timeout)
}

#[derive(uniffi::Object)]
pub struct Bleng {
    // Shared by the UniFFI Bleng object and caller-bound tasks run by the
    // owner. The runtime stays alive until both the foreign object and all
    // in-flight facade waiters release their clones;
    // Tokio/Kameo internals never cross this field's private boundary.
    owner: Arc<runtime::RuntimeOwner>,
}

impl Drop for Bleng {
    fn drop(&mut self) {
        // Handles retain only executor/result access, not an open Hub lifecycle.
        // Explicit shutdown remains the cleanup completion/error barrier.
        self.owner.request_close();
    }
}

impl Bleng {
    // Submission returns the shared private adapter. Only begin_* wraps it in a
    // concrete UniFFI handle; convenience calls retain in-flight close precedence.
    fn submit_initialize(
        &self,
        preserve_completed_result_on_close: bool,
    ) -> Result<OperationAdapter<runtime::BeginInitialize>, BlengError> {
        let pending = self
            .owner
            .submit_operation(preserve_completed_result_on_close, |submission| {
                runtime::BeginInitialize { submission }
            })?;
        Ok(OperationAdapter::new(Arc::clone(&self.owner), pending))
    }

    fn submit_start_scan(
        &self,
        options: ScanOptions,
        preserve_completed_result_on_close: bool,
    ) -> Result<OperationAdapter<runtime::BeginScan>, BlengError> {
        let pending =
            self.owner
                .submit_operation(preserve_completed_result_on_close, |submission| {
                    submission.enable_outcome_observation();
                    runtime::BeginScan {
                        submission,
                        options,
                    }
                })?;
        Ok(OperationAdapter::new(Arc::clone(&self.owner), pending))
    }

    fn submit_stop_scan(
        &self,
        session: ScanSessionId,
        preserve_completed_result_on_close: bool,
    ) -> Result<OperationAdapter<runtime::BeginStop>, BlengError> {
        let pending =
            self.owner
                .submit_operation(preserve_completed_result_on_close, |submission| {
                    runtime::BeginStop {
                        submission,
                        session,
                    }
                })?;
        Ok(OperationAdapter::new(Arc::clone(&self.owner), pending))
    }

    fn submit_connect(
        &self,
        peripheral: PeripheralId,
        auto_connect: bool,
        preserve_completed_result_on_close: bool,
    ) -> Result<OperationAdapter<runtime::BeginConnect>, BlengError> {
        let pending =
            self.owner
                .submit_operation(preserve_completed_result_on_close, |submission| {
                    runtime::BeginConnect {
                        submission,
                        peripheral_id: peripheral,
                        auto_connect,
                    }
                })?;
        Ok(OperationAdapter::new(Arc::clone(&self.owner), pending))
    }

    fn submit_disconnect(
        &self,
        connection: ConnectionId,
        preserve_completed_result_on_close: bool,
    ) -> Result<OperationAdapter<runtime::BeginDisconnect>, BlengError> {
        let pending =
            self.owner
                .submit_operation(preserve_completed_result_on_close, |submission| {
                    runtime::BeginDisconnect {
                        submission,
                        connection,
                    }
                })?;
        Ok(OperationAdapter::new(Arc::clone(&self.owner), pending))
    }

    fn submit_discover_gatt(
        &self,
        connection: ConnectionId,
        preserve_completed_result_on_close: bool,
    ) -> Result<OperationAdapter<runtime::BeginDiscoverGatt>, BlengError> {
        let pending =
            self.owner
                .submit_operation(preserve_completed_result_on_close, |submission| {
                    runtime::BeginDiscoverGatt {
                        submission,
                        connection,
                    }
                })?;
        Ok(OperationAdapter::new(Arc::clone(&self.owner), pending))
    }

    fn submit_read_characteristic(
        &self,
        connection: ConnectionId,
        session: GattSessionId,
        characteristic: GattCharacteristicId,
        preserve_completed_result_on_close: bool,
    ) -> Result<OperationAdapter<runtime::BeginRead>, BlengError> {
        let pending =
            self.owner
                .submit_operation(preserve_completed_result_on_close, |submission| {
                    runtime::BeginRead {
                        submission,
                        connection,
                        session,
                        characteristic,
                    }
                })?;
        Ok(OperationAdapter::new(Arc::clone(&self.owner), pending))
    }

    fn submit_write_characteristic(
        &self,
        connection: ConnectionId,
        session: GattSessionId,
        characteristic: GattCharacteristicId,
        value: Vec<u8>,
        write_type: WriteType,
        preserve_completed_result_on_close: bool,
    ) -> Result<OperationAdapter<runtime::BeginWrite>, BlengError> {
        let pending =
            self.owner
                .submit_operation(preserve_completed_result_on_close, |submission| {
                    runtime::BeginWrite {
                        submission,
                        connection,
                        session,
                        characteristic,
                        value,
                        write_type,
                    }
                })?;
        Ok(OperationAdapter::new(Arc::clone(&self.owner), pending))
    }

    fn submit_enable_notifications(
        &self,
        connection: ConnectionId,
        session: GattSessionId,
        characteristic: GattCharacteristicId,
        mode: NotificationModeRequest,
        preserve_completed_result_on_close: bool,
    ) -> Result<OperationAdapter<runtime::BeginEnableNotifications>, BlengError> {
        let pending =
            self.owner
                .submit_operation(preserve_completed_result_on_close, |submission| {
                    runtime::BeginEnableNotifications {
                        submission,
                        connection,
                        session,
                        characteristic,
                        mode,
                    }
                })?;
        Ok(OperationAdapter::new(Arc::clone(&self.owner), pending))
    }

    fn submit_disable_notifications(
        &self,
        connection: ConnectionId,
        session: GattSessionId,
        subscription: SubscriptionId,
        preserve_completed_result_on_close: bool,
    ) -> Result<OperationAdapter<runtime::BeginDisableNotifications>, BlengError> {
        let pending =
            self.owner
                .submit_operation(preserve_completed_result_on_close, |submission| {
                    runtime::BeginDisableNotifications {
                        submission,
                        connection,
                        session,
                        subscription,
                    }
                })?;
        Ok(OperationAdapter::new(Arc::clone(&self.owner), pending))
    }

    fn submit_read_rssi(
        &self,
        connection: ConnectionId,
        preserve_completed_result_on_close: bool,
    ) -> Result<OperationAdapter<runtime::BeginRssi>, BlengError> {
        let pending =
            self.owner
                .submit_operation(preserve_completed_result_on_close, |submission| {
                    runtime::BeginRssi {
                        submission,
                        connection,
                    }
                })?;
        Ok(OperationAdapter::new(Arc::clone(&self.owner), pending))
    }

    fn submit_mtu(
        &self,
        connection: ConnectionId,
        request: Option<u16>,
        preserve_completed_result_on_close: bool,
    ) -> Result<OperationAdapter<runtime::BeginMtu>, BlengError> {
        let pending =
            self.owner
                .submit_operation(preserve_completed_result_on_close, |submission| {
                    runtime::BeginMtu {
                        submission,
                        connection,
                        request,
                    }
                })?;
        Ok(OperationAdapter::new(Arc::clone(&self.owner), pending))
    }

    fn submit_set_adapter_enabled(
        &self,
        enabled: bool,
        preserve_completed_result_on_close: bool,
    ) -> Result<OperationAdapter<runtime::BeginSetAdapterEnabled>, BlengError> {
        let pending =
            self.owner
                .submit_operation(preserve_completed_result_on_close, |submission| {
                    runtime::BeginSetAdapterEnabled {
                        submission,
                        enabled,
                    }
                })?;
        Ok(OperationAdapter::new(Arc::clone(&self.owner), pending))
    }

    fn submit_associate_companion(
        &self,
        service_uuids: Vec<BluetoothUuid>,
        display_name: Option<String>,
        device_profile: Option<String>,
        notify_on_device_nearby: bool,
        preserve_completed_result_on_close: bool,
    ) -> Result<OperationAdapter<runtime::BeginAssociateCompanion>, BlengError> {
        let pending =
            self.owner
                .submit_operation(preserve_completed_result_on_close, |submission| {
                    runtime::BeginAssociateCompanion {
                        submission,
                        service_uuids,
                        display_name,
                        device_profile,
                        notify_on_device_nearby,
                    }
                })?;
        Ok(OperationAdapter::new(Arc::clone(&self.owner), pending))
    }

    fn submit_query_devices(
        &self,
        kind: DeviceQueryKind,
        service_uuids: Vec<BluetoothUuid>,
        preserve_completed_result_on_close: bool,
    ) -> Result<OperationAdapter<runtime::BeginQueryDevices>, BlengError> {
        let pending =
            self.owner
                .submit_operation(preserve_completed_result_on_close, |submission| {
                    runtime::BeginQueryDevices {
                        submission,
                        kind,
                        service_uuids,
                    }
                })?;
        Ok(OperationAdapter::new(Arc::clone(&self.owner), pending))
    }

    fn submit_set_connection_priority(
        &self,
        connection: ConnectionId,
        priority: ConnectionPriority,
        preserve_completed_result_on_close: bool,
    ) -> Result<OperationAdapter<runtime::BeginSetConnectionPriority>, BlengError> {
        let pending =
            self.owner
                .submit_operation(preserve_completed_result_on_close, |submission| {
                    runtime::BeginSetConnectionPriority {
                        submission,
                        connection,
                        priority,
                    }
                })?;
        Ok(OperationAdapter::new(Arc::clone(&self.owner), pending))
    }

    fn submit_refresh_gatt_cache(
        &self,
        connection: ConnectionId,
        preserve_completed_result_on_close: bool,
    ) -> Result<OperationAdapter<runtime::BeginRefreshGattCache>, BlengError> {
        let pending =
            self.owner
                .submit_operation(preserve_completed_result_on_close, |submission| {
                    runtime::BeginRefreshGattCache {
                        submission,
                        connection,
                    }
                })?;
        Ok(OperationAdapter::new(Arc::clone(&self.owner), pending))
    }

    fn submit_create_bond(
        &self,
        connection: ConnectionId,
        pin: Option<String>,
        preserve_completed_result_on_close: bool,
    ) -> Result<OperationAdapter<runtime::BeginCreateBond>, BlengError> {
        let pending =
            self.owner
                .submit_operation(preserve_completed_result_on_close, |submission| {
                    runtime::BeginCreateBond {
                        submission,
                        connection,
                        pin,
                    }
                })?;
        Ok(OperationAdapter::new(Arc::clone(&self.owner), pending))
    }

    fn submit_remove_bond(
        &self,
        connection: ConnectionId,
        preserve_completed_result_on_close: bool,
    ) -> Result<OperationAdapter<runtime::BeginRemoveBond>, BlengError> {
        let pending =
            self.owner
                .submit_operation(preserve_completed_result_on_close, |submission| {
                    runtime::BeginRemoveBond {
                        submission,
                        connection,
                    }
                })?;
        Ok(OperationAdapter::new(Arc::clone(&self.owner), pending))
    }

    fn submit_read_descriptor(
        &self,
        connection: ConnectionId,
        session: GattSessionId,
        characteristic: GattCharacteristicId,
        descriptor: GattDescriptorId,
        preserve_completed_result_on_close: bool,
    ) -> Result<OperationAdapter<runtime::BeginReadDescriptor>, BlengError> {
        let pending =
            self.owner
                .submit_operation(preserve_completed_result_on_close, |submission| {
                    runtime::BeginReadDescriptor {
                        submission,
                        connection,
                        session,
                        characteristic,
                        descriptor,
                    }
                })?;
        Ok(OperationAdapter::new(Arc::clone(&self.owner), pending))
    }

    fn submit_write_descriptor(
        &self,
        connection: ConnectionId,
        session: GattSessionId,
        characteristic: GattCharacteristicId,
        descriptor: GattDescriptorId,
        value: Vec<u8>,
        preserve_completed_result_on_close: bool,
    ) -> Result<OperationAdapter<runtime::BeginWriteDescriptor>, BlengError> {
        let pending =
            self.owner
                .submit_operation(preserve_completed_result_on_close, |submission| {
                    runtime::BeginWriteDescriptor {
                        submission,
                        connection,
                        session,
                        characteristic,
                        descriptor,
                        value,
                    }
                })?;
        Ok(OperationAdapter::new(Arc::clone(&self.owner), pending))
    }

    /// Runs one bounded observation on this owner's single runtime.
    ///
    /// Lanes that are not operations (a state stream with no command behind it) still need the
    /// owner's executor and its timer: a foreign caller may have neither. Dropping the returned
    /// future retires that observation only — it never cancels BLE work, and it never shortens
    /// another reader's wait.
    pub async fn observe_with_timeout<T: Send + 'static>(
        &self,
        timeout_ms: u64,
        observation: impl Future<Output = T> + Send + 'static,
    ) -> Result<T, BlengError> {
        let timeout = validate_timeout(timeout_ms)?;
        self.owner
            .run_task(async move {
                tokio::time::timeout(timeout, observation)
                    .await
                    .map_err(|_| BlengError::Timeout)
            })
            .await?
    }
}

#[uniffi::export]
impl Bleng {
    #[uniffi::constructor]
    pub fn new() -> Result<Arc<Self>, BlengError> {
        Ok(Arc::new(Self {
            owner: Arc::new(runtime::RuntimeOwner::new().map_err(|_| BlengError::WorkerStopped)?),
        }))
    }

    pub async fn assemble_backend(&self, backend: Arc<dyn BleBackend>) -> Result<(), BlengError> {
        let owner = Arc::clone(&self.owner);
        self.owner
            .run_task(async move { owner.assemble_backend(backend).await })
            .await?
            .map_err(BlengError::from)
    }

    /// Cancels by ID when the typed operation handle is unavailable. Prefer
    /// `handle.cancel()` when possible: it can win before actor admission and
    /// distinguish a completed, retired operation from an unknown ID.
    /// This path requires actor-owned state; an unadmitted or retired ID may be
    /// `OperationNotFound`. It does not shut down this BLeng instance.
    /// Success acknowledges cancellation handling, not native teardown or remote
    /// rollback; the original operation's late evidence remains asynchronous.
    pub async fn cancel_operation(&self, command_id: CommandId) -> Result<(), BlengError> {
        let owner = Arc::clone(&self.owner);
        self.owner
            .run_task(async move { owner.cancel_operation(command_id).await })
            .await?
            .map_err(BlengError::from)
    }

    /// Eagerly submits to the bounded actor mailbox without waiting for admission.
    /// Stateful admission errors and BLE outcomes are delivered by `wait(timeout_ms)`.
    pub fn begin_initialize(&self) -> Result<Arc<InitializeOperation>, BlengError> {
        Ok(Arc::new(InitializeOperation {
            operation: self.submit_initialize(true)?,
        }))
    }

    /// Eagerly submits to the bounded actor mailbox without waiting for admission.
    /// Stateful admission errors and BLE outcomes are delivered by `wait(timeout_ms)`.
    pub fn begin_start_scan(
        &self,
        options: ScanOptions,
    ) -> Result<Arc<StartScanOperation>, BlengError> {
        Ok(Arc::new(StartScanOperation {
            operation: self.submit_start_scan(options, true)?,
        }))
    }

    /// Eagerly submits to the bounded actor mailbox without waiting for admission.
    /// Stateful admission errors and BLE outcomes are delivered by `wait(timeout_ms)`.
    pub fn begin_stop_scan(
        &self,
        session: ScanSessionId,
    ) -> Result<Arc<StopScanOperation>, BlengError> {
        Ok(Arc::new(StopScanOperation {
            operation: self.submit_stop_scan(session, true)?,
        }))
    }

    /// Eagerly submits to the bounded actor mailbox without waiting for admission.
    /// Stateful admission errors and BLE outcomes are delivered by `wait(timeout_ms)`.
    pub fn begin_connect(
        &self,
        peripheral: PeripheralId,
    ) -> Result<Arc<ConnectOperation>, BlengError> {
        Ok(Arc::new(ConnectOperation {
            operation: self.submit_connect(peripheral, false, true)?,
        }))
    }

    /// Same as [`Self::begin_connect`], but asks the platform for an auto-connect link where it can
    /// express one. A backend that cannot rejects the command with `NotSupported`.
    pub fn begin_connect_with_options(
        &self,
        peripheral: PeripheralId,
        auto_connect: bool,
    ) -> Result<Arc<ConnectOperation>, BlengError> {
        Ok(Arc::new(ConnectOperation {
            operation: self.submit_connect(peripheral, auto_connect, true)?,
        }))
    }

    /// Eagerly submits to the bounded actor mailbox without waiting for admission.
    /// Stateful admission errors and BLE outcomes are delivered by `wait(timeout_ms)`.
    pub fn begin_disconnect(
        &self,
        connection: ConnectionId,
    ) -> Result<Arc<DisconnectOperation>, BlengError> {
        Ok(Arc::new(DisconnectOperation {
            operation: self.submit_disconnect(connection, true)?,
        }))
    }

    /// Eagerly submits to the bounded actor mailbox without waiting for admission.
    /// Stateful admission errors and BLE outcomes are delivered by `wait(timeout_ms)`.
    pub fn begin_discover_gatt(
        &self,
        connection: ConnectionId,
    ) -> Result<Arc<DiscoverGattOperation>, BlengError> {
        Ok(Arc::new(DiscoverGattOperation {
            operation: self.submit_discover_gatt(connection, true)?,
        }))
    }

    /// Eagerly submits to the bounded actor mailbox without waiting for admission.
    /// Stateful admission errors and BLE outcomes are delivered by `wait(timeout_ms)`.
    pub fn begin_read_characteristic(
        &self,
        connection: ConnectionId,
        session: GattSessionId,
        characteristic: GattCharacteristicId,
    ) -> Result<Arc<ReadOperation>, BlengError> {
        Ok(Arc::new(ReadOperation {
            operation: self.submit_read_characteristic(
                connection,
                session,
                characteristic,
                true,
            )?,
        }))
    }

    /// Eagerly submits to the bounded actor mailbox without waiting for admission.
    /// Stateful admission errors and BLE outcomes are delivered by `wait(timeout_ms)`.
    pub fn begin_write_characteristic(
        &self,
        connection: ConnectionId,
        session: GattSessionId,
        characteristic: GattCharacteristicId,
        value: Vec<u8>,
        write_type: WriteType,
    ) -> Result<Arc<WriteOperation>, BlengError> {
        Ok(Arc::new(WriteOperation {
            operation: self.submit_write_characteristic(
                connection,
                session,
                characteristic,
                value,
                write_type,
                true,
            )?,
        }))
    }

    /// Eagerly submits to the bounded actor mailbox without waiting for admission.
    /// A descriptor is addressed through the characteristic that owns it.
    pub fn begin_read_descriptor(
        &self,
        connection: ConnectionId,
        session: GattSessionId,
        characteristic: GattCharacteristicId,
        descriptor: GattDescriptorId,
    ) -> Result<Arc<ReadDescriptorOperation>, BlengError> {
        Ok(Arc::new(ReadDescriptorOperation {
            operation: self.submit_read_descriptor(
                connection,
                session,
                characteristic,
                descriptor,
                true,
            )?,
        }))
    }

    /// Eagerly submits to the bounded actor mailbox without waiting for admission.
    /// Descriptor writes always expect an ATT response, so there is no write type.
    pub fn begin_write_descriptor(
        &self,
        connection: ConnectionId,
        session: GattSessionId,
        characteristic: GattCharacteristicId,
        descriptor: GattDescriptorId,
        value: Vec<u8>,
    ) -> Result<Arc<WriteDescriptorOperation>, BlengError> {
        Ok(Arc::new(WriteDescriptorOperation {
            operation: self.submit_write_descriptor(
                connection,
                session,
                characteristic,
                descriptor,
                value,
                true,
            )?,
        }))
    }

    /// Eagerly submits to the bounded actor mailbox without waiting for admission.
    /// Stateful admission errors and BLE outcomes are delivered by `wait(timeout_ms)`.
    pub fn begin_enable_notifications(
        &self,
        connection: ConnectionId,
        session: GattSessionId,
        characteristic: GattCharacteristicId,
        mode: NotificationModeRequest,
    ) -> Result<Arc<EnableNotificationsOperation>, BlengError> {
        Ok(Arc::new(EnableNotificationsOperation {
            operation: self.submit_enable_notifications(
                connection,
                session,
                characteristic,
                mode,
                true,
            )?,
        }))
    }

    /// Eagerly submits to the bounded actor mailbox without waiting for admission.
    /// Stateful admission errors and BLE outcomes are delivered by `wait(timeout_ms)`.
    pub fn begin_disable_notifications(
        &self,
        connection: ConnectionId,
        session: GattSessionId,
        subscription: SubscriptionId,
    ) -> Result<Arc<DisableNotificationsOperation>, BlengError> {
        Ok(Arc::new(DisableNotificationsOperation {
            operation: self.submit_disable_notifications(
                connection,
                session,
                subscription,
                true,
            )?,
        }))
    }

    /// Eagerly submits to the bounded actor mailbox without waiting for admission.
    /// Stateful admission errors and BLE outcomes are delivered by `wait(timeout_ms)`.
    pub fn begin_read_rssi(
        &self,
        connection: ConnectionId,
    ) -> Result<Arc<ReadRssiOperation>, BlengError> {
        Ok(Arc::new(ReadRssiOperation {
            operation: self.submit_read_rssi(connection, true)?,
        }))
    }

    /// Requests an MTU where the platform supports it. A successful submission is not a
    /// negotiated MTU; `wait` reports the platform's actual snapshot or a typed failure.
    pub fn begin_request_mtu(
        &self,
        connection: ConnectionId,
        mtu: u16,
    ) -> Result<Arc<MtuOperation>, BlengError> {
        Ok(Arc::new(MtuOperation {
            operation: self.submit_mtu(connection, Some(mtu), true)?,
        }))
    }

    /// Queries the current MTU/payload limits without requesting native negotiation.
    pub fn begin_query_mtu(
        &self,
        connection: ConnectionId,
    ) -> Result<Arc<MtuOperation>, BlengError> {
        Ok(Arc::new(MtuOperation {
            operation: self.submit_mtu(connection, None, true)?,
        }))
    }

    /// Requests a link scheduling preference where the platform supports it.
    /// The receipt only records that the platform accepted the request.
    pub fn begin_set_connection_priority(
        &self,
        connection: ConnectionId,
        priority: ConnectionPriority,
    ) -> Result<Arc<SetConnectionPriorityOperation>, BlengError> {
        Ok(Arc::new(SetConnectionPriorityOperation {
            operation: self.submit_set_connection_priority(connection, priority, true)?,
        }))
    }

    /// Requests a radio power change where the platform supports it.
    /// Success only means the platform accepted the request; observe the adapter lane for state.
    pub fn begin_set_adapter_enabled(
        &self,
        enabled: bool,
    ) -> Result<Arc<SetAdapterEnabledOperation>, BlengError> {
        Ok(Arc::new(SetAdapterEnabledOperation {
            operation: self.submit_set_adapter_enabled(enabled, true)?,
        }))
    }

    /// Reads a read-only system inventory. Results are data, not connectable capabilities.
    pub fn begin_query_devices(
        &self,
        kind: DeviceQueryKind,
        service_uuids: Vec<BluetoothUuid>,
    ) -> Result<Arc<QueryDevicesOperation>, BlengError> {
        Ok(Arc::new(QueryDevicesOperation {
            operation: self.submit_query_devices(kind, service_uuids, true)?,
        }))
    }

    /// Requests one companion-device association where the platform supports it.
    ///
    /// `device_profile` is the host's own platform profile choice; the library declares no
    /// companion permission. Success means the platform reported an association, not that a device
    /// is present or connected — read `DeviceQueryKind::Companion` afterwards for the list.
    ///
    /// This is the only command without a `timeout_ms` convenience form: the platform's system
    /// chooser is paced by the user, so a bounded one-shot call would turn correct slow use into a
    /// timeout. Callers own the wait through the returned handle.
    pub fn begin_associate_companion(
        &self,
        service_uuids: Vec<BluetoothUuid>,
        display_name: Option<String>,
        device_profile: Option<String>,
        notify_on_device_nearby: bool,
    ) -> Result<Arc<AssociateCompanionDeviceOperation>, BlengError> {
        Ok(Arc::new(AssociateCompanionDeviceOperation {
            operation: self.submit_associate_companion(
                service_uuids,
                display_name,
                device_profile,
                notify_on_device_nearby,
                true,
            )?,
        }))
    }

    /// Requests bonding where the platform supports it. `pin` is the platform's numeric PIN pairing
    /// input; `None` leaves the system pairing UI in charge. A successful receipt is the
    /// platform-reported terminal bond state, not a pairing promise.
    pub fn begin_create_bond(
        &self,
        connection: ConnectionId,
        pin: Option<String>,
    ) -> Result<Arc<BondOperation>, BlengError> {
        Ok(Arc::new(BondOperation::create(
            self.submit_create_bond(connection, pin, true)?,
        )))
    }

    /// Removes a bond where the platform supports it. Success is the platform-reported `None` state.
    pub fn begin_remove_bond(
        &self,
        connection: ConnectionId,
    ) -> Result<Arc<BondOperation>, BlengError> {
        Ok(Arc::new(BondOperation::remove(
            self.submit_remove_bond(connection, true)?,
        )))
    }

    /// Requests invalidation of the platform's cached GATT topology. On success the engine also
    /// drops this connection's discovered topology, so earlier GATT sessions must be rediscovered.
    pub fn begin_refresh_gatt_cache(
        &self,
        connection: ConnectionId,
    ) -> Result<Arc<RefreshGattCacheOperation>, BlengError> {
        Ok(Arc::new(RefreshGattCacheOperation {
            operation: self.submit_refresh_gatt_cache(connection, true)?,
        }))
    }

    pub async fn initialize(&self, timeout_ms: u64) -> Result<AdapterSnapshot, BlengError> {
        let deadline = completion_deadline(timeout_ms)?;
        let operation = self.submit_initialize(false)?;
        operation.wait_until(deadline).await
    }

    pub async fn start_scan(
        &self,
        options: ScanOptions,
        timeout_ms: u64,
    ) -> Result<ScanSessionId, BlengError> {
        let deadline = completion_deadline(timeout_ms)?;
        let operation = self.submit_start_scan(options, false)?;
        operation.wait_until(deadline).await
    }

    pub async fn stop_scan(
        &self,
        session: ScanSessionId,
        timeout_ms: u64,
    ) -> Result<(), BlengError> {
        let deadline = completion_deadline(timeout_ms)?;
        let operation = self.submit_stop_scan(session, false)?;
        operation.wait_until(deadline).await
    }

    pub async fn connect(
        &self,
        peripheral: PeripheralId,
        timeout_ms: u64,
    ) -> Result<ConnectionId, BlengError> {
        let deadline = completion_deadline(timeout_ms)?;
        let operation = self.submit_connect(peripheral, false, false)?;
        operation.wait_until(deadline).await
    }

    /// Convenience variant of [`Self::connect`] that requests an auto-connect link.
    pub async fn connect_with_options(
        &self,
        peripheral: PeripheralId,
        auto_connect: bool,
        timeout_ms: u64,
    ) -> Result<ConnectionId, BlengError> {
        let deadline = completion_deadline(timeout_ms)?;
        let operation = self.submit_connect(peripheral, auto_connect, false)?;
        operation.wait_until(deadline).await
    }

    /// Requests a radio power change where the platform supports it.
    pub async fn set_adapter_enabled(
        &self,
        enabled: bool,
        timeout_ms: u64,
    ) -> Result<(), BlengError> {
        let deadline = completion_deadline(timeout_ms)?;
        let operation = self.submit_set_adapter_enabled(enabled, false)?;
        operation.wait_until(deadline).await
    }

    /// Reads a read-only system inventory. Results are data, not connectable capabilities.
    pub async fn query_devices(
        &self,
        kind: DeviceQueryKind,
        service_uuids: Vec<BluetoothUuid>,
        timeout_ms: u64,
    ) -> Result<Vec<DeviceRecord>, BlengError> {
        let deadline = completion_deadline(timeout_ms)?;
        let operation = self.submit_query_devices(kind, service_uuids, false)?;
        operation.wait_until(deadline).await
    }

    pub async fn disconnect(
        &self,
        connection: ConnectionId,
        timeout_ms: u64,
    ) -> Result<(), BlengError> {
        let deadline = completion_deadline(timeout_ms)?;
        let operation = self.submit_disconnect(connection, false)?;
        operation.wait_until(deadline).await
    }

    /// Discovers the complete GATT database needed to address characteristics
    /// and descriptors. The same completion is also emitted as a
    /// `BleEvent::GattDiscovered` event for callers that consume the event stream.
    ///
    /// Disable active subscriptions and await any pending subscription transitions
    /// before discovery. Otherwise admission returns `Busy` with reason `Gatt`;
    /// discovery does not implicitly disable subscriptions.
    pub async fn discover_gatt(
        &self,
        connection: ConnectionId,
        timeout_ms: u64,
    ) -> Result<GattDatabase, BlengError> {
        let deadline = completion_deadline(timeout_ms)?;
        let operation = self.submit_discover_gatt(connection, false)?;
        operation.wait_until(deadline).await
    }

    pub async fn read_characteristic(
        &self,
        connection: ConnectionId,
        session: GattSessionId,
        characteristic: GattCharacteristicId,
        timeout_ms: u64,
    ) -> Result<Vec<u8>, BlengError> {
        let deadline = completion_deadline(timeout_ms)?;
        let operation =
            self.submit_read_characteristic(connection, session, characteristic, false)?;
        operation.wait_until(deadline).await
    }

    pub async fn write_characteristic(
        &self,
        connection: ConnectionId,
        session: GattSessionId,
        characteristic: GattCharacteristicId,
        value: Vec<u8>,
        write_type: WriteType,
        timeout_ms: u64,
    ) -> Result<(), BlengError> {
        let deadline = completion_deadline(timeout_ms)?;
        let operation = self.submit_write_characteristic(
            connection,
            session,
            characteristic,
            value,
            write_type,
            false,
        )?;
        operation.wait_until(deadline).await
    }

    pub async fn enable_notifications(
        &self,
        connection: ConnectionId,
        session: GattSessionId,
        characteristic: GattCharacteristicId,
        mode: NotificationModeRequest,
        timeout_ms: u64,
    ) -> Result<NotificationSubscription, BlengError> {
        let deadline = completion_deadline(timeout_ms)?;
        let operation =
            self.submit_enable_notifications(connection, session, characteristic, mode, false)?;
        operation
            .wait_until(deadline)
            .await
            .map(|(subscription, actual_mode)| NotificationSubscription {
                subscription,
                actual_mode,
            })
    }

    pub async fn disable_notifications(
        &self,
        connection: ConnectionId,
        session: GattSessionId,
        subscription: SubscriptionId,
        timeout_ms: u64,
    ) -> Result<(), BlengError> {
        let deadline = completion_deadline(timeout_ms)?;
        let operation =
            self.submit_disable_notifications(connection, session, subscription, false)?;
        operation.wait_until(deadline).await
    }

    pub async fn read_rssi(
        &self,
        connection: ConnectionId,
        timeout_ms: u64,
    ) -> Result<i16, BlengError> {
        let deadline = completion_deadline(timeout_ms)?;
        let operation = self.submit_read_rssi(connection, false)?;
        operation.wait_until(deadline).await
    }

    pub async fn request_mtu(
        &self,
        connection: ConnectionId,
        mtu: u16,
        timeout_ms: u64,
    ) -> Result<MtuInfo, BlengError> {
        let deadline = completion_deadline(timeout_ms)?;
        let operation = self.submit_mtu(connection, Some(mtu), false)?;
        operation.wait_until(deadline).await
    }

    pub async fn query_mtu(
        &self,
        connection: ConnectionId,
        timeout_ms: u64,
    ) -> Result<MtuInfo, BlengError> {
        let deadline = completion_deadline(timeout_ms)?;
        let operation = self.submit_mtu(connection, None, false)?;
        operation.wait_until(deadline).await
    }

    pub async fn read_descriptor(
        &self,
        connection: ConnectionId,
        session: GattSessionId,
        characteristic: GattCharacteristicId,
        descriptor: GattDescriptorId,
        timeout_ms: u64,
    ) -> Result<Vec<u8>, BlengError> {
        let deadline = completion_deadline(timeout_ms)?;
        let operation =
            self.submit_read_descriptor(connection, session, characteristic, descriptor, false)?;
        operation.wait_until(deadline).await
    }

    pub async fn write_descriptor(
        &self,
        connection: ConnectionId,
        session: GattSessionId,
        characteristic: GattCharacteristicId,
        descriptor: GattDescriptorId,
        value: Vec<u8>,
        timeout_ms: u64,
    ) -> Result<(), BlengError> {
        let deadline = completion_deadline(timeout_ms)?;
        let operation = self.submit_write_descriptor(
            connection,
            session,
            characteristic,
            descriptor,
            value,
            false,
        )?;
        operation.wait_until(deadline).await
    }

    pub async fn set_connection_priority(
        &self,
        connection: ConnectionId,
        priority: ConnectionPriority,
        timeout_ms: u64,
    ) -> Result<(), BlengError> {
        let deadline = completion_deadline(timeout_ms)?;
        let operation = self.submit_set_connection_priority(connection, priority, false)?;
        operation.wait_until(deadline).await
    }

    /// Convenience form of [`Self::begin_create_bond`] for the common case: PIN entry is left to
    /// the system pairing UI. Use `begin_create_bond` with `pin` when the platform needs a numeric
    /// PIN instead.
    pub async fn create_bond(
        &self,
        connection: ConnectionId,
        timeout_ms: u64,
    ) -> Result<BondState, BlengError> {
        let deadline = completion_deadline(timeout_ms)?;
        let operation = self.submit_create_bond(connection, None, false)?;
        operation.wait_until(deadline).await
    }

    pub async fn remove_bond(
        &self,
        connection: ConnectionId,
        timeout_ms: u64,
    ) -> Result<BondState, BlengError> {
        let deadline = completion_deadline(timeout_ms)?;
        let operation = self.submit_remove_bond(connection, false)?;
        operation.wait_until(deadline).await
    }

    /// Requests invalidation of the platform's cached GATT topology for this connection.
    pub async fn refresh_gatt_cache(
        &self,
        connection: ConnectionId,
        timeout_ms: u64,
    ) -> Result<(), BlengError> {
        let deadline = completion_deadline(timeout_ms)?;
        let operation = self.submit_refresh_gatt_cache(connection, false)?;
        operation.wait_until(deadline).await
    }

    /// Reads the bounded event FIFO, including later adapter snapshot changes.
    /// No last-known snapshot is replayed. Concurrent readers divide this stream,
    /// so consumers should share one reader and project their own application state.
    pub async fn next_event(&self, timeout_ms: u64) -> Result<Option<BleEvent>, BlengError> {
        let timeout = validate_timeout(timeout_ms)?;
        let owner = Arc::clone(&self.owner);
        self.owner
            .run_task(async move { owner.next_event(timeout).await })
            .await?
            .map_err(BlengError::from)
    }

    pub async fn shutdown(&self) -> Result<(), BlengError> {
        let owner = Arc::clone(&self.owner);
        self.owner
            .run_task(async move { owner.close().await })
            .await?
            .map_err(BlengError::from)
    }
}

#[cfg(test)]
#[path = "../tests/facade_tests.rs"]
mod tests;
