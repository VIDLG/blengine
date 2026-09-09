# Apple backend initial CoreBluetooth slice

Date: 2026-09-02

## Scope

This report records the first production Swift/CoreBluetooth backend slice built against the current unfrozen Stable UniFFI candidate. The slice establishes package structure, single-owner lifecycle, serial queue confinement, initialization state mapping, ingress delivery, and deterministic tests. Scan, connection, GATT, subscription, signed-device runtime, and RF behavior are intentionally not claimed.

## Implementation

The new `platforms/apple` Swift package contains:

- `AppleBleBackend`, a Stable UniFFI `BleBackend` foreign-trait implementation;
- one process-local CoreBluetooth-controller ownership lease;
- an application-supplied serial `DispatchQueue` used for all backend state and `CBCentralManager` delegate callbacks;
- one-time ingress binding and root release during idempotent dispose;
- asynchronous `CBCentralManager` creation after initialize acceptance;
- first-`centralManagerDidUpdateState` initialization completion followed by passive state updates;
- `CBManagerState` and `CBManagerAuthorization` mapping to the unified adapter snapshot;
- callback-first dispose that detaches ingress/delegate before releasing the manager;
- explicit typed pre-acceptance rejection for operations not implemented by this slice.

The capability declaration is limited to CoreBluetooth properties already supported by the selected API shape. Required Notification/Indication mode selection and actual-mode reporting remain false because CoreBluetooth does not expose that guarantee through `setNotifyValue` completion.

## Validation

The current package/runtime prerequisite was regenerated and validated first:

```sh
pixi run -e apple-ci just spike apple-package
```

The production Apple slice then passed its public project recipe:

```sh
pixi run -e apple-ci just apple check
```

Observed results:

- six macOS mapping/lifecycle tests passed;
- every current `CBManagerState` case mapped;
- every current `CBManagerAuthorization` case mapped;
- scan and connection authorization use the same CoreBluetooth global authorization evidence;
- initialization transitions exactly once on the first state callback;
- repeated initialization is rejected before acceptance;
- close takes precedence over missing ingress after dispose;
- backend contract version matches the same generated candidate;
- generic iOS Simulator build passed;
- generic iOS Device arm64 build passed with signing disabled.

The first test run exposed a close-order bug: initialization after dispose returned `InvalidState` because ingress absence was checked before lifecycle closure. The implementation was corrected to return `Closed`, and the full recipe passed afterward.

## Android Peripheral setup status

The official Android CLI `1.0.15985488` was installed for Apple Silicon and project host-path checks were made cross-platform:

- Unix SDK executables no longer require `.exe`;
- the NDK prebuilt host path selects `darwin-x86_64` on macOS;
- Apple Silicon selects the API 36 `arm64-v8a` emulator image;
- the CLI resolver recognizes the official `~/.local/bin/android` install location;
- Windows retains its existing executable names, NDK host tag, and x86_64 image.

The SDK/NDK bootstrap did not complete because repeated requests to Google's `package_list.binpb` endpoint were refused. The project's post-install verification correctly treated the CLI's misleading zero exit status as failure because every required SDK package remained absent. No Android fixture build, ADB status, or RF test was attempted on this Mac, and none is recorded as passed.

## Remaining boundaries

The following remain open:

- real macOS initialization assembly/runtime through a bundled app with Bluetooth usage description;
- scan/filter/advertisement mapping and lifecycle;
- connection, GATT discovery, read/write, subscription, RSSI, and disconnect;
- signed iPhone app build, install, launch, and Bluetooth authorization;
- Android Peripheral preflight from this Mac after SDK bootstrap succeeds;
- macOS/iPhone RF matrix against Android and Linux/BlueZ/BLESS providers;
- lifecycle accounting, fault recovery, and soak;
- artifact-only `tests/consumers/apple` release packages;
- rerun against the frozen ABI candidate.

## Follow-up

Later on 2026-09-02, the bundled real-macOS initialization gate and deterministic scan slice were completed. The first terminal-launched runtime reported adapter `Unavailable` and authorization `NotDetermined`; a subsequent visible Cocoa launch through LaunchServices completed the system Bluetooth prompt and passed with `PoweredOn + Authorized`. Scan RF remains open. See the [scan and macOS runtime report](2026-09-02-apple-scan-slice-macos-runtime.md).
