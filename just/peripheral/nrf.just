nrf_control := "python -m tests.peripherals.nrf52840.provider"

# Run host regression tests without hardware, firmware tools, or nrftest.
nrf52840-test:
    python -m unittest tests.peripherals.nrf52840.test_provider

# Run the non-destructive Core/GAP/GATT/profile doctor over the configured serial port.
nrf52840-doctor:
    {{nrf_control}} doctor

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
