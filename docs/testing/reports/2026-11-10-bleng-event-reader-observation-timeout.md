# BLeng single-reader observation timeout — 2026-11-10

## Scope and verdict

The Android and Apple formal RN platform-session readers now treat a typed timeout from
`nextEvent` as expiration of one observation window, not end-of-stream. Android's six new JVM
regressions and Apple's six new Simulator regressions passed. The full Apple native run **failed**
(25 passed, 1 failed); its separate resource-vocabulary/close failure remains open. This report
is not a full native, Hermes reload, Appium, RF, release or ABI gate pass.

This follows the [owned-worker retirement slice](2026-11-10-android-owned-worker-retirement.md),
without claiming either defect caused the HANS-OFF UiAutomator2/ADB stall. Maestro was not used.

## Verified defect and bounded correction

BLeng's bounded event read reports an observation deadline as typed `Timeout`. The previous
platform loops placed `owner.nextEvent(300_000)` inside the outer reader-task error handler;
a normal idle timeout therefore ended the sole event reader. Apple also closed its recovery
inbox on that task's exit. This is a source/test finding, not a captured five-minute device idle
failure or proof of the automation stall's root cause.

The correction retains one reader and the existing 300,000 ms read window:

- Catch only typed `BlengException.Timeout` / `BlengError.Timeout` thrown by the read itself.
- Check coroutine/task cancellation before every read, including after an observation timeout.
- Continue after one or several observation timeouts without replaying delivered events.
- Preserve EOF as stream termination; cancellation and non-timeout read failures propagate.
- Keep delivery outside that catch: even a typed Timeout thrown by a delivery callback is terminal.
- Keep Android projection destruction and Apple's recovery-inbox close in their existing task
  finalizers. No additional runtime, queue, timer, command retry or public JS/FFI API was added.

Implementation and assertions:

- [Android reader](../../../integrations/react-native-bleng/android/src/main/kotlin/com/blehub/reactnative/bleng/BlengEventReader.kt)
  and [platform session](../../../integrations/react-native-bleng/android/src/main/kotlin/com/blehub/reactnative/bleng/AndroidPlatformSession.kt).
- [Android reader tests](../../../integrations/react-native-bleng/android/src/test/kotlin/com/blehub/reactnative/bleng/BlengEventReaderTest.kt).
- [Apple platform reader](../../../integrations/react-native-bleng/ios/ApplePlatformSession.swift)
  and [formal native tests](../../../tests/consumers/react-native-bleng/ios/BleHubBlengHostNativeTests/BleHubBlengHostNativeTests.swift).

## Android red → green

The initial run without timeout continuation reported 15 tests, 2 failures. The failed cases were
`observationTimeoutsDoNotEndTheReaderOrDuplicateDeliveredEvents` and
`cancellationAtAnObservationTimeoutStopsBeforeTheNextRead`. The original red log, XML and baseline
source remain under `.work/bleng-event-reader-timeout-2026-11-10/` and were not rewritten.

After the correction:

```sh
pixi run just react-native bleng-android-unit-test
```

Passed: `BlengEventReaderTest` 6/6 and `OwnedBackendThreadTest` 9/9, zero errors/skips/failures.
Green XML and log are preserved separately in that evidence directory. The first attempt to copy
these results used the default Gradle build path and failed locally before copying; the corrected
capture uses the package's configured `.work/react-native-bleng-android/` build root. This was not
a test or device failure.

## Apple build and runtime attempts

```sh
pixi run just react-native formal-host-ios-build
```

Passed: isolated Swift platform contract and formal App/native-test `build-for-testing`. This is
a source/local-package build, not clean artifact-only packaging or runtime evidence.

```sh
pixi run just react-native formal-host-ios-native-test 633606C2-89B2-4707-9CF7-2FEF326D7100
```

Failed in its repeated build prerequisite: `pod install --no-repo-update` exceeded the owner's
180-second limit. No tests began in this attempt; no timeout/dependency change was made.
The failure and Pod log are preserved separately from the prior successful native build log.

Using the already built test products and the same maintained runtime recipe:

```sh
pixi run just --no-deps react-native formal-host-ios-native-test 633606C2-89B2-4707-9CF7-2FEF326D7100
```

Runtime: iPhone 17 Pro Simulator, iOS 26.5, arm64. Result: **26 tests, 25 passed, 1 failed, 0 skipped**.
All six `testDrainBlengEvents*` cases passed, covering timeouts followed by events/EOF, no read past
EOF, transport/worker failure, cancellation, delivery Timeout, and task cancellation at a timeout.
These use scripted reads rather than waiting five minutes or issuing RF commands.

The failing `testFormalRustSupervisorOwnerStartsAndClosesWithoutProbeSurface` reported:

1. Resource-name equality mismatch: actual snapshot additionally contains `peerCacheEntries`;
   the platform session's `requiredZeroResourceNames` does not.
2. The same test node also records `Platform(detail: "supervisor has no active session")`.
   The test closes explicitly and also in teardown; this close behavior needs separate investigation.

No resource assertion was relaxed, no failure was excluded, and this suite remains failed.
The original `.xcresult`, runtime log, summary and test tree are preserved under
`.work/bleng-event-reader-timeout-2026-11-10/`. The six passing reader cases do not upgrade the
other test or prove complete Supervisor resource retirement.

## Other validation

| Exact command | Observed result |
|---|---|
| `pixi run just react-native formal-host-android-build debug` | App and AndroidTest APK compile passed; no installation |
| `pixi run just react-native formal-host-test` | 57 passed, 2 suites |
| `pixi run just test supervisor` | 124 passed, 6 suites |
| `pixi run just test bleng` | 312 passed, 3 suites |

Final checks: `pixi run just fmt`, `pixi run just lint`, `pixi run just docs-check-mermaid`
(27 diagrams in 235 Markdown files) and `git --no-pager diff --check` passed. Git emitted
existing CRLF-normalization warnings, not whitespace errors. The authored Markdown also passed
UTF-8, trailing-whitespace, conflict-marker, fence-balance and local-link checks after correcting
a quoting error in the first local verifier invocation.

## Limits and next work

No physical phone, shared ADB daemon, DUT permission, radio or fixture firmware was changed in
this slice. Final read-only `simctl list devices booted` showed no booted Simulator, matching the
initial shutdown state; no shutdown or foreign-process cleanup was needed.
Simulator tests do not count as phone/native RF evidence. Neither deterministic timeout
coverage nor the earlier Android worker test proves actual Hermes invalidation, old-engine reader
retirement, pending scan cleanup or same-candidate scan/reload convergence.

Next: resolve the Apple resource-vocabulary and repeated-close findings under their owning
lifecycle contracts, then validate formal scan/engine retirement and actual Hermes reload using
the existing owners and host evidence chain. Appium remains the default UI driver; Maestro remains
a small explicit opt-in comparison, not a second BLE stack or release gate.
