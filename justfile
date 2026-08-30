jvm_run := "pixi run -e jvm"
android_run := "pixi run -e android"
ffi_library := if os() == "windows" {
    ".work/cargo-target/debug/blehub.dll"
} else if os() == "macos" {
    ".work/cargo-target/debug/libblehub.dylib"
} else {
    ".work/cargo-target/debug/libblehub.so"
}

import "just/peripherals.just"
import "just/spikes.just"
import "just/tests.just"

default:
    @just --list

docs-update:
    python tools/update_platform_docs.py


# Install locked Node dependencies used by documentation checks.
docs-install:
    npm ci --ignore-scripts --no-audit --no-fund

# Validate Mermaid syntax in project-authored Markdown.
docs-check-mermaid:
    npm run check:mermaid


build-bindings-library:
    rtk cargo build -p blehub

generate-kotlin: build-bindings-library
    rm -rf generated/kotlin
    rtk cargo run -p blehub --features bindgen --bin uniffi-bindgen -- generate {{ffi_library}} --language kotlin --config uniffi.toml --out-dir generated/kotlin --no-format

generate-swift: build-bindings-library
    rm -rf generated/swift
    rtk cargo run -p blehub --features bindgen --bin uniffi-bindgen -- generate {{ffi_library}} --language swift --config uniffi.toml --out-dir generated/swift --no-format

generate-bindings: generate-kotlin generate-swift



toolchain-paths:
    python -m tools.local_config report

sccache-stats:
    sccache --show-stats

ble-scan *args:
    python -m tools.scan_ble {{args}}

swift-bootstrap:
    python -m tools.swift_toolchain install

swift-doctor:
    python -m tools.swift_toolchain verify

swift-report:
    python -m tools.swift_toolchain report

android-bootstrap:
    {{android_run}} python -m tools.android_toolchain install

android-doctor:
    {{android_run}} python -m tools.android_toolchain verify

android-report:
    {{android_run}} python -m tools.android_toolchain report

android-avd-bootstrap:
    {{android_run}} python -m tools.android_toolchain avd-create

android-emulator-start:
    {{android_run}} python -m tools.android_toolchain emulator-start

android-emulator-stop:
    {{android_run}} python -m tools.android_toolchain emulator-stop
