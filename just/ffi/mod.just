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

# Formal two-component BLeng package. It is a separate package line from
# apple-package and does not make blehub-uniffi depend on BLeng.
[macos]
apple-bleng-package:
    {{apple_run}} python -m tools.swift_toolchain verify
    {{apple_run}} python -m tools.apple.package_uniffi --bleng

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

# Build the production Android Central backend library against the generated
# production binding and the same staged native image. This is host-only: it
# does not install, start, or claim Android RF.
android-backend-build: android-build
    {{android_run}} python -m tools.run_android -- rtk gradle -p platforms/android :library:assembleDebug --no-daemon

android-backend-publish: android-build
    {{android_run}} python -m tools.run_android -- rtk gradle -p platforms/android :library:assembleRelease :library:publishReleasePublicationToFixtureRepository --no-daemon

# Build the clean Android consumer against the published backend AAR. The
# consumer still resolves the canonical UniFFI/native-image AAR transitively.
android-backend-consumer-build: android-backend-publish
    {{android_run}} python -m tools.run_android -- rtk gradle -p tests/consumers/android :app:assembleDebug :app:assembleDebugAndroidTest --no-daemon

# Run only the backend-AAR consumer lifecycle test through the project-owned
# adb push + pm install instrumentation runner.
android-backend-consumer device="physical": android-backend-consumer-build
    {{android_run}} python -m tools.android.run_instrumentation --device {{device}} --app-apk tests/consumers/android/app/build/outputs/apk/debug/app-debug.apk --test-apk tests/consumers/android/app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk --test-component dev.blehub.ffi.runtime.consumer.test/androidx.test.runner.AndroidJUnitRunner --test-class dev.blehub.ffi.runtime.consumer.PublishedArtifactConsumerTest --log .work/android-instrumentation/android-backend-consumer.log

[private]
android-consumer-build: publish-android
    {{android_run}} python -m tools.run_android -- rtk gradle -p tests/consumers/android :app:assembleDebug :app:assembleDebugAndroidTest --no-daemon

# Run the Stable UniFFI Android ART and published-AAR production ABI runtime gates.
# The project-owned runner uses adb push + pm install, avoiding Gradle UTP streamed-install hangs.
android device="auto": android-build android-consumer-build
    {{android_run}} python -m tools.android.run_instrumentation --device {{device}} --test-apk tests/ffi/runtime/android/bindings/build/outputs/apk/androidTest/debug/bindings-debug-androidTest.apk --test-component dev.blehub.ffi.runtime.android.test/androidx.test.runner.AndroidJUnitRunner --test-class dev.blehub.ffi.runtime.UniFfiAndroidRuntimeTest --log .work/android-instrumentation/production-bindings.log
    {{android_run}} python -m tools.android.run_instrumentation --device {{device}} --app-apk tests/consumers/android/app/build/outputs/apk/debug/app-debug.apk --test-apk tests/consumers/android/app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk --test-component dev.blehub.ffi.runtime.consumer.test/androidx.test.runner.AndroidJUnitRunner --test-class dev.blehub.ffi.runtime.consumer.PublishedArtifactConsumerTest --log .work/android-instrumentation/production-consumer.log

# Host-only checks for the formal BLeng package export and native-image audit.
android-bleng-test-tools:
    {{android_run}} python -m unittest tools.test_package_bleng_android

# Formal blehub+bleng AAR and artifact-only APK build.
# Static only: never selects, installs on, or starts an Android device.
android-bleng-build:
    {{android_run}} python -m tools.package_bleng_android

# Re-audit the staged BLeng AAR ELFs with NDK llvm-nm, without rebuilding.
android-bleng-audit:
    {{android_run}} python -m tools.package_bleng_android --audit-only

# Explicit BLeng ART gate. Installs through adb push + pm install, so no UI
# confirmation or Gradle streamed install is required.
android-bleng device="physical": android-bleng-build
    {{android_run}} python -m tools.android.run_instrumentation --device {{device}} --app-apk .work/android-bleng/consumer/app/outputs/apk/debug/app-debug.apk --test-apk .work/android-bleng/consumer/app/outputs/apk/androidTest/debug/app-debug-androidTest.apk --test-component dev.blehub.bleng.consumer.test/androidx.test.runner.AndroidJUnitRunner --test-class dev.blehub.bleng.PublishedBlengTest --log .work/android-bleng/logs/art-instrumentation.log
