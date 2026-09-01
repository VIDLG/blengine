# Scan over real RF for the managed Peripheral service UUID.
# Args: timeout seconds, optional service UUID.
scan-smoke timeout="30" service_uuid="fff00000-0000-4000-8000-000000000000":
    @rtk cargo run --quiet --manifest-path tests/hil/windows/Cargo.toml --bin scan -- {{ timeout }} {{ service_uuid }}

# Scan, connect, and disconnect over real RF using the managed Peripheral service UUID.
# Args: timeout seconds per operation, optional service UUID.
connect-smoke timeout="30" service_uuid="fff00000-0000-4000-8000-000000000000":
    @rtk cargo run --quiet --manifest-path tests/hil/windows/Cargo.toml --bin connect -- {{ timeout }} {{ service_uuid }}

# Scan, connect, discover, and validate the managed Peripheral GATT profile over real RF.
# Args: timeout seconds per operation, optional service UUID.
gatt-smoke timeout="30" service_uuid="fff00000-0000-4000-8000-000000000000":
    @rtk cargo run --quiet --manifest-path tests/hil/windows/Cargo.toml --bin gatt -- {{ timeout }} {{ service_uuid }}

# Scan, connect, discover, and validate characteristic read/write over real RF.
# Args: timeout seconds per operation, optional service UUID.
read-write-smoke timeout="30" service_uuid="fff00000-0000-4000-8000-000000000000":
    @rtk cargo run --quiet --manifest-path tests/hil/windows/Cargo.toml --bin read-write -- {{ timeout }} {{ service_uuid }}

# Measure uncached read plus WinRT-terminal and Peripheral-confirmed GATT write throughput.
# Args: whole-batch timeout seconds, service UUID, operations per mode, comma-separated payload bytes (20..512), auto|android|qemu.
gatt-throughput timeout="30" service_uuid="ffe00000-0000-4000-8000-000000000000" operations="200" payload_bytes="20" provider="auto":
    @cargo run --quiet --release --manifest-path tests/hil/windows/Cargo.toml --bin gatt-throughput -- {{ timeout }} {{ service_uuid }} {{ operations }} {{ payload_bytes }} {{ provider }}

# Measure sequenced Peripheral-to-Windows Notification delivery throughput.
# Args: timeout seconds, provider service UUID, android|qemu, count, auto|guest pacing microseconds, payload bytes (20..512).
notification-throughput timeout="30" service_uuid="fff00000-0000-4000-8000-000000000000" provider="android" operations="200" pacing_interval_us="auto" payload_bytes="20":
    @cargo run --quiet --release --manifest-path tests/hil/windows/Cargo.toml --bin notification-throughput -- {{ timeout }} {{ service_uuid }} {{ provider }} {{ operations }} {{ pacing_interval_us }} {{ payload_bytes }}

# Validate notification/indication enable, value delivery, disable, and stale-route silence.
# Args: timeout seconds, service UUID, default|notification|indication, android|qemu.
notification-smoke timeout="30" service_uuid="fff00000-0000-4000-8000-000000000000" mode="default" provider="android":
    @rtk cargo run --quiet --manifest-path tests/hil/windows/Cargo.toml --bin notifications -- {{ timeout }} {{ service_uuid }} {{ mode }} {{ provider }}

# Run the Phase 2 repeated scan/connect/dispose and backend-lease reacquisition soak.
# Args: timeout seconds per operation, QEMU/BLESS service UUID, cycle count.
scan-connect-soak timeout="30" service_uuid="ffe00000-0000-4000-8000-000000000000" cycles="10":
    @cargo run --quiet --manifest-path tests/hil/windows/Cargo.toml --bin scan-connect-soak -- {{ timeout }} {{ service_uuid }} {{ cycles }}

# Run the final Phase 2 10-cycle scan/connect exit gate.
scan-connect-soak-gate timeout="30" service_uuid="ffe00000-0000-4000-8000-000000000000":
    @cargo run --quiet --manifest-path tests/hil/windows/Cargo.toml --bin scan-connect-soak -- {{ timeout }} {{ service_uuid }} 10

# Repeatedly dispose an active scan/connection/GATT/subscription and reacquire the backend lease.
# Args: timeout seconds per operation, QEMU/BLESS service UUID, cycle count.
resource-lifecycle-smoke timeout="30" service_uuid="ffe00000-0000-4000-8000-000000000000" cycles="10":
    @cargo run --quiet --manifest-path tests/hil/windows/Cargo.toml --bin resource-lifecycle -- {{ timeout }} {{ service_uuid }} {{ cycles }}

# Validate a provider-initiated disconnect through ConnectionStatusChanged.
# Args: timeout seconds, service UUID, android|android-radio|qemu, connect-only|discover.
passive-disconnect-smoke timeout="30" service_uuid="fff00000-0000-4000-8000-000000000000" provider="android" connection_anchor="connect-only":
    @rtk cargo run --quiet --manifest-path tests/hil/windows/Cargo.toml --bin passive-disconnect -- {{ timeout }} {{ service_uuid }} {{ provider }} {{ connection_anchor }}
