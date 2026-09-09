# Apple architecture hardening and RSSI slice — 2026-09-03

## Result

**PASS for the deterministic architecture-hardening and RSSI slice.**

This result applies to the current unfrozen Phase 3 candidate. It is not an ABI-freeze result, an iPhone runtime result, a development-signing result, or an RF result. No RF command was run for this slice.

## Scope

This slice reviewed and hardened the shared Swift/CoreBluetooth backend used by both macOS and iOS, plus the Rust event-ingress and Hub disposal boundaries that the foreign backend consumes.

The review retained the existing architecture:

- one `BleHubApple` Swift product and one CoreBluetooth implementation for macOS and iOS;
- direct CoreBluetooth rather than a high-level async/reactive BLE wrapper;
- one backend-owned serial `DispatchQueue` for all CoreBluetooth objects, delegate callbacks, and mutable Apple state;
- synchronous fire-and-forget command dispatch with eventual platform outcomes represented as `BleEvent` values;
- caller-owned downstream execution and business state.

## Contract clarification

`BleEventSink::on_event` is a synchronous, short event-handoff boundary. A sink must promptly accept or reject the event and return. It must not block on backend progress, synchronously issue a command to the same `BleHub`, or synchronously dispose the same Hub. Follow-up work must be handed to caller-owned execution such as a channel, actor, runtime, or UI queue.

This keeps ingress synchronization deliberately small: one mutex protects phase, downstream ownership, serialized validation/delivery, and close-drain. BleHub does not add a channel, worker, executor, recursive lock, delivery-owner recursion model, or public event pump.

Native callback races remain supported. In particular, the backend still handles concurrent command/dispose calls, adapter loss, passive disconnect, service invalidation, native callbacks arriving after logical retirement, and callbacks from stale native objects or connection generations.

## Rust lifecycle hardening

`BleHub::dispose` now:

1. obtains the exclusive backend slot guard and waits for already-entered command dispatches;
2. atomically takes the backend from the slot;
3. releases the command/close guard immediately;
4. drains and closes event ingress;
5. disposes the backend through the local strong `Arc`;
6. releases that local backend ownership.

After the slot becomes `None`, new commands can acquire the read guard but return `CommandDispatchError::Closed`; they cannot reach the retained backend. The Hub ownership lock no longer crosses ingress callback drain or native backend cleanup, removing the supported-path AB/BA lock-order risk without weakening backend lifetime.

A deterministic regression test blocks an admitted sink callback, starts disposal on another thread, observes the closed backend slot after the write guard has been released, verifies a new command returns `Closed` while disposal is still waiting for ingress drain, then releases the callback and verifies exactly one backend disposal.

## Apple backend hardening

### Queue and process ownership

`AppleBleBackend` no longer accepts an arbitrary `DispatchQueue`. Its public initializer accepts only a diagnostic queue label and constructs a private serial queue, so the `@unchecked Sendable` safety argument is true by construction. A process lease continues to reject a second active Apple backend and is released by idempotent disposal.

### Scan retention

Locally enforced `namePrefix` and manufacturer `companyId` predicates now run before backend-private peripheral ID allocation and strong `CBPeripheral` retention. Rejected advertisements do not consume IDs or retain invisible peripherals. Accepted peripheral IDs remain stable across scan sessions. The allocator emits every valid `UInt64` ID without wrap or reuse and then fails closed on exhaustion.

### Bounded GATT work and callback barriers

The shared per-connection GATT I/O/subscription FIFO now has a 1,024-operation admission cap and uses a head-indexed compacting queue rather than repeated `Array.removeFirst()` shifts. Rejection occurs before queue mutation. Rediscovery preparation has explicit `ready`, `unsubscribeQueued`, and `unsafe` outcomes: no-subscription state may proceed immediately, active subscriptions are retired through the FIFO before native discovery, and more than 1,024 active subscriptions or an inconsistent route phase triggers the existing global safety-fault teardown rather than silently starting `discoverServices`.

Retired read/write/notification-state callbacks and notification value routes remain correlated by exact native peripheral/characteristic identity and connection/subscription generation. Each barrier class has a safe capacity of 128 routes. The 129th route is retained, one internal safety fault is latched, future GATT admission is rejected, and the backend performs one explicit global fail-closed teardown. The fault no longer silently poisons unrelated routes while leaving the backend apparently ready.

Retirement also returns an ordered, identity-deduplicated list of notification characteristics that may still be active natively. Disconnect, dispose, adapter loss, rediscovery failure, service invalidation, and safety-fault teardown perform best-effort `setNotifyValue(false, ...)` before releasing native ownership where CoreBluetooth is available.

Aggregate discovery now enforces private allocation guards of 128 services, 1,024 total characteristics, and 2,048 total descriptors. These are FFI/allocation limits, not ATT handle, negotiated-MTU, or CoreBluetooth discovery limits. Each callback is checked before its objects enter the pending draft; a one-over result produces a `Gatt` terminal, publishes no partial replacement, and preserves any previously committed GATT session.

### RSSI

`supportsReadRssi` is now backed by a real implementation. `readRssi` validates ready/authorized/powered-on connected state, registers one pending read per connection, and calls `CBPeripheral.readRSSI()`.

Completion correlation requires:

- the active connection ID;
- the exact connection generation;
- the exact native `CBPeripheral` object;
- one pending RSSI command.

A valid result emits `RssiRead` before successful `CommandCompleted`. Native error, missing value, non-integral value, or value outside `Int16` emits one failed terminal. Disconnect, adapter loss, safety fault, and disposal retire pending reads before native teardown and retain an exact peripheral callback barrier so a late callback cannot complete a reused lifecycle.

The shared source implements both checked-in documented delegate paths:

- iOS: `peripheral(_:didReadRSSI:error:)`;
- macOS legacy completion: `peripheralDidUpdateRSSI(_:error:)` with `peripheral.rssi`.

The legacy declaration is availability-annotated so the required macOS path builds without a deprecation warning. No KVC or private API is used.

### Event and error semantics

Ingress return statuses are handled explicitly:

- `Delivered`: continue;
- `InvalidEvent` and `DownstreamFailed`: structured diagnostic, consume the attempt, and continue the batch;
- `Closed`: stop the batch.

Adapter loss during an already accepted logical disconnect now emits successful `Disconnected` state followed by a successful disconnect terminal rather than a contradictory failed connection state plus successful terminal.

CoreBluetooth errors receive conservative category mapping. Known cancellation, timeout, unsupported, ATT permission, and ATT protocol errors map to their unified categories; unknown errors retain the operation fallback category and preserve Apple domain/code/message details. A wire-valid write that exceeds the current CoreBluetooth write maximum is an accepted operation with one `Gatt` terminal and no native write; it is not reported as a capability-level `NotSupported` result.

Connection-unavailable event composition was extracted into a pure internal component and tested for connecting, connected/passive, pending-connect cancellation, and accepted-disconnect cases.

## Validation

The following completed successfully during this slice:

```text
pixi run cargo test --quiet -p blehub
20 passed, 0 failed

pixi run cargo test --quiet --workspace
76 passed, 0 failed on the macOS host

pixi run -e apple-ci just apple check
71 passed, 0 failed
unsigned generic iOS simulator/device builds passed

pixi run -e apple-ci swift build --quiet --package-path tests/hil/apple/macos --product BleHubAppleMacOSProbe
passed

pixi run just fmt
passed

pixi run just lint
strict Clippy, Ruff, formatting, Just formatting, and provider-profile isolation passed

pixi run just docs-check-mermaid
15 Mermaid diagrams in 49 Markdown files passed
```

The focused ingress and lifecycle tests cover:

- bind-phase push rejection;
- weak-handle closure;
- sink panic containment and subsequent delivery;
- external close waiting for an admitted callback;
- disposal releasing the command gate before ingress drain;
- commands observing `Closed` after backend slot removal;
- backend ingress-root release and idempotent disposal.

The Apple tests cover the prior initialization, scan, connection, aggregate GATT discovery, characteristic I/O, and Notification/Indication state machines plus:

- process lease contention/release and faulted lifecycle rejection;
- local scan filtering before ID allocation/retention and allocator exhaustion;
- bounded FIFO admission and explicit rediscovery preparation through the exact 1,024-subscription boundary;
- fail-closed one-over rediscovery without native discovery;
- aggregate GATT-tree exact-boundary and one-over guards for services, total characteristics, and total descriptors;
- bounded explicit safety faults and ordered native unsubscribe plans;
- CoreBluetooth error category/details mapping;
- connection outcome composition during adapter loss and safety teardown;
- RSSI success, failure, exact correlation, retirement barrier, lifecycle reuse, and drain.

An independent follow-up audit initially found three Medium issues outside the already-approved Hub concurrency model: platform write-limit failures used the capability-level category, rediscovery conflated ready and unsafe states, and aggregate discovery lacked explicit allocation guards. After the fixes documented above, a final independent read-only re-review reported **High 0 / Medium 0 / Low 0** across those paths and the adjacent Apple lifecycle/concurrency interactions.

## Subsequent RF evidence

After the deterministic gates and final review, the same current candidate passed the project-managed macOS-to-Android `PlatformDefault` Notification RF workflow, including exact `deadbeef` value delivery, disable ordering, fixture-side rejection plus host silence after disable, logical disconnect, and post-disconnect silence. See the [Apple Notification/Indication slice](2026-09-03-apple-notification-indication-slice.md). This does not establish Indication, passive-disconnect recovery, or iPhone behavior.

## Open Phase 3 gates

This slice does not close the following:

- macOS Indication RF and RSSI RF;
- passive disconnect/recovery and lifecycle/reentrancy/soak HIL;
- staged production Apple artifact packaging and artifact-only macOS/iOS consumers;
- signed installation and launch on the connected iPhone 14;
- independent iPhone CoreBluetooth and RF validation;
- reruns required after the Phase 5 ABI freeze.

Before any RF command, the DUT, provider, expected duration, and exclusive resources must be stated and the user must provide fresh explicit confirmation.
