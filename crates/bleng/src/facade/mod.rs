//! Caller-facing BLeng facade and private-runtime boundary.
//!
//! Typed operation handles live in operations.rs; error types live in error.rs.
//! Dropping a handle retires caller delivery, while cancel() explicitly requests
//! native control under the operation policy and backend's cancellation support.

use std::{sync::Arc, time::Duration};

use blehub::{
    AdapterSnapshot, BleBackend, BleEvent, CommandId, ConnectionId, GattCharacteristicId,
    GattDatabase, GattSessionId, NotificationModeRequest, PeripheralId, ScanOptions, ScanSessionId,
    SubscriptionId, WriteType,
};
use tokio::time::Instant;

use crate::{error::BlengError, runtime};

mod operations;

use operations::OperationAdapter;
pub use operations::{
    ConnectOperation, DisableNotificationsOperation, DisconnectOperation, DiscoverGattOperation,
    EnableNotificationsOperation, InitializeOperation, ReadOperation, ReadRssiOperation,
    StartScanOperation, StopScanOperation, WriteOperation,
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
        preserve_completed_result_on_close: bool,
    ) -> Result<OperationAdapter<runtime::BeginConnect>, BlengError> {
        let pending =
            self.owner
                .submit_operation(preserve_completed_result_on_close, |submission| {
                    runtime::BeginConnect {
                        submission,
                        peripheral_id: peripheral,
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
            operation: self.submit_connect(peripheral, true)?,
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
        let operation = self.submit_connect(peripheral, false)?;
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
    ) -> Result<SubscriptionId, BlengError> {
        let deadline = completion_deadline(timeout_ms)?;
        let operation =
            self.submit_enable_notifications(connection, session, characteristic, mode, false)?;
        operation
            .wait_until(deadline)
            .await
            .map(|(subscription, _actual_mode)| subscription)
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
