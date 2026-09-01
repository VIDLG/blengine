use std::{
    error::Error,
    time::{Duration, Instant},
};

use blehub::{CommandId, ConnectCommand, ConnectionId, ConnectionState, ScanSessionId};

mod support;

use support::{
    aggregate_results, create_accounted_hub, initialize_hub, parse_cycle_count,
    parse_timeout_and_service, report_cycle_progress,
    resource::{
        process_resource_snapshot, require_connected_resource_graph, wait_for_resource_release,
    },
    scan_and_stop, wait_for_connection_command,
};

const DEFAULT_FIXTURE_SERVICE: &str = "ffe00000-0000-4000-8000-000000000000";
const DEFAULT_CYCLES: usize = 10;
const CONNECTION_ID: ConnectionId = ConnectionId { value: 1 };

fn main() -> Result<(), Box<dyn Error>> {
    let (timeout, service_uuid) = parse_timeout_and_service(DEFAULT_FIXTURE_SERVICE)?;
    let cycles = parse_cycle_count(DEFAULT_CYCLES)?;
    let started = Instant::now();
    let process_before = process_resource_snapshot()?;
    let mut process_samples = Vec::with_capacity(cycles);

    for cycle in 1..=cycles {
        let command_base = u64::try_from(cycle)? * 100;
        let initialize_id = command_id(command_base + 1);
        let scan_id = command_id(command_base + 2);
        let stop_id = command_id(command_base + 3);
        let connect_id = command_id(command_base + 4);
        let scan_session_id = ScanSessionId { value: 1 };
        let (hub, backend, probe, receiver) = create_accounted_hub()?;

        let scenario_result: Result<(), Box<dyn Error>> = (|| {
            initialize_hub(&hub, &receiver, initialize_id, timeout)?;
            let observation = scan_and_stop(
                &hub,
                &receiver,
                scan_id,
                stop_id,
                scan_session_id,
                service_uuid.clone(),
                timeout,
            )?;
            hub.connect(ConnectCommand {
                command_id: connect_id,
                connection_id: CONNECTION_ID,
                peripheral_id: observation.peripheral_id,
            })?;
            wait_for_connection_command(
                &receiver,
                connect_id,
                CONNECTION_ID,
                ConnectionState::Connected,
                timeout,
            )?;
            require_connected_resource_graph(&backend, &probe)
        })();

        let first_dispose = hub.dispose();
        let first_release = wait_for_resource_release(&backend, &probe, Duration::from_secs(5));
        let second_dispose = hub.dispose();
        let second_release = wait_for_resource_release(&backend, &probe, Duration::from_secs(1));
        receiver.finish_quiet(
            &format!("Windows scan/connect soak cycle {cycle}/{cycles}"),
            aggregate_results([
                (
                    "scan/connect setup failed",
                    scenario_result.map_err(|error| error.to_string()),
                ),
                (
                    "connected-state disposal failed",
                    first_dispose.map_err(|error| error.to_string()),
                ),
                (
                    "resources remained after disposal",
                    first_release.map_err(|error| error.to_string()),
                ),
                (
                    "idempotent disposal failed",
                    second_dispose.map_err(|error| error.to_string()),
                ),
                (
                    "resources reappeared after idempotent disposal",
                    second_release.map_err(|error| error.to_string()),
                ),
            ]),
        )?;
        process_samples.push(process_resource_snapshot()?);
        report_cycle_progress("Windows scan/connect soak", cycle, cycles, started)?;
    }

    let process_cycle_one = process_samples.first().copied().unwrap_or(process_before);
    let settled_index = process_samples.len().saturating_sub(1).min(2);
    let process_settled = process_samples
        .get(settled_index)
        .copied()
        .unwrap_or(process_cycle_one);
    let process_after = process_samples.last().copied().unwrap_or(process_before);
    let settled_maximum_handles = process_samples[settled_index..]
        .iter()
        .map(|sample| sample.handle_count)
        .max()
        .unwrap_or(process_settled.handle_count);
    println!(
        "Windows scan/connect soak: PASS ({cycles}/{cycles} cycles); process handles cold {} -> cycle1 {} -> settled {} -> final {} (settled max {}), private bytes cold {} -> cycle1 {} -> settled {} -> final {}, working-set bytes cold {} -> cycle1 {} -> settled {} -> final {}",
        process_before.handle_count,
        process_cycle_one.handle_count,
        process_settled.handle_count,
        process_after.handle_count,
        settled_maximum_handles,
        process_before.private_bytes,
        process_cycle_one.private_bytes,
        process_settled.private_bytes,
        process_after.private_bytes,
        process_before.working_set_bytes,
        process_cycle_one.working_set_bytes,
        process_settled.working_set_bytes,
        process_after.working_set_bytes,
    );
    Ok(())
}

fn command_id(value: u64) -> CommandId {
    CommandId { value }
}
