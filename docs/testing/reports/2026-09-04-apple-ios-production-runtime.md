# iPhone production ABI and CoreBluetooth runtime

Date: 2026-09-04

## Result

The current unfrozen Apple production candidate passed the physical iPhone production runtime gate on the project Mac mini.

Command:

```sh
pixi run -e apple-ci just apple runtime-ios-device
```

Observed result:

```text
apple_login_keychain=usable
apple_ios_device_name=iPhone1
apple_ios_device_model=iPhone 14
apple_ios_device_version=26.2
apple_ios_developer_mode=enabled
apple_ios_xcode_project=generated
apple_ios_signed_device_build=passed
apple_ios_device_install=passed
apple_ios_device_launch=passed
apple_ios_corebluetooth_adapter_state=poweredOn
apple_ios_corebluetooth_scan_authorization=authorized
apple_ios_corebluetooth_connection_authorization=authorized
apple_ios_corebluetooth_initialize=passed
apple_ios_corebluetooth_authorized_adapter_state=poweredOn
apple_ios_corebluetooth_authorization=passed
apple_ios_corebluetooth_runtime=passed
```

## What this proves

- The tracked XcodeGen project can consume the staged production Apple package.
- The production Stable UniFFI native image and generated Swift API link into a device build.
- The app can be development-signed with the selected Apple Development identity.
- The signed app installs and launches on the physical iPhone 14.
- The exported production `BleHub` initializes through the direct Swift HIL path.
- CoreBluetooth reports a powered-on adapter and authorized scan and connection access.
- The iOS application-side production ABI/runtime initialization path completes on device.

The HIL app uses the production package, constructs the exported production `BleHub` with the Apple backend, and records results through a direct Swift `BleEventSink`. It does not use a Rust test component, a second native image, or `spikes/`.

## Boundaries

This is an iOS production ABI, signing, installation, launch, and initialization pass. It is not an iPhone RF pass. The following remain separate:

- iPhone scan and discovery against a real Peripheral;
- iPhone connection and aggregate GATT discovery against a real Peripheral;
- iPhone read/write and Notification/Indication RF;
- iPhone disconnect, recovery, and post-disconnect silence;
- final ABI-freeze rerun against the complete Windows + Apple + Android candidate.

The next Apple RF provider is native macOS BLESS/CoreBluetooth using the Mac's system-selected built-in controller and the provider-specific `blehub-macos-bless-basic-gatt-v1` profile. UB500 is not used by that native macOS Peripheral path.
