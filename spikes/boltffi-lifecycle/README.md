# BoltFFI lifecycle spike

This isolated Phase 0 project tests the buildable form of BleHub's bidirectional long-lived object graph without implementing BLE.

The current BoltFFI revision rejects a Rust exported class handle as a foreign trait method parameter. This spike therefore uses two-phase assembly:

```text
Rust BleHubBuilder retains foreign BleEventSink
-> builder.backend_event_sink() returns a Rust class handle
-> Kotlin/Swift backend attaches that handle locally
-> builder.finish(backend) passes the foreign BleBackend to Rust
-> backend pushes through the retained Rust handle
-> Rust synchronously invokes the foreign BleEventSink
```

This exercises the intended runtime ownership directions. The separate `boltffi-class-handle-callback-param` fixture records the unsupported preferred `BleBackend::attach(handle)` shape, so the two-phase builder is the current plan rather than only a temporary fallback.

Run the Rust-only checks through the default Pixi environment:

```text
pixi run just ffi-spike-test-lifecycle
pixi run just ffi-spike-generate-kotlin
```

Run the Windows Kotlin/JVM bridge and runtime tests through the JVM environment:

```text
pixi run -e jvm just llvm-mingw-report
pixi run -e jvm just ffi-spike-test-kotlin-jvm
```

The JVM bridge uses the pinned LLVM-MinGW release managed by `tools/llvm_mingw.py`. Because conda-forge currently provides Rust's `x86_64-pc-windows-gnu` standard library but not the LLVM-specific `x86_64-pc-windows-gnullvm` component, the build creates project-local compatibility archives under `.work/`: LLVM `libunwind` satisfies `libgcc_eh`, and compiler-rt builtins satisfy `libgcc`. The shared LLVM-MinGW installation is not modified.

The checked-in Gradle Wrapper downloads Gradle 8.11 from the Huawei Cloud mirror and verifies its SHA-256 digest. Gradle build output remains ignored.

The Kotlin/JVM suite covers foreign backend/sink retention, synchronous and asynchronous callbacks, a Rust-created callback thread, typed errors, callback serialization, close drain, late events, 10,000 assembly/dispose cycles, and representative payload sizes. It also records a current BoltFFI No-Go blocker: unchecked Kotlin exceptions from either foreign callback are cleared by generated JNI glue and reported as success/`Delivered`. `sinkExceptionProbe` runs that fault in a separate JVM process.

Apple generation and runtime tests require a macOS environment and will be added to the remote Phase 0 lane.
