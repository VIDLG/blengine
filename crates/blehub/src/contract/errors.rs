use super::Platform;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, uniffi::Enum)]
pub enum BleErrorCategory {
    Permission,
    AdapterUnavailable,
    InvalidState,
    NotFound,
    Timeout,
    Cancelled,
    Connection,
    Gatt,
    Protocol,
    NotSupported,
    Backend,
    Internal,
}

#[derive(Clone, Debug, PartialEq, Eq, uniffi::Record)]
pub struct PlatformErrorDetails {
    pub platform: Platform,
    pub code: Option<String>,
    pub name: Option<String>,
    pub message: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Eq, thiserror::Error, uniffi::Record)]
#[error("{category:?}: {message}")]
pub struct BleEventError {
    pub category: BleErrorCategory,
    pub message: String,
    pub platform: Option<PlatformErrorDetails>,
}

#[derive(Clone, Debug, PartialEq, Eq, thiserror::Error, uniffi::Error)]
pub enum BleHubError {
    #[error("backend assembly failed: {detail}")]
    BackendAssembly { detail: String },
    #[error("backend cleanup failed: {detail}")]
    BackendCleanup {
        detail: String,
        platform: Option<PlatformErrorDetails>,
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
    #[error("backend unavailable for command dispatch: {detail}")]
    BackendUnavailable {
        detail: String,
        platform: Option<PlatformErrorDetails>,
    },
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

#[derive(Clone, Debug, PartialEq, Eq, thiserror::Error)]
pub enum EventSinkError {
    #[error("downstream rejected the event: {message}")]
    Rejected { message: String },
}
