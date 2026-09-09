set working-directory := "../.."

uniffi_manifest := "crates/blehub-uniffi/Cargo.toml"
uniffi_library := if os() == "windows" {
    ".work/cargo-target/debug/blehub_uniffi.dll"
} else if os() == "macos" {
    ".work/cargo-target/debug/libblehub_uniffi.dylib"
} else {
    ".work/cargo-target/debug/libblehub_uniffi.so"
}
generated_root := ".work/api-snapshot-generated"

[private]
build:
    rtk cargo build --quiet --locked --manifest-path {{uniffi_manifest}}

# Generate Kotlin bindings from the current candidate library.
kotlin: build
    rm -rf {{generated_root}}/kotlin
    rtk cargo run --quiet --locked --manifest-path {{uniffi_manifest}} --features bindgen --bin uniffi-bindgen -- generate {{uniffi_library}} --language kotlin --config uniffi.toml --out-dir {{generated_root}}/kotlin --no-format

# Generate Swift bindings from the current candidate library.
swift: build
    rm -rf {{generated_root}}/swift
    rtk cargo run --quiet --locked --manifest-path {{uniffi_manifest}} --features bindgen --bin uniffi-bindgen -- generate {{uniffi_library}} --language swift --config uniffi.toml --out-dir {{generated_root}}/swift --no-format

# Generate all supported language bindings.
generate: kotlin swift

# Accept intentional Rust/Kotlin/Swift public API shape changes.
snapshot-update: generate
    python -m tools.api_snapshots update

# Verify generated bindings and Rust public API against reviewed snapshots.
snapshot-check: generate
    python -m tools.api_snapshots check
