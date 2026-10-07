# React Native Android single-DK scan and dual-route Notification boundary

**Date:** 2026-10-04
**Scope:** private React Native/Nitro Android production backend and two nRF52840 DK fixtures
**Status:** static callback guards passed; dual-DK fixture readiness passed; current Android RN dual-DK Notification, strict Indication and destructive fault/recovery gates passed on OnePlus 11; Android RN JS runtime reload/invalidation passed; iOS dual Notification and dual Indication passed; an earlier Android second-concurrent-route failure remains retained as historical evidence

## Changes validated

- Added an explicit profile-aware single-DK runner:
  - `host-android-rf-single <android-device> <fixture-device> <service-uuid> [mode]`;
  - fixture name, CDC port, J-Link identity and profile are loaded as one local mapping;
  - the requested service UUID must match the selected profile;
  - single-DK controller use requires an explicit `allow_single_device` path and cannot weaken the two-DK gate.
- Added Android production callback route seam `resolveCharacteristicNotificationRoute`.
  It retains the existing requirements that a callback must match the active route, the exact current `BluetoothGatt` object, and the exact native characteristic object.
- Added JVM tests for:
  - interleaved A/B notification route resolution;
  - stale route, GATT identity mismatch and characteristic identity mismatch rejection.
- Added an Android multi-DK notification diagnostic matrix:
  - `both` (default);
  - `only-dk-a`;
  - `only-dk-b`;
  - `reverse` enable order.
- Hardened the shared RN Android RF controllers with a bounded `pidof` liveness check after launch. Single-DK, dual-DK and dual-DK fault runners now fail immediately when the RN host exits before its pass marker instead of waiting for the full RF timeout.
- Changed Android service filtering to local post-callback filtering rather than relying on vendor controller filter offload. Empty requested service filters retain the existing “accept all” behavior. The local matcher has focused Kotlin tests.

## Static validation

Passed:

```text
Python fixture/Android RF controller and multi-ledger tests: 25/25
RN Android RF controller regression tests: 20/20
Ruff focused check
Android library JVM unit tests: :library:test
Android backend publish to the local fixture Maven repository
RN TypeScript/Nitrogen generation and Android CLI autolinking
RN Android release host build and root-aware adb push + pm install
```

The Android library test run completed successfully with both debug and release unit-test variants. The Android SDK emitted its existing SDK XML compatibility warning; it did not affect the build or test result.

## Hardware evidence

The OnePlus 11 is `3ac5cc09`. Both DKs passed the bounded readiness gate:

```text
nrf52840_multi_readiness=passed devices=2
```

The DK-B profile is:

```text
service=fd200000-0000-4000-8000-000000000000
local_name=BleHubNrf52840B
profile=blehub-nrf-indication-v2-b.json
```

The same BTP/AutoPTS-compatible firmware image is used by DK-A and DK-B. No second firmware image is required for the profile distinction.

## DK-B single-device result

The profile-aware runner successfully:

- loaded the explicit `dk_b` mapping;
- started the DK-B fixture and reached `NRF52840_DIRECT_READY`;
- built and installed the RN production host APK;
- initialized the production Android backend through RN/Nitro/Rust;
- started the Android scan.

It then failed at the bounded advertisement step:

```text
matching advertisement timed out after 44987ms
```

The retained OnePlus log recorded approximately 3,670 native scan results for the scanner registration, but no BleHub `AdvertisementObserved` matching the DK-B service UUID. This is not classified as a DK-B firmware/readiness failure: the fixture was ready, and the same DK-B is discovered in the two-DK Android flow.

## Historical two-DK Android result

An earlier candidate and controller run retained the following boundary evidence:

- `only-dk-a` and `only-dk-b` passed;
- `both` delivered the first Notification value but timed out on the second;
- `reverse` reproduced the timeout on whichever route was enabled second.

That historical result was consistent with a OnePlus controller/native delivery boundary rather than DK-B identity, UUID selection, Rust correlation or JS listener cross-talk. It remains retained for provenance and is not silently rewritten.

## Current candidate two-DK Android result

After the RN host rebuild and the current fixture/controller rerun on OnePlus 11 `3ac5cc09`, the same `both` route passed for both configured profiles:

- both advertisements and independent peripheral identities;
- both connections and GATT sessions;
- interleaved read/write/readback;
- both Notification CCCD enables and exact `EF` values;
- both Notification disables with stale-route silence;
- both disconnects and RN probe close.

The strict `both + indication` rerun also passed the equivalent two-route flow with `actualMode=Indication`, exact values, disable silence and cleanup. The current candidate therefore supersedes the earlier boundary for the tested hardware/session, while the historical failure remains a non-current diagnostic record.

## Interpretation

The current evidence separates these facts:

1. **Both DK profiles are usable:** both reach readiness and work through the RN/Nitro/Rust Android path.
2. **Current Android dual-DK Notification passes:** both concurrent routes completed on OnePlus 11 `3ac5cc09`.
3. **Current Android dual-DK strict Indication passes:** both pure Indication routes completed with exact mode/value and stale-route silence.
4. **The earlier second-route failure remains historical:** it must not be used to block the current candidate, but it also must not be deleted from the evidence history.

No UUID workaround or shared contract change was made.

## Dual-DK Indication follow-up (2026-10-04)

The multi-DK host and shared RN flow were parameterized with `subscription_mode=notification|indication`; the fixture selects the `updates` characteristic for Notification and the pure `indications` characteristic for strict Indication. Both DKs continue to use the same BTP/AutoPTS-compatible firmware image; only the host-owned profile, service UUID/local name, CDC port, J-Link identity and BTP session differ.

The coordination controller was corrected to match a subscription marker and its service UUID on the same log line. The previous cumulative-log matcher could trigger the wrong DK when an earlier advertisement contained the other service UUID. Fixture teardown now best-effort resets the host-owned dynamic GATT server after peer disconnect/advertising stop, preventing a pending ATT Indication from poisoning the next readiness/provision attempt. Two consecutive post-recovery `nrf52840_multi_readiness=passed devices=2` runs passed.

Android evidence on OnePlus 11 `3ac5cc09`:

- `only-dk-b` strict Indication passed through production Android backend → Nitro → RN → Rust: both DKs connected and completed GATT/I/O, DK-B enabled pure Indication, received exact `EF`, disabled with 2-second stale-route silence, disconnected and closed.
- The current `both` strict Indication rerun passed both routes through the production Android backend → Nitro → RN → Rust path, including exact `EF`, actual Indication mode, disable silence, disconnect and close.

The iPhone 14 Plus `00008110-000C61160C39401E` passed the full `both` strict Indication flow with both DKs: independent advertisement/connection/GATT identities, interleaved I/O, both `actualMode=Indication` enables, exact `EF` values, both disables with stale-route silence, both disconnects, listener removal and close. This is an iOS private low-level RN/Nitro pass, not public BLeng/RN API evidence.

The earlier Android result remains recorded as historical controller-boundary evidence. The current candidate has now passed both concurrent Notification and strict Indication routes without changing the shared contract or applying a UUID workaround.

## Android RN dual-DK destructive recovery rerun (2026-10-04)

The production RN Android host was rebuilt, installed through the root-aware ADB path, and run against both configured nRF52840 DK profiles on OnePlus 11 `3ac5cc09`. The bounded fault runner completed the full destructive sequence:

1. both DK connections and GATT routes became ready;
2. only DK-A was stopped;
3. DK-B remained usable while DK-A was absent;
4. DK-A was restarted by the same multi-fixture controller;
5. DK-A reconnected with a fresh connection/GATT route and the runtime emitted the pass marker;
6. the RN host and both fixture process groups were cleaned up.

The result was:

```text
android-rf-multi-fault:passed
```

This is a new Android RN multi-device fault-isolation/recovery pass. Together with the current Notification and strict Indication reruns, it completes the current Android RN two-DK matrix for the tested OnePlus/DK session.

## Android RN lifecycle and RSSI follow-up (2026-10-04)

The Android RN host lifecycle stress gate passed on OnePlus 11 `3ac5cc09`:

```text
stress:complete:passed
```

This covered repeated close/recreate, listener removal, late-callback suppression and post-close dispatch behavior in the installed release host without RF fixture involvement.

The profile-aware single-DK RSSI gate then ran explicitly against `dk_a` and service `fd000000-0000-4000-8000-000000000000`. The production Android backend → Nitro → RN → Rust path passed scan, connect, GATT route, RSSI callback/terminal, disconnect and post-disconnect silence:

```text
rf-rssi:passed ... rssiDbm=-57
```

This is single-DK RSSI evidence. The shared multi-DK flow was then extended with per-connection RSSI commands and strict ledger checking. The Android RN dual-DK Notification rerun passed two RSSI events while both connections remained active, with independent command/connection/peripheral correlation and bounded dBm values. This is the current Android multi-device RSSI pass.

The corresponding iOS multi-DK RSSI attempt did not enter RF because `devicectl` reported the iPhone unavailable before launch. It is not classified as an RF failure and remains pending until the device is available again.

The available iPhone 14 Plus later completed the current dual-DK Notification and strict Indication flows with the RSSI step included before subscription enable. The observed RSSI values were independently correlated to the two connection/peripheral identities (`DK-A=-69 dBm`, `DK-B=-61 dBm`); both modes then completed exact `EF` value delivery, disable ordering, stale-route silence, disconnect and close. These are current private RN/Nitro RF passes, not public BLeng/RN API evidence.

## iOS RN dual-DK destructive recovery follow-up (2026-10-04)

Using the existing signed RN host and the available iPhone 14 Plus `00008110-000C61160C39401E`, the same two-DK destructive recovery flow passed:

```text
rf-multi-fault:passed
passiveFault=dk_a
bSurvived=true
aReconnected=true
connectionCount=3
gattSessionCount=3
```

The iOS production path therefore also demonstrated DK-B survival while DK-A was stopped, followed by fresh DK-A connection/GATT recovery, listener removal and probe close. This is private RN/Nitro integration evidence, not public BLeng/RN API evidence.

## iOS RN active RF reload follow-up (2026-10-04)

After the iPhone was unlocked, the maintained `host-ios-device-rf-reload` gate passed against the current signed RN host and the standalone nRF52840 fixture:

```text
React Native Nitro runtime gate passed
nrf52840_fixture=stopped
```

The gate validated two complete generations with one production RN/Nitro probe flow per generation: initialization, scan, connect, GATT discovery, Notification enable and exact `EF` value. Generation 0 reached an active subscription, the native reload trigger caused the expected host-disconnect coordination marker, and generation 1 rebuilt the route with fresh runtime evidence. The checker also validated generation ordering, fixture-host disconnect ordering, correlation fields and cleanup without failed markers. This is an iOS private RN/Nitro active-reload pass. Android now has an independent non-RF JS runtime reload/invalidation pass documented below; the two results are not substituted for one another.

## Cross-platform RN multi-DK repeated-cycle follow-up (2026-10-04)

The shared `rf-multi` JavaScript flow was exercised through the new bounded repeated-cycle controller on both platforms. Each cycle created a fresh RN probe and independent two-DK fixture lifecycle; the controller retained a separate cycle log and stopped on the first failure.

Android OnePlus 11 `3ac5cc09`:

```text
android-rf-multi:cycle=1/2:passed
android-rf-multi:cycle=2/2:passed
```

Both cycles completed dual scan, independent connections/GATT discovery, interleaved I/O, per-connection RSSI, Notification value/disable silence, disconnect and probe close. Both DK profiles reached readiness on independent BTP sessions in both cycles.

iOS iPhone 14 Plus `00008110-000C61160C39401E`:

```text
ios-rf-multi:cycle=1/2:passed
ios-rf-multi:cycle=2/2:passed
```

Both cycles completed the same dual-device ledger. In the second cycle CoreBluetooth assigned the two discovered peripherals in the opposite numeric order; the service UUID, connection, GATT session, characteristic, subscription and RSSI correlations remained correct, demonstrating that the flow does not depend on discovery order. The two cycles are repeated-cycle pressure evidence, not a single uninterrupted long-lived JS-process soak.

## Five-cycle multi-DK pressure rerun (2026-10-04)

The same signed/build candidate then completed five fresh-probe cycles on each platform without changing the shared contract:

```text
Android: android-rf-multi:cycle=1/5..5/5:passed
iOS:     ios-rf-multi:cycle=1/5..5/5:passed
```

All ten cycles reached dual-DK readiness, completed the shared scan/connection/GATT/interleaved-I/O/RSSI/Notification ledger, disconnected both routes, closed the RN probe and stopped both fixture process groups. The per-cycle logs remain under the Android and iOS RN host build directories. This is stronger repeated-cycle pressure evidence, but it still does not claim one uninterrupted long-lived JS process or a multi-hour soak.

## Strict Indication repeated-cycle follow-up (2026-10-04)

The same bounded controller then ran three cycles per platform with `subscription-mode=indication`:

```text
Android: android-rf-multi:cycle=1/3..3/3:passed
iOS:     ios-rf-multi:cycle=1/3..3/3:passed
```

All six cycles selected the pure Indication characteristic, observed `actualMode=Indication`, received exact `EF`, verified disable ordering and two-second stale-route silence, disconnected both devices and closed the RN probe. This extends the repeated-cycle evidence to both subscription modes; it is still not a continuous long-process soak.

## Continuous multi-device soak follow-up (2026-10-04)

The new `rf-multi-soak` mode keeps one RN process, one Nitro probe, one production backend instance and one two-DK fixture lifecycle alive across full multi-device ledgers. Each cycle uses unique command/connection/session IDs; the fixture coordination uses `cycle-N` directories, resets the read-write value to `00`, waits for the real GAP disconnect, then restarts advertising before the next scan.

Android OnePlus 11 `3ac5cc09` first completed three continuous cycles with both DKs and Notification mode. The first implementation attempt correctly failed fast in cycle 2 because the fixture had not restarted advertising and the scan saw no target advertisement. That was not relabeled as an Android RF failure: the fixture-owned soak coordination was corrected, and the rerun passed all three cycles with the existing ledger checker and final fixture cleanup. A later ten-cycle Notification run initially reached the runtime pass marker but its polling `adb logcat -d` snapshot retained only four inner ledgers because the device's 256 KiB main ring buffer discarded earlier lines. The runtime had in fact emitted cycles 1 through 10; the checker correctly rejected the incomplete evidence. The Android multi-DK runner was then changed to start a dedicated filtered `ReactNativeJS` logcat stream after clearing the buffer and retain the complete bounded runtime ledger. The rerun passed all ten cycles:

```text
Android: rf-multi-soak:cycle=1/3..3/3:passed
Android: rf-multi-soak:passed:cycles=3
Android: rf-multi-soak:cycle=1/10..10/10:passed
Android: rf-multi-soak:passed:cycles=10:routeMode=both:subscriptionMode=notification
```

This is same-process multi-device Notification pressure evidence, not a multi-hour soak. Strict Indication continuous soak was run separately and is recorded below; longer-duration or multi-hour runs remain a separate gate.

An earlier iOS 14 Plus attempt built and installed the signed host and started both DK fixtures, but `devicectl` returned `SBMainWorkspace Locked` before app launch. The runner classified this as an iOS device precondition failure and cleaned up the fixture; it is not an iOS RF pass or fail. The runner now queries `devicectl device info lockState --json-output -` before fixture startup and rejects a paired-but-locked device immediately.

## Continuous multi-device soak completion (2026-10-04)

After the iPhone was kept unlocked, the same signed candidate completed the continuous strict-subscription matrix on both mobile RN hosts. Each gate kept one RN process, one Nitro probe, one production backend instance and both DK fixture processes alive across all inner ledgers; it did not recreate the JS probe between cycles. Notification used a bounded ten-cycle run; strict Indication used a bounded three-cycle run.

```text
Android OnePlus 11 3ac5cc09:
  rf-multi-soak:cycle=1/10..10/10:passed (Notification)
  rf-multi-soak:passed:cycles=10:routeMode=both:subscriptionMode=notification
  rf-multi-soak:cycle=1/3..3/3:passed (strict Indication)
  rf-multi-soak:passed:cycles=3:routeMode=both:subscriptionMode=indication

iOS iPhone 14 Plus 00008110-000C61160C39401E:
  rf-multi-soak:cycle=1/10..10/10:passed (Notification)
  rf-multi-soak:passed:cycles=10:routeMode=both:subscriptionMode=notification
  rf-multi-soak:cycle=1/3..3/3:passed (strict Indication)
  rf-multi-soak:passed:cycles=3:routeMode=both:subscriptionMode=indication
```

The iOS Notification and strict Indication runs both observed `PoweredOn`/`Authorized`, two independently correlated advertisements, connections, GATT databases, interleaved read/write/readback and RSSI results, two subscription routes, exact `EF` values, disable-before-silence ordering, both disconnects, listener removal and final probe close. Strict Indication recorded `actualMode=Indication` for both DKs. Android strict Indication recorded the same two-route evidence; Android Notification continuous soak used the dedicated streaming capture and retained all ten ledgers. All four platform/mode logs passed the existing cycle ledger checker and both fixture process groups were stopped during cleanup.

This closes the current private RN/Nitro two-DK continuous matrix for the tested OnePlus/iPhone/DK session: ten same-process Notification cycles and three same-process strict Indication cycles on each mobile platform, with complete ledgers and cleanup. It is still bounded pressure evidence, not a multi-hour soak, and it does not prove the future public BLeng RN package or ABI freeze.

## Next gate

Use bounded, explicitly named experiments without changing the shared BleHub contract:

1. retain the DK-B single-device advertisement timeout as an explicit vendor/topology diagnostic;
2. retain the earlier second-route failure as historical evidence, but use the current candidate's dual Notification and strict Indication passes as the active result;
3. retain the deterministic callback seam tests and fail-fast runner tests as production route/control guards;
4. retain the completed Android/iOS dual-DK RSSI, Android twenty-cycle Notification, iOS twenty-cycle Notification and iOS five-cycle strict Indication bounded evidence; proceed only with any explicitly requested longer-duration soak and pending-Promise-during-backend-dispose gate without changing the shared BleHub contract. The Android close-before-initialize Promise, bounded handoff overflow/close gates, and non-RF JS runtime reload/invalidation are now covered by the host recipes.

## Cross-platform backend lifecycle audit (2026-10-04)

The Apple backend audit found no production correction for the optional `Disconnecting` event: CoreBluetooth cancellation is nonblocking, so Apple reports the reliable logical terminal from `didDisconnectPeripheral`. Apple GATT/RSSI retirement, native-object correlation, rediscovery unsubscribe ordering and dispose ordering remain covered by controller-level tests. For the specific CoreBluetooth `.resetting` state, the backend now fails closed: it drains connections, disposes scan identity state, marks the backend `faulted`, and requires the caller to dispose and assemble a fresh backend. This avoids reusing retained `CBPeripheral` objects when old callbacks carry no operation token. Ordinary powered-off and authorization transitions remain recoverable in the current backend. The deterministic lifecycle suite now also asserts that a reset-faulted backend cannot be re-initialized and must be replaced. A physical adapter-reset RF run is still not claimed.

The Android backend lifecycle hardening now:

- rejects new characteristic, notification and RSSI operations after a route enters `DISCONNECTING`;
- retires pending discovery/I/O/RSSI/CCCD commands and local subscription routes before native disconnect cleanup;
- ignores duplicate `CONNECTED` callbacks and never restores `CONNECTED` after a disconnect has been accepted;
- avoids claiming Android `Cancelled` for pending connect, because `BluetoothGatt.disconnect()` is a void request and the callback does not prove causation;
- handles `BluetoothGattCallback.onServiceChanged` by retiring the old GATT state and forcing reconnect before rediscovery, avoiding same-object discovery/RSSI callback reuse;
- closes a `BluetoothGatt` returned after its route was already removed by disposal;
- keeps disposal in a retryable `CLOSING` state until owner-Handler cleanup succeeds, retaining the ingress and native connection cleanup snapshot across a rejected post or cleanup timeout;
- keeps the Android API 18 callback overloads for the project `minSdk=24`, while isolating their deprecation suppression and value snapshotting inside `AndroidGattCallback`.

The Android library clean compile and debug/release JVM tests passed after these changes. Android callback delivery and GATT database construction now live in focused source modules; Apple operation-event projection is also separated from the CoreBluetooth lifecycle owner. The current Android RN dual-DK Notification, strict Indication and destructive recovery results are real RF passes for this candidate; the earlier second-route boundary remains historical evidence.

## Android RN native event handoff follow-up (2026-10-04)

The private Android Nitro adapter no longer invokes Nitro listeners directly from the synchronous Rust/foreign event-sink callback. `AndroidEventHandoff` now transfers raw production `BleEvent` values to a dedicated `HandlerThread` with a finite capacity of 256 events. Close retires acceptance and discards queued delivery; listener selection remains outside the probe lock so Nitro re-entry cannot deadlock the probe. Capacity overflow or a failed Handler post permanently retires the handoff and enters the existing asynchronous probe-close path rather than silently continuing with a partially delivered event stream.

The deterministic tests cover:

- close before an accepted event is delivered, followed by permanent late-submission rejection;
- capacity overflow, preservation of the already accepted event, and permanent retirement.

The tests run from the RN host consumer instrumentation source set, because a library `src/androidTest` is not automatically packaged into the consuming RN application's test APK. The host recipe now builds both app/test APKs and uses `tools.android.run_instrumentation`, which performs root-aware `adb push` followed by `adb shell su -c pm install`; it does not use `adb install` or the OEM package-installer UI.

Validation for this follow-up:

```text
RN host debug + instrumentation APK build: passed
OnePlus 11 3ac5cc09 Android host instrumentation: 3/3 passed
  - AndroidEventHandoffInstrumentedTest: 2/2
  - HybridBleHubProbeCloseInstrumentedTest: 2/2 close-before-initialize and pending-dispose Promise gates
RN Android recipe android-test: passed
RN TypeScript/Nitrogen generation and Android autolinking: passed
workspace lint, format checks and focused Python tests: passed
```

This is an Android native handoff/close boundary pass. The pending-dispose test uses a private deterministic backend seam: `close()` returns a Promise that remains pending while backend `dispose()` is blocked, then settles after the gate is released; repeated `close()` returns the exact same Promise. It does not add a Nitro/JS API or claim Android RF behavior. The separate Android RN JS runtime reload/invalidation gate is recorded below.

## Android RN close during native completion callback (2026-10-04)

The Android lifecycle stress flow now includes a real RN JS/Nitro re-entry case: the completion listener calls `probe.close()` while the native completion callback is being delivered. The flow waits for that close Promise to settle, verifies a subsequent initialize dispatch returns `CLOSED`, and then continues the existing recreate/late-callback/post-close stress cycles.

OnePlus 11 `3ac5cc09` passed:

```text
stress:close-during-callback:passed
stress:recreate-close:passed
stress:late-callback:passed
stress:post-close-dispatch:passed
stress:complete:passed
```

The gate used the existing root-aware `adb push`/`pm install` path and the bounded Android runtime controller. This is real Android RN JS -> Nitro -> Kotlin -> production Android backend lifecycle evidence, but it does not claim a multi-hour soak.

## Android RN JS runtime reload/invalidation follow-up (2026-10-04)

The Android RN host now triggers React Native 0.87's public `ReactDelegate.reload()` through a `singleTask` Activity `onNewIntent` entry point. The runner treats the Activity intent-delivery warning as normal, waits for two JS generations, requires first-generation cleanup, and always force-stops the host during bounded cleanup.

OnePlus 11 `3ac5cc09` passed the complete non-RF reload gate:

```text
RN/Nitrogen/typecheck/autolinking: passed
Android release host build: passed
root-aware adb push + su pm install: passed
reload:ready:generation=1
reload:cleanup-started
reload:ready:generation=2
runtime checker: passed
```

The checker validates exactly two reload probe generations, exactly one cleanup marker, the completion/round-trip/dispatch markers for both generations, and no failure markers. The first attempt exposed and corrected a stale checker contract that still required obsolete `reload:native-triggered` and `reload:complete:passed` markers; those markers belong to an older protocol and are not emitted by the current native-triggered reload design. This pass proves Android RN JS runtime context recreation and cleanup, not RF behavior, multi-hour soak, or the future public BLeng RN package.

## Android RN 20-cycle multi-DK pressure attempt and completion (2026-10-04)

The Android OnePlus 11 two-DK Notification soak was extended from the previously passing ten-cycle bounded run to twenty cycles. The first attempt used the ordinary 180-second multi-RF budget, which was insufficient for a twenty-cycle run: cycles 1 through 18 completed, and the controller stopped the run before cycle 19 completed. The resulting fixture timeout was a cleanup consequence after the runner sent termination, not evidence of an Android GATT callback failure. This attempt is retained as historical controller-budget diagnosis, not as an RF failure.

The shared runner then changed its default budget for `rf-multi-soak` to `max(180, 30 * soak_cycles)` seconds. The dedicated filtered Android runtime capture and fixture health checks were retained. The rerun completed all twenty dual-route ledgers:

```text
Android OnePlus 11 3ac5cc09:
  rf-multi-soak:cycle=1/20..20/20:passed (Notification)
  rf-multi-soak:passed:cycles=20:routeMode=both:subscriptionMode=notification
  duration: approximately 195 seconds
```

Each cycle covered scan, both connections, GATT discovery, interleaved read/write/readback, RSSI, Notification value, disable silence and disconnect. Both fixture processes were healthy throughout and were stopped during bounded cleanup. `MultiFixtureController.check_health()` now observes owned fixture processes during the RN wait loop and immediately raises on a non-zero fixture exit with the fixture log tail.

This closes the Android twenty-cycle bounded multi-device Notification pressure gate for the tested candidate. It is not a multi-hour soak, strict-Indication twenty-cycle result, public BLeng RN package evidence, or ABI-freeze evidence.

The first corresponding iOS twenty-cycle attempt was blocked at launch because the physical iPhone 14 Plus was still locked during the device-state transition. It is not RF evidence. After the device reported `passcodeRequired=false` and `unlockedSinceBoot=true`, the rerun completed the full twenty-cycle gate:

```text
iOS iPhone 14 Plus 00008110-000C61160C39401E:
  rf-multi-soak:cycle=1/20..20/20:passed (Notification)
  rf-multi-soak:passed:cycles=20:routeMode=both:subscriptionMode=notification
  runtime duration: approximately 172 seconds
```

The iOS run retained both independent route ledgers through scan, connection, GATT discovery, interleaved read/write/readback, RSSI, Notification value, disable silence, disconnect and final probe close. This closes the iOS twenty-cycle bounded multi-device Notification pressure gate for the tested candidate.

The requested strict Indication enhancement also passed:

```text
iOS iPhone 14 Plus 00008110-000C61160C39401E:
  rf-multi-soak:cycle=1/5..5/5:passed (strict Indication)
  rf-multi-soak:passed:cycles=5:routeMode=both:subscriptionMode=indication
  runtime duration: approximately 43 seconds
```

All ten subscription routes recorded `actualMode=Indication`, exact `EF` values, disable-before-silence ordering and both disconnects. This is bounded pressure evidence rather than a multi-hour soak.

## iOS RN native adapter lifecycle parity (2026-10-05)

The iOS RN host now contains a standalone native XCTest target, `BleHubNitroHostNativeTests`, linked to the same CocoaPods Nitro/UniFFI/Apple modules as the RN app. It tests the actual `HybridBleHubProbe.swift`; the SwiftPM `BoundedEventHandoff` package and the UI target remain adjacent-layer tests, not substitutes for this gate.

The test-only adapter seam blocks native disposal with a `DispatchSemaphore` and verifies:

- `close()` returns a pending Promise while disposal has not returned;
- repeated `close()` calls return the identical Promise;
- releasing the disposal gate produces one successful settlement;
- the Promise is not rejected or settled more than once.

The seam is internal to the private adapter, is absent from the Nitro spec and public package surface, and does not change the default production path (`AppleBleBackend` + production Stable UniFFI image). The simulator gate passed through the RN Xcode workspace:

```text
RN iOS native lifecycle build-for-testing: passed
BleHubNitroHostNativeTests.testClosePromiseRemainsPendingUntilDisposeReturnsAndSettlesOnce: passed
signed device RF and dual-DK evidence: unchanged; this gate requires no device, DK or keychain password
```

The corresponding recipe is `pixi run just react-native host-ios-native-lifecycle-test <simulator-udid>`. This closes the iOS/Android private adapter pending-dispose parity gap; it does not add a public RN API or change the existing iOS/Android RF evidence.
