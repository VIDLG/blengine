# Apple backend development and testing

## Status and scope

This guide implements the approved unified Apple Phase 3 in [the Plan](../../.plan/PLAN.md#phase-3统一-apple-backendmacos先行随后ios). It records execution details only; the Plan remains authoritative for public behavior, lifecycle, packaging, and exit conditions.

The maintained Apple execution host is the project Apple Silicon Mac mini running macOS 26.2 with Xcode 26.2 and Swift 6.2.3. All Apple Swift runtime, package, backend, and HIL gates run there through local Pixi + Just. The `apple-ci` name denotes a local Pixi environment, not a GitHub-hosted runner. There is no maintained Windows Swift lane, Windows-managed Swift root/bootstrap, or hosted macOS workflow. The pre-revision Stable UniFFI candidate passed macOS runtime and three-slice XCFramework/SPM isolated-consumer gates; see the [bring-up report](reports/2026-09-02-apple-production-candidate-bringup.md). Those results remain historical evidence, but the revised exported-Hub/foreign-sink production ABI is no longer wholly unrun. The current post-migration candidate passed `pixi run just ffi apple-package` for all three Apple slices and symbol checks, `pixi run just ffi swift-macos` with direct Swift backend/sink implementations across the event catalog plus concurrency/error/lifecycle cases, and the Android Kotlin binding/AAR production package build with direct language-side backend/sink support. See the [single-image production ABI migration report](reports/2026-09-03-single-image-production-abi-migration.md).

The production Swift/CoreBluetooth initialization, scan, connection-lifecycle, aggregate GATT-discovery, characteristic read/write, Notification/Indication, architecture-hardening, and RSSI slices have passed 71 deterministic macOS tests and unsigned generic iOS simulator/device builds. The latest deterministic result covers a backend-owned serial queue, state-machine/native-object correlation, one bounded shared per-connection I/O/subscription FIFO, exact read/value/state/RSSI callback routing, both write modes, CoreBluetooth notification-mode policy, native unsubscribe before teardown and rediscovery, bounded explicit fail-closed late-callback/value-route safety faults, local filtering before peripheral retention, and conservative native error mapping; see the [GATT discovery slice report](reports/2026-09-03-apple-gatt-discovery-slice.md), [characteristic I/O slice report](reports/2026-09-03-apple-gatt-io-slice.md), [Notification/Indication slice report](reports/2026-09-03-apple-notification-indication-slice.md), and [architecture-hardening/RSSI report](reports/2026-09-03-apple-architecture-hardening-rssi-slice.md). The cleaned candidate now passes the complete macOS-to-Android production RF matrix: scan, connection, aggregate GATT, characteristic I/O, PlatformDefault Notification, strict Indication, RSSI, and passive-disconnect/recovery. See the [complete macOS Android RF matrix report](reports/2026-09-05-apple-macos-android-rf-matrix.md). The project Android Peripheral has passed real macOS aggregate GATT discovery, fixture database/CCCD validation, initial characteristic read, write with response and exact readback, write without response local submission and exact delayed readback, exactly-once event ordering, Notification, strict Indication, RSSI, passive disconnect, and post-disconnect silence. The no-response terminal remains local CoreBluetooth submission only; the later readback is separate fixture-application evidence. A bundled macOS app also passed real CoreBluetooth construction, ingress, and initialization event ordering. Its first terminal-launched run reported adapter `Unavailable` and authorization `NotDetermined`; a later visible Cocoa launch through LaunchServices completed the system Bluetooth prompt and passed with `PoweredOn + Authorized`. Historical macOS BLESS/QEMU bring-up evidence is retained in the [macOS BLESS/QEMU report](reports/2026-09-02-apple-bless-qemu-macos-bringup.md), but this provider is retired and no longer a maintained Apple RF path. A connected iPhone 14 running iOS 26.2 and valid Apple Development signing identities are visible to Xcode tooling. The tracked HIL Xcode project generation and unsigned generic iOS build passed against the production package. The HIL uses a direct Swift sink and shares only a pure-Swift helper with the macOS runtime probe. The iPhone doctor and signed device runtime now pass after the explicit human-only login-keychain authorization workflow: the current iPhone 14 build was signed, installed, launched, and completed production CoreBluetooth initialization with a powered-on adapter and authorized scan/connection access. This closes the iPhone signing, installation, launch, and production ABI/runtime initialization gate, but not iPhone RF. See the [iPhone production runtime report](reports/2026-09-04-apple-ios-production-runtime.md). The current `just apple runtime-macos` ad-hoc terminal run completed the initialization path with adapter `Unavailable` and authorization `NotDetermined`; it is not the earlier `PoweredOn + Authorized` LaunchServices result and did not enter RF.

The public ABI is not frozen. The completed post-migration gates apply only to the current tested candidate; results produced before the architecture revision remain historical, and the complete applicable catalog must run again from the final Windows + Apple + Android candidate before the Phase 5 freeze. Phase 5 requires macOS/iOS Swift evidence from the real Mac mini, not a Windows Swift supplement.

## Execution order

Run Apple development in this order:

1. On the project real Mac mini, validate the Swift toolchain selected by `xcode-select` with `pixi run just swift-doctor`. This is verification only; BleHub does not bootstrap Swift or resolve a configurable Swift root.
2. Run the Stable UniFFI production-ABI macOS runtime gate with `pixi run just ffi swift-macos`. The Swift test target consumes the `crates/blehub-uniffi` production image, implements test `BleBackend` and `BleEventSink` objects directly, and calls the exported production `BleHub` API.
3. Build and test the production XCFramework/SPM package with `pixi run just ffi apple-package`; this validates the same single native image used by runtime and HIL, but does not replace an independently isolated artifact-only consumer. Then run `pixi run just apple consumer-macos` to build and launch the clean macOS consumer from `.work/staged/apple-production` only.
4. Run the Swift/CoreBluetooth backend mapping/lifecycle tests and compile generic iOS destinations with `pixi run just apple check`. `AppleBleBackend` constructs and owns its private serial queue; all native state and delegate callbacks are confined to it.
5. Run the bundled real-macOS initialization probe with `pixi run just apple runtime-macos`. The probe must consume the staged production package and use a direct Swift sink. If authorization has not been decided, run `pixi run just apple authorize-macos`; it launches a visible Cocoa app through LaunchServices and waits for the system Bluetooth prompt. Because the probe is ad-hoc signed, a rebuilt or re-signed bundle may require authorization again.
6. With a ready Android provider, run `pixi run just apple rf-macos-android`; this is the maintained macOS RF matrix and executes scan, connection, GATT, I/O, PlatformDefault Notification, strict Indication, RSSI, and passive-disconnect/recovery in one bounded fixture lifecycle. Keep the graphical login session unlocked; a locked session can prevent the ad-hoc probe from receiving CoreBluetooth/TCC initialization.
7. For a single bounded diagnostic case, pass one scenario instead of `all`: `scan`, `connection`, `gatt`, `io`, `notification`, `indication`, `rssi`, or `passive-disconnect`. Do not promote a single case as a complete matrix pass.
8. The Notification case uses Android `FFF2...` and requires actual Notification mode. The Indication case uses the same profile's `FFF50000...` `read + indicate` characteristic and the same unified `android-indicate` control; the fixture routes by the active Indication CCCD.
9. Build unsigned generic iOS device and simulator consumers before attempting signing or installation.
10. Build, sign, install, and run the real iPhone HIL app through Xcode/devicectl. It must consume the staged production package, construct the exported production `BleHub` with `AppleBleBackend`, and record events through a direct Swift `BleEventSink`; it may share the pure-Swift HIL helper used by the macOS runtime probe, but a replacement Rust test component or second native image is not allowed.
11. With the Android Peripheral ready and the iPhone kept unlocked, run `pixi run -e apple-ci just apple runtime-ios-android` from the interactive macOS Terminal. This starts the Android fixture, runs the signed iPhone Central scan/connect/GATT/read/write/disconnect scenario, and stops the fixture through a cleanup trap. The command consumes the same production package as `runtime-ios-device`; it is not an Android backend test.
12. Run iPhone Central RF against the project Android Peripheral with `pixi run -e apple-ci just apple runtime-ios-android`. When the nRF52840 DK is available, add its standalone Peripheral control workflow without changing the Apple backend or production ABI.
13. Run macOS and iPhone RF tests only after the selected Peripheral passes preflight.

The package gate contains these native slices:

- iOS device: `arm64`;
- iOS simulator: `arm64`, `x86_64`;
- macOS: `arm64`, `x86_64`.

A generic iOS build proves that the package compiles and links for the device platform. It does not prove signing, installation, launch, CoreBluetooth authorization, or radio behavior on a physical iPhone.

The production `BleEventSink` is a foreign trait defined in `crates/blehub`; Apple applications and HIL implement it directly in Swift through the production generated API. `BleEventSink::on_event` is only a prompt handoff into caller-owned execution. It must not block on backend progress or synchronously issue commands/dispose against the same Hub; enqueue follow-up work and return first. Production uses Apple CoreBluetooth directly in the shared Swift backend. The current Swift ecosystem decision rejects high-level async/reactive BLE wrappers that would own BleHub's execution or state semantics. A pinned local-source audit also rejected adding Nordic `IOS-CoreBluetooth-Mock` to the current targets: complete-backend use requires broad CB/CBM adaptation, while its scan queue, wrapper identity, and no-response behavior are not authoritative CoreBluetooth models. Deterministic coverage continues through BleHub's focused controller fakes before generic builds and HIL; see the [Swift BLE ecosystem assessment](../research/swift-ble-ecosystem-assessment.md).

## Controllers and Peripheral providers

The Mac mini Central uses the controller selected by macOS CoreBluetooth. On the current host that is the built-in Apple `BCM_4388C2` PCIe controller.

CoreBluetooth uses the controller selected by macOS and exposes no public controller-selection API. Apple RF tests therefore identify the Central controller from the host and use an independent Peripheral provider; USB enumeration alone is not RF evidence.

Use these Peripheral providers:

| Provider | Apple development use | Boundary |
|---|---|---|
| Project Android fixture | Immediate macOS/iPhone scan, connect, GATT, read/write, Notification, Indication, disconnect, and recovery development | Does not replace BLESS cross-stack or Linux controller-fault evidence |
| Linux VM + BlueZ/BLESS | Authoritative programmable BLESS fault, controller-loss, recovery, and soak provider | Windows-hosted QEMU control plane is retained; its USB controller is machine-configured and nRF52840 Dongle HCI integration is a future hardware gate |
| Standalone nRF52840 hardware | Cross-stack reference Peripheral for Apple/Windows Central RF | DK and Dongle firmware/control protocol are hardware-specific and must be validated when devices arrive |

Prepare the Android fixture with:

```sh
pixi run just peripheral android-start
pixi run just peripheral android-status
```

Trigger deterministic values with:

```sh
pixi run just peripheral android-notify deadbeef
pixi run just peripheral android-indicate deadbeef
```


Before any RF run, report the selected Apple DUT, fixture provider, expected duration, and exclusive resources, then wait for explicit confirmation that the fixture is ready. A missing or misconfigured fixture is an infrastructure result, not a backend failure.

## Signing and physical iPhone gates

Use Xcode and `xcrun devicectl` as the primary build, signing, installation, launch, and device-log path. Never write Apple account credentials, certificate private keys, provisioning profiles, device pairing records, or team-specific secrets into tracked project files.

Before recording a physical-device pass, capture:

- Xcode, Swift, macOS, iOS, device model, and architecture;
- selected development team and signing mode without recording secrets;
- package/candidate identity and contract version;
- installation and launch result;
- Bluetooth authorization and `CBCentralManager` state sequence;
- Peripheral profile ID and advertised service UUID;
- executed command/event matrix and cleanup evidence.

The iPhone doctor validates both sides of the signing boundary before invoking
`xcodebuild`: the selected non-Personal Xcode Team must have a matching Apple
Development identity in the default login keychain. Check the local state with:

```text
security find-identity -v -p codesigning
defaults read com.apple.dt.Xcode IDEProvisioningTeamByIdentifier
```

If the selected Team is absent from the `security` output, signing cannot work
even when Xcode remembers the paid account. In Xcode Settings > Apple Accounts,
select the account for that Team and use Manage Certificates to create or
download an Apple Development certificate; the corresponding private key must
be installed in the login keychain. Unlock the keychain and authorize the
signing tools once with `pixi run just apple unlock-login-keychain`, then rerun
`pixi run just apple ios-device-doctor`. Do not copy certificates, private keys,
account credentials, or Team-specific values into tracked files.

macOS evidence never proves iOS. Simulator evidence never proves CoreBluetooth RF behavior.
