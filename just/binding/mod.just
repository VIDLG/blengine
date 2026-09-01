set working-directory := "../.."

ffi_library := if os() == "windows" {
    ".work/cargo-target/debug/blehub.dll"
} else if os() == "macos" {
    ".work/cargo-target/debug/libblehub.dylib"
} else {
    ".work/cargo-target/debug/libblehub.so"
}

[private]
build:
    rtk cargo build -p blehub

# Generate Kotlin bindings from the current candidate library.
kotlin: build
    rm -rf generated/kotlin
    rtk cargo run -p blehub --features bindgen --bin uniffi-bindgen -- generate {{ffi_library}} --language kotlin --config uniffi.toml --out-dir generated/kotlin --no-format

# Generate Swift bindings from the current candidate library.
swift: build
    rm -rf generated/swift
    rtk cargo run -p blehub --features bindgen --bin uniffi-bindgen -- generate {{ffi_library}} --language swift --config uniffi.toml --out-dir generated/swift --no-format

# Generate all supported language bindings.
generate: kotlin swift

# Accept intentional Rust/Kotlin/Swift public API shape changes.
snapshot-update: generate
    python -m tools.api_snapshots update

# Verify generated bindings and Rust public API against reviewed snapshots.
snapshot-check: generate
    python -m tools.api_snapshots check
