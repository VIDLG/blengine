use super::Platform;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, uniffi::Enum)]
pub enum BleErrorCategory {
    Permission,
    AdapterUnavailable,
    InvalidState,
    NotFound,
    Timeout,
    Cancelled,
    OperationAborted,
    Connection,
    Gatt,
    Protocol,
    NotSupported,
    Backend,
    Internal,
}

/// Platform diagnostics are boxed at every error-embedding site: the record is large (three
/// optional strings plus two numeric codes), and an unboxed copy in each error payload pushes
/// every `Result` return past clippy's error-size budget without adding value to the hot path.
#[derive(Clone, Debug, PartialEq, Eq, uniffi::Record)]
pub struct PlatformErrorDetails {
    pub platform: Platform,
    pub code: Option<String>,
    pub name: Option<String>,
    pub message: Option<String>,
    /// Numeric platform status when the platform reports one.
    pub status: Option<i32>,
    /// Bluetooth ATT error code when the failure came from an ATT response.
    pub att_code: Option<i32>,
}

#[derive(Clone, Debug, PartialEq, Eq, thiserror::Error, uniffi::Record)]
#[error("{category:?}: {message}")]
pub struct BleEventError {
    pub category: BleErrorCategory,
    pub message: String,
    pub platform_details: Option<Box<PlatformErrorDetails>>,
}

#[derive(Clone, Debug, PartialEq, Eq, thiserror::Error, uniffi::Error)]
pub enum BleHubError {
    #[error("backend assembly failed: {detail}")]
    BackendAssembly { detail: String },
    #[error("backend cleanup failed: {detail}")]
    BackendCleanup {
        detail: String,
        platform_details: Option<Box<PlatformErrorDetails>>,
    },
    #[error("backend resource is busy: {detail}")]
    ResourceBusy { detail: String },
    #[error("invalid configuration `{field}`: {detail}")]
    InvalidConfiguration { field: String, detail: String },
    #[error("FFI contract error: {detail}")]
    FfiContract { detail: String },
}

impl From<uniffi::UnexpectedUniFFICallbackError> for BleHubError {
    fn from(error: uniffi::UnexpectedUniFFICallbackError) -> Self {
        Self::FfiContract {
            detail: error.reason,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, thiserror::Error, uniffi::Error)]
pub enum CommandDispatchError {
    #[error("BleHub is closed; cannot dispatch command")]
    Closed,
    #[error("invalid command request: {detail}")]
    InvalidRequest { detail: String },
    #[error("invalid backend state: {detail}")]
    InvalidState { detail: String },
    #[error("operation was not found: {target_command_id:?}")]
    OperationNotFound { target_command_id: super::CommandId },
    #[error("operation already completed: {target_command_id:?}")]
    OperationAlreadyCompleted { target_command_id: super::CommandId },
    #[error("operation cannot be cancelled: {target_command_id:?}")]
    OperationNotCancellable { target_command_id: super::CommandId },
    #[error("backend resource exhausted: {detail}")]
    ResourceExhausted { detail: String },
    #[error("backend unavailable for command dispatch: {detail}")]
    BackendUnavailable {
        detail: String,
        platform_details: Option<Box<PlatformErrorDetails>>,
    },
    #[error("backend does not support this operation: {detail}")]
    NotSupported { detail: String },
    #[error("unexpected foreign backend exception: {detail}")]
    FfiContract { detail: String },
}

impl From<uniffi::UnexpectedUniFFICallbackError> for CommandDispatchError {
    fn from(error: uniffi::UnexpectedUniFFICallbackError) -> Self {
        Self::FfiContract {
            detail: error.reason,
        }
    }
}
