# Compile the production Windows backend without accessing Bluetooth hardware.
check:
    rtk cargo check -p blehub-windows --all-targets
    rtk cargo check --manifest-path tests/hil/windows/Cargo.toml --all-targets

# Run deterministic Windows mapping and lifecycle tests without RF.
test:
    rtk cargo test -p blehub-windows

# Build staged crate archives and a local Cargo directory source.
package:
    @python tools/stage_windows_crates.py

# Explicitly refresh the tracked artifact-consumer lock after same-version staged sources change.
package-refresh-consumer-lock:
    @python tools/stage_windows_crates.py --refresh-consumer-lock
