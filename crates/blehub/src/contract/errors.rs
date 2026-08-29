use boltffi::UnexpectedFfiCallbackError;

use super::Platform;

#[boltffi::data]
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
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

#[boltffi::data]
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PlatformErrorDetails {
    pub platform: Platform,
    pub code: Option<String>,
    pub name: Option<String>,
    pub message: Option<String>,
}

#[boltffi::data]
#[derive(Clone, Debug, PartialEq, Eq, derive_more::Display, derive_more::Error)]
#[display("{category:?}: {message}")]
pub struct BleEventError {
    pub category: BleErrorCategory,
    pub message: String,
    pub platform: Option<PlatformErrorDetails>,
}

#[boltffi::error]
#[derive(Clone, Debug, PartialEq, Eq, derive_more::Display, derive_more::Error)]
pub enum BleHubError {
    #[display("backend assembly failed: {detail}")]
    BackendAssembly { detail: String },
    #[display("backend cleanup failed: {detail}")]
    BackendCleanup {
        detail: String,
        platform: Option<PlatformErrorDetails>,
    },
    #[display("backend resource is busy: {detail}")]
    ResourceBusy { detail: String },
    #[display("invalid configuration `{field}`: {detail}")]
    InvalidConfiguration { field: String, detail: String },
    #[display("FFI contract error: {detail}")]
    FfiContract { detail: String },
}

impl From<UnexpectedFfiCallbackError> for BleHubError {
    fn from(error: UnexpectedFfiCallbackError) -> Self {
        Self::FfiContract {
            detail: error.to_string(),
        }
    }
}

#[boltffi::error]
#[derive(Clone, Debug, PartialEq, Eq, derive_more::Display, derive_more::Error)]
pub enum CommandDispatchError {
    #[display("BleHub is closed; cannot dispatch command")]
    Closed,
    #[display("invalid command request: {detail}")]
    InvalidRequest { detail: String },
    #[display("invalid backend state: {detail}")]
    InvalidState { detail: String },
    #[display("backend unavailable for command dispatch: {detail}")]
    BackendUnavailable {
        detail: String,
        platform: Option<PlatformErrorDetails>,
    },
    #[display("unexpected foreign backend exception: {detail}")]
    FfiContract { detail: String },
}

impl From<UnexpectedFfiCallbackError> for CommandDispatchError {
    fn from(error: UnexpectedFfiCallbackError) -> Self {
        Self::FfiContract {
            detail: error.to_string(),
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, derive_more::Display, derive_more::Error)]
pub enum EventSinkError {
    #[display("downstream rejected the event: {message}")]
    Rejected { message: String },
}
