//! Rust session orchestration above one-generation BLeng owners.
//!
//! This crate contains the pure recovery state machine, Rust session orchestration,
//! and the native platform boundary. Native adapters remain responsible for creating
//! platform BLE objects.

mod adapter_delivery;
mod bond_progress;
mod companion_presence;
mod connection_delivery;
mod error;
mod notification_delivery;
mod observation;
mod platform;
mod pull_guard;
mod recovery;
mod scan;
mod scan_delivery;
mod supervisor;

#[cfg(feature = "ffi")]
mod ffi;

pub use adapter_delivery::{
    AdapterDelivery, AdapterDeliveryError, AdapterHandoff, AdapterSubmissionError,
    MAX_PENDING_SNAPSHOTS,
};
pub use bond_progress::{
    BondHandoff, BondProgressDelivery, BondProgressEnd, BondProgressError, BondProgressStep,
    BondSubmissionError, MAX_BOND_STATES,
};
pub use companion_presence::{
    CompanionPresence, CompanionPresenceDelivery, CompanionPresenceDeliveryError,
    CompanionPresenceHandoff, CompanionPresenceSubmissionError, MAX_PENDING_PRESENCE,
};
pub use connection_delivery::{
    ConnectionEnd, ConnectionEndBy, ConnectionHandoff, ConnectionLane, ConnectionLaneError,
    ConnectionSubmissionError,
};
pub use error::{RecoveryTargetError, SupervisorError, SupervisorOperationError};
pub use notification_delivery::{
    MAX_NOTIFICATION_BYTES, MAX_NOTIFICATION_VALUES, NotificationDelivery,
    NotificationDeliveryError, NotificationDeliverySnapshot, NotificationEnd,
    NotificationEndReason, NotificationEvent, NotificationHandoff, NotificationSource,
    NotificationSubmissionError,
};
pub use observation::SessionObservationProjection;
pub use platform::{
    PlatformActionResult, PlatformOwnerSession, PlatformOwnerSessionFactory,
    PlatformRecoveryAction, RecoveryTarget, ResourceSnapshot,
};
pub use recovery::*;
pub use scan::{ScanEvent, ScanEventEnvelope};
pub use scan_delivery::{
    MAX_SCAN_ADVERTISEMENTS, MAX_SCAN_DATA_BYTES, ScanDelivery, ScanDeliveryError,
    ScanDeliverySnapshot, ScanHandoff, ScanSubmissionError,
};
pub use supervisor::{OwnerStart, SessionSupervisor, SupervisorConfig};

#[cfg(feature = "ffi")]
pub use ffi::{
    ForeignAdapterControlBeginResult, ForeignAdapterEnd, ForeignAdapterEndReason,
    ForeignAdapterObservation, ForeignAdapterObservationBeginResult, ForeignAndroidScanOptions,
    ForeignBondBeginResult, ForeignBondProgress, ForeignBondValueResult,
    ForeignCompanionAssociationBeginResult, ForeignCompanionPresence,
    ForeignCompanionPresenceBeginResult, ForeignCompanionPresenceEnd,
    ForeignCompanionPresenceEndReason, ForeignCompanionPresenceObservation,
    ForeignConnectBeginResult, ForeignConnectResult, ForeignConnectionEnd, ForeignConnectionEndBy,
    ForeignConnectionEndObservation, ForeignDescriptorReadBeginResult,
    ForeignDescriptorReadValueResult, ForeignDescriptorWriteBeginResult, ForeignDeviceListResult,
    ForeignDeviceQueryBeginResult, ForeignDeviceRecord, ForeignDisconnectBeginResult,
    ForeignExternalRecoverySignal, ForeignGattDiscoveryBeginResult, ForeignGattDiscoveryResult,
    ForeignManufacturerDataFilter, ForeignMtuBeginResult, ForeignMtuValueResult,
    ForeignNotificationDeliverySnapshot, ForeignNotificationDisableResult, ForeignNotificationEnd,
    ForeignNotificationEndReason, ForeignNotificationObservation,
    ForeignNotificationSubscribeBeginResult, ForeignNotificationSubscribeResult,
    ForeignOperationCommandResult, ForeignOperationFailure, ForeignOperationFailureDomain,
    ForeignOwnerContext, ForeignOwnerStart, ForeignPeripheralAdoptResult,
    ForeignPlatformActionResult, ForeignPlatformOwnerSession, ForeignPlatformOwnerSessionFactory,
    ForeignPlatformOwnerSessionFactoryAdapter, ForeignPlatformRecoveryAction,
    ForeignPriorityBeginResult, ForeignReadBeginResult, ForeignReadValueResult,
    ForeignRecoveryAction, ForeignRecoveryLink, ForeignRecoveryMode, ForeignRecoveryObservation,
    ForeignRecoveryOutcome, ForeignRecoveryPlan, ForeignRecoveryPolicyConfig,
    ForeignRecoverySignal, ForeignRecoveryTarget, ForeignRefreshBeginResult, ForeignResourceCount,
    ForeignRssiBeginResult, ForeignRssiValueResult, ForeignScanAdvertisement,
    ForeignScanBeginResult, ForeignScanDeliverySnapshot, ForeignScanEnd, ForeignScanEndReason,
    ForeignScanEventEnvelope, ForeignScanObservation, ForeignScanOptions, ForeignScanStopResult,
    ForeignSupervisorConfig, ForeignWriteBeginResult, SessionEventProjection,
    SupervisedAdapterControl, SupervisedAdapterObservation, SupervisedBond,
    SupervisedCompanionAssociation, SupervisedCompanionPresenceObservation, SupervisedConnect,
    SupervisedConnection, SupervisedConnectionPriority, SupervisedDescriptorRead,
    SupervisedDescriptorWrite, SupervisedDeviceQuery, SupervisedDisconnect,
    SupervisedGattCharacteristic, SupervisedGattDescriptor, SupervisedGattDiscovery,
    SupervisedGattService, SupervisedGattSession, SupervisedMtu, SupervisedNotificationDisable,
    SupervisedNotifications, SupervisedPeripheral, SupervisedRead, SupervisedRefreshGattCache,
    SupervisedRssi, SupervisedScan, SupervisedScanStop, SupervisedWrite, Supervisor,
    SupervisorFfiError,
};

#[cfg(feature = "ffi")]
uniffi::setup_scaffolding!();
