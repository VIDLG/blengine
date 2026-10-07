# BLeng foreign API candidate review

## Status

This is a **candidate design review**, not a public ABI approval and not a
production package change. It follows the isolated UniFFI 0.32 mechanism probe
in [`spikes/archive/bleng-uniffi-async/`](../../spikes/archive/bleng-uniffi-async/README.md).
The candidate native image, when implemented, must contain exactly two
production components:

```text
blehub
bleng
```

The toy async component and `foreign-probe` feature remain excluded.

## Ownership boundary

The detailed waiter lifecycle and completion model is documented in
[`bleng-waiter-design-2026-10-06.md`](bleng-waiter-design-2026-10-06.md). It
covers `WaiterRegistry`, `Pending`, `WaiterTicket`, dispatch/domain/terminal
event ordering, timeout and late-event semantics, and the single-consumer event
stream boundary.

`blehub` remains the only canonical owner of the low-level contract:

- `BleHub`;
- command, event, error, ID, scan, connection and GATT DTOs;
- foreign `BleBackend` and `BleEventSink` traits;
- `BackendEventIngressHandle` and `EventIngressStatus`.

`bleng` owns only the async orchestration boundary:

- the caller-facing async owner object;
- owned Tokio/Kameo runtime and actor lifecycle;
- waiter, timeout, cancellation and late-event policy;
- backend assembly admission and cleanup coordination;
- a reviewed BLeng-specific lifecycle/error facade.

Tokio, Kameo, `RuntimeOwner`, actor references, Rust futures and native platform
handles must not appear in generated Swift/Kotlin declarations.

### Async operation ownership

The current per-instance private-runtime model and caller-runtime coexistence
rules are recorded in the [runtime integration section](bleng-cleanup-design-2026-10-07.md#private-runtime-and-caller-runtime)
and its [Chinese version](bleng-cleanup-design-2026-10-07.zh-CN.md). They explain
why facade operations use an explicit owner handle rather than the caller's
executor, and distinguish async handoff from nested blocking `block_on`.

The public UniFFI `Bleng` object and the private runtime owner are intentionally
separate ownership layers:

```text
foreign Bleng handle
        │
        ▼
Arc<Bleng>
        │
        ▼
Arc<RuntimeOwner>
        │
        ├── Tokio runtime
        ├── BlengActor
        ├── RuntimeLifecycle
        │     ├── phase gate + notification
        │     └── HubCleanup → cleanup coordinator
        └── event FIFO
```

Each facade method clones `Arc<RuntimeOwner>` before spawning its `'static`
operation future. The spawned future therefore retains the runtime owner while
it waits for actor state and native callback evidence. Releasing the foreign
Swift/Kotlin/RN object must not destroy the runtime, actor, waiter registry, or
cleanup coordinator underneath an operation that is still running.

`AbortOnDrop` binds the foreign waiter lifetime to the spawned facade task:

```text
foreign waiter released
    → AbortOnDrop aborts the facade waiter task
    → its RuntimeOwner Arc clone is eventually released
    → normal RuntimeOwner/Hub cleanup remains responsible for native state
```

This cancellation only retires the BLeng waiter task. It does not promise that
an already-dispatched native BLE command is cancelled, and it does not interrupt
a foreign synchronous attach or a started `spawn_blocking` operation. Native
late callbacks must still be handled by the BLeng actor, correlation and cleanup
rules. The `Arc` is therefore required for Rust async lifetime and shared
ownership; it is not what makes the UniFFI object opaque, and it does not expose
Tokio or Kameo to foreign callers.

The BLeng component may receive `blehub::BleBackend` and return or accept
canonical `blehub` DTOs. It must not define copies of `CommandId`, `BleEvent`,
`BackendCapabilities`, `BleBackend`, or GATT types. The isolated probe already
proved this cross-component direction on macOS Swift and Kotlin/JVM, but not
production packaging.

## Candidate object and lifecycle

The proposed object name is `Bleng` in the `bleng` component namespace.

```text
Bleng.new() -> BlengCreateError
Bleng.assembleBackend(BleBackend) -> async BlengAssemblyError
Bleng.initialize(timeoutMs) -> async BlengOperationError<AdapterSnapshot>
Bleng.nextEvent() -> async BlengOperationError<Optional<BleEvent>>
Bleng.shutdown() -> async BlengShutdownError
```

`shutdown()` is intentional. UniFFI Kotlin objects already expose synchronous
`close()`/`destroy()` for releasing the generated object handle. The isolated
probe demonstrated that exporting an async `close()` creates a Kotlin binding
conflict. `shutdown()` means logical BLeng shutdown and does not replace the
foreign wrapper's handle-release operation.

The object has these rules:

1. It owns its runtime; callers do not provide Tokio or an executor.
2. It accepts at most one backend assembly. Duplicate and post-shutdown
   assembly are rejected without leaking or transferring a raw assembly ticket.
3. `shutdown()` is idempotent and all callers observe the same cleanup result.
4. Shutdown rejects new operations, terminates pending waiters, stops the actor,
   waits for admitted synchronous attach/rollback, and waits for exactly-once
   backend disposal.
5. Dropping the generated Swift/Kotlin wrapper is not a substitute for
   `shutdown()`; ARC/GC release is a separate handle lifecycle.
6. A caller task/coroutine cancellation only settles the foreign waiter unless
   the generated binding and the BLeng operation explicitly provide stronger
   semantics. It must not be described as cancellation of a native BLE command.

`nextEvent()` is a single logical reader. Concurrent readers are serialized by
BLeng and compete for one bounded FIFO; each event is consumed once and is not
broadcast to every reader. This is independent of the one-active-connection
rule for a Peripheral: one Hub may still manage multiple Peripherals, while
one application event consumer remains responsible for dispatching the shared
stream. Bounded ingress and fail-closed overflow behavior remain mandatory.

## Candidate operation surface

Once the current crate-private operation slices are complete, the candidate
facade should expose the existing contract without inventing a second protocol:

| Async method | Result | Notes |
|---|---|---|
| `initialize(timeoutMs)` | `AdapterSnapshot` | Requires matching snapshot and terminal event. |
| `startScan(options, timeoutMs)` | `ScanSessionId` | One active scan; IDs remain caller-visible contract IDs. |
| `stopScan(scanId, timeoutMs)` | `Unit` | Native scan stop completion, not merely dispatch acceptance. |
| `connect(peripheralId, timeoutMs)` | `ConnectionId` | One lifecycle per peripheral and strict generation correlation. |
| `disconnect(connectionId, timeoutMs)` | `Unit` | Logical lifecycle completion; no stronger physical disconnect claim. |
| `discoverGatt(connectionId, timeoutMs)` | `GattSessionId` | Aggregate all-or-error database publication. |
| `readCharacteristic(connectionId, sessionId, characteristicId, timeoutMs)` | `Bytes` | Exact connection/session/characteristic routing. |
| `writeCharacteristic(...)` | `Unit` | Without-response remains local/native submission semantics. |
| `setNotifications(...)` | `Unit` | Notification/Indication mode policy stays in the canonical contract. |
| `readRssi(connectionId, timeoutMs)` | `Int16` | Capability and terminal error mapping stays explicit. |
| `nextEvent()` | `Optional<BleEvent>` | One serialized logical reader; close returns end-of-stream. |
| `shutdown()` | `Unit` | Shared idempotent cleanup result. |

The exact generated names, argument wrappers, and error variants are still
candidate decisions. Methods should not be added to the production component
until their Rust fake-backend semantics and cross-language generated surface
are covered.

## Candidate errors

Do not reuse the probe's coarse `ForeignProbeError`. The production candidate
needs a stable, small error taxonomy that separates local orchestration from
BLE-domain errors already carried by canonical events:

- `Closed`: the BLeng owner has reached terminal shutdown;
- `NotReady`: initialization or required lifecycle state has not been reached;
- `InvalidArgument`: local validation failed before dispatch;
- `Busy`: an operation conflicts with an accepted operation on the same scope;
- `Timeout`: the caller-owned deadline expired;
- `TransportFault`: bounded ingress, actor or owner transport failed closed;
- `AssemblyFailed`: backend admission/contract setup failed;
- `CleanupFailed`: shutdown/rollback completed with a cleanup error;
- `OperationFailed`: the canonical terminal/domain event reported failure.

This list is not yet an ABI commitment. In particular:

- it must not hide the canonical `BleHubError`/`CommandDispatchError` meaning;
- it must not turn caller cancellation into a synthetic native cancellation;
- it must not promise that a blocked foreign synchronous callback can be
  forcibly interrupted;
- it must preserve the `BleHub::new` failed-construction rollback result: when
  backend `dispose()` fails, the returned `BleHubError::BackendCleanup` includes
  the primary assembly and cleanup diagnostics.

Timeout representation remains an integer millisecond boundary for
Swift/Kotlin portability. The reviewed candidate accepts `1..=86_400_000` ms
(one millisecond through 24 hours); `0` and values above 24 hours are rejected
before backend dispatch. There is no infinite-timeout sentinel.

### Structured-error candidate implementation (still not a public ABI)

The candidate [`BlengError` conversion](../../crates/bleng/src/candidate_api.rs)
now uses exhaustive structured conversions. It no longer turns
[`WaiterError`](../../crates/bleng/src/waiter.rs) into debug text, assembly into
`format!("{other:?}")`, or cleanup into `Display` text. The foreign caller can
now distinguish local admission/lifecycle failures from canonical dispatch or
BLE terminal errors. The iOS strict-Indication and recovery probes now inspect
error variants and canonical categories rather than parsing strings.

The implementation keeps existing canonical ownership and uses exhaustive
conversions, without changing the default production package:

| Source | Proposed candidate representation |
|---|---|
| Canonical terminal/domain failure | `Operation { error: BleEventError }` |
| Canonical synchronous dispatch rejection | `DispatchRejected { error: CommandDispatchError }` |
| Backend assembly/rollback failure | `Assembly { error: BleHubError }` |
| Backend shutdown cleanup failure | `Cleanup { error: BleHubError }` |
| Local lifecycle/deadline | Distinct `Closed`, `Cancelled`, `Disconnected`, `Timeout`, `TransportFault`, `WorkerStopped` |
| Missing facade state | `NotReady { reason }`: owner, scan, connection, GATT or subscription |
| Conflicting admission | `Busy { reason }`: scan, peripheral, GATT, operation or backend slot |
| Capacity/ID exhaustion | `ResourceExhausted { resource }`: pending waiters or identifiers |
| Internal ownership/ID invariant | `ContractFailure { reason }`: duplicate command ID or duplicate owned Hub |
| Existing timeout validation | Retain `InvalidTimeout`; accept `1..=86_400_000` ms and reject `0`/larger values |

`BleEventError`, `CommandDispatchError` and `BleHubError` stay defined in
[`blehub`](../../crates/blehub/src/contract/errors.rs); BLeng must not copy their
categories or lose their platform diagnostics. Assembly rollback reported as
`BleHubError::BackendCleanup` remains an **assembly** error, not a shutdown
error. Text may remain useful for display but must not select control flow.
Local timeout/cancellation and canonical BLE timeout/cancellation retain their
different origins; neither asserts native operation cancellation.

Two implementation limits must not be hidden by the proposed type names:
[`AttachError`](../../crates/bleng/src/cleanup.rs) currently merges an assembling
and an already-assembled backend slot, and admission receives only an open/closed
predicate. It cannot reconstruct a distinct transport-fault cause from
`AttachError::Closed`. More precise assembly lifecycle reporting requires a
source-side change and its own race tests, not just a conversion rename.

The user approved this candidate direction: (1) preserve the three canonical
payload types in the foreign error; (2) adopt scoped not-ready/busy and separate
exhaustion/contract reasons; (3) keep async domain failure as a thrown
`Operation` error while still preserving the canonical event stream; (4) use a
single logical event reader; and (5) bound timeout values to
`1..=86_400_000` ms. The implementation remains candidate-only and does not
switch default packaging. Candidate bindings/snapshots and static foreign
consumers have been regenerated and compiled. Existing RF receipts from the
structured-error candidate remain valid only for the reviewed API shape after
the required affected RF rerun; this rename/timeout change still requires that
RF revalidation before graduation.

## Current implementation evidence

A feature-gated `candidate-api` facade now exists in `crates/bleng` as
`Bleng`/`BlengError`. Its internal runtime owner remains crate-private as
`BlengOwner`; only the reviewed `Bleng` object crosses UniFFI. The facade is
deliberately not enabled by default. The sole `crates/blehub-uniffi` bridge has
an opt-in `candidate-bleng` feature that re-exports this facade without
changing the default one-component package.

The opt-in bridge was built as one macOS native image and generated both
components from that image:

- Swift `blehub.swift` + `bleng.swift`, headers and modulemaps;
- Kotlin `dev.blehub.ffi` + `uniffi.bleng` bindings with the tracked
  `uniffi.toml` configuration;
- Swift generated sources compiled as a module;
- Kotlin generated sources compiled with the pinned JVM Kotlin and JNA 5.18.1;
- the image contained `blehub` and `bleng` UniFFI symbols and no toy/probe
  symbols.

The initial generation was binding evidence; the opt-in Apple XCFramework and
macOS artifact runtime/unsigned iOS app links are recorded below. The candidate
is not the default production package, Android AAR, iOS device runtime,
Android ART, reload or RF evidence. The facade exposes assembly,
initialization, scan/stop, connect/disconnect, GATT discovery, characteristic
read/write, notification enable/disable, RSSI, one logical event reader and
shutdown. Only assembly, initialization, scan and shutdown have the macOS
foreign runtime coverage below; other operations still need foreign runtime
and lifecycle tests before API freeze.

The opt-in snapshot gate now uses `pixi run just binding
candidate-snapshot-check`. It builds the two-component image in an isolated
`.work/cargo-target/bleng-candidate-snapshot` target and regenerates bindings
under `.work/bleng-candidate-snapshot-generated`, leaving the default
`blehub` snapshot/build paths unchanged. Three separate, **unfrozen** candidate
snapshots under `api-snapshots/bleng-candidate-{rust,kotlin,swift}.txt` pin
Rust async signatures, Swift async protocol/error variants, and Kotlin suspend
interface/error variants. The gate requires exactly the `blehub` and `bleng`
generated files, rejects duplicated canonical DTOs and checks that Kotlin
imports the canonical `BleBackend`, `BleEvent` and `AdapterSnapshot` from
`dev.blehub.ffi`. Its fail-closed file/type checks have a small hermetic unit
test. The existing default `pixi run just binding snapshot-check` still passes
against its original three `blehub` snapshots. No snapshot is a public ABI
approval or an Apple/Android package consumer pass.

## Opt-in Apple candidate artifact gate

The isolated `pixi run just ffi apple-bleng-candidate-package` recipe now builds
all five Rust Apple targets and stages a three-slice XCFramework/SPM package at
`.work/staged/apple-bleng-candidate`, separate from the default
`.work/staged/apple-production`. Each slice uses one native archive, both
`blehubFFI.h`/`blengFFI.h` headers and a combined module map. The packager
checks the exact generated binding file set, both component symbol families,
forbidden probe/test symbols, the slice architectures and staged headers. The
macOS artifact-only Swift consumer at
[`tests/consumers/apple/bleng-candidate-macos/`](../../tests/consumers/apple/bleng-candidate-macos/Package.swift)
imports only the staged package; its async fake-backend flow passed backend
assembly, initialization, scan, duplicate shutdown, exactly-once dispose,
closed late ingress, typed post-shutdown rejection, and shutdown during a
pending foreign initialization (the pending waiter returns `Closed`). Run it via
`pixi run just apple consumer-bleng-candidate-macos`. The packaging guard has
hermetic tests in [`tools/apple/test_package_uniffi.py`](../../tools/apple/test_package_uniffi.py).

Apple `nm` can return nonzero for Rust 1.95 LLVM bitcode members while still
listing the archive's exported Mach-O symbols. The gate only tolerates the
observed `Unknown attribute kind (105)` and `no symbols` diagnostics, rejects
all other diagnostics, and still requires the exact `blehub` + `bleng`
component families. Successful artifact linking is checked independently by
the Swift consumer. This is **macOS fake-backend foreign runtime evidence**,
not production CoreBluetooth, iOS device, Android ART, RF, ABI freeze or a
switch of the default package.

The separate [`tests/consumers/apple/bleng-candidate-ios/`](../../tests/consumers/apple/bleng-candidate-ios/project.yml)
XcodeGen app depends only on the staged candidate Swift package. The explicit
`pixi run just apple consumer-bleng-candidate-ios-unsigned` recipe passed
unsigned generic iOS **device and Simulator** builds. This unsigned gate only
compiles/links the async `BlengApi` + foreign fake-backend flow; the separate
signed device runtime result is recorded below.
The linked-image gate checks that exactly one app Mach-O contains exactly the
`blehub` and `bleng` UniFFI symbol families and no test/probe family. In Xcode
Debug the symbols live in the app's `.debug.dylib`, not its small launcher
executable; the gate inspects both and other bundled dylibs. Fail-closed cases
are covered in
[`tools/apple/test_consumer_bleng_candidate_ios.py`](../../tools/apple/test_consumer_bleng_candidate_ios.py).
Unsigned linking is not signed installation, iOS foreign runtime, RF, or a
CoreBluetooth backend pass. The existing single-component iOS consumer remains
untouched.

The explicit `pixi run just apple consumer-bleng-candidate-ios-device` gate
checks the connected physical device and visible
signing identity before building, closes build stdin (no password argument,
local-config secret or terminal prompt), checks the signed provisioned app's
single-image symbol set, then installs and launches it with a unique run ID.
It accepts only a matching result file, stops on a matching failure and bounds
result observation to 90 seconds. The app shows running/passed/failed status
rather than a blank screen; this is a fake backend and never starts BLE RF.
On 2026-09-30 `devicectl` initially listed the paired iPhone 14 Plus as
`available` with a `disconnected` wired tunnel; preflight correctly rejected
before signing. A bounded `devicectl device info details` check then established
the tunnel. The maintained device recipe now attempts this **once**, only when
there is exactly one paired, Developer-Mode-enabled, wired physical iPhone (or
one explicitly selected by `BLEHUB_APPLE_DEVICE`), then rechecks the original
strict connected-device/signing preflight.

The rerun passed on physical iPhone 14 Plus
`00008110-000C61160C39401E`: signed/provisioned app build, linked-app exact
two-component/single-image symbols, install, launch with run ID
`7dde0558-1224-42bd-ac4c-1dfa3edeb130`, and a matching on-device `passed`
result. The exercised iOS fake backend covered `BlengApi` assembly, initialize,
scan, duplicate shutdown, once-only dispose, late ingress rejection, and typed
post-shutdown `Closed`. A subsequent same-candidate device run additionally
passed shutdown **during a pending foreign initialization**, with the pending
Swift async waiter returning `Closed`, once-only dispose, and closed late
ingress. It signed, installed, launched and read back run ID
`70570775-2e3d-4406-bad9-181581f8da08` from the physical iPhone. Neither
run exercises native CoreBluetooth, real RF, the other BLeng methods, Android
ART, or RN. No password was read, passed to a command, or stored in local
configuration.

### 2026-09-30 foreign lifecycle follow-up

The two artifact consumers now also exercise a successful initialize with a
`PoweredOff`/`Denied` snapshot, two started Swift `nextEvent()` tasks settling to
end-of-stream on shutdown, and a pending initialize whose Swift task is cancelled
before explicit shutdown. The latter must return `Closed`, dispose exactly once
and reject retained late ingress. It tests **explicit shutdown**, not automatic
Swift cancellation of the UniFFI future or native BLE operation.

The backend-entry/reader-start handshakes use bounded semaphore waits on a
Dispatch worker, resumed through a Swift continuation; they do not block Swift's
cooperative executor or use sleeps as synchronization. Reader-start signals occur
before the foreign calls and do not prove that both Rust futures were already
blocked simultaneously. The existing Rust runtime tests cover the serialized
logical reader; this foreign check verifies that both started tasks settle during
shutdown. The basic initialization/scan flow now explicitly shuts down on failure
rather than relying on ARC/Drop cleanup.

Validation of this extension:

- `pixi run just apple consumer-bleng-candidate-macos`: runtime passed, including
  the extended fake-backend checks and three-slice package checks.
- `pixi run just apple consumer-bleng-candidate-ios-unsigned`: device and Simulator
  compile/link and final-app exact two-component/single-image checks passed.
- `pixi run just apple consumer-bleng-candidate-ios-device`: signed/provisioned
  build, single-image and installation passed, but launch run
  `cabfe2e6-ec92-4cdc-b792-bc5bb7c23d5a` was rejected by `SBMainWorkspace` as
  `Locked`. The extension has **not** passed iOS device runtime; earlier passing
  run IDs above retain their original coverage. No password was supplied and no
  Peripheral/RF gate ran.
- `pixi run rtk cargo test -p bleng --features candidate-api`: 133 passed.
- A default concurrent `blehub` run returned 31 passed / 1 failed in the existing
  assembly lock-release probe. Investigation reproduced interference between
  parallel tests sharing the global registry: `try_lock()` observes other test
  threads, not only the sink's own thread. The isolated probe passed 20/20 and
  the full serial suite passed 10/10 (32 tests each). Production assembly and
  the test assertions were not changed; the parallel-test instability remains
  a separate follow-up, not a production failure proven by this run.

### Candidate-only iOS production-backend initialization

An independent [iOS HIL app](../../tests/hil/apple/bleng-candidate-ios/project.yml)
compiles the existing `platforms/apple/Sources/BleHubApple` Swift sources into
its app target, following the maintained Apple HIL source topology. It has
**one** package dependency, the staged `apple-bleng-candidate` image: it does
not import `platforms/apple/Package.swift`, which points at the default
`apple-production` image, and does not replace the fake-backend artifact-only
consumer above. `pixi run just apple bleng-candidate-ios-apple-init-build`
passed unsigned generic iOS device/Simulator builds and checked that the
linked app contains exactly one image with the `blehub` + `bleng` UniFFI
component symbols. Its signed device counterpart constructs the production
`AppleBleBackend`, assembles it into `BlengApi`, awaits `initialize()` and
explicitly calls `shutdown()`; it does **not** start scanning or connect to a
Peripheral.

The first signed iPhone 14 Plus run signed, installed and launched but returned
`Timeout` for run ID `1526022e-1821-41fb-a0f9-f5996aaf53ff` at the 30-second
initialization deadline. After the user approved the iOS Bluetooth prompt,
the unchanged recipe reran and passed with run ID
`9fc83b4f-c30b-407b-a935-0d07a76df444`, reporting adapter `poweredOn`,
scan authorization `authorized`, and connection authorization `authorized`
from the device's result file. The sequence is consistent with authorization
blocking the first callback, but does not isolate TCC as the sole cause of the
first timeout. It establishes **iOS BLeng candidate + production CoreBluetooth
initialization** on this device/candidate, not scan, GATT, RF, RN, Android or
public ABI approval.

### Candidate-only iOS CoreBluetooth scan RF

The isolated [iOS HIL app](../../tests/hil/apple/bleng-candidate-ios/project.yml)
now has an explicit `--rf-scan-service-uuid` mode, separate from its unchanged
initialization-only path. The host loads the maintained nRF profile, starts the
provider-owned BTP fixture, builds/signs and checks the app's single linked
`blehub` + `bleng` image, installs and launches it on the iPhone, and requires a
unique run-ID result with the expected service UUID. The app calls real
`BlengApi` initialization and profile-filtered `startScan()`, consumes a
matching advertisement through `nextEvent()`, calls `stopScan()`, verifies a
requested stop event and observes one second without another advertisement for
the stopped session, then shuts down. The host always stops the managed fixture
on exit. The gate is `pixi run just apple bleng-candidate-ios-rf-scan`.

On 2026-09-30 the first on-device run `9e86c6eb-4f61-473f-a1f2-cff7e25b3804`
reported `scan=passed` but the host rejected its valid Apple `peripheral_id=0`.
The host validator was corrected and regression-tested without changing the
Apple backend or scan scenario. The complete rerun passed on physical iPhone 14
Plus `00008110-000C61160C39401E` with standalone nRF52840 DK and profile
`blehub-nrf52840-indication-v2`: run ID
`cdc26b89-3903-498f-ba38-bfebe7b84759`, service UUID
`fd000000-0000-4000-8000-000000000000`, scan session ID `3`, peripheral ID
`0`, adapter `poweredOn` and scan authorization `authorized`. The provider was
confirmed stopped after the run. This establishes **BLeng candidate + production
iOS CoreBluetooth single-device scan/start/stop RF** for this candidate; it does
not establish connect, GATT, I/O, notifications, RSSI, passive recovery, two-DK
concurrency, Android, RN or public ABI approval. The one-second post-stop
observation checks the app event stream, not absence of all radio packets.

### Candidate-only iOS CoreBluetooth connect/disconnect RF

The explicit `pixi run just apple bleng-candidate-ios-rf-connection` gate extends
the same profile-filtered iPhone + standalone DK scan. Within the *same*
`BlengApi`/`AppleBleBackend` owner, it keeps the discovered backend-private
`PeripheralId`, stops scanning, calls `connect()` and requires the ordered
`Connecting` then `Connected` events for the returned connection ID under the
same connect command ID. It then calls `disconnect()`, requires a correlated
`Disconnected` event with a **different, non-nil** disconnect command ID,
and shuts down. The BLeng calls themselves also wait for their domain and
terminal completion. The host checks the exact current run ID, profile UUID,
scan evidence and the connection lifecycle result; the fixture is stopped on
exit.

On 2026-09-30 the candidate signed/installed/ran on physical iPhone 14 Plus
`00008110-000C61160C39401E` against the standalone nRF52840 DK profile
`blehub-nrf52840-indication-v2`. The single-image/two-component app check
passed; device run ID `0f1fe63f-2444-4a80-8e7d-2cea287ef03e` returned
scan session `3`, peripheral ID `0`, connection ID `6`, and
`connecting,connected,disconnected`. The fixture was confirmed stopped. This is
**single-device iOS BLeng candidate + production CoreBluetooth scan/connect/
logical-disconnect RF**, not a claim about when the physical radio link drops,
GATT, I/O, subscriptions, RSSI, fault recovery, two-DK concurrency, Android,
RN, or a frozen/public ABI.

### Candidate-only iOS GATT, characteristic I/O and RSSI RF

The same isolated iPhone 14 Plus + standalone nRF52840 DK app extended the
production `AppleBleBackend` path without changing the default package. The
signed single-image/two-component gate and run-ID-correlated device receipts
passed for:

- `pixi run just apple bleng-candidate-ios-rf-gatt`, run
  `69f3aef6-865d-46fb-ba20-4d9397a295d8`: GATT session `8`, one primary
  fixture service, FFF1–FFF5 with exact properties, two CCCDs and unique opaque
  service/characteristic/descriptor instance IDs.
- `pixi run just apple bleng-candidate-ios-rf-io`, run
  `bd0dc5ba-4da6-47de-8afd-f299da364767`: initial read `00`,
  response-write `10` with exact readback `10`, no-response write `ab` with
  separate delayed readback `ab`. The no-response terminal means **local
  CoreBluetooth submission**, not remote confirmation.
- `pixi run just apple bleng-candidate-ios-rf-rssi`, run
  `69544c81-9bf9-48be-ad9e-73a16b9884d3`: connected RSSI `-61` dBm,
  compared with the same connection's sole `RssiRead` event and checked within
  the contract's Int16 range.

Each fixture was confirmed stopped after its run. These are separate
**single-device BLeng candidate iOS production CoreBluetooth RF** gates, not
subscription, passive-disconnect/recovery, two-DK, Android, RN or frozen ABI
results.

### Candidate-only iOS notification and recovery RF

The optional `bleng-candidate-ios-rf-notification` path builds/installs the
signed app before starting the coordinated DK provider so build time cannot
consume its 240-second coordination deadline. Its app verifies the FFF2
`PlatformDefault` Notification mode, exact `EF` event and correlated
subscription enable/disable, then observes two seconds without a new app value
event after the host's post-disable coordination acknowledgement. That
acknowledgement does **not** attest to a second GATT transmission. Generic iOS
device and Simulator builds passed. The first signed attempt installed and
passed the single-image gate, but the iPhone was locked at `devicectl` launch
(`SBMainWorkspace` `Locked`); no app RF ran, and the provider was stopped.
After unlocking, Notification RF passed; the final hardened-runner series
returned run `9619aab1-3880-47c5-9710-a38c72504570`, actual Notification,
subscription `10` and exact `EF`.

The separate strict-Indication path also passed signed device RF against the
previous string-error candidate:
`bleng-candidate-ios-rf-indication` reuses the same signed-app and coordinated
DK mechanism with the provider in `indication` mode. It asks for strict
Indication on FFF2 (dual notify/indicate) and expects the candidate facade's
`Operation` error with canonical category `NotSupported`, verifies no active
route via subsequent FFF2 notification enable/disable, then requires actual
Indication on FFF5 (indication-only), exact `EF`, disable and two seconds of app
event silence. The HIL now uses the structured `BleEventError` payload; the
previous run remains evidence for the old candidate and must be repeated after
the API change. The final series passed run
`fed77a61-bba1-4535-957b-aeb538a641cb`, actual Indication and subscription
`15`. Subsequent FFF2 enable/disable establishes facade admission availability,
not an independent native-route resource proof.

The same HIL now passed active-subscription passive loss/reconnect (run
`711a1054-4d1c-4035-a5fb-9bc2e9c01dc4`) and three-cycle single-owner recovery
(run `0a0fc511-8f5c-48e8-a774-eb29b47d88a4`). Receipts retain the native
command-less `connection-error` category instead of relabeling it as an
`.ok(.disconnected)` event; stale old-connection commands return
`ConnectionNotActive`, and old subscriptions on a new connection return
`SubscriptionNotActive`. Recovery includes exact `00` readback, fresh
connection/GATT/subscription identities, bounded app-event silence and final
logical disconnect/shutdown. Two erroneous HIL expectations were corrected
before these complete passing reruns, without changing Rust/Apple production
logic. The provider and runner subsequently received exact PID/PGID startup
rollback hardening and these four gates reran successfully.

Scan/connect/GATT/I/O/RSSI also reran on the final HIL. The complete run-ID matrix,
failed attempts, implementation references and evidence limits are in the
[single-DK iOS RF report](../testing/reports/2026-09-30-bleng-ios-single-dk-rf.md).
The enumerated single-device matrix is complete for this candidate; Android,
two-DK concurrent RF, public error/API review and default-package graduation
remain separate acceptance boundaries.

### Reviewed name/timeout candidate rerun

Following the API review, the exported object is now `Bleng` (with crate-private
`BlengOwner`), `nextEvent()` is one shared single-consumer FIFO, and timeout
inputs accept only `1..=86_400_000` ms. The complete iPhone 14 Plus + standalone
nRF52840 DK matrix was rerun against the rebuilt two-component image:

- scan: `99f9b67d-f4c1-4f4b-af4b-a9068bef9a09`;
- connection/disconnect: `2e244a36-2df1-43a2-ba32-071b936ec39a`;
- GATT: `84e537a1-3f84-492d-ad98-a443367c409d`;
- characteristic I/O: `ef6d87b1-e227-4c09-9c7b-56b901b76ad3`;
- RSSI: `de4d107e-d1f2-435e-9fdf-ee17eda0267e`;
- Notification: `9dfa14f3-63eb-40e4-8a57-b7a48317dde3`;
- strict Indication: `78ab14dc-bd7d-4942-a53a-27a9ed2c1f80`;
- passive disconnect and three-cycle recovery: `dc8aeb52-15ec-404c-9e0e-415172017ca7`.

The provider also stopped using optional `GATT_RESET_SERVER` as a normal startup
requirement; profile doctor/provision is the resident-database boundary. The
Apple runner now waits for the CoreDevice wired tunnel and launch-unlock state
with bounded retries. These are still candidate-only receipts: they do not
prove Android, two-DK concurrency, RN high-level API, public ABI freeze or
default two-component packaging.

### Candidate naming and graduation

`candidate` deliberately names opt-in Cargo features, isolated staging/work
directories, unfrozen snapshots and test-only recipes/apps; it is not a second
production image, a public package name or an instruction to keep two Apple
products forever. The currently generated `Bleng`/`BlengError` are real
candidate exported names, **subject to public API review**; they should not be
assumed final just because these tests link. Graduation requires an explicit
API/error/lifecycle review, the remaining Apple/Android package, runtime and
RF gates, and the complete same-candidate ABI validation. Only then should
`crates/blehub-uniffi` default packaging switch to exactly the two reviewed
components; migrate relevant checks to that sole product and remove the
candidate-only feature/staging/recipes/apps that no longer provide independent
evidence. Do not rename candidate paths into production or remove default
one-component checks prematurely.

## Production package acceptance order

After this candidate review is approved, implementation proceeds in this order:

1. Finish and test the full crate-private BLeng operation surface and cleanup
   error accounting in Rust.
2. Add a dedicated, reviewed `bleng` UniFFI facade; do not export
   `foreign-probe` or rename the probe into production.
3. Make `crates/blehub-uniffi` the sole image owner for exactly `blehub` and
   `bleng`; reject toy/test/probe components.
4. Extend binding generation and API snapshots with per-component allowlists.
5. Extend Apple staging to both components, one XCFramework/native image and
   clean artifact-only consumer.
6. Extend Android AAR generation and clean consumer; then run Android ART
   lifecycle/foreign-object tests.
7. Run iOS device runtime and only then BLeng-specific iOS RF, separately from
   existing low-level Apple RF evidence.

Until these gates pass, statements must use “BLeng candidate” or “isolated
mechanism probe”, not “production BLeng”, “public API” or “ABI frozen”.
Completed RF gates must name the exact candidate, platform, device and scope;
none substitutes for the remaining RF or package acceptance gates.

## Deliberately deferred decisions

The following require the public API review before implementation is frozen:

1. whether `nextEvent()` is sufficient or a dedicated event-reader object is
   required;
2. exact error enum and whether domain failure remains an error or is exposed
   only through canonical events;
3. exact timeout type/range and whether a default deadline exists;
4. whether `assembleBackend` belongs on `Bleng` or a separate builder/factory;
5. whether the first production candidate exposes all operation slices at once
   or lands behind staged feature gates;
6. generated Kotlin/Swift package/module names and snapshot ownership.

These are architectural review points, not implementation details to infer
from the temporary probe.
