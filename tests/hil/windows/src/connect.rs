use std::{
    error::Error,
    time::{Duration, Instant},
};

use blehub::{
    BleErrorCategory, BleEvent, CommandId, ConnectCommand, ConnectionId, ConnectionState,
    DisconnectCommand, ReadRssiCommand, ScanSessionId,
};

mod support;

use support::{
    EventReceiver, aggregate_results, create_hub, initialize_hub, parse_timeout_and_service,
    receive, scan_and_stop, wait_for_connection_command,
};

const DEFAULT_FIXTURE_SERVICE: &str = "fff00000-0000-4000-8000-000000000000";
const INITIALIZE_COMMAND_ID: CommandId = CommandId { value: 1 };
const START_SCAN_COMMAND_ID: CommandId = CommandId { value: 2 };
const STOP_SCAN_COMMAND_ID: CommandId = CommandId { value: 3 };
const CONNECT_COMMAND_ID: CommandId = CommandId { value: 4 };
const READ_RSSI_COMMAND_ID: CommandId = CommandId { value: 5 };
const DISCONNECT_COMMAND_ID: CommandId = CommandId { value: 6 };
const SCAN_SESSION_ID: ScanSessionId = ScanSessionId { value: 1 };
const CONNECTION_ID: ConnectionId = ConnectionId { value: 1 };

fn main() -> Result<(), Box<dyn Error>> {
    let (timeout, service_uuid) = parse_timeout_and_service(DEFAULT_FIXTURE_SERVICE)?;
    let (hub, receiver) = create_hub()?;

    let connect_result: Result<(), Box<dyn Error>> = (|| {
        initialize_hub(&hub, &receiver, INITIALIZE_COMMAND_ID, timeout)?;
        let observation = scan_and_stop(
            &hub,
            &receiver,
            START_SCAN_COMMAND_ID,
            STOP_SCAN_COMMAND_ID,
            SCAN_SESSION_ID,
            service_uuid,
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

        hub.read_rssi(ReadRssiCommand {
            command_id: READ_RSSI_COMMAND_ID,
            connection_id: CONNECTION_ID,
        })?;
        wait_for_unsupported_rssi(&receiver, timeout)?;

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
        )?;
        Ok(())
    })();
    let dispose_result = hub.dispose();

    receiver.finish(
        "Windows connect smoke",
        aggregate_results([
            (
                "connect smoke failed",
                connect_result.map_err(|error| error.to_string()),
            ),
            (
                "Windows backend disposal failed",
                dispose_result.map_err(|error| error.to_string()),
            ),
        ]),
    )
}

fn wait_for_unsupported_rssi(
    receiver: &EventReceiver,
    timeout: Duration,
) -> Result<(), Box<dyn Error>> {
    let deadline = Instant::now() + timeout;
    loop {
        match receive(receiver, deadline)? {
            BleEvent::CommandCompleted {
                command_id: READ_RSSI_COMMAND_ID,
                error: Some(error),
            } if error.category == BleErrorCategory::NotSupported => return Ok(()),
            BleEvent::CommandCompleted {
                command_id: READ_RSSI_COMMAND_ID,
                error,
            } => return Err(format!("unexpected Windows RSSI completion: {error:?}").into()),
            BleEvent::RssiRead {
                command_id: READ_RSSI_COMMAND_ID,
                ..
            } => {
                return Err(
                    "Windows emitted a connected RSSI value despite false capability".into(),
                );
            }
            _ => {}
        }
    }
}
