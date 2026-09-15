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

# Run one explicitly selected iPhone-to-Android RF scenario.
[macos]
runtime-ios-android-scenario scenario="notification": prepare-ios-device
    set -eu; trap 'pixi run just peripheral android-stop >/dev/null 2>&1 || true' EXIT; pixi run just peripheral android-ensure; {{apple_run}} python -m tools.apple.test_ios_runtime runtime-rf-android {{scenario}}

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


# External fixture must already be advertising; these recipes never start/stop a provider.
# Supported macOS scenarios: scan, connection, gatt, io, rssi. Reset IO initial value before each run.
[macos]
rf-macos-external profile scenario="io" write_with_response="102030" write_without_response="abcd": prepare-hil
    {{apple_run}} python -m tools.apple.test_macos_runtime rf-external {{quote(scenario)}} --profile {{quote(profile)}} --write-with-response {{quote(write_with_response)}} --write-without-response {{quote(write_without_response)}}

# iPhone IO includes scan/connect/discovery, exact readbacks, disconnect and the complete ledger.
[macos]
rf-ios-external profile write_with_response="102030" write_without_response="abcd": prepare-ios-device
    {{apple_run}} python -m tools.apple.test_ios_runtime runtime-rf-external io --profile {{quote(profile)}} --write-with-response {{quote(write_with_response)}} --write-without-response {{quote(write_without_response)}}

# Start the project-managed external nRF52840 fixture and run the iPhone IO RF gate.
[macos]
rf-ios-nrf52840 profile="tests/conformance/scenarios/blehub-nrf-indication-v2.json" write_with_response="10" write_without_response="ab": prepare-ios-device
    set -eu; trap 'pixi run just peripheral nrf52840-stop >/dev/null 2>&1 || true' EXIT; pixi run just peripheral nrf52840-direct-start; {{apple_run}} python -m tools.apple.test_ios_runtime runtime-rf-external io --profile {{quote(profile)}} --write-with-response {{quote(write_with_response)}} --write-without-response {{quote(write_without_response)}}

# Start the project-managed external nRF52840 fixture and run the macOS IO RF gate.
[macos]
rf-macos-nrf52840 profile="tests/conformance/scenarios/blehub-nrf-indication-v2.json" write_with_response="10" write_without_response="ab": prepare-hil
    set -eu; trap 'pixi run just peripheral nrf52840-stop >/dev/null 2>&1 || true' EXIT; pixi run just peripheral nrf52840-direct-start; {{apple_run}} python -m tools.apple.test_macos_runtime rf-external io --profile {{quote(profile)}} --write-with-response {{quote(write_with_response)}} --write-without-response {{quote(write_without_response)}}

# Run the provider-owned nRF Notification RF gate. The nRF process owns the serial port
# and applies the value only after the macOS probe confirms CCCD enablement.
[macos]
rf-macos-nrf52840-notification profile="tests/conformance/scenarios/blehub-nrf-indication-v2.json" write_with_response="10" write_without_response="ab": prepare-hil
    set -eu; coordination="$PWD/.work/peripheral/nrf52840/macos-notification-coordination"; rm -rf "$coordination"; mkdir -p "$coordination"; trap 'pixi run just peripheral nrf52840-stop >/dev/null 2>&1 || true' EXIT; pixi run python -m tests.peripherals.nrf52840.provider direct-start --coordination-path "$coordination"; {{apple_run}} python -m tools.apple.test_macos_runtime rf-external notification --profile {{quote(profile)}} --write-with-response {{quote(write_with_response)}} --write-without-response {{quote(write_without_response)}} --coordination-path "$coordination"

# Run strict Indication on the nRF updates characteristic. The profile has both
# notify and indicate properties, so the Apple probe explicitly requests Indication.
[macos]
rf-macos-nrf52840-indication profile="tests/conformance/scenarios/blehub-nrf-indication-v2.json" write_with_response="10" write_without_response="ab": prepare-hil
    set -eu; coordination="$PWD/.work/peripheral/nrf52840/macos-indication-coordination"; rm -rf "$coordination"; mkdir -p "$coordination"; trap 'pixi run just peripheral nrf52840-stop >/dev/null 2>&1 || true' EXIT; pixi run python -m tests.peripherals.nrf52840.provider direct-start --coordination-path "$coordination" --coordination-mode indication; {{apple_run}} python -m tools.apple.test_macos_runtime rf-external indication --profile {{quote(profile)}} --write-with-response {{quote(write_with_response)}} --write-without-response {{quote(write_without_response)}} --coordination-path "$coordination"

# Run the provider-owned RSSI RF gate.
[macos]
rf-macos-nrf52840-rssi profile="tests/conformance/scenarios/blehub-nrf-indication-v2.json" write_with_response="10" write_without_response="ab": prepare-hil
    set -eu; trap 'pixi run just peripheral nrf52840-stop >/dev/null 2>&1 || true' EXIT; pixi run just peripheral nrf52840-direct-start; {{apple_run}} python -m tools.apple.test_macos_runtime rf-external rssi --profile "$PWD/{{profile}}" --write-with-response {{quote(write_with_response)}} --write-without-response {{quote(write_without_response)}}

# Trigger a real Peripheral-initiated disconnect through the nRF BTP GAP service.
[macos]
rf-macos-nrf52840-passive-disconnect profile="tests/conformance/scenarios/blehub-nrf-indication-v2.json" write_with_response="10" write_without_response="ab": prepare-hil
    set -eu; coordination="$PWD/.work/peripheral/nrf52840/macos-passive-disconnect-coordination"; rm -rf "$coordination"; mkdir -p "$coordination"; trap 'pixi run just peripheral nrf52840-stop >/dev/null 2>&1 || true' EXIT; pixi run python -m tests.peripherals.nrf52840.provider direct-start --coordination-path "$coordination" --coordination-mode passive-disconnect; {{apple_run}} python -m tools.apple.test_macos_runtime rf-external passive-disconnect --profile {{quote(profile)}} --write-with-response {{quote(write_with_response)}} --write-without-response {{quote(write_without_response)}} --coordination-path "$coordination"

# Run the active macOS nRF v2 RF matrix, including indication-only coverage.
[macos]
rf-macos-nrf52840-all: prepare-hil
    pixi run just apple rf-macos-nrf52840
    pixi run just apple rf-macos-nrf52840-notification
    pixi run just apple rf-macos-nrf52840-rssi
    pixi run just apple rf-macos-nrf52840-passive-disconnect

# Compile/sign the macOS HIL bundle without launching CoreBluetooth or performing RF.
[macos]
build-macos-hil: prepare-hil
    {{apple_run}} python -m tools.apple.test_macos_runtime build

# Build the real iPhone HIL sources in a separate unsigned project; no device or keychain access.
[macos]
build-ios-hil: prepare-ios-device
    {{apple_run}} python -m tools.apple.test_ios_runtime build-unsigned

# Pure host profile/dispatch regression tests; no package build or hardware access.
[macos]
test-rf-host:
    {{apple_run}} python -m unittest tools.apple.test_rf_fixture

# Build the independent macOS consumer from the staged production Swift package only.
[macos]
consumer-macos: prepare
    {{apple_run}} swift run --quiet --package-path tests/consumers/apple/macos --configuration release BleHubAppleArtifactConsumer

# Run all non-RF checks for the current Apple backend slice.
[macos]
check: prepare
    {{apple_run}} python -m tools.apple.test_backend check
