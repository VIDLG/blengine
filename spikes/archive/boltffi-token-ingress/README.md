# BoltFFI production-shaped token ingress fixture

This Phase 0 fixture validates the current BleHub token-ingress object graph through bindings generated and packaged by the pinned official crates.io `boltffi_cli 0.30.1`. It is not a production backend and does not restore the historical builder/foreign-sink design.

The exported `Phase0Fixture` is test-only. It accepts the production `BleBackend` foreign trait, creates a Rust-only recording `BleEventSink`, and delegates assembly, commands, event routing, and disposal to the real `blehub::BleHub`. Kotlin retains only the generated `BackendEventIngressToken` and calls the production top-level `pushBackendEvent(token, event)` function.

## Run on Windows

Use the Pixi-managed JDK, Gradle, `clang-cl`, `lld-link`, and the Rust MSVC target:

```text
pixi run just ffi-phase0-test-kotlin-jvm
```

Both Gradle projects resolve plugins and dependencies only through Aliyun mirrors in their `settings.gradle.kts` files. The producer compiles generated Kotlin and creates `blehub-phase0-token-ingress-bindings.jar`; the separate `kotlin-jvm-consumer` project compiles against only that JAR and the produced native DLL directory. The JNI C bridge and Rust DLL share the Windows MSVC ABI. Neither project requires a globally installed JDK, Gradle, Clang, or LLD.

Run the supplementary Windows Swift lane with the explicitly bootstrapped shared toolchain:

```text
pixi run just swift-bootstrap
pixi run just ffi-phase0-test-swift-windows
```

The lane regenerates the complete Swift source and C declarations, builds the same fixture as an MSVC Rust DLL, compiles a standalone Swift executable, and exercises the production-shaped foreign backend/token ingress path plus 10,000 ARC release cycles. It does not replace the macOS gates for CoreBluetooth, Apple queue behavior, Objective-C interoperability, XCFramework/SPM packaging, signing, or simulator/device behavior.

## Run on Android

Bootstrap and verify the project-managed SDK/NDK explicitly, then run the API 36 ART/AAR lane:

```text
pixi run just android-bootstrap
pixi run just android-avd-bootstrap
pixi run just ffi-phase0-test-android-emulator
```

The producer packages `arm64-v8a` and `x86_64` native libraries into an AAR. A separate Android project consumes only that AAR. Gradle plugin and dependency repositories are restricted to the configured Aliyun mirrors. This emulator lane validates FFI, VM attachment, concurrency, lifecycle, and packaging; it does not validate BLE hardware or RF behavior.

## Known BoltFFI Kotlin issues

Unmodified BoltFFI `0.30.1` Kotlin output does not compile when a generated `#[boltffi::error]` data-enum variant contains `message: String`: the generated property hides `Throwable.message` without `override`. The standalone reproducer and requested compile regression are tracked upstream as [BoltFFI #871](https://github.com/boltffi/boltffi/issues/871).

The fixture names diagnostic fields on exported error variants `detail`, so its raw generated Kotlin compiles without a post-generation patch. Data records that do not extend Kotlin `Exception`, including `BleFailure` and `PlatformErrorDetails`, retain their semantically appropriate `message` fields.

Official `0.30.1` also clears unchecked Kotlin exceptions from foreign callbacks returning Rust `Result<(), E>` and reports success to Rust. The two production-shaped JVM tests remain enabled and failing, and the upstream blocker is tracked as [BoltFFI #872](https://github.com/boltffi/boltffi/issues/872). Android now runs the explicit-ACK exception probe, but that validates BleHub's candidate containment mechanism rather than closing the BoltFFI defect.

`UnitResultExceptionWorkaroundTest` contains twelve passing comparison cases. Non-Unit return-slot ACKs are rejected: a closed enum produced an invalid discriminant and aborted Rust, while primitive/record outputs are left unwritten after an unchecked exception. An API 36 x86_64 emulator observed stale `true` and falsely accepted the record ACK even though desktop JVM and an arm64-v8a device happened to observe `false`.

The surviving candidate uses an independent one-shot `ForeignCallAckToken`: Rust registers it before the Unit callback, the foreign implementation calls exported `acknowledgeForeignCall(token)` only at the accepted linearization point, and Rust checks/removes it afterward. Missing ACK and 10,000 unchecked exceptions become `CommandDispatchError.FfiContract`; explicit ACK succeeds; a declared typed error before ACK remains unchanged. This passes on desktop JVM, x86_64 ART, and arm64-v8a ART. It remains probe evidence only; production `BleBackend` requires a separate contract review and broader lifecycle/concurrency/Swift validation before adopting this pattern.

Generated files, native libraries, Gradle output, and test reports are ignored build artifacts. Durable conclusions are recorded in `docs/research/phase0-token-ingress-runtime-results.md`.
