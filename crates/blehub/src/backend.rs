use std::sync::Arc;

use crate::{
    AssociateCompanionDeviceCommand, BackendCapabilities, BackendEventIngressHandle, BleHubError,
    CancelOperationRequest, CommandDispatchError, ConnectCommand, CreateBondCommand,
    DisconnectCommand, DiscoverGattCommand, InitializeCommand, MtuCommand, QueryDevicesCommand,
    ReadCharacteristicCommand, ReadDescriptorCommand, ReadRssiCommand, RefreshGattCacheCommand,
    RemoveBondCommand, SetAdapterEnabledCommand, SetConnectionPriorityCommand,
    SetNotificationsCommand, StartScanCommand, StopScanCommand, WriteCharacteristicCommand,
    WriteDescriptorCommand,
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

    /// Requests a radio power change where the platform supports it. Success only means the
    /// request was accepted; the new state must arrive through the adapter observation lane.
    fn set_adapter_enabled(
        &self,
        command: SetAdapterEnabledCommand,
    ) -> Result<(), CommandDispatchError>;

    /// Reads a read-only system inventory (connected or bonded devices). Results are not
    /// connectable capabilities. Platforms without the requested inventory reject the command the
    /// same way any other unsupported capability is rejected.
    fn query_devices(&self, command: QueryDevicesCommand) -> Result<(), CommandDispatchError>;

    /// Requests one companion-device association. Success only means the platform reported an
    /// association; it is neither a present device nor a link. Platforms without a companion model
    /// reject the command with `CommandDispatchError::NotSupported`.
    fn associate_companion_device(
        &self,
        command: AssociateCompanionDeviceCommand,
    ) -> Result<(), CommandDispatchError>;

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

    fn read_descriptor(&self, command: ReadDescriptorCommand) -> Result<(), CommandDispatchError>;

    fn write_descriptor(&self, command: WriteDescriptorCommand)
    -> Result<(), CommandDispatchError>;

    fn set_notifications(
        &self,
        command: SetNotificationsCommand,
    ) -> Result<(), CommandDispatchError>;

    fn read_rssi(&self, command: ReadRssiCommand) -> Result<(), CommandDispatchError>;

    /// Queries current link MTU/payload limits, or requests an MTU where the platform supports it.
    /// `request: None` must not perform native negotiation.
    fn mtu(&self, command: MtuCommand) -> Result<(), CommandDispatchError>;

    /// Requests a link scheduling preference where the platform supports it.
    /// Success only means the platform accepted the request; it is not a measured link mode.
    fn set_connection_priority(
        &self,
        command: SetConnectionPriorityCommand,
    ) -> Result<(), CommandDispatchError>;

    /// Requests bonding (pairing) where the platform supports it.
    /// A successful terminal is the platform-reported `Bonded`/failure state, never an assumption.
    /// Backends that cannot initiate bonding reject the command instead of pretending success.
    fn create_bond(&self, command: CreateBondCommand) -> Result<(), CommandDispatchError>;

    /// Requests bond removal where the platform supports it.
    /// A successful terminal is the platform-reported `None` bond state.
    fn remove_bond(&self, command: RemoveBondCommand) -> Result<(), CommandDispatchError>;

    /// Requests invalidation of the platform's cached GATT topology for a connection. A backend
    /// that cannot invalidate its cache rejects the command instead of pretending a refresh.
    fn refresh_gatt_cache(
        &self,
        command: RefreshGattCacheCommand,
    ) -> Result<(), CommandDispatchError>;

    /// Requests cancellation of an accepted operation. `Ok(())` only means
    /// that the backend serialized the request; native work may still be
    /// stopping and the target keeps its original command identity.
    fn cancel_operation(&self, request: CancelOperationRequest)
    -> Result<(), CommandDispatchError>;

    /// Stops native work and releases backend-owned resources, including ingress.
    /// Called synchronously by Hub shutdown or assembly rollback. This method,
    /// its synchronous callbacks, and backend Drop/deinit must not directly or
    /// indirectly wait for the owning Hub's `dispose` to finish: Hub shutdown
    /// cannot complete until they return. Backend-local idempotent cleanup is
    /// distinct from recursively closing the owning Hub.
    fn dispose(&self) -> Result<(), BleHubError>;
}
