#![cfg(windows)]

mod backend;
mod connection;
mod error;
mod gatt;
mod initialization;
mod mapping;
mod resource_accounting;
mod scan;

pub use backend::WindowsBackend;
#[cfg(feature = "resource-accounting")]
#[doc(hidden)]
pub use resource_accounting::{
    WindowsCallbackResourceSnapshot, WindowsLogicalResourceSnapshot, WindowsResourceProbe,
};
