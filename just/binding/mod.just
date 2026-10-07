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
bleng_manifest := "crates/bleng-uniffi/Cargo.toml"
bleng_target := ".work/cargo-target/bleng-snapshot"
bleng_generated_root := ".work/bleng-snapshot-generated"
bleng_library := if os() == "windows" {
    bleng_target + "/debug/bleng_uniffi.dll"
} else if os() == "macos" {
    bleng_target + "/debug/libbleng_uniffi.dylib"
} else {
    bleng_target + "/debug/libbleng_uniffi.so"
}

[private]
build:
    rtk cargo build --quiet --locked --manifest-path {{uniffi_manifest}}

# Generate Kotlin bindings from the current BleHub library.
kotlin: build
    rm -rf {{generated_root}}/kotlin
    rtk cargo run --quiet --locked --manifest-path {{uniffi_manifest}} --features bindgen --bin uniffi-bindgen -- generate {{uniffi_library}} --language kotlin --config uniffi.toml --out-dir {{generated_root}}/kotlin --no-format

# Generate Swift bindings from the current BleHub library.
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

# Build the formal BLeng package from its own final native-image owner.
[private]
bleng-build:
    CARGO_TARGET_DIR={{bleng_target}} rtk cargo build --quiet --locked --manifest-path {{bleng_manifest}}

[private]
bleng-generate: bleng-build
    rm -rf {{bleng_generated_root}}
    CARGO_TARGET_DIR={{bleng_target}} rtk cargo run --quiet --locked --manifest-path {{bleng_manifest}} --features bindgen --bin uniffi-bindgen -- generate {{bleng_library}} --language kotlin --config uniffi.toml --out-dir {{bleng_generated_root}}/kotlin --no-format
    CARGO_TARGET_DIR={{bleng_target}} rtk cargo run --quiet --locked --manifest-path {{bleng_manifest}} --features bindgen --bin uniffi-bindgen -- generate {{bleng_library}} --language swift --config uniffi.toml --out-dir {{bleng_generated_root}}/swift --no-format

# Accept intentional changes to the reviewed BLeng API surface.
bleng-snapshot-update: bleng-generate
    python -m tools.api_snapshots update --bleng

# Fail closed if the BLeng component or its reviewed API shape drifts.
bleng-snapshot-check: bleng-generate
    python -m unittest tools.test_api_snapshots -q
    python -m tools.api_snapshots check --bleng
