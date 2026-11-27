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
    AndroidScanMode, AndroidScanOptions, AndroidScanPhy, AssociateCompanionDeviceCommand,
    BondState, CancelOperationRequest, CommandKind, ConnectCommand, ConnectionPriority,
    CreateBondCommand, DeviceQueryKind, DeviceRecord, DisconnectCommand, DiscoverGattCommand,
    InitializeCommand, MAX_COMPANION_PROFILE_BYTES, MAX_LOCAL_NAME_BYTES, MAX_MTU,
    MAX_SCAN_REPORT_DELAY_MS, MAX_WRITE_VALUE_BYTES, MIN_MTU, ManufacturerDataFilter, MtuCommand,
    MtuInfo, NotificationAction, NotificationMode, NotificationModeRequest, QueryDevicesCommand,
    ReadCharacteristicCommand, ReadDescriptorCommand, ReadRssiCommand, RefreshGattCacheCommand,
    RemoveBondCommand, ScanOptions, SetAdapterEnabledCommand, SetConnectionPriorityCommand,
    SetNotificationsCommand, StartScanCommand, StopScanCommand, WriteCharacteristicCommand,
    WriteDescriptorCommand, WriteType,
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
