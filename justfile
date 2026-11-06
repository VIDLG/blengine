android_run := "pixi run -e android"

# Reviewed API generation and snapshots.
mod binding "just/binding/mod.just"

# Test Peripheral providers.
mod peripheral "just/peripheral/mod.just"

# Stable UniFFI production runtime and package validation.
mod ffi "just/ffi/mod.just"

# Unit, conformance, fixture, and instrumentation selection.
mod test "just/test/mod.just"

# Production Apple backend development and platform build gates.
mod apple "just/apple/mod.just"

# Private React Native Nitro integration and internal consumer gates.
mod react-native "just/react-native/mod.just"

default:
    @just --list --list-submodules

fmt:
    rtk cargo fmt --all

    ruff check --fix tools tests/support/rf_lab tests/peripherals/nrf52840/support
    ruff format tools tests/support/rf_lab tests/peripherals/nrf52840/support
    just --fmt

check:
    rtk cargo check --workspace

[private]
lint-common:
    just --fmt --check
    rtk cargo fmt --all -- --check

    rtk cargo clippy --workspace --all-targets -- -D warnings
    ruff check tools tests/support/rf_lab tests/peripherals/nrf52840/support
    just peripheral profiles-check

[unix]
lint: lint-common

verify: check lint test::all binding::snapshot-check

docs-update:
    python tools/update_platform_docs.py

# Install locked Node dependencies used by documentation checks.
docs-install:
    npm ci --ignore-scripts --no-audit --no-fund

# Validate Mermaid syntax in project-authored Markdown.
docs-check-mermaid:
    npm run check:mermaid

# Report reclaimable generated artifacts without deleting anything.
work-report:
    python -m tools.clean_work report

# Remove stale RN artifact runs and generated RN host output, retaining one run.
work-clean keep_runs="1":
    python -m tools.clean_work apply --keep-runs {{ quote(keep_runs) }}

# Explicitly remove the local archived copies and rebuildable Cargo target cache too.
# This is intentionally separate because it makes the next build substantially slower.
work-clean-all keep_runs="1":
    python -m tools.clean_work apply --keep-runs {{ quote(keep_runs) }} --include-archive-local --include-cargo-target

toolchain-paths:
    python -m tools.local_config report

# Start the local Mutagen project using .gitignore and blehub.local.toml.
# Explicit arguments override environment variables and local configuration.
mutagen-sync-create host="" mutagen_exe="" user="" remote_path="" session_name="":
    python -m tools.mutagen_sync start --host {{ quote(host) }} --executable {{ quote(mutagen_exe) }} --user {{ quote(user) }} --remote-path {{ quote(remote_path) }} --session-name {{ quote(session_name) }}

# Show the local Mutagen project's sessions and synchronization problems.
mutagen-sync-status mutagen_exe="" session_name="":
    python -m tools.mutagen_sync list --executable {{ quote(mutagen_exe) }} --session-name {{ quote(session_name) }}

# Force one synchronization cycle for the local Mutagen project.
mutagen-sync-flush mutagen_exe="" session_name="":
    python -m tools.mutagen_sync flush --executable {{ quote(mutagen_exe) }} --session-name {{ quote(session_name) }}

# Terminate the local Mutagen project without deleting synchronized files.
mutagen-sync-terminate mutagen_exe="" session_name="":
    python -m tools.mutagen_sync terminate --executable {{ quote(mutagen_exe) }} --session-name {{ quote(session_name) }}

sccache-stats:
    sccache --show-stats

ble-scan *args:
    python -m tools.scan_ble {{ args }}

# Capture BLE air traffic with the independent nRF52840 sniffer dongle.
# Port and nrfutil home resolve from ignored blehub.local.toml unless overridden.
ble-sniffer-capture *args:
    python -m tools.ble_sniffer capture {{ args }}

# List live serial devices before selecting a sniffer port.
ble-sniffer-ports:
    python -m tools.ble_sniffer ports

# Extract observed BLE advertising addresses/names from a Nordic PCAP via Scapy.
ble-sniffer-pcap-devices pcap:
    python -m tools.ble_sniffer pcap-devices --pcap {{ quote(pcap) }}

# Summarize decoded BLE data/ATT writes/notifications from a Nordic PCAP via Scapy.
ble-sniffer-pcap-att pcap:
    python -m tools.ble_sniffer pcap-att --pcap {{ quote(pcap) }}

swift-doctor:
    python -m tools.swift_toolchain verify

swift-report:
    python -m tools.swift_toolchain report

android-bootstrap:
    {{ android_run }} python -m tools.android_toolchain install

android-doctor:
    {{ android_run }} python -m tools.android_toolchain verify

android-report:
    {{ android_run }} python -m tools.android_toolchain report

android-avd-bootstrap:
    {{ android_run }} python -m tools.android_toolchain avd-create

android-emulator-start:
    {{ android_run }} python -m tools.android_toolchain emulator-start

android-emulator-stop:
    {{ android_run }} python -m tools.android_toolchain emulator-stop
