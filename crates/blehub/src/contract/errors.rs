use boltffi::UnexpectedFfiCallbackError;

use super::{CommandKind, Platform};

#[boltffi::data]
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum ErrorCategory {
    Permission,
    AdapterUnavailable,
    InvalidState,
    NotFound,
    Timeout,
    Cancelled,
    Connection,
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
pub struct BleFailure {
    pub category: ErrorCategory,
    pub message: String,
    pub platform: Option<PlatformErrorDetails>,
}

#[boltffi::data]
#[derive(Clone, Debug, PartialEq, Eq, derive_more::Display, derive_more::Error)]
#[display("{operation:?}: {failure}")]
pub struct BleOperationError {
    pub operation: CommandKind,
    pub failure: BleFailure,
}

#[boltffi::error]
#[derive(Clone, Debug, PartialEq, Eq, derive_more::Display, derive_more::Error)]
pub enum BleHubError {
    #[display("backend assembly failed: {message}")]
    BackendAssembly { message: String },
    #[display("backend cleanup failed: {message}")]
    BackendCleanup {
        message: String,
        platform: Option<PlatformErrorDetails>,
    },
    #[display("backend resource is busy: {message}")]
    ResourceBusy { message: String },
    #[display("FFI contract error: {message}")]
    FfiContract { message: String },
}

impl From<UnexpectedFfiCallbackError> for BleHubError {
    fn from(error: UnexpectedFfiCallbackError) -> Self {
        Self::FfiContract {
            message: error.to_string(),
        }
    }
}

#[boltffi::error]
#[derive(Clone, Debug, PartialEq, Eq, derive_more::Display, derive_more::Error)]
pub enum CommandDispatchError {
    #[display("BleHub is closed; cannot dispatch {operation:?}")]
    Closed { operation: CommandKind },
    #[display("invalid {operation:?} request: {message}")]
    InvalidRequest {
        operation: CommandKind,
        message: String,
    },
    #[display("invalid backend state for {operation:?}: {message}")]
    InvalidState {
        operation: CommandKind,
        message: String,
    },
    #[display("backend unavailable for {operation:?} dispatch: {message}")]
    BackendUnavailable {
        operation: CommandKind,
        message: String,
        platform: Option<PlatformErrorDetails>,
    },
    #[display("unexpected foreign backend exception: {message}")]
    FfiContract { message: String },
}

impl From<UnexpectedFfiCallbackError> for CommandDispatchError {
    fn from(error: UnexpectedFfiCallbackError) -> Self {
        Self::FfiContract {
            message: error.to_string(),
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, derive_more::Display, derive_more::Error)]
pub enum EventSinkError {
    #[display("downstream rejected the event: {message}")]
    Rejected { message: String },
}
