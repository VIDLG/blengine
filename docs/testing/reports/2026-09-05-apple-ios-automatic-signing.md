# iOS automatic signing and runtime

Date: 2026-09-05

## Result

The current Apple iOS production candidate passed the complete physical-device
deployment path using Apple's automatic provisioning flow. No AppleScript,
Accessibility UI automation, or fastlane dependency was required.

Command:

```sh
pixi run -e apple-ci just apple runtime-ios-device
```

Observed result:

```text
apple_login_keychain=usable
apple_ios_signing_identity=delegated_to_xcodebuild
apple_ios_device_name=iPhone1
apple_ios_device_model=iPhone 14
apple_ios_device_version=26.6.1
apple_ios_developer_mode=enabled
apple_ios_xcode_project=existing
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

The isolated signing spike also produced an Xcode-managed iOS Team
Provisioning Profile with `TeamIdentifier=FWVYQ287W5` and an app Mach-O with
the same `TeamIdentifier`, using `xcodebuild -allowProvisioningUpdates`.

## Signing workflow

The runtime passes the selected Team and bundle identifier to `xcodebuild` and
uses `-allowProvisioningUpdates` and
`-allowProvisioningDeviceRegistration`. A certificate Team ID visible to
`security find-identity` does not have to equal the selected Team when Xcode's
managed automatic signing can provision the correct profile; the signed app
and embedded profile are the authoritative checks.

The generated Xcode project is reused rather than regenerated automatically,
so Xcode edits are preserved. If `project.yml` changes, the runtime blocks and
requires the explicit `ios-project-regenerate` recipe before trying again.
The `ios-signing` recipe only opens Xcode as a manual fallback.

## Community tooling decision

fastlane is not installed on this machine. Its `match`, `cert`, and `sigh`
actions are suitable for a shared CI certificate/profile workflow, but would
introduce a separate credential-storage and Apple Developer API policy. The
single-machine production lane therefore stays on the Apple-provided
`xcodebuild` flow; fastlane is not added as a runtime dependency.

This is an iOS signing, installation, launch, and initialization pass. It is
not an iPhone RF pass.
