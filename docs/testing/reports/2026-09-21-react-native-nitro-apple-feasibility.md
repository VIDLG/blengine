# React Native Nitro Apple feasibility and infrastructure extraction

Date: 2026-09-21

## Scope

This report records the internal low-level React Native bridge evidence obtained before extracting the reviewed feasibility implementation into its long-lived ownership boundary:

- module: `integrations/react-native-internal/`;
- downstream host: `tests/consumers/react-native-internal/`;
- orchestration: `just/react-native/mod.just`;
- helper tools: `tools/react_native/`.

The package remains private and exposes a minimal internal `BleHub` probe. It is not the future public `BLeng` surface, does not add a TurboModule API, and does not create a second Rust native image.

## Verified Apple evidence before extraction

The feasibility candidate passed the following gates on the project Mac and a connected iPhone 14 Plus:

- Nitrogen TypeScript validation and Swift/Kotlin/C++ generation;
- CocoaPods installation;
- unsigned generic iOS Simulator build;
- generic macOS build;
- production Stable UniFFI single-image checks;
- RN JavaScript → Nitro HybridObject → production `BleHubUniFfi` → `BleHubApple` runtime initialization;
- maximum canonical `u64` acceptance and rejection of empty, leading-zero, signed, fractional, and overflowing forms;
- `ArrayBuffer` and typed-error transport;
- listener removal, duplicate close, late-callback silence, and post-close rejection;
- eight-cycle iOS and macOS lifecycle stress;
- physical-iPhone bridgeless engine reload/invalidation with a second production probe generation;
- physical-iPhone XCUITest transition from `Run 1 / 5/5` through `Recreate and run` to `Run 2 / 5/5`.

These are host, bridge, assembly, and lifecycle results. They are not BLE RF evidence. The engine reload result does not prove Fast Refresh's separate incremental source-patch behavior.

## Extraction decisions

- The integration is named `@blehub/react-native-internal`, with native module name `BleHubReactNative` and Android/C++ namespace `blehubreactnative`.
- The test applications retain `BleHubNitroHost` and `BLEHUB_NITRO_RUNTIME` identifiers because they identify the Nitro host gate rather than a public API.
- Generated Nitrogen glue, staged Apple package contents, Pods, workspaces, dependencies, and build output are regenerated locally and are not tracked.
- Just owns working directories; `run_quiet.py` only captures full logs, emits a compact success line, prints a bounded failure tail, and preserves exit status.
- XCUITest remains the UI automation framework. Swift Testing is reserved for future pure-Swift unit logic where appropriate.
- Android source and static recipes are retained, but Android device runtime is not claimed and did not block this Apple-first extraction by explicit user direction.

## Post-extraction validation

The long-lived paths and renamed package passed:

- module TypeScript validation and regenerated Swift/Kotlin/C++ bindings;
- mobile and macOS host TypeScript checks;
- RN CLI Android autolinking to `BleHubReactNativePackage`;
- Android debug host build and APK single-image check;
- iOS and macOS CocoaPods installation with machine-independent repository paths in both lockfiles;
- unsigned generic iOS Simulator and macOS builds;
- iOS Simulator and macOS production UniFFI single-image checks;
- signed physical-iPhone build, installation, and device-product single-image check;
- physical-iPhone basic runtime, eight-cycle lifecycle stress, and bridgeless engine reload/invalidation;
- physical-iPhone XCUITest recreate flow, with the `.xcresult` reporting one passed test and zero failures;
- development-signed macOS Release build with strict nested-signature verification, Apple Development authority, stable bundle ID, and `TeamIdentifier=FWVYQ287W5`;
- development-signed macOS basic runtime through RN, Nitro, production UniFFI, and `BleHubApple` after rebuilding and re-signing;
- development-signed macOS eight-cycle lifecycle stress in a second process after another rebuild and re-sign, including recreate/close, listener teardown, late-callback silence, duplicate close, and post-close rejection;
- repository lint, focused Python formatting/lint, Just formatting, and authored Markdown integrity checks.

The first two post-extraction iPhone launch attempts were rejected because the device was locked; the unlocked reruns above then passed and supersede those environment failures. The initial ad-hoc macOS runtime exposed a real fixture limitation: each newly launched process could leave `kTCCServiceBluetoothAlways` pending even after one process had been approved. It is retained only as diagnostic evidence and is not the maintained runtime path. The long-lived runtime recipes now use an Apple Development signature and explicit team argument. After resetting only the stale test-app `BluetoothAlways` decision and granting the signed app once, both formal signed recipes passed across subsequent rebuild/re-sign operations and independent processes.

## Remaining admission work

Android still requires device/emulator runtime validation and is not blocked on this extraction. The signed macOS recipes require a locally available Apple Development identity, an explicitly supplied team, a human-unlocked login keychain, and one-time Bluetooth approval; they never accept or persist a password. Before treating a platform as a complete formal RN integration gate, rerun the applicable same-candidate build, package, single-image, runtime, lifecycle, and RF gates after relevant package or ABI changes. The future public RN surface waits for the separately reviewed `BLeng` implementation and must reuse this host integration without duplicating the low-level contract or native image.
