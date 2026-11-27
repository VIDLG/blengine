//! Concrete typed operation handles and their shared runtime adapter.

use std::{future::Future, sync::Arc};

use tokio::time::Instant;

use blehub::{
    AdapterSnapshot, BondState, CommandId, ConnectionId, GattDatabase, MtuInfo, NotificationMode,
    ScanSessionId, SubscriptionId,
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

    /// Runs one observation on this operation's private runtime, bounded by a caller deadline.
    ///
    /// Observation is not result consumption and never requests native cancellation: dropping or
    /// timing out the caller only aborts observation. The deadline must run on this operation's
    /// runtime because a foreign caller may have no timer of its own.
    async fn observe_with_timeout<T: Send + 'static>(
        &self,
        timeout_ms: u64,
        observation: impl Future<Output = T> + Send + 'static,
    ) -> Result<T, BlengError> {
        let deadline = completion_deadline(timeout_ms)?;
        self.owner
            .run_task(async move {
                tokio::time::timeout_at(deadline, observation)
                    .await
                    .map_err(|_| BlengError::Timeout)
            })
            .await?
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
        self.operation
            .observe_with_timeout(timeout_ms, observation)
            .await
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

impl ConnectOperation {
    /// Observes on this connection's own runtime without consuming its result or requesting
    /// cancellation. The connect result is already consumed by `wait`; this only lends the
    /// runtime that keeps an observation timer alive when the caller has none.
    pub async fn observe_with_timeout<T: Send + 'static>(
        &self,
        timeout_ms: u64,
        observation: impl Future<Output = T> + Send + 'static,
    ) -> Result<T, BlengError> {
        self.operation
            .observe_with_timeout(timeout_ms, observation)
            .await
    }
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

/// The actual subscription an enable established.
///
/// `actual_mode` is part of the completion receipt, not a later event lookup: a
/// `PlatformDefault` request lets the backend choose, and a caller must not have to
/// observe a second channel to learn what its own operation did. `None` means the
/// platform did not report it, which is not the same as "notification".
#[derive(Clone, Copy, Debug, PartialEq, Eq, uniffi::Record)]
pub struct NotificationSubscription {
    pub subscription: SubscriptionId,
    pub actual_mode: Option<NotificationMode>,
}

/// A single-consumer typed result and independent native cancellation capability.
#[derive(uniffi::Object)]
pub struct EnableNotificationsOperation {
    pub(super) operation: OperationAdapter<runtime::BeginEnableNotifications>,
}

impl EnableNotificationsOperation {
    /// Observes on this operation's private runtime without consuming its result or requesting BLE
    /// cancellation. A caller deadline must not require the caller's own runtime timers, and
    /// dropping the caller only aborts observation.
    pub async fn observe_with_timeout<T: Send + 'static>(
        &self,
        timeout_ms: u64,
        observation: impl Future<Output = T> + Send + 'static,
    ) -> Result<T, BlengError> {
        self.operation
            .observe_with_timeout(timeout_ms, observation)
            .await
    }
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
    pub async fn wait(&self, timeout_ms: u64) -> Result<NotificationSubscription, BlengError> {
        self.operation
            .wait(timeout_ms)
            .await
            .map(|(subscription, actual_mode)| NotificationSubscription {
                subscription,
                actual_mode,
            })
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

/// A single-consumer descriptor value and independent native cancellation capability.
#[derive(uniffi::Object)]
pub struct ReadDescriptorOperation {
    pub(super) operation: OperationAdapter<runtime::BeginReadDescriptor>,
}

#[uniffi::export]
impl ReadDescriptorOperation {
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

/// A single-consumer descriptor write receipt and independent native cancellation capability.
#[derive(uniffi::Object)]
pub struct WriteDescriptorOperation {
    pub(super) operation: OperationAdapter<runtime::BeginWriteDescriptor>,
}

#[uniffi::export]
impl WriteDescriptorOperation {
    /// Stable command ID, not proof of admission or BLE success.
    pub fn command_id(&self) -> CommandId {
        self.operation.command_id
    }

    /// Requests cancellation without consuming the result; no remote rollback.
    pub async fn cancel(&self) -> Result<(), BlengError> {
        self.operation.cancel().await
    }

    /// Consumes one receipt; a receipt is not a read-back.
    pub async fn wait(&self, timeout_ms: u64) -> Result<(), BlengError> {
        self.operation.wait(timeout_ms).await
    }
}

/// A single-consumer MTU snapshot and independent native cancellation capability.
#[derive(uniffi::Object)]
pub struct MtuOperation {
    pub(super) operation: OperationAdapter<runtime::BeginMtu>,
}

#[uniffi::export]
impl MtuOperation {
    /// Stable command ID, not proof of admission, negotiation or BLE success.
    pub fn command_id(&self) -> CommandId {
        self.operation.command_id
    }

    /// Requests cancellation without consuming the result; no remote rollback.
    pub async fn cancel(&self) -> Result<(), BlengError> {
        self.operation.cancel().await
    }

    /// Consumes one result; timeout starts when wait is called.
    pub async fn wait(&self, timeout_ms: u64) -> Result<MtuInfo, BlengError> {
        self.operation.wait(timeout_ms).await
    }
}

/// A single-consumer link-priority receipt and independent native cancellation capability.
#[derive(uniffi::Object)]
pub struct SetConnectionPriorityOperation {
    pub(super) operation: OperationAdapter<runtime::BeginSetConnectionPriority>,
}

#[uniffi::export]
impl SetConnectionPriorityOperation {
    /// Stable command ID, not proof that the controller applied the requested scheduling.
    pub fn command_id(&self) -> CommandId {
        self.operation.command_id
    }

    /// Requests cancellation without consuming the result; no remote rollback.
    pub async fn cancel(&self) -> Result<(), BlengError> {
        self.operation.cancel().await
    }

    /// Consumes one receipt; the platform reports no effective link mode.
    pub async fn wait(&self, timeout_ms: u64) -> Result<(), BlengError> {
        self.operation.wait(timeout_ms).await
    }
}

/// One read-only system inventory; the returned records are not connectable capabilities.
#[derive(uniffi::Object)]
pub struct QueryDevicesOperation {
    pub(super) operation: OperationAdapter<runtime::BeginQueryDevices>,
}

#[uniffi::export]
impl QueryDevicesOperation {
    pub fn command_id(&self) -> CommandId {
        self.operation.command_id
    }

    /// Requests cancellation without consuming the result; a read-only query has no native work.
    pub async fn cancel(&self) -> Result<(), BlengError> {
        self.operation.cancel().await
    }

    /// Consumes one inventory snapshot.
    pub async fn wait(&self, timeout_ms: u64) -> Result<Vec<blehub::DeviceRecord>, BlengError> {
        self.operation.wait(timeout_ms).await
    }
}

/// One bounded GATT-cache refresh receipt and independent (always refused) cancellation capability.
#[derive(uniffi::Object)]
pub struct RefreshGattCacheOperation {
    pub(super) operation: OperationAdapter<runtime::BeginRefreshGattCache>,
}

#[uniffi::export]
impl RefreshGattCacheOperation {
    pub fn command_id(&self) -> CommandId {
        self.operation.command_id
    }

    /// Requests cancellation without consuming the result; a completed refresh has no rollback.
    pub async fn cancel(&self) -> Result<(), BlengError> {
        self.operation.cancel().await
    }

    /// Consumes one receipt; success means the platform accepted the refresh and the engine
    /// dropped this connection's discovered topology.
    pub async fn wait(&self, timeout_ms: u64) -> Result<(), BlengError> {
        self.operation.wait(timeout_ms).await
    }
}

/// One bounded adapter power receipt and independent (always refused) cancellation capability.
#[derive(uniffi::Object)]
pub struct SetAdapterEnabledOperation {
    pub(super) operation: OperationAdapter<runtime::BeginSetAdapterEnabled>,
}

#[uniffi::export]
impl SetAdapterEnabledOperation {
    /// Stable command ID, not proof that the radio changed.
    pub fn command_id(&self) -> CommandId {
        self.operation.command_id
    }

    /// Requests cancellation without consuming the result; a power request has no rollback.
    pub async fn cancel(&self) -> Result<(), BlengError> {
        self.operation.cancel().await
    }

    /// Consumes one receipt; the new adapter state arrives through the adapter lane.
    pub async fn wait(&self, timeout_ms: u64) -> Result<(), BlengError> {
        self.operation.wait(timeout_ms).await
    }
}

/// One bounded companion-device association receipt and independent (always refused)
/// cancellation capability.
#[derive(uniffi::Object)]
pub struct AssociateCompanionDeviceOperation {
    pub(super) operation: OperationAdapter<runtime::BeginAssociateCompanion>,
}

#[uniffi::export]
impl AssociateCompanionDeviceOperation {
    /// Stable command ID, not proof that an association exists.
    pub fn command_id(&self) -> CommandId {
        self.operation.command_id
    }

    /// Requests cancellation without consuming the result; a chooser request has no rollback.
    pub async fn cancel(&self) -> Result<(), BlengError> {
        self.operation.cancel().await
    }

    /// Consumes one receipt; the association list arrives through the companion inventory query.
    pub async fn wait(&self, timeout_ms: u64) -> Result<(), BlengError> {
        self.operation.wait(timeout_ms).await
    }
}

/// One bounded bond (create/remove) receipt and independent cancellation capability.
///
/// Both directions share one operation type, like `MtuOperation` for query/request: the receipt is
/// the platform-reported `BondState`, and the direction decides which state counts as success.
#[derive(uniffi::Object)]
pub struct BondOperation {
    pub(super) direction: BondDirection,
}

pub(super) enum BondDirection {
    Create(OperationAdapter<runtime::BeginCreateBond>),
    Remove(OperationAdapter<runtime::BeginRemoveBond>),
}

impl BondOperation {
    pub(super) fn create(operation: OperationAdapter<runtime::BeginCreateBond>) -> Self {
        Self {
            direction: BondDirection::Create(operation),
        }
    }

    pub(super) fn remove(operation: OperationAdapter<runtime::BeginRemoveBond>) -> Self {
        Self {
            direction: BondDirection::Remove(operation),
        }
    }

    /// Observes progress on this operation's private runtime without consuming its terminal result
    /// or requesting cancellation. The operation's own `wait` remains the success receipt.
    pub async fn observe_with_timeout<T: Send + 'static>(
        &self,
        timeout_ms: u64,
        observation: impl Future<Output = T> + Send + 'static,
    ) -> Result<T, BlengError> {
        match &self.direction {
            BondDirection::Create(operation) => {
                operation
                    .observe_with_timeout(timeout_ms, observation)
                    .await
            }
            BondDirection::Remove(operation) => {
                operation
                    .observe_with_timeout(timeout_ms, observation)
                    .await
            }
        }
    }
}

#[uniffi::export]
impl BondOperation {
    /// Stable command ID, not proof that the peer is bonded or the link is encrypted.
    pub fn command_id(&self) -> CommandId {
        match &self.direction {
            BondDirection::Create(operation) => operation.command_id,
            BondDirection::Remove(operation) => operation.command_id,
        }
    }

    /// Requests cancellation without consuming the result; no remote rollback.
    pub async fn cancel(&self) -> Result<(), BlengError> {
        match &self.direction {
            BondDirection::Create(operation) => operation.cancel().await,
            BondDirection::Remove(operation) => operation.cancel().await,
        }
    }

    /// Consumes one terminal bond state; `Bonded` for create, `None` for removal.
    pub async fn wait(&self, timeout_ms: u64) -> Result<BondState, BlengError> {
        match &self.direction {
            BondDirection::Create(operation) => operation.wait(timeout_ms).await,
            BondDirection::Remove(operation) => operation.wait(timeout_ms).await,
        }
    }
}
