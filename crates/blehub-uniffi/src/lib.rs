//! Production Stable UniFFI artifact assembly for BleHub.
//!
//! The canonical contract and runtime behavior live in `blehub`. This crate
//! deliberately owns only the final native library and binding-generation
//! entry point used by production language packages.

pub use blehub;
pub use blehub::*;
