use std::sync::Arc;

use crate::{
    BackendCapabilities, BackendEventIngressHandle, BleHubError, CommandDispatchError,
    ConnectCommand, StartScanCommand,
};

/// Platform implementation boundary. Rust implementations must not panic:
/// BleHub propagates Rust panics normally rather than translating them into
/// typed fallback errors. UniFFI maps declared and unexpected foreign
/// exceptions through the methods' `Result` error channels.
#[uniffi::export(foreign)]
pub trait BleBackend: Send + Sync {
    fn capabilities(&self) -> Result<BackendCapabilities, BleHubError>;

    /// Takes the backend's sole long-lived ingress root. Implementations may
    /// create temporary clones only for queued or in-flight callbacks and must
    /// release their root before `dispose` returns, including error paths.
    fn bind_event_ingress(
        &self,
        ingress: Arc<BackendEventIngressHandle>,
    ) -> Result<(), BleHubError>;

    fn start_scan(&self, command: StartScanCommand) -> Result<(), CommandDispatchError>;

    fn connect(&self, command: ConnectCommand) -> Result<(), CommandDispatchError>;

    fn dispose(&self) -> Result<(), BleHubError>;
}
