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

# Compile the Apple backend and current BLeng package for generic iOS destinations.
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

# Run iPhone-to-nRF52840 Notification with explicit host/device marker bridging.
[macos]
rf-ios-nrf52840-notification profile="tests/conformance/scenarios/blehub-nrf-indication-v2.json" write_with_response="10" write_without_response="ab": prepare-ios-device
    set -eu; coordination="$PWD/.work/peripheral/nrf52840/ios-notification-coordination"; rm -rf "$coordination"; mkdir -p "$coordination"; trap 'pixi run just peripheral nrf52840-stop >/dev/null 2>&1 || true' EXIT; pixi run python -m tests.peripherals.nrf52840.provider direct-start --coordination-path "$coordination" --coordination-mode notification; {{apple_run}} python -m tools.apple.test_ios_runtime runtime-rf-external notification --profile {{quote(profile)}} --write-with-response {{quote(write_with_response)}} --write-without-response {{quote(write_without_response)}} --coordination-path "$coordination"

# Run strict iPhone-to-nRF52840 Indication on the indication-only characteristic.
[macos]
rf-ios-nrf52840-indication profile="tests/conformance/scenarios/blehub-nrf-indication-v2.json" write_with_response="10" write_without_response="ab": prepare-ios-device
    set -eu; coordination="$PWD/.work/peripheral/nrf52840/ios-indication-coordination"; rm -rf "$coordination"; mkdir -p "$coordination"; trap 'pixi run just peripheral nrf52840-stop >/dev/null 2>&1 || true' EXIT; pixi run python -m tests.peripherals.nrf52840.provider direct-start --coordination-path "$coordination" --coordination-mode indication; {{apple_run}} python -m tools.apple.test_ios_runtime runtime-rf-external indication --profile {{quote(profile)}} --write-with-response {{quote(write_with_response)}} --write-without-response {{quote(write_without_response)}} --coordination-path "$coordination"

# Run connected RSSI on iPhone-to-nRF52840 without provider coordination.
[macos]
rf-ios-nrf52840-rssi profile="tests/conformance/scenarios/blehub-nrf-indication-v2.json" write_with_response="10" write_without_response="ab": prepare-ios-device
    set -eu; trap 'pixi run just peripheral nrf52840-stop >/dev/null 2>&1 || true' EXIT; pixi run just peripheral nrf52840-direct-start; {{apple_run}} python -m tools.apple.test_ios_runtime runtime-rf-external rssi --profile {{quote(profile)}} --write-with-response {{quote(write_with_response)}} --write-without-response {{quote(write_without_response)}}

# Trigger a real nRF52840 Peripheral-initiated disconnect and verify iOS recovery markers.
[macos]
rf-ios-nrf52840-passive-disconnect profile="tests/conformance/scenarios/blehub-nrf-indication-v2.json" write_with_response="10" write_without_response="ab": prepare-ios-device
    set -eu; coordination="$PWD/.work/peripheral/nrf52840/ios-passive-disconnect-coordination"; rm -rf "$coordination"; mkdir -p "$coordination"; trap 'pixi run just peripheral nrf52840-stop >/dev/null 2>&1 || true' EXIT; pixi run python -m tests.peripherals.nrf52840.provider direct-start --coordination-path "$coordination" --coordination-mode passive-disconnect; {{apple_run}} python -m tools.apple.test_ios_runtime runtime-rf-external passive-disconnect --profile {{quote(profile)}} --write-with-response {{quote(write_with_response)}} --write-without-response {{quote(write_without_response)}} --coordination-path "$coordination"

# Run the three-cycle Peripheral-loss/recovery contract on iPhone and nRF52840.
[macos]
rf-ios-nrf52840-recovery profile="tests/conformance/scenarios/blehub-nrf-indication-v2.json" write_with_response="10" write_without_response="ab": prepare-ios-device
    set -eu; coordination="$PWD/.work/peripheral/nrf52840/ios-recovery-coordination"; rm -rf "$coordination"; mkdir -p "$coordination"; trap 'pixi run just peripheral nrf52840-stop >/dev/null 2>&1 || true' EXIT; pixi run python -m tests.peripherals.nrf52840.provider direct-start --coordination-path "$coordination" --coordination-mode recovery-ios; {{apple_run}} python -m tools.apple.test_ios_runtime runtime-rf-external recovery --profile {{quote(profile)}} --write-with-response {{quote(write_with_response)}} --write-without-response {{quote(write_without_response)}} --coordination-path "$coordination"

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

# Build the two-component BLeng image and run an artifact-only macOS lifecycle consumer.
# This does not modify the default apple-production package or require Bluetooth hardware.
[macos]
consumer-bleng-macos:
    pixi run just ffi apple-bleng-package
    {{apple_run}} swift run --quiet --scratch-path .work/apple-bleng-consumer --package-path tests/consumers/apple/bleng-macos --configuration release BlengArtifactConsumer

# Independently link BLeng in unsigned iOS device + Simulator apps.
# No keychain, connected device, CoreBluetooth runtime, or RF access.
[macos]
consumer-bleng-ios-unsigned:
    pixi run just ffi apple-bleng-package
    {{apple_run}} python -m tools.apple.consumer_bleng_ios build-unsigned

# Sign/install/run the BLeng fake-backend lifecycle on a connected iPhone.
# Keychain preparation is a human-only step; this recipe never reads or accepts a password.
[macos]
consumer-bleng-ios-device:
    pixi run just ffi apple-bleng-package
    {{apple_run}} python -m tools.apple.consumer_bleng_ios device

# Build BLeng + the production CoreBluetooth Swift backend in an
# isolated iOS HIL app. Initialization only; no scan, Peripheral, DK or RF.
[macos]
bleng-ios-apple-init-build:
    pixi run just ffi apple-bleng-package
    {{apple_run}} python -m tools.apple.bleng_ios_backend build-unsigned

# Explicitly sign/install/launch the same initialization-only HIL on iPhone.
[macos]
bleng-ios-apple-init-device:
    pixi run just ffi apple-bleng-package
    {{apple_run}} python -m tools.apple.bleng_ios_backend device

# Explicit BLeng + production CoreBluetooth iPhone scan RF against the
# managed standalone DK. The provider owns the BTP port and is always stopped.
[macos]
bleng-ios-rf-scan:
    pixi run just ffi apple-bleng-package
    set -eu; trap 'pixi run just peripheral nrf52840-stop >/dev/null 2>&1 || true' EXIT; pixi run just peripheral nrf52840-direct-start; {{apple_run}} python -m tools.apple.bleng_ios_backend rf-scan

# Extend the BLeng RF scan to connect and actively disconnect the DK.
# The same provider owns the only BTP port and is stopped on every exit path.
[macos]
bleng-ios-rf-connection:
    pixi run just ffi apple-bleng-package
    set -eu; trap 'pixi run just peripheral nrf52840-stop >/dev/null 2>&1 || true' EXIT; pixi run just peripheral nrf52840-direct-start; {{apple_run}} python -m tools.apple.bleng_ios_backend rf-connection

# Validate the BLeng-discovered nRF service, GATT instance IDs, characteristic
# properties and subscription descriptors before active disconnect and cleanup.
[macos]
bleng-ios-rf-gatt:
    pixi run just ffi apple-bleng-package
    set -eu; trap 'pixi run just peripheral nrf52840-stop >/dev/null 2>&1 || true' EXIT; pixi run just peripheral nrf52840-direct-start; {{apple_run}} python -m tools.apple.bleng_ios_backend rf-gatt

# Verify BLeng characteristic reads, both write modes, and delayed readback.
# No-response success remains local submission, not remote acknowledgement.
[macos]
bleng-ios-rf-io:
    pixi run just ffi apple-bleng-package
    set -eu; trap 'pixi run just peripheral nrf52840-stop >/dev/null 2>&1 || true' EXIT; pixi run just peripheral nrf52840-direct-start; {{apple_run}} python -m tools.apple.bleng_ios_backend rf-io

# Compare the BLeng readRssi result to the event for the active DK connection.
[macos]
bleng-ios-rf-rssi:
    pixi run just ffi apple-bleng-package
    set -eu; trap 'pixi run just peripheral nrf52840-stop >/dev/null 2>&1 || true' EXIT; pixi run just peripheral nrf52840-direct-start; {{apple_run}} python -m tools.apple.bleng_ios_backend rf-rssi

# Coordinated BLeng/CoreBluetooth notification RF against the standalone DK.
# The runner installs the app before starting the fixture and owns its PID cleanup.
[macos]
bleng-ios-rf-notification:
    pixi run just ffi apple-bleng-package
    {{apple_run}} python -m tools.apple.bleng_ios_backend rf-notification

# Coordinated strict Indication on FFF5, with FFF2 strict-mode rejection.
# Explicit opt-in only; the runner owns fixture startup and PID cleanup.
[macos]
bleng-ios-rf-indication:
    pixi run just ffi apple-bleng-package
    {{apple_run}} python -m tools.apple.bleng_ios_backend rf-indication

# Peripheral-triggered loss with an active EF Notification, stale-handle rejection,
# reconnect/discovery/readback. Build/install precedes owned provider startup.
[macos]
bleng-ios-rf-passive-disconnect profile="tests/conformance/scenarios/blehub-nrf-indication-v2.json" notification_value_hex="ef" initial_hex="00":
    pixi run just ffi apple-bleng-package
    {{apple_run}} python -m tools.apple.bleng_ios_backend rf-passive-disconnect --profile {{quote(profile)}} --notification-value-hex {{quote(notification_value_hex)}} --initial-hex {{quote(initial_hex)}}

# Three recovery-ios cycles on one BLeng owner; the recovered connection remains
# live into the next cycle. Explicit RF opt-in, PID-scoped fixture cleanup.
[macos]
bleng-ios-rf-recovery profile="tests/conformance/scenarios/blehub-nrf-indication-v2.json" notification_value_hex="ef" initial_hex="00":
    pixi run just ffi apple-bleng-package
    {{apple_run}} python -m tools.apple.bleng_ios_backend rf-recovery --profile {{quote(profile)}} --notification-value-hex {{quote(notification_value_hex)}} --initial-hex {{quote(initial_hex)}}

# Independently compile the staged production package from an iOS-only consumer graph.
[macos]
consumer-ios: prepare
    {{apple_run}} python -m tools.apple.consumer_ios build-unsigned

# Explicit opt-in: development-sign, install, launch, and verify the artifact-only iPhone app.
[macos]
consumer-ios-device: prepare
    {{apple_run}} python -m tools.apple.consumer_ios device

# Run all non-RF checks for the current Apple backend slice.
[macos]
check: prepare
    {{apple_run}} python -m tools.apple.test_backend check
