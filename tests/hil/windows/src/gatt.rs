use std::error::Error;

use blehub::{
    CommandId, ConnectCommand, ConnectionId, ConnectionState, DisconnectCommand, GattSessionId,
    ScanSessionId,
};

mod support;

use support::{
    aggregate_results, create_hub, discover_gatt, initialize_hub, parse_timeout_and_service,
    scan_and_stop, validate_fixture_database, wait_for_connection_command,
};

const DEFAULT_FIXTURE_SERVICE: &str = "fff00000-0000-4000-8000-000000000000";
const INITIALIZE_COMMAND_ID: CommandId = CommandId { value: 1 };
const START_SCAN_COMMAND_ID: CommandId = CommandId { value: 2 };
const STOP_SCAN_COMMAND_ID: CommandId = CommandId { value: 3 };
const CONNECT_COMMAND_ID: CommandId = CommandId { value: 4 };
const DISCOVER_GATT_COMMAND_ID: CommandId = CommandId { value: 5 };
const DISCONNECT_COMMAND_ID: CommandId = CommandId { value: 6 };
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

        let discovery_result: Result<(), Box<dyn Error>> = (|| {
            let database = discover_gatt(
                &hub,
                &receiver,
                DISCOVER_GATT_COMMAND_ID,
                CONNECTION_ID,
                GATT_SESSION_ID,
                timeout,
            )?;
            validate_fixture_database(&database, &service_uuid, CONNECTION_ID, GATT_SESSION_ID)?;
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
                "GATT discovery check failed",
                discovery_result.map_err(|error| error.to_string()),
            ),
            (
                "disconnect cleanup failed",
                disconnect_result.map_err(|error| error.to_string()),
            ),
        ])
    })();
    let dispose_result = hub.dispose();

    receiver.finish(
        "Windows GATT smoke",
        aggregate_results([
            (
                "GATT smoke failed",
                smoke_result.map_err(|error| error.to_string()),
            ),
            (
                "Windows backend disposal failed",
                dispose_result.map_err(|error| error.to_string()),
            ),
        ]),
    )
}
