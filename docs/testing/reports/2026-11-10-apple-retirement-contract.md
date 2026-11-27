# Apple retirement resource and close contracts — 2026-11-10

## Verdict and scope

The formal Apple RN native suite now passes **27/27 tests, zero failures/skips** on the
arm64 iPhone 17 Pro Simulator, iOS 26.5. This includes all six observation-timeout tests and
the new peer-cache retirement regression. This is a new candidate result, not a rewrite of the
[earlier 25-pass/1-failure run](2026-11-10-bleng-event-reader-observation-timeout.md).

No physical phone, ADB daemon, DUT permission, radio or fixture firmware was changed. No Appium,
Maestro, real Hermes reload or RF scenario ran. The ordinary iOS build recipe remains blocked
by its Pod preparation; the passing native tests used a verified installed-graph continuation.

## Verified causes and minimal changes

### Required resource omitted

The Apple backend's `diagnosticSnapshotRfResources` reports 11 native resource categories,
including `peerCacheEntries`. The formal `ApplePlatformSession.requiredZeroResourceNames` listed
only 10. This was not merely a test expecting too few fields: a snapshot omitting the peer-cache
category could pass retirement validation, because that category was not required.

The production correction adds `peerCacheEntries` to the required list. It does not remove or
filter any observed resource, accept missing as zero, or change backend accounting. The existing
native test still requires exact resource-name equality and all counts equal to zero.

The new `testPeerCacheRetirementRequiresAnExplicitZeroCount` feeds three snapshots through the
actual Swift → Rust Supervisor path using the existing no-radio scripted backend:

| Peer-cache entry | Required close observation |
|---|---|
| Missing | Terminal `missing_resource` failure |
| Explicit 0 | Successful terminal close |
| Explicit 1 | Terminal `native_resources_not_zero` failure |

Each case observes close twice and checks that backend disposal remains exactly once. This is a
snapshot-validation regression, not a physical peer-cache lifetime or RF test.

### Teardown called one-shot shutdown twice

`AppleRustSupervisorOwner.close()` directly invokes Rust's one-shot `shutdown()`, returning the
retirement snapshot. Repeating it after retirement yields `no active session` by existing design.
`closeResult()` uses `shutdownView()`, which publishes/caches the first terminal close outcome and
supports repeated observation without retrying cleanup. A terminal cleanup failure stays failed.

The native smoke previously called `close()` explicitly and again in its teardown. The teardown
now uses `closeResult()` and asserts a closed owner with no failure. The test also observes that
successful receipt twice after the explicit close. No Rust, UniFFI, public JS or product bridge
close semantics changed. Existing Rust tests already assert that repeated raw shutdown errors
while the cached view preserves its first success/failure.

Changed owners:

- [ApplePlatformSession.swift](../../../integrations/react-native-bleng/ios/ApplePlatformSession.swift)
- [Formal native tests](../../../tests/consumers/react-native-bleng/ios/BleHubBlengHostNativeTests/BleHubBlengHostNativeTests.swift)
- [Consumer responsibility guide](../../../tests/consumers/README.md)

## Red → green evidence

Local evidence root: `.work/apple-retirement-contract-2026-11-10/`.

1. Added the peer-cache regression and corrected teardown, with the old required-resource list.
   Full native run: **25 passed, 2 failed**. One failure was the original exact-name mismatch;
   the other showed a missing peer-cache entry incorrectly produced no failure instead of
   `missing_resource`. The prior repeated-shutdown teardown error was no longer present.
2. Added the required peer-cache category and rebuilt the same installed graph.
3. Full native run: **27 passed, 0 failed, 0 skipped**.

The original `red.xcresult`, `red-summary.json`, runtime/build logs and old platform source remain
separate from `green.xcresult`, `green-summary.json`, green logs and source SHA-256 receipt.
No test was excluded to obtain the pass.

Runtime command for both runs, after their respective source builds:

```sh
pixi run just --no-deps react-native formal-host-ios-native-test 633606C2-89B2-4707-9CF7-2FEF326D7100
```

The simulator was initially shut down. Final read-only `simctl list devices booted` showed no
booted simulator; no foreign process or simulator shutdown was needed.

## Build-preparation failure and local continuation

```sh
pixi run just react-native formal-host-ios-build
```

Failed again at `pod install --no-repo-update`, bounded to 180 seconds. The installed RN 0.87.1
`rncore.rb` executes an HTTP HEAD availability probe for its prebuilt Core Maven artifact before
checking the existing tarball cache. The child curl command has no explicit timeout. A matching
curl process was observed outliving the parent and later disappeared without agent intervention.
The underlying network/server/proxy cause is unverified; this is not a BLE or retirement failure.

Local source also explains why a direct workspace build initially failed: Pod evaluation removes
`build/generated/ios/ReactCodegen` before entering that probe, while regeneration occurs later.
The first continuation therefore failed on missing safe-area `ComponentDescriptors.cpp`. Both
preparation and compile failures are retained; they are not red assertion evidence.

Admission for local continuation verified identical `Podfile.lock` and `Pods/Manifest.lock`,
matching Podfile checksum and installed formal podspec, existing compiled-source references and
cached artifacts. The lock/Manifest SHA-256 remained unchanged before and after testing:

```text
bf6c8b171ee905f4c262773897420f28f5e0e98051a535e51bcace916c5b2694
```

Restored only generated outputs using the installed RN generator, without evaluating the Podfile,
installing tools/dependencies, changing third-party source or updating locks:

```sh
pixi run just --command pixi run python tools/react_native/run_quiet.py --no-tty --progress-seconds 20 .work/apple-retirement-contract-2026-11-10/local-codegen.log -- pixi run -e apple-ci /usr/bin/env -u RCT_SCRIPT_OUTPUT_DIR node /Users/vision/projects/blehub/tests/consumers/react-native-bleng/node_modules/react-native/scripts/generate-codegen-artifacts.js -p /Users/vision/projects/blehub/tests/consumers/react-native-bleng -o /Users/vision/projects/blehub/tests/consumers/react-native-bleng/ios -t ios
```

Then ran the existing owner's workspace build step against those installed Pods. The red build
used `red-build-after-codegen.log`; the green command was:

```sh
pixi run just --command pixi run python tools/react_native/run_quiet.py --no-tty --progress-seconds 20 .work/apple-retirement-contract-2026-11-10/green-build.log -- pixi run -e apple-ci xcodebuild -quiet -workspace tests/consumers/react-native-bleng/ios/BleHubBlengHost.xcworkspace -scheme BleHubBlengHost -derivedDataPath tests/consumers/react-native-bleng/ios/build/simulator-derived -configuration Debug -sdk iphonesimulator -destination 'generic/platform=iOS Simulator' CODE_SIGNING_ALLOWED=NO CODE_SIGNING_REQUIRED=NO -only-testing:BleHubBlengHostNativeTests build-for-testing
```

The compile invocations retained a 180-second execution limit and passed after codegen. This
continuation is local-package/native compile evidence, not a successful rerun of Pod preparation,
a new maintained runner, or clean artifact-only packaging proof.

## Other validation and remaining gates

- `pixi run just test supervisor`: **124 passed**, 6 suites.
- `pixi run just react-native formal-host-test`: **57 passed**, 2 suites.

Final checks passed: `pixi run just fmt`, `pixi run just lint`,
`pixi run just docs-check-mermaid` (27 diagrams, 236 Markdown files), and
`git --no-pager diff --check`. Authored Markdown passed UTF-8, trailing-whitespace,
conflict-marker, fence-balance and local-link checks. Git emitted existing CRLF-normalization
warnings, not whitespace errors.

The previously observed Apple native failure is resolved for this candidate. The ordinary build's
network-sensitive Pod preparation remains an infrastructure issue. Actual formal Hermes engine
invalidation, old-reader retirement across reload, same-candidate mobile scan/RF and release
cleanup/provenance gates remain separate next work. The HANS-OFF Appium/ADB stall is not claimed
fixed by either retirement or reader changes; Appium stays default and Maestro remains opt-in.
