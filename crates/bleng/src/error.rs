//! Public BLeng error taxonomy and runtime-to-foreign error mapping.
//!
//! This module is the reviewed error boundary between the private runtime and
//! the Stable UniFFI facade. Runtime-specific errors stay private; only this
//! typed taxonomy crosses the BLeng API.

use blehub::{BleEventError, BleHubError, CommandDispatchError, CommandId};

use super::{cleanup, runtime, waiter};

/// Identifies the missing lifecycle prerequisite for an accepted BLeng object.
///
/// These are admission errors: the requested BLE command has not been
/// dispatched to the backend when one of these reasons is returned.
#[derive(Clone, Copy, Debug, PartialEq, Eq, uniffi::Enum)]
pub enum BlengNotReadyReason {
    /// No backend has been attached to this BLeng instance.
    BackendNotAssembled,
    /// A backend is attached, but its adapter initialization has not completed.
    NotInitialized,
    /// The requested scan session does not currently exist.
    ScanNotActive,
    /// The requested connection is absent, failed, or already disconnected.
    ConnectionNotActive,
    /// GATT discovery has not produced the requested session/characteristic.
    GattNotReady,
    /// The requested notification subscription is not active.
    SubscriptionNotActive,
}

/// Identifies the BLeng-owned state that currently prevents admission.
///
/// Busy is distinct from not-ready: the required resource exists, but conflicting
/// ownership must finish or be explicitly released before admission can succeed.
#[derive(Clone, Copy, Debug, PartialEq, Eq, uniffi::Enum)]
pub enum BlengBusyReason {
    /// A scan session is already active, or its start/stop transition is pending.
    Scan,
    /// The peripheral already has a pending or active connection in this BLeng instance.
    Peripheral,
    /// Discovery conflicts with pending connection/GATT work or an active subscription.
    /// Complete pending operations and disable subscriptions before rediscovering.
    Gatt,
    /// A conflicting per-connection operation is in flight, such as read, write, RSSI,
    /// disconnect, or a notification transition.
    Operation,
    /// This BLeng instance already owns its single backend slot.
    BackendSlotOccupied,
}

/// Identifies a bounded BLeng resource that can no longer be allocated safely.
#[derive(Clone, Copy, Debug, PartialEq, Eq, uniffi::Enum)]
pub enum BlengResource {
    /// The bounded waiter registry cannot accept another pending operation.
    PendingWaiters,
    /// The bounded submission mailbox cannot accept another entry right now.
    SubmissionQueue,
    /// The monotonic command/session/connection ID allocator reached its limit.
    Identifiers,
}

/// Identifies a violation of a caller or ownership invariant.
///
/// These are not ordinary transient BLE states. Retrying the same invalid
/// request without changing its identity or ownership is not expected to help.
#[derive(Clone, Copy, Debug, PartialEq, Eq, uniffi::Enum)]
pub enum BlengContractFailure {
    /// A command ID was zero or duplicated a live registration.
    InvalidCommandId,
    /// The same canonical BleHub object was attached more than once.
    DuplicateHubOwnership,
}

#[derive(Debug, thiserror::Error, uniffi::Error)]
pub enum BlengError {
    #[error("BLeng is closed")]
    Closed,
    #[error("BLeng operation was cancelled")]
    Cancelled,
    #[error("BLeng operation was not found: {command_id:?}")]
    OperationNotFound { command_id: CommandId },
    #[error("BLeng operation already completed: {command_id:?}")]
    OperationAlreadyCompleted { command_id: CommandId },
    #[error("BLeng operation cannot be cancelled: {command_id:?}")]
    OperationNotCancellable { command_id: CommandId },
    #[error("BLeng operation result was already consumed")]
    OperationResultConsumed,
    #[error("BLE connection was lost")]
    Disconnected,
    #[error("timeout must be between 1 ms and 24 hours")]
    InvalidTimeout,
    #[error("BLeng prerequisite is not ready: {reason:?}")]
    NotReady { reason: BlengNotReadyReason },
    #[error("BLeng operation is busy: {reason:?}")]
    Busy { reason: BlengBusyReason },
    #[error("BLeng resource exhausted: {resource:?}")]
    ResourceExhausted { resource: BlengResource },
    #[error("BLeng contract failure: {reason:?}")]
    ContractFailure { reason: BlengContractFailure },
    #[error("BLeng dispatch rejected: {error}")]
    DispatchRejected { error: CommandDispatchError },
    #[error("BLeng operation failed: {error}")]
    Operation { error: BleEventError },
    #[error("backend assembly failed: {error}")]
    Assembly { error: BleHubError },
    #[error("BLeng cleanup failed: {error}")]
    Cleanup { error: BleHubError },
    #[error("BLeng operation timed out")]
    Timeout,
    #[error("BLeng transport fault")]
    TransportFault,
    /// Private runtime startup or worker execution failed unexpectedly.
    /// Caller-side wait retirement does not produce this error or cancel native BLE work.
    #[error("BLeng worker stopped")]
    WorkerStopped,
}

// These conversions intentionally are not one-to-one. Private errors describe
// runtime mechanics; `BlengError` exposes stable caller-facing semantics and
// must not leak actor, waiter, cleanup, or backend-assembly implementation names.
impl From<runtime::TaskFailure> for BlengError {
    fn from(error: runtime::TaskFailure) -> Self {
        // These are execution failures, not BLE timeouts or domain errors.
        // The trace and FFI error omit payloads; the process panic hook is host-owned.
        let cause = match error {
            runtime::TaskFailure::Panicked => "panic",
            runtime::TaskFailure::Cancelled => "cancelled",
            runtime::TaskFailure::WorkerStopped => "worker_stopped",
        };
        tracing::error!(target: "bleng::runtime", cause, "private runtime task stopped");
        Self::WorkerStopped
    }
}

impl From<runtime::AssemblyError> for BlengError {
    fn from(error: runtime::AssemblyError) -> Self {
        match error {
            runtime::AssemblyError::Admission(
                cleanup::AssemblyAdmissionError::DuplicateOwnedHub,
            ) => Self::ContractFailure {
                reason: BlengContractFailure::DuplicateHubOwnership,
            },
            runtime::AssemblyError::Admission(
                cleanup::AssemblyAdmissionError::AdditionalHubRejected,
            ) => Self::Busy {
                reason: BlengBusyReason::BackendSlotOccupied,
            },
            runtime::AssemblyError::Admission(cleanup::AssemblyAdmissionError::Closed) => {
                Self::Closed
            }
            runtime::AssemblyError::Backend(error) => Self::Assembly { error },
            runtime::AssemblyError::WorkerStopped => Self::WorkerStopped,
        }
    }
}

impl From<runtime::EventReadError> for BlengError {
    fn from(error: runtime::EventReadError) -> Self {
        match error {
            runtime::EventReadError::Timeout => Self::Timeout,
            runtime::EventReadError::Fault => Self::TransportFault,
        }
    }
}

impl From<waiter::WaiterError> for BlengError {
    fn from(error: waiter::WaiterError) -> Self {
        match error {
            waiter::WaiterError::Closed => Self::Closed,
            waiter::WaiterError::Cancelled => Self::Cancelled,
            waiter::WaiterError::Timeout => Self::Timeout,
            waiter::WaiterError::TransportFault => Self::TransportFault,
            waiter::WaiterError::DispatchRejected(error) => Self::DispatchRejected { error },
            waiter::WaiterError::Operation(error) => Self::Operation { error },
            waiter::WaiterError::InvalidCommandId => Self::ContractFailure {
                reason: BlengContractFailure::InvalidCommandId,
            },
            waiter::WaiterError::SubmissionQueueFull => Self::ResourceExhausted {
                resource: BlengResource::SubmissionQueue,
            },
            waiter::WaiterError::CapacityExceeded => Self::ResourceExhausted {
                resource: BlengResource::PendingWaiters,
            },
            waiter::WaiterError::ScanBusy => Self::Busy {
                reason: BlengBusyReason::Scan,
            },
            waiter::WaiterError::ScanNotActive => Self::NotReady {
                reason: BlengNotReadyReason::ScanNotActive,
            },
            waiter::WaiterError::PeripheralBusy => Self::Busy {
                reason: BlengBusyReason::Peripheral,
            },
            waiter::WaiterError::ConnectionNotActive => Self::NotReady {
                reason: BlengNotReadyReason::ConnectionNotActive,
            },
            waiter::WaiterError::GattBusy => Self::Busy {
                reason: BlengBusyReason::Gatt,
            },
            waiter::WaiterError::GattNotReady => Self::NotReady {
                reason: BlengNotReadyReason::GattNotReady,
            },
            waiter::WaiterError::OperationBusy => Self::Busy {
                reason: BlengBusyReason::Operation,
            },
            waiter::WaiterError::SubscriptionNotActive => Self::NotReady {
                reason: BlengNotReadyReason::SubscriptionNotActive,
            },
            waiter::WaiterError::Disconnected => Self::Disconnected,
            waiter::WaiterError::BackendNotAssembled => Self::NotReady {
                reason: BlengNotReadyReason::BackendNotAssembled,
            },
            waiter::WaiterError::OperationNotFound(command_id) => {
                Self::OperationNotFound { command_id }
            }
            waiter::WaiterError::OperationAlreadyCompleted(command_id) => {
                Self::OperationAlreadyCompleted { command_id }
            }
            waiter::WaiterError::OperationNotCancellable(command_id) => {
                Self::OperationNotCancellable { command_id }
            }
            waiter::WaiterError::OperationResultConsumed => Self::OperationResultConsumed,
            waiter::WaiterError::NotInitialized => Self::NotReady {
                reason: BlengNotReadyReason::NotInitialized,
            },
            waiter::WaiterError::IdExhausted => Self::ResourceExhausted {
                resource: BlengResource::Identifiers,
            },
        }
    }
}

impl From<cleanup::CleanupError> for BlengError {
    fn from(error: cleanup::CleanupError) -> Self {
        match error {
            cleanup::CleanupError::Backend(error) => Self::Cleanup { error },
            cleanup::CleanupError::WorkerStopped => Self::WorkerStopped,
        }
    }
}
