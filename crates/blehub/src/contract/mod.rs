mod capabilities;
mod commands;
mod errors;
mod events;
mod ids;

pub use capabilities::{BLEHUB_CONTRACT_VERSION, BackendCapabilities, Platform};
pub use commands::{CommandKind, ConnectCommand, StartScanCommand};
pub use errors::{
    BleFailure, BleHubError, BleOperationError, CommandDispatchError, ErrorCategory,
    EventSinkError, PlatformErrorDetails,
};
pub use events::{BleEvent, BleEventPayload, CommandOutcome, ConnectionPhase};
pub use ids::{CommandId, ConnectionId, PeripheralId, ScanSessionId};
