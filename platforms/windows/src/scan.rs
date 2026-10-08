use std::sync::{Arc, Mutex};

use blehub::{
    BleErrorCategory, BleEvent, BleEventError, BleHubError, CommandDispatchError, CommandId,
    ScanOptions, ScanSessionId, ScanStopReason, ScanStopResult, StartScanCommand, StopScanCommand,
};
use windows::{
    Devices::Bluetooth::{
        Advertisement::{
            BluetoothLEAdvertisementReceivedEventArgs, BluetoothLEAdvertisementWatcher,
            BluetoothLEAdvertisementWatcherStoppedEventArgs,
        },
        BluetoothError,
    },
    Foundation::TypedEventHandler,
};

use crate::{
    backend::{
        BackendState, Lifecycle, WindowsBackend, ensure_ready, lock_delivery, lock_state,
        log_cleanup_failure, push_events, track_callback,
    },
    error::{
        bluetooth_event_error, cleanup_error, dispatch_error, event_error, simple_event_error,
    },
    mapping::map_advertisement_observation,
    resource_accounting::CallbackResource,
};

pub(crate) struct ScanState {
    generation: u64,
    start_command_id: CommandId,
    scan_session_id: ScanSessionId,
    options: ScanOptions,
    watcher: BluetoothLEAdvertisementWatcher,
    received_token: i64,
    stopped_token: i64,
    start_completed: bool,
    stop_command_id: Option<CommandId>,
}

pub(crate) fn begin(
    backend: &WindowsBackend,
    command: StartScanCommand,
) -> Result<(), CommandDispatchError> {
    let generation = {
        let mut state = lock_state(&backend.state);
        ensure_ready(&state)?;
        if state.scan.is_some() {
            return Err(CommandDispatchError::InvalidState {
                detail: "a Windows scan is already active".to_owned(),
            });
        }
        let generation = state.next_scan_generation;
        state.next_scan_generation =
            state.next_scan_generation.checked_add(1).ok_or_else(|| {
                CommandDispatchError::BackendUnavailable {
                    detail: "Windows scan generation space is exhausted".to_owned(),
                    platform_details: None,
                }
            })?;
        generation
    };

    let watcher = BluetoothLEAdvertisementWatcher::new()
        .map_err(|error| dispatch_error("create advertisement watcher", error))?;

    let received_state = Arc::downgrade(&backend.state);
    let received_delivery = Arc::clone(&backend.delivery);
    let received_resource = track_callback(&backend.state, CallbackResource::AdvertisementReceived);
    let received_handler = TypedEventHandler::<
        BluetoothLEAdvertisementWatcher,
        BluetoothLEAdvertisementReceivedEventArgs,
    >::new(move |_sender, args| {
        let _received_resource = &received_resource;
        if let (Some(state), Some(args)) = (received_state.upgrade(), args.as_ref()) {
            handle_advertisement_received(&state, &received_delivery, generation, args);
        }
        Ok(())
    });
    let received_token = watcher
        .Received(&received_handler)
        .map_err(|error| dispatch_error("register advertisement callback", error))?;

    let stopped_state = Arc::downgrade(&backend.state);
    let stopped_delivery = Arc::clone(&backend.delivery);
    let stopped_resource = track_callback(&backend.state, CallbackResource::AdvertisementStopped);
    let stopped_handler = TypedEventHandler::<
        BluetoothLEAdvertisementWatcher,
        BluetoothLEAdvertisementWatcherStoppedEventArgs,
    >::new(move |_sender, args| {
        let _stopped_resource = &stopped_resource;
        if let (Some(state), Some(args)) = (stopped_state.upgrade(), args.as_ref()) {
            handle_watcher_stopped(&state, &stopped_delivery, generation, args);
        }
        Ok(())
    });
    let stopped_token = match watcher.Stopped(&stopped_handler) {
        Ok(token) => token,
        Err(error) => {
            let _ = watcher.RemoveReceived(received_token);
            return Err(dispatch_error("register scan-stopped callback", error));
        }
    };

    {
        let mut state = lock_state(&backend.state);
        if let Err(error) = ensure_ready(&state) {
            drop(state);
            let _ = watcher.RemoveReceived(received_token);
            let _ = watcher.RemoveStopped(stopped_token);
            return Err(error);
        }
        if state.scan.is_some() {
            drop(state);
            let _ = watcher.RemoveReceived(received_token);
            let _ = watcher.RemoveStopped(stopped_token);
            return Err(CommandDispatchError::InvalidState {
                detail: "a Windows scan started concurrently".to_owned(),
            });
        }
        state.scan = Some(ScanState {
            generation,
            start_command_id: command.command_id,
            scan_session_id: command.scan_session_id,
            options: command.options,
            watcher: watcher.clone(),
            received_token,
            stopped_token,
            start_completed: false,
            stop_command_id: None,
        });
    }

    if let Err(error) = watcher.Start() {
        finish_start_failure(
            &backend.state,
            &backend.delivery,
            generation,
            event_error(
                BleErrorCategory::Backend,
                "start advertisement watcher",
                error,
            ),
        );
        return Ok(());
    }

    let delivery_guard = lock_delivery(&backend.delivery);
    let (delivery, deferred_stop) = {
        let mut state = lock_state(&backend.state);
        let ingress = state.ingress();
        let Some(scan) = state
            .scan
            .as_mut()
            .filter(|scan| scan.generation == generation)
        else {
            return Ok(());
        };
        scan.start_completed = true;
        let deferred_stop = scan
            .stop_command_id
            .map(|command_id| (scan.watcher.clone(), scan.scan_session_id, command_id));
        let delivery = ingress.map(|ingress| {
            (
                ingress,
                vec![
                    BleEvent::ScanStarted {
                        command_id: scan.start_command_id,
                        scan_id: scan.scan_session_id,
                    },
                    BleEvent::CommandCompleted {
                        command_id: scan.start_command_id,
                        error: None,
                    },
                ],
            )
        });
        (delivery, deferred_stop)
    };
    if let Some((ingress, events)) = delivery {
        push_events(&ingress, events, "complete Windows scan start");
    }
    drop(delivery_guard);
    if let Some((watcher, scan_session_id, command_id)) = deferred_stop {
        invoke_scan_stop(
            &backend.state,
            &backend.delivery,
            watcher,
            scan_session_id,
            command_id,
        );
    }
    Ok(())
}

pub(crate) fn stop(
    backend: &WindowsBackend,
    command: StopScanCommand,
) -> Result<(), CommandDispatchError> {
    let delivery_guard = lock_delivery(&backend.delivery);
    let (watcher, deferred, duplicate_delivery) = {
        let mut state = lock_state(&backend.state);
        if state.lifecycle == Lifecycle::Closed {
            return Err(CommandDispatchError::Closed);
        }
        if state.lifecycle != Lifecycle::Ready {
            return Err(CommandDispatchError::InvalidState {
                detail: "Windows backend is not initialized".to_owned(),
            });
        }
        if state.scan.is_none() {
            if state.last_stopped_scan == Some(command.scan_session_id) {
                let delivery = state.ingress().map(|ingress| {
                    (
                        ingress,
                        BleEvent::CommandCompleted {
                            command_id: command.command_id,
                            error: None,
                        },
                    )
                });
                (None, false, delivery)
            } else {
                return Err(CommandDispatchError::InvalidState {
                    detail: "the requested Windows scan session is not active".to_owned(),
                });
            }
        } else {
            let Some(scan) = state.scan.as_mut() else {
                return Err(CommandDispatchError::BackendUnavailable {
                    detail: "Windows scan state disappeared during stop dispatch".to_owned(),
                    platform_details: None,
                });
            };
            if scan.scan_session_id != command.scan_session_id {
                return Err(CommandDispatchError::InvalidState {
                    detail: "the requested Windows scan session is not active".to_owned(),
                });
            }
            if scan.stop_command_id.is_some() {
                return Err(CommandDispatchError::InvalidState {
                    detail: "a stop command is already pending for this Windows scan".to_owned(),
                });
            }
            scan.stop_command_id = Some(command.command_id);
            if scan.start_completed {
                (Some(scan.watcher.clone()), false, None)
            } else {
                (None, true, None)
            }
        }
    };

    if let Some((ingress, event)) = duplicate_delivery {
        push_events(
            &ingress,
            vec![event],
            "complete duplicate Windows scan stop",
        );
        return Ok(());
    }

    drop(delivery_guard);
    if deferred {
        return Ok(());
    }
    let Some(watcher) = watcher else {
        return Err(CommandDispatchError::BackendUnavailable {
            detail: "Windows scan watcher disappeared during stop dispatch".to_owned(),
            platform_details: None,
        });
    };
    invoke_scan_stop(
        &backend.state,
        &backend.delivery,
        watcher,
        command.scan_session_id,
        command.command_id,
    );
    Ok(())
}

fn invoke_scan_stop(
    state: &Arc<Mutex<BackendState>>,
    delivery: &Arc<Mutex<()>>,
    watcher: BluetoothLEAdvertisementWatcher,
    scan_session_id: ScanSessionId,
    command_id: CommandId,
) {
    if let Err(error) = watcher.Stop() {
        let _delivery_guard = lock_delivery(delivery);
        let failure = event_error(
            BleErrorCategory::Backend,
            "stop advertisement watcher",
            error,
        );
        let (ingress, event) = {
            let mut state = lock_state(state);
            let Some(scan) = state.scan.as_mut() else {
                return;
            };
            if scan.scan_session_id != scan_session_id || scan.stop_command_id != Some(command_id) {
                return;
            }
            let Some(command_id) = scan.stop_command_id.take() else {
                return;
            };
            let Some(ingress) = state.ingress() else {
                return;
            };
            (
                ingress,
                BleEvent::CommandCompleted {
                    command_id,
                    error: Some(failure),
                },
            )
        };
        push_events(&ingress, vec![event], "fail Windows scan stop");
    }
}

pub(crate) fn cleanup_for_dispose(scan: ScanState, first_error: &mut Option<BleHubError>) {
    if let Err(error) = scan.watcher.RemoveReceived(scan.received_token) {
        first_error.get_or_insert_with(|| cleanup_error("remove advertisement callback", error));
    }
    if let Err(error) = scan.watcher.RemoveStopped(scan.stopped_token) {
        first_error.get_or_insert_with(|| cleanup_error("remove scan-stopped callback", error));
    }
    if let Err(error) = scan.watcher.Stop() {
        first_error.get_or_insert_with(|| cleanup_error("stop advertisement watcher", error));
    }
}

fn handle_advertisement_received(
    state: &Arc<Mutex<BackendState>>,
    delivery: &Arc<Mutex<()>>,
    generation: u64,
    args: &BluetoothLEAdvertisementReceivedEventArgs,
) {
    let address = match args.BluetoothAddress() {
        Ok(address) => address,
        Err(error) => {
            tracing::warn!(
                target: "blehub_windows::scan",
                detail = %error,
                "dropped Windows advertisement without an address"
            );
            return;
        }
    };
    let address_type = match args.BluetoothAddressType() {
        Ok(address_type) => address_type,
        Err(error) => {
            tracing::warn!(
                target: "blehub_windows::scan",
                detail = %error,
                "dropped Windows advertisement without an address type"
            );
            return;
        }
    };
    let (scan_session_id, options, peripheral_id) = {
        let mut current = lock_state(state);
        let Some(scan) = current
            .scan
            .as_ref()
            .filter(|scan| scan.generation == generation && scan.start_completed)
        else {
            return;
        };
        let scan_session_id = scan.scan_session_id;
        let options = scan.options.clone();
        let Some(peripheral_id) = current.allocate_scan_peripheral_id(address, address_type.0)
        else {
            tracing::error!(
                target: "blehub_windows::scan",
                "peripheral ID space is exhausted"
            );
            return;
        };
        (scan_session_id, options, peripheral_id)
    };
    let observation = match map_advertisement_observation(args, scan_session_id, peripheral_id) {
        Ok(observation) => observation,
        Err(error) => {
            tracing::warn!(
                target: "blehub_windows::scan",
                detail = %error,
                "dropped an advertisement that could not be mapped"
            );
            return;
        }
    };
    if !options.matches(&observation) {
        return;
    }
    let _delivery_guard = lock_delivery(delivery);
    let ingress = {
        let current = lock_state(state);
        if !current
            .scan
            .as_ref()
            .is_some_and(|scan| scan.generation == generation && scan.start_completed)
        {
            return;
        }
        current.ingress()
    };
    if let Some(ingress) = ingress {
        push_events(
            &ingress,
            vec![BleEvent::AdvertisementObserved {
                advertisement: observation,
            }],
            "deliver Windows advertisement",
        );
    }
}

fn handle_watcher_stopped(
    state: &Arc<Mutex<BackendState>>,
    delivery: &Arc<Mutex<()>>,
    generation: u64,
    args: &BluetoothLEAdvertisementWatcherStoppedEventArgs,
) {
    let native_error = match args.Error() {
        Ok(error) => error,
        Err(error) => {
            tracing::warn!(
                target: "blehub_windows::scan",
                detail = %error,
                "failed to read Windows watcher stopped error"
            );
            BluetoothError::OtherError
        }
    };
    let _delivery_guard = lock_delivery(delivery);
    let (scan, ingress) = {
        let mut current = lock_state(state);
        let Some(scan) = current.scan.take() else {
            return;
        };
        if scan.generation != generation {
            current.scan = Some(scan);
            return;
        }
        current.last_stopped_scan = Some(scan.scan_session_id);
        (scan, current.ingress())
    };
    let events = watcher_stopped_events(
        scan.start_completed,
        scan.start_command_id,
        scan.scan_session_id,
        scan.stop_command_id,
        native_error,
    );
    if let Some(ingress) = ingress {
        push_events(&ingress, events, "complete Windows scan stop");
    }
    drop(_delivery_guard);
    log_cleanup_failure(
        scan.watcher.RemoveReceived(scan.received_token),
        "remove advertisement callback after scan stop",
    );
    log_cleanup_failure(
        scan.watcher.RemoveStopped(scan.stopped_token),
        "remove stopped callback after scan stop",
    );
}

fn watcher_stopped_events(
    start_completed: bool,
    start_command_id: CommandId,
    scan_session_id: ScanSessionId,
    stop_command_id: Option<CommandId>,
    native_error: BluetoothError,
) -> Vec<BleEvent> {
    if !start_completed {
        let error = if native_error == BluetoothError::Success {
            simple_event_error(
                BleErrorCategory::Backend,
                "advertisement watcher stopped before scan start completed",
            )
        } else {
            bluetooth_event_error("start advertisement watcher", native_error)
        };
        let mut events = vec![BleEvent::CommandCompleted {
            command_id: start_command_id,
            error: Some(error),
        }];
        if let Some(command_id) = stop_command_id {
            let error = (native_error != BluetoothError::Success)
                .then(|| bluetooth_event_error("stop advertisement watcher", native_error));
            events.push(BleEvent::CommandCompleted { command_id, error });
        }
        return events;
    }

    let result = if native_error == BluetoothError::Success {
        ScanStopResult::Ok {
            reason: stop_command_id.map_or(ScanStopReason::SystemSuspended, |command_id| {
                ScanStopReason::Requested { command_id }
            }),
        }
    } else {
        ScanStopResult::Err {
            error: bluetooth_event_error("advertisement watcher stopped", native_error),
        }
    };
    let mut events = vec![BleEvent::ScanStopped {
        scan_id: scan_session_id,
        result,
    }];
    if let Some(command_id) = stop_command_id {
        let error = (native_error != BluetoothError::Success)
            .then(|| bluetooth_event_error("stop advertisement watcher", native_error));
        events.push(BleEvent::CommandCompleted { command_id, error });
    }
    events
}

fn scan_start_failure_events(
    start_command_id: CommandId,
    stop_command_id: Option<CommandId>,
    error: BleEventError,
) -> Vec<BleEvent> {
    let mut events = vec![BleEvent::CommandCompleted {
        command_id: start_command_id,
        error: Some(error),
    }];
    if let Some(command_id) = stop_command_id {
        events.push(BleEvent::CommandCompleted {
            command_id,
            error: None,
        });
    }
    events
}

fn finish_start_failure(
    state: &Arc<Mutex<BackendState>>,
    delivery: &Arc<Mutex<()>>,
    generation: u64,
    error: BleEventError,
) {
    let delivery_guard = lock_delivery(delivery);
    let (scan, ingress) = {
        let mut current = lock_state(state);
        let Some(scan) = current.scan.take() else {
            return;
        };
        if scan.generation != generation {
            current.scan = Some(scan);
            return;
        }
        current.last_stopped_scan = Some(scan.scan_session_id);
        (scan, current.ingress())
    };
    if let Some(ingress) = ingress {
        push_events(
            &ingress,
            scan_start_failure_events(scan.start_command_id, scan.stop_command_id, error),
            "fail Windows scan start",
        );
    }
    drop(delivery_guard);
    log_cleanup_failure(
        scan.watcher.RemoveReceived(scan.received_token),
        "remove advertisement callback after scan-start failure",
    );
    log_cleanup_failure(
        scan.watcher.RemoveStopped(scan.stopped_token),
        "remove stopped callback after scan-start failure",
    );
    log_cleanup_failure(
        scan.watcher.Stop(),
        "stop advertisement watcher after scan-start failure",
    );
}

#[cfg(test)]
mod tests {
    use super::*;

    fn command_id(value: u64) -> CommandId {
        CommandId { value }
    }

    fn scan_session_id(value: u64) -> ScanSessionId {
        ScanSessionId { value }
    }

    #[test]
    fn deferred_stop_gets_a_terminal_when_scan_start_fails() {
        let start_command_id = command_id(1);
        let stop_command_id = command_id(2);
        let error = simple_event_error(BleErrorCategory::Backend, "scripted start failure");

        assert_eq!(
            scan_start_failure_events(start_command_id, Some(stop_command_id), error.clone()),
            vec![
                BleEvent::CommandCompleted {
                    command_id: start_command_id,
                    error: Some(error),
                },
                BleEvent::CommandCompleted {
                    command_id: stop_command_id,
                    error: None,
                },
            ]
        );
    }

    #[test]
    fn deferred_stop_uses_requested_reason_after_scan_start_completes() {
        let start_command_id = command_id(1);
        let stop_command_id = command_id(2);
        let scan_session_id = scan_session_id(3);

        assert_eq!(
            watcher_stopped_events(
                true,
                start_command_id,
                scan_session_id,
                Some(stop_command_id),
                BluetoothError::Success,
            ),
            vec![
                BleEvent::ScanStopped {
                    scan_id: scan_session_id,
                    result: ScanStopResult::Ok {
                        reason: ScanStopReason::Requested {
                            command_id: stop_command_id,
                        },
                    },
                },
                BleEvent::CommandCompleted {
                    command_id: stop_command_id,
                    error: None,
                },
            ]
        );
    }

    #[test]
    fn deferred_stop_completes_if_watcher_stops_before_scan_start_completion() {
        let start_command_id = command_id(1);
        let stop_command_id = command_id(2);

        let events = watcher_stopped_events(
            false,
            start_command_id,
            scan_session_id(3),
            Some(stop_command_id),
            BluetoothError::Success,
        );

        assert_eq!(events.len(), 2);
        assert!(matches!(
            &events[0],
            BleEvent::CommandCompleted {
                command_id,
                error: Some(_),
            } if *command_id == start_command_id
        ));
        assert_eq!(
            events[1],
            BleEvent::CommandCompleted {
                command_id: stop_command_id,
                error: None,
            }
        );
    }
}
