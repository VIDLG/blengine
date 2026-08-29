//! Public data types used by BleHub commands, events, errors, and backend assembly.
//!
//! The defining modules remain under [`crate::contract`] and [`crate::ingress`]
//! because BoltFFI-generated bindings rely on their canonical paths. This module
//! is a stable, discoverable Rust-facing namespace; the same items are also
//! available from the crate root.

pub use crate::contract::{
    BackendCapabilities, BleErrorCategory, BleEvent, BleEventError, BleEventPayload, BleHubError,
    CommandDispatchError, CommandId, CommandKind, ConnectCommand, ConnectionId, ConnectionState,
    EventSinkError, PeripheralId, Platform, PlatformErrorDetails, ScanSessionId, ScanStopReason,
    StartScanCommand,
};
pub use crate::ingress::{BackendEventIngressToken, EventIngressStatus};
