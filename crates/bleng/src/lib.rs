//! Async BLeng facade boundary for BleHub.
//!
//! This crate owns caller-facing async lifecycle, waiter, timeout,
//! cancellation, and connection/GATT projection semantics without changing
//! the synchronous `blehub` contract. The public BLeng ABI remains unfrozen.
//!
//! Kameo is used only inside this crate's private runtime owner; its actor and
//! Tokio types do not cross the UniFFI facade.

#![deny(unsafe_code)]

mod cleanup;
mod error;
mod facade;
pub mod ffi;
mod operation_observation;
mod runtime;
mod waiter;

pub use operation_observation::{OperationFailureObserver, OperationFailureRegistration};

uniffi::setup_scaffolding!();
pub use error::{
    BlengBusyReason, BlengContractFailure, BlengError, BlengNotReadyReason, BlengResource,
};
pub use facade::{
    Bleng, ConnectOperation, DisableNotificationsOperation, DisconnectOperation,
    DiscoverGattOperation, EnableNotificationsOperation, InitializeOperation, ReadOperation,
    ReadRssiOperation, StartScanOperation, StopScanOperation, WriteOperation,
};
