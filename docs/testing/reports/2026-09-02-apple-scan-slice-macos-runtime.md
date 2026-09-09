# Apple scan slice and bundled macOS CoreBluetooth runtime

Date: 2026-09-02

## Scope

This report records the second production Swift/CoreBluetooth backend slice built against the current unfrozen Stable UniFFI candidate. It adds scan lifecycle, advertisement mapping, local predicate enforcement, backend-private peripheral identity, and a bundled real-macOS initialization probe. No scan RF, connection, GATT, subscription, signed-iPhone runtime, or Apple release-artifact result is claimed.

## Scan implementation

The Apple backend now:

- accepts at most one active scan;
- uses `scanForPeripherals(withServices:options:)` for service pushdown and the duplicate-reporting hint;
- uses the documented CoreBluetooth local start/stop completion points;
- emits `ScanStarted -> CommandCompleted` for a successful start;
- emits requested `ScanStopped -> CommandCompleted` for an active stop;
- treats a repeated stop of the last stopped session as terminal success without fabricating a second `ScanStopped`;
- maps local name, normal plus overflow service UUIDs, manufacturer data, service data, RSSI, and connectability into `AdvertisementObservation`;
- canonicalizes 16-bit, 32-bit, and 128-bit CoreBluetooth UUIDs;
- rechecks service, case-sensitive name-prefix, and company-ID predicates after native mapping;
- allocates a sequential opaque `PeripheralId` from `CBPeripheral.identifier`, retains the native peripheral for later connection work, and reuses the ID across scan sessions in the same backend lifecycle;
- stops an active scan with an error event if later CoreBluetooth state or authorization becomes unavailable;
- clears scan routes and retained peripherals before detaching/stopping CoreBluetooth during dispose.

The manufacturer parser treats the first two CoreBluetooth manufacturer bytes as the little-endian company identifier and the remainder as payload. Deterministic mapping covers this representation, but the project-authored compatibility research still classifies the exact Apple company-ID byte boundary as requiring authoritative format or RF evidence before final promotion.

## Deterministic and generic-build validation

The public non-RF recipe passed:

```sh
pixi run -e apple-ci just apple check
```

Observed results:

- 11 macOS mapping/lifecycle/scan tests passed;
- scan exclusivity, wrong-session rejection, idempotent repeated stop, and cross-session peripheral-ID reuse passed;
- 16-bit, 32-bit, and 128-bit UUID canonicalization passed;
- advertisement field mapping and local predicate enforcement passed;
- authorization and powered-on scan availability mapping passed;
- generic iOS Simulator build passed;
- generic iOS Device arm64 build passed with signing disabled.

The first scan test run compiled successfully but had one incorrect test expectation: `CBUUID` preserves the supplied canonical 128-bit UUID in `uuidString` rather than shortening it to `FFF0`. The assertion was corrected and all 11 tests passed.

## Bundled macOS initialization runtime

A source-tree HIL consumer now exists at `tests/hil/apple/macos`. The `apple runtime-macos` recipe:

1. regenerates the current candidate package;
2. builds a standalone SwiftPM executable against `BleHubApple` and the candidate bindings;
3. assembles a macOS `.app` containing `NSBluetoothAlwaysUsageDescription`;
4. validates its property list and ad-hoc signs the bundle;
5. creates `AppleBleBackend` through the Rust `ProductionCandidateFixture`;
6. dispatches `InitializeCommand` and verifies the first two events are the matching `AdapterSnapshotChanged` and successful `CommandCompleted`.

The noninteractive runtime recipe passed:

```sh
pixi run -e apple-ci just apple runtime-macos
```

Its first terminal-launched execution observed:

```text
apple_macos_corebluetooth_adapter_state=unavailable
apple_macos_corebluetooth_scan_authorization=notDetermined
apple_macos_corebluetooth_connection_authorization=notDetermined
apple_macos_corebluetooth_initialize=passed
```

This passed bundled-process CoreBluetooth construction, delegate callback delivery, Stable UniFFI ingress, command correlation, and initialization event order, but did not decide authorization. System logs confirmed that LaunchServices execution reached `kTCCServiceBluetoothAlways` `AUTHREQ_PROMPTING`. Early interactive attempts timed out because the command-line-style bundle was not running a visible Cocoa application event loop; those were probe-infrastructure failures, not backend failures.

The probe was corrected to launch a visible regular Cocoa app, run `NSApplication` on the main thread, and wait up to 120 seconds while backend work remained on its supplied serial queue. The interactive recipe then passed:

```sh
pixi run -e apple-ci just apple authorize-macos
```

```text
apple_macos_corebluetooth_adapter_state=poweredOn
apple_macos_corebluetooth_scan_authorization=authorized
apple_macos_corebluetooth_connection_authorization=authorized
apple_macos_corebluetooth_initialize=passed
apple_macos_corebluetooth_authorized_adapter_state=poweredOn
apple_macos_corebluetooth_authorization=passed
```

The failed interactive probe also exposed an unconditional dispose-time `stopScan()` call while CoreBluetooth was not powered on. CoreBluetooth logged this as API misuse. Dispose now calls native `stopScan()` only when BleHub has an active scan, while still clearing scan routes and detaching the delegate before manager release.

One visible LaunchServices run passes real macOS Bluetooth authorization and the `PoweredOn + Authorized` initialization boundary. The development probe is ad-hoc signed, so rebuilding or re-signing it can create a new TCC code identity and require `authorize-macos` again; a later rebuilt terminal-launched instance accordingly returned `NotDetermined`. Valid Apple Development identities are visible, but `codesign` from the current agent process could not access the private key and returned `errSecInternalComponent`, so no development-signing gate is claimed. None of these results is a scan or RF pass.

## Peripheral and authorization status

The Android SDK/NDK bootstrap on this Mac remains blocked by connection refusal from Google's package-list endpoint. No Android fixture preflight or RF run was attempted. The UB500 remains USB-visible but unavailable to CoreBluetooth and cannot directly host the Linux/BlueZ BLESS fixture on macOS.

Before a real scan run, the selected Android or Linux/BlueZ/BLESS Peripheral must pass explicit preflight. The DUT, provider, duration, and exclusive resources must be stated before that run.

## Remaining boundaries

The following remain open:

- real macOS scan/filter/advertisement and start/stop RF behavior;
- connection, GATT discovery, read/write, subscription, RSSI, and disconnect;
- signed iPhone app build, install, launch, and Bluetooth authorization;
- Android Peripheral preflight from this Mac after SDK bootstrap succeeds;
- macOS/iPhone RF matrix against Android and Linux/BlueZ/BLESS providers;
- lifecycle accounting, fault recovery, and soak;
- artifact-only `tests/consumers/apple` release packages;
- rerun against the frozen ABI candidate.
