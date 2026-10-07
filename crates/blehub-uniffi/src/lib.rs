//! Production Stable UniFFI artifact assembly for BleHub.
//!
//! The canonical contract and runtime behavior live in `blehub`. This crate
//! deliberately owns only the final native library and binding-generation
//! entry point used by production language packages.

// Re-export the canonical component from this BleHub-only final native image.
pub use blehub;
pub use blehub::*;

// Keep the component scaffolding in the final image when this crate is linked
// as a package assembly root.
blehub::uniffi_reexport_scaffolding!();
