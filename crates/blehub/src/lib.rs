pub mod assembly;
pub mod backend;
pub mod contract;
pub mod ffi;
mod hub;
pub mod ingress;
pub mod types;

pub use assembly::{BackendAssembly, attach_backend};
pub use backend::BleBackend;
pub use contract::*;
pub use hub::BleHub;
pub use ingress::{BackendEventIngressHandle, BleEventSink, EventIngressStatus, EventSinkError};

uniffi::setup_scaffolding!();
