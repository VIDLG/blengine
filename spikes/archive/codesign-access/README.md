# macOS codesign access spike

- Status: active investigation only
- Scope: isolate Apple Development private-key access from Xcode project generation, provisioning, CoreDevice, and BLE/RF.
- Non-production: this directory is not imported by production crates, Apple HIL, Pixi environments, or Just recipes.

## Question

The iPhone HIL can discover a paired and connected iPhone and the production Apple package builds, but `xcodebuild` fails while signing the generated device app with:

```text
errSecInternalComponent
Command CodeSign failed with a nonzero exit code
```

The existing Apple workflow already has a human-only recipe, `pixi run just apple unlock-login-keychain`, which runs `security unlock-keychain` and `security set-key-partition-list`. Ordinary build and test recipes must not request or store a password.

This spike checks whether the failure is:

1. missing/locked login-keychain identity visibility;
2. general `codesign` malfunction; or
3. inability of `codesign` to use the Apple Development private key.

## Run

Run from the project root on the real Mac mini:

```text
pixi run python spikes/archive/codesign-access/probe.py
```

The default run:

- reports only the default keychain path and the count of valid code-signing identities;
- copies the current iOS `debug.dylib` to a temporary directory;
- performs an ad-hoc sign/verify probe on that copy;
- does not unlock a keychain, change key partition lists, or modify the build artifact;
- skips the private-key probe unless an identity selector is explicitly supplied.

To test the Apple Development private key, first obtain the identity selector locally without putting it in a script or config:

```text
security find-identity -v -p codesigning
```

Then pass the exact identity name to the spike. The selector is redacted from the displayed command, and only the temporary copy is modified:

```text
pixi run python spikes/archive/codesign-access/probe.py --identity "Apple Development: ..."
```

If the current failed build did not leave the expected artifact, use another local Mach-O artifact explicitly:

```text
pixi run python spikes/archive/codesign-access/probe.py --artifact /path/to/artifact --identity "Apple Development: ..."
```

## Interpretation

| Result | Meaning |
|---|---|
| `codesign_ad_hoc_probe=failed` | The local `codesign` tool or input artifact is itself unusable; do not diagnose provisioning yet. |
| `codesign_ad_hoc_probe=passed`, `codesign_development_probe=failed` with `errSecInternalComponent` | Strong isolated evidence that the Apple Development private key is unavailable to the invoking `codesign` process, commonly key-partition/access-control state. |
| Development sign passes, Xcode still fails | The private key is usable; investigate Xcode automatic provisioning, profile/device registration, entitlements, or the exact build invocation. |
| Development sign and Xcode build pass | The signing boundary is cleared; continue to install/launch/CoreBluetooth runtime, then RF. |

A successful `security find-identity` count alone is insufficient: it proves an identity is discoverable, not that `codesign` can use its private key.

## Current investigation

The latest iPhone RF attempt reached the generated Xcode device build with:

```text
apple_ios_device_name=iPhone1
apple_ios_developer_mode=enabled
apple_login_keychain=usable
```

The build failed before installation with:

```text
BleHubAppleIOSProbe.debug.dylib: errSecInternalComponent
```

The isolated probe reproduced the boundary without involving Xcode or CoreDevice:

```text
codesign_valid_identity_count=5
codesign_identity_team_ids=6J7DUSCP6X,DVF5T95W89,X52857NZLW,YFVHYWPUUJ
codesign_ad_hoc_probe=passed
codesign_development_probe=failed
... errSecInternalComponent
```

The same development-sign probe fails for both the iOS `debug.dylib` and a copied system `/usr/bin/true` Mach-O. This proves the failure is not caused by the app artifact:

```text
/usr/bin/true: replacing existing signature
/usr/bin/true: errSecInternalComponent
```

A name-based selector for the newly installed `6J7DUSCP6X` certificate is also ambiguous because two identical certificates are present. Selecting one certificate by SHA-1 removes the ambiguity but still returns `errSecInternalComponent`.

The current Xcode preference selects the non-Personal Team `FWVYQ287W5`, which is not among the certificate Team IDs visible in the login keychain. This is a second independent issue to resolve after private-key access: even if the key partition is repaired, automatic signing cannot use a certificate that belongs to another Team.

Therefore no iPhone CoreBluetooth or BLE/RF conclusion can be drawn from the failed run. Android Peripheral preflight was independently `ready=ok`.

## Boundary

Do not promote this spike's commands, identity selectors, temporary artifacts, or conclusions into production automation without a separate reviewed change. In particular, never add a password argument, export a private key, weaken keychain ACLs globally, or make ordinary Apple tests unlock a login keychain.
