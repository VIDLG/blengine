use std::{
    error::Error,
    time::{Duration, Instant},
};

use blehub::{
    BleEvent, BluetoothUuid, CommandId, ConnectCommand, ConnectionId, ConnectionState,
    ConnectionStateResult, GattSessionId, NotificationAction, NotificationMode,
    NotificationModeRequest, NotificationState, ScanSessionId, SetNotificationsCommand,
    SubscriptionId,
};

mod support;

use support::{
    EventReceiver, aggregate_results, create_accounted_hub, discover_gatt, initialize_hub,
    parse_timeout_and_service, receive, report_cycle_progress,
    resource::{
        require_connected_resource_graph, require_subscribed_resource_graph,
        wait_for_connection_resource_release, wait_for_resource_release,
    },
    run_fixture_recipe as run_bounded_fixture_recipe, scan_and_stop, set_notifications_and_wait,
    validate_fixture_database, wait_for_connection_command,
};

const DEFAULT_FIXTURE_SERVICE: &str = "fff00000-0000-4000-8000-000000000000";
const DEFAULT_CYCLES: usize = 1;
const MAX_CYCLES: usize = 10;
const RESOURCE_RELEASE_TIMEOUT: Duration = Duration::from_secs(5);

#[derive(Clone, Copy)]
enum FixtureProvider {
    Android,
    AndroidRadio,
    Qemu,
}

impl FixtureProvider {
    fn parse(value: &str) -> Result<Self, Box<dyn Error>> {
        match value {
            "android" => Ok(Self::Android),
            "android-radio" => Ok(Self::AndroidRadio),
            "qemu" => Ok(Self::Qemu),
            _ => Err(format!(
                "invalid fixture provider {value:?}; expected android, android-radio, or qemu"
            )
            .into()),
        }
    }

    fn disconnect_recipe(self) -> &'static str {
        match self {
            Self::Android => "android-disconnect",
            Self::AndroidRadio => "android-radio-disable",
            Self::Qemu => "vm-usb-detach",
        }
    }

    fn restore(self) -> Result<(), Box<dyn Error>> {
        match self {
            Self::Android => Ok(()),
            Self::AndroidRadio => {
                run_all_fixture_recipes(&["android-radio-enable", "android-resume"])
            }
            Self::Qemu => run_all_fixture_recipes(&["vm-usb-attach", "vm-fixture-start"]),
        }
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum ConnectionAnchor {
    ConnectOnly,
    Discover,
    Subscribed,
}

impl ConnectionAnchor {
    fn parse(value: &str) -> Result<Self, Box<dyn Error>> {
        match value {
            "connect-only" => Ok(Self::ConnectOnly),
            "discover" => Ok(Self::Discover),
            "subscribed" => Ok(Self::Subscribed),
            _ => Err(format!(
                "invalid connection anchor {value:?}; expected connect-only, discover, or subscribed"
            )
            .into()),
        }
    }
}

fn main() -> Result<(), Box<dyn Error>> {
    let (timeout, service_uuid) = parse_timeout_and_service(DEFAULT_FIXTURE_SERVICE)?;
    let provider = FixtureProvider::parse(
        &std::env::args()
            .nth(3)
            .unwrap_or_else(|| "android".to_owned()),
    )?;
    let anchor = ConnectionAnchor::parse(
        &std::env::args()
            .nth(4)
            .unwrap_or_else(|| "connect-only".to_owned()),
    )?;
    let cycles = parse_cycles()?;
    let started = Instant::now();

    for cycle in 1..=cycles {
        run_cycle(cycle, cycles, timeout, &service_uuid, provider, anchor)?;
        report_cycle_progress("Windows passive-disconnect smoke", cycle, cycles, started)?;
    }

    println!("Windows passive-disconnect smoke: PASS ({cycles}/{cycles} cycles)");
    Ok(())
}

fn parse_cycles() -> Result<usize, Box<dyn Error>> {
    let cycles = std::env::args()
        .nth(5)
        .map(|value| value.parse::<usize>())
        .transpose()?
        .unwrap_or(DEFAULT_CYCLES);
    if !(1..=MAX_CYCLES).contains(&cycles) {
        return Err(format!("cycle count must be in range 1..={MAX_CYCLES}").into());
    }
    Ok(cycles)
}

fn run_cycle(
    cycle: usize,
    cycles: usize,
    timeout: Duration,
    service_uuid: &BluetoothUuid,
    provider: FixtureProvider,
    anchor: ConnectionAnchor,
) -> Result<(), Box<dyn Error>> {
    let cycle_id = u64::try_from(cycle)?;
    let command_base = cycle_id * 100;
    let initialize_command_id = command_id(command_base + 1);
    let start_scan_command_id = command_id(command_base + 2);
    let stop_scan_command_id = command_id(command_base + 3);
    let connect_command_id = command_id(command_base + 4);
    let discover_gatt_command_id = command_id(command_base + 5);
    let enable_notifications_command_id = command_id(command_base + 6);
    let scan_session_id = ScanSessionId { value: cycle_id };
    let connection_id = ConnectionId { value: cycle_id };
    let gatt_session_id = GattSessionId { value: cycle_id };
    let subscription_id = SubscriptionId { value: cycle_id };
    let (hub, backend, probe, receiver) = create_accounted_hub()?;

    let mut recovery_required = false;
    let passive_result = (|| {
        initialize_hub(&hub, &receiver, initialize_command_id, timeout)?;
        let observation = scan_and_stop(
            &hub,
            &receiver,
            start_scan_command_id,
            stop_scan_command_id,
            scan_session_id,
            service_uuid.clone(),
            timeout,
        )?;

        hub.connect(ConnectCommand {
            command_id: connect_command_id,
            connection_id,
            peripheral_id: observation.peripheral_id,
        })?;
        wait_for_connection_command(
            &receiver,
            connect_command_id,
            connection_id,
            ConnectionState::Connected,
            timeout,
        )?;

        if anchor != ConnectionAnchor::ConnectOnly {
            let database = discover_gatt(
                &hub,
                &receiver,
                discover_gatt_command_id,
                connection_id,
                gatt_session_id,
                timeout,
            )?;
            let fixture =
                validate_fixture_database(&database, service_uuid, connection_id, gatt_session_id)?;
            if anchor == ConnectionAnchor::Subscribed {
                set_notifications_and_wait(
                    &hub,
                    &receiver,
                    SetNotificationsCommand {
                        command_id: enable_notifications_command_id,
                        connection_id,
                        gatt_session_id,
                        subscription_id,
                        characteristic_id: fixture.updates_characteristic_id,
                        action: NotificationAction::Enable {
                            mode: NotificationModeRequest::PlatformDefault,
                        },
                    },
                    NotificationState::Enabled {
                        actual_mode: Some(NotificationMode::Notification),
                    },
                    timeout,
                )?;
            }
        }

        if anchor == ConnectionAnchor::Subscribed {
            require_subscribed_resource_graph(&backend, &probe)?;
        } else {
            require_connected_resource_graph(&backend, &probe)?;
        }

        recovery_required = true;
        run_fixture_recipe(provider.disconnect_recipe())?;
        wait_for_passive_disconnect(&receiver, connection_id, timeout)?;
        wait_for_connection_resource_release(&backend, &probe, RESOURCE_RELEASE_TIMEOUT)
    })();
    let dispose_result = hub.dispose();
    let release_result = wait_for_resource_release(&backend, &probe, RESOURCE_RELEASE_TIMEOUT);
    let restore_result = if recovery_required {
        provider.restore()
    } else {
        Ok(())
    };

    receiver.finish_quiet(
        &format!("Windows passive-disconnect cycle {cycle}/{cycles}"),
        aggregate_results([
            (
                "passive disconnect check failed",
                passive_result.map_err(|error: Box<dyn Error>| error.to_string()),
            ),
            (
                "Windows backend disposal failed",
                dispose_result.map_err(|error| error.to_string()),
            ),
            (
                "Windows resource release after disposal failed",
                release_result.map_err(|error| error.to_string()),
            ),
            (
                "fixture recovery failed",
                restore_result.map_err(|error| error.to_string()),
            ),
        ]),
    )?;
    Ok(())
}

fn command_id(value: u64) -> CommandId {
    CommandId { value }
}

fn wait_for_passive_disconnect(
    receiver: &EventReceiver,
    connection_id: ConnectionId,
    timeout: Duration,
) -> Result<(), Box<dyn Error>> {
    let deadline = Instant::now() + timeout;
    loop {
        match receive(receiver, deadline)? {
            BleEvent::ConnectionStateChanged {
                command_id: None,
                connection_id: event_connection_id,
                result:
                    ConnectionStateResult::Ok {
                        state: ConnectionState::Disconnected,
                    },
            } if event_connection_id == connection_id => return Ok(()),
            BleEvent::ConnectionStateChanged {
                command_id: None,
                connection_id: event_connection_id,
                result: ConnectionStateResult::Err { error },
            } if event_connection_id == connection_id => return Err(error.to_string().into()),
            BleEvent::BackendError { error } => return Err(error.to_string().into()),
            _ => {}
        }
    }
}

fn run_all_fixture_recipes(recipes: &[&str]) -> Result<(), Box<dyn Error>> {
    let failures = recipes
        .iter()
        .filter_map(|recipe| {
            run_fixture_recipe(recipe)
                .err()
                .map(|error| format!("{recipe}: {error}"))
        })
        .collect::<Vec<_>>();
    if failures.is_empty() {
        Ok(())
    } else {
        Err(failures.join("\n").into())
    }
}

fn run_fixture_recipe(recipe: &str) -> Result<(), Box<dyn Error>> {
    let timeout = match recipe {
        "android-radio-disable" | "android-radio-enable" => Duration::from_secs(45),
        "android-start" => Duration::from_secs(300),
        _ => Duration::from_secs(180),
    };
    let output = run_bounded_fixture_recipe(recipe, &[], timeout)?;
    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);
    if output.status.success() {
        Ok(())
    } else {
        Err(format!(
            "fixture recipe {recipe} exited with {}: {stdout}{stderr}",
            output.status
        )
        .into())
    }
}
