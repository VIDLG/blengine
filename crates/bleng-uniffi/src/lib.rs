//! BLeng Stable UniFFI component assembly.
//!
//! Final Stable UniFFI artifact assembly for BLeng.
//!
//! The async runtime and foreign-facing API live in `bleng`; the canonical
//! synchronous contract lives in `blehub`. This crate owns the BLeng package's
//! final native image and bindings, combining both components without making
//! `blehub-uniffi` depend on BLeng.

pub use blehub;
pub use blehub::*;
pub use bleng::{Bleng, BlengError};
pub use bleng_supervisor::{
    ForeignAdapterEnd, ForeignAdapterEndReason, ForeignAdapterObservation,
    ForeignAdapterObservationBeginResult, ForeignCompanionAssociationBeginResult,
    ForeignExternalRecoverySignal, ForeignOwnerContext, ForeignOwnerStart,
    ForeignPlatformActionResult, ForeignPlatformOwnerSession, ForeignPlatformOwnerSessionFactory,
    ForeignPlatformOwnerSessionFactoryAdapter, ForeignPlatformRecoveryAction,
    ForeignRecoveryAction, ForeignRecoveryLink, ForeignRecoveryMode, ForeignRecoveryObservation,
    ForeignRecoveryOutcome, ForeignRecoveryPlan, ForeignRecoveryPolicyConfig,
    ForeignRecoverySignal, ForeignRecoveryTarget, ForeignResourceCount, ForeignSupervisorConfig,
    SupervisedAdapterObservation, SupervisedCompanionAssociation, Supervisor, SupervisorFfiError,
};

blehub::uniffi_reexport_scaffolding!();
bleng::uniffi_reexport_scaffolding!();
bleng_supervisor::uniffi_reexport_scaffolding!();
