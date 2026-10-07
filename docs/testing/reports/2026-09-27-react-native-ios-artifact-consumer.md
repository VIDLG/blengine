# Private React Native iOS artifact-only consumer checkpoint

Initial date: 2026-09-27. Updated: 2026-09-28. Scope: **unsigned static iOS Simulator/device builds, signed iPhone runtime, and clean-artifact iPhone XCUITest passed** for the private, unfrozen RN/Nitro `BleHubProbe`. This is neither a public BLeng package nor an iPhone RF gate.

## Isolation and candidate

With the production Apple package already staged by an explicit preparation step, `pixi run just react-native host-ios-artifact-consumer` generates the locked Nitro bridge, then [`tools/react_native/ios_artifact_consumer.py`](../../../tools/react_native/ios_artifact_consumer.py) creates a fresh ignored `.work/react-native-ios-artifact-consumer/run-<uuid>/` directory. An explicit iOS-only file whitelist is packed into an npm tarball; the separate host installs the tarball, not the checkout integration directory. Its Metro, TypeScript, RN autolinking and Podfile resolve the installed module under the host's `node_modules`; the normal source-linked RN host is not the build directory. Android is outside this iOS-only artifact.

The passing run used the same preparation inputs; the final recipe no longer restages them automatically. The final recipe rerun retained `run-805d0b0b03b849eaaca80bf139f0e2b2` under the ignored work directory. Local tarball `blehub-react-native-internal-0.0.0.tgz` has SHA-256 `b9a45bc253be10cac79632c82aa435c6cd3bf3e561f0fd9a2f166ad70704073a`. The tarball and installed XCFramework static libraries were checked byte-for-byte by SHA-256 against the staged production package; the script does **not** rewrite native bytes to hide binary build-path metadata. This identifies a local test artifact, not a published release or a reproducible hash for future candidates. Each run preserves its own log and refuses an existing run directory.

## Checks observed

- Offline npm installation, clean host TypeScript, and RN CLI iOS autolinking resolved `@blehub/react-native-internal` and its podspec from the tarball-installed package rather than repository source.
- CocoaPods installed `BleHubReactNative`, `BleHubApple` and `BleHubUniFfi` from that installed package. The generated host xcconfig contained exactly one simulator and one device `-force_load` pointing into its installed XCFramework; the selected archives matched the staged source SHA-256. A checkout- or staging-root linker path fails closed. The generated local Xcode environment is normalized to `command -v node` and is checked for checkout-path leakage.
- Unsigned `generic/platform=iOS Simulator` and `generic/platform=iOS` builds succeeded in separate DerivedData directories. The final Debug app dylib in each passed the explicit UniFFI single-image symbol checker. Quiet build, pod, CLI, TypeScript and checker logs are retained under the run's `logs/` directory.
- The hardware-free runner tests passed 10 cases covering package whitelisting, native provenance, checkout-path isolation, incorrect/duplicate linker flags and existing-run preservation.

## Signed device runtime result

The isolated recipe passed on iPhone 14 Plus `00008110-000C61160C39401E` with Development Team `FWVYQ287W5`:

```text
pixi run just react-native host-ios-artifact-device-runtime 00008110-000C61160C39401E FWVYQ287W5
react_native_ios_artifact_consumer=device-runtime-passed
```

The tarball SHA-256 was `b9a45bc253be10cac79632c82aa435c6cd3bf3e561f0fd9a2f166ad70704073a`. The installed artifact's signed device dylib passed the single-image checker. The bounded device runtime ledger passed canonical `u64` completion (`18446744073709551615`), bytes/error round-trip, dispatch acceptance, listener removal and post-removal invalid-state behavior, and explicit close. The retained runtime log is under the ignored run directory `run-a86b09725d3a44d3aee2af67da956b24/`. No nRF fixture, Bluetooth scan, or RF command ran.

## Clean-artifact XCUITest result

The clean artifact host then passed the physical-device XCUITest recipe:

```text
pixi run just react-native host-ios-artifact-ui-test 00008110-000C61160C39401E FWVYQ287W5
react_native_ios_artifact_consumer=device-ui-test-passed
```

The passing run is retained under `run-2c5b0e3b1eeb449180cbca193cbd756c/`. XCUITest launched the installed artifact host, asserted the visible contract-lab title and `5/5` contract coverage, verified the `contract passed` state, tapped `Recreate and run`, and verified the second completed run plus the close control. This is a UI/lifecycle gate, not RF evidence. The UI mode now goes directly from signed install to XCUITest; the separate device-runtime recipe remains the marker-based native/runtime gate and is not redundantly launched first.

The artifact runner closes child stdin and creates a new process session, so Xcode/codesign cannot use the agent terminal as a password prompt. Apple login-keychain setup remains an explicit human-only prerequisite; passwords are never accepted, stored, or passed by the recipe. A black iPhone Mirroring frame during app termination or XCTest handoff is not a test oracle; the XCUITest result and its failure log are authoritative.

## Limits and next gates

This consumer is generated from a copy of the existing test host's hand-written UI/Xcode project and installed with locally cached pinned npm dependencies; the **integration module** is consumed as a packed artifact, not released to a registry. An unsigned generic build does not launch RN JavaScript, exercise physical CoreBluetooth, test signing, or prove that Nitro's JS queue drains or a real CoreBluetooth late callback is quarantined. The existing source-linked signed iPhone UI, lifecycle and nRF RF runs remain separate evidence. Android packaging/runtime and public BLeng/React Native API review also remain separate. The diagnostic DK's read-hold firmware cannot substitute for the ordinary nRF provider needed by the normal iOS RF baseline.
