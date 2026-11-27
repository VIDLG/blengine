# BLeng multi-device and actor assessment — 2026-09-26

Status: local-source research and spike implementation evidence. On 2026-09-28 the user approved the BLeng boundaries recorded in [`.plan/PLAN.md`](../../../.plan/PLAN.md), including direct selection of Kameo as the BLeng actor implementation. The standalone spike now pins `kameo = 0.22.2` and has a generated lockfile; this does not add Kameo to production, freeze a Rust/UniFFI/TypeScript API, change the BleHub ABI, or claim a BLeng runtime/RF pass. The single normative architecture plan is [`.plan/PLAN.md`](../../../.plan/PLAN.md).

## What the three RN libraries actually demonstrate

The inspected revisions and broader implementation trade-offs are recorded in the [RN BLE source comparison](../react-native/react-native-ble-library-implementation-comparison-2026-09-26.md). All three expose per-device connection APIs and native state capable of tracking several *different* peripherals:

| Library | Verified local-source evidence | Boundary |
|---|---|---|
| BLE PLX | [`BleManager.js`](../../../../contrib/react-native-ble-plx/src/BleManager.js) exposes `connectToDevice(deviceIdentifier)` and `connectedDevices`; Android [`BleModule.java`](../../../../contrib/react-native-ble-plx/android/src/main/java/com/bleplx/adapter/BleModule.java) keeps device/connection maps keyed by device ID. | A subsequent scan replaces the prior scan listener; the separate iOS `MultiplatformBleAdapter` native engine is not in this checkout. |
| BLE Manager | [`src/index.ts`](../../../../contrib/react-native-ble-manager/src/index.ts) exposes `connect(peripheralId)` and `getConnectedPeripherals`; Android [`BleManager.java`](../../../../contrib/react-native-ble-manager/android/src/main/java/it/innove/BleManager.java) holds a peripheral map and each [`Peripheral.java`](../../../../contrib/react-native-ble-manager/android/src/main/java/it/innove/Peripheral.java) owns its GATT/queue. iOS [`SwiftBleManager.swift`](../../../../contrib/react-native-ble-manager/ios/SwiftBleManager.swift) holds keyed peripherals/connections. | The platform scan is a shared manager resource, not an independent scan for each connection. |
| BLE Nitro | [`NativeBleNitro.nitro.ts`](../../../../contrib/react-native-ble-nitro/src/specs/NativeBleNitro.nitro.ts) accepts a `deviceId` and exposes connected devices; [`BleNitroBleManager.kt`](../../../../contrib/react-native-ble-nitro/android/src/main/java/com/margelo/nitro/co/zyke/ble/BleNitroBleManager.kt) and [`BleNitroBleManager.swift`](../../../../contrib/react-native-ble-nitro/ios/BleNitroBleManager.swift) retain device-keyed GATT/peripheral entries. | Only one active scan per manager is apparent; `findAndConnect` can stop that scan. Android system-connected enumeration is not identical to GATT handles owned by this library. |

These are **API and local implementation observations**, not an audited device-count limit or two-device simultaneous RF result. No such simultaneous RF run was performed in this investigation. BLE controller, OS, peripherals, application lifecycle and platform backend may impose different effective limits.

BleHub's [ID scope](../../../.plan/PLAN.md) already supports separate `ConnectionId` values and GATT sessions per connection. The [backend observable contract](../../../.plan/PLAN.md) limits an active connection to **one per peripheral**, not one peripheral per Hub; [scan policy](../../../.plan/PLAN.md) limits a backend instance to **one physical scan** at a time. The proposed BLeng layer should preserve these two distinct constraints. A single standalone nRF Peripheral alone cannot validate two simultaneous peripheral connections.

## What to borrow for BLeng, and where it belongs

| Reference | Borrow as a design question for Rust BLeng | Do not transplant |
|---|---|---|
| [BLE PLX](../react-native/react-native-ble-library-implementation-comparison-2026-09-26.md) | Application-facing device/connection and GATT-instance navigation; separate operation result from long-lived observation/subscription; explicit destroy, transaction cancellation, restoration as a future capability. Preserve multiple distinct connected devices without making each scan independent. | An eagerly constructed manager, mandatory Base64 values, UUID convenience paths that silently select a duplicate, or its Android Rx/iOS external engine as BLeng implementation. Cancellation of a waiter does not imply an over-the-air rollback; background restoration is not BleHub v0.1. |
| [BLE Manager](../react-native/react-native-ble-library-implementation-comparison-2026-09-26.md) | Operation-by-operation platform coverage checklist; scan generation preventing an old timer from stopping a new scan; per-device GATT callback sequencing, CCCD enable/disable completion, and the ability to continue working with device B after device A disconnects. Its Android scan rejection and iOS discovery validation routes are useful negative tests for never-stranded waiters. | UUID-only characteristic addressing; separate callback dictionaries and timers in the RN adapter; accepting a command without settling its waiter. Descriptor I/O, bonding and MTU are candidate later features, not v0.1 promises. |
| [BLE Nitro](../react-native/react-native-ble-library-implementation-comparison-2026-09-26.md) | Nitro spec/factory shape, bytes/typed-error transport and packaging/reload cases for the **future RN adapter**; verify a host Promise waits for the BLeng-defined outcome. | A second Swift/Kotlin BLE engine, UUID-keyed callback routes, a descriptor-only queue treated as complete GATT serialization, or JS discovery timeout misread as native cancellation. Nitro's packaging patterns belong in RN integration, not in the BLeng domain actor. |

This comparison concerns **design affordances and failure cases**, not a claim that the community libraries were benchmarked or tested for simultaneous RF here. The proposed first BLeng Rust surface should expose scan sessions, per-connection handles, GATT instance identity, subscription teardown, capability/structured errors and explicit close; precise signatures, cache policy and convenience shortcuts require separate API review. UUID lookup may be a later *explicitly ambiguity-rejecting* helper, never a silent first match. BLeng manages caller-side operation/session state; backend owns native objects and GATT FIFO; Nitro owns only host transport/lifecycle.

## Actor library question

The user confirmed the actor library is **Kameo**. Its [official crate documentation](https://docs.rs/kameo/latest/kameo/actor/trait.Spawn.html) describes `Spawn` using a Tokio task and a configurable bounded mailbox; the [request documentation](https://docs.rs/kameo/latest/kameo/request/ask.rs.html) illustrates `ask`/`tell`. This is evidence that Kameo can represent a BLeng actor, **not** evidence that it handles BLE operation/domain/terminal correlation, callback-ingress nonblocking delivery, cancellation, foreign-runtime loading, or native cleanup for us. In particular, a handler must not await the BLE completion before returning to its mailbox: correlate a waiter before dispatch and complete it from a later event. The spike uses the exact `0.22.2` crates.io release, with checksum `7cbab7323ed30490812f43ef6416a9e21bb150e9633e451ed124ca276b1ca82a` recorded in its standalone lockfile. The locally fetched source was audited for the used APIs; this is a release-source audit, not a claim that an upstream git checkout has been added to the workspace.

A single actor with bounded messages and per-connection state is the simplest first model. It can handle short state transitions and waiter registration for several devices without making the native backend's *per-connection* GATT FIFO global. An actor cannot guarantee cross-device progress if it awaits an entire BLE operation, executes blocking dispatch on its own mailbox task, or performs blocking `BleHub::dispose()` on that task. The callback sink must quickly hand events to the actor without waiting for actor state or reentering the Hub. A full mailbox cannot silently discard domain/terminal events and leave resolved-looking but permanently pending requests: define a fail-closed transport fault and settle waiters.

**Approved direction, production dependency added, behavior still gated:** Kameo is selected as the BLeng actor implementation; the project will not maintain a parallel actor-framework comparison. The standalone spike has now proven bounded mailbox overflow/timeout, BLeng-style waiter correlation, two-device interleaving, timeout/cancellation/late-generation rejection, nonblocking handler dispatch, graceful stop, and panic-to-stopped fail-closed behavior against a fake event model. The production `crates/bleng` skeleton pins Kameo and now has an internal bounded `BleEventSink` → actor → receiver transport exercised against a real `BleHub` and fake backend; its API still needs review, including ownership of the Hub, waiter correlation, `BleHub::dispose()` isolation, and fault-to-cleanup wiring. Its supervision/restart machinery must not silently restart a stopped actor while orphaning accepted commands, waiters or backend resources; the recovery policy must be explicit and fail closed. Kameo belongs only in `crates/bleng`, not `crates/blehub`, and does not authorize an executor in the BleHub core or one actor per device without measured need.

## Spike implementation evidence

The non-workspace package at [`spikes/bleng-kameo/`](../../../spikes/bleng-kameo/) is intentionally not production code and is not referenced by RN or long-lived tests. Its eight tests cover:

- explicit bounded mailbox capacity, `try_send` overflow, and bounded mailbox timeout;
- waiter registration before event delivery, domain plus terminal completion, duplicate terminal rejection, and unknown IDs;
- interleaved operations for two connection IDs with one device completing independently;
- timeout and cancellation as BLeng waiter outcomes, with late events rejected;
- connection-generation identity preventing stale events from completing a new operation;
- handler registration/dispatch returning before a later callback event;
- graceful stop and post-stop send rejection;
- actor panic resulting in shutdown rather than an implicit recovered actor.

The lockfile records Kameo `0.22.2` and its transitive Tokio-based runtime dependencies. Validation on 2026-09-28:

```text
cargo test --manifest-path spikes/bleng-kameo/Cargo.toml --locked   # 8 passed
cargo clippy --manifest-path spikes/bleng-kameo/Cargo.toml --locked --all-targets -- -D warnings
cargo fmt --manifest-path spikes/bleng-kameo/Cargo.toml -- --check
```

This spike does not prove native BLE callback delivery, production waiter futures, `BleHub::dispose()` blocking isolation, actor restart policy with live native resources, RN lifecycle, package/symbol gates, or RF behavior. Those remain production BLeng and host-integration gates.

The production boundary is now represented by [`crates/bleng/`](../../../crates/bleng/). It is deliberately an API-neutral skeleton with the exact Kameo dependency and an internal runtime/actor lifecycle slice: actor startup occurs inside `Runtime::enter()`, close is async and idempotent, and drop only performs best-effort actor kill plus nonblocking `shutdown_background` for the internal runtime. Explicit close waits for actor shutdown, but neither path yet disposes a real Hub or promises all Tokio threads have joined. An async-host regression test guards against blocking/panicking on runtime drop. A canonical `BleHub` + fake backend regression sends an event through the production sink into the actor's bounded receiver and checks late-event rejection after close. A saturated receiver or actor mailbox marks a shared fault, wakes a waiting receiver, prevents queued events being treated as success after the fault, and rejects later events. The single atomic ingress phase prevents a normal close racing mailbox rejection from overwriting it with an overflow fault; an event accepted immediately before explicit close can be retired by close and is **not** counted as operation success. Because no Hub owner/waiter registry is connected yet, this is **not** a completed fail-closed cleanup or Promise test. `delegate = 0.13.5` generates mechanical async forwarding (`Bleng::close`); state transitions and resource ownership stay explicit. There is no `#[tokio::main]` or executable entrypoint in the library. `crates/blehub` remains unchanged and owns the synchronous contract. No public constructor, BLE operation, Promise shape, or UniFFI surface has been added yet.

Validation after adding the skeleton and runtime slice:

```text
cargo fmt --all -- --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace --locked -- --test-threads=1   # passed
cargo test -p bleng --locked                        # 7 runtime/transport tests passed
pixi run just lint
pixi run just docs-check-mermaid
```

A parallel `cargo test --workspace --locked` run failed in the unmodified `blehub::assembly::tests::pending_finish_drops_sink_outside_registry_lock_and_completes_nested_cancel` lock-probe assertion. This same failure was previously observed; the serial full workspace run passed. The parallel failure is not claimed as a pass or repaired by this BLeng slice.

## Proposed acceptance evidence

1. Fake source: two peripheral IDs, two connection IDs, overlapping discovery/read/write/subscriptions, interleaved callbacks and one device's passive disconnect; the other remains live. Same-peripheral double connect is rejected; scan stays singleton.
2. Race matrix: register waiter before dispatch; synchronous rejection; event before dispatch returns; domain before/after terminal; failure after domain; duplicate/late events; timeout versus completion; cancellation without claiming native abort; GATT rediscovery makes old handles unusable.
3. Resource matrix: bounded ingress and waiter/subscriber queues, explicit overflow fault, stalled subscriber isolation, close during pending operations, repeated/concurrent close with same cleanup result, no sink reentry and no leaking native root.
4. Two distinct real advertising peripherals with distinct profiles on each supported platform, if equipment permits: connect concurrently, independently discover/read or subscribe, disconnect one while the other continues, then close. Report platform/provider/controller identity and any capacity failures; absence of a second RF provider means **not yet validated**, not pass/fail.
5. Separate RN iOS/Android staged-artifact, generated-binding, single-image, runtime/reload and RF checks after the Rust contract is reviewed; existing low-level BleHub or backend RF evidence cannot be relabeled as BLeng evidence.

Open review decisions: the first public Rust API shape and the actual Rust/foreign-host async bridging strategy (the internal Tokio runtime owner is approved); subscription fan-out/backpressure policy; same-Hub low/high-level write ownership; whether optional convenience caching is worth its lifecycle cost. No automatic reconnect, silent retry of writes or fixed multi-device count is proposed.
