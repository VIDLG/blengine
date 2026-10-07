nrf_control := "python -m tests.peripherals.nrf52840.provider"

# Run host regression tests without hardware, firmware tools, or nrftest.
nrf52840-test:
    python -m unittest tests.peripherals.nrf52840.test_provider

# Validate the optional ATT-hold build, protocol, runner and RN ledger without hardware or nrftest.
nrf52840-att-hold-test:
    python -m unittest tests.peripherals.nrf52840.diagnostic.test_build_dk tests.peripherals.nrf52840.diagnostic.test_image tests.peripherals.nrf52840.diagnostic.test_read_hold tests.peripherals.nrf52840.diagnostic.test_profile tests.peripherals.nrf52840.diagnostic.test_device tests.peripherals.nrf52840.diagnostic.test_doctor tools.react_native.test_pending_read_ledger tools.react_native.test_run_ios_pending_read_reload -q

# Opt-in BleHub-owned diagnostic build using the existing configured nrftest
# west/SDK/Pixi tools read-only. Never update, install, reset, or flash.
nrf52840-att-hold-build:
    pixi run python -m tests.peripherals.nrf52840.diagnostic.build_dk

# Read-only diagnostic capability/status probe; does not attest DK flash or run RF.
nrf52840-att-hold-doctor probe_serial:
    python -m tests.peripherals.nrf52840.diagnostic.doctor --probe-serial {{quote(probe_serial)}}

# Run the non-destructive Core/GAP/GATT/profile doctor over the configured serial port.
nrf52840-doctor:
    {{nrf_control}} doctor

# Require all explicitly configured DKs to pass BTP/profile readiness.
# This never flashes or resets firmware and cleans up both fixture processes.
nrf52840-multi-readiness:
    python -m tests.peripherals.nrf52840.multi_fixture readiness

# Start the direct provider-owned BTP fixture and wait for advertising readiness.
nrf52840-direct-start:
    {{nrf_control}} direct-start

# Run the long-lived direct provider-owned BTP fixture in the foreground.
nrf52840-direct-serve:
    {{nrf_control}} direct-serve

# Start the transitional fixture process without flashing or resetting the target.
nrf52840-start:
    {{nrf_control}} start

# Verify that the nRF52840 fixture emitted its readiness marker.
nrf52840-ready:
    {{nrf_control}} ready

# Report the external nRF52840 fixture process state.
nrf52840-status:
    {{nrf_control}} status

# Stop the external nRF52840 fixture without flashing or resetting the target.
nrf52840-stop:
    {{nrf_control}} stop
