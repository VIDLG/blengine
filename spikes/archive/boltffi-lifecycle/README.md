# BoltFFI lifecycle spike

This isolated historical Phase 0 project tests the old buildable form of BleHub's bidirectional long-lived object graph without implementing BLE. It uses the exact crates.io `boltffi 0.30.1` release and is not the production-shaped token-ingress fixture.

The current BoltFFI revision rejects a Rust exported class handle as a foreign trait method parameter. This spike therefore uses two-phase assembly:

```text
Rust BleHubBuilder retains foreign BleEventSink
-> builder.backend_event_sink() returns a Rust class handle
-> Kotlin/Swift backend attaches that handle locally
-> builder.finish(backend) passes the foreign BleBackend to Rust
-> backend pushes through the retained Rust handle
-> Rust synchronously invokes the foreign BleEventSink
```

This exercises useful runtime ownership directions. The separate `boltffi-class-handle-callback-param` fixture records the unsupported preferred direct-handle shape, but BleHub's current Plan uses one-step `BleHub::new` plus token ingress; this builder graph remains historical evidence only.

Run the Rust-only checks through the default Pixi environment:

```text
pixi run just ffi-spike-test-lifecycle
pixi run just ffi-spike-generate-kotlin
```

Run the Windows Kotlin/JVM bridge and runtime tests through the JVM environment:

```text
pixi run -e jvm just ffi-spike-test-kotlin-jvm
```

The JVM bridge uses Pixi-managed `clang-cl` and `lld-link` with Rust's `x86_64-pc-windows-msvc` target. This keeps the Windows JVM and Swift host lanes on the same native ABI without coupling JVM validation to the separately installed Swift toolchain.

The checked-in Gradle Wrapper downloads Gradle 8.11 from the Huawei Cloud mirror and verifies its SHA-256 digest. Gradle build output remains ignored.

The Kotlin/JVM suite covers foreign backend/sink retention, synchronous and asynchronous callbacks, a Rust-created callback thread, typed errors, callback serialization, close drain, late events, 10,000 assembly/dispose cycles, and representative payload sizes. It also records a current BoltFFI No-Go blocker: unchecked Kotlin exceptions from either foreign callback are cleared by generated JNI glue and reported as success/`Delivered`. `sinkExceptionProbe` runs that fault in a separate JVM process.

Apple generation and runtime tests belong to the production-shaped token-ingress fixture and its macOS GitHub Actions lane.
