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
use tokio::task::AbortHandle;

use crate::{error::BlengError, runtime};

const MAX_TIMEOUT_MS: u64 = 86_400_000;

fn validate_timeout(milliseconds: u64) -> Result<Duration, BlengError> {
    if !(1..=MAX_TIMEOUT_MS).contains(&milliseconds) {
        return Err(BlengError::InvalidTimeout);
    }
    Ok(Duration::from_millis(milliseconds))
}

struct AbortOnDrop(AbortHandle);

impl Drop for AbortOnDrop {
    fn drop(&mut self) {
        self.0.abort();
    }
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
    /// Dropping the foreign waiter aborts this task, without promising cancellation
    /// of a native BLE command that has already been dispatched.
    async fn spawn_on_runtime<T: Send + 'static>(
        &self,
        future: impl Future<Output = T> + Send + 'static,
    ) -> Result<T, BlengError> {
        let task = self.owner.handle().spawn(future);
        // Foreign future release retires the BLeng waiter. It does not promise
        // cancellation of an already-dispatched native BLE command.
        let _abort = AbortOnDrop(task.abort_handle());
        task.await.map_err(|_| BlengError::WorkerStopped)
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
