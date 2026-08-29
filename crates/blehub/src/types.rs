//! Public data types used by BleHub commands, events, errors, and backend assembly.
//!
//! The defining modules remain under [`crate::contract`] and [`crate::ingress`]
//! because BoltFFI-generated bindings rely on their canonical paths. This module
//! is a stable, discoverable Rust-facing namespace; the same items are also
//! available from the crate root.

pub use crate::contract::{
    BackendCapabilities, BleEvent, BleEventPayload, BleFailure, BleHubError, BleOperationError,
    CommandDispatchError, CommandId, CommandKind, CommandOutcome, ConnectCommand, ConnectionId,
    ConnectionPhase, ErrorCategory, EventSinkError, PeripheralId, Platform, PlatformErrorDetails,
    ScanSessionId, StartScanCommand,
};
pub use crate::ingress::{BackendEventIngressToken, EventIngressStatus};
