//! Stable UniFFI-facing exports for the BleHub contract.
//!
//! The canonical records, enums, backend trait, ingress object, and Hub object
//! remain owned by their domain modules. This module only provides one explicit
//! ABI-facing aggregation point and does not duplicate the contract.

pub use crate::backend::BleBackend;
pub use crate::contract::*;
pub use crate::hub::BleHub;
pub use crate::ingress::{
    BackendEventIngressHandle, BleEventSink, EventIngressStatus, EventSinkError,
};
