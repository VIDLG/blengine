# Apple same-process RCTHost engine reload — 2026-11-11

## Scoped verdict

The formal native target now has a passing **same-process, same-RCTHost two-engine reload**
through actual bundled package JS, real RN/Hermes runtime lifecycle and scripted native BLE
backends. The final suite passed **44/44 tests**, zero failures/skips, on the arm64 iPhone 17 Pro
Simulator, iOS 26.5. This extends, but does not rewrite, the earlier
[runtime/module binding evidence](2026-11-10-apple-engine-runtime-binding.md).

**This is not a rendered App/UI, mobile RF, artifact-only package or release gate.** The generic
AppDelegate has not yet been changed to attach predecessor relationships. The native consumer
harness owns this relationship and creates no surface. No phone, ADB daemon, DUT permission,
radio or firmware was changed. Android engine migration and legacy pending-RF migration remain
separate work.

## Native startup admission

The existing [engine lifetime](../../../integrations/react-native-bleng/ios/HybridBlengSessionOwner.swift)
now accepts one immutable, exact native predecessor before binding any new owner:

- A successor startup task observes the **future actual RN invalidation and completed native
  retirement**, not a snapshot that might return nil before the callback.
- New `start()` waits outside the lifecycle actor's accepted-call section. No new native owner
  or backend is constructed while predecessor cleanup is pending.
- After permission, the original start ticket is revalidated. A close/invalidation during that
  wait cannot publish a new live receipt.
- Deadlines are bounded to 1–30 seconds; this harness uses 10 seconds. Timeout/cancellation removes
  the observer and does not cancel old cleanup, manufacture invalidation or replay commands.
  Completion observers are bounded to eight per lifetime.
- Success requires old native `ownerClosed` and no cleanup failure. A prior startup failure is
  retained in the transition record, even if an intermediate engine never created a backend.
- Linking is one-shot; cycles, reconfiguration and multiple successors claiming the same old
  lifetime are refused. The static linking mutex has no current-engine or owner registry.
- Native-only per-lifetime factory injection configures the actual package-created object before
  first start. It reuses the existing scripted backend, not a global replacement Nitro constructor
  or second native image. Generic unconfigured production startup behavior is unchanged.

Three new deterministic tests cover immutable/cycle-safe linking, missing-callback timeout with
failure propagation through an idle intermediate engine, and invalidation while waiting without
waiting for the predecessor callback. Existing admission, cleanup-failure, reader and resource
regressions remain enabled.

## Real host and actual JS execution

The consumer-owned [BlengHostReloadHarness](../../../tests/consumers/react-native-bleng/ios/BleHubBlengHostNativeTests/BlengHostReloadHarness.mm)
uses `RCTReactNativeFactory` and its root-view factory to start a real `RCTHost` without a surface.
It preserves RN's normal builtin providers and links the existing `ReactAppDependencyProvider`
archive. It targets that same host's reload method, never a global reload broadcast.

The [local JS entry](../../../tests/consumers/react-native-bleng/ios/BleHubBlengHostNativeTests/BlengHostReload.js)
constructs via the actual `createBlengSessionOwner()`, retains the owner in its VM, calls the actual
`owner.start()`, and reports real Promise settlement through a consumer-only evidence module.
The host supplies the real CallInvoker; the previous no-op-invoker manager harness is not used as
a substitute for this proof.

The native module-creation hook captures each exact lifetime and configures generation two's
predecessor before returning its module to RN. Evidence is attributed natively to the owning
host/run/generation, has a 32-record limit, and rejects duplicate or out-of-order transitions.
JS supplies no trusted run ID, PID or generation.

`stop` releases the factory, root-view-factory host reference and host reference. RN's own teardown
then invokes module invalidation; tests separately await native retirement with deadlines. No
manual call to the production lifetime invalidation method stands in for the old RN callback.

## Positive and cleanup-failure assertions

Both tests require generation one's real JS startup to resolve before requesting reload. During
the old post-reader native snapshot hold they verify:

- The exact host object is unchanged and two distinct lifetime modules exist.
- Generation two has submitted its real JS `start()` but has not settled.
- New native owner creation and backend initialization counts are **zero**.

After releasing old cleanup:

| Case | Old native outcome | Generation two |
|---|---|---|
| Successful retirement | Closed, no failure, exactly one disposal | Constructs/initializes once and JS startup resolves |
| Scripted native leak | Closed with `native_resources_not_zero`, exactly one disposal | Creates/initializes nothing and JS startup rejects |

The leak is a deliberate backend diagnostic input, not an observed physical leak. Each test
also checks the actual old native receipt. No resource assertion, close contract or timeout was
weakened to obtain a pass.

Green native event attachments, both from PID **69528**:

| Case | Native run ID | Final event |
|---|---|---|
| Successful retirement | `B3E180FB-809E-4D05-84AC-5CFC9B46E6D0` | generation 2 `start-resolved` |
| Preserved cleanup failure | `1B7A1553-AC79-40DF-A1C1-E57063CC44E7` | generation 2 `start-rejected` |

Each has eight contiguous events: host-started → lifetime-configured → start-submitted →
start-resolved for generation one, then the same first three stages and the appropriate settlement
for generation two. There is no `native-failed` event. The native held-barrier/resource assertions
are in XCTest; the small event attachment is not an independent RF or release-bundle verifier.

The action output also shows four actual bundle evaluations, four Hermes/Nitro Dispatcher installs,
and four JSICache destructions across these two cases. No JS runtime errors or module invalidation
timeouts were observed in the green window. RN priority-inversion warnings and Debug localhost
packager connection refusals remain: no Metro server was started, but this is **not network-zero
Debug evidence** or proof of every RN/OS thread exiting.

## Build and first-run failures, preserved separately

The normal command:

```sh
ENTERPRISE_REPOSITORY=https://repo.reactnative.dev/maven2 pixi run just react-native formal-host-ios-build
```

**Failed** again during the 180-second `pod install` Core artifact probe, before bundle/native
compilation. The prior per-command base URL was not a permanent network fix. No same-condition
Pod rerun, third-party patch, dependency upgrade or deadline expansion was used here.

Installed Podfile.lock/Manifest bytes and the installed formal Podspec were then checked against
the current sources and matched. Package source/dependency file sets had not changed; the native
consumer project/resource changes do not require a new Pod graph. Local RN codegen restored the
derived sources removed before the hung probe. The existing owner's exact finite local RN bundle
step and workspace `build-for-testing` step were executed as an explicit installed-graph
continuation. This does not upgrade the normal build command's failed verdict.

Initial native compilation found a missing search path for the existing provider archive. The
native target's Debug/Release `LIBRARY_SEARCH_PATHS` now includes its actual product directory;
there is still one provider implementation/archive link.

First runtime: **42 passed, 2 failed**. Both real-host cases stopped before startup because the
test-only evidence module returned a bare ObjCTurboModule with no `report` method-map entry.
Console evidence showed `TypeError: undefined is not a function`, before `owner.start()`.
A small proper wrapper now registers arity two and delegates to the same native report method/
queue. The entry also explicitly checks method availability. Production startup gates were not
changed in response to this failure. Original failed results and console/event extraction remain.

After that correction, the same full native target passed **44/44**.

## Validation commands and evidence

```sh
pixi run just --no-deps react-native formal-host-ios-native-test 633606C2-89B2-4707-9CF7-2FEF326D7100
pixi run just react-native tools-test bleng_ios_consumer
pixi run just react-native tools-test recipe_wiring
pixi run just react-native formal-host-test
pixi run just test supervisor
```

Results: native **44 passed**; build-owner software **12 passed**; recipe wiring **15 passed**;
Jest **68 passed**; Rust Supervisor **124 passed**. The normal build's prerequisite typecheck,
autolinking and isolated Swift platform contract passed before the Pod timeout. Local bundling
runs finite Metro `runBuild`, not a server. The build owner now admits its local inputs, rejects
missing/empty output and verifies the copied XCTest resource's SHA-256, with synthetic failure/
timeout/stale-output/hash-mismatch coverage.

The successful local native compile command was:

```sh
pixi run just --command pixi run python tools/react_native/run_quiet.py --no-tty --progress-seconds 20 .work/apple-rcthost-reload-2026-11-10/native-build-evidence-fixed.log -- pixi run -e apple-ci xcodebuild -quiet -workspace tests/consumers/react-native-bleng/ios/BleHubBlengHost.xcworkspace -scheme BleHubBlengHost -derivedDataPath tests/consumers/react-native-bleng/ios/build/simulator-derived -configuration Debug -sdk iphonesimulator -destination 'generic/platform=iOS Simulator' CODE_SIGNING_ALLOWED=NO CODE_SIGNING_REQUIRED=NO -only-testing:BleHubBlengHostNativeTests build-for-testing
```

The generated and copied 2,007,851-byte JS bundle match, SHA-256:

```text
ec68c7db96c120ceafb8474adf2b1508d3ac065c4918e2bcc3a76b085dbf2d49
```

Evidence root: `.work/apple-rcthost-reload-2026-11-10/` (attempt began before this report date).
It retains the Pod timeout, failed archive-link compile, original 42/2 `.xcresult` and extracted
JS errors, green `.xcresult`/summaries, successful logs and `passed-diagnostic-extraction` containing
the two native run traces. `source-sha256.json` records the final native/JS/build-owner sources.
Final checks passed: `pixi run just fmt`, `pixi run just lint`,
`pixi run just docs-check-mermaid` (27 diagrams in 239 Markdown files),
`git --no-pager diff --check`, and authored Markdown UTF-8, whitespace, conflict, fence and local-link
checks. The 12 build-owner and 15 wiring tests were rerun after formatting and passed. Existing
Git CRLF-normalization warnings were not whitespace errors. Final read-only simulator inventory
showed no booted simulator; no foreign or physical-device cleanup was performed.

## Remaining boundary

The generic formal AppDelegate still has no predecessor coordinator; rendered App remount,
phone execution, RF, clean artifact provenance and the final release-shaped candidate remain
unproven. Reuse this native mechanism and existing App/host owners for that integration, rather
than copying the legacy pending-RF API, introducing a global current engine or treating Appium/
Maestro page success as native retirement proof.
