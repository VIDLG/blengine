use crate::{
    BackendCapabilities, BackendEventIngressToken, BleHubError, CommandDispatchError,
    ConnectCommand, StartScanCommand,
};

#[boltffi::export]
pub trait BleBackend: Send + Sync {
    fn capabilities(&self) -> Result<BackendCapabilities, BleHubError>;

    fn bind_event_ingress(&self, token: BackendEventIngressToken) -> Result<(), BleHubError>;

    fn start_scan(&self, command: StartScanCommand) -> Result<(), CommandDispatchError>;

    fn connect(&self, command: ConnectCommand) -> Result<(), CommandDispatchError>;

    fn dispose(&self) -> Result<(), BleHubError>;
}
