# Formal React Native BLeng consumer

This directory is the clean downstream executable consumer for
[`@blehub/react-native-bleng`](../../../integrations/react-native-bleng/README.md).
It is the only maintained React Native UI host for the formal package line.

The host exercises the approved typed lifecycle and scan surfaces:
`createBlengSessionOwner()`, `start(): Promise<BlengAdapterSnapshot>`,
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
pixi run just react-native formal-host-check
pixi run just react-native formal-host-test
```

Jest uses the same pinned RN testing libraries as the transitional host, with independent discovery
and dependencies. It mocks the formal package's `createBlengSessionOwner` boundary only; render/
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
pixi run just react-native formal-host-ios-native-test SIMULATOR_ID
```

The Android build accepts only `debug` (default: `assembleDebug` +
`assembleDebugAndroidTest`) or `release` (`assembleRelease`, no instrumentation APK).
Gradle retains its normal dependency resolution behavior; no `--offline` flag is forced.
Full build logs are written to `.work/logs/formal-host-android-{variant}-build.log`.
The release output is `android/app/build/outputs/apk/release/app-release.apk`; the current
consumer signs it with its debug key, so this is a bundled-JS test candidate, not a store release.
Neither build variant installs an APK or executes a device gate.

The normal build dependency still runs `formal-host-check`, including `formal-host-install`
(`npm ci`). To reuse already-installed `node_modules` for a device-free build-only attempt, run
the checks and build separately, skipping dependencies explicitly. On this Mac, bound the
build to 180 seconds with a further 10-second termination allowance; on timeout inspect the
log and only this attempt's processes before considering a retry:

```sh
pixi run just --no-deps react-native formal-host-check
timeout --kill-after=10s 180s pixi run just --no-deps react-native formal-host-android-build release
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

## Shared Appium lifecycle E2E

The maintained shared scenario is
[`run_bleng_ui`](../../../tools/react_native/run_bleng_ui.py), not separate Swift and Kotlin
copies of the same button flow. It uses each platform's existing Appium helper and the App's
`testID` attributes to check `idle → started → visible initialization snapshot → closed`, with
Tests/Evidence/Current run navigation between actions and final retained Evidence. Returning to
Current run must show the exact same initialization state/authorization fields, not merely other
valid enum values.
The three state/authorization fields must be present and valid, but no powered-on or authorized
value is required for this initialization/lifecycle gate. Tests call only the visible UI, never a
raw UniFFI handle or an internal probe.

**The bare Android lifecycle CLI below remains blocked.** Managed scan has a concrete
[`AndroidAutomationOwner`](../../../tools/react_native/bleng_android_session.py) software path,
with live preparation, protected transport/session binding and a readback finalizer. It is not a
ready-JSON bypass or a live RF verdict; see the [candidate and remaining gaps](../../../docs/testing/reports/2026-11-04-formal-android-session-owner.md). The phone is already rooted; root use and installer confirmation are not the
blocker. The managed candidate accounts for old-session/process scope and settings/AppOps
readback under an explicit prepared profile; actual device behavior and full driver/config
provenance remain unverified. Explicit helper preparation reuses the existing
root-aware installer, rather than requiring manual installation or a no-root driver. The bare CLI runner records
`stage=driver-admission`, fails before starting Appium/creating a session, and the CLI exits 2.
Prepared helper APKs or a different unreviewed driver version do not override this refusal.
See the [audit and remaining work](../../../docs/testing/reports/2026-11-04-formal-android-automation-admission.md).
iOS behavior is unchanged; synthetic scenario passes do not authorize either platform's live gate.

This is an **opt-in attach gate**, not an installation or device-provisioning command:

1. Prepare the selected physical device under the existing host ownership/lease protocol. Do not
   run alongside another UI, native HIL, RF, permission or radio gate on that device.
2. Explicitly install the formal App with a packaged JS bundle (Release-shaped, no Metro dependency)
   and open its idle page. Do not restart or replace an unknown old App to satisfy this precondition.
3. Prepare the existing Appium server/driver dependencies separately. Android requires installed
   UiAutomator2 helpers **and the concrete prepared owner profile (device validation pending)**;
   iOS requires WDA signing/developer authorization and the existing RemoteXPC
   tunnel preflight. This recipe does not install tools, unlock a keychain, or request passwords.
4. Run the shared gate with explicit device and application identities. For Android, supply the
   activity; for iOS, supply the signing team:

```text
pixi run just react-native formal-host-ui android DEVICE_ID com.blehubblenghost APP_IUM_ENTRY .MainActivity
pixi run just react-native formal-host-ui ios DEVICE_ID org.reactjs.native.example.BleHubBlengHost APP_IUM_ENTRY "" TEAM_ID
```

`APP_IUM_ENTRY` is the local path to the already-installed Appium Node entry script. For custom
ports, timeout or WDA settings, use `pixi run python -m tools.react_native.run_bleng_ui --help`.

The runner neither installs nor launches/resets the target App. It verifies the foreground App,
requires the formal idle Tests page, browses without starting a run, clicks Start and Close once
each, and waits for visible results with bounded polling. Navigation must preserve session state;
the final Evidence page must contain both observed successful actions. A click with an uncertain result is not retried. Driver initialization
failure is not scenario execution. Finishing the WebDriver session must leave the result page
visible; this behavior still requires real-device evidence.

Each UUID attempt under `.work/react-native-bleng-ui/` records `result.json` (including verified
`startup_snapshot` state and independent authorizations), Appium logs and
best-effort page source/screenshot. Primary, diagnostic and cleanup errors remain separate;
cleanup failure makes the result fail. On a scenario failure, the runner does not guess a native
cleanup outcome or terminate the App: inspect the evidence and use the owning host's explicit
cleanup procedure before releasing device ownership. UI `closed` is not independent
resource-zero evidence, and no UI result implies BLE/RF, artifact or recovery coverage.

## Managed Android lifecycle

The separate maintained entry is `formal-host-android-managed-lifecycle` →
[`run_bleng_android_lifecycle`](../../../tools/react_native/run_bleng_android_lifecycle.py) →
[`run_android_lifecycle`](../../../tools/react_native/bleng_android_lifecycle.py).
It does **not** unblock or change the bare attach-only `formal-host-ui android` entry above.
It consumes an already-built bundled-JS APK; build and helper APK installation remain separate.

Pass the physical serial, fully qualified activity, local Appium paths, distinct ports, reviewed
APK/helper hashes and explicit authorizations. For argument details:

```sh
pixi run just react-native formal-host-android-managed-lifecycle --help
```

Command shape (replace the uppercase tokens with reviewed local values; no hashes or device
paths are embedded in the recipe):

```text
pixi run just react-native formal-host-android-managed-lifecycle \
  --device DEVICE_ID --app-id com.blehubblenghost \
  --activity com.blehubblenghost.MainActivity \
  --apk PREBUILT_RELEASE_APK --apk-sha256 REVIEWED_APK_SHA256 \
  --appium-home APPIUM_HOME --appium-entry APPIUM_ENTRY --node NODE_EXECUTABLE \
  --appium-port APPIUM_PORT --system-port SYSTEM_PORT --output ATTEMPTS_DIRECTORY \
  --helper-sha256 io.appium.settings=SETTINGS_SHA256 \
  --helper-sha256 io.appium.uiautomator2.server=SERVER_SHA256 \
  --helper-sha256 io.appium.uiautomator2.server.test=SERVER_TEST_SHA256 \
  --timeout 240 --phase-timeout 30 --cleanup-seconds 60 \
  --authorize-install --authorize-helper-settings --use-root
```

`--use-root` opts into the existing root-aware App installer; omitting it never triggers root
fallback. Helper preparation has its own explicitly authorized root-scoped mock-location AppOp
and temporary store rollback. This is real Android helper configuration, not mocked BLE or a
DUT BLE permission grant. Invalid local inputs fail before device acquisition. Unknown old App
processes/sessions are refused; do not automatically stop a retained prior result to make a run fit.

One absolute scenario deadline covers freezing/preparation/UI; cleanup has its own bounded budget.
The runner executes the existing lifecycle scene, then closes the owned session/server/platform,
restores helper scope, finalizes the App, writes prepared evidence and finalizes the phone journal
before publishing `result.json`/`manifest.json`. Success keeps the Evidence page. Primary,
cleanup, phone and publication errors stay distinct; unknown cleanup quarantines ownership.

Each UUID attempt contains the frozen `candidate.apk`, candidate/helper/platform/automation
receipts and logs, `phone.json`, `prepared-result.json`, `result.json` and `manifest.json`.
The CLI replays the sealed bundle before reporting success. Replay is also independently available:

```text
pixi run just react-native formal-host-lifecycle-verify ATTEMPT_DIRECTORY
```

Offline integrity success can describe a faithfully recorded failed attempt; it does not upgrade
its verdict or grant device authority. Managed lifecycle success still has `host_gate_status=pending`
and `rf=not_run`. Powered-off or denied initialization snapshots are valid lifecycle observations,
not scan readiness. The maintained CLI software evidence and the failed/quarantined device attempt are documented
[separately](../../../docs/testing/reports/2026-11-04-formal-android-managed-runner.md). Subsequent
identity-checked physical recovery restored helper/driver settings and archived the known lease;
a fresh same-candidate run failed near Close again and was independently recovered. Neither failure
was rewritten as passed; see [Close diagnosis and candidate](../../../docs/testing/reports/2026-11-05-formal-android-close-diagnosis.md).

The host emits fixed `BLEHUB_FORMAL_CLOSE` JS call/Promise/receipt/failure/finally phase logs without
IDs or payload. Android bridge/UniFFI-call/reader/snapshot phases are gated by
`Log.isLoggable("BleHubBlengClose", Log.DEBUG)` and remain disabled by default. Enabling that log tag
requires the explicit `--close-diagnostics` opt-in. The runner then owns that single-property scope
under the actual phone lease, captures original-PID logs before teardown, restores the original value
with readback, and refuses release if restoration is uncertain. Ordinary runs do not change the tag.
The same opt-in opens one fixed-tag live logcat stream after original App binding and before UI
Start, so diagnosis need not rely solely on a new ADB stream after failure. Capture is limited to
the scenario deadline (at most 300 seconds), 8 MiB raw bytes and 64 recent host-arrival records.
On UI failure, after the one fresh UiAutomator2 status probe, a separate read-only ADB observation
uses at most three seconds of the existing diagnostic budget. It connects only to the existing
`127.0.0.1:5037` daemon, records version/cached-state/transport-selection/service-acceptance/boot-output
stages and stops on failure, without CLI autostart, daemon management, retry or forwarding changes.
`platform/adb-transport.json` remains an observation: cached `device`, transport OKAY and boot-ID
comparison do not prove ownership, close or resource-zero. Live capture covers both probes before
local reaping and DELETE. Ordinary successful runs do not execute this failure probe.

It includes filtered system/RN/Appium logs that may be sensitive; it does not collect unrestricted
App logs, clear buffers or introduce settings changes. After the failure-status/ADB probes and before
DELETE, bounded local cleanup reaps only that attempt's capture child. Unconfirmed local cleanup
blocks release. The raw/arrival records prove neither handler execution nor health: output can include
stderr or buffered records, and silence is not health evidence. Original-PID post-run logs remain separate.

These logs do not change close semantics or become a JS diagnostics/result API; the two monotonic
clock domains must not be directly subtracted. The diagnostic APK's UI runs still failed with no confirmed Close phase. Each failed run was
subsequently identity-checked, physically retired and its helper/tag configuration restored before
lease archival; the original failed results and missing DELETE receipts remain unchanged. One direct
Kotlin close/restart instrumentation case passed, but does not exercise JS/UI. The documented
clickGesture experiment also failed and was reverted. See the report before further diagnosis;
software/build/integrity or that single native case is not a formal RN UI pass.

The same opt-in now captures raw PNG/XML, window and stable bounds from the exact chosen Close
element before its one click. Each read retains the original session/transport/authority/deadline;
the App/page/element are rechecked afterwards, with no re-selection or retry on capture failure.
PNG/XML may contain sensitive UI data and stay in the ignored local bundle. Optional metadata and
raw-file hashes are verified without inferring a Close verdict. Android sessions with an explicit
HTTP timeout also disable implicit SDK HTTP retries, preserving controlled observation polling.
The [pre-click candidate](../../../docs/testing/reports/2026-11-05-formal-android-preclose-evidence.md)
shows a visible Close target, but still has a post-click UI-query failure; it is not a passed gate.

### Idle automation diagnostic control

For isolating automation/navigation from session startup, append both
`--automation-idle-control --close-diagnostics` to the same reviewed managed command.
This opt-in requires the same install/helper authorizations, leases and identity checks;
it does not bypass preparation or adopt an old App/session.

The scene remains `idle`: two fixed rounds of Tests → Evidence → Current run → Tests,
then Evidence, with no Start, Close, scan or initialization-snapshot reads. Its bundle scope is
`formal-android-managed-idle-control`, successful observations are exactly `["idle"]`, and
`startup_snapshot` is empty. The CLI reports `formal_android_managed_idle_control`, **not** a
lifecycle pass; offline verification preserves that distinction and rejects a mismatched scope.
All session/helper/phone cleanup remains required. Success keeps the idle Evidence page visible.
This control does not prove a started owner, native cleanup/resource-zero, reload or RF.
See [the idle-control candidate](../../../docs/testing/reports/2026-11-05-formal-android-preclose-evidence.md#12-2026-11-06idle-ui-control-真机通过start-后的故障仍待定位).

### Start-hold diagnostic control

Append `--automation-start-hold-control --close-diagnostics` instead of the idle flag.
The two control flags are mutually exclusive and require the same reviewed artifacts,
install/helper authorization and concrete owners as the normal managed entry.

The scene is idle → Start once → Current run/started → one startup snapshot → two
bounded state reads separated by five quiet seconds → same-page Close → closed.
There are no navigation or scan clicks. The success page is Current run/closed, not
Evidence. Scope `formal-android-managed-start-hold-control` and CLI key
`formal_android_managed_start_hold_control` remain distinct from lifecycle navigation.
The sealed `automation/start-hold-control.json` binds the quiet window and scope;
missing/incomplete hold evidence cannot become a successful control. Cleanup and
failure diagnostics are unchanged; success still means `host_gate=pending`, `rf=not_run`.

The original APK passed this control on the physical Android phone. It does not
prove navigation, native thread/resource-zero, reload or RF; see the
[diagnostic report](../../../docs/testing/reports/2026-11-05-formal-android-preclose-evidence.md).

The Android App explicitly selects `androidx.collection:collection:1.4.4` to avoid
the RN 0.87.1 Fabric `MutableIntObjectMap` corruption described in
[upstream fix #58389](https://github.com/react/react-native/pull/58389).
The old release actually resolved 1.4.2. This is an App-scoped dependency correction,
not a forced resolution in the BLE library or a claimed fix for the transport stall:
the 1.4.4 candidate still failed after navigating back to Current run.

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

### Concrete App identity checks and existing-WDA attach

`pin_android_app()` and `pin_ios_app()` in the same host module now create fixed, callable identity
bindings for `require_app_identity`. They verify phone/run and driver/session/device/App configuration,
share the host's absolute deadline and reject rebinding; checks do not select a replacement process.

- Android uses a unique PID, exact UID/cmdline, `/proc` start ticks, OS boot UUID and the installed
  single `base.apk` SHA256. Procfs denial, split APKs, hash mismatch or PID reuse fail closed with no
  root/install/fallback. Every call performs fresh reads; actual device support and cost are unverified.
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

`tools.apple.ios_appium.create_ios_attached_driver()` uses an explicit loopback running-WDA URL
and bounded HTTP timeout, with no build/signing/deployment configuration or fallback. It disables
App launch/reset/quit-termination. Preparing/owning WDA and its forwarding is separate; a URL is
not ownership proof. Existing build/deploy gates continue using their explicit older helper.

**The concrete live host is still pending:** it must select the physical phone, supply managed
candidate provenance, prepare WDA/UiAutomator2 and the bound driver, and own bounded failure
cleanup/finalization plus the final evidence bundle. Android emulator-serial rejection does not
replace physical-device selection. Neither new identity sampling nor macOS XCTest proves mobile
runtime, RF or cleanup. See [app identity evidence](../../../docs/testing/reports/2026-11-04-formal-host-app-identity.md).
Target-App no-install does not mean unrelated WDA preparation never builds/deploys. Neither function acquires/releases
leases, opens serial transport, starts/stops fixtures or creates Appium drivers. A successful
`exercise_owned_scan()` deliberately leaves these resources with the host. Initial powered-on/
authorization observations are not live permission proof; CLI remains lifecycle-only.

Connect/GATT/I/O/notification and reload workflows remain outstanding. Existing native UI tests
and legacy RF evidence are not deleted based on synthetic Appium tests.

The [approved scan design](../../../docs/research/react-native/react-native-bleng-scan-contract.md)
is now implemented in formal specs/exports, Rust product projection, both native bridges and
this App's manual workflow. Both native compile graphs and software regressions have current
[candidate evidence](../../../docs/testing/reports/2026-11-04-formal-rn-scan.md); mobile native tests,
Hermes/Appium and RF are still separate unverified gates. The isolated pinned-Nitro codegen
experiment remains historical evidence, not a replacement implementation or runtime proof.
