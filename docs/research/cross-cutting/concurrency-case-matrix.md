# BleHub concurrency case matrix and engineering guidance

Original evidence date: 2026-09-03. The current decision sections are aligned
with the user-authorized lifecycle/platform/Tauri refactor; this update does not
add a test run or relabel historical passes.

## 1. Purpose

This document is the practical concurrency checklist for BleHub core and platform backends. It complements the normative architecture in [the Plan](../../../.plan/PLAN.md) and the lower-level FFI ownership analysis in [FFI lifecycle and concurrency](../integrations/ffi-lifecycle-concurrency.md).

The goal is not to make every possible recursive call legal. The goal is to support the concurrency that native BLE platforms and real applications cannot avoid, while keeping ownership, event ordering, and shutdown behavior reviewable.

The current design deliberately distinguishes:

- supported multi-threaded calls and native callback races;
- unsupported synchronous reentry from the downstream sink into the same Hub;
- caller-owned asynchronous follow-up work;
- backend-owned native serialization and correlation;
- Hub-owned assembly, ingress, and close boundaries.

## 2. Ground rules

### 2.1 Commands are dispatch requests

A command method's synchronous `Result` only reports whether dispatch was accepted or rejected. Native/BLE completion is reported later through events.

The backend acceptance point must atomically establish all correlation and cleanup ownership needed for future callbacks. Before that point, rejection must leave no callback source or later event path. After that point, native outcomes must not be converted back into a synchronous dispatch error.

### 2.2 The sink is a prompt handoff boundary

`BleEventSink::on_event` must:

- promptly accept or reject the event and return;
- perform only bounded validation, copying, or enqueueing;
- avoid waiting for BLE progress, native callbacks, backend locks, UI work, disk, or network;
- avoid panic and express expected rejection as `EventSinkError`;
- never synchronously call a command or `dispose` on the same `BleHub`;
- enqueue follow-up work onto caller-owned execution before returning.

The sink may use a channel, actor, runtime task queue, UI dispatcher, or another application-owned mechanism. `Ok(())` means that mechanism accepted the delivery attempt; it does not mean business processing completed.

Synchronous same-Hub sink reentry is a contract violation rather than a supported concurrency case. Supporting it would require recursive delivery semantics, callback-owner tracking, special close behavior, and additional cross-lock rules for little practical value.

### 2.3 Native late callbacks remain supported

Prohibiting sink reentry does not weaken native callback handling. BLE APIs may report callbacks after logical cancellation, disconnect, invalidation, or local teardown. Backends must reject or quarantine those callbacks using exact native identity and lifecycle correlation rather than assuming cancellation creates a hard callback boundary.

### 2.4 Core owns no execution runtime

BleHub core does not add a worker thread, executor, mailbox, timer, retry loop, or public event pump. Native backends use unavoidable platform execution contexts. Downstream applications own business execution.

## 3. Synchronization boundaries

| Boundary | Ownership | Protects | Does not protect |
|---|---|---|---|
| Hub command/close gate | `BleHub` | Backend slot and command-versus-dispose exclusion | Full cleanup completion, event delivery, BLE state, downstream queue |
| Lifecycle mutex and Condvar | `BleHub` | `Open -> Closing -> Closed(result)`, unique closer, concurrent waiters, cached cleanup result | Command dispatch, ingress, native cleanup execution, BLE state |
| Event ingress mutex | `EventIngressState` | Binding/active/closed phase, sink ownership, serial `on_event`, close drain | BLE correlation, command state, downstream business work |
| Apple serial queue | `AppleBleBackend` | CoreBluetooth objects, delegate callbacks, Apple backend mutable state | Rust Hub ownership, downstream execution |
| Connection/GATT/RSSI controllers | Platform backend | Native correlation, generations, FIFO ownership, retirement barriers | Caller timeout/retry/business state |
| One-shot assembly registry mutex | `crates/blehub` assembly | Ticket/entry transitions and ownership extraction | Foreign construction, final sink/entry destruction, Hub cleanup, event routing |
| Tauri emission gate and task ownership | Integration | Entered emission versus stop, bounded transport and task retirement | Core ingress/native cleanup, frontend processing or caller command state |

The supported lock/lifecycle order is:

```text
command:
  Hub read guard
  -> short backend dispatch
  -> release read guard

dispose owner:
  lifecycle mutex: claim Open -> Closing, then release mutex
  -> Hub write guard waits for entered commands
  -> take backend slot and release write guard
  -> ingress close/drain and sink release
  -> backend dispose through local strong ownership
  -> release backend
  -> lifecycle mutex: publish Closed(result), notify all waiters

concurrent/repeated dispose:
  Closing -> Condvar wait loop, releasing lifecycle mutex while waiting
  Closed(result) -> return the same cached success or cleanup error

event:
  backend serialized callback state
  -> construct final BleEvent
  -> ingress mutex
  -> validate and synchronously call prompt sink
  -> release ingress mutex
```

Neither the Hub command gate nor lifecycle mutex crosses ingress drain or native cleanup. `Closing` claims cleanup ownership; taking the backend slot closes command admission; publishing `Closed(result)` proves cleanup completed. An empty slot is not a completion barrier. Condvar waiting adds no core runtime or execution queue. Event ingress does not acquire the command gate, so synchronous backend push during dispatch is allowed; same-Hub sink reentry is not. Platform teardown that may trigger callbacks occurs only after correlation retirement.

## 4. Core and Hub case matrix

| Case | Required behavior | Mechanism | Evidence or test shape |
|---|---|---|---|
| Multiple threads issue commands | Commands may overlap at the Hub boundary; backend sees valid calls without use-after-free | Shared Hub read guard plus backend-specific serialization | Concurrent dispatch stress; backend counters and lifecycle accounting |
| Two commands reuse an exclusive caller ID/resource | Exactly one is accepted according to backend policy; rejection occurs before native submission | Backend serialized validation and correlation registry | Same-ID and same-peripheral collision tests |
| Command starts before dispose | Dispose waits until the entered backend method returns | Read/write exclusion on backend slot | Blocking backend command, concurrent dispose, completion ordering |
| Dispose starts before a new command | Backend slot becomes `None`; the command returns `Closed` and never reaches backend | Slot `take()` under write guard | `dispose_releases_command_gate_before_waiting_for_ingress_drain` |
| Dispose waits on an active sink callback | Command gate is already released; close waits only on ingress mutex | Take slot, release write guard, then ingress close | Block sink, begin dispose, verify new command returns `Closed`, release sink |
| Concurrent/repeated dispose during ingress drain | All callers wait until drain and cleanup finish; none returns success merely because the slot is empty | `Open -> Closing -> Closed(result)` and Condvar wait loop | Block sink, start two disposers, verify both wait for drain/cleanup |
| Concurrent/repeated dispose during backend cleanup | One cleanup owner; every caller returns the same cached result after release | Lifecycle result published only after backend dispose/drop | Block cleanup, start second disposer, then verify common completion/result |
| Backend cleanup returns an error | Hub remains closed, releases safely releasable ownership, and returns the same error on later dispose | `Closed(Some(error))` and notify-all, not `None -> Ok` | Failing-dispose and concurrent cleanup-result tests |
| Backend has an extra external `Arc` owner | Hub still closes ingress and backend releases its root handle | Backend `dispose` contract, weak ingress state | Extra-backend-owner root-release test |
| Hub drops without explicit dispose | Same close algorithm runs as fallback | `Drop` delegates to `dispose` | Lifecycle/drop stress |
| Command gate is poisoned | Close continues safe cleanup; command returns backend-unavailable diagnostic | Poison recovery policy at narrow lock boundary | Focused poison injection where practical |
| Constructor capability query fails | Backend is best-effort disposed; sink is released; no ingress bind remains | Constructor rollback | Assembly-failure tests |
| Rust-owned host drops an assembly before attach | Remove the ticket under registry mutex; release the pending sink after unlocking | `BackendAssembly` RAII extracts the entry before destruction | Pending sink destructor that checks/reenters assembly registry |
| Assembly finish/cancellation encounters an unexpected entry | Extract first; destroy entry/foreign ownership and dispose unclaimed Hub outside the registry mutex | Entry ownership, not just explicit dispose calls, obeys the lock boundary | Unexpected-entry/finish-failure destruction and rollback cases |
| Assembly guard drops while foreign attach is constructing the Hub | Attach observes cancellation; any successfully constructed Hub is disposed outside the registry mutex | `Attaching -> Cancelled` transition plus post-construction check | Blocking attach/drop race test |
| Assembly guard drops after attach but before finish | Unclaimed Hub is removed and disposed exactly once outside the registry mutex | `Attached` ownership remains in the guard-controlled slot | Drop-attached assembly test |
| Assembly finishes successfully | `Arc<BleHub>` ownership transfers to the Rust caller; guard drop performs no cleanup | Consuming `finish()` marks the guard complete only after a successful take | Finish-transfer test |
| Duplicate, unknown, or late foreign attach | Reject without replacing a live slot or aliasing an old Hub | Monotonic ticket plus explicit entry-state validation | Unknown/duplicate/late attach tests |
| Backend pushes while ingress is binding | Push returns `Closed`; activation fails; constructor rolls back | `Binding { push_attempted }` phase | Bind-time push test |
| Bind fails after retaining handle incorrectly | Hub closes state and invokes backend cleanup; weak route dies | Constructor rollback and weak ingress state | Malicious/error backend assembly test |

## 5. Event-ingress case matrix

| Case | Required behavior | Mechanism | Important limitation |
|---|---|---|---|
| Multiple native callback threads push simultaneously | `on_event` calls for one Hub never run concurrently | One ingress mutex covers validation and sink call | Mutex acquisition order is observed delivery-attempt order, not a platform causal total order |
| Push races with external close | An admitted callback finishes; close then marks closed and releases sink | Same mutex is delivery and close-drain boundary | Sink must return promptly |
| Push after logical close | Return `Closed`; do not call sink | Closed phase | Backend stops the current event batch after `Closed` |
| Push after Hub/state destruction | Weak upgrade fails and returns `Closed` | Handle owns only `Weak<EventIngressState>` | Late native handles are safe no-ops |
| Invalid event reaches ingress | Reject current attempt as `InvalidEvent` | Wire-invariant validation under ingress mutex | Backend logs and continues later events; it does not retry |
| Sink returns typed rejection | Return `DownstreamFailed` for that attempt | `EventSinkError` mapping | Rejection is not a BLE failure and does not restore correlation |
| Sink panics | Contain panic at the exact sink-call boundary and return `DownstreamFailed` | Narrow `catch_unwind` | Panic remains a sink contract violation |
| Sink synchronously reenters same Hub | Unsupported and prohibited | Contract and caller-owned handoff | Implementations must queue follow-up work instead |
| Sink blocks indefinitely | Violates contract and can block native callback progress and close | Contract, bounded downstream design | Use nonblocking/bounded enqueue with telemetry |
| Domain event is rejected but terminal remains | Backend consumes rejection, completes local cleanup, and still attempts terminal | Per-event ingress status handling | Only `Closed` stops the rest of a batch |

## 6. Native connection and adapter case matrix

| Case | Required behavior | Apple implementation strategy |
|---|---|---|
| Scan callback arrives after stop | Do not emit against the stopped scan session | Retire active scan session before `stopScan`; observation requires active session |
| Advertisement fails local name/company filter | Do not allocate public ID or retain peripheral | Apply local predicate before identity allocation/retention |
| CoreBluetooth service-filter match hides advertised UUID fields | Keep public advertised services limited to actual service/overflow fields | Internal native-filter evidence may satisfy the service predicate; never append request UUIDs |
| HIL selects a provider with hidden advertised fields | Match the issued single-provider scan and its session; multiple filters prove only OR | Selection evidence stays in HIL, never mutates the public observation |
| Same peripheral appears in later scan | Preserve backend-private public identity | Stable peripheral-object/identifier mapping within backend lifecycle |
| Connect and second connect target same peripheral | Reject second lifecycle before native connect | One active lifecycle per peripheral identifier and object route |
| Connection ID is already in use | Reject before native operation | Connection ID registry under serial queue |
| Disconnect accepted while connect is pending | Retire logical work, cancel native connect, wait for terminal native route | `disconnecting(pendingConnect: true)` phase and exact route |
| Late `didConnect` arrives after disconnect request | Do not restore connected state; cancel again | Disconnecting route returns `cancelAgain` |
| `didFailToConnect` and `didDisconnect` compete | Only the callback that still owns the route can complete it | Route removal before event composition |
| Passive disconnect occurs while connected | Emit command-less disconnected state; fail pending native work once | Exact peripheral route retirement, then event composition |
| Adapter becomes unavailable during connect | Fail connect and terminal exactly once when state loss supplies completion evidence | Drain connection route and compose connect failure |
| Adapter becomes unavailable during accepted disconnect | Preserve successful logical disconnect semantics | Emit successful `Disconnected`, then successful disconnect terminal |
| Adapter loss with active scan | Stop native scan and emit one failed scan-stop result | Retire scan session before event push |
| Adapter loss with pending GATT/RSSI | Retire each accepted operation once before native cleanup | Controller retirement under Apple serial queue |
| Dispose races with CoreBluetooth callback | Whichever enters the Apple queue first owns the transition; later route lookup fails | Backend-owned serial queue and closed lifecycle |
| Callback arrives from replacement object with same UUID | Ignore it | Exact `ObjectIdentifier` plus generation, not UUID alone |
| Connection generation/ID space exhausts | Fail closed without wrap or reuse | Monotonic allocator with explicit exhausted state |
| Two Apple backend instances compete for controller ownership | Reject the second active owner | Process-level Apple backend lease |

An accepted operation does not imply that every native platform will eventually call back. Caller-owned timeout policy remains necessary. When a concrete state transition such as adapter loss or route retirement gives the backend a completion point, the backend makes at most one terminal attempt. Hub disposal is different: it abandons pending projections and revokes not-yet-mature terminal obligations rather than manufacturing command events during shutdown.

## 7. GATT discovery and I/O case matrix

| Case | Required behavior | Apple implementation strategy |
|---|---|---|
| Duplicate service/characteristic/descriptor UUIDs | Preserve every native instance | Native-object-backed opaque IDs; never UUID-key operation maps |
| Discovery callback belongs to old connection generation | Ignore it | Connection generation and exact peripheral identity |
| Partial aggregate discovery fails | Publish no partial replacement database | Draft hierarchy committed only after every level succeeds |
| Services invalidate during pending discovery | Fail pending discovery and quarantine stale callbacks | Service-invalidation generation quarantine until connection retirement |
| Rediscovery starts with active subscriptions | Native unsubscribe each route before replacing database | Shared FIFO emits ordered retirement actions |
| Rediscovery unsubscribe callback fails | Do not keep ambiguous routes alive | Fail closed and cancel connection |
| Multiple GATT operations on one connection | Preserve FIFO and one native callback owner | Shared per-connection I/O/subscription queue |
| Different connections have work | Progress independently | One queue per exact connection correlation |
| Queue reaches capacity | Reject before mutation or native call | 1,024-operation per-connection admission cap |
| Read callback targets wrong characteristic | Ignore; do not consume current operation | Exact characteristic object and callback kind |
| Write-with-response callback is late | Consume retired barrier or ignore stale route | Exact callback barrier installed during retirement |
| Write-without-response has no capacity | Wait for native ready callback | `canSendWriteWithoutResponse` plus ready callback |
| Write-without-response is submitted | Terminal means local CoreBluetooth submission only | Complete immediately after native method call, never claim remote receipt |
| Payload exceeds wire hard limit | Reject synchronously before acceptance | Shared DTO validation |
| Payload exceeds current platform write maximum | Produce accepted command terminal failure without native write | Separate with-response/without-response native limits |
| `didUpdateValueFor` could be read or notification | Never guess using "next value" heuristics | Explicit read/subscription mutual exclusion and exact route arbitration |
| Active notification value races with read | Route to subscription; disallow ambiguous read admission | Subscription route takes precedence while active/transitioning |
| Notification enable callback reports wrong native state | Fail command without falsely publishing enabled | Compare requested state with `isNotifying` |
| Disable fails | Preserve the still-active subscription route | Restore active phase and continue accepting values |
| Disable succeeds then a stale value arrives | Preserve old correlation only for quarantine; never route to a new subscription/read | Value-route retirement barrier |
| Disconnect/dispose with active notification | Retire logical route first, then best-effort native unsubscribe | Ordered identity-deduplicated cleanup characteristics |
| Native unsubscribe itself triggers callbacks | Callback sees retired route/barrier and cannot resurrect state | Callback-first teardown |
| Callback/value-route barriers exceed safe capacity | Emit one explicit internal safety fault and globally fail closed | Safe cap 128, retain 129th route, latch once, reject future admission |
| Safety fault drains unrelated live routes | Fail each pending command once, stop scan, cancel connections, emit backend fault | Backend-wide safety handler after controller latch |

### Windows driver and cleanup ownership

The Windows state mutex is the authority for driver ownership, not queue
emptiness. Admission claims `io_driver_running` under that lock before the
native driver starts; callback completion and driver yield/retirement transfer
ownership under the same lock. An empty queue can coexist with an active
driver between phases, so checking only `is_empty()` could start a second owner.
See archived [`gatt/io.rs`](../../../platforms/archive/windows/src/gatt/io.rs).

A disconnect callback may inspect the cleanup gate before taking final locks,
but must recheck it under the final state lock before retiring a connection.
If cleanup began meanwhile, defer the disconnect and let the cleanup path drain
it. The earlier check is not sufficient against a check-to-use race. See
archived [`connection.rs`](../../../platforms/archive/windows/src/connection.rs) and
archived [`gatt/tests.rs`](../../../platforms/archive/windows/src/gatt/tests.rs).
These are source-backed invariants, not a claim of a Windows runtime rerun.

## 8. RSSI case matrix

| Case | Required behavior | Apple implementation strategy |
|---|---|---|
| Two RSSI reads target one connection | Reject the second before native submission | One pending RSSI read per connection |
| RSSI callback comes from stale generation | Ignore | Exact connection generation |
| RSSI callback comes from replacement peripheral object | Ignore | Exact `CBPeripheral` object identity |
| Native callback contains an error | Emit one failed command terminal | Conservative connection-error mapping |
| Native callback has missing/non-integral/out-of-range value | Emit one failed terminal without truncation | Exact `Int16` conversion |
| Disconnect retires pending RSSI | Fail it once and install a peripheral callback barrier | RSSI controller retirement |
| Late RSSI callback arrives before lifecycle reuse | Consume barrier silently | Exact retired-peripheral route |
| New lifecycle attempts RSSI before late route is safe | Reject admission | Retirement barrier |
| macOS/iOS use different delegate completion paths | Keep one shared controller and platform delegate adapters | iOS modern `didReadRSSI`; macOS documented legacy callback/value path |

## 9. Notification and native-cleanup ordering

Native teardown must follow this logical order:

```text
retire public/native correlation
-> remove callback ownership
-> construct pending command failures or lifecycle outcomes
-> best-effort native unsubscribe/cancel/close
-> release child native objects
-> release parent peripheral/manager ownership
-> attempt already-composed events in contract order
```

The exact location of event delivery relative to native cleanup depends on the operation's contract, but no native callback may regain ownership after retirement. A failed best-effort native unsubscribe does not restore a logically retired subscription.

For Apple, `setNotifyValue(false, for:)` is best effort during disconnect/dispose/adapter-loss cleanup. It is not treated as a guaranteed radio-level acknowledgement, and disposal does not emit synthetic command events.

## 10. Error and event-ordering rules under concurrency

- A synchronous dispatch `Err` means no event source for that command was accepted.
- Once any event for a command is emitted, the backend method must return dispatch success.
- A successful operation emits its domain event before successful `CommandCompleted`.
- A terminal failure normally emits only failed `CommandCompleted`, unless a separate passive domain transition is required.
- Passive platform changes use `command_id = None` unless there is proven causality to an accepted command.
- `InvalidEvent` and `DownstreamFailed` consume one delivery attempt; they do not undo backend state.
- `Closed` stops further delivery attempts to that ingress.
- Disposal emits no command events and causes caller projections to become abandoned.
- Native callback absence is handled by caller timeout policy, not by a core timer.
- Foreign failure logging uses fixed diagnostic/error classes and controlled context, not arbitrary foreign error/sink/panic text. Public typed errors and ingress result messages retain their contract data; redacted logging must not erase public error details or become string-parsing control flow.

## 11. Common failed designs and lessons

### 11.1 Do not support unnecessary sink reentry

Allowing `on_event -> same Hub command/dispose` creates recursive delivery, command/close lock-order, and callback-lifetime problems. A one-step caller-owned queue handoff removes the entire class without reducing real BLE capability.

### 11.2 Do not hold Hub ownership locks across ingress drain or native cleanup

Once the backend slot is `None`, new wire-valid commands can return `Closed`, but another disposer must still wait for `Closed(result)` through the lifecycle Condvar. Keeping the write guard while waiting for callbacks or foreign cleanup permits AB/BA cycles; removing that guard without adding a completion barrier permits premature success. Both boundaries are required.

### 11.3 Do not trust cancellation as a callback boundary

CoreBluetooth, WinRT, Android Bluetooth, and BlueZ may report callbacks after logical cancellation. Retire correlation before native teardown and retain bounded stale-route evidence when callback kinds cannot otherwise be distinguished.

### 11.4 Do not key GATT work by UUID alone

Duplicate UUID instances are legal. UUID maps merge distinct native attributes and allow a callback for one instance to complete another operation.

### 11.5 Do not globally poison silently

A bounded safety structure must have an explicit failure policy. Silent barrier drop can route stale callbacks to reused lifecycles; silent global poisoning leaves unrelated resources alive with no observable backend fault.

### 11.6 Do not overstate native completion semantics

No-response write completion is local submission. Logical disconnect does not guarantee the physical link is already gone. Best-effort unsubscribe does not prove remote CCCD state. Reports and events must preserve these boundaries.

### 11.7 Keep lock roles disjoint

A lock should protect one ownership domain. Hub ownership, event delivery, and native BLE state use separate boundaries with an explicit order. Avoid taking one while calling code that can acquire another unless the contract proves the call cannot reenter.

### 11.8 Prefer deterministic state-machine tests before RF

Most race invariants can be tested using exact fake object identities, scripted callback order, barriers, and lifecycle counters. RF then validates platform behavior rather than trying to discover basic state-machine bugs nondeterministically.

### 11.9 Tauri handoff and teardown are integration responsibilities

The maintained plugin splits setup/lifecycle, commands, events, and projection
into separate modules. Its sink uses bounded futures MPSC `channel(128)` plus
one reserved slot for its single sender. Overflow closes the source, wakes the
task, best-effort projects a transport error, and initiates Hub cleanup. It does
not silently drop a terminal and continue, guarantee fault delivery, or inject
a synthetic BLE fault into core ingress.

| Case | Required behavior | Boundary |
|---|---|---|
| Native callback hands off an event | Prompt `try_send`; no WebView work on callback stack | Sink/channel |
| Full queue or failed emission | Retire transport and trigger cleanup; best-effort overflow diagnostic only | Plugin-owned task; no retry or core protocol change |
| Close races with active emission | Wait for entered emit, prevent later emit; no emission lock across native cleanup | Emission quiescence before Hub dispose |
| Close with queued or idle task | Discard buffers, abort task; async close awaits the taken task handle's destruction | Plugin stops its own task, not core |
| App exit is requested but cancelled | Keep the plugin alive | Use actual `Exit`, not `ExitRequested` |
| Plugin is removed | Run stop/cleanup through `on_drop` | Same retirement path as app exit |
| Install twice or reinstall after removal | Reject; a closed Hub is not reusable | One managed installation per app; restart needs a new app and Hub |
| Rust listener tears down plugin synchronously inside emit | Prohibited; schedule follow-up work | Avoid reentering the emission gate |
| Frontend sees acknowledgement without terminal/domain | Keep waiting under caller policy | Example command tracker, fresh string IDs, typed state/error matching |

This does not recall events already emitted to the WebView, nor does it require
core to drain a downstream queue. See [plugin source](../../../integrations/archive/tauri-plugin-blehub/plugins/blehub/src/events.rs)
and [lifecycle wiring](../../../integrations/archive/tauri-plugin-blehub/plugins/blehub/src/lib.rs).

## 12. Test strategy

Every concurrency-critical change should select from these test forms:

1. **Pure event composition:** verify exact domain/terminal order without native frameworks.
2. **Controller state-machine tests:** inject exact objects, generations, callback kinds, and stale orderings.
3. **Barrier-based Rust concurrency tests:** block a command or sink callback and assert lock/close linearization without sleeps where possible.
4. **Backend scripted-effect tests:** record native call ordering such as unsubscribe, delegate detach, cancel, and release.
5. **Subprocess tests:** isolate panic, foreign exception, final-release, or deadlock-prone cases with a hard timeout.
6. **Lifecycle stress:** repeat construct/bind/initialize/dispose and assert exact-zero owned resources.
7. **HIL fault injection:** remove the provider/controller or disable the radio while a known callback is active.
8. **RF soak:** validate loss, duplication, ordering, recovery, and post-disable/post-disconnect silence.

Tests should protect a project-specific invariant, not trivial field assignment or delegation.

## 13. Evidence boundaries and open validation

The following is the original 2026-09-03 evidence snapshot, not a result for
the current refactor or frozen ABI:

- Rust core workspace tests pass on the macOS host;
- Hub dispose releases the command gate after backend slot removal and before ingress drain;
- ingress serializes sink callbacks and drains admitted delivery on close;
- Apple controllers cover exact identity/generation, connection races, GATT FIFO and barriers, subscription transitions, service invalidation, and RSSI retirement;
- 67 deterministic Apple tests and unsigned generic iOS simulator/device builds pass;
- an independent review reported High 0 / Medium 0 / Low 0 for the supported concurrency model.

The original snapshot listed these remaining Phase 3 gates (later reports and
the Plan govern their subsequent status; this list is not a new execution ledger):

- production-backend scripted native-effect ordering at the orchestration layer;
- macOS Notification and RSSI RF;
- passive disconnect/recovery and lifecycle/soak HIL;
- staged artifact-only macOS/iOS consumers;
- independently signed, installed, launched, and RF-tested iPhone execution;
- applicable reruns after the production ABI freeze.

For this documentation alignment, the implementation invariants above were
checked against core `hub.rs`/`assembly.rs`, Windows driver/cleanup paths,
Apple advertisement mapping and HIL selection, and the maintained Tauri plugin.
No tests, native builds, Windows runtime, or RF runs are newly claimed here.
The coordinating agent owns the final refactor validation record, including
the nonnative plugin/explicit-demo workspace tests and Apple backend/selection
checks already reported in the task handoff.

`pixi run just tauri::tauri-rust-test` explicitly tests two independent
workspaces: the plugin without default features and the example without default
features plus `demo`. It cannot establish default native linkage, Windows
runtime, macOS/iOS RF, or full lifecycle/soak evidence. Those remain separately
validated on the same candidate. Tauri-specific open work does not block
Phase 5 public ABI freeze; production core/backend/runtime/package obligations
remain unchanged.
