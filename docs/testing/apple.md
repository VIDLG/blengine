# Apple backend development and testing

## Status and scope

Current normal-native candidate results and the remaining iPhone owner blocker are indexed in the
[native matrix report](reports/2026-10-25-native-matrix-validation.md). Earlier foundation/RF facts
below retain their named candidate scope; they are not a current-device checklist or ABI exit.

This guide implements the approved unified Apple Phase 3 in [the Plan](../../.plan/PLAN.md#phase-3统一-apple-backendmacos先行随后ios). It records execution details only; the Plan remains authoritative for public behavior, lifecycle, packaging, and exit conditions.

The maintained Apple execution host is the project Apple Silicon Mac mini running macOS 26.2 with Xcode 26.2 and Swift 6.2.3. All Apple Swift runtime, package, backend, and HIL gates run there through local Pixi + Just. The `apple-ci` name denotes a local Pixi environment, not a GitHub-hosted runner. There is no maintained Windows Swift lane, Windows-managed Swift root/bootstrap, or hosted macOS workflow. The pre-revision Stable UniFFI candidate passed macOS runtime and three-slice XCFramework/SPM isolated-consumer gates; see the [bring-up report](reports/2026-09-02-apple-production-candidate-bringup.md). Those results remain historical evidence, but the revised exported-Hub/foreign-sink production ABI is no longer wholly unrun. The current post-migration candidate passed `pixi run just ffi apple-package` for all three Apple slices and symbol checks, `pixi run just ffi swift-macos` with direct Swift backend/sink implementations across the event catalog plus concurrency/error/lifecycle cases, and the Android Kotlin binding/AAR production package build with direct language-side backend/sink support. See the [single-image production ABI migration report](reports/2026-09-03-single-image-production-abi-migration.md).

The production Swift/CoreBluetooth initialization, scan, connection-lifecycle, aggregate GATT-discovery, characteristic read/write, Notification/Indication, architecture-hardening, and RSSI slices have passed 71 deterministic macOS tests and unsigned generic iOS simulator/device builds. The latest deterministic result covers a backend-owned serial queue, state-machine/native-object correlation, one bounded shared per-connection I/O/subscription FIFO, exact read/value/state/RSSI callback routing, both write modes, CoreBluetooth notification-mode policy, native unsubscribe before teardown and rediscovery, bounded explicit fail-closed late-callback/value-route safety faults, local filtering before peripheral retention, and conservative native error mapping; see the [GATT discovery slice report](reports/2026-09-03-apple-gatt-discovery-slice.md), [characteristic I/O slice report](reports/2026-09-03-apple-gatt-io-slice.md), [Notification/Indication slice report](reports/2026-09-03-apple-notification-indication-slice.md), and [architecture-hardening/RSSI report](reports/2026-09-03-apple-architecture-hardening-rssi-slice.md). The cleaned candidate now passes the complete macOS-to-Android production RF matrix: scan, connection, aggregate GATT, characteristic I/O, PlatformDefault Notification, strict Indication, RSSI, and passive-disconnect/recovery. See the [complete macOS Android RF matrix report](reports/2026-09-05-apple-macos-android-rf-matrix.md). The project Android Peripheral has passed real macOS aggregate GATT discovery, fixture database/CCCD validation, initial characteristic read, write with response and exact readback, write without response local submission and exact delayed readback, exactly-once event ordering, Notification, strict Indication, RSSI, passive disconnect, and post-disconnect silence. The no-response terminal remains local CoreBluetooth submission only; the later readback is separate fixture-application evidence. A bundled macOS app also passed real CoreBluetooth construction, ingress, and initialization event ordering. Its first terminal-launched run reported adapter `Unavailable` and authorization `NotDetermined`; a later visible Cocoa launch through LaunchServices completed the system Bluetooth prompt and passed with `PoweredOn + Authorized`. Historical macOS BLESS/QEMU bring-up evidence is retained in the [macOS BLESS/QEMU report](reports/2026-09-02-apple-bless-qemu-macos-bringup.md), but this provider is retired and no longer a maintained Apple RF path. A connected iPhone 14 running iOS 26.2 and valid Apple Development signing identities are visible to Xcode tooling. The tracked HIL Xcode project generation and unsigned generic iOS build passed against the production package. The HIL uses a direct Swift sink and shares only a pure-Swift helper with the macOS runtime probe. The iPhone doctor and signed device runtime now pass after the explicit human-only login-keychain authorization workflow: the current iPhone 14 build was signed, installed, launched, and completed production CoreBluetooth initialization with a powered-on adapter and authorized scan/connection access. This closes the iPhone signing, installation, launch, and production ABI/runtime initialization gate, but not iPhone RF. See the [iPhone production runtime report](reports/2026-09-04-apple-ios-production-runtime.md). The current `just apple runtime-macos` ad-hoc terminal run completed the initialization path with adapter `Unavailable` and authorization `NotDetermined`; it is not the earlier `PoweredOn + Authorized` LaunchServices result and did not enter RF.

The public ABI is not frozen. The completed post-migration gates apply only to the current tested candidate; results produced before the architecture revision remain historical, and the complete applicable catalog must run again from the final maintained Apple + Android candidate before the Phase 5 freeze. Phase 5 requires macOS/iOS Swift evidence from the real Mac mini, not a Windows Swift supplement; the archived Windows backend is not an active gate.

## Execution order

The following is the bring-up/candidate sequence, not a requirement to repeat every gate daily. Choose targeted checks by the change; ordinary RF reuses an identity-checked signed artifact and existing authorization, without resetting TCC or launching UI automation.

1. On the project real Mac mini, validate the Swift toolchain selected by `xcode-select` with `pixi run just swift-doctor`. This is verification only; BleHub does not bootstrap Swift or resolve a configurable Swift root.
2. Run the Stable UniFFI production-ABI macOS runtime gate with `pixi run just ffi swift-macos`. The Swift test target consumes the `crates/blehub-uniffi` production image, implements test `BleBackend` and `BleEventSink` objects directly, and calls the exported production `BleHub` API.
3. Build and test the production XCFramework/SPM package with `pixi run just ffi apple-package`; this validates the same single native image used by runtime and HIL, but does not replace an independently isolated artifact-only consumer. Then run `pixi run just apple consumer-macos` to prepare and run the artifact-only macOS consumer against `.work/staged/apple-production` (UniFFI image/bindings) and `.work/staged/apple-backend-production` (`BleHubApple` Swift sources depending on that same image). The backend is staged separately, not imported from checkout sources or duplicated into a second native image. The consumer checks same-backend bound/disposed resource accounting and lease reuse without initializing CoreBluetooth or scanning. Packaging/receipt regressions run through `pixi run just apple test-consumer-tools`; unsigned `apple consumer-ios` compilation does not replace the explicitly authorized iPhone runtime receipt, which must uniquely identify this run and include `backend_resources=passed`.
4. Run the Swift/CoreBluetooth backend mapping/lifecycle tests and compile generic iOS destinations with `pixi run just apple check`. `AppleBleBackend` constructs and owns its private serial queue; all native state and delegate callbacks are confined to it.
5. Run the bundled real-macOS initialization probe with `pixi run just apple runtime-macos`. The probe must consume the staged production package and use a direct Swift sink. If authorization has not been decided, run `pixi run just apple authorize-macos`; it launches a visible Cocoa app through LaunchServices and waits for the system Bluetooth prompt. Because the probe is ad-hoc signed, a rebuilt or re-signed bundle may require authorization again.
6. For normal native RF, use the explicit prebuilt `rf-native-smoke` entry below with the standalone nRF52840 Multifunction Fault v2 fixture. The historical `rf-macos-android`, `rf-macos-external` and `rf-macos-nrf52840*` entries remain fail-closed; a profile or caller-supplied request does not unlock them.
7. Run `io` and `notification` as two separately identified sessions of the existing production native probe. They cover the minimum normal path, not the historical shortcut matrix, strict Indication, RSSI, passive loss/recovery, REJECT cases or stress.
8. The Notification scene uses the profile's `updates` characteristic, requires actual Notification mode, bridges native enable/disable markers, and checks post-disable silence. No Android Peripheral or radio mutation is involved.
9. Build unsigned generic iOS device and simulator consumers before attempting signing or installation.
10. Build, sign, install, and run the real iPhone HIL app through Xcode/devicectl. It must consume the staged production package, construct the exported production `BleHub` with `AppleBleBackend`, and record events through a direct Swift `BleEventSink`; it may share the pure-Swift HIL helper used by the macOS runtime probe, but a replacement Rust test component or second native image is not allowed.
11. For iPhone normal native RF, supply an already signed production HIL `.app` to `rf-native-smoke ios`; the runner never builds, signs, selects a phone implicitly or uses `--terminate-existing`.
12. The old `runtime-ios-android*`, `rf-ios-external` and `rf-ios-nrf52840*` normal shortcuts are retained as fail-closed entries without build/provider/cleanup prerequisites. Do not call the historical Python RF launch helpers directly as an alternative to v2 admission. The separately named Native Flow 12 diagnostic is not part of this normal smoke.
13. Run macOS and iPhone RF tests only after explicit authorization and fixture readiness. Host-only tests and unsigned builds do not prove RF.

For software-only recovery projection/inbox/runner checks, run
`pixi run just apple bleng-recovery-test`. It explicitly prepares the combined BLeng package and
runs the existing Swift tests as a standalone macOS XCTest bundle in the HIL project—not a second
App and not an artifact-only consumer. It does not construct a CoreBluetooth backend, boot a
simulator, install on a phone, sign or mutate radio. Each run retains a unique xcresult and logs
under `.work/apple-bleng-ios-backend/recovery-host/Results/`; success requires actual executed
passing tests with no failures, skips or expected failures. This is a targeted local software gate,
not macOS/iPhone RF or recovery-exit evidence. The scoped advertisement capture and its remaining
history/overflow limits are described in [the recovery architecture](../architecture/bleng-recovery.md#apple-recovery-advertisement按需scan-scoped-capture).

The package gate contains these native slices:

- iOS device: `arm64`;
- iOS simulator: `arm64`, `x86_64`;
- macOS: `arm64`, `x86_64`.

A generic iOS build proves that the package compiles and links for the device platform. It does not prove signing, installation, launch, CoreBluetooth authorization, or radio behavior on a physical iPhone.

The production `BleEventSink` is a foreign trait defined in `crates/blehub`; Apple applications and HIL implement it directly in Swift through the production generated API. `BleEventSink::on_event` is only a prompt handoff into caller-owned execution. It must not block on backend progress or synchronously issue commands/dispose against the same Hub; enqueue follow-up work and return first. Production uses Apple CoreBluetooth directly in the shared Swift backend. The current Swift ecosystem decision rejects high-level async/reactive BLE wrappers that would own BleHub's execution or state semantics. A pinned local-source audit also rejected adding Nordic `IOS-CoreBluetooth-Mock` to the current targets: complete-backend use requires broad CB/CBM adaptation, while its scan queue, wrapper identity, and no-response behavior are not authoritative CoreBluetooth models. Deterministic coverage continues through BleHub's focused controller fakes before generic builds and HIL; see the [Swift BLE ecosystem assessment](../research/platform/swift-ble-ecosystem-assessment.md).

## Controllers and Peripheral providers

The Mac mini Central uses the controller selected by macOS CoreBluetooth. On the current host that is the built-in Apple `BCM_4388C2` PCIe controller.

CoreBluetooth uses the controller selected by macOS and exposes no public controller-selection API. Apple RF tests therefore identify the Central controller from the host and use an independent Peripheral provider; USB enumeration alone is not RF evidence.

The maintained normal native entry uses the standalone **nRF52840 Multifunction Fault v2 + BTP** fixture only. Supply the exact installed-image contract, matching canonical profile and explicit BTP port. Android Peripheral and Linux VM/BLESS commands in historical reports are not active alternatives. See [firmware responsibilities](../research/peripheral/nrf52840-fault-firmware-v2.md).

### Explicit prebuilt native smoke

`tools/apple/native_hil.py` is one concrete host owner for the existing macOS and iOS production HIL Apps, not an RN runner or a new App. Each invocation owns **one** fresh immutable `prepare_session` request and a `DirectFixture`; live installed-image attestation happens inside that fixture's physical lease. The runner asserts the same receipt, input hashes and active lease before installation/launch and throughout coordination. Immediately before install, launch, notification value mutation and disable-marker release, it also reads `FaultController.require_idle()` and compares the current boot nonce with the pinned boot. Marker/result polling does not repeatedly query firmware limits. Missing artifact, device, port, contract, profile or live proof fails closed; no implicit build, tool installation, firmware flash/reset or radio recovery fallback exists. `DirectFixture.start` reinitializes the attested dynamic GATT profile and starts advertising; it does not flash firmware.

After separately authorized RF preparation, invoke with literal local paths/identities (shell variables below stand for those explicit values):

```sh
pixi run just apple rf-native-smoke ios "$IOS_UDID" "$BTP_PORT" "$CONTRACT" "$PROFILE" "$SIGNED_IOS_APP" io
pixi run just apple rf-native-smoke ios "$IOS_UDID" "$BTP_PORT" "$CONTRACT" "$PROFILE" "$SIGNED_IOS_APP" notification

pixi run just apple rf-native-smoke macos "$MAC_HOSTNAME" "$BTP_PORT" "$CONTRACT" "$PROFILE" "$MACOS_APP" io
pixi run just apple rf-native-smoke macos "$MAC_HOSTNAME" "$BTP_PORT" "$CONTRACT" "$PROFILE" "$MACOS_APP" notification
```

- `MAC_HOSTNAME` must exactly equal this Mac's `socket.gethostname()`; iOS requires a canonical physical UDID and an available, paired, unlocked phone. The native per-device flock alone is only lane-local. On iOS the runner additionally acquires the exact atomic directory lock used by [`run_ios_rf_scan`](../../tools/react_native/run_ios_rf_scan.py), `.work/peripheral/nrf52840/rn-ios-scan.lock`, for its whole attempt (including cleanup); either owner rejects an existing directory and neither adopts/deletes the other's owner. This RN lock is host-wide, not per UDID. [`IosRadioLease`](../../tools/react_native/ios_radio_lease.py) has a separate, restoration-bound lifecycle that is not safely reusable as a plain smoke lease: native only checks its canonical per-UDID owner path and `.restoration-pending` before preflight and subsequent SDK/mutation actions, rejecting any existing entry without changing it. It does **not** reserve the radio namespace; a radio acquisition can race an absence check. `result.json.device_exclusion` records the shared RN lock ownership/release and this check-only boundary. RN tools not using that directory and arbitrary Apps remain outside the exclusion. This is not a global phone/radio lock: do not schedule other Central/radio work concurrently.
- App bundles must already exist, contain no symlinks, and contain the selected `BleHubAppleMacOSProbe` / `BleHubAppleIOSProbe` executable and declare `BleHubNativeHilProtocolVersion = 2` in their Info.plist; an older prebuilt App is rejected before SDK or serial access. They are copied into the attempt and fingerprinted; no checkout build fallback runs. iOS needs a separately development-signed artifact with the opt-in native ownership helper. macOS launches the exact snapshotted executable directly and requires prior Bluetooth authorization; it does not silently switch to LaunchServices or repair TCC.
- Both scenes explicitly pass the immutable canonical profile's `local_name` through `--scan-name-prefix` into the existing Swift `ScanOptions.namePrefix`, alongside the single service UUID filter. The native result must echo `scan_filter=service+name` and the exact prefix; no name is hardcoded and no public ABI changes. Protocol v2 rejects older prebuilt Apps lacking this combined-filter contract. `io` is scan → connect → discover → initial read → response-write/readback (`10`) → no-response local submission/readback (`ab`) → disconnect → dispose/post-dispose silence. `notification` separately scans/connects/discovers, enables actual Notification, triggers the profile value via the fixture's single BTP owner, disables, bridges the silence window, disconnects and disposes. Native event-order/ledger assertions remain in the existing probes; no events are synthesized by the host.
- Each attempt has a fresh App boot UUID, SDK/child PID, bundle ID and exact executable identity. iOS cleanup additionally requires a fresh challenge response from that boot and a matching current SDK process row. Failed launches with unknown PID or changed boot/executable are reported as uncertain cleanup, never guessed or globally stopped. macOS cleanup only reaps/stops the `Popen` child owned by this attempt. The fixture's v2 boot nonce is pinned before startup mutations and checked at each host mutation boundary as well as verdict/cleanup. A changed/unproved boot is closed at the transport/lease boundary without issuing cleanup mutations to the new boot. Otherwise the fixture is retired through its own lease and serial session, with peer-idle and advertising-stop checks; no provider-wide stop runs.
- Successful iPhone result pages stay visible by default (`success_app=keep`). When explicitly authorized to close between scenes, append `close-owned` as the final Just argument (CLI `--success-app close-owned`) for the intermediate iOS run. Cleanup still requires this run's identity, fresh boot challenge and matching current PID/executable, then confirms termination; it never adopts a pre-existing App or closes an unknown boot. A failed authorized close fails the host result. The final scene should omit the option so its successful page remains visible. Each scene retains its own run/boot/fixture session; this is not a universal batch/cleanup runner. A successful App marker alone is not host Passed: the host also requires its marker bridge, complete result assertions, Peripheral peer retirement and successful cleanup.
- Runtime is bounded by a 240-second whole-session budget (CLI `--timeout` accepts 1–300), bounded SDK/BTP operations and separate native/fixture cleanup bounds. Full SDK/build output and the attempt's `result.txt`, `result.json`, immutable inputs, live receipt and native identity stay under ignored `.work/apple-native-hil/`. Admitted attempts additionally record `failure.stage` (artifact/ownership/device/fixture/install/launch/runtime/result/retirement), independent `cleanup_failures`, launch/identity observations and termination attempted/confirmed. Stage is not a diagnosis of TCC, signing or BLE root cause. `primary_error` and `cleanup_errors` remain separate; evidence-publication failure is attached to an existing primary rather than replacing it. Normal smoke never mutates system permissions or radio; UI automation is explicitly not checked or required.

Hardware-free validation (no App launch or serial/device commands):

```sh
pixi run -e apple-ci python -m unittest tools.tests.react_native.test_apple_native_hil -v
pixi run just apple test-rf-host
```

The new tests are also included by the existing canonical `react-native tools-test` / `tools-fixture-test` discovery. The original macOS fail-closed/profile safety regressions remain in `test-rf-host`. Swift probe-only compilation can use the existing staged production package without rebuilding Rust or accessing devices:

```sh
pixi run -e apple-ci python -m tools.apple.test_macos_runtime build
pixi run -e apple-ci python -m tools.apple.test_ios_runtime build-unsigned
```

These host-only and build checks do not establish installation, CoreBluetooth authorization, physical marker delivery, RF success or native Phase/ABI exit.


Before any RF run, report the selected Apple DUT, fixture provider, expected duration, and exclusive resources, then wait for explicit confirmation that the fixture is ready. A missing or misconfigured fixture is an infrastructure result, not a backend failure.

## Signing and physical iPhone gates

### Scoped read-only preflight

[`ios_preflight.py`](../../tools/apple/ios_preflight.py) separates inspection from preparation and execution. The maintained doctor has **no package/build prerequisites**, does not generate a project, and does not install tools, sign an App, unlock Keychain, start XCUITest, launch an App, reset permissions or switch radio:

```sh
pixi run just apple ios-device-doctor
pixi run just apple ios-device-doctor prebuilt "$SIGNED_IOS_APP"
pixi run just apple ios-device-doctor prebuilt "$SIGNED_IOS_APP" "$IOS_UDID"
# UI-test preflight: also pass the exact xctestrun before a UI gate.
pixi run just apple ios-device-doctor prebuilt "$SIGNED_IOS_APP" "$IOS_UDID" "$XCTESTRUN"
```

| Mode | Required read-only observations | Deliberately not proved |
|---|---|---|
| `build-signing` (default) | Selected Xcode/SDK/Swift tools, version-checked XcodeGen, explicitly configured Team, visible matching Apple Development identity | Actual private-key usability, a successful signature/build, TCC, UI automation or RF |
| `prebuilt` | Existing unlinked physical iPhoneOS `.app`, actual content fingerprint, signature verification and Info/signer identity, actual signed App ID/team/`get-task-allow` compatibility with an unexpired development profile, unchanged fingerprint after inspection; when an exact xctestrun is supplied, Developer Mode and same-family stale/duplicate XCTest runner inventory | Build-tool setup, Keychain private-key access, App installation/runtime, TCC, actual UI test execution or RF |

Only an explicit device argument requests profile membership (in prebuilt mode) and existing bounded read-only device availability/lock-state queries. Supplying an exact xctestrun additionally requires that device and performs read-only Developer Mode and same-App-family XCTest runner inventory checks; it does not install or remove a runner. Omitted or empty device means **no auto-selection and no device query**. Prebuilt inspection does not require XcodeGen, configured signing Team or an unlocked signing Keychain; `security cms` only decodes the embedded profile. Hashes are computed from actual artifact contents, never compared with a hardcoded candidate hash.

The profile may authorize a wildcard App ID (for example, `PREFIX.*` or `PREFIX.dev.example.*`); the signed App's `application-identifier` must still exactly equal the profile's App ID prefix plus the full Info bundle ID. The prefix is read from the profile, not assumed to equal the signing Team. Signed/profile Team and development `get-task-allow=true` must also match. Malformed or out-of-scope wildcards are rejected. This follows [Apple TN2415](https://developer.apple.com/library/archive/technotes/tn2415/_index.html); it checks these three entitlements, not every capability or the OS's complete installation policy.

The JSON report records this invocation's timestamps and `verified / blocked / not-checked` observations. Exit 0 means only all **required observations for this mode** were verified, not that signing, UI automation execution or BLE is ready. Private-key access, CoreBluetooth TCC, actual UI test execution and RF remain explicit `not-checked`; no result is a cached permission grant. No automatic repair or retry follows a blocked observation. The legacy Python `doctor` CLI delegates to this same owner; its mode/App/device options are rejected for runtime/setup commands. The implementation's software-only validation and explicit unproved device boundaries are recorded in the [preflight/permission tooling report](reports/2026-10-25-ios-preflight-permission-optimization.md).

For an authorized two-scene workflow, for example:

```sh
pixi run just apple rf-native-smoke ios "$IOS_UDID" "$BTP_PORT" "$CONTRACT" "$PROFILE" "$SIGNED_IOS_APP" io close-owned &&
pixi run just apple rf-native-smoke ios "$IOS_UDID" "$BTP_PORT" "$CONTRACT" "$PROFILE" "$SIGNED_IOS_APP" notification
```

`close-owned` authorizes only the first invocation's successful, identity-verified App exit. It grants no permission reset/revoke, radio mutation, firmware provisioning, unknown-process termination or stale-owner recovery. The second command must not run if the first fails. Ordinary single-scene invocation keeps the result page by default.

### Explicit preparation and physical evidence

Use Xcode and `xcrun devicectl` as the primary build, signing, installation, launch, and device-log path. Never write Apple account credentials, certificate private keys, provisioning profiles, device pairing records, or team-specific secrets into tracked project files.

Before recording a physical-device pass, capture:

- Xcode, Swift, macOS, iOS, device model, and architecture;
- selected development team and signing mode without recording secrets;
- package/candidate identity and contract version;
- installation and launch result;
- Bluetooth authorization and `CBCentralManager` state sequence;
- Peripheral profile ID and advertised service UUID;
- executed command/event matrix and cleanup evidence.

For a new signed build, the explicitly configured Team needs a matching Apple Development identity and access to its private key. The `build-signing` doctor correlates each currently valid identity fingerprint with its public certificate and compares the certificate subject's Team OU (`2.5.4.11`); the parenthesized suffix of the human-readable Common Name is **not** assumed to be the Team ID. Public certificates without a matching valid identity cannot pass this check. The shared owner is [`signing_identity.py`](../../tools/apple/signing_identity.py); it reads public material only and never exports a private key. This follows the OU-based team extraction in [Apple Security source](https://github.com/apple-oss-distributions/security/blob/main/securityd/src/clientid.cpp).

The `build-signing` doctor **does not prove private-key access** and never invokes a build. An already signed prebuilt App does not require unlocking that signing key again merely to inspect or run it. Read-only local diagnostics include:

```text
security find-identity -v -p codesigning
defaults read com.apple.dt.Xcode IDEProvisioningTeamByIdentifier
```

If the selected Team has no visible matching Apple Development identity, the build-signing preflight is blocked even when Xcode remembers the account. This does not invalidate an independently signed prebuilt App. In Xcode Settings > Apple Accounts,
select the account for that Team and use Manage Certificates to create or
download an Apple Development certificate; the corresponding private key must
be installed in the login keychain. Unlock the keychain and authorize the
signing tools once with `pixi run just apple unlock-login-keychain`, then rerun
`pixi run just apple ios-device-doctor`. Do not copy certificates, private keys,
account credentials, or Team-specific values into tracked files.

### BLeng artifact runtime (non-RF)

`consumer-bleng-macos` runs the installed two-component package through the concrete typed handles. `consumer-bleng-ios-unsigned` compiles the iPhone/Simulator consumer, including the same platform-neutral `TypedOperations.swift` fixture used by macOS; sharing authored test source does not add checkout-relative native headers or libraries. These fixtures do not instantiate CoreBluetooth or require Bluetooth permission.

For an explicitly selected, prepared iPhone, `consumer-bleng-ios-device` builds/signs and runs that existing consumer. It checks current readiness/unlocked state before signing, refuses an already-running consumer before install and launch, and neither uses `--terminate-existing` nor retries a failed launch automatically. Success requires one current run ID, `typed_operations=passed` and the final App pass marker; an older initialize/scan-only result is insufficient. Successful pages remain visible. Missing signing identity or private-key access is a setup block, not a BLE failure or permission grant; no password collection, automatic Keychain unlock or XCUITest is involved.

Current candidate results, preserved failures and unverified device boundaries are recorded in the [core functional-convergence report](reports/2026-10-25-core-functional-convergence.md). macOS evidence never proves iOS. Simulator evidence never proves CoreBluetooth RF behavior.
