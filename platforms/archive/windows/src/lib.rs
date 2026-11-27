#![cfg(windows)]

mod backend;
#[cfg(feature = "hil-callback-gates")]
mod callback_gates;
mod connection;
mod error;
mod gatt;
mod initialization;
mod mapping;
mod resource_accounting;
mod scan;

pub use backend::WindowsBackend;
#[cfg(feature = "hil-callback-gates")]
#[doc(hidden)]
pub use callback_gates::{WindowsHilCallback, WindowsHilCallbackGate};
#[cfg(feature = "resource-accounting")]
#[doc(hidden)]
pub use resource_accounting::{
    WindowsCallbackResourceSnapshot, WindowsLogicalResourceSnapshot, WindowsResourceProbe,
};
