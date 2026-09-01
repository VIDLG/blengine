use std::{
    error::Error,
    mem::size_of,
    process::Output,
    sync::mpsc,
    thread,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

use blehub::{
    BleEvent, CommandId, ConnectCommand, ConnectionId, ConnectionState, DisconnectCommand,
    GattCharacteristicId, GattSessionId, NotificationAction, NotificationMode,
    NotificationModeRequest, NotificationState, ScanSessionId, SetNotificationsCommand,
    SubscriptionId,
};

mod support;

use support::{
    EventReceiver, aggregate_results, create_hub, discover_gatt, initialize_hub,
    parse_timeout_and_service, request_qemu_throughput_phy, run_fixture_recipe, scan_and_stop,
    set_notifications_and_wait, validate_fixture_database, wait_for_connection_command,
};

const DEFAULT_FIXTURE_SERVICE: &str = "fff00000-0000-4000-8000-000000000000";
const DEFAULT_OPERATIONS: usize = 200;
const MAX_OPERATIONS: usize = 10_000;
const DEFAULT_PAYLOAD_BYTES: usize = 20;
const MIN_PAYLOAD_BYTES: usize = 20;
const MAX_PAYLOAD_BYTES: usize = 512;
const BURST_MAGIC: &[u8; 4] = b"BHN1";
const INITIALIZE_COMMAND_ID: CommandId = CommandId { value: 1 };
const START_SCAN_COMMAND_ID: CommandId = CommandId { value: 2 };
const STOP_SCAN_COMMAND_ID: CommandId = CommandId { value: 3 };
const CONNECT_COMMAND_ID: CommandId = CommandId { value: 4 };
const DISCOVER_GATT_COMMAND_ID: CommandId = CommandId { value: 5 };
const ENABLE_NOTIFICATIONS_COMMAND_ID: CommandId = CommandId { value: 6 };
const DISABLE_NOTIFICATIONS_COMMAND_ID: CommandId = CommandId { value: 7 };
const DISCONNECT_COMMAND_ID: CommandId = CommandId { value: 8 };
const SCAN_SESSION_ID: ScanSessionId = ScanSessionId { value: 1 };
const CONNECTION_ID: ConnectionId = ConnectionId { value: 1 };
const GATT_SESSION_ID: GattSessionId = GattSessionId { value: 1 };
const SUBSCRIPTION_ID: SubscriptionId = SubscriptionId { value: 1 };
const CONTROL_POLL_INTERVAL: Duration = Duration::from_millis(20);
const DUPLICATE_DRAIN: Duration = Duration::from_millis(250);

#[derive(Clone, Copy)]
enum FixtureProvider {
    Android,
    Qemu,
}

impl FixtureProvider {
    fn parse(value: &str) -> Result<Self, Box<dyn Error>> {
        match value {
            "android" => Ok(Self::Android),
            "qemu" => Ok(Self::Qemu),
            _ => {
                Err(format!("invalid fixture provider {value:?}; expected android or qemu").into())
            }
        }
    }

    fn recipe(self) -> &'static str {
        match self {
            Self::Android => "android-notification-burst",
            Self::Qemu => "vm-fixture-burst",
        }
    }

    fn control_timeout(self) -> Duration {
        match self {
            Self::Android => Duration::from_secs(210),
            Self::Qemu => Duration::from_secs(150),
        }
    }

    fn name(self) -> &'static str {
        match self {
            Self::Android => "android",
            Self::Qemu => "qemu",
        }
    }
}

struct NotificationMeasurement {
    provider: FixtureProvider,
    operations: usize,
    trigger_to_first: Duration,
    trigger_to_complete: Duration,
    delivery_span: Duration,
    fixture_local_elapsed: Duration,
    pacing_interval_us: u64,
    payload_bytes: usize,
}

#[derive(Clone, Copy)]
struct BurstRequest {
    id: [u8; 8],
    operations: usize,
    pacing_interval_us: u64,
    payload_bytes: usize,
    timeout: Duration,
}

impl NotificationMeasurement {
    fn print(&self) {
        let intervals = (self.operations - 1) as f64;
        let delivery_seconds = self.delivery_span.as_secs_f64();
        let delivery_rate = intervals / delivery_seconds;
        let payload_kib_per_second =
            intervals * self.payload_bytes as f64 / 1024.0 / delivery_seconds;
        let end_to_end_rate = self.operations as f64 / self.trigger_to_complete.as_secs_f64();
        println!(
            "provider={} mode=notification operations={} payload_bytes={} pacing_interval_us={} trigger_to_first_ms={:.3} trigger_to_complete_ms={:.3} delivery_span_ms={:.3} operations_per_second={:.1} payload_kib_per_second={:.3} end_to_end_operations_per_second={:.1} fixture_local_ms={:.3} missing=0 duplicates=0 out_of_order=0",
            self.provider.name(),
            self.operations,
            self.payload_bytes,
            self.pacing_interval_us,
            self.trigger_to_first.as_secs_f64() * 1_000.0,
            self.trigger_to_complete.as_secs_f64() * 1_000.0,
            self.delivery_span.as_secs_f64() * 1_000.0,
            delivery_rate,
            payload_kib_per_second,
            end_to_end_rate,
            self.fixture_local_elapsed.as_secs_f64() * 1_000.0,
        );
    }
}

fn main() -> Result<(), Box<dyn Error>> {
    let (timeout, service_uuid) = parse_timeout_and_service(DEFAULT_FIXTURE_SERVICE)?;
    let provider = FixtureProvider::parse(
        &std::env::args()
            .nth(3)
            .unwrap_or_else(|| "android".to_owned()),
    )?;
    let operations = parse_operation_count()?;
    let pacing_interval_us = parse_pacing_interval(provider)?;
    let payload_bytes = parse_payload_bytes()?;
    let expected_pacing = Duration::from_micros(
        pacing_interval_us.saturating_mul(u64::try_from(operations.saturating_sub(1))?),
    );
    let burst_timeout = timeout
        .max(Duration::from_secs(60))
        .max(expected_pacing.saturating_add(Duration::from_secs(30)));
    let (hub, receiver) = create_hub()?;
    let mut measurement = None;

    let scenario_result: Result<(), Box<dyn Error>> = (|| {
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

        let notification_result: Result<(), Box<dyn Error>> = (|| {
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
            .updates_characteristic_id;
            set_notifications_and_wait(
                &hub,
                &receiver,
                SetNotificationsCommand {
                    command_id: ENABLE_NOTIFICATIONS_COMMAND_ID,
                    connection_id: CONNECTION_ID,
                    gatt_session_id: GATT_SESSION_ID,
                    subscription_id: SUBSCRIPTION_ID,
                    characteristic_id,
                    action: NotificationAction::Enable {
                        mode: NotificationModeRequest::RequireNotification,
                    },
                },
                NotificationState::Enabled {
                    actual_mode: Some(NotificationMode::Notification),
                },
                timeout,
            )?;

            let _ = run_burst(
                &receiver,
                provider,
                characteristic_id,
                BurstRequest {
                    id: burst_id(0x5741_524d_5550_0001),
                    operations: 1,
                    pacing_interval_us,
                    payload_bytes,
                    timeout: burst_timeout,
                },
            )?;
            measurement = Some(run_burst(
                &receiver,
                provider,
                characteristic_id,
                BurstRequest {
                    id: fresh_burst_id()?,
                    operations,
                    pacing_interval_us,
                    payload_bytes,
                    timeout: burst_timeout,
                },
            )?);

            set_notifications_and_wait(
                &hub,
                &receiver,
                SetNotificationsCommand {
                    command_id: DISABLE_NOTIFICATIONS_COMMAND_ID,
                    connection_id: CONNECTION_ID,
                    gatt_session_id: GATT_SESSION_ID,
                    subscription_id: SUBSCRIPTION_ID,
                    characteristic_id,
                    action: NotificationAction::Disable,
                },
                NotificationState::Disabled,
                timeout,
            )?;
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
                "notification throughput benchmark failed",
                notification_result.map_err(|error| error.to_string()),
            ),
            (
                "disconnect cleanup failed",
                disconnect_result.map_err(|error| error.to_string()),
            ),
        ])
    })();
    let dispose_result = hub.dispose();

    receiver.finish_quiet(
        "Windows notification throughput benchmark",
        aggregate_results([
            (
                "benchmark scenario failed",
                scenario_result.map_err(|error| error.to_string()),
            ),
            (
                "Windows backend disposal failed",
                dispose_result.map_err(|error| error.to_string()),
            ),
        ]),
    )?;

    measurement
        .ok_or("notification throughput benchmark produced no measurement")?
        .print();
    println!("Windows notification throughput benchmark: PASS");
    Ok(())
}

fn run_burst(
    receiver: &EventReceiver,
    provider: FixtureProvider,
    characteristic_id: GattCharacteristicId,
    request: BurstRequest,
) -> Result<NotificationMeasurement, Box<dyn Error>> {
    let burst_id_text = request
        .id
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>();
    let operations_text = request.operations.to_string();
    let recipe = provider.recipe();
    let pacing_duration = Duration::from_micros(
        request
            .pacing_interval_us
            .saturating_mul(u64::try_from(request.operations)?),
    );
    let control_timeout = provider
        .control_timeout()
        .max(pacing_duration.saturating_add(Duration::from_secs(30)));
    let mut control_arguments = vec![burst_id_text, operations_text];
    if matches!(provider, FixtureProvider::Qemu) {
        control_arguments.push(request.pacing_interval_us.to_string());
    }
    control_arguments.push(request.payload_bytes.to_string());
    let (control_sender, control_receiver) = mpsc::sync_channel(1);
    let trigger_started = Instant::now();
    let control_thread = thread::spawn(move || {
        let result = run_fixture_recipe(
            recipe,
            &control_arguments
                .iter()
                .map(String::as_str)
                .collect::<Vec<_>>(),
            control_timeout,
        )
        .map_err(|error| error.to_string());
        let status = result
            .as_ref()
            .map(|_| ())
            .map_err(std::clone::Clone::clone);
        let _ = control_sender.send(status);
        result
    });

    let receive_result = receive_burst(
        receiver,
        characteristic_id,
        request,
        trigger_started,
        &control_receiver,
    );
    let control_result = control_thread
        .join()
        .map_err(|_| "fixture burst control thread panicked")?;
    let output = control_result
        .map_err(|error| format!("{} fixture burst control failed: {error}", provider.name()))?;
    require_successful_control(provider, &output)?;
    let fixture_local_elapsed = parse_fixture_local_elapsed(&output)?;
    let mut measurement = receive_result?;
    measurement.provider = provider;
    measurement.fixture_local_elapsed = fixture_local_elapsed;
    measurement.pacing_interval_us = request.pacing_interval_us;
    measurement.payload_bytes = request.payload_bytes;
    ensure_no_burst_notification(receiver, characteristic_id, DUPLICATE_DRAIN)?;
    Ok(measurement)
}

fn receive_burst(
    receiver: &EventReceiver,
    characteristic_id: GattCharacteristicId,
    request: BurstRequest,
    trigger_started: Instant,
    control_receiver: &mpsc::Receiver<Result<(), String>>,
) -> Result<NotificationMeasurement, Box<dyn Error>> {
    let BurstRequest {
        id: burst_id,
        operations,
        payload_bytes,
        timeout,
        ..
    } = request;
    let deadline = trigger_started + timeout;
    let mut first_received = None;
    let mut received = 0;
    let mut control_failure = None;
    while received < operations {
        if let Ok(result) = control_receiver.try_recv()
            && let Err(error) = result
        {
            control_failure = Some(error);
            break;
        }
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            break;
        }
        match receiver.recv_timeout(CONTROL_POLL_INTERVAL.min(remaining)) {
            Ok(BleEvent::NotificationReceived {
                connection_id,
                gatt_session_id,
                subscription_id,
                characteristic_id: event_characteristic_id,
                value,
            }) => {
                if connection_id != CONNECTION_ID
                    || gatt_session_id != GATT_SESSION_ID
                    || subscription_id != SUBSCRIPTION_ID
                    || event_characteristic_id != characteristic_id
                {
                    return Err("notification burst returned mismatched correlation IDs".into());
                }
                let sequence = parse_burst_payload(&value, burst_id, payload_bytes)?;
                if sequence != u64::try_from(received)? {
                    return Err(format!(
                        "notification sequence violation at arrival {}: expected {}, got {sequence}; duplicates/out-of-order/missing data are not accepted",
                        received + 1,
                        received,
                    )
                    .into());
                }
                let now = Instant::now();
                first_received.get_or_insert(now);
                received += 1;
            }
            Ok(BleEvent::BackendError { error }) => {
                return Err(format!("backend error during notification burst: {error}").into());
            }
            Ok(BleEvent::ConnectionStateChanged {
                connection_id: CONNECTION_ID,
                result,
                ..
            }) if !matches!(
                result,
                blehub::ConnectionStateResult::Ok {
                    state: ConnectionState::Connected
                }
            ) =>
            {
                return Err(format!(
                    "connection state changed during notification burst: {result:?}"
                )
                .into());
            }
            Ok(_) | Err(mpsc::RecvTimeoutError::Timeout) => {}
            Err(error) => return Err(error.into()),
        }
    }

    if let Some(error) = control_failure {
        return Err(format!(
            "fixture control failed after {received}/{operations} notifications: {error}"
        )
        .into());
    }
    if received != operations {
        return Err(format!(
            "notification burst timed out after receiving {received}/{operations} unique in-order payloads"
        )
        .into());
    }
    let completed = Instant::now();
    let first_received = first_received.ok_or("notification burst delivered no payload")?;
    Ok(NotificationMeasurement {
        provider: FixtureProvider::Android,
        operations,
        trigger_to_first: first_received.duration_since(trigger_started),
        trigger_to_complete: completed.duration_since(trigger_started),
        delivery_span: completed.duration_since(first_received),
        fixture_local_elapsed: Duration::ZERO,
        pacing_interval_us: 0,
        payload_bytes,
    })
}

fn parse_burst_payload(
    value: &[u8],
    expected_burst_id: [u8; 8],
    payload_bytes: usize,
) -> Result<u64, Box<dyn Error>> {
    if value.len() != payload_bytes {
        return Err(format!(
            "notification payload length mismatch: expected {payload_bytes}, got {}",
            value.len()
        )
        .into());
    }
    if &value[..BURST_MAGIC.len()] != BURST_MAGIC {
        return Err("notification payload has invalid burst magic".into());
    }
    let burst_start = BURST_MAGIC.len();
    let burst_end = burst_start + expected_burst_id.len();
    if value[burst_start..burst_end] != expected_burst_id {
        return Err("notification payload belongs to a stale or unexpected burst".into());
    }
    let sequence = value[burst_end..burst_end + size_of::<u64>()]
        .try_into()
        .map(u64::from_le_bytes)
        .map_err(|_| "notification payload has invalid sequence bytes")?;
    Ok(sequence)
}

fn require_successful_control(
    provider: FixtureProvider,
    output: &Output,
) -> Result<(), Box<dyn Error>> {
    if output.status.success() {
        return Ok(());
    }
    Err(format!(
        "{} fixture burst exited with {}; stdout: {}; stderr: {}",
        provider.name(),
        output.status,
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr),
    )
    .into())
}

fn parse_fixture_local_elapsed(output: &Output) -> Result<Duration, Box<dyn Error>> {
    let stdout = String::from_utf8_lossy(&output.stdout);
    let nanos = stdout
        .split_whitespace()
        .find_map(|token| token.strip_prefix("local_elapsed_ns="))
        .or_else(|| {
            stdout
                .lines()
                .find_map(|line| line.strip_prefix("fixture_local_elapsed_ns="))
        })
        .ok_or("fixture burst output did not contain local_elapsed_ns")?
        .parse::<u64>()?;
    Ok(Duration::from_nanos(nanos))
}

fn ensure_no_burst_notification(
    receiver: &EventReceiver,
    characteristic_id: GattCharacteristicId,
    observation: Duration,
) -> Result<(), Box<dyn Error>> {
    let deadline = Instant::now() + observation;
    loop {
        match receiver.recv_timeout(deadline.saturating_duration_since(Instant::now())) {
            Ok(BleEvent::NotificationReceived {
                connection_id: CONNECTION_ID,
                gatt_session_id: GATT_SESSION_ID,
                subscription_id: SUBSCRIPTION_ID,
                characteristic_id: event_characteristic_id,
                ..
            }) if event_characteristic_id == characteristic_id => {
                return Err(
                    "received a duplicate or late notification after burst completion".into(),
                );
            }
            Ok(_) => {}
            Err(mpsc::RecvTimeoutError::Timeout) => return Ok(()),
            Err(error) => return Err(error.into()),
        }
    }
}

fn parse_operation_count() -> Result<usize, Box<dyn Error>> {
    let operations = std::env::args()
        .nth(4)
        .map(|value| value.parse::<usize>())
        .transpose()?
        .unwrap_or(DEFAULT_OPERATIONS);
    if !(2..=MAX_OPERATIONS).contains(&operations) {
        return Err(format!("operation count must be between 2 and {MAX_OPERATIONS}").into());
    }
    Ok(operations)
}

fn parse_payload_bytes() -> Result<usize, Box<dyn Error>> {
    let payload_bytes = std::env::args()
        .nth(6)
        .map(|value| value.parse::<usize>())
        .transpose()?
        .unwrap_or(DEFAULT_PAYLOAD_BYTES);
    if !(MIN_PAYLOAD_BYTES..=MAX_PAYLOAD_BYTES).contains(&payload_bytes) {
        return Err(format!(
            "notification payload byte count must be between {MIN_PAYLOAD_BYTES} and {MAX_PAYLOAD_BYTES}"
        )
        .into());
    }
    Ok(payload_bytes)
}

fn parse_pacing_interval(provider: FixtureProvider) -> Result<u64, Box<dyn Error>> {
    let default = match provider {
        FixtureProvider::Android => 0,
        FixtureProvider::Qemu => 500,
    };
    let interval_us = match std::env::args().nth(5).as_deref() {
        None | Some("auto") => default,
        Some(value) => value.parse::<u64>()?,
    };
    if interval_us > 1_000_000 {
        return Err("notification pacing interval must not exceed 1000000 microseconds".into());
    }
    if matches!(provider, FixtureProvider::Android) && interval_us != 0 {
        return Err("Android notification burst uses onNotificationSent backpressure and does not accept additional pacing".into());
    }
    Ok(interval_us)
}

fn fresh_burst_id() -> Result<[u8; 8], Box<dyn Error>> {
    let nanos = SystemTime::now().duration_since(UNIX_EPOCH)?.as_nanos();
    Ok((nanos as u64).to_be_bytes())
}

const fn burst_id(value: u64) -> [u8; 8] {
    value.to_be_bytes()
}
