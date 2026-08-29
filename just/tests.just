fmt:
    cargo fmt --all
    cargo fmt --manifest-path spikes/boltffi-token-ingress/Cargo.toml
    cargo fmt --manifest-path spikes/uniffi-lifecycle/Cargo.toml
    cargo fmt --manifest-path spikes/uniffi-error-field-collision/Cargo.toml
    ruff check --fix tools
    ruff format tools

check:
    cargo check --workspace

lint:
    cargo fmt --all -- --check
    cargo fmt --manifest-path spikes/boltffi-token-ingress/Cargo.toml -- --check
    cargo fmt --manifest-path spikes/uniffi-lifecycle/Cargo.toml -- --check
    cargo fmt --manifest-path spikes/uniffi-error-field-collision/Cargo.toml -- --check
    cargo clippy --workspace --all-targets -- -D warnings
    cargo clippy --manifest-path spikes/boltffi-token-ingress/Cargo.toml --all-targets -- -D warnings
    cargo clippy --manifest-path spikes/uniffi-lifecycle/Cargo.toml --all-targets -- -D warnings
    cargo clippy --manifest-path spikes/uniffi-error-field-collision/Cargo.toml --all-targets -- -D warnings
    ruff check tools

test:
    rtk cargo test --workspace

android-test-device mode="auto":
    {{android_run}} python -m tools.android_toolchain device-select --mode {{mode}}

phase0-check: check lint test ffi-spike-check-class-handle-param generate-bindings
