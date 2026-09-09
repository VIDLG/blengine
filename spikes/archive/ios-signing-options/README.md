# iOS signing options spike

Status: bounded local spike; this directory is not a production dependency.

## Question

Can the iOS probe be signed without driving the Xcode UI after the generated
project is opened?

## Apple official path

The following command was run against the generated probe project:

```sh
xcodebuild -quiet \
  -project tests/hil/apple/ios/BleHubAppleIOSProbe.xcodeproj \
  -scheme BleHubAppleIOSProbe \
  -configuration Debug \
  -destination 'generic/platform=iOS' \
  -derivedDataPath .work/apple-signing-spike \
  -allowProvisioningUpdates \
  -allowProvisioningDeviceRegistration \
  DEVELOPMENT_TEAM=FWVYQ287W5 \
  PRODUCT_BUNDLE_IDENTIFIER=dev.blehub.fwvyq287w5.ios-probe \
  CODE_SIGN_STYLE=Automatic \
  ONLY_ACTIVE_ARCH=YES \
  build
```

Result: passed. Xcode produced an iOS Team Provisioning Profile with:

```text
TeamIdentifier=FWVYQ287W5
IsXcodeManaged=true
ProvisionedDevices includes the connected iPhones
```

The signed app's Mach-O also reported `TeamIdentifier=FWVYQ287W5`. The local
keychain still listed an Apple Development certificate under `6J7DUSCP6X`, so
the keychain certificate listing alone is not a sufficient preflight for the
Xcode-managed automatic signing path.

## Community path

fastlane is not installed on this Mac. Its `match`/`cert`/`sigh` actions can
manage certificates and provisioning profiles, but adopting them would add a
credential-storage and Apple Developer API workflow that is unnecessary for
this single-machine interactive lane. The current production path therefore
uses Apple's `xcodebuild -allowProvisioningUpdates`; fastlane remains an
optional future CI signing lane, not a runtime dependency.

## Decision

Keep Xcode UI opening as an escape hatch (`pixi run just apple ios-signing`),
but make automatic `xcodebuild` provisioning the primary deployment path. Do
not add AppleScript or Accessibility UI automation.
