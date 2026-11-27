use std::{
    error::Error,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

use blehub::{
    BleEvent, BleHub, CommandId, ConnectCommand, ConnectionId, ConnectionState, DisconnectCommand,
    GattCharacteristicId, GattSessionId, ReadCharacteristicCommand, ScanSessionId,
    WriteCharacteristicCommand, WriteType,
};

mod support;

use support::{
    EventReceiver, aggregate_results, create_hub, discover_gatt, initialize_hub, receive,
    request_qemu_throughput_phy, run_fixture_recipe, scan_and_stop, validate_fixture_database,
    wait_for_connection_command,
};

const DEFAULT_FIXTURE_SERVICE: &str = "ffe00000-0000-4000-8000-000000000000";
const DEFAULT_OPERATIONS: usize = 200;
const DEFAULT_PAYLOAD_BYTES: usize = 20;
const MIN_PAYLOAD_BYTES: usize = 20;
const MAX_PAYLOAD_BYTES: usize = 512;
const MAX_OPERATIONS: usize = 100_000;

const READ_COMMAND_BASE: u64 = 200_000;
const INITIALIZE_COMMAND_ID: CommandId = command_id(1);
const START_SCAN_COMMAND_ID: CommandId = command_id(2);
const STOP_SCAN_COMMAND_ID: CommandId = command_id(3);
const CONNECT_COMMAND_ID: CommandId = command_id(4);
const DISCOVER_GATT_COMMAND_ID: CommandId = command_id(5);
const DISCONNECT_COMMAND_ID: CommandId = command_id(6);
const SCAN_SESSION_ID: ScanSessionId = ScanSessionId { value: 1 };
const CONNECTION_ID: ConnectionId = ConnectionId { value: 1 };
const GATT_SESSION_ID: GattSessionId = GattSessionId { value: 1 };

#[derive(Clone, Copy)]
enum FixtureProvider {
    Android,
    Qemu,
}

impl FixtureProvider {
    fn parse(value: &str, service_uuid: &str) -> Result<Self, Box<dyn Error>> {
        match value {
            "android" => Ok(Self::Android),
            "qemu" => Ok(Self::Qemu),
            "auto" if service_uuid.starts_with("fff0") => Ok(Self::Android),
            "auto" if service_uuid.starts_with("ffe0") => Ok(Self::Qemu),
            "auto" => Err(format!(
                "cannot infer fixture provider from service UUID {service_uuid}; pass android or qemu"
            )
            .into()),
            _ => Err(format!(
                "invalid fixture provider {value:?}; expected auto, android, or qemu"
            )
            .into()),
        }
    }

    const fn start_recipe(self) -> &'static str {
        match self {
            Self::Android => "android-write-batch-start",
            Self::Qemu => "vm-fixture-write-batch-start",
        }
    }

    const fn status_recipe(self) -> &'static str {
        match self {
            Self::Android => "android-write-batch-status",
            Self::Qemu => "vm-fixture-write-batch-status",
        }
    }

    const fn control_timeout(self) -> Duration {
        match self {
            Self::Android => Duration::from_secs(45),
            Self::Qemu => Duration::from_secs(180),
        }
    }
}

struct WriteBatchStatus {
    callback_span: Duration,
}

struct BenchmarkResult {
    mode: &'static str,
    operations: usize,
    payload_bytes: usize,
    dispatch_elapsed: Duration,
    completion_elapsed: Duration,
    write_batch_status: Option<WriteBatchStatus>,
}

impl BenchmarkResult {
    fn print(&self) {
        let operations = self.operations as f64;
        let completion_seconds = self.completion_elapsed.as_secs_f64();
        let dispatch_rate = operations / self.dispatch_elapsed.as_secs_f64();
        let completion_rate = operations / completion_seconds;
        let payload_kib_per_second =
            operations * self.payload_bytes as f64 / 1024.0 / completion_seconds;
        let remote = self.write_batch_status.as_ref().map_or_else(
            String::new,
            |status| {
                let callback_rate = if self.operations > 1 && !status.callback_span.is_zero() {
                    (self.operations - 1) as f64 / status.callback_span.as_secs_f64()
                } else {
                    0.0
                };
                format!(
                    " fixture_callback_span_ms={:.3} fixture_callback_operations_per_second={callback_rate:.1} peripheral_callbacks=verified missing=0 duplicates=0 out_of_order=0",
                    status.callback_span.as_secs_f64() * 1_000.0,
                )
            },
        );
        println!(
            "mode={} operations={} payload_bytes={} dispatch_ms={:.3} dispatch_ops_per_second={:.1} completion_ms={:.3} operations_per_second={:.1} payload_kib_per_second={:.3} readback=verified{}",
            self.mode,
            self.operations,
            self.payload_bytes,
            self.dispatch_elapsed.as_secs_f64() * 1_000.0,
            dispatch_rate,
            self.completion_elapsed.as_secs_f64() * 1_000.0,
            completion_rate,
            payload_kib_per_second,
            remote,
        );
    }
}

fn main() -> Result<(), Box<dyn Error>> {
    let (timeout, service_uuid) = support::parse_timeout_and_service(DEFAULT_FIXTURE_SERVICE)?;
    let operations = parse_usize_argument(3, DEFAULT_OPERATIONS, "operation count")?;
    let payload_sizes = parse_payload_sizes()?;
    let provider = FixtureProvider::parse(
        &std::env::args().nth(5).unwrap_or_else(|| "auto".to_owned()),
        &service_uuid.value,
    )?;
    if !(2..=MAX_OPERATIONS).contains(&operations) {
        return Err(format!("operation count must be between 2 and {MAX_OPERATIONS}").into());
    }
    let (hub, receiver) = create_hub()?;
    let mut measurements = Vec::with_capacity(payload_sizes.len() * 3);
    let benchmark_result: Result<(), Box<dyn Error>> = (|| {
        initialize_hub(&hub, &receiver, INITIALIZE_COMMAND_ID, timeout)?;
        let observation = scan_and_stop(
            &hub,
            &receiver,
            START_SCAN_COMMAND_ID,
            STOP_SCAN_COMMAND_ID,
            SCAN_SESSION_ID,
            service_uuid.clone(),
            timeout,
        )?;
        hub.connect(ConnectCommand {
            command_id: CONNECT_COMMAND_ID,
            connection_id: CONNECTION_ID,
            peripheral_id: observation.peripheral_id,
        })?;
        wait_for_connection_command(
            &receiver,
            CONNECT_COMMAND_ID,
            CONNECTION_ID,
            ConnectionState::Connected,
            timeout,
        )?;
        if matches!(provider, FixtureProvider::Qemu) {
            request_qemu_throughput_phy()?;
        }

        let io_result: Result<(), Box<dyn Error>> = (|| {
            let database = discover_gatt(
                &hub,
                &receiver,
                DISCOVER_GATT_COMMAND_ID,
                CONNECTION_ID,
                GATT_SESSION_ID,
                timeout,
            )?;
            let characteristic_id = validate_fixture_database(
                &database,
                &service_uuid,
                CONNECTION_ID,
                GATT_SESSION_ID,
            )?
            .read_write_characteristic_id;

            for payload_bytes in payload_sizes.iter().copied() {
                measurements.push(run_read_benchmark(
                    &hub,
                    &receiver,
                    characteristic_id,
                    operations,
                    payload_bytes,
                    timeout,
                )?);
                measurements.push(run_benchmark(
                    &hub,
                    &receiver,
                    characteristic_id,
                    WriteType::WithResponse,
                    "with-response",
                    1_000,
                    0x52,
                    operations,
                    payload_bytes,
                    provider,
                    timeout,
                )?);
                measurements.push(run_benchmark(
                    &hub,
                    &receiver,
                    characteristic_id,
                    WriteType::WithoutResponse,
                    "without-response",
                    1_000_000,
                    0x57,
                    operations,
                    payload_bytes,
                    provider,
                    timeout,
                )?);
            }
            Ok(())
        })();

        let disconnect_result: Result<(), Box<dyn Error>> = (|| {
            hub.disconnect(DisconnectCommand {
                command_id: DISCONNECT_COMMAND_ID,
                connection_id: CONNECTION_ID,
            })?;
            wait_for_connection_command(
                &receiver,
                DISCONNECT_COMMAND_ID,
                CONNECTION_ID,
                ConnectionState::Disconnected,
                timeout,
            )
        })();

        aggregate_results([
            (
                "GATT throughput benchmark failed",
                io_result.map_err(|error| error.to_string()),
            ),
            (
                "disconnect cleanup failed",
                disconnect_result.map_err(|error| error.to_string()),
            ),
        ])
    })();
    let dispose_result = hub.dispose();

    receiver.finish_quiet(
        "Windows GATT throughput benchmark",
        aggregate_results([
            (
                "benchmark scenario failed",
                benchmark_result.map_err(|error| error.to_string()),
            ),
            (
                "Windows backend disposal failed",
                dispose_result.map_err(|error| error.to_string()),
            ),
        ]),
    )?;

    for measurement in measurements {
        measurement.print();
    }
    println!("Windows GATT throughput benchmark: PASS");
    Ok(())
}

fn run_read_benchmark(
    hub: &BleHub,
    receiver: &EventReceiver,
    characteristic_id: GattCharacteristicId,
    operations: usize,
    payload_bytes: usize,
    timeout: Duration,
) -> Result<BenchmarkResult, Box<dyn Error>> {
    let expected = make_payload(0x44, 0, payload_bytes);
    let write_command_id = command_id(READ_COMMAND_BASE);
    hub.write_characteristic(WriteCharacteristicCommand {
        command_id: write_command_id,
        connection_id: CONNECTION_ID,
        gatt_session_id: GATT_SESSION_ID,
        characteristic_id,
        value: expected.clone(),
        write_type: WriteType::WithResponse,
    })?;
    wait_for_write(receiver, write_command_id, characteristic_id, timeout)?;

    let warmup_command_id = command_id(READ_COMMAND_BASE + 1);
    hub.read_characteristic(ReadCharacteristicCommand {
        command_id: warmup_command_id,
        connection_id: CONNECTION_ID,
        gatt_session_id: GATT_SESSION_ID,
        characteristic_id,
    })?;
    let warmup_value = wait_for_read(receiver, warmup_command_id, characteristic_id, timeout)?;
    if warmup_value != expected {
        return Err(format!(
            "read warm-up mismatch: expected {expected:02X?}, got {warmup_value:02X?}"
        )
        .into());
    }

    let first_command = READ_COMMAND_BASE + 2;
    let started = Instant::now();
    for sequence in 0..operations {
        hub.read_characteristic(ReadCharacteristicCommand {
            command_id: command_id(first_command + u64::try_from(sequence)?),
            connection_id: CONNECTION_ID,
            gatt_session_id: GATT_SESSION_ID,
            characteristic_id,
        })?;
    }
    let dispatch_elapsed = started.elapsed();
    wait_for_read_batch(
        receiver,
        first_command,
        operations,
        characteristic_id,
        &expected,
        timeout,
    )?;
    let completion_elapsed = started.elapsed();

    Ok(BenchmarkResult {
        mode: "read",
        operations,
        payload_bytes,
        dispatch_elapsed,
        completion_elapsed,
        write_batch_status: None,
    })
}

#[allow(clippy::too_many_arguments)]
fn run_benchmark(
    hub: &BleHub,
    receiver: &EventReceiver,
    characteristic_id: GattCharacteristicId,
    write_type: WriteType,
    mode: &'static str,
    command_base: u64,
    payload_seed: u8,
    operations: usize,
    payload_bytes: usize,
    provider: FixtureProvider,
    timeout: Duration,
) -> Result<BenchmarkResult, Box<dyn Error>> {
    let warmup_id = command_id(command_base);
    hub.write_characteristic(WriteCharacteristicCommand {
        command_id: warmup_id,
        connection_id: CONNECTION_ID,
        gatt_session_id: GATT_SESSION_ID,
        characteristic_id,
        value: make_payload(payload_seed, 0, payload_bytes),
        write_type,
    })?;
    wait_for_write(receiver, warmup_id, characteristic_id, timeout)?;

    let batch_id = fresh_write_batch_id()?;
    start_write_batch(provider, batch_id, operations)?;
    let first_command = command_base + 1;
    let started = Instant::now();
    for sequence in 0..operations {
        let sequence = u64::try_from(sequence)?;
        hub.write_characteristic(WriteCharacteristicCommand {
            command_id: command_id(first_command + sequence),
            connection_id: CONNECTION_ID,
            gatt_session_id: GATT_SESSION_ID,
            characteristic_id,
            value: make_write_batch_payload(batch_id, sequence, payload_bytes),
            write_type,
        })?;
    }
    let dispatch_elapsed = started.elapsed();
    wait_for_write_batch(
        receiver,
        first_command,
        operations,
        characteristic_id,
        timeout,
    )?;
    let completion_elapsed = started.elapsed();

    if write_type == WriteType::WithoutResponse {
        std::thread::sleep(Duration::from_millis(500));
    }
    let final_sequence = u64::try_from(operations - 1)?;
    let read_command_id = command_id(first_command + u64::try_from(operations)?);
    hub.read_characteristic(ReadCharacteristicCommand {
        command_id: read_command_id,
        connection_id: CONNECTION_ID,
        gatt_session_id: GATT_SESSION_ID,
        characteristic_id,
    })?;
    let actual = wait_for_read(receiver, read_command_id, characteristic_id, timeout)?;
    let expected = make_write_batch_payload(batch_id, final_sequence, payload_bytes);
    if actual != expected {
        return Err(format!(
            "{mode} final readback mismatch: expected {expected:02X?}, got {actual:02X?}"
        )
        .into());
    }

    let write_batch_status = query_write_batch(provider, batch_id, operations)?;

    Ok(BenchmarkResult {
        mode,
        operations,
        payload_bytes,
        dispatch_elapsed,
        completion_elapsed,
        write_batch_status: Some(write_batch_status),
    })
}

fn wait_for_read_batch(
    receiver: &EventReceiver,
    first_command: u64,
    operations: usize,
    characteristic_id: GattCharacteristicId,
    expected: &[u8],
    timeout: Duration,
) -> Result<(), Box<dyn Error>> {
    #[derive(Clone, Copy, Default)]
    struct Progress {
        read: bool,
        completed: bool,
    }

    let mut progress = vec![Progress::default(); operations];
    let last_command = first_command + u64::try_from(operations)? - 1;
    let deadline = Instant::now() + timeout;
    let mut completed = 0;
    while completed < operations {
        match receive(receiver, deadline)? {
            BleEvent::CharacteristicRead {
                command_id,
                connection_id,
                gatt_session_id,
                characteristic_id: event_characteristic_id,
                value,
            } if (first_command..=last_command).contains(&command_id.value) => {
                validate_read_identity(
                    command_id,
                    connection_id,
                    gatt_session_id,
                    event_characteristic_id,
                    characteristic_id,
                )?;
                let state = &mut progress[usize::try_from(command_id.value - first_command)?];
                if state.read {
                    return Err(format!(
                        "read command {} emitted duplicate CharacteristicRead events",
                        command_id.value
                    )
                    .into());
                }
                if state.completed {
                    return Err(format!(
                        "read command {} emitted CharacteristicRead after its terminal event",
                        command_id.value
                    )
                    .into());
                }
                if value != expected {
                    return Err(format!(
                        "read command {} value mismatch: expected {expected:02X?}, got {value:02X?}",
                        command_id.value
                    )
                    .into());
                }
                state.read = true;
            }
            BleEvent::CommandCompleted { command_id, error }
                if (first_command..=last_command).contains(&command_id.value) =>
            {
                let state = &mut progress[usize::try_from(command_id.value - first_command)?];
                if state.completed {
                    return Err(format!(
                        "read command {} emitted duplicate terminal events",
                        command_id.value
                    )
                    .into());
                }
                if let Some(error) = error {
                    return Err(format!("read command {} failed: {error}", command_id.value).into());
                }
                if !state.read {
                    return Err(format!(
                        "read command {} completed before CharacteristicRead",
                        command_id.value
                    )
                    .into());
                }
                state.completed = true;
                completed += 1;
            }
            BleEvent::BackendError { error } => {
                return Err(
                    format!("backend error during {operations}-read batch: {error}").into(),
                );
            }
            _ => {}
        }
    }
    Ok(())
}

fn wait_for_write_batch(
    receiver: &EventReceiver,
    first_command: u64,
    operations: usize,
    characteristic_id: GattCharacteristicId,
    timeout: Duration,
) -> Result<(), Box<dyn Error>> {
    #[derive(Clone, Copy, Default)]
    struct Progress {
        written: bool,
        completed: bool,
    }

    let mut progress = vec![Progress::default(); operations];
    let last_command = first_command + u64::try_from(operations)? - 1;
    let deadline = Instant::now() + timeout;
    let mut completed = 0;
    while completed < operations {
        match receive(receiver, deadline)? {
            BleEvent::CharacteristicWritten {
                command_id,
                connection_id,
                gatt_session_id,
                characteristic_id: event_characteristic_id,
            } if (first_command..=last_command).contains(&command_id.value) => {
                validate_write_identity(
                    command_id,
                    connection_id,
                    gatt_session_id,
                    event_characteristic_id,
                    characteristic_id,
                )?;
                let state = &mut progress[usize::try_from(command_id.value - first_command)?];
                if state.written {
                    return Err(format!(
                        "write command {} emitted duplicate CharacteristicWritten events",
                        command_id.value
                    )
                    .into());
                }
                if state.completed {
                    return Err(format!(
                        "write command {} emitted CharacteristicWritten after its terminal event",
                        command_id.value
                    )
                    .into());
                }
                state.written = true;
            }
            BleEvent::CommandCompleted { command_id, error }
                if (first_command..=last_command).contains(&command_id.value) =>
            {
                let state = &mut progress[usize::try_from(command_id.value - first_command)?];
                if state.completed {
                    return Err(format!(
                        "write command {} emitted duplicate terminal events",
                        command_id.value
                    )
                    .into());
                }
                if let Some(error) = error {
                    return Err(
                        format!("write command {} failed: {error}", command_id.value).into(),
                    );
                }
                if !state.written {
                    return Err(format!(
                        "write command {} completed before CharacteristicWritten",
                        command_id.value
                    )
                    .into());
                }
                state.completed = true;
                completed += 1;
            }
            BleEvent::BackendError { error } => {
                return Err(
                    format!("backend error during {operations}-write batch: {error}").into(),
                );
            }
            _ => {}
        }
    }
    Ok(())
}

fn wait_for_write(
    receiver: &EventReceiver,
    command_id: CommandId,
    characteristic_id: GattCharacteristicId,
    timeout: Duration,
) -> Result<(), Box<dyn Error>> {
    let deadline = Instant::now() + timeout;
    let mut written = false;
    loop {
        match receive(receiver, deadline)? {
            BleEvent::CharacteristicWritten {
                command_id: event_command_id,
                connection_id,
                gatt_session_id,
                characteristic_id: event_characteristic_id,
            } if event_command_id == command_id => {
                validate_write_identity(
                    command_id,
                    connection_id,
                    gatt_session_id,
                    event_characteristic_id,
                    characteristic_id,
                )?;
                if written {
                    return Err(format!(
                        "write command {} emitted duplicate CharacteristicWritten events",
                        command_id.value
                    )
                    .into());
                }
                written = true;
            }
            BleEvent::CommandCompleted {
                command_id: completed_id,
                error,
            } if completed_id == command_id => match error {
                Some(error) => return Err(error.to_string().into()),
                None if written => return Ok(()),
                None => {
                    return Err(format!(
                        "write command {} completed before CharacteristicWritten",
                        command_id.value
                    )
                    .into());
                }
            },
            BleEvent::BackendError { error } => return Err(error.to_string().into()),
            _ => {}
        }
    }
}

fn validate_write_identity(
    command_id: CommandId,
    connection_id: ConnectionId,
    gatt_session_id: GattSessionId,
    actual_characteristic_id: GattCharacteristicId,
    expected_characteristic_id: GattCharacteristicId,
) -> Result<(), Box<dyn Error>> {
    if connection_id != CONNECTION_ID
        || gatt_session_id != GATT_SESSION_ID
        || actual_characteristic_id != expected_characteristic_id
    {
        return Err(format!(
            "write command {} returned mismatched connection/session/characteristic IDs",
            command_id.value
        )
        .into());
    }
    Ok(())
}

fn validate_read_identity(
    command_id: CommandId,
    connection_id: ConnectionId,
    gatt_session_id: GattSessionId,
    actual_characteristic_id: GattCharacteristicId,
    expected_characteristic_id: GattCharacteristicId,
) -> Result<(), Box<dyn Error>> {
    if connection_id != CONNECTION_ID
        || gatt_session_id != GATT_SESSION_ID
        || actual_characteristic_id != expected_characteristic_id
    {
        return Err(format!(
            "read command {} returned mismatched connection/session/characteristic IDs",
            command_id.value
        )
        .into());
    }
    Ok(())
}

fn wait_for_read(
    receiver: &EventReceiver,
    command_id: CommandId,
    characteristic_id: GattCharacteristicId,
    timeout: Duration,
) -> Result<Vec<u8>, Box<dyn Error>> {
    let deadline = Instant::now() + timeout;
    let mut value = None;
    loop {
        match receive(receiver, deadline)? {
            BleEvent::CharacteristicRead {
                command_id: event_command_id,
                connection_id,
                gatt_session_id,
                characteristic_id: event_characteristic_id,
                value: event_value,
            } if event_command_id == command_id => {
                validate_read_identity(
                    command_id,
                    connection_id,
                    gatt_session_id,
                    event_characteristic_id,
                    characteristic_id,
                )?;
                if value.is_some() {
                    return Err(format!(
                        "read command {} emitted duplicate CharacteristicRead events",
                        command_id.value
                    )
                    .into());
                }
                value = Some(event_value);
            }
            BleEvent::CommandCompleted {
                command_id: completed_id,
                error,
            } if completed_id == command_id => match error {
                Some(error) => return Err(error.to_string().into()),
                None => {
                    return value.ok_or_else(|| {
                        format!(
                            "read command {} completed before CharacteristicRead",
                            command_id.value
                        )
                        .into()
                    });
                }
            },
            BleEvent::BackendError { error } => return Err(error.to_string().into()),
            _ => {}
        }
    }
}

fn make_payload(seed: u8, sequence: u64, payload_bytes: usize) -> Vec<u8> {
    let mut payload = (0..payload_bytes)
        .map(|index| seed.wrapping_add((index as u8).wrapping_mul(17)))
        .collect::<Vec<_>>();
    payload[..size_of::<u64>()].copy_from_slice(&sequence.to_le_bytes());
    payload
}

fn make_write_batch_payload(batch_id: [u8; 8], sequence: u64, payload_bytes: usize) -> Vec<u8> {
    let mut payload = Vec::with_capacity(payload_bytes);
    payload.extend_from_slice(b"BHW1");
    payload.extend_from_slice(&batch_id);
    payload.extend_from_slice(&sequence.to_le_bytes());
    payload.extend(
        (payload.len()..payload_bytes)
            .map(|index| batch_id[index % batch_id.len()] ^ (index as u8).wrapping_mul(17)),
    );
    payload
}

fn fresh_write_batch_id() -> Result<[u8; 8], Box<dyn Error>> {
    let nanos = SystemTime::now().duration_since(UNIX_EPOCH)?.as_nanos();
    Ok((nanos as u64).to_be_bytes())
}

fn batch_id_text(batch_id: [u8; 8]) -> String {
    batch_id.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn start_write_batch(
    provider: FixtureProvider,
    batch_id: [u8; 8],
    operations: usize,
) -> Result<(), Box<dyn Error>> {
    let batch_id = batch_id_text(batch_id);
    let operations = operations.to_string();
    let output = run_fixture_recipe(
        provider.start_recipe(),
        &[&batch_id, &operations],
        provider.control_timeout(),
    )?;
    if output.status.success() {
        Ok(())
    } else {
        Err(fixture_control_error(provider.start_recipe(), &output).into())
    }
}

fn query_write_batch(
    provider: FixtureProvider,
    batch_id: [u8; 8],
    operations: usize,
) -> Result<WriteBatchStatus, Box<dyn Error>> {
    let batch_id = batch_id_text(batch_id);
    let recipe = provider.status_recipe();
    let output = run_fixture_recipe(recipe, &[&batch_id], provider.control_timeout())?;
    if !output.status.success() {
        return Err(fixture_control_error(recipe, &output).into());
    }
    let stdout = String::from_utf8(output.stdout)?;
    let status_line = stdout
        .lines()
        .find_map(|line| line.strip_prefix("fixture_write_batch_status "))
        .ok_or("fixture write batch control returned no status line")?;
    let fields = status_line
        .split_ascii_whitespace()
        .map(|token| {
            token
                .split_once('=')
                .ok_or_else(|| format!("invalid fixture write batch status token {token:?}"))
        })
        .collect::<Result<std::collections::HashMap<_, _>, _>>()?;
    let reported_batch_id = fields
        .get("batch_id")
        .ok_or("fixture write batch status has no batch_id")?;
    if !reported_batch_id.eq_ignore_ascii_case(&batch_id) {
        return Err("fixture write batch status returned a different batch ID".into());
    }
    let counter = |name: &str| -> Result<u64, Box<dyn Error>> {
        Ok(fields
            .get(name)
            .ok_or_else(|| format!("fixture write batch status has no {name}"))?
            .parse()?)
    };
    let expected = u64::try_from(operations)?;
    for (name, wanted) in [
        ("expected_count", expected),
        ("received_count", expected),
        ("unique_count", expected),
        ("missing_count", 0),
        ("duplicate_count", 0),
        ("out_of_order_count", 0),
        ("out_of_range_count", 0),
        ("foreign_batch_count", 0),
        ("malformed_count", 0),
    ] {
        let actual = counter(name)?;
        if actual != wanted {
            return Err(format!(
                "fixture write batch {batch_id} has {name}={actual}, expected {wanted}"
            )
            .into());
        }
    }
    let callback_span_nanos = counter("callback_span_ns")?;
    if operations > 1 && callback_span_nanos == 0 {
        return Err("fixture write batch reported a zero callback span".into());
    }
    Ok(WriteBatchStatus {
        callback_span: Duration::from_nanos(callback_span_nanos),
    })
}

fn fixture_control_error(recipe: &str, output: &std::process::Output) -> String {
    format!(
        "fixture recipe {recipe} exited with {}: {}{}",
        output.status,
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr),
    )
}

fn parse_payload_sizes() -> Result<Vec<usize>, Box<dyn Error>> {
    let value = std::env::args()
        .nth(4)
        .unwrap_or_else(|| DEFAULT_PAYLOAD_BYTES.to_string());
    let payload_sizes = value
        .split(',')
        .map(|part| part.parse::<usize>())
        .collect::<Result<Vec<_>, _>>()?;
    if payload_sizes.is_empty() {
        return Err("payload size list must not be empty".into());
    }
    for payload_bytes in &payload_sizes {
        if !(MIN_PAYLOAD_BYTES..=MAX_PAYLOAD_BYTES).contains(payload_bytes) {
            return Err(format!(
                "payload byte count must be between {MIN_PAYLOAD_BYTES} and {MAX_PAYLOAD_BYTES}; values above the negotiated single-write limit may be rejected by the platform"
            )
            .into());
        }
    }
    Ok(payload_sizes)
}

fn parse_usize_argument(
    index: usize,
    default: usize,
    label: &str,
) -> Result<usize, Box<dyn Error>> {
    let value = std::env::args()
        .nth(index)
        .map(|value| value.parse::<usize>())
        .transpose()?
        .unwrap_or(default);
    if value == 0 {
        return Err(format!("{label} must be greater than zero").into());
    }
    Ok(value)
}

const fn command_id(value: u64) -> CommandId {
    CommandId { value }
}
