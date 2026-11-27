# Formal Workbench simulator smoke — 2026-11-11

## Verdict and scope

A real bundled Release build of the formal RN App was installed and launched on
the iPhone 17 Pro iOS 26.5 Simulator:

```text
simulator: 633606C2-89B2-4707-9CF7-2FEF326D7100
bundle: org.reactjs.native.example.BleHubBlengHost
```

The formal UI target passed both lifecycle scenarios:

```text
BleHubBlengHostUITests.testFormalLifecycleCanStartAndClose
BleHubBlengHostUITests.testFormalWorkbenchNavigationPreservesTheSameRun
```

They exercised the actual App process and window, not the native reload harness
and not Jest:

1. launch the bundled App;
2. observe `session-state=idle`;
3. tap `start-session`;
4. observe `session-state=started`;
5. tap `close-session`;
6. observe `session-state=closed`.

The second scenario additionally verifies real Workbench navigation:

1. browse `Evidence → Tests` before a run without creating an owner;
2. start from `Tests`;
3. browse to `Evidence` and observe the successful start record;
4. browse to `Current run` and observe the same initialization snapshot;
5. close from `Current run` and observe `closed`.

This is a simulator UI smoke result. It is not an iPhone/Android device result,
RF result, resource-zero receipt, Appium result, release gate, or ABI-freeze
result.

## Build and launch details

The ordinary formal build intentionally produces a Debug build whose AppDelegate
expects Metro. The UI gate therefore uses a dedicated shared scheme,
`BleHubBlengHostUISimulator`, that builds only the App plus the UI test target in
Release. The native `@testable` target is not part of that scheme, so no
testability override is needed, and the production App and package Podspec keep
their normal settings.

```text
pixi run just react-native formal-host-ios-ui-test 633606C2-89B2-4707-9CF7-2FEF326D7100
```

The recipe depends on the ordinary `formal-host-ios-build` (which must first
verify the Pods preparation receipt), then runs `build-for-testing` and
`test-without-building` against the generic simulator and the exact simulator
ID. Logs: `build/ui-test-build.log` and `build/ui-test-run.log`.

Adding the scheme changed the recorded Pods integration graph, and refreshing
the receipt then exposed a second real defect: React Native's prebuilt Core pod
reassembles `React.xcframework` during a build, so `AvailableLibraries` ordering
in its installed `Info.plist` is not stable. The installed snapshot now hashes
vendored XCFramework metadata semantically — every field and value is retained
and slices are ordered by their unique `LibraryIdentifier` — while a changed
platform, architecture, path or identifier still invalidates the receipt.

The first Debug `simctl launch` was intentionally retained as a negative
observation: it logged `No script URL provided` because Debug expects Metro.
The Release bundled launch instead logged `Running "BleHubBlengHost"` and
`BLEHUB_FORMAL_NAV phase=commit page=Tests` with no missing-script error.

## Boundaries and next step

The result confirms that the formal App can start and close a real bundled
lifecycle session in a real window, and that browsing keeps the same run. Two
capabilities were evaluated and **rejected**, so the reload contract is not
forced into the wrong layer:

- **No production reload trigger.** Triggering a reload from the product App
  would require calling React Native's internal `RCTHost` reload entry, reachable
  only through `RCTHost+Internal.h` and the factory's internal root-view factory.
  Adding that to production code would couple the shipped App to RN internals, so
  the reload and old-owner retirement barrier stays in the native test target,
  which may legitimately import those headers.
- **No windowless workbench render.** Mounting the real `App` component in a
  test-created `RCTHost` crashes in `react-native-safe-area-context`: without a
  window, `initialWindowMetrics` is nil and the object prop conversion rejects
  it. The real workbench therefore renders only in a window-backed App, which is
  exactly what the UI target provides.

Resulting ownership, with no duplicate host or second UI framework:

| Concern | Owner |
|---|---|
| Real App process, window, navigation, Start/Close | `BleHubBlengHostUITests` (Release bundle) |
| Engine reload, old-owner retirement, replacement blocking | `BleHubBlengHostNativeTests` (real RCTHost/Hermes, scripted backends) |
| Shared delegate, predecessor wiring, lifecycle module | `FormalReactNativeDelegate` + `BlengEngineLifetime` |

Both targets exercise the same delegate and lifecycle module, so the split is by
observable capability, not by duplicated implementation. Actual mobile/RF,
Android engine lifecycle migration and final same-candidate release evidence
remain separate; Appium stays the default UI driver and Maestro stays opt-in.
