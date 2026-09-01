use std::{
    mem::size_of,
    sync::Arc,
    thread,
    time::{Duration, Instant},
};

use blehub_windows::{
    WindowsBackend, WindowsCallbackResourceSnapshot, WindowsLogicalResourceSnapshot,
    WindowsResourceProbe,
};
use windows::Win32::System::{
    ProcessStatus::{K32GetProcessMemoryInfo, PROCESS_MEMORY_COUNTERS},
    Threading::{GetCurrentProcess, GetProcessHandleCount},
};

use super::ExampleError;

const RELEASE_POLL_INTERVAL: Duration = Duration::from_millis(10);

#[derive(Clone, Copy, Debug)]
pub struct ProcessResourceSnapshot {
    pub handle_count: u32,
    pub working_set_bytes: usize,
    pub private_bytes: usize,
}

pub fn process_resource_snapshot() -> Result<ProcessResourceSnapshot, ExampleError> {
    let process = unsafe { GetCurrentProcess() };
    let mut handle_count = 0;
    unsafe { GetProcessHandleCount(process, &mut handle_count) }?;

    let mut memory = PROCESS_MEMORY_COUNTERS {
        cb: u32::try_from(size_of::<PROCESS_MEMORY_COUNTERS>())?,
        ..Default::default()
    };
    unsafe {
        K32GetProcessMemoryInfo(
            process,
            &mut memory,
            u32::try_from(size_of::<PROCESS_MEMORY_COUNTERS>())?,
        )
    }
    .ok()?;

    Ok(ProcessResourceSnapshot {
        handle_count,
        working_set_bytes: memory.WorkingSetSize,
        private_bytes: memory.PagefileUsage,
    })
}

pub fn require_full_active_resource_graph(
    backend: &Arc<WindowsBackend>,
    probe: &WindowsResourceProbe,
) -> Result<(), ExampleError> {
    let logical = backend.resource_snapshot();
    let callbacks = probe.snapshot();
    require_active_logical_graph(logical, true)?;

    let missing = [
        ("Radio.StateChanged", callbacks.radio_state_changed),
        ("watcher.Received", callbacks.advertisement_received),
        ("watcher.Stopped", callbacks.advertisement_stopped),
        (
            "device.ConnectionStatusChanged",
            callbacks.connection_status_changed,
        ),
        (
            "device.GattServicesChanged",
            callbacks.gatt_services_changed,
        ),
        (
            "characteristic.ValueChanged",
            callbacks.characteristic_value_changed,
        ),
    ]
    .into_iter()
    .filter_map(|(name, count)| (count == 0).then_some(name))
    .collect::<Vec<_>>();
    if missing.is_empty() {
        Ok(())
    } else {
        Err(format!(
            "active resource graph is missing callback delegates: {}; snapshot={callbacks:?}",
            missing.join(", ")
        )
        .into())
    }
}

pub fn require_connected_resource_graph(
    backend: &Arc<WindowsBackend>,
    probe: &WindowsResourceProbe,
) -> Result<(), ExampleError> {
    let logical = backend.resource_snapshot();
    let callbacks = probe.snapshot();
    require_active_logical_graph(logical, false)?;

    let missing = [
        ("Radio.StateChanged", callbacks.radio_state_changed),
        (
            "device.ConnectionStatusChanged",
            callbacks.connection_status_changed,
        ),
        (
            "device.GattServicesChanged",
            callbacks.gatt_services_changed,
        ),
    ]
    .into_iter()
    .filter_map(|(name, count)| (count == 0).then_some(name))
    .collect::<Vec<_>>();
    if missing.is_empty() {
        Ok(())
    } else {
        Err(format!(
            "connected resource graph is missing callback delegates: {}; snapshot={callbacks:?}",
            missing.join(", ")
        )
        .into())
    }
}

pub fn wait_for_resource_release(
    backend: &Arc<WindowsBackend>,
    probe: &WindowsResourceProbe,
    timeout: Duration,
) -> Result<(), ExampleError> {
    let deadline = Instant::now() + timeout;
    loop {
        let logical = backend.resource_snapshot();
        let callbacks = probe.snapshot();
        if logical.is_zero() && callbacks.is_zero() {
            return Ok(());
        }
        if Instant::now() >= deadline {
            return Err(format!(
                "Windows resources did not return to zero within {timeout:?}; logical={logical:?}; callbacks={callbacks:?}"
            )
            .into());
        }
        thread::sleep(
            RELEASE_POLL_INTERVAL.min(deadline.saturating_duration_since(Instant::now())),
        );
    }
}

fn require_active_logical_graph(
    logical: WindowsLogicalResourceSnapshot,
    require_scan: bool,
) -> Result<(), ExampleError> {
    let valid = logical.ingress_roots == 1
        && logical.adapter_roots == 1
        && logical.radio_roots == 1
        && logical.radio_event_tokens == 1
        && logical.connection_roots == 1
        && logical.owns_adapter_lease
        && (!require_scan || logical.scan_roots == 1);
    if valid {
        Ok(())
    } else {
        Err(format!("active logical resource graph is incomplete: {logical:?}").into())
    }
}

#[allow(dead_code)]
fn _assert_snapshot_traits(_: WindowsCallbackResourceSnapshot, _: WindowsLogicalResourceSnapshot) {}
