set working-directory := "../.."

# Stable UniFFI cross-language runtime gates for the production BleHub ABI.

bindgen_run := "pixi run -e jvm"
android_run := "pixi run -e android"
apple_run := "pixi run -e apple-ci"
# Set BLEHUB_VERBOSE=true for first-run Cargo/Gradle diagnostics without rtk.
verbose := env_var_or_default("BLEHUB_VERBOSE", "false")
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
    {{bindgen_run}} {{if verbose == "true" { "" } else { "rtk " }}}cargo {{if verbose == "true" { "" } else { "--quiet " }}}build --locked --manifest-path {{uniffi_manifest}}


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
    pixi run python -m tools.artifact_fingerprint write apple-production

# Formal two-component BLeng package. It is a separate package line from
# apple-package and does not make blehub-uniffi depend on BLeng.
[macos]
apple-bleng-package:
    {{apple_run}} python -m tools.swift_toolchain verify
    {{apple_run}} python -m tools.apple.package_uniffi --bleng

[private]
generate-android-kotlin: build
    {{bindgen_run}} {{if verbose == "true" { "" } else { "rtk " }}}cargo run {{if verbose == "true" { "" } else { "--quiet " }}}--locked --manifest-path {{uniffi_manifest}} --features bindgen --bin uniffi-bindgen -- generate {{uniffi_library}} --language kotlin --config tests/ffi/config/uniffi.android.toml --out-dir {{generated_root}}/android-kotlin --no-format

[private]
build-android-native:
    {{android_run}} python -m tools.android_toolchain verify
    {{android_run}} python -m tools.build_uniffi_android --manifest {{uniffi_manifest}} --output-root {{generated_root}}/android-jni --library-name libblehub_uniffi.so

[private]
publish-android: generate-android-kotlin build-android-native
    {{android_run}} python -m tools.run_android -- {{if verbose == "true" { "gradle" } else { "rtk gradle" }}} -p tests/ffi/runtime/android :bindings:assembleRelease :bindings:publishReleasePublicationToFixtureRepository --no-daemon

# Incrementally compile production bindings/backend and instrumentation; never use a device.
android-build: publish-android
    {{android_run}} python -m tools.run_android -- {{if verbose == "true" { "gradle" } else { "rtk gradle" }}} -p tests/ffi/runtime/android :bindings:assembleDebugAndroidTest --no-daemon
    {{android_run}} python -m tools.run_android -- {{if verbose == "true" { "gradle" } else { "rtk gradle" }}} -p platforms/android :library:assembleDebug --no-daemon

# Build the production Android Central backend library against the generated
# production binding and the same staged native image. This is host-only: it
# does not install, start, or claim Android RF.
android-backend-build: android-build

# Check production backend code against already staged bindings without publishing,
# changing staging, installing an APK, or operating a device. Optional Gradle test filters
# narrow deterministic JVM execution; debug/release backend artifacts are still rebuilt.
android-backend-check *test_args:
    {{android_run}} python -m tools.run_android -- {{if verbose == "true" { "gradle" } else { "rtk gradle" }}} -p platforms/android :library:testDebugUnitTest {{test_args}} :library:assembleDebug :library:assembleRelease --no-daemon

# Hardware-free production queue/watchdog/retirement regression discovery.
# Build canonical bindings first; this does not install or operate a phone.
android-backend-test: android-build android-backend-check

android-backend-publish: android-build
    {{android_run}} python -m tools.run_android -- {{if verbose == "true" { "gradle" } else { "rtk gradle" }}} -p platforms/android :library:assembleRelease :library:publishReleasePublicationToFixtureRepository --no-daemon
    pixi run python -m tools.artifact_fingerprint write android-production

# Build the clean Android consumer against the published backend AAR. The
# consumer still resolves the canonical UniFFI/native-image AAR transitively.
android-backend-consumer-build: android-backend-publish
    {{android_run}} python -m tools.run_android -- {{if verbose == "true" { "gradle" } else { "rtk gradle" }}} -p tests/consumers/android/blehub :app:assembleDebug :app:assembleDebugAndroidTest --no-daemon

# Run only the backend-AAR consumer lifecycle test through the project-owned
# adb push + pm install instrumentation runner.
android-backend-consumer device="physical": android-backend-consumer-build
    {{android_run}} python -m tools.android.run_instrumentation --device {{device}} --app-apk tests/consumers/android/blehub/app/build/outputs/apk/debug/app-debug.apk --test-apk tests/consumers/android/blehub/app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk --test-component dev.blehub.ffi.runtime.consumer.test/androidx.test.runner.AndroidJUnitRunner --test-class dev.blehub.ffi.runtime.consumer.PublishedArtifactConsumerTest --log .work/android-instrumentation/android-backend-consumer.log

# Explicit clean candidate reconstruction; ordinary checks keep Cargo/Gradle incremental state.
# Only generated bindings/JNI and the three owning Gradle build outputs are cleaned.
android-candidate-build:
    {{android_run}} python -m tools.run_android -- {{if verbose == "true" { "gradle" } else { "rtk gradle" }}} -p tests/ffi/runtime/android :bindings:clean --no-daemon
    {{android_run}} python -m tools.run_android -- {{if verbose == "true" { "gradle" } else { "rtk gradle" }}} -p platforms/android :library:clean --no-daemon
    {{android_run}} python -m tools.run_android -- {{if verbose == "true" { "gradle" } else { "rtk gradle" }}} -p tests/consumers/android/blehub :app:clean --no-daemon
    rm -rf {{generated_root}}/android-kotlin {{generated_root}}/android-jni
    pixi run just ffi android-backend-consumer-build

# Opt-in direct Kotlin RF smoke in the existing artifact consumer; never provisions firmware.
# Build once before run; the host still requires live fixture/phone leases before install/launch.
android-native-hil device port contract profile timeout="120":
    {{android_run}} python -m tools.android.native_hil --device {{ quote(device) }} --port {{ quote(port) }} --contract {{ quote(contract) }} --profile {{ quote(profile) }} --timeout {{ quote(timeout) }}

# Run the Stable UniFFI Android ART and published-AAR production ABI runtime gates.
# The project-owned runner uses adb push + pm install, avoiding Gradle UTP streamed-install hangs.
# Set BLEHUB_VERBOSE=true on first setup to disable rtk through the build chain.
android device="auto": android-backend-consumer-build
    {{android_run}} python -m tools.android.run_instrumentation --device {{device}} --test-apk tests/ffi/runtime/android/bindings/build/outputs/apk/androidTest/debug/bindings-debug-androidTest.apk --test-component dev.blehub.ffi.runtime.android.test/androidx.test.runner.AndroidJUnitRunner --test-class dev.blehub.ffi.runtime.UniFfiAndroidRuntimeTest --log .work/android-instrumentation/production-bindings.log
    {{android_run}} python -m tools.android.run_instrumentation --device {{device}} --app-apk tests/consumers/android/blehub/app/build/outputs/apk/debug/app-debug.apk --test-apk tests/consumers/android/blehub/app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk --test-component dev.blehub.ffi.runtime.consumer.test/androidx.test.runner.AndroidJUnitRunner --test-class dev.blehub.ffi.runtime.consumer.PublishedArtifactConsumerTest --log .work/android-instrumentation/production-consumer.log

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
# confirmation or Gradle streamed install is required. `device` is the ADB serial.
android-bleng device: android-bleng-build
    test -n {{ quote(device) }}
    {{android_run}} python -m tools.android.run_instrumentation --device physical --serial {{ quote(device) }} --app-apk .work/android-bleng/consumer/app/outputs/apk/debug/app-debug.apk --test-apk .work/android-bleng/consumer/app/outputs/apk/androidTest/debug/app-debug-androidTest.apk --test-component dev.blehub.bleng.consumer.test/androidx.test.runner.AndroidJUnitRunner --test-class dev.blehub.bleng.PublishedBlengTest --log .work/android-bleng/logs/art-instrumentation.log
