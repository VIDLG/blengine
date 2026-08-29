jvm_run := "pixi run -e jvm"

default:
    @just --list

docs-update:
    python tools/update_platform_docs.py

fmt:
    cargo fmt --all
    cargo fmt --manifest-path spikes/boltffi-token-ingress/Cargo.toml
    ruff check --fix tools
    ruff format tools

check:
    cargo check --workspace

lint:
    cargo fmt --all -- --check
    cargo fmt --manifest-path spikes/boltffi-token-ingress/Cargo.toml -- --check
    cargo clippy --workspace --all-targets -- -D warnings
    cargo clippy --manifest-path spikes/boltffi-token-ingress/Cargo.toml --all-targets -- -D warnings
    ruff check tools

test:
    rtk cargo test --workspace

docs-check-mermaid:
    npm install --ignore-scripts --no-audit --no-fund
    npm run check:mermaid

ffi-spike-check-class-handle-param:
    python tools/check_expected_cargo_failure.py --manifest-path spikes/boltffi-class-handle-callback-param/Cargo.toml --expected "unsupported expansion: callback method parameter shape"

ffi-spike-test-lifecycle:
    rtk cargo test --manifest-path spikes/boltffi-lifecycle/Cargo.toml

generate-kotlin:
    python tools/run_boltffi_cli.py --project . generate kotlin --deny-skipped

generate-swift:
    python tools/run_boltffi_cli.py --project . generate swift --deny-skipped

generate-bindings: generate-kotlin generate-swift

ffi-spike-generate-kotlin:
    python tools/run_boltffi_cli.py --project spikes/boltffi-lifecycle generate kotlin --deny-skipped

llvm-mingw-install:
    python tools/llvm_mingw.py install

llvm-mingw-report:
    python tools/llvm_mingw.py report

toolchain-paths:
    python -m tools.local_config report

sccache-stats:
    sccache --show-stats

swift-bootstrap:
    python -m tools.swift_toolchain install

swift-doctor:
    python -m tools.swift_toolchain verify

swift-report:
    python -m tools.swift_toolchain report

ffi-spike-build-windows-jvm-bridge: ffi-spike-generate-kotlin
    {{jvm_run}} python -m tools.build_windows_jvm_bridge

ffi-spike-test-kotlin-jvm: ffi-spike-build-windows-jvm-bridge
    {{jvm_run}} rtk gradlew -p spikes/boltffi-lifecycle/platforms/kotlin-jvm test sinkExceptionProbe --no-daemon

ffi-phase0-generate-kotlin:
    python tools/run_boltffi_cli.py --project spikes/boltffi-token-ingress generate kotlin --deny-skipped
    python tools/workaround_boltffi_kotlin_error_message.py spikes/boltffi-token-ingress/generated/kotlin/dev/blehub/phase0/BlehubPhase0FfiFixture.kt

ffi-phase0-generate-swift:
    python tools/run_boltffi_cli.py --project spikes/boltffi-token-ingress generate swift --deny-skipped

ffi-phase0-build-windows-swift-native: ffi-phase0-generate-swift
    rtk cargo build --manifest-path spikes/boltffi-token-ingress/Cargo.toml --target x86_64-pc-windows-msvc

ffi-phase0-test-swift-windows: ffi-phase0-build-windows-swift-native
    python -m tools.test_swift_windows

ffi-phase0-build-windows-jvm-bridge: ffi-phase0-generate-kotlin
    {{jvm_run}} python -m tools.build_windows_jvm_bridge --project spikes/boltffi-token-ingress

ffi-phase0-test-kotlin-jvm: ffi-phase0-build-windows-jvm-bridge
    {{jvm_run}} rtk gradlew -p spikes/boltffi-token-ingress/platforms/kotlin-jvm jar test --no-daemon
    {{jvm_run}} rtk gradlew -p spikes/boltffi-token-ingress/platforms/kotlin-jvm-consumer test --no-daemon

phase0-check: check lint test ffi-spike-check-class-handle-param generate-bindings
