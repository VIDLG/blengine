set working-directory := "../.."

# Run the complete workspace test suite, including blehub-testkit conformance.
default: all

all:
    rtk cargo test --workspace

# Run the public BleHub contract and assembly tests with filtered Cargo output.
blehub:
    rtk cargo test -p blehub --all-targets

# Run BLeng runtime, owner, reservation, and facade tests with filtered output.
bleng:
    rtk cargo test -p bleng --all-targets


# Include native FFI submission and context-fence tests, not only the pure policy.
supervisor:
    rtk cargo test -p bleng-supervisor --all-targets --all-features

# Run the core Rust packages affected by contract/runtime changes.
core: blehub bleng supervisor

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
