# React Native BLeng integration boundary

This directory is the production Apple + Android BLeng package line. It is intentionally separate from:

- `integrations/react-native-internal`, which is a transitional private low-level probe package;
- `tests/consumers/react-native-internal`, which is the transitional legacy RN host;
- `tests/consumers/android/bleng`, which is an artifact-only consumer and test oracle.

The formal RN line has its own [clean consumer host](../../tests/consumers/react-native-bleng/README.md)
and does not depend on the internal probe package. Formal native bridge contract tests live in
that consumer's Swift/Kotlin test targets, not in this library or the legacy probe host; Appium
shares the product UI scenario across platforms. Internal code is not moved wholesale into `tests/consumers`: only test-owned native
seams and legacy evidence move there, after which the internal package and host are deleted.

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

It stages the combined Apple package, checks/generates the locked Nitro glue, and prepares the
formal host's Node dependencies. One [build owner](../../tools/react_native/bleng_ios_consumer.py)
then validates the installed formal Podspec, compiles the four native adapter/owner sources in
an isolated staged SwiftPM graph **without Nitro/React**, and builds the existing formal
CocoaPods App/native-test workspace. The isolated target belongs to the formal host's
`ios/PlatformContract`; its rendered project and build output live only in `.work/`.

Missing package dependencies cannot be hidden by the host's explicit Pod dependencies. The
build is unsigned generic iOS Simulator `build-for-testing`: it neither executes native/UI tests
nor installs/launches an App, signs, controls Settings or claims RF/recovery evidence. This formal
source/local-package host is not a clean npm-tarball/artifact-only gate; the native staged-package
consumers retain their independent packaging/provenance responsibilities. The former standalone
Swift/Nitro compile consumer and its duplicate npm/Pod setup have been retired.

On Apple, `platforms/apple/Sources/BleHubApple` is the only backend source of truth. The
`.work/staged/apple-bleng-react-native/BleHubApple/Sources` tree is generated staging output copied
by the package recipe for CocoaPods and must not be edited directly. Android consumes the published
`blehub-android` backend artifact rather than copying backend sources.

Both platforms expose a lifecycle Nitro bridge with a typed initialization snapshot, backed by
the Rust `Supervisor` and the native `PlatformSession` adapter. The Android package is a single root library module so RN CLI
can autolink it directly; it does not require a nested `:library` project. Nitro's C++/JNI code is generated/minimal framework glue only;
Swift implements the iOS HybridObject and Kotlin implements the Android HybridObject. No hand-written
C++ BLeng or UniFFI business interface is maintained. `createBlengSessionOwner()` exposes typed
initialization/close and the approved scan operation projection. It does not expose raw UniFFI
handles, operation IDs, raw BLE commands/events, recovery context, generation IDs or policy plans.
Owner retirement and fresh-owner recovery remain internal Supervisor actions, not JS methods.
Connect/GATT/I/O/notification projections are not implemented by importing artifact consumer code
or adding `dev.bleng` dependencies to the legacy integration. Remaining RF/reload migration must
retain meaningful host/HIL evidence before the internal probe and legacy host can be deleted.

## Internal scan event handoff

The existing native reader also accepts Rust's per-owner `ScanEventProjection`. It synchronously
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
import { createBlengSessionOwner } from '@blehub/react-native-bleng'
import type { BlengAdapterSnapshot } from '@blehub/react-native-bleng'

const owner = createBlengSessionOwner() // Does not initialize BLE.
const snapshot: BlengAdapterSnapshot = await owner.start()
// snapshot.state, scanAuthorization, connectionAuthorization, name?
const closed = await owner.close()
// closed.ownerClosed and closed.failure are independent facts.
```

`start(): Promise<BlengAdapterSnapshot>` projects the exact `ForeignOwnerStart.adapterSnapshot`
returned by Rust initialization. Swift/Kotlin only map the four adapter states, five authorization
states and optional name; they do not infer readiness or reconstruct identity from callbacks.
The scan and connection authorizations are independent. Missing names remain absent and empty
names remain empty. Initialization may succeed with `poweredOff`, `unavailable`, `unknown`,
`denied` or `notDetermined`; success means the session was initialized, not that BLE is usable.

`close(): Promise<BlengCloseResult>` reports logical `ownerClosed` separately from an optional
identity-free `{ code, message }` failure projected by Rust. Nonterminal busy retains the owner;
terminal cleanup failure closes it without claiming resource-zero. Repeated close preserves the
first terminal result, including failure, until an explicit new start constructs a new owner.
Unexpected bridge exceptions remain Promise rejections; they do not prove owner closure.

This is a **point-in-time initialization snapshot**, not a live adapter-state subscription. The App
labels it accordingly; later radio/permission changes cannot be inferred from it. No generation,
command identity or recovery signal is part of this DTO. This replaces the earlier private
`Promise<void>` return; it is not a compatibility shim or an ABI/public-contract freeze.

The formal consumer owns mapping contract tests and UI tests. `pixi run just react-native
formal-host-test` runs its Jest discovery; native mapping assertions require their separate
native runtime gates. JS tests mock only this formal package boundary, never the legacy probe.

## Typed scan projection (unfrozen candidate)

```ts
const submitted = owner.beginScan({ serviceUuids: [], allowDuplicates: false })
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

The module deliberately excludes `blehub-uniffi` from the `blehub-android` dependency because the
published BLeng AAR already contains the combined `blehub + bleng + bleng-supervisor` UniFFI
Kotlin/native image.
This preserves the one-image package invariant.
