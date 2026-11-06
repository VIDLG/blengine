set working-directory := "../.."

# Public invocation stays `pixi run just apple <command>`; Apple recipes
# re-enter the dedicated local Pixi environment for their implementation steps.
apple_run := "pixi run -e apple-ci"

[private]
prepare:
    {{apple_run}} python -m tools.swift_toolchain verify
    {{apple_run}} python -m tools.apple.package_uniffi
    {{apple_run}} python -m tools.apple.stage_backend --uniffi-package .work/staged/apple-production --output .work/staged/apple-backend-production

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

# Read-only, scenario-specific observations; no package build or project generation.
# prebuilt skips signing-key/XcodeGen requirements; a device is queried only if explicit.
[macos]
ios-device-doctor mode="build-signing" app="" device="" xctestrun="":
    {{apple_run}} python -m tools.apple.ios_preflight --mode {{quote(mode)}} --app {{quote(app)}} --device {{quote(device)}} --xctestrun {{quote(xctestrun)}}

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

# Run the maintained Appium Python-client RN page smoke on one real iPhone.
# The caller must keep the iOS 18+ RemoteXPC tunnel alive; the runner refuses
# to start if the tunnel registry is unavailable. This does not start BLE/RF.
[macos]
appium-rn-smoke app device team appium_entry:
    {{apple_run}} python -m tools.apple.run_ios_appium_rn_smoke --app {{quote(app)}} --device {{quote(device)}} --team {{quote(team)}} --appium-entry {{quote(appium_entry)}}

# Read-only Settings/Bluetooth inspection through the maintained Appium client.
# This never changes Bluetooth and is not the system-loss mutation gate.
[macos]
appium-settings device team appium_entry:
    {{apple_run}} python -m tools.apple.run_ios_appium_settings --device {{quote(device)}} --team {{quote(team)}} --appium-entry {{quote(appium_entry)}}


# Retired unleased native shortcut; fail before builds, fixture control or cleanup.
[macos]
runtime-ios-android:
    {{apple_run}} python -c 'raise SystemExit("Retired unleased native RF shortcut; use apple rf-native-smoke with explicit v2 inputs and prebuilt App")'

# Retired per-scenario shortcut; the new native smoke requires explicit v2 inputs.
[macos]
runtime-ios-android-scenario scenario="notification":
    {{apple_run}} python -c 'raise SystemExit("Retired unleased native RF shortcut; use apple rf-native-smoke")'

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

# Retained compatibility identity; blocked before build/provider/BLE, with no cleanup.
# Android Peripheral is not an alternative to the missing v2 session admission.
[macos]
rf-macos-android scenario="all":
    {{apple_run}} python -m tools.apple.test_macos_runtime rf-android {{quote(scenario)}}


# Legacy Mac RF dispatch remains blocked; the new rf-native-smoke owner alone
# connects immutable v2 inputs, live receipts and identity-bound cleanup.
# A profile or an already-advertising fixture alone never authorizes execution.
[macos]
rf-macos-external profile="" scenario="io" write_with_response="102030" write_without_response="abcd":
    {{apple_run}} python -m tools.apple.test_macos_runtime rf-external {{quote(scenario)}} --profile {{quote(profile)}} --write-with-response {{quote(write_with_response)}} --write-without-response {{quote(write_without_response)}}

# Minimum normal native HIL: one fresh v2 IO or notification session; never builds.
# Run IO and notification separately; this is not the historical RF matrix.
[macos]
rf-native-smoke platform device port fixture_contract fixture_profile app scenario success_app="keep":
    {{apple_run}} python -m tools.apple.native_hil --platform {{quote(platform)}} --device {{quote(device)}} --port {{quote(port)}} --fixture-contract {{quote(fixture_contract)}} --fixture-profile {{quote(fixture_profile)}} --app {{quote(app)}} --scenario {{quote(scenario)}} --success-app {{quote(success_app)}}

# Retain old unleased shortcuts as fail-closed identities, before all prerequisites.
[macos]
rf-ios-external profile="" write_with_response="102030" write_without_response="abcd":
    {{apple_run}} python -c 'raise SystemExit("Retired unleased native RF shortcut; use apple rf-native-smoke")'

# Retired unleased iPhone IO shortcut; use explicit prebuilt rf-native-smoke.
[macos]
rf-ios-nrf52840 profile="" write_with_response="10" write_without_response="ab":
    {{apple_run}} python -c 'raise SystemExit("Retired unleased native RF shortcut; use apple rf-native-smoke")'

# Retired Notification shortcut; no global provider lifecycle or stale profile default.
[macos]
rf-ios-nrf52840-notification profile="" write_with_response="10" write_without_response="ab":
    {{apple_run}} python -c 'raise SystemExit("Retired unleased native RF shortcut; use apple rf-native-smoke notification")'

# Retired strict Indication shortcut; outside the minimal normal smoke.
[macos]
rf-ios-nrf52840-indication profile="" write_with_response="10" write_without_response="ab":
    {{apple_run}} python -c 'raise SystemExit("Retired unleased native RF shortcut; strict indication is outside native smoke")'

# Retired connected RSSI shortcut; outside the minimal normal smoke.
[macos]
rf-ios-nrf52840-rssi profile="" write_with_response="10" write_without_response="ab":
    {{apple_run}} python -c 'raise SystemExit("Retired unleased native RF shortcut; RSSI is outside native smoke")'

# Retired passive-loss shortcut; do not infer recovery coverage from normal smoke.
[macos]
rf-ios-nrf52840-passive-disconnect profile="" write_with_response="10" write_without_response="ab":
    {{apple_run}} python -c 'raise SystemExit("Retired unleased native RF shortcut; passive loss is outside native smoke")'

# Retired recovery shortcut; do not restore its global cleanup trap.
[macos]
rf-ios-nrf52840-recovery profile="" write_with_response="10" write_without_response="ab":
    {{apple_run}} python -c 'raise SystemExit("Retired unleased native RF shortcut; recovery is outside native smoke")'

# Native iOS HIL Flow 12: production AppleBleBackend/CoreBluetooth only; no RN/Nitro.
# The runner creates a fresh immutable session and owns only its provider identity.
[macos]
rf-ios-native-hil-ccc-disable-reject fixture_contract fixture_profile: prepare-ios-device
    {{apple_run}} python -m tools.apple.run_ios_native_hil_ccc_disable --fixture-contract {{quote(fixture_contract)}} --fixture-profile {{quote(fixture_profile)}}

# Retain IO diagnostics, but fail before build/fixture/BLE until v2 admission exists.
# No implicit profile/device selection, global stop, or cleanup of another owner.
[macos]
rf-macos-nrf52840 profile="" write_with_response="10" write_without_response="ab":
    {{apple_run}} python -m tools.apple.test_macos_runtime rf-external io --profile {{quote(profile)}} --write-with-response {{quote(write_with_response)}} --write-without-response {{quote(write_without_response)}}

# Blocked Notification identity; subscription marker bridging remains unimplemented.
[macos]
rf-macos-nrf52840-notification profile="" write_with_response="10" write_without_response="ab":
    {{apple_run}} python -m tools.apple.test_macos_runtime rf-external notification --profile {{quote(profile)}} --write-with-response {{quote(write_with_response)}} --write-without-response {{quote(write_without_response)}}

# Blocked strict Indication identity, targeting the indication-only FFF5 characteristic.
[macos]
rf-macos-nrf52840-indication profile="" write_with_response="10" write_without_response="ab":
    {{apple_run}} python -m tools.apple.test_macos_runtime rf-external indication --profile {{quote(profile)}} --write-with-response {{quote(write_with_response)}} --write-without-response {{quote(write_without_response)}}

# Blocked connected RSSI identity; no fixture lifecycle is acquired here.
[macos]
rf-macos-nrf52840-rssi profile="" write_with_response="10" write_without_response="ab":
    {{apple_run}} python -m tools.apple.test_macos_runtime rf-external rssi --profile {{quote(profile)}} --write-with-response {{quote(write_with_response)}} --write-without-response {{quote(write_without_response)}}

# Blocked Peripheral-initiated disconnect identity; requires its real marker bridge.
[macos]
rf-macos-nrf52840-passive-disconnect profile="" write_with_response="10" write_without_response="ab":
    {{apple_run}} python -m tools.apple.test_macos_runtime rf-external passive-disconnect --profile {{quote(profile)}} --write-with-response {{quote(write_with_response)}} --write-without-response {{quote(write_without_response)}}

# Blocked matrix identity: IO, Notification, strict indication-only, RSSI, passive loss.
# The first blocked gate fails the recipe; blocked is never a matrix pass.
[macos]
rf-macos-nrf52840-all:
    pixi run just apple rf-macos-nrf52840
    pixi run just apple rf-macos-nrf52840-notification
    pixi run just apple rf-macos-nrf52840-indication
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
    {{apple_run}} python -m unittest tools.apple.test_rf_fixture tools.apple.test_macos_rf_safety

# Hardware-free packaging/receipt regressions; no package build or device access.
[macos]
test-consumer-tools:
    {{apple_run}} python -m unittest tools.apple.test_stage_backend tools.apple.test_consumer_ios tools.apple.test_consumer_bleng_ios tools.apple.test_package_uniffi tools.apple.test_bleng_ios_backend.AppleBlengRecoveryHostTests

# Build the independent macOS consumer from staged UniFFI + backend packages.
[macos]
consumer-macos: prepare
    {{apple_run}} swift run --quiet --package-path tests/consumers/apple/blehub/macos --configuration release BleHubAppleArtifactConsumer

# Build the combined BLeng/Supervisor image and run an artifact-only macOS lifecycle consumer.
# This does not modify the default apple-production package or require Bluetooth hardware.
[macos]
consumer-bleng-macos:
    pixi run just ffi apple-bleng-package
    {{apple_run}} swift run --quiet --scratch-path .work/apple-bleng-consumer --package-path tests/consumers/apple/bleng/macos --configuration release BlengArtifactConsumer

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

# Run the existing recovery Swift tests as a standalone macOS XCTest bundle.
# Same staged BLeng image; no App, CoreBluetooth backend, signing or device access.
[macos]
bleng-recovery-test:
    pixi run just ffi apple-bleng-package
    {{apple_run}} python -m tools.apple.bleng_ios_backend test-recovery-host

# Compile the existing BLeng HIL App and its opt-in Settings UI test target.
# This is a build-for-testing check only; it does not launch Settings or mutate radio.
[macos]
bleng-ios-system-loss-ui-build:
    pixi run just ffi apple-bleng-package
    {{apple_run}} python -m tools.apple.bleng_ios_backend build-ui-tests

# Same existing HIL App/UI test target, signed for one physical iOS device.
[macos]
bleng-ios-system-loss-ui-build-signed team:
    pixi run just ffi apple-bleng-package
    {{apple_run}} python -m tools.apple.bleng_ios_backend build-ui-tests --team {{quote(team)}}

# Run one explicitly supplied signed BLeng system-loss App/xctestrun candidate.
# The runner owns the iOS lease, provider, App Documents relay, UI test and cleanup.
[macos]
bleng-ios-system-loss contract="" profile="" port="" app="" xctestrun="" case="" sniffer_port="" sniffer_nrfutil_home="" sniffer_output="":
    pixi run just ffi apple-bleng-package
    {{apple_run}} python -m tools.apple.run_bleng_ios_system_loss --contract {{quote(contract)}} --profile {{quote(profile)}} --port {{quote(port)}} --app {{quote(app)}} --xctestrun {{quote(xctestrun)}} --case {{quote(case)}} --sniffer-port {{quote(sniffer_port)}} --sniffer-nrfutil-home {{quote(sniffer_nrfutil_home)}} --sniffer-output {{quote(sniffer_output)}}

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

# Non-destructive iOS CoreBluetooth Central recreation spike. This reuses the
# existing HIL App and attested v2 fixture, and never toggles phone Bluetooth.
[macos]
bleng-ios-rf-central-recovery-spike contract="" fixture_profile="" port="":
    test -n {{quote(contract)}} && test -n {{quote(fixture_profile)}} && test -n {{quote(port)}}
    pixi run just ffi apple-bleng-package
    {{apple_run}} python -m tools.apple.bleng_ios_backend rf-central-recovery-spike --contract {{quote(contract)}} --fixture-profile {{quote(fixture_profile)}} --port {{quote(port)}}

# No-RF lifecycle probe for retiring one Bleng owner/backend and creating a fresh
# owner/backend in the same existing HIL App.
[macos]
bleng-ios-fresh-owner-lifecycle:
    pixi run just ffi apple-bleng-package
    {{apple_run}} python -m tools.apple.bleng_ios_backend rf-fresh-owner-lifecycle

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
