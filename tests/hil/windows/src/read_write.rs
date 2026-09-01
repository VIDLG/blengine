use std::{
    error::Error,
    time::{Duration, Instant},
};

use blehub::{
    BleEvent, CommandId, ConnectCommand, ConnectionId, ConnectionState, DisconnectCommand,
    GattCharacteristicId, GattSessionId, ReadCharacteristicCommand, ScanSessionId,
    WriteCharacteristicCommand, WriteType,
};

mod support;

use support::{
    EventReceiver, aggregate_results, create_hub, discover_gatt, initialize_hub,
    parse_timeout_and_service, receive, scan_and_stop, validate_fixture_database,
    wait_for_connection_command,
};

const DEFAULT_FIXTURE_SERVICE: &str = "fff00000-0000-4000-8000-000000000000";
const INITIALIZE_COMMAND_ID: CommandId = CommandId { value: 1 };
const START_SCAN_COMMAND_ID: CommandId = CommandId { value: 2 };
const STOP_SCAN_COMMAND_ID: CommandId = CommandId { value: 3 };
const CONNECT_COMMAND_ID: CommandId = CommandId { value: 4 };
const DISCOVER_GATT_COMMAND_ID: CommandId = CommandId { value: 5 };
const INITIAL_READ_COMMAND_ID: CommandId = CommandId { value: 6 };
const WRITE_WITH_RESPONSE_COMMAND_ID: CommandId = CommandId { value: 7 };
const READ_AFTER_RESPONSE_COMMAND_ID: CommandId = CommandId { value: 8 };
const WRITE_WITHOUT_RESPONSE_COMMAND_ID: CommandId = CommandId { value: 9 };
const READ_AFTER_COMMAND_COMMAND_ID: CommandId = CommandId { value: 10 };
const DISCONNECT_COMMAND_ID: CommandId = CommandId { value: 11 };
const WITH_RESPONSE_VALUE: &[u8] = &[0x10, 0x20, 0x30];
const WITHOUT_RESPONSE_VALUE: &[u8] = &[0xAB, 0xCD];
const SCAN_SESSION_ID: ScanSessionId = ScanSessionId { value: 1 };
const CONNECTION_ID: ConnectionId = ConnectionId { value: 1 };
const GATT_SESSION_ID: GattSessionId = GattSessionId { value: 1 };

fn main() -> Result<(), Box<dyn Error>> {
    let (timeout, service_uuid) = parse_timeout_and_service(DEFAULT_FIXTURE_SERVICE)?;
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

            hub.read_characteristic(ReadCharacteristicCommand {
                command_id: INITIAL_READ_COMMAND_ID,
                connection_id: CONNECTION_ID,
                gatt_session_id: GATT_SESSION_ID,
                characteristic_id,
            })?;
            let _initial = wait_for_read(
                &receiver,
                INITIAL_READ_COMMAND_ID,
                characteristic_id,
                timeout,
            )?;

            hub.write_characteristic(WriteCharacteristicCommand {
                command_id: WRITE_WITH_RESPONSE_COMMAND_ID,
                connection_id: CONNECTION_ID,
                gatt_session_id: GATT_SESSION_ID,
                characteristic_id,
                value: WITH_RESPONSE_VALUE.to_vec(),
                write_type: WriteType::WithResponse,
            })?;
            wait_for_write(
                &receiver,
                WRITE_WITH_RESPONSE_COMMAND_ID,
                characteristic_id,
                timeout,
            )?;
            hub.read_characteristic(ReadCharacteristicCommand {
                command_id: READ_AFTER_RESPONSE_COMMAND_ID,
                connection_id: CONNECTION_ID,
                gatt_session_id: GATT_SESSION_ID,
                characteristic_id,
            })?;
            let response_value = wait_for_read(
                &receiver,
                READ_AFTER_RESPONSE_COMMAND_ID,
                characteristic_id,
                timeout,
            )?;
            require_value("write with response", &response_value, WITH_RESPONSE_VALUE)?;

            hub.write_characteristic(WriteCharacteristicCommand {
                command_id: WRITE_WITHOUT_RESPONSE_COMMAND_ID,
                connection_id: CONNECTION_ID,
                gatt_session_id: GATT_SESSION_ID,
                characteristic_id,
                value: WITHOUT_RESPONSE_VALUE.to_vec(),
                write_type: WriteType::WithoutResponse,
            })?;
            wait_for_write(
                &receiver,
                WRITE_WITHOUT_RESPONSE_COMMAND_ID,
                characteristic_id,
                timeout,
            )?;
            std::thread::sleep(Duration::from_millis(500));
            hub.read_characteristic(ReadCharacteristicCommand {
                command_id: READ_AFTER_COMMAND_COMMAND_ID,
                connection_id: CONNECTION_ID,
                gatt_session_id: GATT_SESSION_ID,
                characteristic_id,
            })?;
            let command_value = wait_for_read(
                &receiver,
                READ_AFTER_COMMAND_COMMAND_ID,
                characteristic_id,
                timeout,
            )?;
            require_value(
                "write without response",
                &command_value,
                WITHOUT_RESPONSE_VALUE,
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
                "read/write check failed",
                io_result.map_err(|error| error.to_string()),
            ),
            (
                "disconnect cleanup failed",
                disconnect_result.map_err(|error| error.to_string()),
            ),
        ])
    })();
    let dispose_result = hub.dispose();

    receiver.finish(
        "Windows read/write smoke",
        aggregate_results([
            (
                "read/write smoke failed",
                smoke_result.map_err(|error| error.to_string()),
            ),
            (
                "Windows backend disposal failed",
                dispose_result.map_err(|error| error.to_string()),
            ),
        ]),
    )
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
                connection_id: CONNECTION_ID,
                gatt_session_id: GATT_SESSION_ID,
                characteristic_id: event_characteristic_id,
                value: event_value,
            } if event_command_id == command_id && event_characteristic_id == characteristic_id => {
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
            _ => {}
        }
    }
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
                connection_id: CONNECTION_ID,
                gatt_session_id: GATT_SESSION_ID,
                characteristic_id: event_characteristic_id,
            } if event_command_id == command_id && event_characteristic_id == characteristic_id => {
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
            _ => {}
        }
    }
}

fn require_value(operation: &str, actual: &[u8], expected: &[u8]) -> Result<(), Box<dyn Error>> {
    if actual != expected {
        return Err(format!(
            "{operation} readback mismatch: expected {expected:02X?}, got {actual:02X?}"
        )
        .into());
    }
    Ok(())
}
