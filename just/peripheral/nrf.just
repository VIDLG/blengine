nrf_control := "python -m tests.peripherals.nrf52840.provider"

# Run nRF software regressions without hardware or firmware tools.
# Cross-repository broker tests explicitly skip if nrftest is not configured.
nrf52840-test:
    python -W error::ResourceWarning -m unittest discover -s tests/peripherals/nrf52840 -t .
    python -W error::ResourceWarning -m unittest tools.tests.react_native.test_rf_fixture

# Read-only inventory of every configured DK J-Link identity and its current VCOMs.
# Device names and serials come from blehub.local.toml; no DK count or name is assumed.
nrf52840-inventory:
    python -m tests.peripherals.nrf52840.inventory

# Read the installed image identity from the physical DK control plane.
# This never flashes, resets, advertises, or infers identity from a local build.
nrf52840-attestation probe_serial output:
    python -m tests.peripherals.nrf52840.attestation --probe-serial {{ quote(probe_serial) }} --output {{ quote(output) }}

# Run the non-destructive Core/GAP/GATT/profile doctor over the configured serial port.
nrf52840-doctor:
    {{ nrf_control }} doctor

# Require all explicitly configured DKs to pass BTP/profile readiness.
# This never flashes or resets firmware and cleans up both fixture processes.
nrf52840-multi-readiness:
    python -m tests.peripherals.nrf52840.multi_fixture readiness

# Start the direct provider-owned BTP fixture and wait for advertising readiness.
nrf52840-direct-start fixture_request="":
    if test -n {{ quote(fixture_request) }}; then {{ nrf_control }} direct-start --fixture-request {{ quote(fixture_request) }}; else {{ nrf_control }} direct-start; fi

# Run the long-lived direct provider-owned BTP fixture in the foreground.
nrf52840-direct-serve fixture_request="":
    if test -n {{ quote(fixture_request) }}; then {{ nrf_control }} direct-serve --fixture-request {{ quote(fixture_request) }}; else {{ nrf_control }} direct-serve; fi

# Retired transitional entry: fails closed; use direct-start with a v2 fixture request.
nrf52840-start:
    {{ nrf_control }} start

# Verify that the nRF52840 fixture emitted its readiness marker.
nrf52840-ready:
    {{ nrf_control }} ready

# Report the external nRF52840 fixture process state.
nrf52840-status:
    {{ nrf_control }} status

# Stop the external nRF52840 fixture without flashing or resetting the target.
nrf52840-stop:
    {{ nrf_control }} stop
