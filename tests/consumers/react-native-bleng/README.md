# Formal React Native BLeng consumer

This directory is the clean downstream executable consumer for
[`@blehub/react-native-bleng`](../../../integrations/react-native-bleng/README.md).
It is the only maintained React Native UI host for the formal package line.

The host exercises the approved typed lifecycle and scan surfaces:
`createBlengCentral()`, `initialize(): Promise<BlengInitializeResult>` (`{ snapshot }` or `{ failure }`),
`close(): Promise<BlengCloseResult>`, and explicit `beginScan` / start wait / pull / stop / cancel.
The typed startup result is an unfrozen candidate projection of Rust's actual initialization
snapshot, not a live state stream or an implication of powered-on/authorized BLE. The page displays
adapter state, independent scan/connection authorization and optional name without synthesizing
readiness. Close distinguishes logical owner closure from cleanup success: a nonterminal failure
retains the owner; a terminal cleanup failure retires controls while displaying `closedWithError`,
not resource-zero or a passed gate. Initialization/failure evidence remains until the next explicit
session. Pending lifecycle actions cannot overlap. It does not depend on the transitional
`@blehub/react-native-internal` probe, raw UniFFI handles, command IDs, BLE events, or recovery
policy objects. Artifact-only UniFFI consumers remain under the separate Android and Apple
consumer directories.

## One workbench, one run owner

The formal App now uses **Tests → Current run → Evidence**. The owner, pending action and
observations live above the view switch; navigation only changes presentation. It never creates
an owner, submits a command, closes a session, cancels a wait or clears the current result.
Explicit Start opens Current run; later async completion does not steal navigation back from
Evidence. Close failure retains the same owner for explicit retry.

Evidence preserves observed start/close outcomes, including failed attempts after a successful
cleanup retry, and the initialization snapshot after close. It is a **bounded in-memory UI summary**:
only the latest 32 action observations are shown, with an explicit omitted count. Starting a new
session replaces the previous run summary. This is not persistence across Hermes reload/process
restart, a native resource-zero receipt, or the host's durable evidence bundle/verdict.

Current run also offers an explicit manual scan workflow. Start scan submits once and waits on
the original start result; Pull next requests one observation, with no automatic polling. Stop
submits a typed operation and waits once; Cancel start/stop receives the original control receipt,
not rollback. A consumed stop timeout/failure retains cancel and scan controls. Only an explicit
`Submit another stop` submits a replacement; a rejected replacement preserves the old cancel
handle. A pre-consumption `input/invalid_timeout` permits correcting the original wait input.

The scan controller also lives above navigation; owner close is independent of pending scan
start/pull/stop. Old completions settle their original action record without mutating a new
session. UI state holds only the last eight advertisement text summaries and 16-byte binary
prefixes, not opaque peripherals or full payload buffers. The legacy workbench's RF scenarios,
provider configuration and host ledgers remain unmigrated; manual scan does not replace them.

## Checks

```sh
# Explicit dependency setup: first checkout, lock changes, or a clean candidate job.
pixi run just react-native formal-host-install

# Daily software checks use installed dependencies; no implicit npm ci or device work.
pixi run just react-native formal-host-check
pixi run just react-native formal-host-test
```

Jest discovers only `src/**/*.test.[jt]s?(x)`. Device E2E runs separately from the component
test process; no Detox or DUT npm dependency and no default device gate is added.

Every `testID` in this App comes from [`src/ids.ts`](src/ids.ts): one vocabulary shared by the host
scene code and every UI driver, guarded by `tools/tests/react_native/test_workbench_ids.py`.
The Maestro idle/lifecycle comparison entries were removed on 2026-11-22 - they never gated
anything and their retry policy could not be bounded to a scene window.

Jest uses the same pinned RN testing libraries as the transitional host, with independent discovery
and dependencies. It mocks the formal package's `createBlengCentral` boundary only; render/
browsing creates no native owner. Tests cover non-ready snapshots, independent authorization,
missing/empty names, pending-click serialization, restart and cleanup failures. Workbench tests
also switch views while start/close is pending, verify completion without navigation changes,
retain failure/snapshot evidence after cleanup, and enforce the disclosed summary bound. This is
software UI coverage, not Nitro execution or a physical-device gate. Scan tests cover one-shot
result consumption, explicit new stop versus invalid-input re-wait, single pending pull, typed
failures, owned-data summaries, cancel independence, close/restart fencing and bounded evidence.
Close tests distinguish busy/rejection from terminal cleanup failure and retain both startup and
shutdown failures without inventing successful cleanup.

Android and iOS builds are run through the repository's Pixi/Just recipes after the required
Rust/UniFFI artifacts have been staged. This app is a build/runtime consumer, not a replacement
for native HIL or physical RF evidence.

The Apple entry is `pixi run just react-native formal-host-ios-build [false|true]`: omitted or
`false` is ordinary verification, while `true` is the explicit refresh and passes Python
`--refresh-pods`. Ordinary preparation verifies
`.work/react-native-bleng-ios-consumer/pods-preparation.json` and installed-graph JSON only; it
does not run `pod ipc`, `pod install` or RN artifact probes. Missing/stale evidence fails closed
without adoption or fallback. Node and other dependency installation remain explicit through
`formal-host-install`.

## Apple engine ownership and reload

`ios/BleHubBlengHost/AppDelegate.swift` uses the same host-scoped
`FormalReactNativeDelegate` as the native reload harness. The delegate creates a fresh lifecycle
module for each engine and attaches its exact predecessor before RN returns it to JS. It retains
only the latest lifetime, preserves RN's normal builtin-module fallbacks, and never manually
invalidates an old module to manufacture cleanup evidence.

Native tests run both surface-free reload and a mounted Fabric status view over scripted BLE
backends. They verify that old cleanup gates replacement native startup, preserve cleanup
failures, and observe the view's `started → pending → started/failed` states independently of JS
settlement markers. This small native-mounted view is not the complete workbench or an on-screen
App/window/device gate. See the
[candidate report](../../../docs/testing/reports/2026-11-11-apple-formal-delegate-rendered-reload.md).

## Native bridge contracts

Native contract tests live in this consumer rather than in the integration library or the legacy
probe host. Android verifies native Nitro Promise resolution, the original supervisor rejection
and close/restart behavior. Apple owns the migrated Supervisor start/close resource observation
and the native Swift Nitro Promise lifecycle/rejection test. Both native targets also cover the
complete state/independent-authorization/name projection matrix. Formal scan tests additionally
exercise Rust projected errors/options/timeouts, owned bytes, opaque peripherals, stop/cancel,
retirement and duplicate/stale start receipt fences. Typed close tests use real Rust startup and
retirement failures, retain repeated terminal failures and distinguish nonterminal busy. They
are compiled in the App/test graph; execution requires the explicit native gates below. Apple reader-wiring tests submit
scan operations only through an injected fake backend, checking that failed/cancelled recovery
capture does not end subsequent ordinary scan delivery. They handshake each of 65 advertisements
rather than overflowing the core FIFO with a burst, and await the actual stop terminal. These tests
do not call the old `HybridBleHubProbe`, perform physical RF, or exercise JS/Hermes/UI.

```text
# Compile the App and native contract test artifacts; no device execution:
pixi run just react-native formal-host-android-build
# Explicit Android release candidate with bundled JS (no Metro required):
pixi run just react-native formal-host-android-build release
pixi run just react-native formal-host-ios-build

# Explicit opt-in runtime gates (device/simulator preparation remains separate):
pixi run just react-native formal-host-android-native-test DEVICE_ID
# Platform-simulated companion presence; CDM creates/removes the synthetic association:
pixi run just react-native formal-host-android-companion-presence DEVICE_ID
# Real system chooser through the library's association command (needs the advertising fixture):
pixi run just react-native formal-host-android-companion-association DEVICE_ID
# Final human experiment: same chooser, then carry the DK away for a real disappearance:
pixi run just react-native formal-host-android-companion-peer-leaves DEVICE_ID
pixi run just react-native formal-host-ios-native-test SIMULATOR_ID
# Release bundled Workbench UI gate; builds App + UI tests only.
pixi run just react-native formal-host-ios-ui-test SIMULATOR_ID
```

The companion-presence gate drives the real `BleHubCompanionDeviceService` and presence lane on
the device. It uses CDM's own test hooks (`cmd companiondevice associate` and
`simulate-device-appeared/disappeared`), so it needs neither a chooser nor a physical peer; the
test creates and removes the synthetic association itself and asserts one owner-scoped key across
both transitions. It is platform-simulated presence, not RF evidence.

The companion-association gate is the real chooser: it calls
`beginCompanionAssociation(..., notifyOnDeviceNearby = true)`, proves the phone can see the
advertising fixture, selects the single LE-filtered row, and asserts the library armed presence
and the fixture's **real** appear reaches the lane with one key (the disappearance is simulated so
the test is bounded). It requires the host to start the advertising nRF52840 fixture first.

The companion-peer-leaves gate is the final human experiment: the same chooser and real appear,
then the DK is carried out of range (or powered off) and the test waits for the **real**
disappearance with the same key. The same transition has also been verified automatically by
stopping the fixture's advertising.

The iOS UI gate uses the shared `BleHubBlengHostUISimulator` scheme, which builds
only `BleHubBlengHost` and `BleHubBlengHostUITests` in Release. That keeps the
native `@testable` target, and therefore any testability override, out of the
product App and package Podspec. Its two scenarios cover the real
`Tests → Start → Close` lifecycle and `Tests → Evidence → Current run`
navigation while retaining the same live run and snapshot. See the
[simulator Workbench smoke](../../../docs/testing/reports/2026-11-11-formal-workbench-simulator.md).

The Android build accepts only `debug` (default: `assembleDebug` +
`assembleDebugAndroidTest`) or `release` (`assembleRelease`, no instrumentation APK).
Gradle retains its normal dependency resolution behavior; no `--offline` flag is forced.
Full build logs are written to `.work/logs/formal-host-android-{variant}-build.log`.
The release output is `android/app/build/outputs/apk/release/app-release.apk`; the current
consumer signs it with its debug key, so this is a bundled-JS test candidate, not a store release.
Neither build variant installs an APK or executes a device gate.

The normal build dependency runs `formal-host-check`, not `formal-host-install`.
Dependency installation is explicit: a clean candidate job must run `formal-host-install` first;
daily incremental builds reuse installed `node_modules`. Build still performs typecheck/autolinking,
so skipping dependencies is unnecessary for this reuse. On this Mac, bound the build to 180 seconds
with a further 10-second termination allowance; on timeout inspect the log and only this attempt's
processes before considering a retry:

```sh
pixi run just react-native formal-host-check
pixi run timeout --kill-after=10s 180s just react-native formal-host-android-build release
```

The Apple build has one owner, [`bleng_ios_consumer`](../../../tools/react_native/bleng_ios_consumer.py):
it validates the installed formal Podspec's required production dependencies, compiles the
native adapter/owner sources in the isolated staged SwiftPM graph declared in
[`ios/PlatformContract/project.yml`](ios/PlatformContract/project.yml), then runs unsigned generic
iOS Simulator `build-for-testing` for the formal App/native-test graph. The independent compile
target cannot import Nitro/React; it is a static library, not another App. Rendered projects and
its outputs stay under `.work/react-native-bleng-ios-consumer/`, with no unconditional cache cleanup.
The former separate static Swift/Nitro consumer and its duplicate Node/Pod roots are retired.
The native runtime entry selects only `BleHubBlengHostNativeTests`, not the UI test target. Android uses the maintained instrumentation
installer/runner rather than Gradle `connected*` tasks. Build success only proves compilation and
packaging, not executed native assertions. Both platforms retain the one combined production
UniFFI image and do not create a second test native library.

The independent `pixi run just apple bleng-recovery-test` runs the existing projection/inbox/runner
Swift tests in a standalone macOS XCTest bundle with the staged image. It does not instantiate a
native BLE backend, launch an App or boot a simulator. Unique xcresult validation rejects empty,
skipped or failed execution. This complements, but does not execute or replace, this consumer's
iOS native reader-wiring tests.

## Managed scene lanes

Every managed scene is one host-owned run against the formal App, and the App is driven by its
**own** test target — `BlengSceneDriverUITests` (XCTest) on iOS and
`BlengSceneDriverInstrumentedTest` (instrumentation) on Android. There is no UI-automation server,
no helper APK, no WebDriverAgent, no tunnel and no attach-only web session in either lane; the
Appium chain was deleted on 2026-11-23.

One host entry point covers all eleven scenes:

```sh
pixi run just react-native formal-host-android-managed-scan --help
pixi run just react-native formal-host-ios-managed-scan --help
```

Both take `--scene`, a reviewed candidate (`--apk-sha256` / `--app-tree-sha256`), the exact
automation artifact (`--test-apk-sha256` / `--xctestrun`), the physical fixture identity plus
contract/profile, and the four explicit authorizations (`--authorize-install`,
`--authorize-fixture-start`, `--authorize-bluetooth-permissions`, and `--use-root` on Android).
Nothing is installed, granted, flashed or enabled implicitly, and there is no helper-APK or
HANS option left to pass.

What the lanes own:

- **The host**: candidate freeze, phone lease, fixture session, install/launch, the scene's host
  actions (emit a value, arm/hold/reject a peer fault, drop the peer, re-advertise), cleanup order
  and the sealed bundle.
- **The scene plan**: a declarative list of steps over the App's pinned machine surface
  (`run-status`, `action-ledger`, `notify-latest`, the declared controls). A plan never scrolls:
  the Android accessibility tree omits off-viewport children of a `ScrollView`, so a fact a scene
  needs is pinned in the App instead of hunted with gestures.
- **The App**: only product facts — the state it reports, the actions it records, and the stream's
  terminal.

Each attempt holds the frozen candidate, the request, the driver record and per-phase receipts,
the fixture inputs/live close receipt, the phone journal, and a sealed `result.json`/`manifest.json`.
Replay is movable and offline; it never authorizes a device:

```sh
pixi run just react-native formal-host-scan-verify ATTEMPT_DIRECTORY
```

`status=passed` means this managed scene observation and the recorded host cleanup passed.
`host_gate_status=pending` and the scene's own `rf=<scene>-ui-observation` stay explicit: it is not
a native RF, resource-zero, package/ABI or release exit. See
[the Android in-process lane report](../../../docs/testing/reports/2026-11-23-android-native-scene-lane.md)
and [the iOS all-scenes report](../../../docs/testing/reports/2026-11-23-ios-native-all-scenes.md).

## Host-injected scan UI scenario — not a scan CLI

The same module now provides `exercise_scan()` for a trusted owning host with an existing
WebDriver session. It is **not reachable from `run()`, the CLI or `formal-host-ui`**; those remain
lifecycle-only. There is no `--scenario scan` or ready-JSON shortcut.

The host must inject a bounded `require_authority()` callback tying this physical phone/App/run
to its phone owner, active fixture session, DK lease and fresh installed-image receipt. The name
and advertised service expectations come from that validated contract. The callback is mandatory,
checked during observation and immediately before each remote action; failure stops further UI
commands, including speculative stop/close. It is trusted host code, not a security sandbox.

The function verifies an idle workbench, initializes once, starts scan once and uses at most eight
explicit pulls. Only the newly pulled advertisement's name and advertised service may match;
service-data UUIDs, old samples and different advertisements cannot be combined. Count/sample
reads use a stable count → samples → count observation so asynchronous completion between
WebDriver reads is not mistaken for damaged UI state. The service omission suffix is parsed
strictly; an omitted UUID is never inferred.

One total deadline covers startup, browsing, pulls, one stop and one close. The visible observation
timeout must already match the supplied host budget; this function does not edit inputs. The
host must also bound WebDriver's HTTP timeout because a UI deadline cannot interrupt an in-flight
remote call. Ambiguous click delivery is never retried. No implicit cancel/retry/failure cleanup
runs. Final Evidence must retain the same run, exact ordered success actions, scan count/samples,
`ownerClosed` scan phase and original initialization snapshot. UI closure is still not resource-zero.

### Active ownership composition

[`bleng_scan_host.exercise_owned_scan`](../../../tools/react_native/bleng_scan_host.py) now composes
that guard from the actual phone owner, an already-started in-process `DirectFixture`, and the
same `FixtureSession`. Platform is derived from the phone owner, not a second device selector.
The phone run must equal the fixture session; expected name/service come only from its contract.
Each guard verifies phone ownership, active fixture/broker, frozen request/receipt bytes and the
authoritative receipt/snapshot hashes, then checks them again after the required App-identity
callback. A valid receipt left behind by a stopped fixture is insufficient. This path does not
accept a separate provider process or a receipt file as a substitute for an active fixture object.

`DirectFixture.require_active_session()` is read-only: it rejects unstarted/stopping/stopped or
foreign sessions and a lost broker without serial I/O. Startup becomes active only after actual
startup/advertising succeeds; the same fixture object cannot be started again after an attempt.
This is local ownership, not RF liveness or a new installed-image proof.

iOS uses `SCAN_OBSERVATION_SCOPE` on the existing `IosRadioLease`, sharing the physical phone lock
with radio gates. Its `finish_observation(primary_error, finalize_owned=...)` requires a trusted,
bounded host finalizer while the owner is held. A primary scenario failure remains recorded even
if finalization permits release. Finalizer, ownership, publication or release failure quarantines
rather than claiming release. It performs no radio restoration/readback and does not claim ON,
resource-zero or RF passed. Radio `finish()` and observation `finish_observation()` reject cross-scope
use; the existing radio restoration proofs remain required. Unresolved observation quarantine
cannot be cleared by borrowing a radio gate's restoration transcript.

### Concrete App identity checks for the in-process binding

`*CandidateOwner.bind_native()` creates the one fixed, callable identity binding the scene runs
against. It verifies phone/run ownership first, shares the host's absolute deadline and rejects
rebinding; a check never selects or adopts a replacement process. A failed check poisons the
candidate, so cleanup refuses rather than closing whatever is there now.

- Android proves the **installation**, not the pid: unique uid, `--user 0`, and the installed single
  `base.apk` SHA256. That is what the instrumentation lane can honestly claim, because its own test
  process replaces the launched one. Procfs denial, split APKs or a digest mismatch fail closed with
  no root/install fallback. Every check performs fresh reads.
- iOS binds exact install/launch receipts, CoreDevice ID/PID/executable and a native process nonce.
  The formal App opts in **before RN startup** only with `--blehub-formal-identity-run-id <UUID>`.
  The native-only responder under `Documents/blehub-formal-identity/<runId>/` creates no BLE owner
  and exports no JS getter. Each check copies one fresh challenge and posts one namespaced Darwin
  notification. Only its exact response passes; a same-boot null/old response is bounded pending
  observation, not success. Malformed/foreign data or SDK failure is not retried.

The Apple protocol accepts boot-first `boot_id` / `challenge` canonical UUID literals with ASCII
JSON whitespace; duplicate/escaped keys, old boot and requests over 1024 bytes are rejected. A
formal-only null sentinel exists before identity publication, so first response observation does
not rely on an absent file becoming readable. It never counts as live proof. RN reload does not
regenerate this native nonce.

**iOS candidate provenance is still a required host responsibility.** `read_candidate_sha256`
checks the frozen local artifact, while `require_candidate_provenance` must bind this run's managed
install/launch owner to the fixed receipts. Local hash, bundle inventory or a stale install receipt
alone cannot prove the currently installed binary. There is no default successful callback.

**The concrete live host is the scene producer.** It selects the physical phone, freezes and binds
the candidate, owns bounded failure cleanup/finalization, and seals the final evidence bundle; the
driver seam it hands the App to is `NativeIosSceneDriver` / `NativeAndroidSceneDriver`. Neither new
identity sampling nor macOS XCTest proves mobile runtime, RF or cleanup on its own. See
[app identity evidence](../../../docs/testing/reports/2026-11-04-formal-host-app-identity.md).
Neither side acquires/releases leases, opens serial transport, starts/stops fixtures or opens a
browser session: those stay with the host, and a successful scene deliberately leaves them with it
until cleanup. Initial powered-on/authorization observations are not live permission proof.

### The managed scene set and the fault-armed rule

One exercise function per scene, one evidence vocabulary per scene, one recipe per platform:

| Scene | Recipe suffix | What it proves |
|---|---|---|
| `scan` | `managed-scan` | one advertisement pulled through the bounded manual scan |
| `notification` / `indication` | `managed-notification` / `-indication` | a subscription's actual mode and a host-emitted value reaching the App |
| `io` | `managed-io` | read → write → read-back (a receipt is not a stored value) → RSSI |
| `cancellation` | `managed-cancellation` | a queued read cancelled while the peer holds the active one, then recovery |
| `passive-disconnect` | `managed-passive-disconnect` | the peer ends the link and the App announces it itself, with no command submitted |
| `recovery` / `recovery-ten` | `managed-recovery` / `managed-recovery-ten` | the same cycle 3 / 10 times on one owner: scan, link, discover, read, then a peer end the App announces |
| `write-reject` | `managed-write-reject` | the peer refuses the exact write; nothing moved; the same write then succeeds |
| `ccc-enable-reject` | `managed-ccc-enable-reject` | a refused enable is a failed subscription; a retry subscribes and delivers |
| `ccc-disable-reject` | `managed-ccc-disable-reject` | a refused disable leaves the subscription delivering; a retry retires it |

The last three **arm a fault**, so they stay separate entries instead of joining a normal-path
sequence: the fixture parks the exact request inside the firmware and the host resolves it with the
peer's own decision. A scene never treats a platform error as a peer refusal, nor an enabled
subscription as a delivered value; each claim comes from the side that can make it. Semantics and
offline evidence are recorded in the [fault-reject report](../../../docs/testing/reports/2026-11-20-managed-fault-reject-scenes.md).

The two link-end scenes are host-driven in the opposite direction: the peer ends its own link, so the
App may only *observe* it. They are separate entries because the claim is about what the App can know
without asking — `passive-disconnect` for one end with a quiet window, `recovery(-ten)` for repetition
on the same owner, where a firmware that stopped advertising after the link must be asked by the host
to offer itself again (`DirectFixture.resume_advertising`). Evidence: the
[connection-end report](../../../docs/testing/reports/2026-11-21-connection-end-lane-and-passive-disconnect.md).

### The multi-link lane is not a workbench scene

`multi` cannot be expressed as a workbench scene: the bench holds one link and one subscription by
design, and an artifact consumer may hold exactly one `dev.blehub` artifact. The only shape that
combines the engine with the real Android backend is the formal App's own **non-UI instrumentation**
lane — the same shape as `native-notification`, so no page and no screenshot is involved.

```sh
pixi run just react-native::formal-host-android-native-multi-link \
  --device <serial> --app-apk <apk> --app-apk-sha256 <sha> \
  --test-apk <androidTest.apk> --test-apk-sha256 <sha> \
  --peer "port=<port>,contract=<contract>,profile=<profile>,identity=jlink:<serial>" \
  --peer "port=<port>,contract=<contract>,profile=<profile>,identity=jlink:<serial>" \
  --output .work/android-multi --timeout 900 --cleanup-seconds 150 \
  --authorize-install --authorize-fixture-start --authorize-bluetooth-permissions --use-root
```

The first `--peer` is the link the host ends; every other declared peer must still deliver a value the
host applies **after** that end, and each link must read back the bytes it wrote. The peer list is the
topology — no device name, port or profile is built into the lane. Evidence: the
[multi-link report](../../../docs/testing/reports/2026-11-21-multi-link-isolation.md).

The same entry takes the repetition and the fault as declarations, never as another runner:

- `--rounds 1..10` repeats the whole scene; each round is a complete run of the same contract.
- `--fault write-reject:<index>` makes the host refuse that peer's write inside its own firmware.
  That link must then report the refusal and read back the value it held before the attempt
  (`write-rejected:<index>` / `unchanged:<index>`), while every other link keeps the normal
  `written` / `io` pair and the rest of the scene — values, the peer-initiated end, the survivor —
  is unchanged. The host requires the firmware's own `REJECT` receipt; an uncaptured hold fails
  the round instead of passing as a clean scene.

The [approved scan design](../../../docs/research/react-native/react-native-bleng-scan-contract.md)
is now implemented in formal specs/exports, Rust product projection, both native bridges and
this App's manual workflow. Both native compile graphs and software regressions have current
[candidate evidence](../../../docs/testing/reports/2026-11-04-formal-rn-scan.md); mobile native tests,
Hermes reload behavior and RF are still separate unverified gates. The isolated pinned-Nitro codegen
experiment remains historical evidence, not a replacement implementation or runtime proof.
