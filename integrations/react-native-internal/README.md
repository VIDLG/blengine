# Private React Native integration

This directory owns BleHub's repository-internal React Native New Architecture integration based on Nitro Modules. It is a private npm package and is not the future public `BLeng` API.

The integration infrastructure is being hardened now, but the current `BleHubProbe` surface remains intentionally private and test-shaped. Do not rename it to `BleHub`, publish `@blehub/react-native-internal`, or treat its RF/command methods as a product API. The proposed boundary and future naming are recorded in [`docs/research/react-native/react-native-integration-boundary.md`](../../docs/research/react-native/react-native-integration-boundary.md). For RN/Nitro terminology, naming, autolinking, and lockfiles, see the [developer primer](../../docs/research/react-native/react-native-nitro-primer.md).

## Ownership boundary

This package contains only the repository-owned integration:

- one TypeScript `HybridObject` spec under `specs/`;
- the TypeScript creation wrapper under `src/`;
- thin Swift and Kotlin adapters;
- Android native build/autolinking configuration;
- CocoaPods shells used to consume the production Apple assembly.

React Native applications, Metro configuration, signing, device control, runtime logs, and UI automation belong to the downstream host in [`tests/consumers/react-native-internal`](../../tests/consumers/react-native-internal/README.md).

The integration consumes the canonical production Stable UniFFI image. It must not define a second Rust image, a TurboModule API, a second BLE contract, or duplicate `BleHub` state. The npm package remains `private: true`.

## Current internal surface

The package name `@blehub/react-native-internal` is deliberate: it identifies a transitional repository-owned diagnostics probe, not a semver-stable React Native BLE library. Its native test seams and evidence are being moved to consumer/HIL ownership; the package itself is scheduled for deletion once the formal BLeng consumer and the required host-only RF/HIL coverage converge. It must not be renamed into the public package, and its results cannot be inherited by the formal BLeng consumer.

The current low-level feasibility surface validates:

- dispatch acceptance through one command-shaped method;
- asynchronous production `BleHub` completion events;
- canonical decimal `u64` strings without JavaScript `Number` conversion;
- `ArrayBuffer` transport and typed errors;
- listener registration/removal;
- explicit asynchronous `close()` and late-callback suppression;
- private iOS scan, connect/disconnect, aggregate GATT discovery, and characteristic read/write RF commands and event projections against the production Apple backend (not a BLeng-facing API).

The active iOS adapter constructs the production `BleHub` with `BleHubApple`. Independent iPhone-to-nRF checkpoints cover [connection](../../docs/testing/reports/2026-09-25-react-native-ios-nrf-connect.md), [aggregate GATT](../../docs/testing/reports/2026-09-26-react-native-ios-nrf-gatt.md), [characteristic I/O](../../docs/testing/reports/2026-09-26-react-native-ios-nrf-gatt-io.md), [Notification](../../docs/testing/reports/2026-09-26-react-native-ios-nrf-notification.md), [strict Indication](../../docs/testing/reports/2026-09-26-react-native-ios-nrf-indication.md), [RSSI](../../docs/testing/reports/2026-09-27-react-native-ios-nrf-rssi.md), [ten-cycle passive recovery](../../docs/testing/reports/2026-09-27-react-native-ios-nrf-recovery-ten.md), and the separate [optional DK pending-read reload diagnostic](../../docs/testing/reports/2026-09-27-react-native-ios-dk-pending-att-maintained.md). These are private, unfrozen, individually scoped results, not a public BLeng API or a blanket same-package RF matrix pass. The diagnostic gate's synthetic closed-sink rejection does not prove an actual late CoreBluetooth callback or Nitro queue drain. The separate RN macOS feasibility host was archived under [`tests/consumers/archive/react-native-macos/`](../../tests/consumers/archive/react-native-macos/) and macOS is no longer advertised by this module's active autolinking/podspec gates; this does not remove the production macOS CoreBluetooth backend. The Android adapter now constructs the production `AndroidBleBackend` through `NitroModules.applicationContext`; the private RN bridge has passed the single-DK production Android scan/connect/GATT/I/O/RSSI/Notification/strict-Indication/passive-disconnect matrix, documented in the [2026-10-03 Android RN RF report](../../docs/testing/reports/2026-10-03-react-native-android-nrf-rf.md). Android reload/invalidation and multi-DK evidence remain separate gates.

## Generated and staged inputs

`nitrogen/generated/`, `node_modules/`, Android build output, and staged Apple package contents are local generated artifacts and are ignored. Do not hand-edit or commit them.

The shared backend Podspec is owned by
[`platforms/apple/packaging/BleHubApple.podspec`](../../platforms/apple/packaging/BleHubApple.podspec).
Both Apple RN staging lanes copy that canonical template; neither integration owns a duplicate.
This package's `packaging/apple/` retains its SwiftPM manifest and BleHub-only UniFFI Podspec for
`apple-production-react-native` staging. The separate formal BLeng RN package stages its combined
image and consumer inputs outside the integration tree:

- `.work/staged/apple-bleng` → `.work/staged/apple-bleng-react-native/BleHubUniFfi/`;
- `platforms/apple/Sources/BleHubApple` → `.work/staged/apple-bleng-react-native/BleHubApple/Sources/`;
- `integrations/react-native-bleng/packaging/apple/Package.swift` → the backend SwiftPM manifest;
- `platforms/apple/packaging/BleHubApple.podspec` → the shared backend Podspec; the combined UniFFI
  Podspec is generated by `tools/react_native/stage_bleng_apple.py`.

This private integration no longer owns an `apple-pods/` staging directory. Its remaining Apple
sources are the legacy low-level probe; test-only seams live with the legacy consumer. That host
still consumes the combined production UniFFI/backend artifacts but no longer imports or autolinks
the formal `BleHubReactNativeBleng` Nitro package just to run its owner smoke. Formal Apple/Kotlin
bridge contracts now live in `tests/consumers/react-native-bleng`; raw probe/RF tests stay in the
legacy host until their real owner and replacement coverage are ready.

## Commands

Run all workflows from the repository root through Pixi and the root Just module:

```text
pixi run just react-native install
pixi run just react-native check
pixi run just react-native stage-apple-bleng
pixi run just react-native host-ios-artifact-consumer
pixi run just react-native android-build

```

Host build/runtime commands are documented in the internal consumer README.
