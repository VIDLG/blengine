# Active Stable UniFFI production-candidate fixture.
# Superseded Phase 0 probes live under spikes/archive and intentionally have no recipes.

ffi-candidate-build:
    {{jvm_run}} rtk cargo build --manifest-path spikes/uniffi-production-candidate/Cargo.toml

ffi-candidate-generate-kotlin-windows: ffi-candidate-build
    rm -rf spikes/uniffi-production-candidate/generated/kotlin spikes/uniffi-production-candidate/generated/windows-kotlin-checksums
    {{jvm_run}} rtk cargo run --manifest-path spikes/uniffi-production-candidate/Cargo.toml --features bindgen --bin uniffi-bindgen -- generate .work/cargo-target/debug/blehub_uniffi_production_candidate.dll --language kotlin --config spikes/uniffi-production-candidate/uniffi.windows-kotlin.toml --out-dir spikes/uniffi-production-candidate/generated/kotlin --no-format
    {{jvm_run}} rtk cargo run --manifest-path spikes/uniffi-production-candidate/Cargo.toml --features bindgen --bin uniffi-bindgen -- generate .work/cargo-target/debug/blehub_uniffi_production_candidate.dll --language kotlin --config spikes/uniffi-production-candidate/uniffi.windows-kotlin-checksums.toml --out-dir spikes/uniffi-production-candidate/generated/windows-kotlin-checksums --no-format

ffi-candidate-check-windows-checksums: ffi-candidate-generate-kotlin-windows
    python tools/check_uniffi_windows_checksums.py --library .work/cargo-target/debug/blehub_uniffi_production_candidate.dll --generated-dir spikes/uniffi-production-candidate/generated/windows-kotlin-checksums

ffi-candidate-test-kotlin-jvm: ffi-candidate-check-windows-checksums
    {{jvm_run}} rtk gradlew -p spikes/uniffi-production-candidate/platforms/kotlin-jvm test --no-daemon

ffi-candidate-generate-swift: ffi-candidate-build
    rm -rf spikes/uniffi-production-candidate/generated/swift
    {{jvm_run}} rtk cargo run --manifest-path spikes/uniffi-production-candidate/Cargo.toml --features bindgen --bin uniffi-bindgen -- generate .work/cargo-target/debug/blehub_uniffi_production_candidate.dll --language swift --out-dir spikes/uniffi-production-candidate/generated/swift --no-format

ffi-candidate-test-swift-windows: ffi-candidate-generate-swift swift-doctor
    python -m tools.test_uniffi_production_candidate_swift_windows

ffi-candidate-build-macos:
    rtk cargo build --locked --manifest-path spikes/uniffi-production-candidate/Cargo.toml

ffi-candidate-generate-swift-macos: ffi-candidate-build-macos
    rm -rf spikes/uniffi-production-candidate/generated/swift-macos
    rtk cargo run --locked --manifest-path spikes/uniffi-production-candidate/Cargo.toml --features bindgen --bin uniffi-bindgen -- generate .work/cargo-target/debug/libblehub_uniffi_production_candidate.dylib --language swift --out-dir spikes/uniffi-production-candidate/generated/swift-macos --no-format

ffi-candidate-test-swift-macos: ffi-candidate-generate-swift-macos
    python -m tools.test_uniffi_production_candidate_swift_macos

ffi-candidate-generate-android-kotlin: ffi-candidate-build
    rm -rf spikes/uniffi-production-candidate/generated/android-kotlin
    {{jvm_run}} rtk cargo run --manifest-path spikes/uniffi-production-candidate/Cargo.toml --features bindgen --bin uniffi-bindgen -- generate .work/cargo-target/debug/blehub_uniffi_production_candidate.dll --language kotlin --config spikes/uniffi-production-candidate/uniffi.android.toml --out-dir spikes/uniffi-production-candidate/generated/android-kotlin --no-format

ffi-candidate-build-android-native: android-doctor
    {{android_run}} python -m tools.build_uniffi_android --manifest spikes/uniffi-production-candidate/Cargo.toml --output-root spikes/uniffi-production-candidate/generated/android-jni --library-name libblehub_uniffi_production_candidate.so

ffi-candidate-publish-android: ffi-candidate-generate-android-kotlin ffi-candidate-build-android-native
    {{android_run}} python -m tools.run_android -- rtk gradle -p spikes/uniffi-production-candidate/platforms/android :bindings:assembleDebug :bindings:publishReleasePublicationToFixtureRepository --no-daemon

ffi-candidate-test-android device="auto": ffi-candidate-publish-android
    {{android_run}} python -m tools.run_android --device {{device}} -- rtk gradle -p spikes/uniffi-production-candidate/platforms/android :bindings:connectedDebugAndroidTest --no-daemon
    {{android_run}} python -m tools.run_android --device {{device}} -- rtk gradle -p spikes/uniffi-production-candidate/platforms/android-consumer :app:connectedDebugAndroidTest --no-daemon
