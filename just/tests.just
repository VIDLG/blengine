fmt:
    rtk cargo fmt --all
    rtk cargo fmt --manifest-path spikes/uniffi-production-candidate/Cargo.toml
    ruff check --fix tools
    ruff format tools

check:
    rtk cargo check --workspace

lint:
    rtk cargo fmt --all -- --check
    rtk cargo fmt --manifest-path spikes/uniffi-production-candidate/Cargo.toml -- --check
    rtk cargo clippy --workspace --all-targets -- -D warnings
    rtk cargo clippy --manifest-path spikes/uniffi-production-candidate/Cargo.toml --all-targets -- -D warnings
    ruff check tools

test:
    rtk cargo test --workspace

# Run the platform-independent backend contract and caller projection gate.
conformance:
    rtk cargo test -p blehub-testkit

# Accept intentional Rust/Kotlin/Swift public API shape changes.
api-snapshots-update: generate-bindings
    python -m tools.api_snapshots update

# Verify generated bindings and Rust public API against reviewed snapshots.
api-snapshots-check: generate-bindings
    python -m tools.api_snapshots check

ble-fixture-check *args:
    python -m tools.verify_ble_fixture {{args}}

android-test-device mode="auto":
    {{android_run}} python -m tools.android_toolchain device-select --mode {{mode}}

# Run the platform-independent candidate gate; workspace tests include conformance.
verify: check lint test api-snapshots-check
