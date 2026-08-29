# FFI spike execution catalog

- Status: execution catalog; non-normative
- Scope: Phase 0 validation of a representative production-shaped BoltFFI token/object-graph mechanism, plus a complete ABI-candidate rerun before the Phase 4 production ABI freeze
- Normative source: [`.plan/PLAN.md`](../../.plan/PLAN.md), especially sections 12.1, 12.5, and Phases 0 and 4
- Research basis: [BoltFFI event ingress workaround](../research/boltffi-event-ingress-workaround.md) and [FFI lifecycle and concurrency](../research/ffi-lifecycle-concurrency.md)

This file turns the Plan's Phase 0 questions and pre-freeze FFI gates into runnable spike work. Phase 0 validates a representative production-shaped token/object-graph mechanism; passing that validation is not a production ABI freeze. Before the Phase 4 production ABI freeze, every applicable catalog case must be rerun against the complete ABI candidate. This file does not define BleHub architecture or public semantics. Spike results must be recorded as evidence in `docs/research/`; any resulting architecture or gate change must be reviewed in `.plan/PLAN.md`. Passing this catalog alone does not make a decision normative.

## 1. Purpose and outputs

The Phase 0 spikes must answer whether a representative production-shaped v0.1 token ingress and object graph can be generated, run, packaged, raced, closed, and released safely. The pre-freeze rerun must answer the same applicable questions against the complete ABI candidate and its generated artifacts. Both runs must also keep the future direct-handle path observable without allowing it to block the token path prematurely.

Each executed lane produces:

- the exact BoltFFI revision and generated-source hash;
- host OS/architecture, Rust toolchain, JDK/Gradle or Swift toolchain versions;
- clean generation, build, test, and consumer commands;
- per-case pass/fail results and subprocess exit status for fault cases;
- allocation, registry, native-handle, backend, state, sink, and wrapper counters;
- benchmark workload, warm-up, repetitions, payload distribution, and percentile results;
- a short conclusion classified as verified, inferred, or still unverified.

Do not accept screenshots or a compile-only probe as runtime evidence. Preserve generated API excerpts and failure diagnostics needed to explain a result, but do not treat generated names alone as proof of ownership or runtime behavior.

## 2. Evidence ledger

### 2.1 Already verified locally

| Evidence | What it supports | What it does not support |
|---|---|---|
| `spikes/boltffi-class-handle-callback-param/` fails on the pinned BoltFFI revision with `unsupported expansion: callback method parameter shape` | A foreign trait method cannot currently receive an exported Rust class handle; issue `boltffi/boltffi#869` is a real generator gap | The future runtime ownership, threading, or release behavior after the generator accepts the shape |
| A temporary compile probe accepts named-field `BackendEventIngressToken`, typed `bind_event_ingress`, and top-level `push_backend_event` shapes | The token ABI is syntactically generatable | Kotlin/JVM or Swift execution, packaging, exception behavior, registry races, or final object lifetime |
| Local BoltFFI source and generated Kotlin fixtures implement exported-class retain/release and idempotent `AutoCloseable.close()` | Existing wrapper primitives can support deterministic release | The final token registry graph, Rust-only downstream sink, or BleHub rollback order |
| `spikes/boltffi-lifecycle/` runs Rust and Windows Kotlin/JVM tests for the old builder/foreign-sink graph | Partial evidence for wrapper retention, cross-thread calls, callback serialization, close drain, late callback handling, typed declared errors, repeated lifecycle, and JNI loading | Acceptance of the v0.1 token assembly, final Rust-only downstream, Swift, or direct-handle bind |
| `spikes/boltffi-token-ingress/` runs the real Hub/registry/Rust-only sink with a generated Kotlin foreign backend; 21 Windows JVM mechanism tests include deterministic races and 10,000 lifecycles, followed by a separate bindings-JAR/native-DLL consumer test | The production-shaped token graph and clean local JVM artifact consumption work on the recorded environment | Swift/Android packaging, complete counters/performance, or final CI generator pinning |
| The same fixture compiles generated Swift plus its generated C module against an MSVC Rust DLL on Windows; the smoke covers synchronous and Rust-thread callbacks, 64 concurrent pushes, idempotent dispose, closed stale tokens, and 10,000 unique-token ARC release cycles | The production-shaped foreign-trait/token mechanism and language-level Swift ARC run on the recorded Windows Swift host | CoreBluetooth, Objective-C interop, Apple queue semantics, Apple artifacts/package consumption, or final macOS ARC behavior |
| The old JVM spike reproduces unchecked Kotlin exceptions being cleared by generated JNI glue and reported as a zero/void success; the new fixture safely maps typed and unchecked failures after regeneration from a clean `8f862c8...` archive | The exception boundary works for the clean revision and exact production-shaped candidate tested | Every future ABI shape/release has the same behavior; fault subprocess and pre-freeze reruns remain required |
| Raw Kotlin output for error payload fields named `message` fails to compile; the Phase 0 JVM fixture applies a generated-source `override` workaround | The runtime lane can continue without renaming the reviewed wire field | Stock BoltFFI generation is production-ready; the workaround must be removed after an upstream/fork fix |

### 2.2 Still unverified for the production ABI candidate

- macOS Swift runtime/ARC/queue behavior, immutable CI generator pinning, and final Android/Apple package consumers remain open.
- Complete wrapper/native-handle/weak-state counters for the `BleHub -> foreign backend -> token -> registry Weak -> EventIngressState -> Rust sink` lifetime.
- Registration failure, repeated bind, and complete unexpected foreign failure subprocess coverage without partial Hub exposure or double cleanup.
- Lock-free registry lookup with no snapshot guard held across weak upgrade, validation, downstream code, or close waiting.
- Complex `BleEventPayload` generation quality versus a tagged-record fallback.
- Final-path panic containment, payload ownership release, lifecycle stress, throughput, callback latency, and close latency.
- macOS/Apple ARC and queue behavior, clean Swift Apple package consumption, and later Android ART/Handler/Looper behavior.

## 3. Fixture set

Fixture names below identify responsibilities, not permanent public package names. Phase 0 implementation may combine fixtures when isolation and independent failure reporting are preserved.

| Fixture | Role | Required consumers |
|---|---|---|
| Production-shaped token ingress fixture | Minimal inert `BleBackend`, one-time capabilities query and bind, process-local registry, exported push, `EventIngressState`, Rust-only recording sink, and `BleHub` lifecycle | Pure Rust, desktop Kotlin/JVM, Swift host |
| Error-shape fixture | Public BoltFFI errors, production `derive_more::Display/Error`, the recorded `thiserror` scanner incompatibility, constructor/command/bind/dispose results, event status, expected and unexpected foreign failures | Rust, Kotlin/JVM, Swift |
| Event-shape fixture | Every `BleEventPayload` variant and tagged-record fallback from the same semantic vectors | Rust, Kotlin/JVM, Swift |
| Race and stress harness | Deterministic barriers, counters, watchdogs, state-sequence runner, 10,000-cycle loop, and payload bursts | Reused by all runtime lanes |
| Packaging consumers | Clean projects that consume only produced artifacts, not source-tree classpaths or implicit build outputs | Gradle/JVM and XCFramework/SPM; Android AAR later |
| Existing class-handle probe | Expected failure on the current revision and an expected-pass trigger after issue #869 is fixed | Generator first; Kotlin/JVM and Swift runtime only after generation passes |

The old `spikes/boltffi-lifecycle/` remains historical evidence. Do not count extensions to its builder/foreign-sink graph as acceptance of either the Phase 0 production-shaped mechanism or the complete pre-freeze ABI candidate.

## 4. Instrumented token registry object graph

The production-shaped Phase 0 fixture must instantiate and count this graph, not a reduced integer-routing demo:

```text
Application
  strong -> BleHub

BleHub
  strong -> foreign Box<dyn BleBackend>
  value  -> cached BackendCapabilities
  strong -> Arc<EventIngressState>
  owns   -> BackendEventIngressRegistration

EventIngressState
  strong while open -> Rust Box<dyn BleEventSink>

BackendEventIngressRegistration
  manages -> one process-local registry entry

process-local registry
  token value -> Weak<EventIngressState>

foreign backend
  value copy -> BackendEventIngressToken

native callback
  -> generated top-level push_backend_event(token, event)
  -> snapshot lookup and cloned Weak
  -> Weak::upgrade
  -> EventIngressState::push
  -> Rust BleEventSink::on_event
```

Required counters and probes:

- token allocations, including consumed tokens from failed assembly;
- active registry entries and peak entries;
- strong and weak state counts at named barriers;
- backend create/dispose/drop counts;
- sink create/drop and active/max-active callback counts;
- generated wrapper/native handle retain, release, and final-free counts where observable;
- callbacks attempted, delivered, rejected as invalid/downstream failure, and returned `Closed`;
- capabilities, bind, command, dispose, and wrapper-close invocation counts.

Forbidden acceptance shortcuts:

- registry entries strongly retaining state;
- token reuse after close or any failed assembly step;
- replacing the process-global registry during parallel tests;
- encoding pointers or `Arc` addresses as token values;
- allowing backend, registry, or stale callbacks to retain the Rust sink;
- testing a foreign downstream sink instead of the Rust-only sink path.

## 5. Shared deterministic catalog

### 5.1 Assembly, bind, and rollback

| ID | Execution | Required observation | Pass condition |
|---|---|---|---|
| `ASM-01` | Successful construction with an inert backend | `capabilities()` once, validated and cached; bind once; registration after bind; Hub exposed last | A command and callback use cached capabilities and the real ingress; capabilities count remains exactly one |
| `ASM-02` | Capabilities returns typed `Err` | Best-effort backend dispose; no token entry; no Hub | Original assembly error is returned and all object counts return to baseline |
| `ASM-03` | Capabilities returns invalid metadata | Same rollback instrumentation as `ASM-02` | Validation error is typed; dispose occurs once; no token is registered |
| `ASM-04` | Bind succeeds | Backend stores exactly one token but remains inert | No callback source or event exists before the first command |
| `ASM-05` | Bind returns typed `Err` | State closes/drains and backend is disposed; token remains consumed but unregistered | No token retention after the failing bind contract, no event, no Hub, and baseline counts |
| `ASM-06` | Backend attempts push during bind | Token is not yet registered | Push returns `Closed`; sink receives zero events; construction behavior remains deterministic |
| `ASM-07` | Inject registry registration failure after successful bind | State closes/drains, backend disposes, token is never reused | No partial Hub; no residual entry; exactly one native cleanup |
| `ASM-08` | Call bind twice on one backend | Second call returns the declared assembly error | Original token remains the only stored token; no second ingress and no event duplication |
| `ASM-09` | Dispose fails during rollback | Original assembly error remains primary; cleanup error is observable as diagnostic evidence | Objects and entries still release once; no reopen or partial Hub |

Use barriers around `bind_begin`, `bind_return`, `register_begin`, and `register_return` so prohibited pushes and rollback races are reproducible rather than timing-based.

### 5.2 Token and registry

| ID | Execution | Required observation | Pass condition |
|---|---|---|---|
| `REG-01` | Attempt to construct/register token `0` | Validation rejects it before routing | No entry and no backend retention |
| `REG-02` | Concurrent token allocation | Record every issued value | All values are nonzero and unique |
| `REG-03` | Allocate across successful, failed-bind, and failed-registration cycles | Record consumed values | Values increase monotonically and none are reused |
| `REG-04` | Seed allocator at the near-overflow boundary | Exercise final valid allocation and next allocation | No wrap to `0`; exhaustion returns typed construction failure |
| `REG-05` | Drop all Hub/state strong owners while an entry is visible in an isolated registry fixture | Inspect weak upgrade and sink drop | Entry does not keep state or sink alive; push returns `Closed` |
| `REG-06` | Missing, unregistered, and stale-token push | Route each against active and newly created Hubs | Every call returns `Closed`; no sink receives the event |
| `REG-07` | Block downstream while registry writers add/remove unrelated entries | Measure writer completion and inspect guard scope instrumentation | Snapshot guard is released before weak upgrade/state push; blocked downstream does not pin a registry snapshot guard |
| `REG-08` | Run parallel isolated-registry tests | Compare entries and tokens across fixtures | Tests do not swap or mutate the production global registry |
| `REG-09` | Complete 10,000 assembly/close cycles | Sample active entries and all counters | Registry and object counts return to the initial baseline |

### 5.3 Command, callback, close, and release races

| ID | Controlled race | Expected result |
|---|---|---|
| `RACE-01` | Command holds the backend read lease while close starts | Close waits; backend cannot be disposed under the command |
| `RACE-02` | Close obtains exclusive ownership before a new command | New command receives typed `Closed`; backend is not invoked |
| `RACE-03` | Backend synchronously pushes before a command returns | Event may arrive before `Ok`; no deadlock; command cannot then return `Err` |
| `RACE-04` | Sixteen callback threads push to one Hub | Rust sink maximum active callbacks is one; all accepted deliveries remain intact |
| `RACE-05` | Sink callback blocks while close starts | Close waits for the callback, then releases the sink |
| `RACE-06` | Push passes the first closed check, close sets closed, then push reaches the sink mutex | The second check returns `Closed`; sink is not called |
| `RACE-07` | Push lookup races unregister | Result is either completion of an already admitted delivery or `Closed`; never UAF or misdelivery |
| `RACE-08` | Push races backend dispose | State is already closed; late push returns `Closed`; dispose does not re-enter downstream |
| `RACE-09` | Push occurs after unregister or Hub drop | Lookup miss or weak-upgrade failure returns `Closed` |
| `RACE-10` | Explicit dispose, duplicate dispose, wrapper close/deinit, and Rust `Drop` compete | Native cleanup, registration removal, wrapper final-free, state drop, and sink drop each occur once |
| `RACE-11` | Backend dispose returns typed `Err` | Hub remains permanently closed and all ownership is released |
| `RACE-12` | Old token pushes after a new Hub is fully active | Old push returns `Closed`; new sink sees zero old-token events |

Every race test uses a bounded watchdog and named barriers. A watchdog expiry is a blocker, not a skipped or flaky result.

### 5.4 State-sequence conformance

Run valid and adversarial sequences over these actions:

```text
allocate_token
capabilities_ok | capabilities_err
bind_begin
bind_ok | bind_err | bind_throw
register_ok | register_err
command_begin
sync_push
command_return_ok | command_return_err | command_throw
async_push
close_begin
late_push
backend_dispose_ok | backend_dispose_err | backend_dispose_throw
unregister_token
close_return
wrapper_release
```

Assertions apply after every action:

- dispatch `Err` implies zero events for that command;
- once an event exists for a command, dispatch can only return `Ok`;
- accepted command completion remains an event, not a later synchronous result;
- no illegal transition reopens a closed Hub;
- close return implies zero active downstream callbacks;
- stale, missing, or unregistered tokens cannot target another Hub;
- terminal cleanup and all drop counters return to baseline.

## 6. Kotlin/JVM lane

The desktop JVM lane validates the common generated JNI and object graph. Android ART, Handler/Looper, Android thread attachment, and clean AAR consumption remain a separate Android lane unless Phase 0 explicitly brings them forward.

### 6.1 Build and packaging

1. Generate bindings from the same Rust contract revision used by the Rust fixture.
2. Build the native library and generated JVM artifact from a clean output directory.
3. Consume them from a clean Gradle project with no source-tree classpath leakage.
4. Run the consumer in a fresh JVM process so loading, symbol resolution, and shutdown are tested.
5. Verify the generated lifecycle API does not collide with Kotlin `AutoCloseable.close()`; explicit BleHub disposal and wrapper close remain separately callable and idempotent.

### 6.2 Runtime cases

- Implement the final inert Kotlin backend and retain the generated token DTO after successful bind.
- Enter Rust from a JVM caller thread and let Rust call the Kotlin backend on the same stack.
- Push synchronously before command return, from a JVM executor worker, and from a Rust-created thread that must attach to the JVM.
- Run `ASM-*`, `REG-*`, `RACE-*`, and state-sequence cases through generated APIs rather than Rust-only helpers.
- Exercise 20-, 244-, and 1024-byte payloads for historical comparison, plus zero, configured-boundary, and one-over-boundary vectors from the final wire guards.
- Close through explicit lifecycle method, Kotlin `use`/`close`, and Rust fallback permutations while counting native cleanup and final free.

### 6.3 Kotlin exception subprocesses

Fault cases run one per subprocess to distinguish a typed failure from process corruption:

- declared typed bind/command/dispose failure;
- unchecked exception from `capabilities`, bind, command, and dispose;
- unchecked exception on the synchronous callback-before-return path;
- invalid event status return and Rust downstream typed failure;
- Rust panic fault injection at the outer ingress trampoline.

A subprocess passes only if the fault is reported as a failure appropriate to that boundary, no Rust unwind or JVM exception crosses the ABI, cleanup counters are emitted, and the call is not reported as `Ok`, command success, or `Delivered`. The currently observed JNI `ExceptionClear` plus zero/void success behavior is an explicit blocker.

## 7. Swift host lane

The Swift host lane validates generated protocol conformance, top-level push, ARC, queues, errors, and clean package consumption. It does not claim iOS BLE behavior.

On Windows, BleHub manages a pinned Swift toolchain through `tools/swift_toolchain.py`. The script delegates the official package download, hash verification, dependency handling, and upgrade transaction to WinGet, while BleHub fixes Swift `6.3.3` and the required feature selection. It disables the Swift Android platform and non-x64 Windows SDK payloads for this host lane. The root resolves from `BLEHUB_SWIFT_ROOT`, ignored `blehub.local.toml`, then the portable user cache; the recorded machine selects the shared `D:/dev/swift` root through local config. Ordinary generation and tests never install or update Swift implicitly:

Machine-local toolchain roots are resolved in this order:

1. `BLEHUB_SWIFT_ROOT` or `BLEHUB_ANDROID_ROOT`, for CI and one-off overrides;
2. the corresponding key under `[toolchains]` in ignored `blehub.local.toml`;
3. the portable fallback `~/.cache/blehub/toolchains/<name>`.

Copy `blehub.local.example.toml` to `blehub.local.toml` when a stable machine-specific location is desired. This local file contains paths only; pinned versions, URLs, hashes, and dependency locks remain in tracked scripts and lockfiles. Relative TOML paths are resolved from the repository root. Inspect the effective values and manage the Swift host lane with:

```text
pixi run just toolchain-paths
pixi run just sccache-stats
pixi run just swift-report
pixi run just swift-bootstrap
pixi run just swift-doctor
```

Pixi supplies sccache for ordinary Rust compilation and the generated JNI C glue. Cargo incremental is disabled in Pixi environments so the project does not maintain both incremental object graphs and whole-crate sccache entries. `SCCACHE_DIR` and `SCCACHE_CACHE_SIZE` remain standard machine/CI overrides; sccache reduces repeat compilation, not the required final libraries or packages, and its cache consumes separate disk space. BoltFFI metadata generation deliberately uses a dedicated Cargo target and removes a sccache Rust wrapper: metadata mode changes macro output through dedicated cfg/environment inputs that are not safely separated from ordinary artifacts by the current cache key. The BoltFFI CLI itself remains in the ordinary sccache-enabled target, so only the much smaller metadata dependency graph is duplicated. Swift and Kotlin compilation use their own build-system caches and do not run through sccache.

The Windows Swift lane consumes a Rust `x86_64-pc-windows-msvc` DLL and MSVC import library. This matches Swift's reported `x86_64-unknown-windows-msvc` target and Windows C ABI. The LLVM-MinGW `x86_64-pc-windows-gnu` Rust/JNI artifacts are a separate Kotlin/JVM spike lane and must not be linked into the Swift executable.

The Windows lane is supplementary. It may prove generated-source compilation, pure Swift API ergonomics, Windows FFI loading, callback/thread behavior, explicit disposal, language-level ARC, payload ownership, and host performance where the generated BoltFFI runtime is portable. It cannot prove CoreBluetooth, Objective-C interoperability, Apple dispatch/CoreBluetooth queue semantics, XCFramework/SPM Apple packaging, signing, simulator/device behavior, or the final Apple object graph. Those remain macOS gates, and Windows success does not satisfy the Apple packaging requirements below.

### 7.1 Build and packaging

1. Generate Swift bindings from the same Rust contract revision as the other lanes.
2. Build the native artifact and XCFramework/SPM package from clean outputs.
3. Consume the package from a clean Swift executable or test project.
4. Run in a fresh process and record generated wrapper initialization, explicit disposal, ARC `deinit`, and native final-free counters.

### 7.2 Runtime cases

- Implement the final inert Swift backend and retain the generated token after successful bind.
- Verify capabilities is queried once and bind once through the generated foreign protocol.
- Push synchronously before command return, from a serial `DispatchQueue`, and from concurrent queues.
- Run bind failure, repeated bind, registration rollback, dispose/unregister/late callback, and stale-token isolation cases.
- Race explicit disposal, queue callbacks, wrapper release, and ARC `deinit`; each cleanup counter must reach exactly one.
- Run the event-shape vectors and payload ownership stress through generated Swift values.
- Validate declared Swift error mapping for capabilities, bind, commands, and dispose.
- Run representable unexpected foreign-error and Rust panic/trap fault cases in subprocesses; no unwind may cross the ABI and no failure may be reported as success or `Delivered`.

## 8. Typed errors and unexpected failures

### 8.1 Generation matrix

Generate and compile these shapes in the same public Rust crate:

- public typed errors using the production `#[boltffi::error] + derive_more::Display/Error` combination;
- `#[boltffi::data]` error records using the same Rust `Display/Error` derives;
- a minimal probe recording the current scanner rejection of `thiserror`'s required type-level, unqualified `#[error(...)]` helper; it remains expected-failure until [BoltFFI #870](https://github.com/boltffi/boltffi/issues/870) is fixed;
- `Result<BackendCapabilities, BleHubError>`;
- `Result<(), BleHubError>` for bind and dispose;
- `Result<(), CommandDispatchError>` for every command;
- `Result<BleHub, BleHubError>` for construction;
- `EventIngressStatus` carrying `Closed`, `InvalidEvent`, and `DownstreamFailed` outcomes;
- conversion from `UnexpectedFfiCallbackError` where it is semantically safe and observable.

Rust acceptance requires public error types to implement `std::error::Error`, preserve ergonomic `?` conversion, and retain typed distinctions. Kotlin and Swift acceptance requires generated callers to distinguish declared variants without parsing display text.

If macro composition fails, spike both alternatives before changing the Plan:

1. handwritten `Display`/`Error` implementation;
2. a dedicated FFI wire error converted at the Rust boundary.

### 8.2 Boundary behavior

| Fault | Required mapping |
|---|---|
| Expected backend rejection | Declared typed construction, dispatch, or lifecycle error |
| Rust downstream returns `EventSinkError` | `DownstreamFailed`; current event attempt is consumed and not retried |
| Event shape fails validation | `InvalidEvent`; no downstream call |
| Missing/closed ingress | `Closed`; backend stops using that ingress |
| Unexpected foreign exception/error | Explicit failure; never cleared and replaced by success/`Delivered` |
| Rust downstream panics in fault injection | Contained at the outer FFI trampoline; never presented as an ordinary typed sink rejection |
| Backend emits an event and then returns dispatch `Err` | Conformance failure and blocker |

Error and panic assertions must check the returned value, emitted events, process exit, cleanup counters, and diagnostic channel together. A log message without a failing API result is not containment.

## 9. Complex event generation

Use one semantic vector set covering every `BleEventPayload` variant, including nested records, IDs, `Option`, strings, byte vectors, GATT trees, platform error details, and closed enums.

Run two candidate representations when the direct complex enum is not clearly maintainable:

1. native BoltFFI complex enum;
2. tagged record with one tag and exactly one matching body.

For each Kotlin and Swift candidate, verify:

- generation succeeds without handwritten unsafe glue;
- every variant can be constructed in Rust, observed in the consumer, returned or re-encoded, and compared with the source vector;
- absent versus present optional values remain distinct;
- unsigned IDs and bytes preserve their full intended range;
- unknown or mismatched tag/body input is rejected before downstream delivery;
- generated call sites permit exhaustive, readable discrimination without parsing strings;
- generated source and binary growth are recorded;
- adding a representative variant produces an understandable lockstep ABI diff.

Pass the native enum only if both language lanes produce maintainable, unambiguous APIs. Otherwise, report the tagged-record result to research and propose the ABI choice in the Plan; the spike must not silently switch representations.

## 10. Lifecycle and payload ownership stress

Run at least 10,000 complete cycles per runtime lane:

```text
construct backend
construct Hub
query and cache capabilities
bind token
register Weak entry
issue command with synchronous or asynchronous callback
explicitly dispose or exercise Drop/deinit fallback
unregister
release wrapper
assert baseline
```

Vary:

- successful assembly, bind failure, registration failure, command failure, and dispose failure;
- no callback, one callback, callback-before-return, burst callbacks, and late callbacks;
- single-thread, sixteen-thread contention, serial queue, and concurrent queue;
- zero-byte, 20-byte, 244-byte, 1024-byte, final maximum, and rejected-over-limit payloads;
- downstream success, typed failure, delayed return, and panic fault injection;
- explicit lifecycle call, wrapper close/deinit, and fallback release ordering.

After each batch, not merely process exit, assert:

- registry entries equal the initial baseline;
- backend, state, sink, wrapper, native handle, and payload sentinel counts equal baseline;
- active callbacks are zero;
- cleanup and final free occurred once per object;
- stale-token probes delivered zero events to subsequent Hubs;
- resident memory has no monotonic growth unexplained by bounded runtime caches.

For payload ownership, attach a drop sentinel or equivalent native allocation counter to test payloads where possible. Dispatch completion must release bridge-owned copies even under notification bursts, downstream failure, close, and late-event rejection.

## 11. Performance lane

Performance results are comparative evidence, not a product SLA. Run optimized builds on an otherwise idle host, pin the fixture configuration, warm up each runtime, and report median, p95, p99, maximum, throughput, and allocation counts.

Workloads:

- no-op Rust sink to isolate bridge overhead;
- recording/dispatch-only Rust sink;
- 20-, 244-, and 1024-byte events;
- one producer and sixteen producers targeting one Hub;
- multiple active Hubs to detect unintended global serialization;
- close while callbacks are idle and while one callback is in flight;
- registry insert/remove churn separate from normal read-heavy lookup;
- `ArcSwap<HashMap>` lookup versus a simple `RwLock<HashMap>` baseline using the same vectors;
- token path versus direct handle after the future probe becomes runnable.

Provisional spike pass criteria:

- no deadlock, unbounded latency growth, or throughput collapse under bounded load;
- independent Hubs are not serialized by a registry guard held during downstream work;
- the selected registry has no repeatable greater-than-20% throughput regression or greater-than-25% p99 regression against the simpler baseline without a documented compensating benefit;
- close latency is bounded by the deliberately in-flight synchronous callback plus cleanup, not by callbacks admitted after the closed flag;
- payload and lifecycle counters return to baseline after measurement.

A threshold miss does not authorize an architecture change in this file. Record the data, investigate the cause, and feed a recommendation to research and Plan review.

## 12. Packaging gates

A runtime lane is incomplete until a clean consumer proves artifact boundaries:

- no dependency on generated files outside the published artifact;
- no accidental use of a locally installed native library;
- exported symbols resolve in a fresh process;
- Rust core, generated bindings, backend, DTOs, and closed enums come from one ABI-major contract build;
- lifecycle shutdown succeeds when the consumer owns the last wrapper reference;
- Kotlin/JVM consumer passes now; Android AAR and ART checks are reported separately when that lane runs;
- Swift XCFramework/SPM consumer passes on the supported host target before any iOS BLE claim.

## 13. Pass and blocker criteria

### 13.1 Phase 0 mechanism gate and final pre-freeze gate

Phase 0 passes this mechanism gate only when every applicable criterion below passes for the representative production-shaped token APIs and object graph. Before the Phase 4 production ABI freeze, the same applicable catalog must be rerun against the complete ABI candidate; only that rerun can satisfy the final pre-freeze gate.

- Kotlin/JVM and Swift generate, compile, run, package, and release the exact candidate token APIs under test.
- Capabilities is queried once, bind is one-time, and no callback is delivered during bind or before registration.
- All assembly and registration failures roll back without exposing a Hub, retaining a token, leaking an entry, or cleaning up twice.
- Token `0` is invalid; allocation never reuses or wraps; stale tokens never hit a new Hub.
- Registry entries hold only `Weak`; snapshot guards do not enter state/downstream/foreign code; counts return to baseline after 10,000 cycles.
- Command/close, push/close, push/unregister, dispose/push, wrapper-release, and late-callback races have deterministic safe outcomes.
- Close drains admitted downstream calls; after close returns, active downstream callbacks remain zero.
- Declared typed errors are usable in Rust, Kotlin, and Swift; unexpected foreign failures and Rust panic do not cross the ABI or masquerade as success/`Delivered`.
- One event representation passes all semantic vectors and has a reviewable generated API in both languages.
- Performance and packaging gates pass with recorded, reproducible evidence.

### 13.2 Production blockers

Any of the following blocks ABI freeze regardless of other passing cases:

- crash, UAF, double free, permanent deadlock, stale-event misdelivery, token collision/reuse, or reference/registry leak;
- unchecked Kotlin/Swift failure cleared or converted to command success, construction success, or `Delivered`;
- Rust panic/unwind crossing the FFI boundary;
- callback accepted into downstream after close has completed;
- bind or registration failure exposing a partially usable Hub;
- event followed by dispatch `Err`, or dispatch `Err` followed by an event for the same command;
- cleanup, unregister, or native final-free occurring more than once;
- a generated API that cannot represent the selected typed errors or event variants without ambiguous/string-parsed behavior;
- inability to build and run a clean consumer from produced artifacts;
- unexplained performance behavior indicating global serialization, unbounded close delay, or bridge-level payload retention.

Failures specific to Android ART/Handler/Looper or AAR packaging block the Android lane, not the desktop JVM evidence; they must still be closed before Android production support. Swift host success similarly does not substitute for later iOS device BLE validation.

## 14. Future direct-handle probe

Keep `spikes/boltffi-class-handle-callback-param/` pinned as an expected-failure capability probe on revisions that lack issue #869 support.

On every BoltFFI upgrade:

1. run the probe against the exact pinned revision;
2. if it still fails with the known unsupported-shape diagnostic, record the expected failure and continue using the token lane;
3. if generation succeeds, change the capability result to expected-pass and enable a runtime direct-handle lane;
4. do not delete the token registry or change the Plan based on syntax acceptance alone.

The runtime direct-handle lane must repeat, with `BackendEventIngressHandle` replacing only token routing:

- Kotlin and Swift wrapper retention across callback threads/queues;
- one-time bind, bind failure, repeated bind, registration-equivalent rollback, and no push during bind;
- command-before-return callback, close drain, dispose/late callback, wrapper release races, and panic/exception subprocesses;
- 10,000 lifecycle cycles and all object/drop counters;
- clean Gradle and XCFramework/SPM consumers;
- the same payload and performance workloads, with direct handle no worse than the accepted token path under the recorded provisional thresholds.

A direct-handle migration may be proposed only after both language lanes pass and the generated API is maintainable. The proposal must preserve the assembly and rollback semantics identified by the Plan, coordinate the generated backend ABI update, remove rather than indefinitely duplicate the token entry point, and be reviewed in `.plan/PLAN.md`.

## 15. Result promotion

For each completed spike group:

1. attach raw logs, generated API excerpts, counters, and benchmark data to the test/CI artifact;
2. write or update a durable evidence report under `docs/research/`, explicitly labeling verified, inferred, and unresolved conclusions;
3. identify whether the result confirms the current Plan, reveals an implementation constraint, or requires an architecture decision;
4. edit `.plan/PLAN.md` only through explicit review when a normative contract, gate, representation, dependency, or migration condition changes;
5. keep this catalog focused on execution coverage. Update it when fixtures or regression cases change, not as a substitute for research conclusions or Plan decisions.
