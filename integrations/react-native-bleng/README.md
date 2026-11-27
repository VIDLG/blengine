# React Native BLeng integration boundary

> 中文镜像：[`README.zh-CN.md`](README.zh-CN.md)。

Using the package: [`API.md`](API.md) starts with a five-step quick start, then explains the three
shapes and the failure contract.
[`examples/react-native-bleng-quickstart`](../../examples/react-native-bleng-quickstart/README.md)
is a runnable scan → connect → read → subscribe screen. This file describes ownership and build
boundaries instead.

`BlengProvider` / `getBlengCentral()` keep one central per JS runtime, so React Fast Refresh
keeps the same central and live capabilities. A full reload is a new JS runtime and requires
reconnect; mounting or unmounting the provider never starts or closes BLE. Explicit teardown is
`closeBlengCentral()`.

This directory is the production Apple + Android BLeng package line. It is intentionally separate from:

- `tests/consumers/android/bleng`, which is an artifact-only consumer and test oracle.

The formal RN line has its own [clean consumer host](../../tests/consumers/react-native-bleng/README.md)
and has no second source root. Formal native bridge contract tests live in that consumer's
Swift/Kotlin test targets, not in this library.

The maintained mobile modules consume only published production artifacts:

```text
Android: standard RN Android library module + dev.blehub:bleng:0.0.0 + dev.blehub:blehub-android:0.0.0
Apple:   staged combined apple-bleng UniFFI package + BleHubApple
```

The Apple package line is staged explicitly with:

```text
pixi run just react-native stage-apple-bleng
```

This is separate from the default `apple-production` staging used by the BleHub-only RN package.
Both lanes copy the shared backend Podspec from
[`platforms/apple/packaging/BleHubApple.podspec`](../../platforms/apple/packaging/BleHubApple.podspec),
not from integration-owned duplicates. This package retains its backend SwiftPM manifest under
`packaging/apple/Package.swift`; `tools/react_native/stage_bleng_apple.py` generates its combined
UniFFI Podspec. The BleHub-only lane keeps its own UniFFI template and native image.

The formal iOS consumer has one maintained compile entry:

```text
pixi run just react-native formal-host-ios-build
```

It stages the combined Apple package and checks/generates the locked Nitro glue; Node and Pod
installation remain explicit prerequisites, not work hidden in the ordinary build. One [build
owner](../../tools/react_native/bleng_ios_consumer.py) then validates the installed formal Podspec,
compiles the four native adapter/owner sources in an isolated staged SwiftPM graph **without
Nitro/React**, and builds the existing formal CocoaPods App/native-test workspace. The isolated
target belongs to the formal host's `ios/PlatformContract`; its rendered project and build output
live only in `.work/`.

`formal-host-ios-build` accepts one positional Just argument: omitted or `false` verifies the local
Pods-preparation receipt; `true` explicitly passes Python `--refresh-pods`. Missing or stale
preparation fails closed without adoption or fallback. Explicit refresh invalidates before install
and seals only after successful installation. Source/native byte edits are accepted, while metadata,
codegen, schema, dependency-membership or environment drift requires refresh. The host Podfile
rejects a requested prebuilt React Native source fallback.

Missing package dependencies cannot be hidden by the host's explicit Pod dependencies. The build is
unsigned generic iOS Simulator `build-for-testing`: it neither executes native/UI tests nor
installs/launches an App, signs, controls Settings or claims RF/recovery evidence. This formal
source/local-package host is not a clean npm-tarball/artifact-only gate; the native staged-package
consumers retain their independent packaging/provenance responsibilities. The former standalone
Swift/Nitro compile consumer and its duplicate npm/Pod setup have been retired. See the [iOS Pods
preparation boundary](../../tools/react_native/README.md#formal-ios-pods-preparation-boundary) and
[current candidate report](../../docs/testing/reports/2026-11-11-ios-pod-preparation-boundary.md).

On Apple, `platforms/apple/Sources/BleHubApple` is the only backend source of truth. The
`.work/staged/apple-bleng-react-native/BleHubApple/Sources` tree is generated staging output copied
by the package recipe for CocoaPods and must not be edited directly. Android consumes the published
`blehub-android` backend artifact rather than copying backend sources.

Both platforms expose a lifecycle Nitro bridge with a typed initialization snapshot, backed by
the Rust `Supervisor` and the native `PlatformSession` adapter. The Android package is a single root library module so RN CLI
can autolink it directly; it does not require a nested `:library` project. Nitro's C++/JNI code is generated/minimal framework glue only;
Swift implements the iOS HybridObject and Kotlin implements the Android HybridObject. No hand-written
C++ BLeng or UniFFI business interface is maintained. `createBlengCentral()` exposes typed
initialization/close, the approved scan operation projection, the connect/disconnect operation
projection, the GATT discovery/read/write/RSSI projection, the notification subscription stream and
the adapter-state observation lane (`beginAdapterObservation()`). It does not expose raw UniFFI
handles, operation IDs, raw BLE commands/events, recovery context, generation IDs or policy plans.
Owner retirement and fresh-owner recovery remain internal Supervisor actions, not JS methods.
Notifications are not implemented by importing artifact
consumer code or adding `dev.bleng` dependencies to the legacy integration. Remaining RF/reload
migration must retain meaningful host/HIL evidence before the internal probe and legacy host can be
deleted.

## Apple engine lifetime

On iOS, `createBlengCentral()` materializes the private lifecycle TurboModule before
constructing the Nitro owner, then binds that actual NativeState to the module instance belonging
to this runtime. Missing module/binder, duplicate binding and cross-engine rebinding fail closed.
The production native constructor also rejects `initialize()` while unbound; the factory cannot be
bypassed by directly asking Nitro for the registered object name. Update the JS package and native
binary together; an older binary without this module cannot satisfy the new factory.

One module retains at most 64 bound native owners until invalidation, including explicitly closed
owners that could otherwise restart in the same live runtime. Capacity rejection does not claim
or retire the rejected object. RN's `RCTInvalidating` callback permanently seals each bound owner
and starts its existing native retirement barrier without calling into the old JS runtime.
Synchronous callback return is not async cleanup completion or resource-zero. Native consumers
may observe the retained result, including cleanup failure; no lifecycle controls, IDs or raw
handles are exported to JS by this mechanism.

An owning native host may attach the exact predecessor lifetime before binding any new owner.
The successor's `initialize()` waits for the actual old RN callback and successful native retirement
before constructing a backend, then revalidates its start ticket. Observation deadlines are
bounded to 1–30 seconds; expiry does not cancel old cleanup or manufacture callback evidence.
Invalidation cancels pending startup admission, and a failed intermediate engine cannot erase
an earlier transition failure. There is no process-global current-engine registry.

The formal native target exercises real Hermes/manager invalidation and same-process, same-host
`RCTHost` reload using a bundled entry that calls the actual package factory and `initialize()` over
scripted BLE backends. The formal consumer's AppDelegate and reload harness now share one
host-scoped `FormalReactNativeDelegate` that installs these predecessor relationships before RN
receives a replacement module. It must belong to one factory/host, not be reused across hosts.
Creation rejection returns a sealed, non-bindable module so RN can finish registration and invoke
real invalidation; it never silently falls back or reports rejection as successful startup.

Simulator tests also mount a small Fabric status view and independently inspect its native
UIKit subtree across reload. This is not the full workbench, a presented App window, physical
BLE, mobile/RF, Android engine binding or completed legacy migration. See
[runtime binding](../../docs/testing/reports/2026-11-10-apple-engine-runtime-binding.md),
[scoped RCTHost reload](../../docs/testing/reports/2026-11-11-apple-rcthost-reload.md), and
[shared App delegate/rendered-subtree evidence](../../docs/testing/reports/2026-11-11-apple-formal-delegate-rendered-reload.md).

## Android engine lifetime

Android now carries the same semantics, implemented where the platform actually provides a hook.
`BlengEngineLifetime` (pure Kotlin) seals admission at first invalidation, retires every bound
owner and aggregates their exact close receipts, links at most one successor, and gates that
successor on the predecessor's clean retirement with a bounded 1–30 s observation window. A
cancelled or never-dispatched predecessor gate cannot be reported as a clean retirement.

`BlengEngineLifetimeRegistry` resolves a lifetime only from the exact engine key; an unknown key
returns `null` instead of a default, so there is no process-global current-engine shortcut.
`HybridBlengCentral` binds to the lifetime of its own `ReactApplicationContext` at
construction and refuses `initialize()` while unbound.

`BleHubBlengEngineLifecycleModule` is the RN boundary: a spec-less TurboModule with no JavaScript
method, created eagerly per engine, whose `invalidate()` retires that engine. RN 0.87 bridgeless
`TurboModuleManager.invalidate()` is what delivers that callback. Engine retirement is permanent:
the bound owner refuses a later `initialize()` from a torn-down engine, while an explicit `close()` still
allows a JS-level restart.

Successor gating has a production caller: `BleHubBlengEngineHostWiring` is owned by the host's
`ReactPackage` instance, which lives as long as the host and therefore survives engine reloads.
`getModule(name, context)` runs once per new engine and installs the exact predecessor there, and a
refused link seals the successor instead of letting it start ungated. That package instance is the
Android analogue of Apple's host-scoped delegate; there is no process-global current engine.

Nitro's Android side has no Kotlin JSI binding hook, so `NitroModules.applicationContext` is the one
engine-context source and the owner performs an exact-context lookup that fails closed. One live
`ReactHost` per process is therefore required; a concurrent-engine setup is an unverified limitation,
not a supported mode. There is no test-only bypass in production code.

Verified: 45 JVM tests (including 21 new lifetime/registry/wiring tests), Android App plus
`androidTest` compilation, and a **real device reload gate** (`formal-host-android-engine-reload-test`)
that starts, reloads and destroys a real `ReactHost` in the formal host App's instrumentation
process. See
[parity report](../../docs/testing/reports/2026-11-11-android-engine-retirement-parity.md).

## Connection projection

One failure DTO serves every operation view. `BlengFailureInfo` (`domain`, `code`, `message`,
optional platform details) is produced by Rust in `crates/bleng-supervisor/src/ffi/failure.rs` and
projected mechanically by `BlengFailureMapping` on both platforms; scan and connection results never
classify one runtime error differently. `code` is stable, `message` is display-only.

Connecting starts from the capability an advertisement produced: `BlengPeripheral.beginConnect()`
returns a bounded submission, and only a successful `wait()` yields `BlengConnection`. Both
`BlengPeripheral` and `BlengConnection` are Rust-created objects that carry the exact owner context
that produced them; no `ConnectionId`, address, handle or owner identity crosses into JS, and a
capability from a retired owner fails closed instead of addressing a replacement engine.
`BlengConnection.beginDisconnect()` is the only teardown control, and its wait consumes one result.
Cancellation is an independent receipt and never rolls back native state; a failed disconnect leaves
native state unknown, so the UI keeps the capability instead of claiming a closed link.

Declared timeout values are validated by Rust before any submission allocates a command; a projected
`input/invalid_timeout` failure means no command was sent. Submission, waiting, deadlines and
cancellation stay inside BLeng — the Swift/Kotlin bridge only maps DTOs.

Verified: Rust `ffi_scan` integration tests cover success, single-consumer semantics and the
retired-owner refusal of both capabilities; 5 Jest Workbench tests cover the JS path; the formal
App and `androidTest` APKs compile. Not verified: a real-device connect/disconnect against a
peripheral (needs an nRF52840 lease), so this is not RF evidence.

## GATT discovery, characteristic I/O and RSSI

`BlengConnection.beginDiscoverGatt()` returns a bounded submission whose `wait()` produces a
`BlengGattSession`. That session, and everything below it, is a Rust-created capability tree:
`services()` → `BlengGattService` (`uuid()`, `kind()`, `characteristics()`) →
`BlengGattCharacteristic` (`uuid()`, `properties()`, `descriptors()`, `beginRead()`, `beginWrite()`).
No `ConnectionId`, `GattSessionId`, `GattCharacteristicId` or `GattDescriptorId` crosses into JS, so
there is no numeric address to guess at and no second way to reach an attribute.

Design decisions worth keeping:

- **Identity is the capability.** A session's identity is the exact owner + connection + GATT
  session Rust allocated. A later discovery yields a *new* session; capabilities from the previous
  topology are not re-pointed. Submission stays bounded and accepted, and Rust rejects the stale
topology at admission (`bleng.not_ready.gatt_not_ready`) with nothing dispatched — verified by a
  test that rediscovers and then addresses the retired topology.
- **Descriptors are data only.** Nothing in BleHub's contract reads or writes a descriptor, so the
  projection is `{ uuid }` and the descriptor handle is dropped rather than exported for symmetry.
- **Tri-state service kind.** `primary: Option<bool>` is projected as
  `'primary' | 'secondary' | 'unknown'` rather than a nullable boolean, because the rest of this
  surface uses `undefined` for absence and `null` at the Nitro boundary would be a second
  convention.
- **Absence is distinct from a value.** `BlengReadResult` carries the bytes, so a zero-length read
  is a success; `BlengRssiResult` carries the reading, so `0` and negative dBm are real values.
  Both are tested.
- **Writes are receipts.** A successful write wait means BLeng observed completion, not that the
  peer stored the value; the Workbench says exactly that. Write length validation stays in Rust
  (`MAX_WRITE_VALUE_BYTES`) instead of being mirrored in the bridge.
- **Bounded retention in the App.** The Workbench keeps at most 8 characteristic capabilities and
  reports how many it cannot address, because a capability keeps native state alive.

Verified: 5 Rust `ffi_scan` GATT tests (snapshot projection, single-consumer read/write, replaced
  topology, zero/negative RSSI, retired-supervisor detachment), 6 Jest Workbench GATT tests,
  `bleng-snapshot-check`, and both formal App/test graphs compiling.
Not verified: any of it on a real peripheral, or on a real iPhone. No characteristic has been read
from or written to an actual device, so this is not RF or "works with real hardware" evidence.

## Notification subscriptions

A subscription is a **stream**, so it is the one capability that is not a request/response receipt:

```text
BlengGattCharacteristic.beginSubscription(mode)
  → BlengSubscription.waitUntilEnabled(timeoutMs)  → { mode? }
  → BlengSubscription.nextEvent(timeoutMs)         → { value } | { ended } | failure
  → BlengSubscription.beginDisable() → operation.wait(timeoutMs)
```

Design decisions worth keeping:

- **The enable receipt carries the actual mode.** `EnableNotificationsOperation::wait` now returns
  `NotificationSubscription { subscription, actual_mode: Option<NotificationMode> }` instead of
  dropping the mode into the canonical event stream. A caller must not have to observe a second
  channel to learn what its own operation did, and correlating the mode from events would have made
  "the subscription is active but the mode is unknown" a real, silent state. `None` means the
  platform did not report a mode; it is projected as absent, never guessed from the request.
- **One owner, one reader, two lanes.** `SessionObservationProjection` fans the owner's existing
  canonical event stream out to the scan lane and the notification-subscription lane. No second
  reader or runtime exists, and `ObservationProjectionLease` closes both lanes atomically on
  retirement. The FFI ingest object is therefore named `SessionEventProjection`, not a scan-only
  name.
- **The lane exists before the enable is submitted.** `begin_enable_notifications` registers a
  bounded lane under the same lock that then performs the BLeng submission, and the lane binds on
  the `NotificationStateChanged` that carries *its own* enable command id. A value therefore cannot
  slip between the state change and a later registration, and a stale subscription from an earlier
  enable is never mistaken for this one.
- **Overflow fails the stream closed.** A slow consumer cannot silently lose a value: at 64 values
  or 16 KiB the unread backlog is discarded and the stream **ends with `reason: 'overflowed'`** —
  a terminal of this lane, not an operation failure, because the native subscription and every
  other operation keep running (the scan lane ends the same way). `nextEvent` is
  single-pending-pull, and its deadline is the caller's — a timeout is not a stopped subscription.
- **Terminals are explicit, and retained.** `disabled` (the subscription ended), `connectionClosed`
  (the link went away) and `ownerClosed` (this engine retired) are distinct, and retirement is never
  reported as a synthesized native disable. The end of a stream is a *retained fact*: reading
  `nextEvent` again returns the same terminal, because a consumer polling on its own schedule must
  still be able to learn why the stream ended. Exactly-once delivery belongs to the operation
  receipt (`wait`/`waitUntilEnabled`), which is single-consumer. Retirement is the only event that
  replaces a retained terminal. The scan lane obeys the same rule.
- **Disable is addressed through the capability.** The subscription identity never reaches the
  caller: `beginDisable()` uses the identity learned from this subscription's own enable, so a stale
  capability cannot disable someone else's subscription, and calling it before the subscription is
  observed fails with `notification.subscription_unknown` instead of guessing.

Verified: 6 Rust `ffi_scan` notification tests (ordered values including an empty value, actual-mode
receipt and absence, disable terminal, unknown-subscription refusal, overflow fail-closed,
retired-owner terminal, foreign-subscription filtering), 8 Jest Workbench tests, both formal native
graphs compiling, and the macOS artifact consumer re-running the typed-operation chain against the
regenerated package image. Not verified: a real notification from a real peripheral or iPhone, and
the gated RF notification scenarios. See
[notification subscription report](../../docs/testing/reports/2026-11-12-rn-notification-subscription.md).

## Internal scan event handoff

The existing native reader also accepts Rust's per-owner `SessionEventProjection`. It synchronously
forwards only scan start/advertisement/stop envelopes to an optional native `scanEventSink`, with
the complete source context fixed by Rust. It does not create a second event reader, buffer or
runtime. The existing raw diagnostic sink and Apple recovery inbox remain separate paths; they
are not added to the formal JS exports.

Rust revokes the endpoint before explicit retirement and on ownership drop. This prevents future
projection, not delivery of envelopes already returned; independently queued consumers must still
compare the full source context. It is not native stop/resource-zero evidence. Apple recovery
advertisement capture is now demand-scoped: subscribe before the recovery scan, select its exact
scan ID, and release on selection/failure/cancellation. Ordinary advertisements have no idle
recovery buffer. Active-capture overflow still fails the owner closed, and terminal/radio history
remains bounded separately; sustained scanning is not established by this change. See [scan projection responsibilities](../../docs/architecture/bleng-recovery.md#scan-event-projection固定来源而非第二个-reader).

The formal JS scan API uses this same reader/projection through Rust's bounded `ScanDelivery`;
it never exports the raw native envelopes or adds a second reader. The native diagnostic sink
remains separate from the product surface.

## Typed initialization result (unfrozen candidate)

```ts
import { createBlengCentral, requireOutcome } from '@blengine/react-native-bleng'

const central = createBlengCentral() // Does not initialize BLE.
const { snapshot } = requireOutcome(await central.initialize())
// snapshot.state, scanAuthorization, connectionAuthorization, name?
const closed = await central.close()
// closed.ownerClosed and closed.failure are independent facts.
```

`initialize(): Promise<BlengInitializeResult>` returns `{ snapshot }` or `{ failure }` — a native
refusal is data, exactly as `close()` reports a cleanup failure. `requireOutcome` is the throwing
view of that same union (built on the one `BlengOperationError`), not a second result type. The
`snapshot` projects the exact `ForeignOwnerStart.adapterSnapshot` returned by Rust initialization.
Swift/Kotlin only map the four adapter states, five authorization
states and optional name; they do not infer readiness or reconstruct identity from callbacks.
The scan and connection authorizations are independent. Missing names remain absent and empty
names remain empty. Initialization may succeed with `poweredOff`, `unavailable`, `unknown`,
`denied` or `notDetermined`; success means the session was initialized, not that BLE is usable.

Owner assembly is bounded by the library (currently 30 s). A bound hit is a `{ failure }` with
`code: 'bleng.timeout'`; native work may still be running, so `close()` is the only meaningful next
step. `close()` waits for native cleanup without an upper bound.

`close(): Promise<BlengCloseResult>` reports logical `ownerClosed` separately from an optional
identity-free `{ code, message }` failure projected by Rust. Nonterminal busy retains the owner;
terminal cleanup failure closes it without claiming resource-zero. Repeated close preserves the
first terminal result, including failure, until an explicit new start constructs a new owner.
Unexpected bridge exceptions remain Promise rejections; they do not prove owner closure. A native
start failure is not a rejection — it is the `{ failure }` arm of `initialize()`.

This is a **point-in-time initialization snapshot**. Later radio/permission changes are only
observable through `beginAdapterObservation()` (see [`API.md`](API.md)), never inferred from this DTO. No generation,
command identity or recovery signal is part of this DTO. This replaces the earlier private
`Promise<void>` return; it is not a compatibility shim or an ABI/public-contract freeze.

The formal consumer owns mapping contract tests and UI tests. `pixi run just react-native
formal-host-test` runs its Jest discovery; native mapping assertions require their separate
native runtime gates. Component tests mock this formal package boundary, never the legacy probe.
Factory tests exercise the actual package-source factory with isolated RN/Nitro dependencies.

## Typed scan projection (unfrozen candidate)

```ts
const submitted = central.beginScan({ serviceUuids: [], allowDuplicates: false })
if ('scan' in submitted) {
  const scan = submitted.scan
  const started = await scan.waitUntilStarted(5000)
  // Check started.failure; accepted submission alone is not scan startup.
  const observed = await scan.nextEvent(5000) // Explicit single pending pull, not a callback loop.
  const stopping = scan.beginStop()
  if ('operation' in stopping) {
    const stopped = await stopping.operation.wait(5000)
    // Check stopped.failure; timeout never implies native stop or automatic retry.
  }
}
```

Start/stop result waits are single-consumer; cancellation remains independent through
`cancelStart()` / `stop.cancel()`. Only a pre-consumption input failure permits another wait of
that original result. A consumed timeout/failure is not a promise of a later successful re-wait.
Advertisements carry an opaque owner-bound peripheral, optional native facts and owned
`ArrayBuffer` bytes; no raw identities enter JS. Rust owns 64-advertisement/256-KiB bounded
observation delivery, exact context binding, timeout/error projection and retirement. Native
`dispose()` ends observation only, not BLE scanning. Overflow fails explicitly, without implicit
stop, retry or owner close. See the [scan contract](../../docs/research/react-native/react-native-bleng-scan-contract.md)
and [candidate evidence](../../docs/testing/reports/2026-11-04-formal-rn-scan.md).

## Awaiting one operation

`begin*` stays the submission model. `pendingOperation()` is a derived convenience layer over it for
callers that want `await` without losing the receipt or the cancel control:

```ts
import { pendingOperation, BlengOperationError } from '@blengine/react-native-bleng'

const pending = pendingOperation(characteristic.beginRead(), 5_000) // refuses a refused submission here
try {
  const { value } = await pending.result // the success arm only; failures are thrown
  // ...
} catch (error) {
  if (error instanceof BlengOperationError && error.code === 'bleng.cancelled') {
    // `code` is the only field safe for control flow; `message` is display only.
  }
}
// Cancellation is always explicit: dropping `result` never cancels.
const receipt = await pending.cancel()
```

Three rules make this derived rather than a second operation model: it settles only through the
operation's own bounded `wait`, cancellation only ever happens through an explicit `cancel()` whose
receipt is returned unchanged, and native failures are never re-worded. `beginScan()` and
`beginSubscription()` keep their lane-named controls, so they use the explicit form
`pendingOperation({ operation: { wait: (ms: number) => scan.waitUntilStarted(ms), cancel: () => scan.cancelStart() } }, 5_000)`
rather than a uniform rename that would hide which lane a caller is on. See
[`pending-operation-layer.md`](../../docs/research/react-native/pending-operation-layer.md).

The module deliberately excludes `blehub-uniffi` from the `blehub-android` dependency because the
published BLeng AAR already contains the combined `blehub + bleng + bleng-supervisor` UniFFI
Kotlin/native image.
This preserves the one-image package invariant.
