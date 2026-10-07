# Private React Native integration

This directory owns BleHub's repository-internal React Native New Architecture integration based on Nitro Modules. It is a private npm package and is not the future public `BLeng` API.

The integration infrastructure is being hardened now, but the current `BleHubProbe` surface remains intentionally private and test-shaped. Do not rename it to `BleHub`, publish `@blehub/react-native-internal`, or treat its RF/command methods as a product API. The proposed boundary and future naming are recorded in [`docs/research/react-native-integration-boundary.md`](../../docs/research/react-native-integration-boundary.md). For RN/Nitro terminology, naming, autolinking, and lockfiles, see the [developer primer](../../docs/research/react-native-nitro-primer.md).

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

The package name `@blehub/react-native-internal` is deliberate. It identifies a repository-owned diagnostics and conformance consumer rather than a semver-stable React Native BLE library. This package remains private and separate; the only planned published RN package is `@blehub/react-native`, after a reviewed `BLeng`/application API and its own cross-platform runtime, GATT, RF, packaging, and lifecycle gates. The two packages coexist in the repository, but simultaneous installation in one App is not a supported goal. The future public artifact needs its own clean consumer and cannot inherit this probe's test results.

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

The Apple podspec shells are tracked, while these inputs are staged by the Just recipe:

- `.work/staged/apple-production` → `apple-pods/BleHubUniFfi/`;
- `platforms/apple/Sources/BleHubApple` → `apple-pods/BleHubApple/Sources/`.

This preserves one production UniFFI native image in the final host.

## Commands

Run all workflows from the repository root through Pixi and the root Just module:

```text
pixi run just react-native install
pixi run just react-native check
pixi run just react-native stage-apple-production
pixi run just react-native host-ios-artifact-consumer
pixi run just react-native android-build
pixi run just react-native android-test-build
```

Host build/runtime commands are documented in the internal consumer README.
