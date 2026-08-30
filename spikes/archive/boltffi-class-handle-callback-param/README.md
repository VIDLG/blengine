# BoltFFI foreign callback class-handle parameter spike

This isolated Phase 0 fixture tests whether a foreign implementation of a Rust trait can receive an exported Rust class handle as a method parameter.

BleHub's preferred assembly requires this shape:

```rust
#[boltffi::export]
pub trait BleBackend: Send + Sync {
    fn attach(&self, sink: BackendEventSinkHandle);
}
```

Run from the BleHub repository root:

```text
cargo check --manifest-path spikes/archive/boltffi-class-handle-callback-param/Cargo.toml
```

Expected result on the currently inspected local BoltFFI revision: compilation fails with `unsupported expansion: callback method parameter shape`.

This is a compile-capability fixture, not production BleHub code. Once BoltFFI supports the shape, the expected result should change to a successful build and this fixture should become a regression test.
