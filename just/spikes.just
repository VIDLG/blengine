ffi-spike-check-class-handle-param:
    python tools/check_expected_cargo_failure.py --manifest-path spikes/boltffi-class-handle-callback-param/Cargo.toml --expected "unsupported expansion: callback method parameter shape"

ffi-spike-test-lifecycle:
    rtk cargo test --manifest-path spikes/boltffi-lifecycle/Cargo.toml

ffi-spike-build-uniffi-lifecycle:
    {{jvm_run}} rtk cargo build --manifest-path spikes/uniffi-lifecycle/Cargo.toml

ffi-spike-generate-uniffi-kotlin: ffi-spike-build-uniffi-lifecycle
    {{jvm_run}} rtk cargo run --manifest-path spikes/uniffi-lifecycle/Cargo.toml --features bindgen --bin uniffi-bindgen -- generate .work/cargo-target/debug/blehub_uniffi_lifecycle.dll --language kotlin --out-dir spikes/uniffi-lifecycle/generated/kotlin --no-format

ffi-spike-test-uniffi-kotlin-jvm: ffi-spike-generate-uniffi-kotlin
    {{jvm_run}} rtk gradlew -p spikes/uniffi-lifecycle/platforms/kotlin-jvm test --no-daemon

ffi-spike-generate-uniffi-swift: ffi-spike-build-uniffi-lifecycle
    {{jvm_run}} rtk cargo run --manifest-path spikes/uniffi-lifecycle/Cargo.toml --features bindgen --bin uniffi-bindgen -- generate .work/cargo-target/debug/blehub_uniffi_lifecycle.dll --language swift --out-dir spikes/uniffi-lifecycle/generated/swift --no-format

ffi-spike-test-uniffi-swift-windows: ffi-spike-generate-uniffi-swift swift-doctor
    python -m tools.test_uniffi_swift_windows

ffi-spike-build-uniffi-macos:
    rtk cargo build --locked --manifest-path spikes/uniffi-lifecycle/Cargo.toml

ffi-spike-generate-uniffi-swift-macos: ffi-spike-build-uniffi-macos
    rtk cargo run --locked --manifest-path spikes/uniffi-lifecycle/Cargo.toml --features bindgen --bin uniffi-bindgen -- generate .work/cargo-target/debug/libblehub_uniffi_lifecycle.dylib --language swift --out-dir spikes/uniffi-lifecycle/generated/swift-macos --no-format

ffi-spike-test-uniffi-swift-macos: ffi-spike-generate-uniffi-swift-macos
    python -m tools.test_uniffi_swift_macos

ffi-spike-pack-uniffi-apple:
    python -m tools.package_uniffi_apple

ffi-spike-test-uniffi-apple-package-consumer: ffi-spike-pack-uniffi-apple
    python -m tools.test_uniffi_apple_package

ffi-spike-generate-uniffi-android-kotlin: ffi-spike-build-uniffi-lifecycle
    {{jvm_run}} rtk cargo run --manifest-path spikes/uniffi-lifecycle/Cargo.toml --features bindgen --bin uniffi-bindgen -- generate .work/cargo-target/debug/blehub_uniffi_lifecycle.dll --language kotlin --config spikes/uniffi-lifecycle/uniffi.android.toml --out-dir spikes/uniffi-lifecycle/generated/android-kotlin --no-format

ffi-spike-build-uniffi-android-native: android-doctor
    {{android_run}} python -m tools.build_uniffi_android

ffi-spike-publish-uniffi-android: ffi-spike-generate-uniffi-android-kotlin ffi-spike-build-uniffi-android-native
    {{android_run}} python -m tools.run_android -- rtk gradle -p spikes/uniffi-lifecycle/platforms/android :bindings:assembleDebug :bindings:publishReleasePublicationToFixtureRepository --no-daemon

ffi-spike-test-uniffi-android device="auto": ffi-spike-publish-uniffi-android
    {{android_run}} python -m tools.run_android --device {{device}} -- rtk gradle -p spikes/uniffi-lifecycle/platforms/android :bindings:connectedDebugAndroidTest --no-daemon
    {{android_run}} python -m tools.run_android --device {{device}} -- rtk gradle -p spikes/uniffi-lifecycle/platforms/android-consumer :app:connectedDebugAndroidTest --no-daemon

# Compatibility name retained for existing docs; target selection is now auto.
ffi-spike-test-uniffi-android-emulator: ffi-spike-publish-uniffi-android
    {{android_run}} python -m tools.run_android --device auto -- rtk gradle -p spikes/uniffi-lifecycle/platforms/android :bindings:connectedDebugAndroidTest --no-daemon
    {{android_run}} python -m tools.run_android --device auto -- rtk gradle -p spikes/uniffi-lifecycle/platforms/android-consumer :app:connectedDebugAndroidTest --no-daemon

ffi-spike-build-uniffi-error-field-collision:
    {{jvm_run}} rtk cargo build --manifest-path spikes/uniffi-error-field-collision/Cargo.toml

ffi-spike-generate-uniffi-error-field-collision: ffi-spike-build-uniffi-error-field-collision
    {{jvm_run}} rtk cargo run --manifest-path spikes/uniffi-error-field-collision/Cargo.toml --features bindgen --bin uniffi-bindgen -- generate .work/cargo-target/debug/blehub_uniffi_error_field_collision.dll --language kotlin --out-dir spikes/uniffi-error-field-collision/generated/kotlin --no-format

ffi-spike-check-uniffi-error-field-collision: ffi-spike-generate-uniffi-error-field-collision
    {{jvm_run}} python tools/check_expected_gradle_failure.py --project-dir spikes/uniffi-error-field-collision/platforms/kotlin-jvm --task compileKotlin --expected "Conflicting declarations:" --expected "hides member of supertype 'Throwable'"

ffi-spike-generate-kotlin:
    python -m tools.run_boltffi_cli --project spikes/boltffi-lifecycle generate kotlin --deny-skipped

ffi-phase0-pack-android: boltffi-doctor android-doctor
    {{android_run}} python -m tools.run_android -- python -m tools.run_boltffi_cli --project spikes/boltffi-token-ingress pack android --deny-skipped

ffi-phase0-build-android-aar: ffi-phase0-pack-android
    {{android_run}} python -m tools.run_android -- rtk gradle -p spikes/boltffi-token-ingress/platforms/android :bindings:assembleDebug --no-daemon

ffi-phase0-test-android device="auto": ffi-phase0-build-android-aar
    {{android_run}} python -m tools.run_android --device {{device}} -- rtk gradle -p spikes/boltffi-token-ingress/platforms/android :bindings:connectedDebugAndroidTest --no-daemon
    {{android_run}} python -m tools.run_android --device {{device}} -- rtk gradle -p spikes/boltffi-token-ingress/platforms/android-consumer :app:connectedDebugAndroidTest --no-daemon

# Compatibility name retained for existing docs; target selection is now auto.
ffi-phase0-test-android-emulator: ffi-phase0-build-android-aar
    {{android_run}} python -m tools.run_android --device auto -- rtk gradle -p spikes/boltffi-token-ingress/platforms/android :bindings:connectedDebugAndroidTest --no-daemon
    {{android_run}} python -m tools.run_android --device auto -- rtk gradle -p spikes/boltffi-token-ingress/platforms/android-consumer :app:connectedDebugAndroidTest --no-daemon

ffi-spike-build-windows-jvm-bridge: ffi-spike-generate-kotlin
    {{jvm_run}} python -m tools.build_windows_jvm_bridge

ffi-spike-test-kotlin-jvm: ffi-spike-build-windows-jvm-bridge
    {{jvm_run}} rtk gradlew -p spikes/boltffi-lifecycle/platforms/kotlin-jvm test sinkExceptionProbe --no-daemon

ffi-phase0-generate-kotlin:
    python -m tools.run_boltffi_cli --project spikes/boltffi-token-ingress generate kotlin --deny-skipped

ffi-phase0-generate-swift:
    python -m tools.run_boltffi_cli --project spikes/boltffi-token-ingress generate swift --deny-skipped

ffi-phase0-build-windows-swift-native: ffi-phase0-generate-swift
    rtk cargo build --manifest-path spikes/boltffi-token-ingress/Cargo.toml --target x86_64-pc-windows-msvc

ffi-phase0-test-swift-windows: ffi-phase0-build-windows-swift-native
    python -m tools.test_swift_windows

ffi-phase0-build-macos-swift-native: ffi-phase0-generate-swift
    rtk cargo build --manifest-path spikes/boltffi-token-ingress/Cargo.toml

ffi-phase0-test-swift-macos: ffi-phase0-build-macos-swift-native
    python -m tools.test_swift_macos

ffi-phase0-pack-apple: boltffi-doctor
    python -m tools.run_boltffi_cli --project spikes/boltffi-token-ingress pack apple --deny-skipped

ffi-phase0-test-apple-package-consumer: ffi-phase0-pack-apple
    python -m tools.test_boltffi_apple_package

ffi-phase0-build-windows-jvm-bridge: ffi-phase0-generate-kotlin
    {{jvm_run}} python -m tools.build_windows_jvm_bridge --project spikes/boltffi-token-ingress

ffi-phase0-test-kotlin-ack-workaround: ffi-phase0-build-windows-jvm-bridge
    {{jvm_run}} rtk gradlew -p spikes/boltffi-token-ingress/platforms/kotlin-jvm test --tests dev.blehub.phase0.UnitResultExceptionWorkaroundTest --no-daemon

ffi-phase0-test-kotlin-jvm: ffi-phase0-build-windows-jvm-bridge
    {{jvm_run}} rtk gradlew -p spikes/boltffi-token-ingress/platforms/kotlin-jvm jar test --no-daemon
    {{jvm_run}} rtk gradlew -p spikes/boltffi-token-ingress/platforms/kotlin-jvm-consumer test --no-daemon
