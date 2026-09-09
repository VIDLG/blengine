# Apple production-candidate environment, runtime, and package bring-up

Date: 2026-09-02

## Scope

This report records the first real-macOS execution of the current unfrozen BleHub Stable UniFFI production candidate and its XCFramework/Swift Package gate. It also records the available Apple DUT and Peripheral topology without promoting unexecuted signing, installation, CoreBluetooth, or RF work to a pass.

## Environment

- Host: Apple Silicon Mac mini
- macOS: 26.2
- Xcode: 26.2
- Swift: 6.2.3
- Pixi: 0.78.0
- Rust: 1.95.0 from the locked `apple-ci` environment
- Stable UniFFI: 0.32.0 from the candidate Cargo lock
- Public ABI state: unfrozen

Xcode tooling reported one connected iPhone 14 running iOS 26.2. The keychain reported four valid Apple Development code-signing identities. No account email, certificate hash, device UDID, private key, provisioning profile, or pairing record is stored in this report. These observations prove prerequisite visibility only.

## Peripheral/controller observations

The TP-Link UB500 enumerated in the macOS USB tree as an active Bluetooth-class device:

- VID: `0x2357`
- PID: `0x0604`
- product: `TP-Link UB500 Adapter`

It did not register as an `IOBluetoothHCIController`. `system_profiler` and IOKit continued to expose only the built-in Apple `BCM_4388C2` PCIe Bluetooth controller to the macOS Bluetooth stack. Therefore no Apple result may claim that CoreBluetooth used UB500.

The project BLESS 0.3.0 fixture is locked to `linux-64` and implemented against Linux/BlueZ. It cannot run the existing managed BLESS provider directly on macOS. Immediate Apple RF development uses the project Android Peripheral fixture. Authoritative BLESS fault/recovery/soak work still requires the Linux Peripheral VM to own UB500, unless a separate physical-Linux topology is explicitly reviewed later.

## Commands and results

The source-only checkout was prepared with:

```sh
brew install pixi
pixi install --locked -e default
pixi install --locked -e apple-ci
```

The existing current-candidate macOS runtime gate passed:

```sh
pixi run -e apple-ci just spike swift-macos
```

Observed terminal marker:

```text
swift_uniffi_production_candidate=passed
```

The logged Rust panic in that run is the fixture's intentional downstream-panic containment case; the process completed successfully and the panic did not cross the FFI boundary as a false success.

The new package gate passed:

```sh
pixi run -e apple-ci just spike apple-package
```

It performed and verified:

- release Rust static library builds for `aarch64-apple-ios`;
- release builds for `aarch64-apple-ios-sim` and `x86_64-apple-ios`;
- release builds for `aarch64-apple-darwin` and `x86_64-apple-darwin`;
- XCFramework slices:
  - iOS device: `arm64`;
  - iOS simulator: `arm64`, `x86_64`;
  - macOS: `arm64`, `x86_64`;
- both generated UniFFI C modules and Swift sources from the same candidate archive;
- Swift Package host build;
- an isolated macOS SPM consumer that depends only on the generated package and reruns the production-candidate lifecycle/runtime program;
- unsigned Xcode generic iOS Simulator package build;
- unsigned Xcode generic iOS Device package build.

Observed terminal marker:

```text
uniffi_production_candidate_apple_xcframework_spm_consumer=passed
```

## Conclusion

The current unfrozen candidate passes the real-macOS Stable UniFFI Swift runtime and XCFramework/SPM package-consumer prerequisites required before starting the CoreBluetooth backend. A later implementation step has since added the initialization-only production CoreBluetooth slice documented in the [initialization-slice report](2026-09-02-apple-initialization-slice.md). This result does not freeze the ABI and must be rerun where affected against the final Phase 5 freeze candidate.

The following remain unexecuted and are not passed:

- production Swift/CoreBluetooth behavior beyond the deterministic initialization-only slice, including real bundled-app initialization runtime;
- signed iPhone application build;
- provisioning, installation, launch, and device runtime;
- iOS Bluetooth authorization and CoreBluetooth state mapping;
- macOS or iPhone scan/connect/GATT/read/write/Notification/Indication RF matrix;
- Apple lifecycle/fault/recovery/soak against Android and Linux/BlueZ/BLESS providers;
- Apple release artifact consumers under `tests/consumers/apple`;
- final frozen-ABI Apple package rerun.
