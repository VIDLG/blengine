//! Async BLeng facade boundary for BleHub.
//!
//! This crate owns caller-facing async lifecycle, waiter, timeout,
//! cancellation, and connection/GATT projection semantics without changing
//! the synchronous `blehub` contract. The public BLeng ABI remains unfrozen.
//!
//! Kameo is used only inside this crate's private runtime owner; its actor and
//! Tokio types do not cross the UniFFI facade.

#![deny(unsafe_code)]

mod api;
mod cleanup;
mod error;
mod runtime;
mod waiter;

uniffi::setup_scaffolding!();
pub use api::Bleng;
pub use error::BlengError;
