use thiserror::Error;

use crate::recovery::RecoveryError;
use bleng::BlengError;

use crate::platform::ResourceSnapshot;

/// Errors found while validating a cross-platform recovery target.
#[derive(Clone, Debug, Error, PartialEq, Eq)]
pub enum RecoveryTargetError {
    #[error("recovery service UUID is invalid")]
    InvalidServiceUuid,
    #[error("recovery characteristic UUID is invalid")]
    InvalidCharacteristicUuid,
    #[error("recovery scan name prefix must not be empty")]
    EmptyScanNamePrefix,
    #[error("recovery scan service UUID at index {index} is invalid")]
    InvalidScanServiceUuid { index: usize },
}

/// Synchronous context admission and typed BLeng submission errors.
#[derive(Debug, Error)]
#[cfg_attr(feature = "ffi", derive(uniffi::Error))]
pub enum SupervisorOperationError {
    #[error("supervisor has no active session")]
    NoActiveSession,
    #[error("operation context does not match the active owner and recovery policy")]
    StaleContext,
    #[error("supervisor recovery is not ready for operations")]
    RecoveryNotReady,
    #[error("supervisor operation is already in progress")]
    SupervisorBusy,
    #[error("supervisor is unavailable")]
    SupervisorUnavailable,
    #[error("invalid operation context: {detail}")]
    InvalidContext { detail: String },
    #[error("BLeng failed: {error}")]
    Bleng {
        #[from]
        error: BlengError,
    },
}

/// Errors at the Rust supervisor/platform-session boundary.
#[derive(Debug, Error)]
pub enum SupervisorError {
    #[error("supervisor already has an active session")]
    AlreadyActive,
    #[error("supervisor has already started its initial session")]
    AlreadyStarted,

    #[error("supervisor has no active session")]
    NoActiveSession,
    #[error("platform session factory failed: {detail}")]
    Platform { detail: String },
    #[error("BLeng failed: {0}")]
    Bleng(#[from] BlengError),
    #[error("recovery policy rejected the observation: {0}")]
    Recovery(#[from] RecoveryError),
    #[error("recovery barrier configuration rejected: {0}")]
    RecoveryConfiguration(RecoveryError),
    #[error("platform session context does not match the requested context")]
    PlatformContextMismatch,
    #[error("supervisor must configure at least one required zero resource")]
    RequiredResourcesNotConfigured,
    #[error("required zero resource name must not be blank")]
    BlankRequiredResourceName,
    #[error("required resource is missing from the retirement snapshot: {resource}")]
    MissingResource { resource: String },
    #[error("native resources remained after retirement: {snapshot:?}")]
    NativeResourcesNotZero { snapshot: ResourceSnapshot },
    #[error("session startup failed: {primary_error}; cleanup failed: {cleanup_error}")]
    StartupCleanup {
        primary_error: String,
        cleanup_error: String,
    },
    #[error("owner retirement failed: {primary_error}; additional failures: {additional_failures}")]
    RetirementCleanup {
        primary_error: String,
        additional_failures: String,
    },
    #[error("recovery action is not pending: {action}")]
    RecoveryActionNotPending { action: &'static str },
    #[error("recovery action is not executable by the platform session: {action}")]
    RecoveryActionNotPlatform { action: &'static str },

    #[error("recovery target is required for action: {action}")]
    RecoveryTargetMissing { action: &'static str },
    #[error("invalid recovery target: {0}")]
    InvalidRecoveryTarget(#[from] RecoveryTargetError),
    #[error("recovery action sequence mismatch: expected {expected}, received {received}")]
    RecoveryActionSequenceMismatch { expected: u64, received: u64 },
}
