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

The provider does not flash firmware. The configured nRF application firmware
must already be running. Firmware setup, reset, flashing, and AutoPTS setup
remain explicit `nrftest` operations.

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
same start/ready/stop shape as the Android and BLESS fixtures.
