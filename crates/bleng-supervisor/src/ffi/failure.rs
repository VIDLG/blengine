//! One failure projection shared by every foreign operation view.
//!
//! Scan and connection views report through the same fixed DTO and the same mapping from the
//! existing Rust taxonomies. A second, view-specific failure codec would let two surfaces classify
//! one runtime error differently.

use blehub::{
    BleErrorCategory, BleEventError, BleHubError, CommandDispatchError, PlatformErrorDetails,
};
use bleng::{
    BlengBusyReason, BlengContractFailure, BlengError, BlengNotReadyReason, BlengResource,
};

use crate::SupervisorOperationError;

/// Where in the operation path a failure was observed. This is a view projection, not a new
/// runtime taxonomy: every variant maps from an existing Rust error.
#[derive(Clone, Copy, Debug, PartialEq, Eq, uniffi::Enum)]
pub enum ForeignOperationFailureDomain {
    Input,
    Submission,
    Operation,
    Observation,
    Delivery,
    Lifecycle,
}

/// Identity-free product failure for a native operation view.
#[derive(Clone, Debug, PartialEq, Eq, uniffi::Record)]
pub struct ForeignOperationFailure {
    pub domain: ForeignOperationFailureDomain,
    pub code: String,
    pub message: String,
    pub platform_details: Option<PlatformErrorDetails>,
}

impl ForeignOperationFailure {
    pub(super) fn new(domain: ForeignOperationFailureDomain, code: &str, message: &str) -> Self {
        Self {
            domain,
            code: code.to_owned(),
            message: message.to_owned(),
            platform_details: None,
        }
    }

    pub(super) fn input(code: &str, message: &str) -> Self {
        Self::new(ForeignOperationFailureDomain::Input, code, message)
    }
}

/// Validates a foreign timeout before any operation takes its result slot or installs a pull.
pub(super) fn normalize_timeout(value: f64) -> Result<u64, Box<ForeignOperationFailure>> {
    // u64::MAX rounds up to 2^64 as f64. Reject that exclusive upper bound before casting.
    // Integral values (including zero/over-24h) then use the original u64 validation.
    if !value.is_finite() || value.fract() != 0.0 || value < 0.0 || value >= u64::MAX as f64 {
        return Err(Box::new(ForeignOperationFailure::input(
            "invalid_timeout",
            "timeout must be an integer between 1 ms and 24 hours",
        )));
    }
    Ok(value as u64)
}

/// Receipt for one bounded operation control step. An absent failure is this call's success, not
/// remote delivery, native teardown or rollback.
#[derive(Clone, Debug, PartialEq, Eq, uniffi::Record)]
pub struct ForeignOperationCommandResult {
    pub failure: Option<ForeignOperationFailure>,
}

impl ForeignOperationCommandResult {
    pub(super) fn from_failure(failure: Option<ForeignOperationFailure>) -> Self {
        Self { failure }
    }
}

// Only internal Result errors are boxed; the fixed foreign DTO layout stays unchanged.
impl From<SupervisorOperationError> for Box<ForeignOperationFailure> {
    fn from(error: SupervisorOperationError) -> Self {
        Box::new(ForeignOperationFailure::from(error))
    }
}

impl From<SupervisorOperationError> for ForeignOperationFailure {
    fn from(error: SupervisorOperationError) -> Self {
        let (code, message) = match error {
            SupervisorOperationError::NoActiveSession => (
                "supervisor.no_active_session",
                "supervisor has no active session",
            ),
            SupervisorOperationError::StaleContext => (
                "supervisor.stale_context",
                "operation context does not match the active owner",
            ),
            SupervisorOperationError::RecoveryNotReady => (
                "supervisor.recovery_not_ready",
                "supervisor recovery is not ready for operations",
            ),
            SupervisorOperationError::SupervisorBusy => (
                "supervisor.busy",
                "supervisor operation is already in progress",
            ),
            SupervisorOperationError::SupervisorUnavailable => {
                ("supervisor.unavailable", "supervisor is unavailable")
            }
            SupervisorOperationError::InvalidContext { detail } => {
                return Self::input("supervisor.invalid_context", &detail);
            }
            SupervisorOperationError::Bleng { error } => {
                return bleng_failure(ForeignOperationFailureDomain::Submission, error);
            }
        };
        Self::new(ForeignOperationFailureDomain::Lifecycle, code, message)
    }
}

pub(super) fn bleng_failure(
    domain: ForeignOperationFailureDomain,
    error: BlengError,
) -> ForeignOperationFailure {
    let (code, message) = match error {
        BlengError::Closed => ("closed", "BLeng is closed"),
        BlengError::Cancelled => ("cancelled", "BLeng operation was cancelled"),
        BlengError::OperationNotFound { .. } => {
            ("operation_not_found", "BLeng operation was not found")
        }
        BlengError::OperationAlreadyCompleted { .. } => (
            "operation_already_completed",
            "BLeng operation already completed",
        ),
        BlengError::OperationNotCancellable { .. } => (
            "operation_not_cancellable",
            "BLeng operation cannot be cancelled",
        ),
        BlengError::OperationResultConsumed => (
            "operation_result_consumed",
            "BLeng operation result was already consumed",
        ),
        BlengError::Disconnected => ("disconnected", "BLE connection was lost"),
        BlengError::InvalidTimeout => {
            return ForeignOperationFailure::input(
                "invalid_timeout",
                "timeout must be between 1 ms and 24 hours",
            );
        }
        BlengError::NotReady { reason } => match reason {
            BlengNotReadyReason::BackendNotAssembled => (
                "not_ready.backend_not_assembled",
                "no backend has been assembled",
            ),
            BlengNotReadyReason::NotInitialized => (
                "not_ready.not_initialized",
                "adapter initialization has not completed",
            ),
            BlengNotReadyReason::ScanNotActive => {
                ("not_ready.scan_not_active", "scan is not active")
            }
            BlengNotReadyReason::ConnectionNotActive => (
                "not_ready.connection_not_active",
                "connection is not active",
            ),
            BlengNotReadyReason::GattNotReady => ("not_ready.gatt_not_ready", "GATT is not ready"),
            BlengNotReadyReason::SubscriptionNotActive => (
                "not_ready.subscription_not_active",
                "subscription is not active",
            ),
        },
        BlengError::Busy { reason } => match reason {
            BlengBusyReason::Scan => ("busy.scan", "scan is busy"),
            BlengBusyReason::Peripheral => ("busy.peripheral", "peripheral is busy"),
            BlengBusyReason::Gatt => ("busy.gatt", "GATT is busy"),
            BlengBusyReason::Operation => ("busy.operation", "a conflicting operation is pending"),
            BlengBusyReason::BackendSlotOccupied => {
                ("busy.backend_slot_occupied", "backend slot is occupied")
            }
        },
        BlengError::ResourceExhausted { resource } => match resource {
            BlengResource::PendingWaiters => (
                "resource_exhausted.pending_waiters",
                "pending waiter capacity exhausted",
            ),
            BlengResource::SubmissionQueue => (
                "resource_exhausted.submission_queue",
                "submission queue capacity exhausted",
            ),
            BlengResource::Identifiers => (
                "resource_exhausted.identifiers",
                "identifier capacity exhausted",
            ),
        },
        BlengError::ContractFailure { reason } => match reason {
            BlengContractFailure::InvalidCommandId => (
                "contract_failure.invalid_command_id",
                "invalid command identity",
            ),
            BlengContractFailure::DuplicateHubOwnership => (
                "contract_failure.duplicate_hub_ownership",
                "duplicate Hub ownership",
            ),
        },
        BlengError::DispatchRejected { error } => return dispatch_failure(domain, error),
        BlengError::Operation { error } => return ble_failure(domain, "bleng.operation", error),
        BlengError::Assembly { error } => return hub_failure(domain, "bleng.assembly", error),
        BlengError::Cleanup { error } => return hub_failure(domain, "bleng.cleanup", error),
        BlengError::Timeout => ("timeout", "BLeng operation timed out"),
        BlengError::TransportFault => ("transport_fault", "BLeng transport fault"),
        BlengError::WorkerStopped => ("worker_stopped", "BLeng worker stopped"),
    };
    ForeignOperationFailure::new(domain, &format!("bleng.{code}"), message)
}

fn dispatch_failure(
    domain: ForeignOperationFailureDomain,
    error: CommandDispatchError,
) -> ForeignOperationFailure {
    let mut platform_details = None;
    let (code, message) = match error {
        CommandDispatchError::Closed => ("closed", "BleHub is closed".to_owned()),
        CommandDispatchError::InvalidRequest { detail } => ("invalid_request", detail),
        CommandDispatchError::InvalidState { detail } => ("invalid_state", detail),
        CommandDispatchError::OperationNotFound { .. } => {
            ("operation_not_found", "operation was not found".to_owned())
        }
        CommandDispatchError::OperationAlreadyCompleted { .. } => (
            "operation_already_completed",
            "operation already completed".to_owned(),
        ),
        CommandDispatchError::OperationNotCancellable { .. } => (
            "operation_not_cancellable",
            "operation cannot be cancelled".to_owned(),
        ),
        CommandDispatchError::ResourceExhausted { detail } => ("resource_exhausted", detail),
        CommandDispatchError::BackendUnavailable {
            detail,
            platform_details: details,
        } => {
            platform_details = details;
            ("backend_unavailable", detail)
        }
        CommandDispatchError::NotSupported { detail } => ("not_supported", detail),
        CommandDispatchError::FfiContract { detail } => ("ffi_contract", detail),
    };
    ForeignOperationFailure {
        domain,
        code: format!("bleng.dispatch_rejected.{code}"),
        message,
        platform_details: platform_details.map(|details| *details),
    }
}

fn hub_failure(
    domain: ForeignOperationFailureDomain,
    path: &str,
    error: BleHubError,
) -> ForeignOperationFailure {
    let mut platform_details = None;
    let (code, message) = match error {
        BleHubError::BackendAssembly { detail } => ("backend_assembly", detail),
        BleHubError::BackendCleanup {
            detail,
            platform_details: details,
        } => {
            platform_details = details;
            ("backend_cleanup", detail)
        }
        BleHubError::ResourceBusy { detail } => ("resource_busy", detail),
        BleHubError::InvalidConfiguration { detail, .. } => ("invalid_configuration", detail),
        BleHubError::FfiContract { detail } => ("ffi_contract", detail),
    };
    ForeignOperationFailure {
        domain,
        code: format!("{path}.{code}"),
        message,
        platform_details: platform_details.map(|details| *details),
    }
}

pub(super) fn ble_failure(
    domain: ForeignOperationFailureDomain,
    path: &str,
    error: BleEventError,
) -> ForeignOperationFailure {
    let category = match error.category {
        BleErrorCategory::Permission => "permission",
        BleErrorCategory::AdapterUnavailable => "adapter_unavailable",
        BleErrorCategory::InvalidState => "invalid_state",
        BleErrorCategory::NotFound => "not_found",
        BleErrorCategory::Timeout => "timeout",
        BleErrorCategory::Cancelled => "cancelled",
        BleErrorCategory::OperationAborted => "operation_aborted",
        BleErrorCategory::Connection => "connection",
        BleErrorCategory::Gatt => "gatt",
        BleErrorCategory::Protocol => "protocol",
        BleErrorCategory::NotSupported => "not_supported",
        BleErrorCategory::Backend => "backend",
        BleErrorCategory::Internal => "internal",
    };
    ForeignOperationFailure {
        domain,
        code: format!("{path}.{category}"),
        message: error.message,
        platform_details: error.platform_details.map(|details| *details),
    }
}

#[cfg(test)]
#[path = "tests/failure.rs"]
mod tests;
