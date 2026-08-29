# Phase 0 token ingress runtime results

- Status: partial Phase 0 mechanism evidence; the complete Phase 0 gate is not closed
- Date: 2026-08-29
- Normative source: [`.plan/PLAN.md`](../../.plan/PLAN.md)
- Execution catalog: [`docs/testing/ffi-spikes.md`](../testing/ffi-spikes.md)
- Fixture: [`spikes/boltffi-token-ingress/`](../../spikes/boltffi-token-ingress/)

## 1. Conclusion

The representative production-shaped Rust token ingress runs successfully through both desktop Kotlin/JVM and Swift on Windows. The fixture uses the real `blehub::BleHub`, real process-global `BackendEventIngressRegistry`, real `EventIngressState`, real public command/event/error types, Kotlin and Swift implementations of the real foreign `BleBackend`, and a Rust-only recording `BleEventSink`.

The Windows JVM mechanism suite passed 21 tests, including 10,000 complete foreign-backend assembly/dispose cycles; a separate artifact-only Gradle consumer test also passed. The supplementary Windows Swift lane compiled the complete generated binding with its generated C module and an MSVC Rust DLL, then passed synchronous and Rust-thread callbacks, 64 concurrent pushes, idempotent dispose, stale-token rejection, and 10,000 unique-token ARC release cycles. No executed test observed a token collision, stale-token misdelivery, double backend cleanup, callback admission after close, command/close use-after-free, or panic/unexpected Kotlin exception crossing the ABI.

This is not yet a Phase 0 Go decision. Kotlin generation currently needs a narrowly scoped workaround for BoltFFI's `message` property output; the project still needs a reproducibly pinned clean generator source for CI; macOS Swift/CoreBluetooth-adjacent ARC and queue behavior, Apple packaging, final counter coverage, and performance gates remain open.

`Phase 0` is a project execution gate, not a runtime capability or ABI version, so production contract symbols do not encode phase names. `BLEHUB_CONTRACT_VERSION` is the single backend compatibility value and changes only for an incompatible contract revision, not when the project advances to another implementation phase. Root generated Kotlin exposes it as `blehubContractVersion`; the multi-crate Phase 0 fixture exports `fixtureContractVersion` as a compile-time alias because BoltFFI does not otherwise merge an unused dependency constant into that fixture's bindings.

## 2. Exact environment and evidence boundary

| Item | Value |
|---|---|
| Host | Windows 11 10.0 amd64 |
| Rust | `rustc 1.95.0 (59807616e 2026-04-14)` |
| JDK | Azul OpenJDK `17.0.18+8-LTS` from Pixi |
| Gradle | `8.11.1` from Pixi |
| Kotlin Gradle plugin | `2.1.0` |
| Swift | `6.3.3`, `x86_64-unknown-windows-msvc`, assertions build; official WinGet `Swift.Toolchain` installed under `D:/dev/swift` |
| Windows native tools | VS Build Tools `17.14`, MSVC `14.44.35207`, Windows SDK `10.0.26100.0` |
| Rust `boltffi` dependency | crates.io exact `0.30.1`, locked by each fixture `Cargo.lock` |
| BoltFFI CLI base revision | `8f862c8dd35ec9d8437ac0212c001baf0d700dd2`; a clean `git archive` snapshot is retained in ignored work storage |
| Current generated Kotlin SHA-256 after the temporary override patch | `d880a1a5cb65c64b2f75828cfe0cea942ac775086d74340cc26a4c4b55f070fa` |
| Current generated JNI glue SHA-256 | `115f5b87a2d767cfe621259ed08d685705b3bb865d9ec6d85e9f9fc34ebc7d1f` |

The sibling BoltFFI worktree was not clean and already contained user callback/JNI changes. To avoid attributing those changes to the base revision, `git archive HEAD` exported a clean `8f862c8...` snapshot into BleHub's ignored `.work` directory. Before the contract-version alias was added, that clean snapshot regenerated the candidate, the known Kotlin `message` workaround changed only its intended fields, and both the 21-case mechanism suite and artifact consumer passed with `--rerun-tasks`. The clean and sibling-worktree outputs had identical post-workaround Kotlin and JNI hashes for that earlier candidate, so the typed and unchecked Kotlin exception findings are not based solely on the dirty worktree.

The current hashes above include the subsequently exported contract-version alias. They were regenerated and tested successfully with the sibling CLI, but clean-snapshot regeneration of this exact candidate could not be repeated: online resolution failed at the configured `rsproxy.cn` TLS handshake, and offline resolution lacked locked `uuid 1.26.0`. The failed attempts did not rewrite the generated files. Therefore clean-versus-sibling hash equality for the current exact artifact remains unverified and is part of the immutable-generator blocker; the report does not attribute the current hashes to a clean source tree.

CI still needs an explicit immutable generator source/revision and complete dependency availability rather than depending on whichever state a sibling checkout or network mirror happens to have.

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

The generated Kotlin/JNI mechanism lane passed 21 tests with zero failures or skips:

- capabilities typed failure rollback;
- invalid contract version rollback before bind;
- bind typed failure without token retention;
- push during bind returns `Closed` because registration has not occurred;
- unchecked capabilities and bind exceptions map to `BleHubError.FfiContract` and roll back, including the clean generator rerun;
- synchronous push before command return;
- push from a JVM executor worker;
- Rust-created caller thread attaches to JVM and re-enters Rust through top-level push;
- typed dispatch rejection returns zero events;
- unchecked command exception maps to `CommandDispatchError.FfiContract`;
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

The 21-test mechanism report completed in approximately `0.69s` on the recorded host. A second Gradle project then consumed only `blehub-phase0-token-ingress-bindings.jar` plus the produced native DLL directory in a fresh test JVM; its single bind/push/dispose smoke test passed and confirmed `Phase0Fixture` was loaded from a JAR. These timings are smoke observations, not benchmark results.

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

## 5. BoltFFI findings

### 5.1 Multi-crate public type paths

BoltFFI can merge dependency contracts and generate a harness API that accepts the production `BleBackend` and uses production DTO/error types. Its macro expansion uses defining canonical module paths, so those modules must be publicly nameable from the harness crate.

### 5.2 `thiserror` marker conflict

Using the unqualified `#[error(...)]` helper attributes required by `thiserror` on a record error in the BoltFFI-scanned production crate caused BoltFFI `0.30.1` source scanning to report `invalid BoltFFI marker error(...)`; the standalone reproducer and source-level cause are tracked upstream as [BoltFFI #870](https://github.com/boltffi/boltffi/issues/870). `thiserror` would still implement the standard `std::error::Error` trait; the conflict is source-scanner syntax, not error semantics. The current code preserves typed `#[boltffi::error]` wire enums and derives the same `Display`/`std::error::Error` behavior with `derive_more`, whose `#[display(...)]` attributes passed Rust compilation plus Kotlin and Swift binding generation. This is a verified compile-time workaround, not a reason to collapse errors into strings.

### 5.3 Kotlin `message` override bug

Raw generated Kotlin fails compilation for data-enum error variants containing `message: String`. BoltFFI renders a sealed error class extending `Exception`, then renders each payload property as plain `val message`; Kotlin requires `override val message` because `Throwable` already declares that property.

The fixture applies `tools/workaround_boltffi_kotlin_error_message.py` after generation. The script:

- operates only on a specified generated Kotlin file;
- changes only unqualified `message: String` properties inside generated sealed `Exception` subclasses;
- changed eight fields in the current ABI;
- preserves all Rust and wire field names;
- reports a no-op when a future generator already emits valid properties.

Because the raw output does not compile, typed Kotlin errors remain a production blocker until BoltFFI fixes the template or BleHub pins a reviewed generator fork. Renaming the reviewed public `message` fields merely to avoid a generator bug is not recommended.

## 6. Build and source policy

The JVM lane runs through Pixi:

```text
pixi run just ffi-phase0-test-kotlin-jvm
```

`tools/build_windows_jvm_bridge.py` now accepts `--project`, reads the native package name from that project's `boltffi.toml`, builds the Rust GNU DLL, and compiles the generated JNI C bridge with the managed LLVM-MinGW toolchain.

Both Gradle projects use the Pixi JDK and Gradle. Plugin and dependency resolution are restricted to Aliyun mirrors in `settings.gradle.kts`; no official Gradle Plugin Portal or Maven Central repository is configured. The producer compiles generated source and creates a bindings JAR. The separate consumer has no generated-source directory on its source set and compiles only against that JAR, while loading the produced Rust/JNI DLLs from the native artifact directory.

## 7. Remaining blockers and next gates

The following remain unverified or incomplete:

1. pin an immutable clean BoltFFI generator source/revision in project tooling and CI instead of depending on a sibling worktree state;
2. replace the Kotlin `message` post-generation workaround with an upstream/fork generator fix and an expected-failure regression probe;
3. add foreign registration-failure injection, named-barrier strong/weak counts, peak-entry, native-wrapper final-free, and all-object-baseline counters across the JVM and Swift lanes;
4. run fault cases in isolated subprocesses and verify process exit/cleanup records;
5. run the remaining Swift ARC/queue/close-race/exception cases plus clean XCFramework/SPM consumer tests on macOS; Windows host evidence does not close this Apple gate;
6. add payload-boundary vectors and recorded performance/close-latency workloads;
7. later run Android ART, Handler/Looper, SDK/NDK, AAR, emulator, and device gates before Android production work.

Until these items are closed, the evidence supports continuing Phase 0 implementation but does not authorize freezing the public ABI or starting production platform backends.
