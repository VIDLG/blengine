//! Public data types used by BleHub commands, events, errors, and backend assembly.
//!
//! The defining modules remain under [`crate::contract`] and [`crate::ingress`].
//! This module is a stable, discoverable Rust-facing namespace; the same items
//! are also available from the crate root.

pub use crate::contract::{
    BackendCapabilities, BleErrorCategory, BleEvent, BleEventError, BleEventPayload, BleHubError,
    CommandDispatchError, CommandId, CommandKind, ConnectCommand, ConnectionId, ConnectionState,
    ConnectionStateResult, EventSinkError, PeripheralId, Platform, PlatformErrorDetails,
    ScanSessionId, ScanStopReason, ScanStopResult, StartScanCommand,
};
pub use crate::ingress::{BackendEventIngressHandle, EventIngressStatus};
