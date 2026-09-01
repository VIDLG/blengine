use std::{
    error::Error,
    time::{Duration, Instant},
};

use blehub::{
    BleEvent, CommandId, ConnectCommand, ConnectionId, ConnectionState, ConnectionStateResult,
    GattSessionId, ScanSessionId,
};

mod support;

use support::{
    EventReceiver, aggregate_results, create_hub, discover_gatt, initialize_hub,
    parse_timeout_and_service, receive, run_fixture_recipe as run_bounded_fixture_recipe,
    scan_and_stop, validate_fixture_database, wait_for_connection_command,
};

const DEFAULT_FIXTURE_SERVICE: &str = "fff00000-0000-4000-8000-000000000000";
const INITIALIZE_COMMAND_ID: CommandId = CommandId { value: 1 };
const START_SCAN_COMMAND_ID: CommandId = CommandId { value: 2 };
const STOP_SCAN_COMMAND_ID: CommandId = CommandId { value: 3 };
const CONNECT_COMMAND_ID: CommandId = CommandId { value: 4 };
const DISCOVER_GATT_COMMAND_ID: CommandId = CommandId { value: 5 };
const SCAN_SESSION_ID: ScanSessionId = ScanSessionId { value: 1 };
const CONNECTION_ID: ConnectionId = ConnectionId { value: 1 };
const GATT_SESSION_ID: GattSessionId = GattSessionId { value: 1 };

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
                run_all_fixture_recipes(&["android-radio-enable", "android-start"])
            }
            Self::Qemu => run_all_fixture_recipes(&["vm-usb-attach", "vm-fixture-start"]),
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
    let retain_discovered_gatt = match std::env::args().nth(4).as_deref().unwrap_or("connect-only")
    {
        "connect-only" => false,
        "discover" => true,
        value => {
            return Err(format!(
                "invalid connection anchor {value:?}; expected connect-only or discover"
            )
            .into());
        }
    };
    let (hub, receiver) = create_hub()?;

    let mut recovery_required = false;
    let passive_result = (|| {
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

        if retain_discovered_gatt {
            let database = discover_gatt(
                &hub,
                &receiver,
                DISCOVER_GATT_COMMAND_ID,
                CONNECTION_ID,
                GATT_SESSION_ID,
                timeout,
            )?;
            validate_fixture_database(&database, &service_uuid, CONNECTION_ID, GATT_SESSION_ID)?;
        }

        recovery_required = true;
        run_fixture_recipe(provider.disconnect_recipe())?;
        wait_for_passive_disconnect(&receiver, CONNECTION_ID, timeout)
    })();
    let dispose_result = hub.dispose();
    let restore_result = if recovery_required {
        provider.restore()
    } else {
        Ok(())
    };

    receiver.finish(
        "Windows passive-disconnect smoke",
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
                "fixture recovery failed",
                restore_result.map_err(|error| error.to_string()),
            ),
        ]),
    )
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
