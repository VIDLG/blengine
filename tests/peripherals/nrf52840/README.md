# nRF52840 Peripheral fixture

This directory is the BleHub-owned provider entry for the standalone nRF52840
Peripheral fixture. The shared profile is under
`tests/conformance/scenarios/blehub-nrf-indication-v2.json`; this directory contains
provider source and documentation only. The current provider is a transitional
bridge used during migration. The final provider will own its host control path
and standalone firmware project; it must not require the independent `nrftest`
repository at runtime.

## Configuration

Set the external fixture root in ignored `blehub.local.toml`:

```toml
[toolchains]
nrftest_root = "/absolute/path/to/nrftest"

[peripheral_nrf52840]
# Optional when nrftest.local.toml already selects the unique device.
port = "/dev/cu.usbmodem..."
```

The same values can be supplied for one run with:

```text
BLEHUB_NRFTEST_ROOT
BLEHUB_NRF52840_PORT
```

For the private two-DK gate, use explicit ignored local mappings. The full USB
serial includes leading zeroes and must match the CDC port reported by
`pyserial`; the controller fails closed before starting either fixture if the
mapping is missing or inconsistent:

```toml
[peripheral_nrf52840.devices.dk_a]
probe_serial = "001050252028"
port = "/dev/cu.usbmodem0010502520281"
profile = "tests/conformance/scenarios/blehub-nrf-indication-v2.json"

[peripheral_nrf52840.devices.dk_b]
probe_serial = "001050251479"
port = "/dev/cu.usbmodem0010502514791"
profile = "tests/conformance/scenarios/blehub-nrf-indication-v2-b.json"
```

`host-android-rf-multi` then performs profile provisioning/doctor and direct
fixture readiness independently for both boards. Startup is failure-atomic:
if either board is absent, has no compatible BTP firmware, or fails doctor,
the other newly started board is stopped. No recipe flashes firmware.

The provider does not flash firmware. The configured nRF application firmware
must already be running. Firmware setup and flashing remain explicit `nrftest`
operations. Normal startup does not assume the optional BTP GATT reset-server
command exists: the doctor validates the configured profile, reuses a matching
resident database, and initializes its values before advertising; an incomplete
or different database fails closed.

## Optional ATT read-hold diagnostic (private iPhone RF gate)

`diagnostic/read_hold.py` owns only the private one-shot BTP control protocol. The tracked `tests/conformance/scenarios/blehub-nrf-att-read-hold-v1.json` selects a separate advertised service and local name; its single read-write value explicitly requires read authorization. `diagnostic/profile.py` validates that identity and permission before provisioning. The normal profile loader rejects that authorization field, so the default nRF fixture cannot silently start a diagnostic profile. None of these pure-software modules open a port or flash/reset a DK. Validate them with `pixi run just peripheral nrf52840-att-hold-test` (no `nrftest` installation required).

The explicit BleHub-owned build (`diagnostic/build_dk.py`) stages a copy of the pinned Zephyr Tester plus the tracked read-hold patch under ignored `.work/nrf-att-hold-maintained-dk`. Run `pixi run just peripheral nrf52840-att-hold-build` only when the locally configured `nrftest` checkout already has its west workspace, SDK 1.0.1 and Pixi tools installed. This **build-time-only** reuse reads the existing `nrftest.local.toml` toolchain paths and Pixi lock; it does not import its Python implementation, update its sources, install tools, modify shared `contrib/zephyr`, or flash the DK. Before and after compiling, the builder verifies the pinned Zephyr manifest, west module revisions and clean worktrees. Output already present is refused rather than overwritten. `diagnostic/image.py` validates the local HEX and its build manifest, **not** the firmware installed on the DK.

With the normal provider stopped, `pixi run just peripheral nrf52840-att-hold-doctor <probe-serial>` performs a read-only, exclusive BTP capability/status probe. It refuses a present RN or provider lock, and its success proves only that the attached DK responds to the diagnostic protocol—not which HEX is installed or any Central RF result.

The opt-in iPhone runner (`diagnostic/device.py`) owns the sole BTP serial session and RN fixture lock. Run `pixi run just react-native host-ios-device-rf-pending-read-reload <udid> <team> <probe-serial> <local-hex-sha256>` after preparing the diagnostic DK separately. The recipe builds the signed RN host and checks its single image; it does not flash/reset the DK. Under the exclusive lock, the runner explicitly clears only the Tester's dynamic GATT database while disconnected before provisioning the diagnostic profile; it fails closed if the reset is rejected. Never run the normal `nrf52840-direct-start` fixture concurrently. The [maintained iPhone + DK RF rerun](../../../docs/testing/reports/2026-09-27-react-native-ios-dk-pending-att-maintained.md) passed on the unfrozen private RN candidate; the earlier spike is archived under `spikes/archive/nrf-att-hold/`. This is not an installed-image readback, general late-CoreBluetooth-callback proof, full GATT I/O gate or public RN API. See [the plan](../../../.plan/PLAN.md) for the scope and installed-image identity limit.

## Provider commands

From the BleHub root:

```sh
pixi run just peripheral nrf52840-start
pixi run just peripheral nrf52840-status
pixi run just peripheral nrf52840-stop
```

`nrf52840-start` launches the locked `nrftest` BTP GATT fixture using the
conformance scenario profile and waits for its ready marker. It keeps the
fixture process alive until `nrf52840-stop` or an RF scenario's cleanup trap.

The provider is not a second BLE implementation and does not expose a BleHub
Peripheral product API. It only gives project-managed Central HIL recipes the
same start/ready/stop shape as the Android and BLESS fixtures. The multi-DK
controller is private test infrastructure; its two profiles preserve the same
GATT semantics while using distinct service UUIDs and local names.
