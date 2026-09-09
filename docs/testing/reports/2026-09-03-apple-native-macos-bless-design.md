# Native macOS BLESS Peripheral design

Date: 2026-09-03

## Decision

Apple development keeps the complete Apple Silicon QEMU/Debian/BlueZ/BLESS control path alongside the validated Windows-hosted path, but native macOS BLESS remains a separate CoreBluetooth provider that never uses UB500. The Mac mini uses its system-selected built-in controller for BleHub CoreBluetooth Central work and for the native BLESS/CoreBluetooth Peripheral fixture used by an iPhone Central. The QEMU path may use UB500 only after guest HCI and BlueZ readiness pass.

The approved provider topology is:

- macOS BleHub Central -> project Android Peripheral;
- iPhone BleHub Central -> project Android Peripheral;
- iPhone BleHub Central -> native macOS BLESS/CoreBluetooth Peripheral;
- Windows or macOS managed-QEMU path -> Linux/BlueZ/BLESS + UB500 for controller fault injection, recovery, and soak, when that host's passthrough gate is ready.

A native macOS BLESS process is not promoted as the same-Mac Central's independent RF provider. It primarily provides a second implementation for iPhone Central testing.

## Verified upstream mechanism

The project-locked official BLESS `0.3.0` wheel was inspected. On `sys.platform == "darwin"`, BLESS selects `BlessServerCoreBluetooth` from `bless.backends.corebluetooth.server`. Its server owns a `CBPeripheralManager`, registers mutable GATT services, and starts advertising through CoreBluetooth. No public adapter-selection path exists in this implementation; the controller is system-selected.

This corrects the earlier overbroad statement that BLESS itself requires Linux/BlueZ. The existing project-managed QEMU fixture is Linux/BlueZ-specific, while upstream BLESS also contains CoreBluetooth and WinRT server backends.

## Project changes

A dedicated Pixi feature/environment now locks the native fixture dependencies:

```text
apple-bless
```

A provider-specific profile was added:

```text
profile_id=blehub-macos-bless-basic-gatt-v1
local_name=BleHubMac
service_uuid=ffd00000-0000-4000-8000-000000000000
```

Its GATT roles, characteristic UUIDs, properties, and initial values remain semantically aligned with the Android and QEMU profiles. Profile ID, local name, and advertised/root service UUID are distinct so concurrently visible providers cannot satisfy the same service filter.

The shared BLESS fixture now accepts an explicit expected profile ID while preserving the QEMU profile as its default. This prevents a native macOS invocation from weakening the existing Linux guest profile assertion.

Available recipes:

```sh
pixi run just peripheral macos-bless-check
pixi run just peripheral macos-bless-start
pixi run just peripheral macos-bless-status
pixi run just peripheral macos-bless-log
pixi run just peripheral macos-bless-stop
```

Runtime files are isolated under `.work/peripheral/macos-bless/`. The controller verifies process identity before signaling a recorded PID and waits for the fixture's run-correlated `ready` status.

## Validation

The non-RF checks passed:

```sh
pixi lock
pixi run just peripheral profiles-check
pixi run just peripheral macos-bless-check
```

Observed markers:

```text
profile=blehub-macos-bless-basic-gatt-v1 file=macos-bless-basic-gatt-peripheral.json service_uuid=ffd00000-0000-4000-8000-000000000000 status=valid
provider_profile_isolation=passed
macos_bless_backend=corebluetooth
macos_bless_controller=system-selected
macos_bless_ub500=not-used
macos_bless_check=passed
```

No native macOS BLESS advertisement, iPhone scan, connection, GATT, or subscription pass is claimed yet. A later physical UB500 cold-plug recheck retained the macOS QEMU control design but reproduced the Realtek `0xfc61` HCI timeout; see the [cold-plug report](2026-09-03-apple-qemu-ub500-cold-plug-recheck.md). Starting RF advertising remains behind explicit DUT/provider/duration/exclusive-resource confirmation.

## Boundaries

- Native macOS BLESS uses CoreBluetooth and the built-in system-selected controller, not UB500.
- It does not replace Linux/BlueZ/BLESS + UB500 fault-injection or soak evidence.
- macOS Peripheral evidence does not prove the BleHub macOS Central backend or the iOS Central backend.
- iPhone build/sign/install/runtime and RF gates remain independent.
- All pre-freeze Apple evidence must be rerun where affected by the frozen ABI or final artifacts.
