//! Rust session orchestration above one-generation BLeng owners.
//!
//! This crate contains the pure recovery state machine, Rust session orchestration,
//! and the native platform boundary. Native adapters remain responsible for creating
//! platform BLE objects.

mod error;
mod platform;
mod recovery;
mod supervisor;

#[cfg(feature = "ffi")]
mod ffi;

pub use error::{RecoveryTargetError, SupervisorError};
pub use platform::{
    PlatformActionResult, PlatformOwnerSession, PlatformOwnerSessionFactory,
    PlatformRecoveryAction, RecoveryTarget, ResourceSnapshot,
};
pub use recovery::*;
pub use supervisor::{SessionSupervisor, SupervisorConfig};

#[cfg(feature = "ffi")]
pub use ffi::{
    ForeignOwnerContext, ForeignPlatformActionResult, ForeignPlatformOwnerSession,
    ForeignPlatformOwnerSessionFactory, ForeignPlatformOwnerSessionFactoryAdapter,
    ForeignPlatformRecoveryAction, ForeignRecoveryAction, ForeignRecoveryLink,
    ForeignRecoveryObservation, ForeignRecoveryPlan, ForeignRecoveryPolicyConfig,
    ForeignRecoverySignal, ForeignRecoveryTarget, ForeignResourceCount, ForeignSupervisorConfig,
    Supervisor, SupervisorFfiError,
};

#[cfg(feature = "ffi")]
uniffi::setup_scaffolding!();
