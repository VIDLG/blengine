use std::{
    error::Error,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
        mpsc,
    },
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};

use blehub::{
    BleErrorCategory, BleEvent, BluetoothUuid, CommandId, ConnectCommand, ConnectionId,
    ConnectionState, ConnectionStateResult, GattCharacteristicId, GattSessionId,
    NotificationAction, NotificationMode, NotificationModeRequest, NotificationState,
    ReadCharacteristicCommand, ScanSessionId, SetNotificationsCommand, SubscriptionId,
    WriteCharacteristicCommand, WriteType,
};
use blehub_windows::{
    WindowsBackend, WindowsCallbackResourceSnapshot, WindowsHilCallback, WindowsResourceProbe,
};

mod support;

use support::{
    EventReceiver, ExampleError, FixtureDatabase, aggregate_results, create_accounted_hub,
    discover_gatt, initialize_hub, receive,
    resource::{
        require_connected_resource_graph, require_subscribed_resource_graph,
        wait_for_connection_resource_release, wait_for_resource_release,
    },
    run_fixture_recipe, scan_and_stop, set_notifications_and_wait, validate_fixture_database,
    wait_for_connection_command,
};

const INITIALIZE_COMMAND_ID: CommandId = CommandId { value: 1 };
const START_SCAN_COMMAND_ID: CommandId = CommandId { value: 2 };
const STOP_SCAN_COMMAND_ID: CommandId = CommandId { value: 3 };
const CONNECT_COMMAND_ID: CommandId = CommandId { value: 4 };
const DISCOVER_GATT_COMMAND_ID: CommandId = CommandId { value: 5 };
const INFLIGHT_COMMAND_ID: CommandId = CommandId { value: 6 };
const SCAN_SESSION_ID: ScanSessionId = ScanSessionId { value: 1 };
const CONNECTION_ID: ConnectionId = ConnectionId { value: 1 };
const GATT_SESSION_ID: GattSessionId = GattSessionId { value: 1 };
const SUBSCRIPTION_ID: SubscriptionId = SubscriptionId { value: 1 };
const WRITE_VALUE: &[u8] = &[0x49, 0x4E, 0x46, 0x4C, 0x49, 0x47, 0x48, 0x54];
const NOTIFICATION_VALUE_HEX: &str = "a1b2c3d4";
const QUIET_WINDOW: Duration = Duration::from_secs(2);
const RESOURCE_RELEASE_TIMEOUT: Duration = Duration::from_secs(5);

struct Cli {
    timeout: Duration,
    service_uuid: BluetoothUuid,
    provider: FixtureProvider,
    target: FaultTarget,
}

#[derive(Clone, Copy)]
enum FixtureProvider {
    AndroidRadio,
    Qemu,
}

impl FixtureProvider {
    fn parse(value: &str) -> Result<Self, ExampleError> {
        match value {
            "android-radio" => Ok(Self::AndroidRadio),
            "qemu" => Ok(Self::Qemu),
            _ => Err(format!("invalid provider {value:?}; expected android-radio or qemu").into()),
        }
    }

    fn fault(self) -> Result<(), ExampleError> {
        match self {
            Self::AndroidRadio => {
                run_checked_recipe("android-radio-disable", &[], Duration::from_secs(45))
            }
            Self::Qemu => run_checked_recipe("vm-usb-detach", &[], Duration::from_secs(180)),
        }
    }

    fn trigger_notification(self) -> Result<(), ExampleError> {
        match self {
            Self::AndroidRadio => run_checked_recipe(
                "android-notify",
                &[NOTIFICATION_VALUE_HEX],
                Duration::from_secs(45),
            ),
            Self::Qemu => run_checked_recipe(
                "vm-fixture-update",
                &[NOTIFICATION_VALUE_HEX],
                Duration::from_secs(180),
            ),
        }
    }

    fn notification_worker_timeout(self) -> Duration {
        match self {
            Self::AndroidRadio => Duration::from_secs(45),
            Self::Qemu => Duration::from_secs(180),
        }
    }

    fn restore(self) -> Result<(), ExampleError> {
        let (first, second, first_timeout, second_timeout) = match self {
            Self::AndroidRadio => (
                "android-radio-enable",
                "android-resume",
                Duration::from_secs(45),
                Duration::from_secs(300),
            ),
            Self::Qemu => (
                "vm-usb-attach",
                "vm-fixture-start",
                Duration::from_secs(180),
                Duration::from_secs(180),
            ),
        };
        let first_result = run_checked_recipe(first, &[], first_timeout);
        let second_result = run_checked_recipe(second, &[], second_timeout);
        aggregate_results([
            (
                first,
                first_result.map_err(|error: ExampleError| error.to_string()),
            ),
            (
                second,
                second_result.map_err(|error: ExampleError| error.to_string()),
            ),
        ])
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum FaultTarget {
    Read,
    Write,
    Cccd,
    NotificationValue,
}

impl FaultTarget {
    fn parse(value: &str) -> Result<Self, ExampleError> {
        match value {
            "read" => Ok(Self::Read),
            "write" => Ok(Self::Write),
            "cccd" => Ok(Self::Cccd),
            "notification-value" => Ok(Self::NotificationValue),
            _ => Err(format!(
                "invalid target {value:?}; expected read, write, cccd, or notification-value"
            )
            .into()),
        }
    }

    fn name(self) -> &'static str {
        match self {
            Self::Read => "read",
            Self::Write => "write",
            Self::Cccd => "cccd",
            Self::NotificationValue => "notification-value",
        }
    }

    fn callback(self) -> WindowsHilCallback {
        match self {
            Self::Read => WindowsHilCallback::CharacteristicRead,
            Self::Write => WindowsHilCallback::CharacteristicWrite,
            Self::Cccd => WindowsHilCallback::CccdWrite,
            Self::NotificationValue => WindowsHilCallback::NotificationValue,
        }
    }

    fn command_id(self) -> Option<CommandId> {
        (self != Self::NotificationValue).then_some(INFLIGHT_COMMAND_ID)
    }

    fn callback_count(self, snapshot: WindowsCallbackResourceSnapshot) -> usize {
        match self {
            Self::Read => snapshot.characteristic_read,
            Self::Write => snapshot.characteristic_write,
            Self::Cccd => snapshot.cccd_write,
            Self::NotificationValue => snapshot.characteristic_value_changed,
        }
    }
}

struct Worker {
    receiver: mpsc::Receiver<Result<(), String>>,
    handle: JoinHandle<()>,
    timeout: Duration,
}

impl Worker {
    fn finish(self) -> Result<(), ExampleError> {
        let Self {
            receiver,
            handle,
            timeout,
        } = self;
        let receive_result = receiver.recv_timeout(timeout);
        let join_result = handle.join();

        match receive_result {
            Ok(result) => {
                if join_result.is_err() {
                    return Err("in-flight worker panicked after reporting its result".into());
                }
                result.map_err(Into::into)
            }
            Err(mpsc::RecvTimeoutError::Disconnected) => match join_result {
                Ok(()) => Err("in-flight worker exited without reporting a result".into()),
                Err(_) => Err("in-flight worker panicked".into()),
            },
            Err(mpsc::RecvTimeoutError::Timeout) => {
                let timeout_error = format!(
                    "in-flight worker did not finish within {timeout:?} after gate release"
                );
                let eventual_result = match join_result {
                    Ok(()) => match receiver.try_recv() {
                        Ok(result) => result,
                        Err(mpsc::TryRecvError::Empty) => Err(
                            "in-flight worker joined while its result channel remained empty"
                                .to_owned(),
                        ),
                        Err(mpsc::TryRecvError::Disconnected) => {
                            Err("in-flight worker exited without reporting a result".to_owned())
                        }
                    },
                    Err(_) => Err("in-flight worker panicked".to_owned()),
                };
                match eventual_result {
                    Ok(()) => Err(timeout_error.into()),
                    Err(error) => Err(format!(
                        "{timeout_error}\neventual in-flight worker result: {error}"
                    )
                    .into()),
                }
            }
        }
    }
}

fn main() -> Result<(), Box<dyn Error>> {
    let cli = parse_cli()?;
    let provider = cli.provider;
    let target_name = cli.target.name();
    let fault_started = AtomicBool::new(false);
    let fault_result = run(cli, &fault_started);
    let recovery_result = if fault_started.load(Ordering::Acquire) {
        provider.restore()
    } else {
        Ok(())
    };
    let result = aggregate_results([
        (
            "in-flight fault check failed",
            fault_result.map_err(|error| error.to_string()),
        ),
        (
            "fixture recovery failed",
            recovery_result.map_err(|error| error.to_string()),
        ),
    ]);
    if result.is_ok() {
        println!("Windows in-flight {target_name} fault smoke: PASS");
    }
    result
}

fn parse_cli() -> Result<Cli, ExampleError> {
    let mut arguments = std::env::args().skip(1);
    let usage = "usage: inflight-fault <timeout> <service_uuid> <android-radio|qemu> <read|write|cccd|notification-value>";
    let timeout_seconds = arguments.next().ok_or(usage)?.parse::<u64>()?;
    if timeout_seconds == 0 {
        return Err("timeout must be greater than zero seconds".into());
    }
    let service_uuid = arguments.next().ok_or(usage)?;
    let service_uuid = BluetoothUuid::canonicalize(&service_uuid)
        .ok_or_else(|| format!("invalid fixture service UUID: {service_uuid}"))?;
    let provider = FixtureProvider::parse(&arguments.next().ok_or(usage)?)?;
    let target = FaultTarget::parse(&arguments.next().ok_or(usage)?)?;
    if arguments.next().is_some() {
        return Err(usage.into());
    }
    Ok(Cli {
        timeout: Duration::from_secs(timeout_seconds),
        service_uuid,
        provider,
        target,
    })
}

fn run(cli: Cli, fault_started: &AtomicBool) -> Result<(), ExampleError> {
    let (hub, backend, probe, receiver) = create_accounted_hub()?;
    let check_result = run_fault_check(
        &hub,
        &backend,
        &probe,
        &receiver,
        cli.timeout,
        &cli.service_uuid,
        cli.provider,
        cli.target,
        fault_started,
    );
    let dispose_result = hub.dispose();
    let release_result = wait_for_resource_release(&backend, &probe, RESOURCE_RELEASE_TIMEOUT);

    receiver.finish_quiet(
        "Windows in-flight fault smoke",
        aggregate_results([
            (
                "fault sequence failed",
                check_result.map_err(|error| error.to_string()),
            ),
            (
                "Windows backend disposal failed",
                dispose_result.map_err(|error| error.to_string()),
            ),
            (
                "Windows resources did not return to exact zero",
                release_result.map_err(|error| error.to_string()),
            ),
        ]),
    )
}

#[allow(clippy::too_many_arguments)]
fn run_fault_check(
    hub: &Arc<blehub::BleHub>,
    backend: &Arc<WindowsBackend>,
    probe: &WindowsResourceProbe,
    receiver: &EventReceiver,
    timeout: Duration,
    service_uuid: &BluetoothUuid,
    provider: FixtureProvider,
    target: FaultTarget,
    fault_started: &AtomicBool,
) -> Result<(), ExampleError> {
    initialize_hub(hub, receiver, INITIALIZE_COMMAND_ID, timeout)?;
    let observation = scan_and_stop(
        hub,
        receiver,
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
        receiver,
        CONNECT_COMMAND_ID,
        CONNECTION_ID,
        ConnectionState::Connected,
        timeout,
    )?;
    let database = discover_gatt(
        hub,
        receiver,
        DISCOVER_GATT_COMMAND_ID,
        CONNECTION_ID,
        GATT_SESSION_ID,
        timeout,
    )?;
    let fixture =
        validate_fixture_database(&database, service_uuid, CONNECTION_ID, GATT_SESSION_ID)?;

    if target == FaultTarget::NotificationValue {
        set_notifications_and_wait(
            hub,
            receiver,
            notification_enable_command(fixture.updates_characteristic_id),
            NotificationState::Enabled {
                actual_mode: Some(NotificationMode::Notification),
            },
            timeout,
        )?;
        require_subscribed_resource_graph(backend, probe)?;
    } else {
        require_connected_resource_graph(backend, probe)?;
    }

    let gate = backend.arm_hil_callback_gate(target.callback());
    let worker = start_worker(Arc::clone(hub), provider, target, &fixture, timeout);
    let blocked_result = (|| {
        if !gate.wait_entered(timeout) {
            return Err(format!(
                "{} callback did not enter gate generation {} within {timeout:?}",
                target.name(),
                gate.generation()
            )
            .into());
        }
        fault_started.store(true, Ordering::Release);
        provider.fault()?;
        wait_for_fault_outcomes(receiver, target, &fixture, timeout)?;
        require_blocked_fault_snapshot(backend, probe, target)
    })();

    gate.release();
    let worker_result = worker.finish();
    let quiet_result = ensure_post_release_quiet(receiver, target, &fixture, QUIET_WINDOW);
    let connection_release_result =
        wait_for_connection_resource_release(backend, probe, RESOURCE_RELEASE_TIMEOUT);

    aggregate_results([
        (
            "blocked callback fault assertions failed",
            blocked_result.map_err(|error: ExampleError| error.to_string()),
        ),
        (
            "in-flight worker failed",
            worker_result.map_err(|error| error.to_string()),
        ),
        (
            "post-release quiet window failed",
            quiet_result.map_err(|error| error.to_string()),
        ),
        (
            "connection resources did not reach the exact released snapshot",
            connection_release_result.map_err(|error| error.to_string()),
        ),
    ])
}

fn start_worker(
    hub: Arc<blehub::BleHub>,
    provider: FixtureProvider,
    target: FaultTarget,
    fixture: &FixtureDatabase,
    timeout: Duration,
) -> Worker {
    let read_write_characteristic_id = fixture.read_write_characteristic_id;
    let updates_characteristic_id = fixture.updates_characteristic_id;
    let (sender, receiver) = mpsc::channel();
    let worker_timeout = if target == FaultTarget::NotificationValue {
        provider.notification_worker_timeout()
    } else {
        timeout
    };
    let handle = thread::spawn(move || {
        let result = match target {
            FaultTarget::Read => hub
                .read_characteristic(ReadCharacteristicCommand {
                    command_id: INFLIGHT_COMMAND_ID,
                    connection_id: CONNECTION_ID,
                    gatt_session_id: GATT_SESSION_ID,
                    characteristic_id: read_write_characteristic_id,
                })
                .map_err(|error| error.to_string()),
            FaultTarget::Write => hub
                .write_characteristic(WriteCharacteristicCommand {
                    command_id: INFLIGHT_COMMAND_ID,
                    connection_id: CONNECTION_ID,
                    gatt_session_id: GATT_SESSION_ID,
                    characteristic_id: read_write_characteristic_id,
                    value: WRITE_VALUE.to_vec(),
                    write_type: WriteType::WithResponse,
                })
                .map_err(|error| error.to_string()),
            FaultTarget::Cccd => hub
                .set_notifications(notification_enable_command(updates_characteristic_id))
                .map_err(|error| error.to_string()),
            FaultTarget::NotificationValue => provider
                .trigger_notification()
                .map_err(|error| error.to_string()),
        };
        let _ = sender.send(result);
    });
    Worker {
        receiver,
        handle,
        timeout: worker_timeout,
    }
}

fn notification_enable_command(characteristic_id: GattCharacteristicId) -> SetNotificationsCommand {
    SetNotificationsCommand {
        command_id: INFLIGHT_COMMAND_ID,
        connection_id: CONNECTION_ID,
        gatt_session_id: GATT_SESSION_ID,
        subscription_id: SUBSCRIPTION_ID,
        characteristic_id,
        action: NotificationAction::Enable {
            mode: NotificationModeRequest::PlatformDefault,
        },
    }
}

fn wait_for_fault_outcomes(
    receiver: &EventReceiver,
    target: FaultTarget,
    fixture: &FixtureDatabase,
    timeout: Duration,
) -> Result<(), ExampleError> {
    let deadline = Instant::now() + timeout;
    let mut command_failures = 0;
    let mut disconnects = 0;
    loop {
        match receive(receiver, deadline)? {
            BleEvent::CommandCompleted { command_id, error }
                if target.command_id() == Some(command_id) =>
            {
                let error = error.ok_or_else(|| {
                    format!(
                        "in-flight {} command unexpectedly completed successfully",
                        target.name()
                    )
                })?;
                if !matches!(
                    error.category,
                    BleErrorCategory::Connection | BleErrorCategory::Gatt
                ) {
                    return Err(format!(
                        "in-flight {} command failed with {:?}, expected Connection or Gatt: {}",
                        target.name(),
                        error.category,
                        error
                    )
                    .into());
                }
                command_failures += 1;
                if command_failures > 1 {
                    return Err(format!(
                        "in-flight {} command produced duplicate failures",
                        target.name()
                    )
                    .into());
                }
            }
            BleEvent::CommandCompleted { command_id, .. }
                if target == FaultTarget::NotificationValue =>
            {
                return Err(format!(
                    "notification-value fault unexpectedly produced command terminal {}",
                    command_id.value
                )
                .into());
            }
            BleEvent::CharacteristicRead { command_id, .. }
            | BleEvent::CharacteristicWritten { command_id, .. }
                if target.command_id() == Some(command_id) =>
            {
                return Err(format!(
                    "in-flight {} command produced a success event before disconnect",
                    target.name()
                )
                .into());
            }
            BleEvent::NotificationStateChanged { command_id, .. }
                if target.command_id() == Some(command_id) =>
            {
                return Err(
                    "in-flight CCCD command produced a success state before disconnect".into(),
                );
            }
            BleEvent::NotificationReceived {
                connection_id: CONNECTION_ID,
                gatt_session_id: GATT_SESSION_ID,
                subscription_id: SUBSCRIPTION_ID,
                characteristic_id,
                ..
            } if characteristic_id == fixture.updates_characteristic_id => {
                return Err(
                    "notification escaped while its value callback gate was blocked".into(),
                );
            }
            BleEvent::ConnectionStateChanged {
                command_id: None,
                connection_id: CONNECTION_ID,
                result:
                    ConnectionStateResult::Ok {
                        state: ConnectionState::Disconnected,
                    },
            } => {
                disconnects += 1;
                if disconnects > 1 {
                    return Err("passive disconnect produced duplicate disconnected events".into());
                }
            }
            BleEvent::ConnectionStateChanged {
                command_id: None,
                connection_id: CONNECTION_ID,
                result: ConnectionStateResult::Err { error },
            } => return Err(error.to_string().into()),
            BleEvent::BackendError { error } => return Err(error.to_string().into()),
            _ => {}
        }

        let command_outcome_observed = if target.command_id().is_some() {
            command_failures == 1
        } else {
            command_failures == 0
        };
        if command_outcome_observed && disconnects == 1 {
            return Ok(());
        }
    }
}

fn require_blocked_fault_snapshot(
    backend: &Arc<WindowsBackend>,
    probe: &WindowsResourceProbe,
    target: FaultTarget,
) -> Result<(), ExampleError> {
    let logical = backend.resource_snapshot();
    if logical.connection_roots != 0 {
        return Err(format!(
            "connection root remained live while {} callback was blocked: {logical:?}",
            target.name()
        )
        .into());
    }
    let callbacks = probe.snapshot();
    let target_count = target.callback_count(callbacks);
    if target_count != 1 {
        return Err(format!(
            "expected exactly one live {} callback delegate while blocked, got {target_count}; snapshot={callbacks:?}",
            target.name()
        )
        .into());
    }
    Ok(())
}

fn ensure_post_release_quiet(
    receiver: &EventReceiver,
    target: FaultTarget,
    fixture: &FixtureDatabase,
    observation: Duration,
) -> Result<(), ExampleError> {
    let deadline = Instant::now() + observation;
    loop {
        match receiver.recv_timeout(deadline.saturating_duration_since(Instant::now())) {
            Ok(BleEvent::CommandCompleted { command_id, error })
                if target.command_id() == Some(command_id) =>
            {
                return Err(format!(
                    "in-flight {} command produced a duplicate terminal after gate release: {error:?}",
                    target.name()
                )
                .into());
            }
            Ok(BleEvent::CommandCompleted { command_id, error })
                if target == FaultTarget::NotificationValue =>
            {
                return Err(format!(
                    "notification-value fault produced an unexpected post-release command terminal {}: {error:?}",
                    command_id.value
                )
                .into());
            }
            Ok(BleEvent::CharacteristicRead { command_id, .. })
            | Ok(BleEvent::CharacteristicWritten { command_id, .. })
                if target.command_id() == Some(command_id) =>
            {
                return Err(format!(
                    "in-flight {} command produced stale success after gate release",
                    target.name()
                )
                .into());
            }
            Ok(BleEvent::NotificationStateChanged { command_id, .. })
                if target.command_id() == Some(command_id) =>
            {
                return Err(
                    "in-flight CCCD command produced stale success after gate release".into(),
                );
            }
            Ok(BleEvent::NotificationReceived {
                connection_id: CONNECTION_ID,
                gatt_session_id: GATT_SESSION_ID,
                subscription_id: SUBSCRIPTION_ID,
                characteristic_id,
                ..
            }) if characteristic_id == fixture.updates_characteristic_id => {
                return Err("stale notification was delivered after gate release".into());
            }
            Ok(BleEvent::ConnectionStateChanged {
                command_id: None,
                connection_id: CONNECTION_ID,
                result:
                    ConnectionStateResult::Ok {
                        state: ConnectionState::Disconnected,
                    },
            }) => return Err("passive disconnect produced a duplicate terminal".into()),
            Ok(BleEvent::BackendError { error }) => return Err(error.to_string().into()),
            Ok(_) => {}
            Err(mpsc::RecvTimeoutError::Timeout) => return Ok(()),
            Err(error) => return Err(error.into()),
        }
    }
}

fn run_checked_recipe(
    recipe: &str,
    arguments: &[&str],
    timeout: Duration,
) -> Result<(), ExampleError> {
    let output = run_fixture_recipe(recipe, arguments, timeout)?;
    if output.status.success() {
        return Ok(());
    }
    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);
    Err(format!(
        "fixture recipe {recipe} exited with {}: {stdout}{stderr}",
        output.status
    )
    .into())
}
