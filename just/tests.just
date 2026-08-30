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

ble-fixture-check *args:
    python -m tools.verify_ble_fixture {{args}}

android-test-device mode="auto":
    {{android_run}} python -m tools.android_toolchain device-select --mode {{mode}}

phase0-check: check lint test generate-bindings
