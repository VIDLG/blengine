# FFI runtime and artifact execution catalog

- Status: execution catalog; non-normative
- Scope: Stable UniFFI `0.32.0` direct-handle runtime validation against the single production image/package, and the complete ABI-candidate rerun required before the Phase 5 production ABI freeze
- Normative source: [`.plan/PLAN.md`](../../.plan/PLAN.md), especially sections 12.1, 12.5, and Phases 0 and 5
- Research basis: [FFI lifecycle and concurrency](../research/integrations/ffi-lifecycle-concurrency.md) and [UniFFI versus BoltFFI for BleHub](../research/integrations/uniffi-vs-boltffi-for-blehub.md)

This file converts the Plan's FFI requirements into runnable work. It does not define architecture or public semantics. `tests/ffi/runtime` must exercise the exported production API from the artifact assembled by `crates/blehub-uniffi`; it owns no Rust crate, test component, or second native image. A passing runtime gate proves the production ABI behavior it executes, while independent clean-consumer isolation and real BLE remain separate gates. The complete candidate must be rerun before ABI freeze. Durable conclusions belong in `docs/research/`, and normative changes require Plan review.

## 1. Current FFI architecture and historical baseline

The canonical Stable UniFFI direct-ingress object graph is:

```text
Rust, Swift, or Kotlin application
  strong -> production BleHub object or generated wrapper

BleHub
  strong -> Arc<dyn BleBackend>
  value  -> validated BackendCapabilities
  strong -> Arc<EventIngressState>

EventIngressState
  strong while open -> Arc<dyn BleEventSink>

foreign backend
  root strong -> generated BackendEventIngressHandle wrapper

BackendEventIngressHandle
  weak -> EventIngressState

native callback
  -> retained handle.push(event)
  -> Weak::upgrade
  -> EventIngressState::push
  -> BleEventSink::on_event
  -> direct Rust call or generated Swift/Kotlin callback
```

BleHub production routing has no project-authored token allocator, process-local registry, registration guard, free push function, raw pointer, or integerized `Arc`. UniFFI's generated opaque object handles and foreign callback handle maps remain framework-managed implementation details and must still pass lifecycle tests.

The repository owners for this graph are:

- `crates/blehub`: canonical definitions and exports for production `BleHub`, foreign `BleBackend`, foreign `BleEventSink`, direct ingress, commands, events, and errors;
- `crates/blehub-uniffi`: the only production final native image, locked bindgen entry, and production package staging;
- `tests/support/blehub-testkit`: reusable Rust-only test support;
`tests/ffi/runtime`: Swift and Android Kotlin code that directly implements test backend/sink objects and executes the production ABI.

Test behavior is supplied in the consuming language, not injected through a Rust test component. Every runtime process loads the same production image shape that ships; no production module or long-lived test depends on `spikes/`.

The following remain historical evidence only:

- `spikes/archive/uniffi-production-candidate/`, whose old object graph remains migration evidence only;
- `spikes/archive/boltffi-lifecycle/`;
- `spikes/archive/boltffi-token-ingress/`;
- `spikes/archive/boltffi-class-handle-callback-param/`;
- BoltFFI #869/#872 and token-ACK probes.

They remain useful for explaining the recorded regression workloads, performance comparison, and why the migration was selected. They are frozen under `spikes/archive/`, have no active recipes or CI jobs, do not define the production gate, and must not coexist with the UniFFI ingress in production code.

## 2. Evidence ledger

### 2.1 Verified mechanism evidence

| Evidence | Supports | Does not support |
|---|---|---|
| Current-candidate 2026-09-03 macOS migration rerun ([report](reports/2026-09-03-single-image-production-abi-migration.md)) | Single production image/package; exported production `BleHub`; direct Swift backend/sink implementation; Swift event catalog and concurrency/error/lifecycle cases; three Apple slices and symbol checks; Android AAR assembly/publish and macOS NDK native-slice builds | Archived Windows backend/consumer is no longer a current gate; remaining active gaps are Android ART, Android BLE, iPhone signing/install/runtime, RF, independent final artifact-only consumers, final performance, or ABI freeze |
| Historical `spikes/archive/uniffi-production-candidate/` runs on Windows Kotlin/JVM and Windows Swift `6.3.3` | Foreign backend, direct weak ingress, binding-phase rejection, typed/undeclared callback errors, close drain, root release, panic containment, Rust/native threads, and 10,000 lifecycle cycles in the pre-revision graph | Exported production `BleHub`, foreign sink callbacks, the single production image, current source paths, or final package/performance thresholds |
| Historical Android 16 physical arm64 (`MAG-AN00`) and x86_64 AVD runs | Normal UniFFI checksum initialization, ART callback paths, rollback/lifecycle cases, both Android ABIs, transitive JNA, and published-artifact loading in the pre-revision graph | The revised production ABI, Android Framework BLE callbacks, permissions, RF, vendor behavior, or release/minified production AAR |
| `spikes/archive/uniffi-lifecycle/` on desktop and Android hosts | Representative Stable UniFFI retention, direct Rust object parameters, selected races, payload/performance workloads, and packaging mechanisms | The production object graph by itself; it remains the mechanism baseline rather than production evidence |
| Historical, non-maintained GitHub Actions run [`33249751300`](https://github.com/VIDLG/blehub/actions/runs/33249751300) | Real macOS Swift runtime, retained direct object, ARC/lifecycle/error cases, XCFramework, clean macOS SPM consumer, and generic iOS simulator/device compile-link for the representative fixture | The final production object graph, any current gate, CoreBluetooth, signing, installation, BLE hardware, or RF; the hosted workflow has been removed |
| BoltFFI comparison fixtures | Historical direct-JNI performance and workaround behavior | Approval of token routing or ACK workaround for production |

### 2.2 Historical object-graph result and migration boundary

The 2026-08-29 object-graph rerun verified the following mechanisms in the pre-revision graph:

- the real weak ingress path reached a sink from Kotlin, Swift, Android ART, and a Rust-created caller thread;
- ingress remains in `Binding` until bind succeeds; push during bind returns `Closed`, records the violation, and rejects construction before any sink delivery;
- valid capabilities, typed/undeclared capabilities failures, invalid contract version, typed/undeclared bind failures, and retain-before-bind-error rollback;
- typed and undeclared command rejection produce no event;
- close waits for an admitted blocked callback, closes ingress before backend disposal, and makes a dispose-time or late push return `Closed`;
- backend disposal releases the sole long-lived ingress root before returning, including typed and undeclared cleanup-error paths;
- invalid event, sink rejection, and a Rust sink panic remained bounded by `EventIngressStatus`; the deliberate panic did not unwind across the ABI;
- 10,000 complete state/sink/backend cycles returned the observable counters to baseline in desktop Kotlin/JVM, Windows Swift, Android physical, and Android emulator lanes;
- Android loaded both `arm64-v8a` and `x86_64` published native artifacts rather than using a source-tree project dependency.

Those historical results alone do not validate the revised exported `BleHub` and foreign `BleEventSink` ABI. The maintained runtime now re-expresses the current applicable cases with direct Swift and Android Kotlin test backend/sink objects against the production image; the 2026-09-03 macOS rerun passed the Swift event catalog plus concurrency/error/lifecycle cases. Android ART remains a separate required gate. Desktop Kotlin/JVM and Windows Swift are historical, non-maintained evidence and are not current or future supplementary runtime gates.

The retired desktop Windows Kotlin/JNA lane had a carrier defect for checksum functions declared as C `uint16_t`; its workaround and checksum comparison tool are historical evidence only. The maintained Android Kotlin binding retains generated checksum checks, and generated bindings are not patched.

### 2.3 Still required before production ABI freeze

- Independent macOS and iOS production artifact-only consumers now pass staged-package assembly, direct Swift callback ingress, and explicit close on the project Mac and iPhone; see the [2026-09-25 Apple artifact checkpoint](reports/2026-09-25-apple-phase3-artifact-consumers.md). These are local, unfrozen-candidate gates, not RF or a complete Phase 3 exit. Verify candidate-specific manifest/lock metadata and rerun affected gates on the reviewed exit candidate; repository-owned runtime and package projects do not replace consumer isolation.
- Run the production AAR on Android ART with direct Kotlin backend/sink objects. The AAR assembly/publish and macOS NDK native slices pass, but physical execution did not start because APK installation timed out twice with `ShellCommandUnresponsiveException` while the phone was locked.
- Rerun the complete event catalog and owned payload boundaries whenever the public contract changes; the current Swift event catalog pass does not waive other applicable language/platform lanes.
- Complete remaining deterministic catalog cases, including explicit repeated bind, rollback-cleanup secondary diagnostics, broad state-sequence generation, and payload lifetime after the foreign frame returns.
- Record final absolute performance, close latency, package size, release/minified Android behavior, artifact manifests, and generator/runtime/ABI metadata checks.
- Rerun the complete applicable catalog from the final maintained Apple + Android candidate before the Phase 5 ABI freeze. Apple Swift runtime, package, backend, and HIL coverage runs on the project real Mac mini and includes independent macOS/iOS gates; it does not include a Windows Swift supplement. The 2026-09-03 post-migration Swift, Apple package, and Android package-build results are current-candidate evidence, not an ABI freeze, Android ART pass, iPhone pass, or RF pass; desktop Kotlin/JVM remains historical evidence only.

## 3. Required fixtures

| Fixture | Responsibility | Consumers |
|---|---|---|
| Direct-handle object graph | Exported Hub, inert backend, capabilities, one-time bind, weak ingress handle, foreign sink, Hub lifecycle | Pure Rust plus Android ART and macOS/iOS Swift production-ABI tests on the project real Mac mini |
| Error and panic boundary | Callable errors, event-error data, unexpected backend/sink errors, sink rejection, poison, panic containment | Rust, Android Kotlin/ART, Swift |
| Event wire | Every `BleEvent` variant, `ScanStopResult`, `ConnectionStateResult`, `NotificationState`, and optional terminal error | Rust, Android Kotlin/ART, Swift |
| Race/stress controller | Language-side test backend/sink, named barriers, watchdogs, counters, state sequences, 10,000 lifecycle cycles, payload bursts | Maintained Swift and Android Kotlin/ART runtime lanes |
| Production-ABI runtime consumers | Load the `crates/blehub-uniffi` artifact and exercise exported `BleHub` with direct language-side test backend/sink objects | Android ART/AAR and Swift/XCFramework/SPM |
| Production artifact-only consumers | Consume staged production artifacts only, without source-tree paths, test exports or implicit native outputs | `tests/consumers/<platform>` |
| Historical comparison | Same representative command/push workloads through pinned BoltFFI and Stable UniFFI | Android and applicable desktop/Apple runners |

## 4. Counters and forbidden shortcuts

Required counters where observable:

- backend create, bind, dispose, and drop;
- ingress object create/drop and generated wrapper retain/release/final-free;
- state and sink create/drop;
- active and maximum-active downstream callbacks;
- callbacks attempted, delivered, invalid, downstream-failed, panicked, and closed;
- capabilities and command invocation counts;
- payload sentinel allocations/releases;
- process RSS and package size for recorded performance runs.

Forbidden shortcuts:

- giving the handle a strong reference to `EventIngressState`;
- substituting a Rust-only recording facade for the required direct Swift/Android Kotlin `BleEventSink` implementation;
- exposing a test-only Hub or facade instead of the production `BleHub` object;
- building or loading a second UniFFI component/native image for runtime tests;
- relying on Kotlin GC/finalizers or unspecified Swift ARC timing for logical dispose;
- letting backend `dispose()` keep its root ingress wrapper until backend destruction;
- patching generated bindings without recording and reviewing the patch;
- treating UniFFI's internal handle map as a BleHub-owned routing registry;
- accepting compile-only generation as runtime ownership evidence.

## 5. Deterministic catalog

### 5.1 Assembly, capabilities, and bind

| ID | Execution | Pass condition |
|---|---|---|
| `ASM-01` | Construct with valid capabilities | `capabilities()` runs once; `contract_version` and metadata validate; bind runs once; Hub is exposed only after success |
| `ASM-02` | Capabilities returns typed error | Backend best-effort dispose runs once; no bind; original error is returned; counters return to baseline |
| `ASM-03` | Capabilities returns undeclared foreign error | It maps to `BleHubError::FfiContract`; rollback completes; no partial Hub |
| `ASM-04` | Capabilities reports invalid contract version/metadata | Validation fails before bind; backend is disposed; no partial Hub |
| `ASM-05` | Bind succeeds | Backend retains exactly one root handle and remains inert; no callback source starts during assembly |
| `ASM-06` | Bind returns typed error without retaining | State closes/drains; backend disposes; original bind error is returned; counters return to baseline |
| `ASM-07` | Bind throws undeclared foreign error | Error maps to `FfiContract`; state/backend/wrapper cleanup completes once |
| `ASM-08` | Backend retains before returning bind error | Contract violation is detected by counters; rollback still closes state, disposes backend, releases the retained root, and exposes no Hub |
| `ASM-09` | Backend attempts `push` during bind | Push cannot reach the sink; construction remains deterministic and is rejected as a contract violation |
| `ASM-10` | Bind is called twice | Second bind is a declared assembly failure; no second root handle or duplicate delivery path exists |
| `ASM-11` | Dispose fails during rollback | Original assembly error remains primary; cleanup failure is separately diagnosed; all safely releasable ownership returns to baseline |

Use named barriers around `capabilities_begin/return`, `bind_begin/retain/return`, `rollback_begin`, and `dispose_release_ingress`. Do not use sleeps as the only ordering mechanism.

### 5.2 Handle ownership and backend disposal

| ID | Execution | Pass condition |
|---|---|---|
| `OWN-01` | Drop all Hub/state strong owners while backend retains the handle | Handle does not keep state or sink alive; later push returns `Closed` |
| `OWN-02` | Explicit Hub dispose | State closes/drains before backend cleanup; backend atomically takes and releases its root ingress before returning |
| `OWN-03` | Backend dispose returns typed/undeclared failure | Backend remains closed; root ingress is still released; remaining cleanup continues best-effort |
| `OWN-04` | Caller retains another backend `Arc` after Hub dispose | Backend object may remain alive, but its root ingress wrapper has already been released and cannot deliver |
| `OWN-05` | A queued closure independently retains the handle | Closure may outlive root release, but push returns `Closed`; final wrapper count returns to baseline after closure completes |
| `OWN-06` | Duplicate dispose and Drop/deinit/Cleaner fallback compete | Logical cleanup, root release, native final-free, state drop, and sink drop each occur once |
| `OWN-07` | Complete 10,000 lifecycle cycles | Backend, handle, state, sink, payload, and wrapper counters return to initial baseline after every batch |

### 5.3 Command, callback, and close races

| ID | Controlled race | Expected result |
|---|---|---|
| `RACE-01` | Command holds backend read gate while close starts | Close waits; backend is not disposed under the command |
| `RACE-02` | Close obtains exclusive gate before a new command | New command returns typed `Closed`; backend is not invoked |
| `RACE-03` | Backend synchronously pushes before command return | Event may arrive before `Ok`; no deadlock; backend cannot then return dispatch `Err` |
| `RACE-04` | Sixteen threads push into one Hub | Maximum active sink callbacks is one; no global serialization across independent Hubs |
| `RACE-05` | Sink blocks while close starts | Close waits for the admitted callback, then takes/releases sink |
| `RACE-06` | Push passes first closed check before close, then waits for sink mutex | Second check returns `Closed`; sink is not called |
| `RACE-07` | Push races backend root-handle release | Result is an admitted delivery finishing before drain or `Closed`; never UAF/misdelivery |
| `RACE-08` | Push occurs after Hub/state drop | `Weak::upgrade` fails and returns `Closed` |
| `RACE-09` | Sink returns `EventSinkError` | `DownstreamFailed`; current attempt is not retried; later events remain usable |
| `RACE-10` | Sink mutex is poisoned | Close recovers, takes/releases sink, and continues cleanup; push returns bounded diagnostic failure |
| `RACE-11` | Rust sink panics or foreign sink throws an undeclared exception | The applicable narrow containment boundary records a distinct violation trace and returns `DownstreamFailed`; ingress mutex is not poisoned, a later push remains usable, no panic/exception crosses ABI, and the result is never `Delivered` |

Every race uses bounded watchdogs and named barriers. A timeout is a blocker, not a skipped/flaky pass.

### 5.4 State-sequence model

Generate valid and adversarial sequences over:

```text
capabilities_ok | capabilities_typed_err | capabilities_throw | capabilities_invalid
bind_begin
bind_retain | bind_ok | bind_typed_err | bind_throw
command_begin
sync_push
command_return_ok | command_return_err | command_throw
async_push
sink_ok | sink_err | sink_poison | sink_panic
close_begin
state_closed_and_drained
backend_dispose_begin
backend_root_ingress_release
backend_dispose_ok | backend_dispose_err | backend_dispose_throw
close_return
queued_handle_release
backend_extra_owner_release
```

After every action assert:

- dispatch `Err` implies zero events for that command;
- any event for a command implies dispatch can only return `Ok`;
- accepted command completion remains an event;
- closed objects never reopen;
- close return implies zero active downstream callbacks;
- a retained handle cannot target a different/new Hub;
- assembly error remains primary during rollback cleanup failure;
- all drop/final-free counters eventually return to baseline.

## 6. Language lanes

### 6.1 Retired desktop Kotlin/JVM lane

The desktop Kotlin/JVM lane has been removed from the maintained test graph. Its historical Stable UniFFI/JNA, checksum, lifecycle, and callback results remain in archived reports only. It was not a Windows BLE backend test and did not provide coverage that the maintained Android Kotlin/AAR lane and Apple Swift runtime cannot provide.

### 6.2 Android ART/AAR

Prefer a connected physical device; otherwise use the managed emulator for FFI-only tests. The lane must report which target ran. It validates:

- arm64-v8a and x86_64 native packaging as applicable;
- generated Kotlin/JNA production API, production AAR/POM, transitive JNA, loading, and internal direct production-ABI consumption;
- ART thread/callback behavior and all applicable lifecycle/error cases;
- release/minified consumer rules before production Android gate;
- performance and package-size comparison using the recorded common workload.

It does not validate `ScanCallback`, `BluetoothGattCallback`, permissions, RF, or vendor BLE behavior until the production Android backend/HIL phases.

Current command: `pixi run just ffi android <physical|emulator>`. The earlier Android 16 physical and x86_64 AVD runs remain historical mechanism evidence. The post-migration production AAR assembly/publish and macOS-hosted NDK native-slice builds passed after the Windows-only NDK linker assumption was replaced. Physical ART execution did not run because APK installation timed out twice with `ShellCommandUnresponsiveException` while the phone was locked. A future run must load the production AAR, implement backend and sink directly in Kotlin, construct the exported production Hub, and retain normal UniFFI checksum validation; package assembly alone is not ART evidence.

### 6.3 Apple Swift runtime and packaging

Maintained Swift runtime, packaging, backend, and HIL evidence runs on the project real Mac mini through local Pixi + Just with the Xcode-selected Swift toolchain. There is no maintained Windows Swift host, project-managed Swift root/bootstrap, or GitHub-hosted macOS workflow. Earlier Windows Swift and GitHub Actions results remain historical, non-maintained mechanism evidence only.

Required coverage:

- Swift backend retains the generated ingress object;
- serial/concurrent `DispatchQueue` callbacks and Rust-created caller thread;
- declared/undeclared errors and applicable assembly/ownership/race cases;
- explicit backend dispose releases the root strong reference before return;
- queued closures may temporarily retain handles and later receive `Closed`;
- ARC/final-free counters and 10,000 lifecycle cycles;
- XCFramework slices, modulemap/header, clean macOS SPM runtime consumer, and generic iOS simulator/device compile-link.

This does not claim CoreBluetooth, signing, installation, or BLE/RF behavior.

Current maintained commands are `pixi run just ffi swift-macos` and `pixi run just ffi apple-package`, executed locally on the real Mac mini; `apple-ci` is the Pixi environment name, not a hosted-CI requirement. The 2026-09-03 current candidate passed the macOS runtime against the one production image with direct Swift backend and sink implementations, the exported production Hub, event catalog, and applicable concurrency/error/lifecycle cases, plus all three Apple package slices and symbol checks. iOS signing/device runtime and independent artifact-only consumer validation remain separate. The removed `ffi swift-windows` command is not a future gate.

## 7. Error, panic, and event-wire gate

Generate and run these exact roles:

- `BleHubError` and `CommandDispatchError` as `uniffi::Error` callable channels;
- `BleEventError` as ordinary event data, not a callable error;
- `CommandCompleted.error: Option<BleEventError>`;
- `ScanStopResult::Ok | Err`;
- `ConnectionStateResult::Ok | Err`;
- `NotificationState` and every `BleEvent` variant;
- `EventIngressStatus::{Delivered, Closed, InvalidEvent, DownstreamFailed}`;
- `From<uniffi::UnexpectedUniFFICallbackError>` mappings.

For Kotlin and Swift, verify every value can be constructed in Rust, observed without string parsing, round-tripped where supported, and compared with the source vector. Record generated API snapshots. If a complex enum requires a tagged-record fallback, report it to Plan review; do not silently change the wire representation.

Panic/exception subprocesses pass only when the returned status/error, diagnostic channel, process exit, and cleanup counters agree. A log by itself is not containment.

## 8. Performance and packaging

Use optimized builds, fixed warm-up/repetitions, and the same command/push vectors for Stable UniFFI and the historical BoltFFI baseline. Report median, p95, p99, maximum, throughput, allocation/GC observations, close latency, package size, and environment identity.

Workloads include:

- no-op and recording direct Rust, Kotlin, and Swift sinks as applicable;
- 20-, 244-, and 1024-byte payloads plus final wire boundaries;
- one and sixteen producer threads;
- multiple independent Hubs;
- idle close and close with one callback in flight;
- 10,000 complete lifecycle cycles.

The accepted Android JNA/JNI relative tradeoff does not waive final absolute regression checks. A threshold miss requires recorded investigation and Plan review; it does not authorize an architecture change in this catalog.

A packaging lane passes only when a fresh consumer uses produced artifacts without source-tree classpaths, path/project dependencies, accidental native-library lookup, or mixed contract versions. Artifacts must record contract version, generator/runtime revision, architectures/ABIs, and expected native entries.

## 9. Pass and blocker criteria

Historical Phase 0 mechanism Go remains evidence for the selected framework and weak-ingress mechanism, not for the revised ABI. The current post-migration candidate now passes the applicable macOS Swift production runtime gate plus the Apple package/symbol gate; Android package assembly is also complete, but Android ART, final artifact-only consumers, iPhone, RF, and final performance boundaries remain separate. Before Phase 5, the exported production `BleHub` + foreign backend + direct ingress + foreign sink graph must pass every applicable case through the single production image. Phase 5 must rerun the catalog against the complete ABI candidate, with macOS/iOS Swift gates on the project real Mac mini and no Windows Swift supplement.

Any of the following blocks ABI freeze:

- crash, UAF, double free, permanent deadlock, stale-event misdelivery, or reference/handle leak;
- unexpected foreign failure or Rust panic crossing the ABI or masquerading as success/`Delivered`;
- invalid contract metadata accepted during assembly;
- bind failure exposing a Hub or retaining an ingress after rollback;
- backend `dispose()` returning while still owning its root ingress wrapper;
- callback accepted into downstream after close/drain completes;
- event followed by dispatch `Err`, or dispatch `Err` followed by an event for the same command;
- cleanup or final-free occurring more than once;
- ambiguous/unusable generated errors or event data enums;
- inability to build and run an artifact-only consumer;
- unexplained performance, close-latency, payload-retention, or package-size regression.

## 10. Result promotion

For each completed group:

1. preserve raw logs, generated API excerpts, counters, benchmark data, toolchain identity, and artifact manifests;
2. write or update a durable report under `docs/research/`, labeling conclusions verified, inferred, or unresolved;
3. identify whether the result confirms the Plan, reveals an implementation constraint, or requires a decision;
4. modify `.plan/PLAN.md` only through explicit review;
5. keep BoltFFI results as historical comparison rather than a second production path.
