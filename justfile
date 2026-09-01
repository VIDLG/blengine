android_run := "pixi run -e android"

# Reviewed API generation and snapshots.
mod binding "just/binding/mod.just"

# Test Peripheral providers.
mod peripheral "just/peripheral/mod.just"

# Active capability and production-candidate probes.
mod spike "just/spike/mod.just"

# Unit, conformance, fixture, and instrumentation selection.
mod test "just/test/mod.just"

# Production Windows backend development and explicit RF smoke tests.
mod windows "just/windows/mod.just"

default:
    @just --list --list-submodules

fmt:
    rtk cargo fmt --all
    rtk cargo fmt --manifest-path tests/hil/windows/Cargo.toml
    rtk cargo fmt --manifest-path tests/consumers/windows/Cargo.toml
    rtk cargo fmt --manifest-path examples/windows-central/Cargo.toml
    rtk cargo fmt --manifest-path spikes/uniffi-production-candidate/Cargo.toml
    ruff check --fix tools
    ruff format tools
    just --fmt

check:
    rtk cargo check --workspace

lint:
    just --fmt --check
    rtk cargo fmt --all -- --check
    rtk cargo fmt --manifest-path tests/hil/windows/Cargo.toml -- --check
    rtk cargo fmt --manifest-path tests/consumers/windows/Cargo.toml -- --check
    rtk cargo fmt --manifest-path examples/windows-central/Cargo.toml -- --check
    rtk cargo fmt --manifest-path spikes/uniffi-production-candidate/Cargo.toml -- --check
    rtk cargo clippy --workspace --all-targets -- -D warnings
    rtk cargo clippy --manifest-path tests/hil/windows/Cargo.toml --all-targets -- -D warnings
    rtk cargo clippy --manifest-path examples/windows-central/Cargo.toml --all-targets -- -D warnings
    rtk cargo clippy --manifest-path spikes/uniffi-production-candidate/Cargo.toml --all-targets -- -D warnings
    ruff check tools
    just peripheral profiles-check

verify: check lint test::all binding::snapshot-check

docs-update:
    python tools/update_platform_docs.py

# Install locked Node dependencies used by documentation checks.
docs-install:
    npm ci --ignore-scripts --no-audit --no-fund

# Validate Mermaid syntax in project-authored Markdown.
docs-check-mermaid:
    npm run check:mermaid

toolchain-paths:
    python -m tools.local_config report

sccache-stats:
    sccache --show-stats

ble-scan *args:
    python -m tools.scan_ble {{ args }}

swift-bootstrap:
    python -m tools.swift_toolchain install

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
