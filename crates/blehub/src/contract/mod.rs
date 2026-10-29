mod advertisement;
mod capabilities;
mod commands;
mod errors;
mod events;
mod gatt;
mod ids;

pub use advertisement::{AdvertisementObservation, ManufacturerData, ServiceData};
pub use capabilities::{
    AdapterSnapshot, AdapterState, AuthorizationState, BLEHUB_CONTRACT_VERSION,
    BackendCapabilities, Platform, blehub_contract_version,
};
pub use commands::{
    CancelOperationRequest, CommandKind, ConnectCommand, DisconnectCommand, DiscoverGattCommand,
    InitializeCommand, MAX_SCAN_NAME_PREFIX_BYTES, MAX_WRITE_VALUE_BYTES, NotificationAction,
    NotificationMode, NotificationModeRequest, ReadCharacteristicCommand, ReadRssiCommand,
    ScanOptions, SetNotificationsCommand, StartScanCommand, StopScanCommand,
    WriteCharacteristicCommand, WriteType,
};
pub use errors::{
    BleErrorCategory, BleEventError, BleHubError, CommandDispatchError, PlatformErrorDetails,
};
pub use events::{
    BleEvent, ConnectionState, ConnectionStateResult, NotificationState, ScanStopReason,
    ScanStopResult,
};
pub use gatt::{
    CharacteristicProperties, GattCharacteristic, GattDatabase, GattDescriptor, GattService,
};
pub use ids::{
    BluetoothUuid, CommandId, ConnectionId, GattCharacteristicId, GattDescriptorId, GattServiceId,
    GattSessionId, PeripheralId, ScanSessionId, SubscriptionId,
};
