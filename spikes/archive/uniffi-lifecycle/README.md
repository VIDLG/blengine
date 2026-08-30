# UniFFI lifecycle comparison spike

This isolated Phase 0 fixture tests the BleHub-specific shape that BoltFFI issue
#869 currently rejects:

```rust
#[uniffi::export(foreign)]
trait BleBackend: Send + Sync {
    fn bind_event_ingress(
        &self,
        ingress: Arc<BackendEventIngressHandle>,
    ) -> Result<(), BleHubError>;
}
```

The stable UniFFI Kotlin/JNA backend is used deliberately. The experimental
`uniffi-bindgen-kotlin-jni` backend is assessed from local source but is not
mixed into this fixture.

Run from the BleHub root:

```text
pixi run just ffi-spike-test-uniffi-kotlin-jvm
pixi run just ffi-spike-test-uniffi-swift-windows
```

On a macOS host with Xcode, run the Apple runtime and packaging gates through the
Pixi-managed `apple-ci` environment:

```text
pixi run -e apple-ci just ffi-spike-test-uniffi-swift-macos
pixi run -e apple-ci just ffi-spike-test-uniffi-apple-package-consumer
```

The runtime fixtures cover direct Rust object retention by foreign backends,
foreign and Rust-created callback threads, declared and unexpected callback
failures, concurrent pushes, dispose waiting for an admitted callback, late
events, representative payloads, and 10,000 create/dispose cycles with Rust
and foreign-object lifecycle assertions.

The Apple packaging gate builds static Rust libraries for macOS, iOS device, and
iOS simulator architectures; assembles and inspects an XCFramework; runs a clean
high-level Swift Package consumer on macOS; and compile-links the package for
generic iOS simulator and device destinations without code signing. It does not
claim CoreBluetooth or real-device radio behavior.
