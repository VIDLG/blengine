set working-directory := "../.."

# Active Stable UniFFI production-candidate fixture.
# Superseded probes under spikes/archive intentionally have no recipes.

jvm_run := "pixi run -e jvm"
android_run := "pixi run -e android"
manifest := "spikes/uniffi-production-candidate/Cargo.toml"
candidate_dll := ".work/cargo-target/debug/blehub_uniffi_production_candidate.dll"

[private]
build:
    {{jvm_run}} rtk cargo build --manifest-path {{manifest}}

[private]
generate-kotlin-windows: build
    rm -rf spikes/uniffi-production-candidate/generated/kotlin spikes/uniffi-production-candidate/generated/windows-kotlin-checksums
    {{jvm_run}} rtk cargo run --manifest-path {{manifest}} --features bindgen --bin uniffi-bindgen -- generate {{candidate_dll}} --language kotlin --config spikes/uniffi-production-candidate/uniffi.windows-kotlin.toml --out-dir spikes/uniffi-production-candidate/generated/kotlin --no-format
    {{jvm_run}} rtk cargo run --manifest-path {{manifest}} --features bindgen --bin uniffi-bindgen -- generate {{candidate_dll}} --language kotlin --config spikes/uniffi-production-candidate/uniffi.windows-kotlin-checksums.toml --out-dir spikes/uniffi-production-candidate/generated/windows-kotlin-checksums --no-format

[private]
check-windows-checksums: generate-kotlin-windows
    python tools/check_uniffi_windows_checksums.py --library {{candidate_dll}} --generated-dir spikes/uniffi-production-candidate/generated/windows-kotlin-checksums

# Run the production-candidate desktop Kotlin/JVM gate.
kotlin-jvm: check-windows-checksums
    {{jvm_run}} rtk gradlew -p spikes/uniffi-production-candidate/platforms/kotlin-jvm test --no-daemon

[private]
generate-swift: build
    rm -rf spikes/uniffi-production-candidate/generated/swift
    {{jvm_run}} rtk cargo run --manifest-path {{manifest}} --features bindgen --bin uniffi-bindgen -- generate {{candidate_dll}} --language swift --out-dir spikes/uniffi-production-candidate/generated/swift --no-format

# Run the supplementary production-candidate Windows Swift host gate.
swift-windows: generate-swift
    python -m tools.swift_toolchain verify
    python -m tools.test_uniffi_production_candidate_swift_windows

[private]
build-macos:
    rtk cargo build --locked --manifest-path {{manifest}}

[private]
generate-swift-macos: build-macos
    rm -rf spikes/uniffi-production-candidate/generated/swift-macos
    rtk cargo run --locked --manifest-path {{manifest}} --features bindgen --bin uniffi-bindgen -- generate .work/cargo-target/debug/libblehub_uniffi_production_candidate.dylib --language swift --out-dir spikes/uniffi-production-candidate/generated/swift-macos --no-format

# Run the deferred production-candidate macOS Swift gate.
swift-macos: generate-swift-macos
    python -m tools.test_uniffi_production_candidate_swift_macos

[private]
generate-android-kotlin: build
    rm -rf spikes/uniffi-production-candidate/generated/android-kotlin
    {{jvm_run}} rtk cargo run --manifest-path {{manifest}} --features bindgen --bin uniffi-bindgen -- generate {{candidate_dll}} --language kotlin --config spikes/uniffi-production-candidate/uniffi.android.toml --out-dir spikes/uniffi-production-candidate/generated/android-kotlin --no-format

[private]
build-android-native:
    {{android_run}} python -m tools.android_toolchain verify
    {{android_run}} python -m tools.build_uniffi_android --manifest {{manifest}} --output-root spikes/uniffi-production-candidate/generated/android-jni --library-name libblehub_uniffi_production_candidate.so

[private]
publish-android: generate-android-kotlin build-android-native
    {{android_run}} python -m tools.run_android -- rtk gradle -p spikes/uniffi-production-candidate/platforms/android :bindings:assembleDebug :bindings:publishReleasePublicationToFixtureRepository --no-daemon

# Run the production-candidate Android ART and published-AAR consumer gates.
android device="auto": publish-android
    {{android_run}} python -m tools.run_android --device {{device}} -- rtk gradle -p spikes/uniffi-production-candidate/platforms/android :bindings:connectedDebugAndroidTest --no-daemon
    {{android_run}} python -m tools.run_android --device {{device}} -- rtk gradle -p spikes/uniffi-production-candidate/platforms/android-consumer :app:connectedDebugAndroidTest --no-daemon
