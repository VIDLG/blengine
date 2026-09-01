#![allow(dead_code)]

mod process;
pub mod resource;

use std::{
    collections::VecDeque,
    error::Error,
    io::{self, Write},
    sync::{Arc, Mutex, mpsc},
    time::{Duration, Instant},
};

use blehub::{
    AdvertisementObservation, BleEvent, BleEventSink, BleHub, BluetoothUuid,
    CharacteristicProperties, CommandId, ConnectionId, ConnectionState, ConnectionStateResult,
    DiscoverGattCommand, EventSinkError, GattCharacteristic, GattCharacteristicId, GattDatabase,
    GattService, GattSessionId, InitializeCommand, NotificationState, ScanOptions, ScanSessionId,
    SetNotificationsCommand, StartScanCommand, StopScanCommand,
};
use blehub_windows::{WindowsBackend, WindowsResourceProbe};

const READ_WRITE_CHARACTERISTIC: &str = "fff10000-0000-4000-8000-000000000000";
const UPDATES_CHARACTERISTIC: &str = "fff20000-0000-4000-8000-000000000000";
const READ_ONLY_CHARACTERISTIC: &str = "fff30000-0000-4000-8000-000000000000";
const WRITE_ONLY_CHARACTERISTIC: &str = "fff40000-0000-4000-8000-000000000000";
const CCCD: &str = "00002902-0000-1000-8000-00805f9b34fb";

pub type ExampleError = Box<dyn Error>;

const EVENT_TRACE_LIMIT: usize = 16;
const EVENT_TRACE_CHAR_LIMIT: usize = 512;

pub struct EventReceiver {
    receiver: mpsc::Receiver<BleEvent>,
    trace: Mutex<VecDeque<String>>,
    verbose: bool,
}

impl EventReceiver {
    fn new(receiver: mpsc::Receiver<BleEvent>) -> Self {
        Self {
            receiver,
            trace: Mutex::new(VecDeque::with_capacity(EVENT_TRACE_LIMIT)),
            verbose: std::env::var_os("BLEHUB_HIL_VERBOSE").is_some(),
        }
    }

    pub fn recv_timeout(&self, timeout: Duration) -> Result<BleEvent, mpsc::RecvTimeoutError> {
        let event = self.receiver.recv_timeout(timeout)?;
        let full_rendered = format!("{event:?}");
        if self.verbose {
            println!("{full_rendered}");
        }
        let mut characters = full_rendered.chars();
        let mut rendered = characters
            .by_ref()
            .take(EVENT_TRACE_CHAR_LIMIT)
            .collect::<String>();
        if characters.next().is_some() {
            rendered.push_str("… [event truncated]");
        }
        let mut trace = self.trace.lock().unwrap_or_else(|error| error.into_inner());
        if trace.len() == EVENT_TRACE_LIMIT {
            trace.pop_front();
        }
        trace.push_back(rendered);
        Ok(event)
    }

    pub fn finish(
        &self,
        label: &str,
        result: Result<(), ExampleError>,
    ) -> Result<(), ExampleError> {
        self.finish_with_success_output(label, result, true)
    }

    pub fn finish_quiet(
        &self,
        label: &str,
        result: Result<(), ExampleError>,
    ) -> Result<(), ExampleError> {
        self.finish_with_success_output(label, result, false)
    }

    fn finish_with_success_output(
        &self,
        label: &str,
        result: Result<(), ExampleError>,
        print_success: bool,
    ) -> Result<(), ExampleError> {
        match result {
            Ok(()) => {
                if print_success {
                    println!("{label}: PASS");
                }
                Ok(())
            }
            Err(error) => {
                eprintln!("{label}: FAIL; recent BLE events:");
                for event in self
                    .trace
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner())
                    .iter()
                {
                    eprintln!("  {event}");
                }
                Err(error)
            }
        }
    }
}

pub fn run_fixture_recipe(
    recipe: &str,
    arguments: &[&str],
    timeout: Duration,
) -> Result<std::process::Output, ExampleError> {
    process::run_fixture_recipe(recipe, arguments, timeout)
}

pub fn request_qemu_throughput_phy() -> Result<(), ExampleError> {
    let output = run_fixture_recipe(
        "vm-fixture-request-throughput-phy",
        &[],
        Duration::from_secs(45),
    )?;
    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);
    if !output.status.success() {
        return Err(format!(
            "QEMU fixture LE 2M request exited with {}: {stdout}{stderr}",
            output.status
        )
        .into());
    }
    if !stdout.contains("phy_after=2/2") {
        return Err("QEMU fixture did not verify active LE 2M TX/RX".into());
    }
    println!("provider=qemu active_phy=2m/2m");
    Ok(())
}

pub fn aggregate_results<const N: usize>(
    results: [(&str, Result<(), String>); N],
) -> Result<(), ExampleError> {
    let failures = results
        .into_iter()
        .filter_map(|(context, result)| result.err().map(|error| format!("{context}: {error}")))
        .collect::<Vec<_>>();
    if failures.is_empty() {
        Ok(())
    } else {
        Err(failures.join("\n").into())
    }
}

pub struct FixtureDatabase {
    pub read_write_characteristic_id: GattCharacteristicId,
    pub updates_characteristic_id: GattCharacteristicId,
}

struct ChannelSink {
    sender: mpsc::Sender<BleEvent>,
}

impl BleEventSink for ChannelSink {
    fn on_event(&self, event: BleEvent) -> Result<(), EventSinkError> {
        self.sender.send(event).map_err(|error| EventSinkError {
            message: error.to_string(),
        })
    }
}

pub fn report_cycle_progress(
    label: &str,
    completed: usize,
    total: usize,
    started: Instant,
) -> Result<(), ExampleError> {
    let interval = (total / 10).max(1);
    if completed.is_multiple_of(interval) || completed == total {
        println!(
            "{label}: progress {completed}/{total}, elapsed={:?}",
            started.elapsed()
        );
        io::stdout().flush()?;
    }
    Ok(())
}

pub fn parse_cycle_count(default: usize) -> Result<usize, ExampleError> {
    let cycles = std::env::args()
        .nth(3)
        .map(|value| value.parse::<usize>())
        .transpose()?
        .unwrap_or(default);
    if cycles == 0 {
        return Err("cycle count must be greater than zero".into());
    }
    Ok(cycles)
}

pub fn parse_timeout_and_service(
    default_fixture_service: &str,
) -> Result<(Duration, BluetoothUuid), ExampleError> {
    let timeout = std::env::args()
        .nth(1)
        .map(|value| value.parse::<u64>())
        .transpose()?
        .map_or(Duration::from_secs(30), Duration::from_secs);
    let service_uuid = std::env::args()
        .nth(2)
        .unwrap_or_else(|| default_fixture_service.to_owned());
    let service_uuid = BluetoothUuid::canonicalize(&service_uuid)
        .ok_or_else(|| format!("invalid fixture service UUID: {service_uuid}"))?;
    Ok((timeout, service_uuid))
}

pub fn create_hub() -> Result<(BleHub, EventReceiver), ExampleError> {
    let (sender, receiver) = mpsc::channel();
    let backend = Arc::new(WindowsBackend::new()?);
    let hub = BleHub::new(backend, Box::new(ChannelSink { sender }))?;
    Ok((hub, EventReceiver::new(receiver)))
}

pub fn create_accounted_hub() -> Result<
    (
        BleHub,
        Arc<WindowsBackend>,
        WindowsResourceProbe,
        EventReceiver,
    ),
    ExampleError,
> {
    let (sender, receiver) = mpsc::channel();
    let (backend, probe) = WindowsBackend::new_accounted()?;
    let backend = Arc::new(backend);
    let hub = BleHub::new(backend.clone(), Box::new(ChannelSink { sender }))?;
    Ok((hub, backend, probe, EventReceiver::new(receiver)))
}

pub fn initialize_hub(
    hub: &BleHub,
    receiver: &EventReceiver,
    command_id: CommandId,
    timeout: Duration,
) -> Result<(), ExampleError> {
    hub.initialize(InitializeCommand { command_id })?;
    wait_for_command(receiver, command_id, timeout)
}

pub fn scan_and_stop(
    hub: &BleHub,
    receiver: &EventReceiver,
    start_command_id: CommandId,
    stop_command_id: CommandId,
    scan_session_id: ScanSessionId,
    service_uuid: BluetoothUuid,
    timeout: Duration,
) -> Result<AdvertisementObservation, ExampleError> {
    hub.start_scan(StartScanCommand {
        command_id: start_command_id,
        scan_session_id,
        options: ScanOptions {
            service_uuids: vec![service_uuid],
            allow_duplicates: true,
            ..ScanOptions::default()
        },
    })?;

    let deadline = Instant::now() + timeout;
    let mut start_completed = false;
    let mut observation = None;
    loop {
        match receive(receiver, deadline)? {
            BleEvent::CommandCompleted { command_id, error } if command_id == start_command_id => {
                match error {
                    None => start_completed = true,
                    Some(error) => return Err(error.to_string().into()),
                }
            }
            BleEvent::AdvertisementObserved { advertisement } => {
                observation = Some(advertisement);
            }
            _ => {}
        }
        if start_completed && let Some(observation) = observation {
            hub.stop_scan(StopScanCommand {
                command_id: stop_command_id,
                scan_session_id,
            })?;
            wait_for_command(receiver, stop_command_id, timeout)?;
            return Ok(observation);
        }
    }
}

pub fn wait_for_command(
    receiver: &EventReceiver,
    command_id: CommandId,
    timeout: Duration,
) -> Result<(), ExampleError> {
    let deadline = Instant::now() + timeout;
    loop {
        if let BleEvent::CommandCompleted {
            command_id: completed_id,
            error,
        } = receive(receiver, deadline)?
            && completed_id == command_id
        {
            return match error {
                None => Ok(()),
                Some(error) => Err(error.to_string().into()),
            };
        }
    }
}

pub fn set_notifications_and_wait(
    hub: &BleHub,
    receiver: &EventReceiver,
    command: SetNotificationsCommand,
    expected_state: NotificationState,
    timeout: Duration,
) -> Result<(), ExampleError> {
    let command_id = command.command_id;
    let connection_id = command.connection_id;
    let gatt_session_id = command.gatt_session_id;
    let subscription_id = command.subscription_id;
    let characteristic_id = command.characteristic_id;
    hub.set_notifications(command)?;

    let deadline = Instant::now() + timeout;
    let mut state_observed = false;
    loop {
        match receive(receiver, deadline)? {
            BleEvent::NotificationStateChanged {
                command_id: event_command_id,
                connection_id: event_connection_id,
                gatt_session_id: event_gatt_session_id,
                subscription_id: event_subscription_id,
                characteristic_id: event_characteristic_id,
                state,
            } if event_command_id == command_id
                && event_connection_id == connection_id
                && event_gatt_session_id == gatt_session_id
                && event_subscription_id == subscription_id
                && event_characteristic_id == characteristic_id =>
            {
                if state != expected_state {
                    return Err(format!(
                        "notification state mismatch: expected {expected_state:?}, got {state:?}"
                    )
                    .into());
                }
                state_observed = true;
            }
            BleEvent::CommandCompleted {
                command_id: completed_id,
                error,
            } if completed_id == command_id => match error {
                Some(error) => return Err(error.to_string().into()),
                None if state_observed => return Ok(()),
                None => {
                    return Err(format!(
                        "notification command {} completed before NotificationStateChanged",
                        command_id.value
                    )
                    .into());
                }
            },
            _ => {}
        }
    }
}

pub fn wait_for_connection_command(
    receiver: &EventReceiver,
    command_id: CommandId,
    connection_id: ConnectionId,
    expected_state: ConnectionState,
    timeout: Duration,
) -> Result<(), ExampleError> {
    let deadline = Instant::now() + timeout;
    let mut state_observed = false;
    let mut command_completed = false;
    loop {
        match receive(receiver, deadline)? {
            BleEvent::ConnectionStateChanged {
                command_id: Some(event_command_id),
                connection_id: event_connection_id,
                result,
            } if event_command_id == command_id && event_connection_id == connection_id => {
                match result {
                    ConnectionStateResult::Ok { state } if state == expected_state => {
                        state_observed = true;
                    }
                    ConnectionStateResult::Ok { .. } => {}
                    ConnectionStateResult::Err { error } => {
                        return Err(error.to_string().into());
                    }
                }
            }
            BleEvent::CommandCompleted {
                command_id: completed_id,
                error,
            } if completed_id == command_id => match error {
                None => command_completed = true,
                Some(error) => return Err(error.to_string().into()),
            },
            _ => {}
        }
        if state_observed && command_completed {
            return Ok(());
        }
    }
}

pub fn discover_gatt(
    hub: &BleHub,
    receiver: &EventReceiver,
    command_id: CommandId,
    connection_id: ConnectionId,
    gatt_session_id: GattSessionId,
    timeout: Duration,
) -> Result<GattDatabase, ExampleError> {
    hub.discover_gatt(DiscoverGattCommand {
        command_id,
        connection_id,
        gatt_session_id,
    })?;

    let deadline = Instant::now() + timeout;
    let mut database = None;
    loop {
        match receive(receiver, deadline)? {
            BleEvent::GattDiscovered {
                command_id: event_command_id,
                database: discovered,
            } if event_command_id == command_id => database = Some(discovered),
            BleEvent::CommandCompleted {
                command_id: completed_id,
                error,
            } if completed_id == command_id => match error {
                Some(error) => return Err(error.to_string().into()),
                None => {
                    return database.ok_or_else(|| {
                        "GATT discovery terminal arrived before GattDiscovered".into()
                    });
                }
            },
            _ => {}
        }
    }
}

pub fn validate_fixture_database(
    database: &GattDatabase,
    fixture_service_uuid: &BluetoothUuid,
    connection_id: ConnectionId,
    gatt_session_id: GattSessionId,
) -> Result<FixtureDatabase, ExampleError> {
    if database.connection_id != connection_id || database.gatt_session_id != gatt_session_id {
        return Err("GATT database did not preserve the requested connection/session IDs".into());
    }
    let service = find_service(database, fixture_service_uuid)?;
    if service.primary != Some(true) {
        return Err("Windows root fixture service was not marked primary".into());
    }

    let read_write = require_characteristic(
        service,
        READ_WRITE_CHARACTERISTIC,
        CharacteristicProperties {
            readable: true,
            writable_with_response: true,
            writable_without_response: true,
            notifiable: false,
            indicatable: false,
        },
    )?;
    let updates = require_characteristic(
        service,
        UPDATES_CHARACTERISTIC,
        CharacteristicProperties {
            readable: true,
            writable_with_response: false,
            writable_without_response: false,
            notifiable: true,
            indicatable: true,
        },
    )?;
    require_characteristic(
        service,
        READ_ONLY_CHARACTERISTIC,
        CharacteristicProperties {
            readable: true,
            writable_with_response: false,
            writable_without_response: false,
            notifiable: false,
            indicatable: false,
        },
    )?;
    require_characteristic(
        service,
        WRITE_ONLY_CHARACTERISTIC,
        CharacteristicProperties {
            readable: false,
            writable_with_response: true,
            writable_without_response: true,
            notifiable: false,
            indicatable: false,
        },
    )?;
    let cccd = canonical_uuid(CCCD)?;
    if !updates
        .descriptors
        .iter()
        .any(|descriptor| descriptor.uuid == cccd)
    {
        return Err("updates characteristic did not expose the CCCD".into());
    }
    Ok(FixtureDatabase {
        read_write_characteristic_id: read_write.id,
        updates_characteristic_id: updates.id,
    })
}

fn find_service<'a>(
    database: &'a GattDatabase,
    uuid: &BluetoothUuid,
) -> Result<&'a GattService, ExampleError> {
    database
        .services
        .iter()
        .find(|service| &service.uuid == uuid)
        .ok_or_else(|| format!("fixture service {uuid:?} was not discovered").into())
}

fn require_characteristic<'a>(
    service: &'a GattService,
    uuid: &str,
    expected_properties: CharacteristicProperties,
) -> Result<&'a GattCharacteristic, ExampleError> {
    let uuid = canonical_uuid(uuid)?;
    let characteristic = service
        .characteristics
        .iter()
        .find(|characteristic| characteristic.uuid == uuid)
        .ok_or_else(|| format!("fixture characteristic {uuid:?} was not discovered"))?;
    if characteristic.properties != expected_properties {
        return Err(format!(
            "fixture characteristic {uuid:?} properties mismatch: {:?}",
            characteristic.properties
        )
        .into());
    }
    Ok(characteristic)
}

fn canonical_uuid(value: &str) -> Result<BluetoothUuid, ExampleError> {
    BluetoothUuid::canonicalize(value)
        .ok_or_else(|| format!("invalid expected Bluetooth UUID: {value}").into())
}

pub fn receive(receiver: &EventReceiver, deadline: Instant) -> Result<BleEvent, ExampleError> {
    Ok(receiver.recv_timeout(deadline.saturating_duration_since(Instant::now()))?)
}
