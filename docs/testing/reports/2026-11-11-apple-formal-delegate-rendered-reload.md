# Formal Apple delegate and rendered-subtree reload — 2026-11-11

## Verdict and scope

The formal App now uses the same host-scoped delegate as its native reload tests. The complete
native suite passes **47/47 tests, zero failures/skips** on the arm64 iPhone 17 Pro Simulator,
iOS 26.5. Two new tests mount and remount a small real Fabric status view, inspect the native
UIKit subtree independently of JS evidence, and retain the existing old-native-retirement /
replacement-startup assertions. A third test covers rejected module configuration and RN cleanup.

This extends the [surface-free RCTHost result](2026-11-11-apple-rcthost-reload.md), without changing
its historical scope. It is **not the complete workbench, a presented App/window or device UI
gate**. BLE remains scripted; no phone, shared ADB daemon, permission, radio or firmware was
changed. Android engine migration, physical RF and release provenance remain separate work.

## One delegate for App and regression

[AppDelegate.swift](../../../tests/consumers/react-native-bleng/ios/BleHubBlengHost/AppDelegate.swift)
now constructs [FormalReactNativeDelegate](../../../tests/consumers/react-native-bleng/ios/BleHubBlengHost/FormalReactNativeDelegate.mm),
which is compiled into both the App and existing native-test target. It replaces the old Swift
delegate without changing the Debug Metro URL or Release bundled-main URL selection.

The shared delegate:

- Supplies the normal existing dependency provider and intercepts only the BLeng lifecycle module.
  Other class/instance/C++ providers keep RN's usual factory fallbacks.
- Creates a fresh lifecycle module, keeps only its latest native lifetime, and configures the
  successor's exact predecessor before returning that module to RN. The retirement deadline
  remains 10 seconds; there is no wait for cleanup inside module creation.
- Is scoped to one factory/host; there is no process-global current-engine registry.
- Uses bounded synchronous lock sections, releases the lock for its host hook, and fails closed
  on reentrant/concurrent creation instead of waiting indefinitely.
- Has a default no-op observation/configuration hook. The test subclass uses that hook for
  scripted-backend injection and native evidence, not for duplicate predecessor linkage.

The [reload harness](../../../tests/consumers/react-native-bleng/ios/BleHubBlengHostNativeTests/BlengHostReloadHarness.mm)
now subclasses the formal delegate. Its previous independent predecessor-configuration code
has been removed. The build owner admits the AppDelegate, shared headers/implementation,
bridging header and native harness before any subprocess or generation; synthetic regressions
cover each missing input and the shared source wiring.

## Fail-closed module creation without stranding RN

Review of installed RN 0.87.1 found that throwing out of its instance-creation delegate skips the
holder's completion bookkeeping. Later invalidation can wait for that holder before reaching
RN's own invalidation-group deadline. Therefore a rejected creation must not escape that boundary
or return nil and permit an unconfigured fallback.

The shared delegate now marks its own failure sticky and returns the actual fresh module with
native `rejectHostAdmission()` applied. This disables owner binding but **does not invoke
invalidation**. RN can finish registration, the JS binder rejects startup, and actual RN teardown
can still call the module's invalidation hook. No cleanup failure becomes success.

The focused real-host regression injects an Objective-C hook exception before the Swift backend
configuration callback, verifies the module has zero bound owners and has not been artificially
invalidated, releases the host, then observes the real RN callback and empty native retirement
within five seconds. It passed. The one JS binding-rejection error in the suite is expected from
this case; it is not hidden or described as a clean console. Its passing XCTest assertions prove
the scoped failure behavior; this case does not attach a raw event array or run ID.

## Rendered checks

The existing local [JS entry](../../../tests/consumers/react-native-bleng/ios/BleHubBlengHostNativeTests/BlengHostReload.js)
still calls the real package factory and starts the owner once. It now registers a small
`BlengHostReload` component with an engine-local status store. Component mount/remount does not
create another owner, repeat startup, or close one through an effect cleanup.

Rendered mode retains a 390×844 standalone root UIView. It creates no window, scene, second App
or runner. Reload invokes the owned host's surface-restarting listener directly, not a global
reload broadcast. Native tests search the UIKit subtree for the accessible
`bleng-reload-status` marker and exact status using a bounded three-second observation window:

1. First engine's actual startup resolves, and native view status is `started`.
2. The same host reloads its engine and surface. Old retirement is held at its post-reader
   snapshot barrier; the replacement view is `pending`, and replacement backend creation and
   initialization counts are zero.
3. Releasing old cleanup permits `started` after success, or `failed` after
   `native_resources_not_zero`. The failure case never creates a new backend.
4. Host release triggers RN invalidation and each captured lifetime's native completion is
   awaited separately. Returning from stop alone is not cleanup evidence.

Both earlier surface-free cases still pass through the same shared delegate.

### Green identities and event attachments

All four reload cases ran in native PID **25316**. Each has eight contiguous native events,
with no `native-failed` event: generation-one host/configure/submit/resolve, then generation-two
host/configure/submit and its appropriate settlement.

| Case | Native run ID | Generation-two settlement |
|---|---|---|
| Surface-free success | `70205BDF-CFA8-41BF-9786-5152BB2166BD` | `start-resolved` |
| Surface-free cleanup failure | `96AE75AE-F861-47B9-9112-A2659FC89609` | `start-rejected` |
| Rendered success | `6F9009F1-A89F-42F3-8B2E-AD5357A8D3C4` | `start-resolved` |
| Rendered cleanup failure | `436BECDF-378A-4D6D-8B56-40BCD4EF2581` | `start-rejected` |

Each rendered case logs `Running "BlengHostReload"` twice, consistent with surface restart.
Native subtree matches are XCTest assertions, not independent screenshots/Appium evidence.
RN priority-inversion warnings and Debug localhost packager connection refusals remain. No Metro
server was started; this is not network-zero execution or proof of all RN/OS thread retirement.

## Build, validation and evidence

This turn intentionally did not repeat the already-failing Pod network prerequisite. Before the
installed-graph continuation, Podfile.lock and Manifest bytes matched, the independently evaluated
current formal Podspec matched its installed JSON, and generated code was present. Their lock
SHA-256 remains:

```text
1633fa09c2d12fb1f68826594a67cf13db548bebb8c1566c109898dda3d05b9b
```

No Podspec/dependency graph or generated code changed. The new shared consumer source belongs
to the existing App/native targets, not a new Pod. Local bundling used the existing owner's finite
RN CLI arguments; no server or install fallback was introduced.

The first native compile failed because the App target lacked the lifecycle-header search path
that the native target already used. Matching the existing Pod private-header search path for
App Debug/Release fixed it, without copying headers or editing generated Pods. The original
`initial-build.log` is retained. Subsequent compile and runtime passed:

```sh
pixi run just --command pixi run python tools/react_native/run_quiet.py --no-tty --progress-seconds 20 .work/apple-formal-delegate-rendered-reload-2026-11-11/admission-fixed-build.log -- pixi run -e apple-ci xcodebuild -quiet -workspace tests/consumers/react-native-bleng/ios/BleHubBlengHost.xcworkspace -scheme BleHubBlengHost -derivedDataPath tests/consumers/react-native-bleng/ios/build/simulator-derived -configuration Debug -sdk iphonesimulator -destination 'generic/platform=iOS Simulator' CODE_SIGNING_ALLOWED=NO CODE_SIGNING_REQUIRED=NO -only-testing:BleHubBlengHostNativeTests build-for-testing
pixi run just --no-deps react-native formal-host-ios-native-test 633606C2-89B2-4707-9CF7-2FEF326D7100
pixi run just react-native tools-test bleng_ios_consumer
pixi run just react-native tools-test recipe_wiring
pixi run just react-native formal-host-test
pixi run just test supervisor
```

Results: native **47 passed**; build owner **14 passed**; recipe wiring **15 passed**;
Jest **68 passed**; Rust Supervisor **124 passed**. This compile is an installed-source/Pod-graph
continuation, not a new normal `formal-host-ios-build` pass or clean artifact packaging result.
The earlier default/network-sensitive Pod failure remains unresolved.

The generated and copied 2,008,961-byte reload bundle match, SHA-256:

```text
6ae8f62a9db0f0a49c868e8a1f238f3a28d2c165896e2ffa0cf150f0caed0862
```

Evidence root: `.work/apple-formal-delegate-rendered-reload-2026-11-11/`. It retains the compile
failure and successful build, full green `.xcresult`/summaries, JS bundle, native run traces,
console diagnostics and source hashes. Old reports/results were not rewritten. Final checks passed:
`pixi run just fmt`, `pixi run just lint`, `pixi run just docs-check-mermaid`
(27 diagrams in 240 Markdown files), `git --no-pager diff --check`, and authored Markdown
UTF-8/whitespace/conflict/fence/local-link checks. The 14 build-owner and 15 wiring tests were
rerun after formatting and passed. Existing Git CRLF-normalization warnings were not whitespace
errors. Final read-only simulator inventory showed no booted simulator; no foreign or
physical-device cleanup was performed.

## Remaining work

The App startup code now has predecessor wiring. A real bundled Release App has also passed the
existing UI target's simulator smoke for `Tests → Start → Close` in an actual App process/window;
see [formal Workbench simulator smoke](2026-11-11-formal-workbench-simulator.md). The real UI target now also covers the bounded Tests → Evidence → Current run navigation
while retaining the same live run and snapshot. Engine reload and the old-owner retirement
barrier remain owned by this native target, because a production reload trigger would require
React Native internal APIs and the windowless host cannot render the window-dependent
workbench; see the [ownership table](2026-11-11-formal-workbench-simulator.md#boundaries-and-next-step).
Actual mobile/RF, Android engine lifecycle migration and final same-candidate release evidence
remain separate; Appium stays default and Maestro stays opt-in comparison.
