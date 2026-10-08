//! Stable UniFFI facade for BLeng.
//!
//! The async runtime remains private to this crate. This module owns the
//! foreign-facing BLeng object and typed errors.

use std::{future::Future, sync::Arc, time::Duration};

use blehub::{
    AdapterSnapshot, BleBackend, BleEvent, ConnectionId, GattCharacteristicId, GattDatabase,
    GattSessionId, NotificationModeRequest, PeripheralId, ScanOptions, ScanSessionId,
    SubscriptionId, WriteType,
};
use tokio_util::task::AbortOnDropHandle;

use crate::{error::BlengError, runtime};

const MAX_TIMEOUT_MS: u64 = 86_400_000;

fn validate_timeout(milliseconds: u64) -> Result<Duration, BlengError> {
    if !(1..=MAX_TIMEOUT_MS).contains(&milliseconds) {
        return Err(BlengError::InvalidTimeout);
    }
    Ok(Duration::from_millis(milliseconds))
}

#[derive(uniffi::Object)]
pub struct Bleng {
    // Shared by the UniFFI Bleng object and every `'static` operation task
    // spawned by `spawn_on_runtime`. The runtime owner stays alive until both the
    // foreign object and all in-flight facade waiters release their clones;
    // Tokio/Kameo internals never cross this field's private boundary.
    owner: Arc<runtime::RuntimeOwner>,
}

impl Bleng {
    /// Spawns one foreign-facing operation on the private runtime and awaits it.
    ///
    /// A foreign caller may have no Tokio runtime, while a Rust caller may have
    /// a different one. The explicit owner handle keeps operation timers and
    /// actor orchestration on this instance's runtime in either case. The
    /// caller only awaits a JoinHandle; this does not nest a blocking executor.
    /// Dropping the foreign waiter aborts this task, without promising cancellation
    /// of a native BLE command that has already been dispatched.
    async fn spawn_on_runtime<T: Send + 'static>(
        &self,
        future: impl Future<Output = T> + Send + 'static,
    ) -> Result<T, BlengError> {
        // Foreign future release retires the BLeng waiter. It does not promise
        // cancellation of an already-dispatched native BLE command.
        AbortOnDropHandle::new(self.owner.handle().spawn(future))
            .await
            .map_err(|_| BlengError::WorkerStopped)
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
        self.spawn_on_runtime(async move { owner.assemble_backend(backend).await })
            .await??;
        Ok(())
    }

    /// Initializes once and returns the first correlated initialization snapshot.
    /// This is not a live adapter-state query; repeated initialization is rejected.
    /// Observe later AdapterSnapshotChanged events through next_event() and maintain
    /// caller-owned last-observed state instead of treating this result as a cache.
    pub async fn initialize(&self, timeout_ms: u64) -> Result<AdapterSnapshot, BlengError> {
        let deadline = validate_timeout(timeout_ms)?;
        let owner = Arc::clone(&self.owner);
        Ok(self
            .spawn_on_runtime(async move { owner.initialize(deadline).await })
            .await??)
    }

    pub async fn start_scan(
        &self,
        options: ScanOptions,
        timeout_ms: u64,
    ) -> Result<ScanSessionId, BlengError> {
        let deadline = validate_timeout(timeout_ms)?;
        let owner = Arc::clone(&self.owner);
        Ok(self
            .spawn_on_runtime(async move { owner.start_scan(options, deadline).await })
            .await??)
    }

    pub async fn stop_scan(
        &self,
        session: ScanSessionId,
        timeout_ms: u64,
    ) -> Result<(), BlengError> {
        let deadline = validate_timeout(timeout_ms)?;
        let owner = Arc::clone(&self.owner);
        self.spawn_on_runtime(async move { owner.stop_scan(session, deadline).await })
            .await??;
        Ok(())
    }

    pub async fn connect(
        &self,
        peripheral: PeripheralId,
        timeout_ms: u64,
    ) -> Result<ConnectionId, BlengError> {
        let deadline = validate_timeout(timeout_ms)?;
        let owner = Arc::clone(&self.owner);
        Ok(self
            .spawn_on_runtime(async move { owner.connect(peripheral, deadline).await })
            .await??)
    }

    pub async fn disconnect(
        &self,
        connection: ConnectionId,
        timeout_ms: u64,
    ) -> Result<(), BlengError> {
        let deadline = validate_timeout(timeout_ms)?;
        let owner = Arc::clone(&self.owner);
        self.spawn_on_runtime(async move { owner.disconnect(connection, deadline).await })
            .await??;
        Ok(())
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
        let deadline = validate_timeout(timeout_ms)?;
        let owner = Arc::clone(&self.owner);
        Ok(self
            .spawn_on_runtime(async move { owner.discover_gatt(connection, deadline).await })
            .await??)
    }

    pub async fn read_characteristic(
        &self,
        connection: ConnectionId,
        session: GattSessionId,
        characteristic: GattCharacteristicId,
        timeout_ms: u64,
    ) -> Result<Vec<u8>, BlengError> {
        let deadline = validate_timeout(timeout_ms)?;
        let owner = Arc::clone(&self.owner);
        Ok(self
            .spawn_on_runtime(async move {
                owner
                    .read_characteristic(connection, session, characteristic, deadline)
                    .await
            })
            .await??)
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
        let deadline = validate_timeout(timeout_ms)?;
        let owner = Arc::clone(&self.owner);
        self.spawn_on_runtime(async move {
            owner
                .write_characteristic(
                    connection,
                    session,
                    characteristic,
                    value,
                    write_type,
                    deadline,
                )
                .await
        })
        .await??;
        Ok(())
    }

    pub async fn enable_notifications(
        &self,
        connection: ConnectionId,
        session: GattSessionId,
        characteristic: GattCharacteristicId,
        mode: NotificationModeRequest,
        timeout_ms: u64,
    ) -> Result<SubscriptionId, BlengError> {
        let deadline = validate_timeout(timeout_ms)?;
        let owner = Arc::clone(&self.owner);
        Ok(self
            .spawn_on_runtime(async move {
                owner
                    .enable_notifications(connection, session, characteristic, mode, deadline)
                    .await
            })
            .await??
            .0)
    }

    pub async fn disable_notifications(
        &self,
        connection: ConnectionId,
        session: GattSessionId,
        subscription: SubscriptionId,
        timeout_ms: u64,
    ) -> Result<(), BlengError> {
        let deadline = validate_timeout(timeout_ms)?;
        let owner = Arc::clone(&self.owner);
        self.spawn_on_runtime(async move {
            owner
                .disable_notifications(connection, session, subscription, deadline)
                .await
        })
        .await??;
        Ok(())
    }

    pub async fn read_rssi(
        &self,
        connection: ConnectionId,
        timeout_ms: u64,
    ) -> Result<i16, BlengError> {
        let deadline = validate_timeout(timeout_ms)?;
        let owner = Arc::clone(&self.owner);
        Ok(self
            .spawn_on_runtime(async move { owner.read_rssi(connection, deadline).await })
            .await??)
    }

    /// Reads the bounded event FIFO, including later adapter snapshot changes.
    /// No last-known snapshot is replayed. Concurrent readers divide this stream,
    /// so consumers should share one reader and project their own application state.
    pub async fn next_event(&self, timeout_ms: u64) -> Result<Option<BleEvent>, BlengError> {
        let timeout = validate_timeout(timeout_ms)?;
        let owner = Arc::clone(&self.owner);
        Ok(self
            .spawn_on_runtime(async move { owner.next_event(timeout).await })
            .await??)
    }

    pub async fn shutdown(&self) -> Result<(), BlengError> {
        let owner = Arc::clone(&self.owner);
        self.spawn_on_runtime(async move { owner.close().await })
            .await??;
        Ok(())
    }
}

#[cfg(test)]
#[path = "tests/api_tests.rs"]
mod tests;
