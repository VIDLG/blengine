use std::error::Error;

use blehub::{CommandId, ScanSessionId};

mod support;

use support::{
    aggregate_results, create_hub, initialize_hub, parse_timeout_and_service, scan_and_stop,
};

const DEFAULT_FIXTURE_SERVICE: &str = "fff00000-0000-4000-8000-000000000000";
const INITIALIZE_COMMAND_ID: CommandId = CommandId { value: 1 };
const START_SCAN_COMMAND_ID: CommandId = CommandId { value: 2 };
const STOP_SCAN_COMMAND_ID: CommandId = CommandId { value: 3 };
const SCAN_SESSION_ID: ScanSessionId = ScanSessionId { value: 1 };

fn main() -> Result<(), Box<dyn Error>> {
    let (timeout, service_uuid) = parse_timeout_and_service(DEFAULT_FIXTURE_SERVICE)?;
    let (hub, receiver) = create_hub()?;

    let scan_result: Result<(), Box<dyn Error>> = (|| {
        initialize_hub(&hub, &receiver, INITIALIZE_COMMAND_ID, timeout)?;
        scan_and_stop(
            &hub,
            &receiver,
            START_SCAN_COMMAND_ID,
            STOP_SCAN_COMMAND_ID,
            SCAN_SESSION_ID,
            service_uuid,
            timeout,
        )?;
        Ok(())
    })();
    let dispose_result = hub.dispose();

    receiver.finish(
        "Windows scan smoke",
        aggregate_results([
            (
                "scan smoke failed",
                scan_result.map_err(|error| error.to_string()),
            ),
            (
                "Windows backend disposal failed",
                dispose_result.map_err(|error| error.to_string()),
            ),
        ]),
    )
}
