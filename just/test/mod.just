set working-directory := "../.."

# Run the complete workspace test suite, including blehub-testkit conformance.
default: all

all:
    rtk cargo test --workspace

# Run only the platform-independent backend contract and caller projection gate.
conformance:
    rtk cargo test -p blehub-testkit

# Validate a running BLE Peripheral fixture over real RF from this host.
fixture *args:
    python -m tools.verify_ble_fixture {{args}}

# Report or require the Android device selected for instrumentation tests.
android-device mode="auto":
    {{android_run}} python -m tools.android_toolchain device-select --mode {{mode}}

android_run := "pixi run -e android"
