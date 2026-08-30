//! Public data types used by BleHub commands, events, errors, and backend assembly.
//!
//! The defining modules remain under [`crate::contract`] and [`crate::ingress`].
//! This module is a stable, discoverable Rust-facing namespace; the same items
//! are also available from the crate root.

pub use crate::contract::{
    AdapterSnapshot, AdapterState, AuthorizationState, BackendCapabilities, BleErrorCategory,
    BleEvent, BleEventError, BleHubError, BluetoothUuid, CommandDispatchError, CommandId,
    CommandKind, ConnectCommand, ConnectionId, ConnectionState, ConnectionStateResult,
    DisconnectCommand, DiscoverGattCommand, GattCharacteristicId, GattDescriptorId, GattServiceId,
    GattSessionId, InitializeCommand, NotificationAction, NotificationMode,
    NotificationModeRequest, PeripheralId, Platform, PlatformErrorDetails,
    ReadCharacteristicCommand, ReadRssiCommand, ScanOptions, ScanSessionId, ScanStopReason,
    ScanStopResult, SetNotificationsCommand, StartScanCommand, StopScanCommand, SubscriptionId,
    WriteCharacteristicCommand, WriteType,
};
pub use crate::ingress::{BackendEventIngressHandle, EventIngressStatus, EventSinkError};
