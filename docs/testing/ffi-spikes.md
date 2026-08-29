# FFI spike execution catalog

- Status: execution catalog; non-normative
- Scope: Stable UniFFI `0.32.0` direct-handle mechanism validation in Phase 0, plus a complete ABI-candidate rerun before the Phase 4 production ABI freeze
- Normative source: [`.plan/PLAN.md`](../../.plan/PLAN.md), especially sections 12.1, 12.5, and Phases 0 and 4
- Research basis: [FFI lifecycle and concurrency](../research/ffi-lifecycle-concurrency.md) and [UniFFI versus BoltFFI for BleHub](../research/uniffi-vs-boltffi-for-blehub.md)

This file converts the Plan's FFI questions into runnable work. It does not define architecture or public semantics. A passing representative fixture proves only the mechanism and artifact path it actually exercises; the complete candidate must be rerun before ABI freeze. Durable conclusions belong in `docs/research/`, and normative changes require Plan review.

## 1. Production candidate and historical baseline

The production candidate is Stable UniFFI direct ingress:

```text
Rust application
  strong -> Rust-owned BleHub

BleHub
  strong -> Arc<dyn BleBackend>
  value  -> validated BackendCapabilities
  strong -> Arc<EventIngressState>

EventIngressState
  strong while open -> Rust Box<dyn BleEventSink>

foreign backend
  root strong -> generated BackendEventIngressHandle wrapper

BackendEventIngressHandle
  weak -> EventIngressState

native callback
  -> retained handle.push(event)
  -> Weak::upgrade
  -> EventIngressState::push
  -> Rust BleEventSink::on_event
```

BleHub production routing has no project-authored token allocator, process-local registry, registration guard, free push function, raw pointer, or integerized `Arc`. UniFFI's generated opaque object handles and foreign callback handle maps remain framework-managed implementation details and must still pass lifecycle tests.

The following remain historical evidence only:

- `spikes/boltffi-lifecycle/`;
- `spikes/boltffi-token-ingress/`;
- `spikes/boltffi-class-handle-callback-param/`;
- BoltFFI #869/#872 and token-ACK probes.

They remain useful for regression workloads, performance comparison, and explaining why the migration was selected. They do not define the production gate and must not coexist with the UniFFI ingress in production code.

## 2. Evidence ledger

### 2.1 Verified mechanism evidence

| Evidence | Supports | Does not support |
|---|---|---|
| `spikes/uniffi-production-candidate/` on Windows Kotlin/JVM and Windows Swift `6.3.3` | The actual production `BleHub -> foreign BleBackend -> BackendEventIngressHandle { Weak<EventIngressState> } -> Rust-only BleEventSink` graph; binding-phase rejection; typed/undeclared callback errors; close drain; root release on cleanup errors; panic containment; Rust/native threads; and 10,000 lifecycle cycles | Windows BLE APIs, macOS ARC/package behavior, the complete future public event wire, or final performance/package thresholds |
| The same production candidate on Android 16 physical arm64 (`MAG-AN00`) and Android 16 x86_64 AVD | Normal UniFFI checksum initialization, ART callback paths, production rollback/lifecycle cases, both Android ABIs, AAR/POM publication, transitive JNA, and an artifact-only consumer | Android Framework BLE callbacks, permissions, RF, vendor behavior, or release/minified production AAR |
| `spikes/uniffi-lifecycle/` on desktop and Android hosts | Representative Stable UniFFI retention, direct Rust object parameters, selected races, payload/performance workloads, and packaging mechanisms | The production object graph by itself; it remains the mechanism baseline rather than production evidence |
| GitHub Actions run [`33249751300`](https://github.com/VIDLG/blehub/actions/runs/33249751300) | Real macOS Swift runtime, retained direct object, ARC/lifecycle/error cases, XCFramework, clean macOS SPM consumer, and generic iOS simulator/device compile-link for the representative fixture | The final production object graph, CoreBluetooth, signing, installation, BLE hardware, or RF |
| BoltFFI comparison fixtures | Historical direct-JNI performance and workaround behavior | Approval of token routing or ACK workaround for production |

### 2.2 Verified production-candidate result

The 2026-08-29 production-candidate rerun verified:

- the real weak ingress path reaches the Rust-only sink from Kotlin, Swift, Android ART, and a Rust-created caller thread;
- ingress remains in `Binding` until bind succeeds; push during bind returns `Closed`, records the violation, and rejects construction before any sink delivery;
- valid capabilities, typed/undeclared capabilities failures, invalid contract version, typed/undeclared bind failures, and retain-before-bind-error rollback;
- typed and undeclared command rejection produce no event;
- close waits for an admitted blocked callback, closes ingress before backend disposal, and makes a dispose-time or late push return `Closed`;
- backend disposal releases the sole long-lived ingress root before returning, including typed and undeclared cleanup-error paths;
- invalid event, sink rejection, and a Rust sink panic remain bounded by `EventIngressStatus`; the deliberate panic does not unwind across the ABI;
- 10,000 complete production fixture/state/sink/backend cycles return the observable counters to baseline in desktop Kotlin/JVM, Windows Swift, Android physical, and Android emulator lanes;
- Android packages and consumes `arm64-v8a` and `x86_64` native libraries through a published AAR rather than a source-tree project dependency.

Stable UniFFI `0.32.0` has a Windows Kotlin/JNA carrier defect for checksum functions declared as C `uint16_t`: JNA direct mapping widens the return to Kotlin `Int` without reliably clearing the upper bits. Only `uniffi.windows-kotlin.toml` sets `omit_checksums = true`. The runnable binding is paired with a separately generated checksum-reference binding, and `tools/check_uniffi_windows_checksums.py` compared all 17 exported checksums by their declared low 16 bits plus both `uint32_t` contract-version functions exactly. Android and Swift retain the generated checksum checks and initialized successfully without this workaround. Generated bindings were not patched.

### 2.3 Still required before production ABI freeze

- Run the production candidate on real macOS Swift and through the XCFramework/SPM artifact-only consumer; `ffi-candidate-test-swift-macos` and the `phase0-apple` workflow step are prepared, but have not executed in this Windows session. The current Windows Swift result is supplementary.
- Expand the candidate from the minimal Phase 0 scan/connect wire to every final `BleEventPayload` variant, `ScanStopResult`, `ConnectionStateResult`, `NotificationState`, optional terminal error, and owned payload boundary vector.
- Complete remaining deterministic catalog cases, including explicit repeated bind, rollback-cleanup secondary diagnostics, broad state-sequence generation, and payload lifetime after the foreign frame returns.
- Record final absolute performance, close latency, package size, release/minified Android behavior, artifact manifests, and generator/runtime/ABI metadata checks.
- Rerun the catalog after the Phase 1 public contract is complete and again before the Phase 4 ABI freeze; the current result is a mechanism/object-graph gate, not an ABI freeze.

## 3. Required fixtures

| Fixture | Responsibility | Consumers |
|---|---|---|
| Direct-handle object graph | Inert backend, capabilities, one-time bind, weak ingress handle, Rust sink, Hub lifecycle | Pure Rust, desktop Kotlin/JVM, Android ART, Windows Swift host, macOS Swift CI |
| Error and panic boundary | Callable errors, event-error data, unexpected foreign errors, sink rejection, poison, panic containment | Rust, Kotlin/JVM, Swift |
| Event wire | Every `BleEventPayload` variant, `ScanStopResult`, `ConnectionStateResult`, `NotificationState`, and optional terminal error | Rust, Kotlin/JVM, Swift |
| Race/stress harness | Named barriers, watchdogs, counters, state sequences, 10,000 lifecycle cycles, payload bursts | Every runtime lane |
| Packaging consumers | Consume produced artifacts only, without source-tree classpaths or implicit native outputs | Gradle/JVM, Android AAR, XCFramework/SPM |
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
- testing a foreign downstream sink instead of the Rust-only sink path;
- exposing `BleHub` to Kotlin/Swift merely to simplify the fixture;
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
| `RACE-04` | Sixteen threads push into one Hub | Maximum active Rust sink callbacks is one; no global serialization across independent Hubs |
| `RACE-05` | Sink blocks while close starts | Close waits for the admitted callback, then takes/releases sink |
| `RACE-06` | Push passes first closed check before close, then waits for sink mutex | Second check returns `Closed`; sink is not called |
| `RACE-07` | Push races backend root-handle release | Result is an admitted delivery finishing before drain or `Closed`; never UAF/misdelivery |
| `RACE-08` | Push occurs after Hub/state drop | `Weak::upgrade` fails and returns `Closed` |
| `RACE-09` | Sink returns `EventSinkError` | `DownstreamFailed`; current attempt is not retried; later events remain usable |
| `RACE-10` | Sink mutex is poisoned | Close recovers, takes/releases sink, and continues cleanup; push returns bounded diagnostic failure |
| `RACE-11` | Sink panics | The sink-call-only guard records a distinct violation trace and returns `DownstreamFailed`; ingress mutex is not poisoned, a later push remains usable, no unwind crosses ABI, and the result is never `Delivered` |

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

### 6.1 Desktop Kotlin/JVM

Use Stable UniFFI Kotlin/JNA, not the experimental JNI generator. Build native and generated artifacts into a clean directory and consume them from a separate Gradle project/process.

Required runtime coverage:

- final inert Kotlin backend and retained generated ingress object;
- JVM caller thread, JVM executor worker, and Rust-created caller thread;
- all applicable `ASM-*`, `OWN-*`, `RACE-*`, and state-sequence cases;
- declared and undeclared capabilities/bind/command/dispose failures;
- explicit generated-object destroy/release and Cleaner fallback ordering;
- final event-wire vectors and payload ownership.

Desktop JVM evidence is not Android ART evidence and does not claim BLE behavior.

Current production-candidate command: `pixi run just ffi-candidate-test-kotlin-jvm`. It passed on Windows after independently verifying 17 checksum functions and two contract-version functions. The Windows-only checksum workaround and its scope are recorded in section 2.2.

### 6.2 Android ART/AAR

Prefer a connected physical device; otherwise use the managed emulator for FFI-only tests. The lane must report which target ran. It validates:

- arm64-v8a and x86_64 native packaging as applicable;
- generated Kotlin/JNA, AAR/POM, transitive JNA, loading, and artifact-only consumption;
- ART thread/callback behavior and all applicable lifecycle/error cases;
- release/minified consumer rules before production Android gate;
- performance and package-size comparison using the recorded common workload.

It does not validate `ScanCallback`, `BluetoothGattCallback`, permissions, RF, or vendor BLE behavior until the production Android backend/HIL phases.

Current production-candidate command: `pixi run just ffi-candidate-test-android <physical|emulator>`. Both Android 16 lanes passed: the physical `MAG-AN00` and the `medium_phone` x86_64 AVD each ran nine production-object-graph instrumentation tests plus one published-AAR consumer test. The generated Android bindings retained normal UniFFI checksum validation.

### 6.3 Swift host and Apple packaging

Windows Swift host is supplementary language/runtime evidence. Authoritative Apple packaging requires macOS/Xcode.

Required coverage:

- Swift backend retains the generated ingress object;
- serial/concurrent `DispatchQueue` callbacks and Rust-created caller thread;
- declared/undeclared errors and applicable assembly/ownership/race cases;
- explicit backend dispose releases the root strong reference before return;
- queued closures may temporarily retain handles and later receive `Closed`;
- ARC/final-free counters and 10,000 lifecycle cycles;
- XCFramework slices, modulemap/header, clean macOS SPM runtime consumer, and generic iOS simulator/device compile-link.

This does not claim CoreBluetooth, signing, installation, or BLE/RF behavior.

Current supplementary command: `pixi run just ffi-candidate-test-swift-windows`. Swift `6.3.3` compiled both UniFFI components against one production candidate DLL, retained normal checksum checks, contained the deliberate Rust sink panic, and completed 10,000 ARC lifecycle cycles. `pixi run -e apple-ci just ffi-candidate-test-swift-macos` is wired into `phase0-apple.yml`, but the production candidate has not yet run on macOS/Xcode or through its Apple package consumer.

## 7. Error, panic, and event-wire gate

Generate and run these exact roles:

- `BleHubError` and `CommandDispatchError` as `uniffi::Error` callable channels;
- `BleEventError` as ordinary event data, not a callable error;
- `CommandCompleted.error: Option<BleEventError>`;
- `ScanStopResult::Ok | Err`;
- `ConnectionStateResult::Ok | Err`;
- `NotificationState` and every `BleEventPayload` variant;
- `EventIngressStatus::{Delivered, Closed, InvalidEvent, DownstreamFailed}`;
- `From<uniffi::UnexpectedUniFFICallbackError>` mappings.

For Kotlin and Swift, verify every value can be constructed in Rust, observed without string parsing, round-tripped where supported, and compared with the source vector. Record generated API snapshots. If a complex enum requires a tagged-record fallback, report it to Plan review; do not silently change the wire representation.

Panic/exception subprocesses pass only when the returned status/error, diagnostic channel, process exit, and cleanup counters agree. A log by itself is not containment.

## 8. Performance and packaging

Use optimized builds, fixed warm-up/repetitions, and the same command/push vectors for Stable UniFFI and the historical BoltFFI baseline. Report median, p95, p99, maximum, throughput, allocation/GC observations, close latency, package size, and environment identity.

Workloads include:

- no-op and recording Rust sinks;
- 20-, 244-, and 1024-byte payloads plus final wire boundaries;
- one and sixteen producer threads;
- multiple independent Hubs;
- idle close and close with one callback in flight;
- 10,000 complete lifecycle cycles.

The accepted Android JNA/JNI relative tradeoff does not waive final absolute regression checks. A threshold miss requires recorded investigation and Plan review; it does not authorize an architecture change in this catalog.

A packaging lane passes only when a fresh consumer uses produced artifacts without source-tree classpaths, path/project dependencies, accidental native-library lookup, or mixed contract versions. Artifacts must record contract version, generator/runtime revision, architectures/ABIs, and expected native entries.

## 9. Pass and blocker criteria

Phase 0 mechanism Go requires the representative final BleHub graph to pass every applicable case. Phase 4 must rerun the catalog against the complete ABI candidate.

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
