set working-directory := "../.."

# Stable UniFFI cross-language runtime gates for the production BleHub ABI.

bindgen_run := "pixi run -e jvm"
android_run := "pixi run -e android"
apple_run := "pixi run -e apple-ci"
uniffi_manifest := "crates/blehub-uniffi/Cargo.toml"
generated_root := "tests/ffi/generated"
uniffi_library := if os() == "windows" {
    ".work/cargo-target/debug/blehub_uniffi.dll"
} else if os() == "macos" {
    ".work/cargo-target/debug/libblehub_uniffi.dylib"
} else {
    ".work/cargo-target/debug/libblehub_uniffi.so"
}

[private]
build:
    {{bindgen_run}} rtk cargo build --quiet --locked --manifest-path {{uniffi_manifest}}


[private]
build-macos:
    {{apple_run}} rtk cargo build --quiet --locked --manifest-path {{uniffi_manifest}}

[private]
generate-swift-macos: build-macos
    rm -rf {{generated_root}}/swift-macos
    {{apple_run}} rtk cargo run --quiet --locked --manifest-path {{uniffi_manifest}} --features bindgen --bin uniffi-bindgen -- generate .work/cargo-target/debug/libblehub_uniffi.dylib --language swift --out-dir {{generated_root}}/swift-macos --no-format

# Run the Stable UniFFI macOS Swift runtime gate.
swift-macos: generate-swift-macos
    {{apple_run}} python -m tools.test_uniffi_runtime_swift_macos

# Build the production XCFramework/Swift package.
# Run on the real macOS/Xcode host with all Rust Apple targets present.
apple-package:
    {{apple_run}} python -m tools.swift_toolchain verify
    {{apple_run}} python -m tools.apple.package_uniffi

[private]
generate-android-kotlin: build
    rm -rf {{generated_root}}/android-kotlin
    {{bindgen_run}} rtk cargo run --quiet --locked --manifest-path {{uniffi_manifest}} --features bindgen --bin uniffi-bindgen -- generate {{uniffi_library}} --language kotlin --config tests/ffi/config/uniffi.android.toml --out-dir {{generated_root}}/android-kotlin --no-format

[private]
build-android-native:
    {{android_run}} python -m tools.android_toolchain verify
    {{android_run}} python -m tools.build_uniffi_android --manifest {{uniffi_manifest}} --output-root {{generated_root}}/android-jni --library-name libblehub_uniffi.so

[private]
publish-android: generate-android-kotlin build-android-native
    {{android_run}} python -m tools.run_android -- rtk gradle -p tests/ffi/runtime/android :bindings:assembleDebug :bindings:publishReleasePublicationToFixtureRepository --no-daemon

# Compile production Android bindings and instrumentation without using a device.
android-build: publish-android
    {{android_run}} python -m tools.run_android -- rtk gradle -p tests/ffi/runtime/android :bindings:assembleDebugAndroidTest --no-daemon

# Run the Stable UniFFI Android ART and published-AAR production ABI runtime gates.
android device="auto": publish-android
    {{android_run}} python -m tools.run_android --device {{device}} -- rtk gradle -p tests/ffi/runtime/android :bindings:connectedDebugAndroidTest --no-daemon
    {{android_run}} python -m tools.run_android --device {{device}} -- rtk gradle -p tests/consumers/android :app:connectedDebugAndroidTest --no-daemon
