set working-directory := "../.."

# Public invocation stays `pixi run just apple <command>`; Apple recipes
# re-enter the dedicated local Pixi environment for their implementation steps.
apple_run := "pixi run -e apple-ci"

[private]
prepare:
    {{apple_run}} python -m tools.swift_toolchain verify
    {{apple_run}} python -m tools.apple.package_uniffi

[private]
prepare-hil: prepare

[private]
prepare-ios-device: prepare-hil
    {{apple_run}} python -m tools.apple.xcodegen_toolchain verify

# Explicitly install the shared, version-checked XcodeGen development tool.
[macos]
install-tools:
    {{apple_run}} python -m tools.apple.xcodegen_toolchain install

# Interactively unlock the login keychain and authorize Apple signing tools without accepting a password argument.
[macos]
unlock-login-keychain:
    {{apple_run}} python -m tools.apple.test_ios_runtime unlock-keychain

# Verify connected-iPhone, signing-team, XcodeGen, and generated-project prerequisites.
[macos]
ios-device-doctor: prepare-ios-device
    {{apple_run}} python -m tools.apple.test_ios_runtime doctor

# Explicitly regenerate the ignored Xcode project. Automatic xcodebuild signing
# is attempted first; use ios-signing for manual Xcode setup if needed.
[macos]
ios-project-regenerate: prepare-ios-device
    {{apple_run}} python -m tools.apple.test_ios_runtime project-regenerate

# Open the current generated project in Xcode for interactive Signing & Capabilities setup.
[macos]
ios-signing: prepare-ios-device
    {{apple_run}} python -m tools.apple.test_ios_runtime signing

# Confirm that Xcode wrote a Team and automatic Apple Development signing settings.
[macos]
ios-project-confirm: prepare-ios-device
    {{apple_run}} python -m tools.apple.test_ios_runtime project-confirm

# Build, development-sign, install, launch, and validate CoreBluetooth initialization on iPhone.
[macos]
runtime-ios-device: prepare-ios-device
    {{apple_run}} python -m tools.apple.test_ios_runtime runtime


# Start the Android Peripheral and run the iPhone Central RF scenario.
[macos]
runtime-ios-android: prepare-ios-device
    set -eu; trap 'pixi run just peripheral android-stop >/dev/null 2>&1 || true' EXIT; pixi run just peripheral android-ensure; {{apple_run}} python -m tools.apple.test_ios_runtime runtime-rf-android

# Run deterministic Apple backend mapping and lifecycle tests on macOS.
[macos]
test: prepare
    {{apple_run}} python -m tools.apple.test_backend test
    {{apple_run}} python tests/hil/apple/support/test_rf_scenario.py

# Compile the Apple backend and current candidate for generic iOS destinations.
[macos]
build-ios: prepare
    {{apple_run}} python -m tools.apple.test_backend build-ios

# Build and run the bundled real-macOS CoreBluetooth initialization probe.
[macos]
runtime-macos: prepare-hil
    {{apple_run}} python -m tools.apple.test_macos_runtime runtime

# Launch the bundled probe through LaunchServices and wait for Bluetooth authorization.
[macos]
authorize-macos: prepare-hil
    {{apple_run}} python -m tools.apple.test_macos_runtime authorize

# Run the macOS-to-Android RF matrix. Use scenario=all or one of
# scan,connection,gatt,io,notification,indication,rssi,passive-disconnect.
[macos]
rf-macos-android scenario="all": prepare-hil
    set -eu; trap 'pixi run just peripheral android-stop >/dev/null 2>&1 || true' EXIT; pixi run just peripheral android-ensure; {{apple_run}} python -m tools.apple.test_macos_runtime rf-android {{scenario}}


# Build the independent macOS consumer from the staged production Swift package only.
[macos]
consumer-macos: prepare
    {{apple_run}} swift run --quiet --package-path tests/consumers/apple/macos --configuration release BleHubAppleArtifactConsumer

# Run all non-RF checks for the current Apple backend slice.
[macos]
check: prepare
    {{apple_run}} python -m tools.apple.test_backend check
