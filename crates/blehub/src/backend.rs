use std::sync::Arc;

use crate::{
    BackendCapabilities, BackendEventIngressHandle, BleHubError, CancelOperationRequest,
    CommandDispatchError, ConnectCommand, DisconnectCommand, DiscoverGattCommand,
    InitializeCommand, ReadCharacteristicCommand, ReadRssiCommand, SetNotificationsCommand,
    StartScanCommand, StopScanCommand, WriteCharacteristicCommand,
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

    fn initialize(&self, command: InitializeCommand) -> Result<(), CommandDispatchError>;

    fn start_scan(&self, command: StartScanCommand) -> Result<(), CommandDispatchError>;

    fn stop_scan(&self, command: StopScanCommand) -> Result<(), CommandDispatchError>;

    fn connect(&self, command: ConnectCommand) -> Result<(), CommandDispatchError>;

    fn disconnect(&self, command: DisconnectCommand) -> Result<(), CommandDispatchError>;

    fn discover_gatt(&self, command: DiscoverGattCommand) -> Result<(), CommandDispatchError>;

    fn read_characteristic(
        &self,
        command: ReadCharacteristicCommand,
    ) -> Result<(), CommandDispatchError>;

    fn write_characteristic(
        &self,
        command: WriteCharacteristicCommand,
    ) -> Result<(), CommandDispatchError>;

    fn set_notifications(
        &self,
        command: SetNotificationsCommand,
    ) -> Result<(), CommandDispatchError>;

    fn read_rssi(&self, command: ReadRssiCommand) -> Result<(), CommandDispatchError>;

    /// Requests cancellation of an accepted operation. `Ok(())` only means
    /// that the backend serialized the request; native work may still be
    /// stopping and the target keeps its original command identity.
    fn cancel_operation(&self, request: CancelOperationRequest)
    -> Result<(), CommandDispatchError>;

    fn dispose(&self) -> Result<(), BleHubError>;
}
