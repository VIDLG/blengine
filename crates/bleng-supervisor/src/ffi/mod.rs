mod adapter;
mod adapter_view;
mod companion_presence_view;
mod connection_view;
mod failure;
mod gatt_view;
mod notification_view;
mod object;
mod scan;
mod scan_session;
mod scan_view;
mod types;

pub use adapter::{
    ForeignPlatformOwnerSession, ForeignPlatformOwnerSessionFactory,
    ForeignPlatformOwnerSessionFactoryAdapter,
};
pub use adapter_view::{
    ForeignAdapterEnd, ForeignAdapterEndReason, ForeignAdapterObservation,
    ForeignAdapterObservationBeginResult, SupervisedAdapterObservation,
};
pub use companion_presence_view::{
    ForeignCompanionPresence, ForeignCompanionPresenceBeginResult, ForeignCompanionPresenceEnd,
    ForeignCompanionPresenceEndReason, ForeignCompanionPresenceObservation,
    SupervisedCompanionPresenceObservation,
};
pub use connection_view::{
    ForeignConnectBeginResult, ForeignConnectResult, ForeignConnectionEnd, ForeignConnectionEndBy,
    ForeignConnectionEndObservation, ForeignDisconnectBeginResult, SupervisedConnect,
    SupervisedConnection, SupervisedDisconnect,
};
pub use failure::{
    ForeignOperationCommandResult, ForeignOperationFailure, ForeignOperationFailureDomain,
};
pub use gatt_view::{
    ForeignBondBeginResult, ForeignBondProgress, ForeignBondValueResult,
    ForeignDescriptorReadBeginResult, ForeignDescriptorReadValueResult,
    ForeignDescriptorWriteBeginResult, ForeignGattDiscoveryBeginResult, ForeignGattDiscoveryResult,
    ForeignMtuBeginResult, ForeignMtuValueResult, ForeignPriorityBeginResult,
    ForeignReadBeginResult, ForeignReadValueResult, ForeignRssiBeginResult, ForeignRssiValueResult,
    ForeignWriteBeginResult, SupervisedBond, SupervisedConnectionPriority,
    SupervisedDescriptorRead, SupervisedDescriptorWrite, SupervisedGattCharacteristic,
    SupervisedGattDescriptor, SupervisedGattDiscovery, SupervisedGattService,
    SupervisedGattSession, SupervisedMtu, SupervisedRead, SupervisedRssi, SupervisedWrite,
};
pub use notification_view::{
    ForeignNotificationDeliverySnapshot, ForeignNotificationDisableResult, ForeignNotificationEnd,
    ForeignNotificationEndReason, ForeignNotificationObservation,
    ForeignNotificationSubscribeBeginResult, ForeignNotificationSubscribeResult,
    SupervisedNotificationDisable, SupervisedNotifications,
};
pub use object::{
    Supervisor,
    operations::{
        ForeignAdapterControlBeginResult, ForeignCompanionAssociationBeginResult,
        ForeignDeviceListResult, ForeignDeviceQueryBeginResult, ForeignDeviceRecord,
        ForeignRefreshBeginResult, SupervisedAdapterControl, SupervisedCompanionAssociation,
        SupervisedDeviceQuery, SupervisedRefreshGattCache,
    },
};
pub use scan::{ForeignScanEventEnvelope, SessionEventProjection};
pub use scan_session::{ForeignScanDeliverySnapshot, SupervisedScan};
pub use scan_view::{
    ForeignAndroidScanOptions, ForeignManufacturerDataFilter, ForeignPeripheralAdoptResult,
    ForeignScanAdvertisement, ForeignScanBeginResult, ForeignScanEnd, ForeignScanEndReason,
    ForeignScanObservation, ForeignScanOptions, ForeignScanStopResult, SupervisedPeripheral,
    SupervisedScanStop,
};
pub use types::{
    ForeignExternalRecoverySignal, ForeignOwnerClose, ForeignOwnerCloseFailure,
    ForeignOwnerContext, ForeignOwnerStart, ForeignPlatformActionResult,
    ForeignPlatformRecoveryAction, ForeignRecoveryAction, ForeignRecoveryLink, ForeignRecoveryMode,
    ForeignRecoveryObservation, ForeignRecoveryOutcome, ForeignRecoveryPlan,
    ForeignRecoveryPolicyConfig, ForeignRecoverySignal, ForeignRecoveryTarget,
    ForeignResourceCount, ForeignSupervisorConfig, SupervisorFfiError,
};

// FFI-surface integration suites live inside the crate: they drive the exported objects but also
// assert the typed steps underneath, which are implementation rather than public API.
#[cfg(test)]
#[path = "tests/ffi_scan.rs"]
mod ffi_scan;
