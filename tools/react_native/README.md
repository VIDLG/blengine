# RN host tools

This directory contains executable host tooling and its live implementation, not synthetic test fixtures.

- **Managed scenes run in process on both platforms.** `scene_plans` owns the step vocabulary,
  `scene_native` owns phases, host actions, ledgers and every observation derivation, and the two
  executors only differ in the driver: `scene_ios_native` runs the App's own XCTest target and
  `scene_android_native` its own instrumented test. `bleng_scan_runner.run_prepared_scene` is the
  single envelope (admission, candidate binding, fixture/phone guards, cleanup order, publication)
  and `SceneDriver` is the seam; no UI-automation server, helper APK or WebDriver appears anywhere
  in the path.
- **Producers** freeze the candidate, acquire the phone and fixture lease, install/launch and seal
  the bundle: `bleng_android_scan` and `bleng_ios_scan`, exposed by
  `run_bleng_android_scan` / `run_bleng_ios_scan` (`--scene` selects the managed scene).
- **Evidence** is verified offline by `bleng_scan_evidence` (`verify_scan_bundle`), which also
  owns the release-shaped APK inventory; `evidence_io` owns the bounded file/stream mechanics both
  the verifiers and the lease journals use.
- **Scene targets** (`scene_targets`) derive the characteristic, mode and write payload from the
  leased fixture profile, so no scene declares a UUID, a mode or a byte of its own.
- **Non-scene instrumentation lanes** (multi-link, adapter transition, permission transition,
  native notification) drive the formal App's Nitro surface directly; they own no page.


- Formal iOS scan candidate: `bleng_ios_candidate.IosCandidateOwner` owns one explicitly leased frozen-device-App install/launch and binds its receipts to the existing fresh native identity check. It rejects running old Apps, has no terminate/retry/console fallback, and permanently revokes provenance after detected drift. Its one-shot App finalizer composes with the existing observation lease: retain on success, fresh-identity-checked single PID termination on failure, quarantine uncertainty, and preserve primary/cleanup/publication errors. Cleanup has a separate deadline without reviving the scenario; PID-only termination is not an atomic boot-conditioned operation. This is not a CLI or a complete RF runner: the scene driver, preflight, cross-resource finalization order, live cleanup validation and the final bundle are what turn it into one. See [candidate evidence and limits](../../docs/testing/reports/2026-11-04-formal-host-app-identity.md).
- Formal Android scan candidate: `bleng_android_candidate.AndroidCandidateOwner` validates a frozen APK with the pinned SDK, admits only single-user-0/non-shared-UID ownership, installs and cold-launches once through the shared guarded installer with an explicit root choice and no grants/fallback, and binds the installed hash plus original OS process identity. Its App-only finalizer composes with `AndroidRadioController.finish_app_observation`, not the old radio ON finalizer: success retains the App; failed or uncertain cleanup quarantines the lock. Package force-stop is not atomic with identity checks. A scoped managed-lifecycle device candidate now passed; it is not a scan/RF or complete outer-bundle verdict. See the [real lifecycle evidence](../../docs/testing/reports/2026-11-04-formal-android-managed-lifecycle.md).
- Formal iOS compile: `bleng_ios_consumer` owns installed Podspec validation, the host's Nitro-free `PlatformContract` static target, a finite local RN CLI bundle for the native RCTHost reload entry, and its App/native-test build. It validates the packaged XCTest bundle resource against the generated JS hash; bundling starts no Metro server and has no dependency-install fallback. Use `formal-host-ios-build [false|true]`; it is source/local-package build evidence, not clean npm-artifact or runtime/RF evidence. There is no separate static Nitro consumer or second Node/Pod root.
### Formal iOS Pods preparation boundary

`formal-host-ios-build` accepts one positional Just argument: omitted or `false` is the ordinary path; `true` is the only refresh path and passes Python `--refresh-pods`.

```sh
pixi run just react-native formal-host-ios-build       # false/default: verify locally
pixi run just react-native formal-host-ios-build true  # explicit refresh
```

Ordinary mode verifies `.work/react-native-bleng-ios-consumer/pods-preparation.json` and the installed-graph JSON without `pod ipc`, `pod install`, or RN artifact probes. Missing or stale evidence fails closed: there is no adoption or fallback. Explicit refresh invalidates the receipt before installation and seals it only after a successful installation and stable snapshots. When both host-local RN Core and RN Dependencies archives are complete, refresh binds both local inputs together so RN does not perform remote HEAD probes; a partial cache is not adopted. Source/native byte changes are accepted, but metadata, codegen, schema, dependency-membership or captured-environment drift requires refresh.

The owned refresh process uses process-group deadlines: 240 seconds for install and 120 seconds per snapshot. Snapshot stdout and stderr share a 16 MiB bound; each log is capped at 256 MiB in a unique private directory. The scoped bare-curl vocabulary enforces a 6-second connect timeout, 12-second maximum for `HEAD`, 180-second maximum for `GET`, and zero retries. Absolute Ruby/downloader network work remains bounded by the parent deadline; the shim does not impose a separate network timeout.

This is a narrow, read-only CocoaPods local snapshot, not universal dependency analysis or complete resource-integration proof: it captures resource drift, not an exhaustive initial resource mapping. Vendored XCFramework metadata is hashed semantically, because both the Apple package owner and React Native's prebuilt Core pod can reassemble a framework with different slice order while every field and value stays identical; a changed field still invalidates the receipt. The host Podfile rejects a requested prebuilt React Native source fallback. The current candidate passed both explicit-refresh and ordinary full builds; see the [candidate report](../../docs/testing/reports/2026-11-11-ios-pod-preparation-boundary.md). Those results do not claim fixed network behavior or RF/release passage.

- `rf_fixture` / `host_paths` own host-side fixture and installation/PID checks; the provider/session remains the live attestation authority.
- `capture_io` owns bounded streams and local child teardown without scenario facts. Scenario evidence modules own facts, ACK policy and semantic checks.
- `evidence_io` owns read-only host file/stream mechanics: symlink refusal, size bounds, open-then-verify identity, read-time change detection and strict duplicate-key JSON. It owns no artifact policy or scenario vocabulary.
- `ios_radio_control` owns the iOS radio-control observation vocabulary and its exact-schema audit; `ios_radio_lease` / `ios_radio_restoration` own the phone owner and restoration proof. A released lease is cleanup evidence, not an RF or bundle verdict.
- `run_ios_rf_scan` remains the single-flow iOS diagnostic entry (its process lock is reused by `tools/apple/native_hil`).
- The RF-lab suite graph (`run_rf_lab_full` / `rf_scenario_runner` / pending-read gate / iOS adapter-startup candidate / `rf_evidence_bundle`) and the nRF52840 BTP coordination modes that fed it were retired on 2026-11-22: no maintained recipe passed `--coordination-mode` for them, and the formal managed lanes build evidence through `run_bleng_*_scan --scene ...`. See the [retirement report](../../docs/testing/reports/2026-11-22-react-native-internal-retirement.md).
- The RN surface is **Tests** (category → scenario → setup) → **Current run** (one mounted scenario, preserved across view changes) → **Evidence** (technical identity, App failure and coverage limits). Ordinary workbench launch creates no Probe and dispatches no BLE; explicit `basic` remains the ABI smoke route. Host-selected launches open their existing scenario, not a second execution path.
- Host tools own fixture/radio control, RF checks, cleanup and evidence. The App does not receive the final host physical/cleanup/bundle verdict; App Passed cannot stand in for host-gate Passed.
- Adapter-transition and permission-transition run as formal-App native gates: `formal-host-android-native-adapter-transition`, `formal-host-android-native-permission-transition` and `formal-host-ios-permission-authorization`. Each owns its own scenario tool under `tools/react_native/bleng_*`; see the [2026-11-22 reports](../../docs/testing/reports/2026-11-22-adapter-transition-formal-app.md).

The iOS authorization-only gate lives in the formal host's own UI test target: `pixi run just react-native formal-host-ios-permission-authorization --device <udid> --xctestrun <manifest> --output <dir> --authorize-permission-reset`. It consumes the signed App + manifest from `formal-host-ios-device-test-build`, removes the App for each phase so the real system consent alert appears, answers `deny` then `allow`, and requires one exact xcresult pass per phase. It is explicitly **non-RF** and separate from Android's process-death gate. The earlier internal-host checkpoint remains historical evidence: [2026-10-17 report](../../docs/testing/reports/2026-10-17-rn-ios-permission-authorization.md); the migration is recorded in [2026-11-22 report](../../docs/testing/reports/2026-11-22-ios-permission-authorization-formal-ui.md).

The opt-in iOS radio-control feasibility test also stays in that UI target; it is Settings-control feasibility, not an iOS adapter RF gate or a new App/Python runner. Initial ON, narrow accessibility selection, readback, restoration and unchanged native App identity are required; `inspect-only=1` cannot toggle or prove control. The [attempt history](../../docs/testing/reports/2026-10-17-rn-ios-radio-control-feasibility.md) preserves earlier initialization/navigation failures and a later non-RF ON→OFF→ON/same-native-App pass. Persistent result-page visibility after XCTest exit was not proved.

Tests live in [`../tests/react_native/`](../tests/react_native/). Choose **one** hardware-free Python entry below; neither needs devices, SDK installation, npm or Pods:

```text
# RN tooling-only subset:
pixi run just react-native tools-test
# OR combined tooling + provider + Android permission/radio controller tests:
pixi run just react-native tools-fixture-test
```

The combined command includes the entire `tools-test` subset in one process. Do not run the subset again after the combined command. Synthetic support must never be imported by a live runner/provider or counted as RF evidence. Ordinary RF runs never flash firmware.

For the formal host's Jest suites, after host npm dependencies are installed, run:

```text
pixi run just react-native formal-host-test
```

`formal-host-test` calls the host package's `test` script (`jest --runInBand`) once using the existing Jest configuration. Jest results are not native runtime, device or RF evidence.

See the [ownership, support design and consolidation limits](../../docs/research/react-native/react-native-host-tooling-architecture.md). The core restructuring is complete; further generic tool/framework work is deferred unless a product requirement or observed failure justifies it. Stable CLI names do not imply that a CLI module should own shared implementation.
