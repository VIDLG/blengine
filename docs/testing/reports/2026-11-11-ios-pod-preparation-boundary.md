# iOS Pods preparation boundary — 2026-11-11

## Verdict and scope

The formal iOS build owner now has two explicit paths:

- `pixi run just react-native formal-host-ios-build` verifies a sealed local
  preparation receipt and does not run `pod install`, `pod ipc spec`, or repair
  stale state.
- `pixi run just react-native formal-host-ios-build true` is the only refresh
  path. It invalidates the old receipt before installation, owns the refresh
  process group, and publishes a new receipt only after the installed graph
  passes the bounded snapshot.

This is a source/local-package build boundary. It is not a clean npm artifact
consumer, an iOS runtime result, a physical-device result, an RF result, a
release gate, or an ABI-freeze result.

## Root cause and implemented boundary

RN 0.87.1's CocoaPods helpers perform remote Maven `curl` probes before deciding
whether to use cached React Native artifacts. Those probes can intermittently
stall or fail even when archives are already present. `--no-repo-update` does
not bypass the helper probes.

The owner now:

- uses a local RN Core and RN Dependencies debug tarball when both complete
  archives are present in the host's `Pods/*-artifacts` cache;
- binds both local inputs together, preventing a mixed prebuilt/source graph;
- rejects a requested prebuilt mode that silently falls back to source;
- leaves a partial cache or unsupported graph fail-closed rather than adopting
  it or changing the receipt policy;
- hashes the effective local-artifact environment in the receipt, so changing
  the selected inputs requires an explicit refresh.

This does not repair the remote Maven service. It makes the current formal host
candidate independent of the remote probe when its already-installed local
artifacts are complete.

The refresh uses CocoaPods `--no-repo-update --clean-install --deployment`.
The last two options keep the explicit refresh tied to the locked Podfile graph
and prevent a stale incremental installation from being silently accepted.

## Receipt and graph contract

The receipt is:

```text
.work/react-native-bleng-ios-consumer/pods-preparation.json
```

The snapshot covers declared package/Pod/Nitrogen/codegen metadata, Hermes
version metadata, Podfile/lock/manifest identity, installed JSON podspecs,
selected subspec file membership, XCFramework `Info.plist` metadata, support
files, Xcode project/workspace integration, environment fingerprints, and
bounded target/resource membership checks. Ordinary native source/binary byte
edits remain allowed where they do not change preparation membership or
metadata inputs.

The snapshot is deliberately not a universal CocoaPods dependency analyzer or
a complete proof of every possible resource integration shape. Unsupported,
non-local, oversized, malformed, or ambiguous graph inputs fail closed.

Two ordering-only sources were confirmed to produce false staleness and are now
normalized semantically, without dropping any field or value:

- the Apple package owner sorts `AvailableLibraries` by `LibraryIdentifier`
  when it assembles its own XCFramework;
- the installed snapshot hashes vendored XCFramework metadata the same way,
  because React Native's prebuilt Core pod reassembles `React.xcframework`
  during a build.

A changed platform, variant, architecture, path, identifier or any other field
still invalidates the receipt.

The XCFramework packager now canonicalizes only `AvailableLibraries` ordering
by unique `LibraryIdentifier`, preserving all fields and other array order.
This removes a reproducible false stale result caused by nondeterministic
`xcodebuild` slice ordering without weakening slice metadata checks.

## Evidence

Explicit refresh succeeded with the local cached RN artifacts, followed by an
ordinary build:

```text
pixi run just react-native formal-host-ios-build true
pixi run just react-native formal-host-ios-build
```

The resulting receipt had:

```text
receipt bytes: 13,765,794
receipt SHA-256: 8337b7df750cb6c19ed48d8aafc4b80225004d88c6bfd03a3dc2b2f94fe9a690
installed pod roots: 90
installed targets: 95
declared metadata files: 7,696
```

The ordinary build log began with `bleng_pods_preparation=verified (no pod
install)`, demonstrating that it consumed the receipt rather than refreshing
Pods.

Targeted checks passed:

```text
pixi run just apple test-consumer-tools                 # 36 passed
pixi run just react-native tools-test ios_pod            # 65 passed
pixi run just react-native tools-test bleng_ios_consumer # 37 passed
pixi run just react-native tools-test recipe_wiring     # 18 passed
pixi run just react-native formal-host-test              # 68 Jest tests passed
pixi run just test supervisor                            # 124 passed
pixi run just fmt
pixi run just lint
```

The formal iOS native-test recipe also passed after the deterministic package
and receipt refresh work; its scope remains the existing simulator native test
bundle, not a mobile/RF gate.

## Preserved failures and limits

Earlier explicit-refresh attempts remain represented by their logs and were
not rewritten. They demonstrated:

- remote probe instability;
- RN's mixed prebuilt/source fallback, now rejected by the Podfile;
- source-mode graph expansion introducing non-local installed specs, correctly
  rejected by the local-only snapshot contract;
- XCFramework slice-order drift, fixed at the Apple package owner.

No third-party React Native or CocoaPods source was modified. No phone, ADB
daemon, Appium session, permission, radio, firmware, signing identity, or
keychain was changed. Appium/HANS behavior remains a separate unresolved
Android automation boundary, and Maestro remains opt-in comparison only.
