# BoltFFI production-shaped token ingress fixture

This Phase 0 fixture validates the current BleHub token-ingress object graph through generated Kotlin/JNI bindings. It is not a production backend and does not restore the historical builder/foreign-sink design.

The exported `Phase0Fixture` is test-only. It accepts the production `BleBackend` foreign trait, creates a Rust-only recording `BleEventSink`, and delegates assembly, commands, event routing, and disposal to the real `blehub::BleHub`. Kotlin retains only the generated `BackendEventIngressToken` and calls the production top-level `pushBackendEvent(token, event)` function.

## Run on Windows

Use the Pixi-managed JDK, Gradle, Rust GNU target, and LLVM-MinGW bridge toolchain:

```text
pixi run just ffi-phase0-test-kotlin-jvm
```

Both Gradle projects resolve plugins and dependencies only through Aliyun mirrors in their `settings.gradle.kts` files. The producer compiles generated Kotlin and creates `blehub-phase0-token-ingress-bindings.jar`; the separate `kotlin-jvm-consumer` project compiles against only that JAR and the produced native DLL directory. Neither project requires a globally installed JDK or Gradle.

Run the supplementary Windows Swift lane with the explicitly bootstrapped shared toolchain:

```text
pixi run just swift-bootstrap
pixi run just ffi-phase0-test-swift-windows
```

The lane regenerates the complete Swift source and C declarations, builds the same fixture as an MSVC Rust DLL, compiles a standalone Swift executable, and exercises the production-shaped foreign backend/token ingress path plus 10,000 ARC release cycles. It does not replace the macOS gates for CoreBluetooth, Apple queue behavior, Objective-C interoperability, XCFramework/SPM packaging, signing, or simulator/device behavior.

## Current BoltFFI workaround

Unmodified BoltFFI `0.30.1` Kotlin output does not compile when a generated `#[boltffi::error]` data-enum variant contains `message: String`: the generated property hides `Throwable.message` without `override`.

`tools/workaround_boltffi_kotlin_error_message.py` patches only generated Kotlin error subclasses by adding `override`. It preserves the reviewed Rust/FFI field name and becomes a no-op after the generator emits valid code. A run that needs this patch is workaround-assisted evidence, not proof that stock BoltFFI generation passes.

Generated files, native libraries, Gradle output, and test reports are ignored build artifacts. Durable conclusions are recorded in `docs/research/phase0-token-ingress-runtime-results.md`.
