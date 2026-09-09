android_run := "pixi run -e android"

# Reviewed API generation and snapshots.
mod binding "just/binding/mod.just"

# Test Peripheral providers.
mod peripheral "just/peripheral/mod.just"

# Stable UniFFI production runtime and package validation.
mod ffi "just/ffi/mod.just"

# Unit, conformance, fixture, and instrumentation selection.
mod test "just/test/mod.just"

# Production Windows backend development and explicit RF smoke tests.
mod windows "just/windows/mod.just"

# Production Apple backend development and platform build gates.
mod apple "just/apple/mod.just"

# Tauri integration and reference-app verification entry points.
mod tauri "just/tauri/mod.just"

default:
    @just --list --list-submodules

fmt:
    rtk cargo fmt --all
    rtk cargo fmt --manifest-path tests/hil/windows/Cargo.toml
    rtk cargo fmt --manifest-path tests/consumers/windows/Cargo.toml
    rtk cargo fmt --manifest-path examples/windows-central/Cargo.toml

    ruff check --fix tools
    ruff format tools
    just --fmt

check:
    rtk cargo check --workspace

[private]
lint-common:
    just --fmt --check
    rtk cargo fmt --all -- --check
    rtk cargo fmt --manifest-path tests/hil/windows/Cargo.toml -- --check
    rtk cargo fmt --manifest-path tests/consumers/windows/Cargo.toml -- --check
    rtk cargo fmt --manifest-path examples/windows-central/Cargo.toml -- --check

    rtk cargo clippy --workspace --all-targets -- -D warnings
    ruff check tools
    just peripheral profiles-check

[private]
[windows]
lint-windows:
    rtk cargo clippy --manifest-path tests/hil/windows/Cargo.toml --all-targets -- -D warnings
    rtk cargo clippy --manifest-path examples/windows-central/Cargo.toml --all-targets -- -D warnings

[windows]
lint: lint-common lint-windows

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
