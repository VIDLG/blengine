//! Concrete typed operation handles and their shared runtime adapter.

use std::{future::Future, sync::Arc};

use tokio::time::Instant;

use blehub::{
    AdapterSnapshot, CommandId, ConnectionId, GattDatabase, ScanSessionId, SubscriptionId,
};

use crate::{error::BlengError, runtime};

use super::completion_deadline;

// Foreign tasks and caller runtimes share one private implementation; public
// objects remain concrete so UniFFI and source-level API tools see every method.
pub(super) struct OperationAdapter<M: runtime::OperationSpec> {
    owner: Arc<runtime::RuntimeOwner>,
    // Allocated identity remains readable after completion or shutdown; it is
    // not evidence of actor admission, backend acceptance, or BLE success.
    pub(super) command_id: CommandId,
    control: runtime::OperationControl,
    pub(super) result_slot: runtime::ResultSlot<M>,
}

impl<M: runtime::OperationSpec> OperationAdapter<M> {
    pub(super) fn new(
        owner: Arc<runtime::RuntimeOwner>,
        handoff: runtime::OperationHandoff<M>,
    ) -> Self {
        // Consume the runtime handoff: these three fields move into the facade
        // handle; no second OperationHandoff retains them.
        let (command_id, control, result_slot) = handoff.into_parts();
        Self {
            owner,
            command_id,
            control,
            result_slot,
        }
    }

    /// Wins locally before dispatch claim, or waits for actor/backend control
    /// acknowledgement after claim. Neither path consumes the result or proves
    /// native teardown; dropping the caller's waiter does not request cancellation.
    async fn cancel(&self) -> Result<(), BlengError> {
        let control = self.control.clone();
        let command_id = self.command_id;
        let result = self
            .owner
            .run_task(async move { control.cancel(command_id).await })
            .await?
            .map_err(BlengError::from);
        match result {
            Err(BlengError::OperationNotFound { command_id })
                if self.result_slot.is_completed() =>
            {
                Err(BlengError::OperationAlreadyCompleted { command_id })
            }
            result => result,
        }
    }

    /// Starts a new timeout at the wait call; submission has no deadline of its
    /// own. The result is single-consumer, and timing out retires delivery only.
    async fn wait(&self, timeout_ms: u64) -> Result<M::Output, BlengError> {
        let deadline = completion_deadline(timeout_ms)?;
        self.wait_until(deadline).await
    }

    /// Convenience calls share one absolute deadline across submission and wait.
    /// Abandoning this task does not cancel native work already dispatched.
    pub(super) async fn wait_until(&self, deadline: Instant) -> Result<M::Output, BlengError> {
        let wait = self.result_slot.claim_wait_until(deadline)?;
        self.owner.run_task(wait).await?.map_err(BlengError::from)
    }
}

/// A single-consumer typed result and independent native cancellation capability.
#[derive(uniffi::Object)]
pub struct InitializeOperation {
    pub(super) operation: OperationAdapter<runtime::BeginInitialize>,
}

#[uniffi::export]
impl InitializeOperation {
    /// Stable command ID, not proof of admission or BLE success.
    pub fn command_id(&self) -> CommandId {
        self.operation.command_id
    }

    /// Requests cancellation without consuming the result; no remote rollback.
    pub async fn cancel(&self) -> Result<(), BlengError> {
        self.operation.cancel().await
    }

    /// Consumes one result; timeout starts when wait is called.
    pub async fn wait(&self, timeout_ms: u64) -> Result<AdapterSnapshot, BlengError> {
        self.operation.wait(timeout_ms).await
    }
}

/// A single-consumer typed result and independent native cancellation capability.
#[derive(uniffi::Object)]
pub struct StartScanOperation {
    pub(super) operation: OperationAdapter<runtime::BeginScan>,
}

impl StartScanOperation {
    /// Registers one Rust-only, non-consuming start-failure observer. Returns None
    /// if another registration is active. Keep the returned token to stay registered.
    /// A late registration synchronously replays confirmed failure; success never
    /// notifies. Caller timeout/drop is not failure. The receiver is held weakly.
    pub fn observe_failure(
        &self,
        observer: std::sync::Weak<dyn crate::OperationFailureObserver>,
    ) -> Option<crate::OperationFailureRegistration> {
        self.operation.result_slot.observe_failure(observer)
    }

    /// Observes on this operation's private runtime without consuming its result
    /// or requesting BLE cancellation. Dropping the caller only aborts observation;
    /// retaining this handle does not keep the Hub lifecycle open.
    pub async fn observe_with_timeout<T: Send + 'static>(
        &self,
        timeout_ms: u64,
        observation: impl Future<Output = T> + Send + 'static,
    ) -> Result<T, BlengError> {
        let deadline = completion_deadline(timeout_ms)?;
        self.operation
            .owner
            .run_task(async move {
                tokio::time::timeout_at(deadline, observation)
                    .await
                    .map_err(|_| BlengError::Timeout)
            })
            .await?
    }
}

#[uniffi::export]
impl StartScanOperation {
    /// Stable command ID, not proof of admission or BLE success.
    pub fn command_id(&self) -> CommandId {
        self.operation.command_id
    }

    /// Requests cancellation without consuming the result; no remote rollback.
    pub async fn cancel(&self) -> Result<(), BlengError> {
        self.operation.cancel().await
    }

    /// Consumes one result; timeout starts when wait is called.
    pub async fn wait(&self, timeout_ms: u64) -> Result<ScanSessionId, BlengError> {
        self.operation.wait(timeout_ms).await
    }
}

/// A single-consumer typed result and independent native cancellation capability.
#[derive(uniffi::Object)]
pub struct StopScanOperation {
    pub(super) operation: OperationAdapter<runtime::BeginStop>,
}

#[uniffi::export]
impl StopScanOperation {
    /// Stable command ID, not proof of admission or BLE success.
    pub fn command_id(&self) -> CommandId {
        self.operation.command_id
    }

    /// Requests cancellation without consuming the result; no remote rollback.
    pub async fn cancel(&self) -> Result<(), BlengError> {
        self.operation.cancel().await
    }

    /// Consumes one result; timeout starts when wait is called.
    pub async fn wait(&self, timeout_ms: u64) -> Result<(), BlengError> {
        self.operation.wait(timeout_ms).await
    }
}

/// A single-consumer typed result and independent native cancellation capability.
#[derive(uniffi::Object)]
pub struct ConnectOperation {
    pub(super) operation: OperationAdapter<runtime::BeginConnect>,
}

#[uniffi::export]
impl ConnectOperation {
    /// Stable command ID, not proof of admission or BLE success.
    pub fn command_id(&self) -> CommandId {
        self.operation.command_id
    }

    /// Requests cancellation without consuming the result; no remote rollback.
    pub async fn cancel(&self) -> Result<(), BlengError> {
        self.operation.cancel().await
    }

    /// Consumes one result; timeout starts when wait is called.
    pub async fn wait(&self, timeout_ms: u64) -> Result<ConnectionId, BlengError> {
        self.operation.wait(timeout_ms).await
    }
}

/// A single-consumer typed result and independent native cancellation capability.
#[derive(uniffi::Object)]
pub struct DisconnectOperation {
    pub(super) operation: OperationAdapter<runtime::BeginDisconnect>,
}

#[uniffi::export]
impl DisconnectOperation {
    /// Stable command ID, not proof of admission or BLE success.
    pub fn command_id(&self) -> CommandId {
        self.operation.command_id
    }

    /// Requests cancellation without consuming the result; no remote rollback.
    pub async fn cancel(&self) -> Result<(), BlengError> {
        self.operation.cancel().await
    }

    /// Consumes one result; timeout starts when wait is called.
    pub async fn wait(&self, timeout_ms: u64) -> Result<(), BlengError> {
        self.operation.wait(timeout_ms).await
    }
}

/// A single-consumer typed result and independent native cancellation capability.
#[derive(uniffi::Object)]
pub struct DiscoverGattOperation {
    pub(super) operation: OperationAdapter<runtime::BeginDiscoverGatt>,
}

#[uniffi::export]
impl DiscoverGattOperation {
    /// Stable command ID, not proof of admission or BLE success.
    pub fn command_id(&self) -> CommandId {
        self.operation.command_id
    }

    /// Requests cancellation without consuming the result; no remote rollback.
    pub async fn cancel(&self) -> Result<(), BlengError> {
        self.operation.cancel().await
    }

    /// Consumes one result; timeout starts when wait is called.
    pub async fn wait(&self, timeout_ms: u64) -> Result<GattDatabase, BlengError> {
        self.operation.wait(timeout_ms).await
    }
}

/// A single-consumer typed result and independent native cancellation capability.
#[derive(uniffi::Object)]
pub struct ReadOperation {
    pub(super) operation: OperationAdapter<runtime::BeginRead>,
}

#[uniffi::export]
impl ReadOperation {
    /// Stable command ID, not proof of admission or BLE success.
    pub fn command_id(&self) -> CommandId {
        self.operation.command_id
    }

    /// Requests cancellation without consuming the result; no remote rollback.
    pub async fn cancel(&self) -> Result<(), BlengError> {
        self.operation.cancel().await
    }

    /// Consumes one result; timeout starts when wait is called.
    pub async fn wait(&self, timeout_ms: u64) -> Result<Vec<u8>, BlengError> {
        self.operation.wait(timeout_ms).await
    }
}

/// A single-consumer typed result and independent native cancellation capability.
#[derive(uniffi::Object)]
pub struct WriteOperation {
    pub(super) operation: OperationAdapter<runtime::BeginWrite>,
}

#[uniffi::export]
impl WriteOperation {
    /// Stable command ID, not proof of admission or BLE success.
    pub fn command_id(&self) -> CommandId {
        self.operation.command_id
    }

    /// Requests cancellation without consuming the result; no remote rollback.
    pub async fn cancel(&self) -> Result<(), BlengError> {
        self.operation.cancel().await
    }

    /// Consumes one result; timeout starts when wait is called.
    pub async fn wait(&self, timeout_ms: u64) -> Result<(), BlengError> {
        self.operation.wait(timeout_ms).await
    }
}

/// A single-consumer typed result and independent native cancellation capability.
#[derive(uniffi::Object)]
pub struct EnableNotificationsOperation {
    pub(super) operation: OperationAdapter<runtime::BeginEnableNotifications>,
}

#[uniffi::export]
impl EnableNotificationsOperation {
    /// Stable command ID, not proof of admission or BLE success.
    pub fn command_id(&self) -> CommandId {
        self.operation.command_id
    }

    /// Requests cancellation without consuming the result; no remote rollback.
    pub async fn cancel(&self) -> Result<(), BlengError> {
        self.operation.cancel().await
    }

    /// Consumes one result; timeout starts when wait is called.
    /// Returns the subscription ID; the actual mode remains in the canonical event.
    pub async fn wait(&self, timeout_ms: u64) -> Result<SubscriptionId, BlengError> {
        self.operation
            .wait(timeout_ms)
            .await
            .map(|(subscription, _actual_mode)| subscription)
    }
}

/// A single-consumer typed result and independent native cancellation capability.
#[derive(uniffi::Object)]
pub struct DisableNotificationsOperation {
    pub(super) operation: OperationAdapter<runtime::BeginDisableNotifications>,
}

#[uniffi::export]
impl DisableNotificationsOperation {
    /// Stable command ID, not proof of admission or BLE success.
    pub fn command_id(&self) -> CommandId {
        self.operation.command_id
    }

    /// Requests cancellation without consuming the result; no remote rollback.
    pub async fn cancel(&self) -> Result<(), BlengError> {
        self.operation.cancel().await
    }

    /// Consumes one result; timeout starts when wait is called.
    pub async fn wait(&self, timeout_ms: u64) -> Result<(), BlengError> {
        self.operation.wait(timeout_ms).await
    }
}

/// A single-consumer typed result and independent native cancellation capability.
#[derive(uniffi::Object)]
pub struct ReadRssiOperation {
    pub(super) operation: OperationAdapter<runtime::BeginRssi>,
}

#[uniffi::export]
impl ReadRssiOperation {
    /// Stable command ID, not proof of admission or BLE success.
    pub fn command_id(&self) -> CommandId {
        self.operation.command_id
    }

    /// Requests cancellation without consuming the result; no remote rollback.
    pub async fn cancel(&self) -> Result<(), BlengError> {
        self.operation.cancel().await
    }

    /// Consumes one result; timeout starts when wait is called.
    pub async fn wait(&self, timeout_ms: u64) -> Result<i16, BlengError> {
        self.operation.wait(timeout_ms).await
    }
}
