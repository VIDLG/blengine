use std::{
    error::Error,
    sync::mpsc,
    time::{Duration, Instant},
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
    parse_timeout_and_service, receive, run_fixture_recipe, scan_and_stop,
    set_notifications_and_wait, validate_fixture_database, wait_for_connection_command,
};

const DEFAULT_FIXTURE_SERVICE: &str = "fff00000-0000-4000-8000-000000000000";
const INITIALIZE_COMMAND_ID: CommandId = CommandId { value: 1 };
const START_SCAN_COMMAND_ID: CommandId = CommandId { value: 2 };
const STOP_SCAN_COMMAND_ID: CommandId = CommandId { value: 3 };
const CONNECT_COMMAND_ID: CommandId = CommandId { value: 4 };
const DISCOVER_GATT_COMMAND_ID: CommandId = CommandId { value: 5 };
const ENABLE_NOTIFICATIONS_COMMAND_ID: CommandId = CommandId { value: 6 };
const DISABLE_NOTIFICATIONS_COMMAND_ID: CommandId = CommandId { value: 7 };
const DISCONNECT_COMMAND_ID: CommandId = CommandId { value: 8 };
const FIRST_VALUE_HEX: &str = "deadbeef";
const FIRST_VALUE: &[u8] = &[0xDE, 0xAD, 0xBE, 0xEF];
const AFTER_DISABLE_VALUE_HEX: &str = "cafebabe";
const SCAN_SESSION_ID: ScanSessionId = ScanSessionId { value: 1 };
const CONNECTION_ID: ConnectionId = ConnectionId { value: 1 };
const GATT_SESSION_ID: GattSessionId = GattSessionId { value: 1 };
const SUBSCRIPTION_ID: SubscriptionId = SubscriptionId { value: 1 };
const AFTER_DISABLE_OBSERVATION: Duration = Duration::from_secs(2);

#[derive(Clone, Copy)]
enum SmokeMode {
    Default,
    Notification,
    Indication,
}

impl SmokeMode {
    fn parse(value: &str) -> Result<Self, Box<dyn Error>> {
        match value {
            "default" => Ok(Self::Default),
            "notification" => Ok(Self::Notification),
            "indication" => Ok(Self::Indication),
            _ => Err(format!(
                "invalid notification mode {value:?}; expected default, notification, or indication"
            )
            .into()),
        }
    }

    fn request(self) -> NotificationModeRequest {
        match self {
            Self::Default => NotificationModeRequest::PlatformDefault,
            Self::Notification => NotificationModeRequest::RequireNotification,
            Self::Indication => NotificationModeRequest::RequireIndication,
        }
    }

    fn actual(self) -> NotificationMode {
        match self {
            Self::Default | Self::Notification => NotificationMode::Notification,
            Self::Indication => NotificationMode::Indication,
        }
    }
}

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

    fn update_recipe(self, mode: SmokeMode) -> &'static str {
        match (self, mode) {
            (Self::Android, SmokeMode::Default | SmokeMode::Notification) => "android-notify",
            (Self::Android, SmokeMode::Indication) => "android-indicate",
            (Self::Qemu, _) => "vm-fixture-update",
        }
    }

    fn update_succeeds_after_disable(self) -> bool {
        matches!(self, Self::Qemu)
    }

    fn update_timeout(self) -> Duration {
        match self {
            Self::Android => Duration::from_secs(45),
            Self::Qemu => Duration::from_secs(180),
        }
    }

    fn name(self) -> &'static str {
        match self {
            Self::Android => "Android",
            Self::Qemu => "QEMU/BLESS",
        }
    }
}

fn main() -> Result<(), Box<dyn Error>> {
    let (timeout, service_uuid) = parse_timeout_and_service(DEFAULT_FIXTURE_SERVICE)?;
    let mode = SmokeMode::parse(
        &std::env::args()
            .nth(3)
            .unwrap_or_else(|| "default".to_owned()),
    )?;
    let provider = FixtureProvider::parse(
        &std::env::args()
            .nth(4)
            .unwrap_or_else(|| "android".to_owned()),
    )?;
    let (hub, receiver) = create_hub()?;

    let smoke_result: Result<(), Box<dyn Error>> = (|| {
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
                        mode: mode.request(),
                    },
                },
                NotificationState::Enabled {
                    actual_mode: Some(mode.actual()),
                },
                timeout,
            )?;

            run_fixture_update(provider, mode, FIRST_VALUE_HEX, true)?;
            wait_for_notification(&receiver, characteristic_id, FIRST_VALUE, timeout)?;

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

            run_fixture_update(
                provider,
                mode,
                AFTER_DISABLE_VALUE_HEX,
                provider.update_succeeds_after_disable(),
            )?;
            ensure_no_notification(&receiver, characteristic_id, AFTER_DISABLE_OBSERVATION)?;
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
                "notification check failed",
                notification_result.map_err(|error| error.to_string()),
            ),
            (
                "disconnect cleanup failed",
                disconnect_result.map_err(|error| error.to_string()),
            ),
        ])
    })();
    let dispose_result = hub.dispose();

    receiver.finish(
        "Windows notification smoke",
        aggregate_results([
            (
                "notification smoke failed",
                smoke_result.map_err(|error| error.to_string()),
            ),
            (
                "Windows backend disposal failed",
                dispose_result.map_err(|error| error.to_string()),
            ),
        ]),
    )
}

fn wait_for_notification(
    receiver: &EventReceiver,
    characteristic_id: GattCharacteristicId,
    expected_value: &[u8],
    timeout: Duration,
) -> Result<(), Box<dyn Error>> {
    let deadline = Instant::now() + timeout;
    loop {
        if let BleEvent::NotificationReceived {
            connection_id: CONNECTION_ID,
            gatt_session_id: GATT_SESSION_ID,
            subscription_id: SUBSCRIPTION_ID,
            characteristic_id: event_characteristic_id,
            value,
        } = receive(receiver, deadline)?
            && event_characteristic_id == characteristic_id
        {
            if value != expected_value {
                return Err(format!(
                    "notification value mismatch: expected {expected_value:02X?}, got {value:02X?}"
                )
                .into());
            }
            return Ok(());
        }
    }
}

fn ensure_no_notification(
    receiver: &EventReceiver,
    characteristic_id: GattCharacteristicId,
    observation: Duration,
) -> Result<(), Box<dyn Error>> {
    let deadline = Instant::now() + observation;
    loop {
        match receiver.recv_timeout(deadline.saturating_duration_since(Instant::now())) {
            Ok(event) => {
                if matches!(
                    event,
                    BleEvent::NotificationReceived {
                        connection_id: CONNECTION_ID,
                        gatt_session_id: GATT_SESSION_ID,
                        subscription_id: SUBSCRIPTION_ID,
                        characteristic_id: event_characteristic_id,
                        ..
                    } if event_characteristic_id == characteristic_id
                ) {
                    return Err("received a notification after disable completed".into());
                }
            }
            Err(mpsc::RecvTimeoutError::Timeout) => return Ok(()),
            Err(error) => return Err(error.into()),
        }
    }
}

fn run_fixture_update(
    provider: FixtureProvider,
    mode: SmokeMode,
    value: &str,
    expect_success: bool,
) -> Result<(), Box<dyn Error>> {
    let recipe = provider.update_recipe(mode);
    let output = run_fixture_recipe(recipe, &[value], provider.update_timeout())?;
    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);
    if output.status.success() != expect_success {
        return Err(format!(
            "{} fixture {} unexpectedly {}: {}{}",
            provider.name(),
            recipe,
            if output.status.success() {
                "succeeded"
            } else {
                "failed"
            },
            stdout,
            stderr,
        )
        .into());
    }
    if !expect_success
        && matches!(provider, FixtureProvider::Android)
        && !stdout.contains("no connected device subscribed")
    {
        return Err(format!(
            "Android fixture failed after disable for an unexpected reason: {stdout}{stderr}"
        )
        .into());
    }
    Ok(())
}
