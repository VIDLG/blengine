//! Rust session orchestration above one-generation BLeng owners.
//!
//! This crate contains the pure recovery state machine, Rust session orchestration,
//! and the native platform boundary. Native adapters remain responsible for creating
//! platform BLE objects.

mod error;
mod platform;
mod recovery;
mod scan;
mod scan_delivery;
mod supervisor;

#[cfg(feature = "ffi")]
mod ffi;

pub use error::{RecoveryTargetError, SupervisorError, SupervisorOperationError};
pub use platform::{
    PlatformActionResult, PlatformOwnerSession, PlatformOwnerSessionFactory,
    PlatformRecoveryAction, RecoveryTarget, ResourceSnapshot,
};
pub use recovery::*;
pub use scan::{ScanEvent, ScanEventEnvelope, SessionScanProjection};
pub use scan_delivery::{
    MAX_SCAN_ADVERTISEMENTS, MAX_SCAN_DATA_BYTES, ScanDelivery, ScanDeliveryError,
    ScanDeliverySnapshot, ScanHandoff, ScanSubmissionError,
};
pub use supervisor::{OwnerStart, SessionSupervisor, SupervisorConfig};

#[cfg(feature = "ffi")]
pub use ffi::{
    ForeignExternalRecoverySignal, ForeignOwnerContext, ForeignOwnerStart,
    ForeignPlatformActionResult, ForeignPlatformOwnerSession, ForeignPlatformOwnerSessionFactory,
    ForeignPlatformOwnerSessionFactoryAdapter, ForeignPlatformRecoveryAction,
    ForeignRecoveryAction, ForeignRecoveryLink, ForeignRecoveryMode, ForeignRecoveryObservation,
    ForeignRecoveryOutcome, ForeignRecoveryPlan, ForeignRecoveryPolicyConfig,
    ForeignRecoverySignal, ForeignRecoveryTarget, ForeignResourceCount, ForeignScanAdvertisement,
    ForeignScanBeginResult, ForeignScanCommandResult, ForeignScanDeliverySnapshot, ForeignScanEnd,
    ForeignScanEndReason, ForeignScanEventEnvelope, ForeignScanFailure, ForeignScanFailureDomain,
    ForeignScanObservation, ForeignScanOptions, ForeignScanStopResult, ForeignSupervisorConfig,
    ScanEventProjection, ScanSessionError, SupervisedPeripheral, SupervisedScan,
    SupervisedScanStop, Supervisor, SupervisorFfiError,
};

#[cfg(feature = "ffi")]
uniffi::setup_scaffolding!();
