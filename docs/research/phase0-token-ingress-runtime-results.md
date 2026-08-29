# Phase 0 token ingress runtime results

- Status: partial Phase 0 mechanism evidence; the complete Phase 0 gate is not closed
- Date: 2026-08-29
- Normative source: [`.plan/PLAN.md`](../../.plan/PLAN.md)
- Execution catalog: [`docs/testing/ffi-spikes.md`](../testing/ffi-spikes.md)
- Fixture: [`spikes/boltffi-token-ingress/`](../../spikes/boltffi-token-ingress/)

## 1. Conclusion

The representative production-shaped Rust token ingress runs successfully through Swift on Windows and through Android ART on an API 36 emulator and API 36 arm64 device. The fixture uses the real `blehub::BleHub`, process-global `BackendEventIngressRegistry`, `EventIngressState`, public command/event/error types, foreign implementations of the real `BleBackend`, and a Rust-only recording `BleEventSink`.

The official crates.io `boltffi` and `boltffi_cli` release `0.30.1` now drives every fixture; no build depends on the sibling BoltFFI checkout. The supplementary Windows Swift lane passes synchronous and Rust-thread callbacks, 64 concurrent pushes, idempotent dispose, stale-token rejection, and 10,000 unique-token ARC release cycles. The Android lane packages an AAR for `arm64-v8a` and `x86_64`, runs the production-shaped callback/lifecycle smoke on ART, and validates a clean AAR-only consumer.

This is not yet a Phase 0 Go decision. The original production-shaped desktop JVM lane passes 19 of 21 tests, but two unchecked Kotlin exception cases are cleared and reported to Rust as success; this production blocker is tracked as [BoltFFI #872](https://github.com/boltffi/boltffi/issues/872). Android testing disproved the earlier return-value ACK candidate: generated JNI does not initialize the non-Unit `return_out` slot on the unchecked-exception path, and x86_64 ART observed a stale `true` while arm64 ART happened to observe `false`. Primitive and record sentinels are therefore unsafe regardless of host tests. A separate explicit token-ACK callback probe passes on desktop JVM, x86_64 ART, and arm64 ART without reading that return slot. The production `BleBackend` contract has not yet adopted this workaround candidate. The `message` generator defect is avoided without generated-source patching by using `detail` on exported error variants and remains tracked as [#871](https://github.com/boltffi/boltffi/issues/871). The FFI-focused macOS Swift runtime/ARC and XCFramework/SPM consumer gate passed in authoritative GitHub Actions [run `33249751300`](https://github.com/VIDLG/blehub/actions/runs/33249751300); it does not validate CoreBluetooth or device BLE behavior. Final counter, isolated fault, payload, and performance coverage also remains open.

`Phase 0` is a project execution gate, not a runtime capability or ABI version, so production contract symbols do not encode phase names. `BLEHUB_CONTRACT_VERSION` is the single backend compatibility value and changes only for an incompatible contract revision, not when the project advances to another implementation phase. Root generated Kotlin exposes it as `blehubContractVersion`; the multi-crate Phase 0 fixture exports `fixtureContractVersion` as a compile-time alias because BoltFFI does not otherwise merge an unused dependency constant into that fixture's bindings.

## 2. Exact environment and evidence boundary

| Item | Value |
|---|---|
| Host | Windows 11 10.0 amd64 |
| Rust | `rustc 1.95.0 (59807616e 2026-04-14)` |
| JDK | Azul OpenJDK `17.0.18+8-LTS` from Pixi |
| Gradle | `8.11.1` from Pixi |
| Kotlin Gradle plugin | JVM `2.1.0`; Android `2.1.20` |
| Swift | `6.3.3`, `x86_64-unknown-windows-msvc`, assertions build; official WinGet `Swift.Toolchain` installed under `D:/dev/swift` |
| Windows native tools | Pixi Clang/LLD `22.1.8`; VS Build Tools `17.14`, MSVC `14.44.35207`, Windows SDK `10.0.26100.0` |
| Android | API 36 x86_64 emulator and API 36 arm64-v8a device; build-tools `36.0.0`; NDK `28.2.13676358`; AGP `8.10.1`; Gradle `8.11.1`; JDK 17 |
| Rust `boltffi` dependency | crates.io exact `0.30.1`, locked by each fixture `Cargo.lock` |
| BoltFFI CLI | official crates.io `boltffi_cli 0.30.1`, installed by `tools/boltffi_toolchain.py`; Cargo verifies the crates.io package checksum |

The CLI is installed into ignored project work storage through an isolated Cargo home, so it does not depend on the sibling source checkout or the machine's global Cargo mirror configuration. `tools/run_boltffi_cli.py` verifies and invokes that executable for Kotlin/Swift generation and Android/Apple packaging. Generated source and package output remain ignored artifacts and are always reproducible from the exact fixture contract plus the pinned official CLI.

## 3. Implemented object graph

`spikes/boltffi-token-ingress` is a separate test harness crate. It depends on `crates/blehub` instead of copying registry or Hub implementation. Its exported `Phase0Fixture` performs only test adaptation:

```text
Kotlin TestBackend
  -> generated Box<dyn BleBackend>
  -> Phase0Fixture
  -> real BleHub::new
  -> real global token registry -> Weak<EventIngressState>
  -> Rust-only RecordingSink

Kotlin callback thread
  -> generated pushBackendEvent(token, BleEvent)
  -> real Rust push_backend_event
  -> real registry lookup and Weak upgrade
  -> real EventIngressState::push
  -> Rust-only RecordingSink::on_event
```

The fixture adds counters, sink behavior injection, a Rust-created caller thread, and deterministic callback barriers. It does not expose the downstream Rust sink to Kotlin and does not use `BleHubBuilder` or a Rust pointer encoded as a token.

Making `blehub::backend` and `blehub::ingress` public modules was required because BoltFFI multi-crate macro expansion references the defining canonical module paths. Internal registry/state modules and helpers remain private or `pub(crate)`; the public items are the same contract items already re-exported from the crate root.

## 4. Verified results

### 4.1 Pure Rust

The workspace currently passes 17 unit tests:

- 10,000 registry registration/unregistration cycles return to baseline;
- token `0` registration is rejected;
- 16 threads allocate 16,000 nonzero unique tokens;
- near-overflow allocation returns the final representable sequence value and then fails repeatedly without wrapping;
- a registry `Weak` entry does not retain ingress state and routes to `Closed` after the strong owner drops;
- duplicate registration cannot replace an existing entry;
- invalid events are rejected before downstream delivery;
- downstream panic is contained and poisoned delivery state can still close;
- close waits for an admitted in-flight callback;
- capabilities failure, invalid contract version, and bind failure each dispose once, drop the sink, and leave no isolated registry entry;
- registration failure after successful bind closes the new sink, disposes and unbinds the backend once, and leaves the pre-existing entry unchanged;
- synchronous callback before command return is delivered;
- ordinary dispatch rejection emits no event;
- event-before-dispatch-rejection is recorded as a backend contract violation;
- late event after dispose returns `Closed` and cleanup remains once.

### 4.2 Kotlin/JVM

With official BoltFFI `0.30.1`, the original generated Kotlin/JNI mechanism lane passes 19 of 21 tests. Twelve additional comparison/workaround-probe tests pass, so the complete Gradle run contains 31 passing and two deliberately retained #872 failures. The original passing cases cover:

- capabilities typed failure rollback;
- invalid contract version rollback before bind;
- bind typed failure without token retention;
- push during bind returns `Closed` because registration has not occurred;
- unchecked capabilities exception mapping to `BleHubError.FfiContract` and rollback;
- synchronous push before command return;
- push from a JVM executor worker;
- Rust-created caller thread attaches to JVM and re-enters Rust through top-level push;
- typed dispatch rejection returns zero events;
- downstream typed rejection returns structured `DownstreamFailed`;
- invalid event returns structured `InvalidEvent`;
- Rust sink panic is caught by the exported ingress containment boundary and active callback count returns to zero;
- 16 JVM workers produce 64 serialized sink calls with maximum active callback count one;
- dispose waits for a blocked admitted callback, then rejects a late push;
- dispose waits for a command holding the backend read gate;
- typed dispose failure still closes, unregisters, releases backend ownership, and permits idempotent repeated dispose;
- stale token cannot target a newly active Hub;
- explicit dispose is idempotent and releases the Rust sink once;
- 10,000 lifecycle tokens are unique and every foreign backend is disposed exactly once.

The two failing tests are `uncheckedBindExceptionBecomesFfiContractAndRollsBack` and `uncheckedCommandExceptionBecomesFfiContractWithoutCrossingTheAbi`. Generated Kotlin represents Rust `Result<(), E>` callbacks as `Unit`; when Kotlin throws an undeclared exception, official `0.30.1` clears it and Rust observes success rather than `UnexpectedFfiCallbackError`. The tests remain enabled and failing so this cannot be mistaken for a passing boundary. A separate Gradle artifact consumer is valid for ordinary bind/push/dispose behavior, but the complete JVM lane remains blocked by #872.

### 4.3 Swift on Windows

The project-owned `tools/swift_toolchain.py` pins the official WinGet `Swift.Toolchain` package to `6.3.3` and resolves the machine-local root through `BLEHUB_SWIFT_ROOT`, ignored `blehub.local.toml`, then the portable user cache. On the recorded machine, `D:/dev/swift` is the selected shared root. Bootstrap is explicit and idempotent; ordinary tests never install or upgrade the toolchain. The doctor verifies MSVC, the Windows SDK, Swift runtime loading, and an actual Swift typecheck.

`pixi run just ffi-phase0-test-swift-windows` regenerated the complete Swift source and C declarations without modifying BoltFFI, built the fixture as an `x86_64-pc-windows-msvc` DLL, compiled a standalone Swift executable against the generated Clang module and DLL import library, and ran it in a fresh process. The executable verified:

- the generated Swift source and C ABI declarations typecheck on Windows with Foundation and Objective-C interop disabled;
- Swift implements the real generated `BleBackend` protocol and Rust constructs the real `BleHub`;
- synchronous command-time push and Rust-created-thread callback both re-enter Rust successfully;
- 64 concurrent Swift pushes are delivered while the Rust sink reports maximum active callbacks one;
- repeated explicit dispose cleans the foreign backend once and late push returns `Closed`;
- 10,000 tokens are unique, every stale token remains closed, and every Swift backend becomes weakly unreachable after fixture disposal and ARC release.

This is supplementary host evidence, not an Apple package gate. It does not cover CoreBluetooth, Objective-C interoperability, Apple dispatch/CoreBluetooth queue semantics, Xcode, XCFramework/SPM consumption, signing, simulator/device behavior, or the final CoreBluetooth object graph.

### 4.4 Android ART and AAR

`pixi run just ffi-phase0-test-android-emulator` passed on both the API 36 x86_64 `medium_phone` emulator and a connected API 36 arm64-v8a device. The recipe uses the project-managed SDK/NDK root, official BoltFFI Android pack, Rust Android targets, and Aliyun-only Gradle repositories. It verified:

- generated Kotlin and packaged native libraries for `arm64-v8a` and `x86_64`;
- AAR assembly with the generated classes and native `.so` files;
- execution on ART with the Android main `Handler`/`Looper` available;
- a Rust-created thread attaching to the VM and calling the Kotlin backend;
- eight-worker concurrent ingress with serialization at the Rust downstream;
- dispose idempotence and stale-token rejection;
- 10,000 unchecked exceptions before explicit token acknowledgement becoming `FfiContract` on both ART ABIs, with explicit acknowledgement succeeding;
- a separate Android project consuming only the produced AAR, without generated-source leakage, on both devices.

This is an FFI/runtime/package mechanism result, not BLE evidence. Running the fixture on a physical device does not validate Bluetooth hardware, RF behavior, BLE permissions, vendor callback behavior, or a production Android BLE backend. The debug AAR emitted a non-blocking warning that native libraries could not be stripped and were packaged as-is; release symbol and strip policy remains a later packaging gate.

## 5. BoltFFI findings

### 5.1 Multi-crate public type paths

BoltFFI can merge dependency contracts and generate a harness API that accepts the production `BleBackend` and uses production DTO/error types. Its macro expansion uses defining canonical module paths, so those modules must be publicly nameable from the harness crate.

### 5.2 `thiserror` marker conflict

Using the unqualified `#[error(...)]` helper attributes required by `thiserror` on a record error in the BoltFFI-scanned production crate caused BoltFFI `0.30.1` source scanning to report `invalid BoltFFI marker error(...)`; the standalone reproducer and source-level cause are tracked upstream as [BoltFFI #870](https://github.com/boltffi/boltffi/issues/870). `thiserror` would still implement the standard `std::error::Error` trait; the conflict is source-scanner syntax, not error semantics. The current code preserves typed `#[boltffi::error]` wire enums and derives the same `Display`/`std::error::Error` behavior with `derive_more`, whose `#[display(...)]` attributes passed Rust compilation plus Kotlin and Swift binding generation. This is a verified compile-time workaround, not a reason to collapse errors into strings.

### 5.3 Kotlin `message` override bug

Raw generated Kotlin fails compilation for data-enum error variants containing `message: String`. BoltFFI renders a sealed error class extending `Exception`, then renders each payload property as plain `val message`; Kotlin requires `override val message` because `Throwable` already declares that property. The standalone reproducer and requested generation-plus-compilation regression are tracked upstream as [BoltFFI #871](https://github.com/boltffi/boltffi/issues/871).

BleHub avoids making its build depend on a generated-source patch: diagnostic fields on exported `#[boltffi::error]` variants are named `detail`. This is language-neutral diagnostic data and remains non-parseable text. `BleFailure.message` and `PlatformErrorDetails.message` remain unchanged because those types are ordinary data records rather than Kotlin `Exception` subclasses. The Phase 0 Kotlin lane compiles raw generated output, so it can detect a future generator regression independently of a workaround script.

BoltFFI should still fix the generator because `message` is a valid Rust error payload field and other projects should not need this naming convention. If #871 is fixed before BleHub freezes its ABI, the public naming can be reviewed again; no automatic rename follows from an upstream upgrade.

### 5.4 Unchecked Kotlin `Unit` callback exceptions

Official BoltFFI `0.30.1` does not preserve an undeclared Kotlin exception from a foreign callback whose Rust signature is `Result<(), E>`. The generated JNI path clears the exception and returns the void/zero success representation, so Rust cannot map it to `UnexpectedFfiCallbackError`. This violates BleHub's rule that unexpected foreign failures must never masquerade as successful construction or command dispatch. The minimal upstream reproducer and required regression behavior are tracked in [BoltFFI #872](https://github.com/boltffi/boltffi/issues/872).

BleHub will not patch generated code, use the sibling checkout, skip the tests, or reinterpret the observed success as acceptable.

### 5.5 Explicit token-ACK workaround probe

The fixture first compared three official `0.30.1` callback return shapes while deliberately throwing the same unchecked Kotlin exception:

1. `Result<(), E>` reproduces #872 and is incorrectly reported as success.
2. `Result<ClosedEnumAck, E>` is not a workaround: after the exception is cleared, an invalid enum discriminant reaches Rust and aborts at the non-unwinding FFI boundary.
3. `Result<bool, E>` and `Result<RecordAck { accepted: bool }, E>` appeared to produce an invalid-by-default `false` in desktop JVM tests, but this was accidental rather than contractual.

Generated JNI accepts a native `return_out` pointer for both primitive and record results. It calls Kotlin without initializing that slot; when an unchecked exception occurs, it clears the exception, skips the success writer, and returns an empty error buffer. The x86_64 API 36 emulator then observed a stale `true`, so Rust incorrectly accepted the callback, while the arm64-v8a device happened to observe `false`. Reading the unwritten result is not a portable or safe failure signal. Closed enums, booleans, records, magic values, and nonces in that return slot are all rejected as workaround designs.

The retained probe therefore adds a separate explicit acknowledgement path that does not depend on a foreign return value:

1. Rust allocates a one-shot `ForeignCallAckToken` and records it as pending before entering the foreign Unit callback.
2. The foreign implementation calls exported `acknowledge_foreign_call(token)` only after reaching the accepted linearization point.
3. Rust removes and checks the pending token after the generated callback returns. Normal return or swallowed unchecked exception without ACK becomes `CommandDispatchError::FfiContract`; a declared typed error before ACK remains unchanged.
4. ACK plus a declared rejection is contradictory and becomes `FfiContract`. Once ACK succeeds, later failure cannot retract acceptance.

Twelve focused desktop JVM cases pass, including 10,000 unchecked exceptions before ACK, missing ACK, explicit ACK success, and preservation of a declared typed error. The same 10,000-exception and explicit-success probe passes on both x86_64 and arm64-v8a ART. This is materially stronger evidence than a return-slot sentinel, but it adds one nested native call and one pending-token registration per foreign Unit-result invocation.

Before adoption, the production contract must define the ACK token and exported acknowledgement operation, apply them to `bind_event_ingress`, every foreign command method, and `dispose`, specify ACK as the accepted linearization point, and add rollback, duplicate/stale token, concurrency, cleanup-counter, Swift, and macOS package coverage. `capabilities()` already has a non-Unit result and does not use this workaround. The original #872 tests remain enabled because BoltFFI itself is still unfixed.

Phase 0 therefore remains blocked pending either an official BoltFFI fix or explicit review and production-shaped validation of this token-ACK workaround.

## 6. Build and source policy

All generation and packaging lanes use the pinned official CLI:

```text
pixi run just boltffi-bootstrap
pixi run just boltffi-doctor
pixi run just ffi-phase0-test-kotlin-jvm
pixi run just ffi-phase0-test-android-emulator
```

`boltffi-bootstrap` is explicit; ordinary generation and tests verify the CLI and fail with installation guidance rather than silently downloading it.

`tools/build_windows_jvm_bridge.py` accepts `--project`, reads the native package name from that project's `boltffi.toml`, builds the Rust DLL for `x86_64-pc-windows-msvc`, and compiles and links the generated JNI C bridge with Pixi-managed `clang-cl` and `lld-link`. The desktop JVM and Windows Swift host lanes therefore share the Windows MSVC ABI without making JVM validation depend on the separately installed Swift toolchain. This does not approximate or replace the later Android NDK/Bionic/ART lane.

Both Gradle projects use the Pixi JDK and Gradle. Plugin and dependency resolution are restricted to Aliyun mirrors in `settings.gradle.kts`; no official Gradle Plugin Portal or Maven Central repository is configured. The producer compiles generated source and creates a bindings JAR. The separate consumer has no generated-source directory on its source set and compiles only against that JAR, while loading the produced Rust/JNI DLLs from the native artifact directory.

## 7. Remaining blockers and next gates

The following remain unverified or incomplete:

1. either consume an official BoltFFI fix for #872, or review and apply the explicit token-ACK workaround to every foreign Unit-result method and rerun Kotlin/JVM, Android, Windows Swift, and macOS package lanes; return-value ACK sentinels are rejected;
2. keep #871 as an upstream generator regression while using the reviewed `detail` naming workaround without generated-source patching;
3. add foreign registration-failure injection, named-barrier strong/weak counts, peak-entry, native-wrapper final-free, and all-object-baseline counters across the JVM and Swift lanes;
4. run fault cases in isolated subprocesses and verify process exit/cleanup records;
5. execute the prepared macOS GitHub Actions lane and pass Swift ARC/queue runtime plus clean XCFramework/SPM consumer tests; Windows host evidence does not close this Apple gate;
6. add payload-boundary vectors and recorded performance/close-latency workloads;
7. later validate real Android device and BLE/HIL behavior when a production Android backend exists; the ART/AAR mechanism lane is already passing.

Until these items are closed, the evidence supports continuing Phase 0 implementation but does not authorize freezing the public ABI or starting production platform backends.
