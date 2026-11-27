use std::{
    future::Future,
    mem::size_of,
    pin::{Pin, pin},
    task::{Context, Poll, Waker},
};

use blehub::{
    AdvertisementObservation, BleErrorCategory, BleEventError, BluetoothUuid, ManufacturerData,
    Platform, PlatformErrorDetails, ScanStopReason, ScanStopResult, ServiceData,
};

use super::{
    MAX_SCAN_ADVERTISEMENTS, MAX_SCAN_DATA_BYTES, MAX_SCAN_STOP_DATA_BYTES, ScanDelivery,
    ScanDeliveryError,
};
use crate::{
    recovery::{OwnerBackendIdentity, OwnerGeneration, RecoveryAttemptId, RecoveryContext},
    scan::ScanEvent,
};

const MANUFACTURER_ENTRY_BYTES: usize = size_of::<ManufacturerData>();

fn context_with(owner: &str, backend: &str, generation: u64, attempt: u64) -> RecoveryContext {
    RecoveryContext::new(
        OwnerBackendIdentity::new(owner, backend).unwrap(),
        OwnerGeneration::new(generation).unwrap(),
        RecoveryAttemptId::new(attempt).unwrap(),
    )
}

fn context() -> RecoveryContext {
    context_with("owner-1", "backend-1", 1, 1)
}

fn delivery() -> ScanDelivery {
    ScanDelivery::new(context(), 41.into())
}

fn started(command_id: u64, scan_id: u64) -> ScanEvent {
    ScanEvent::Started {
        command_id: command_id.into(),
        scan_id: scan_id.into(),
    }
}

fn advertisement(scan_id: u64, marker: u8, bytes: usize) -> ScanEvent {
    ScanEvent::Advertisement {
        advertisement: AdvertisementObservation {
            scan_session_id: scan_id.into(),
            peripheral_id: 17.into(),
            local_name: None,
            advertised_services: vec![],
            manufacturer_data: vec![ManufacturerData {
                company_id: 0x004c,
                payload: vec![marker; bytes],
            }],
            service_data: vec![],
            rssi: Some(-127),
            connectable: Some(false),
        },
    }
}

fn rich_advertisement(scan_id: u64) -> ScanEvent {
    let heart_rate = BluetoothUuid::canonicalize("180d").unwrap();
    let custom = BluetoothUuid::canonicalize("12345678-1234-5678-9abc-def012345678").unwrap();
    ScanEvent::Advertisement {
        advertisement: AdvertisementObservation {
            scan_session_id: scan_id.into(),
            peripheral_id: 17.into(),
            local_name: Some("é🦀\0".to_owned()),
            advertised_services: vec![heart_rate.clone(), custom.clone()],
            manufacturer_data: vec![
                ManufacturerData {
                    company_id: 0x004c,
                    payload: vec![0, 255, 128],
                },
                ManufacturerData {
                    company_id: u16::MAX,
                    payload: vec![7, 0],
                },
            ],
            service_data: vec![
                ServiceData {
                    service_uuid: custom,
                    payload: vec![0, 255, 128, 1],
                },
                ServiceData {
                    service_uuid: heart_rate,
                    payload: vec![9],
                },
            ],
            rssi: Some(-127),
            connectable: Some(false),
        },
    }
}

fn stopped(scan_id: u64) -> ScanEvent {
    ScanEvent::Stopped {
        scan_id: scan_id.into(),
        result: ScanStopResult::Ok {
            reason: ScanStopReason::Requested {
                command_id: 42.into(),
            },
        },
    }
}

fn failed_stop(scan_id: u64) -> ScanEvent {
    ScanEvent::Stopped {
        scan_id: scan_id.into(),
        result: ScanStopResult::Err {
            error: BleEventError {
                category: BleErrorCategory::AdapterUnavailable,
                message: "radio lost\0detail".to_owned(),
                platform_details: None,
            },
        },
    }
}

fn stop_with_error_data(
    scan_id: u64,
    message: String,
    platform_details: Option<Box<PlatformErrorDetails>>,
) -> ScanEvent {
    ScanEvent::Stopped {
        scan_id: scan_id.into(),
        result: ScanStopResult::Err {
            error: BleEventError {
                category: BleErrorCategory::AdapterUnavailable,
                message,
                platform_details,
            },
        },
    }
}

fn poll_once<F: Future>(future: Pin<&mut F>) -> Poll<F::Output> {
    future.poll(&mut Context::from_waker(Waker::noop()))
}

fn next_ready(delivery: &ScanDelivery) -> Result<ScanEvent, ScanDeliveryError> {
    let mut next = pin!(delivery.next_event());
    match poll_once(next.as_mut()) {
        Poll::Ready(result) => result,
        Poll::Pending => panic!("expected an immediately available scan event or error"),
    }
}

fn assert_buffer(delivery: &ScanDelivery, advertisements: usize, bytes: usize) {
    let snapshot = delivery.snapshot();
    assert_eq!(snapshot.queued_advertisements, advertisements);
    assert_eq!(snapshot.queued_bytes, bytes);
}

fn assert_latched_failure(delivery: &ScanDelivery, error: ScanDeliveryError) {
    assert_buffer(delivery, 0, 0);
    assert_eq!(delivery.snapshot().queued_stop_bytes, 0);
    assert_eq!(next_ready(delivery).as_ref(), Err(&error));
    bleng::OperationFailureObserver::operation_failed(delivery);
    assert_eq!(next_ready(delivery).as_ref(), Err(&error));
    delivery.detach();
    assert_eq!(next_ready(delivery).as_ref(), Err(&error));
    delivery.offer(&context(), &stopped(7));
    delivery.offer(&context(), &started(41, 7));
    delivery.offer(&context(), &advertisement(7, 99, 1));
    delivery.close();
    delivery.detach();
    assert_buffer(delivery, 0, 0);
    assert_eq!(delivery.snapshot().queued_stop_bytes, 0);
    assert_eq!(next_ready(delivery).as_ref(), Err(&error));
    assert_eq!(next_ready(delivery).as_ref(), Err(&error));
}

#[test]
fn sole_reader_contends_after_submission_before_delivery_installation() {
    use std::{sync::Arc, sync::mpsc, thread, time::Duration};

    use blehub::{
        AdapterSnapshot, AdapterState, AuthorizationState, BleEvent, InitializeCommand,
        ScanOptions, StartScanCommand, StopScanCommand,
    };
    use blehub_testkit::{EventScenario, FakeBackend, RecordedCommand};
    use bleng::Bleng;

    use crate::observation::{ObservationProjectionLease, ScanInstallPause};

    let ScanEvent::Advertisement { advertisement } = rich_advertisement(1) else {
        unreachable!()
    };
    let terminal = BleEvent::ScanStopped {
        scan_id: 1.into(),
        result: ScanStopResult::Ok {
            reason: ScanStopReason::Requested {
                command_id: 3.into(),
            },
        },
    };
    let completed = |command: u64| BleEvent::CommandCompleted {
        command_id: command.into(),
        error: None,
    };
    let (backend, backend_handle) = FakeBackend::scripted([
        EventScenario::emit_before_return(
            RecordedCommand::Initialize(InitializeCommand {
                command_id: 1.into(),
            }),
            vec![
                BleEvent::AdapterSnapshotChanged {
                    command_id: Some(1.into()),
                    snapshot: AdapterSnapshot {
                        state: AdapterState::PoweredOn,
                        scan_authorization: AuthorizationState::Authorized,
                        connection_authorization: AuthorizationState::Authorized,
                        name: None,
                    },
                },
                completed(1),
            ],
        ),
        EventScenario::emit_before_return(
            RecordedCommand::StartScan(StartScanCommand {
                command_id: 2.into(),
                scan_session_id: 1.into(),
                options: ScanOptions::default(),
            }),
            vec![
                BleEvent::AdvertisementObserved {
                    advertisement: advertisement.clone(),
                },
                BleEvent::ScanStarted {
                    command_id: 2.into(),
                    scan_id: 1.into(),
                },
                completed(2),
            ],
        ),
        EventScenario::emit_before_return(
            RecordedCommand::StopScan(StopScanCommand {
                command_id: 3.into(),
                scan_session_id: 1.into(),
            }),
            vec![terminal.clone(), completed(3)],
        ),
    ]);
    let caller = tokio::runtime::Builder::new_current_thread()
        .build()
        .unwrap();
    let owner = Arc::new(Bleng::new().unwrap());
    caller.block_on(async {
        owner.assemble_backend(Arc::new(backend)).await.unwrap();
        owner.initialize(2000).await.unwrap();
    });
    let lease = ObservationProjectionLease::new(context());
    let projection = lease.projection();
    let (submitted_tx, submitted_rx) = mpsc::sync_channel(1);
    let (release_tx, release_rx) = mpsc::sync_channel(1);
    let (contended_tx, contended_rx) = mpsc::sync_channel(1);
    let (projected_tx, projected_rx) = mpsc::channel();
    *projection.install_pause.lock().unwrap() = Some(ScanInstallPause {
        submitted: submitted_tx,
        release: release_rx,
    });
    *projection.install_contention.lock().unwrap() = Some(contended_tx);
    let deadline = Duration::from_secs(2);

    thread::scope(|threads| {
        let submitter = threads.spawn(|| {
            projection.submit_scan(|| {
                owner
                    .begin_start_scan(ScanOptions::default())
                    .map_err(crate::SupervisorOperationError::from)
            })
        });
        assert_eq!(submitted_rx.recv_timeout(deadline).unwrap(), 2.into());
        // Only now let the sole real public-stream reader run. The hook must prove an
        // actual failed try_lock in project(), not merely announce a scheduled task.
        let reader = threads.spawn(|| {
            loop {
                let event = caller
                    .handle()
                    .block_on(owner.next_event(2000))
                    .unwrap()
                    .expect("owner stream ended before stop");
                let ended = matches!(event, BleEvent::ScanStopped { .. });
                if let Some(envelope) = projection.project(event) {
                    assert_eq!(envelope.source_context(), &context());
                    projected_tx.send(envelope.into_parts().1).unwrap();
                }
                if ended {
                    break;
                }
            }
        });
        contended_rx.recv_timeout(deadline).unwrap();
        assert!(matches!(
            projected_rx.try_recv(),
            Err(mpsc::TryRecvError::Empty)
        ));
        release_tx.send(()).unwrap();
        let handoff = submitter.join().unwrap().unwrap();
        assert_eq!(
            caller.block_on(handoff.operation.wait(2000)).unwrap(),
            1.into()
        );
        let expected = ScanEvent::Advertisement { advertisement };
        assert_eq!(projected_rx.recv_timeout(deadline).unwrap(), expected);
        assert_eq!(projected_rx.recv_timeout(deadline).unwrap(), started(2, 1));
        assert_eq!(next_ready(&handoff.delivery), Ok(expected));
        assert!(matches!(
            caller.block_on(handoff.operation.wait(2000)),
            Err(bleng::BlengError::OperationResultConsumed)
        ));
        let stop = owner.begin_stop_scan(1.into()).unwrap();
        caller.block_on(stop.wait(2000)).unwrap();
        let BleEvent::ScanStopped { scan_id, result } = terminal else {
            unreachable!()
        };
        let expected = ScanEvent::Stopped { scan_id, result };
        assert_eq!(projected_rx.recv_timeout(deadline).unwrap(), expected);
        reader.join().unwrap();
        assert_eq!(next_ready(&handoff.delivery), Ok(expected));
    });
    caller.block_on(owner.shutdown()).unwrap();
    assert_eq!(backend_handle.dispose_count(), 1);
    assert!(backend_handle.contract_violations().is_empty());
}

#[test]
fn late_start_failure_registration_is_nonblocking_and_drops_with_delivery() {
    use std::{sync::Arc, sync::mpsc, thread, time::Duration};

    use blehub::ScanOptions;
    use blehub_testkit::FakeBackend;
    use bleng::{Bleng, BlengError, BlengNotReadyReason};

    let caller = tokio::runtime::Builder::new_current_thread()
        .build()
        .unwrap();
    let owner = Bleng::new().unwrap();
    let (backend, backend_handle) = FakeBackend::scripted([]);
    caller
        .block_on(owner.assemble_backend(Arc::new(backend)))
        .unwrap();
    let operation = owner.begin_start_scan(ScanOptions::default()).unwrap();
    assert!(matches!(
        caller.block_on(operation.wait(2000)),
        Err(BlengError::NotReady {
            reason: BlengNotReadyReason::NotInitialized
        })
    ));
    let delivery = Arc::new(ScanDelivery::new(context(), operation.command_id()));
    delivery.offer(&context(), &advertisement(7, 1, 3));
    thread::scope(|threads| {
        let state = delivery.state.lock().unwrap();
        let (done_tx, done_rx) = mpsc::sync_channel(1);
        let receiver = &delivery;
        let operation = &operation;
        let registration = threads.spawn(move || {
            // Failure is already authoritative. Registration replays synchronously
            // while another thread holds state, so callback must not take that lock.
            receiver.observe_failure(operation);
            done_tx.send(()).unwrap();
        });
        let completed = done_rx.recv_timeout(Duration::from_secs(2));
        drop(state);
        registration.join().unwrap();
        completed.expect("start-failure replay blocked on the delivery state lock");
    });
    assert!(delivery.can_replace());
    assert_buffer(&delivery, 0, 0);
    assert_eq!(next_ready(&delivery), Err(ScanDeliveryError::StartFailed));
    let weak = Arc::downgrade(&delivery);
    drop(delivery);
    assert!(
        weak.upgrade().is_none(),
        "BLeng must hold only a weak receiver"
    );

    // The original operation remains alive, but dropping delivery revoked its token.
    let replacement = Arc::new(ScanDelivery::new(context(), operation.command_id()));
    replacement.observe_failure(&operation);
    assert_eq!(
        next_ready(&replacement),
        Err(ScanDeliveryError::StartFailed)
    );
    assert!(matches!(
        caller.block_on(operation.wait(2000)),
        Err(BlengError::OperationResultConsumed)
    ));
    assert!(backend_handle.commands().is_empty());
    caller.block_on(owner.shutdown()).unwrap();
    assert_eq!(backend_handle.dispose_count(), 1);
}

#[test]
fn is_scanning_requires_a_confirmed_start_and_clears_on_terminal() {
    let delivery = delivery();
    assert!(
        !delivery.is_scanning(),
        "an unconfirmed start is not scanning yet"
    );
    delivery.offer(&context(), &advertisement(7, 1, 2));
    assert!(
        !delivery.is_scanning(),
        "prestart advertisements do not confirm a scan"
    );
    delivery.offer(&context(), &started(41, 7));
    assert!(delivery.is_scanning());
    delivery.offer(&context(), &stopped(7));
    assert!(!delivery.is_scanning(), "a terminal ends the lane");
}

#[test]
fn prestart_replay_requires_exact_command_and_preserves_filtered_event_order() {
    let delivery = delivery();
    let first = advertisement(7, 1, 2);
    let second = advertisement(7, 2, 3);
    let terminal = failed_stop(7);
    for event in [
        advertisement(8, 90, 4),
        first.clone(),
        stopped(8),
        second.clone(),
        terminal.clone(),
        advertisement(7, 3, 5),
        stopped(7),
    ] {
        delivery.offer(&context(), &event);
    }
    delivery.offer(&context(), &started(40, 8));
    {
        let mut next = pin!(delivery.next_event());
        assert_eq!(poll_once(next.as_mut()), Poll::Pending);
    }

    delivery.offer(&context(), &started(41, 7));
    assert_buffer(&delivery, 2, 2 * MANUFACTURER_ENTRY_BYTES + 5);
    assert_eq!(next_ready(&delivery), Ok(first));
    assert_buffer(&delivery, 1, MANUFACTURER_ENTRY_BYTES + 3);
    assert_eq!(next_ready(&delivery), Ok(second));
    assert_buffer(&delivery, 0, 0);
    assert_eq!(next_ready(&delivery), Ok(terminal.clone()));
    assert_eq!(next_ready(&delivery), Ok(terminal));
}

#[test]
fn every_source_context_component_is_fenced_before_and_after_binding() {
    let wrong_contexts = [
        context_with("other-owner", "backend-1", 1, 1),
        context_with("owner-1", "other-backend", 1, 1),
        context_with("owner-1", "backend-1", 2, 1),
        context_with("owner-1", "backend-1", 1, 2),
    ];
    for source in wrong_contexts {
        let delivery = delivery();
        delivery.offer(&source, &started(41, 8));
        delivery.offer(&source, &advertisement(7, 90, MAX_SCAN_DATA_BYTES + 1));
        delivery.offer(&source, &stopped(7));
        assert_buffer(&delivery, 0, 0);
        {
            let mut next = pin!(delivery.next_event());
            assert_eq!(poll_once(next.as_mut()), Poll::Pending);
        }

        delivery.offer(&context(), &started(41, 7));
        delivery.offer(&source, &started(41, 8));
        delivery.offer(&source, &advertisement(7, 91, MAX_SCAN_DATA_BYTES + 1));
        delivery.offer(&source, &stopped(7));
        let expected = advertisement(7, 1, 3);
        delivery.offer(&context(), &expected);
        assert_buffer(&delivery, 1, MANUFACTURER_ENTRY_BYTES + 3);
        assert_eq!(next_ready(&delivery), Ok(expected));
        {
            let mut next = pin!(delivery.next_event());
            assert_eq!(poll_once(next.as_mut()), Poll::Pending);
        }
        delivery.offer(&context(), &stopped(7));
        assert_eq!(next_ready(&delivery), Ok(stopped(7)));
    }
}

#[test]
fn duplicate_start_is_idempotent_but_conflicting_matching_start_latches_failure() {
    let delivery = delivery();
    delivery.offer(&context(), &started(41, 7));
    let expected = advertisement(7, 1, 3);
    delivery.offer(&context(), &expected);
    delivery.offer(&context(), &started(41, 7));
    delivery.offer(&context(), &started(40, 8));
    assert_buffer(&delivery, 1, MANUFACTURER_ENTRY_BYTES + 3);
    assert_eq!(next_ready(&delivery), Ok(expected));

    delivery.offer(&context(), &advertisement(7, 2, 4));
    delivery.offer(&context(), &started(41, 8));
    assert_latched_failure(&delivery, ScanDeliveryError::ConflictingStart);
}

#[test]
fn advertisement_limit_is_inclusive_and_overflow_discards_prestart_and_bound_queues() {
    assert_eq!(MAX_SCAN_ADVERTISEMENTS, 64);
    for bind_first in [false, true] {
        let delivery = delivery();
        if bind_first {
            delivery.offer(&context(), &started(41, 7));
        }
        for marker in 0..MAX_SCAN_ADVERTISEMENTS {
            delivery.offer(&context(), &advertisement(7, marker as u8, 1));
        }
        assert_buffer(
            &delivery,
            MAX_SCAN_ADVERTISEMENTS,
            MAX_SCAN_ADVERTISEMENTS * (MANUFACTURER_ENTRY_BYTES + 1),
        );
        delivery.offer(&context(), &advertisement(7, 99, 0));
        assert_latched_failure(&delivery, ScanDeliveryError::AdvertisementOverflow);
    }
}

#[test]
fn byte_budget_counts_all_variable_fields_and_releases_bytes_on_delivery() {
    assert_eq!(MAX_SCAN_DATA_BYTES, 256 * 1024);
    let delivery = delivery();
    delivery.offer(&context(), &started(41, 7));
    let rich = rich_advertisement(7);
    // Nested element storage plus UTF-8 name, UUID strings and all payloads.
    let rich_bytes = 2 * size_of::<BluetoothUuid>()
        + 2 * MANUFACTURER_ENTRY_BYTES
        + 2 * size_of::<ServiceData>()
        + 7
        + 2 * 36
        + 3
        + 2
        + 2 * 36
        + 4
        + 1;
    let filler_bytes = MAX_SCAN_DATA_BYTES - rich_bytes;
    let filler = advertisement(7, 0, filler_bytes - MANUFACTURER_ENTRY_BYTES);
    delivery.offer(&context(), &rich);
    assert_buffer(&delivery, 1, rich_bytes);
    delivery.offer(&context(), &filler);
    assert_buffer(&delivery, 2, MAX_SCAN_DATA_BYTES);
    assert_eq!(next_ready(&delivery), Ok(rich));
    assert_buffer(&delivery, 1, filler_bytes);
    assert_eq!(next_ready(&delivery), Ok(filler));
    assert_buffer(&delivery, 0, 0);

    let reused = advertisement(7, 1, MAX_SCAN_DATA_BYTES - MANUFACTURER_ENTRY_BYTES);
    delivery.offer(&context(), &reused);
    assert_buffer(&delivery, 1, MAX_SCAN_DATA_BYTES);
    assert_eq!(next_ready(&delivery), Ok(reused));
    assert_buffer(&delivery, 0, 0);
}

#[test]
fn cumulative_byte_overflow_is_latched_before_and_after_binding() {
    for bind_first in [false, true] {
        let delivery = delivery();
        if bind_first {
            delivery.offer(&context(), &started(41, 7));
        }
        delivery.offer(
            &context(),
            &advertisement(7, 1, MAX_SCAN_DATA_BYTES - MANUFACTURER_ENTRY_BYTES),
        );
        assert_buffer(&delivery, 1, MAX_SCAN_DATA_BYTES);
        delivery.offer(&context(), &advertisement(7, 2, 1));
        assert_latched_failure(&delivery, ScanDeliveryError::ByteOverflow);
    }
    let delivery = delivery();
    delivery.offer(
        &context(),
        &advertisement(7, 1, MAX_SCAN_DATA_BYTES - MANUFACTURER_ENTRY_BYTES + 1),
    );
    assert_latched_failure(&delivery, ScanDeliveryError::ByteOverflow);
}

#[test]
fn empty_nested_elements_exceed_byte_budget_before_buffering_and_preserve_failure() {
    for (collection, entry_bytes) in [
        ("advertised_services", size_of::<BluetoothUuid>()),
        ("manufacturer_data", MANUFACTURER_ENTRY_BYTES),
        ("service_data", size_of::<ServiceData>()),
    ] {
        let entries = MAX_SCAN_DATA_BYTES / entry_bytes + 1;
        let ScanEvent::Advertisement {
            advertisement: mut oversized,
        } = advertisement(7, 0, 0)
        else {
            unreachable!();
        };
        oversized.manufacturer_data.clear();
        match collection {
            "advertised_services" => {
                oversized.advertised_services = vec![
                    BluetoothUuid {
                        value: String::new(),
                    };
                    entries
                ];
            }
            "manufacturer_data" => {
                oversized.manufacturer_data = vec![
                    ManufacturerData {
                        company_id: 0,
                        payload: vec![],
                    };
                    entries
                ];
            }
            "service_data" => {
                oversized.service_data = vec![
                    ServiceData {
                        service_uuid: BluetoothUuid {
                            value: String::new(),
                        },
                        payload: vec![],
                    };
                    entries
                ];
            }
            _ => unreachable!(),
        }
        let event = ScanEvent::Advertisement {
            advertisement: oversized,
        };
        assert!(entries * entry_bytes > MAX_SCAN_DATA_BYTES);
        for bind_first in [false, true] {
            let delivery = delivery();
            if bind_first {
                delivery.offer(&context(), &started(41, 7));
            }
            delivery.offer(&context(), &advertisement(7, 1, 3));
            assert_buffer(&delivery, 1, MANUFACTURER_ENTRY_BYTES + 3);
            delivery.offer(&context(), &event);
            assert_eq!(
                next_ready(&delivery),
                Err(ScanDeliveryError::ByteOverflow),
                "{collection}, bind_first={bind_first}"
            );
            assert_latched_failure(&delivery, ScanDeliveryError::ByteOverflow);
        }
    }
}

#[test]
fn stop_data_overflow_rejects_each_utf8_field_and_cumulative_pending_data_and_latches() {
    assert_eq!(MAX_SCAN_STOP_DATA_BYTES, 256 * 1024);
    for field in 0..=4 {
        let mut fields: [String; 4] = std::array::from_fn(|_| String::new());
        if field == 4 {
            fields = std::array::from_fn(|_| "é".repeat(MAX_SCAN_STOP_DATA_BYTES / 8));
            fields[3].push('x');
        } else {
            fields[field] = "é".repeat(MAX_SCAN_STOP_DATA_BYTES / 2);
            fields[field].push('x');
        }
        let [message, code, name, detail_message] = fields;
        let oversized = stop_with_error_data(
            7,
            message,
            Some(Box::new(PlatformErrorDetails {
                platform: Platform::Apple,
                code: Some(code),
                name: Some(name),
                message: Some(detail_message),
                status: None,
                att_code: None,
            })),
        );
        for bind_first in [false, true] {
            let delivery = delivery();
            if bind_first {
                delivery.offer(&context(), &started(41, 7));
            }
            delivery.offer(&context(), &advertisement(7, 1, 3));
            delivery.offer(&context(), &oversized);
            assert_eq!(
                next_ready(&delivery),
                Err(ScanDeliveryError::StopDataOverflow),
                "field={field}, bind_first={bind_first}"
            );
            assert_buffer(&delivery, 0, 0);
            assert_eq!(delivery.snapshot().queued_stop_bytes, 0);
            delivery.offer(&context(), &advertisement(7, 2, MAX_SCAN_DATA_BYTES + 1));
            if bind_first {
                delivery.close();
            } else {
                delivery.detach();
            }
            assert_latched_failure(&delivery, ScanDeliveryError::StopDataOverflow);
        }
    }

    let delivery = delivery();
    let bytes_per_stop = MAX_SCAN_STOP_DATA_BYTES / 64;
    for scan_id in 100..163 {
        delivery.offer(
            &context(),
            &stop_with_error_data(scan_id, "é".repeat(bytes_per_stop / 2), None),
        );
    }
    assert_eq!(delivery.snapshot().queued_stop_bytes, 63 * bytes_per_stop);
    delivery.offer(
        &context(),
        &stop_with_error_data(163, "é".repeat(bytes_per_stop / 2) + "x", None),
    );
    // The 64th stop is within the event-count bound, but exceeds the byte bound by one.
    assert_latched_failure(&delivery, ScanDeliveryError::StopDataOverflow);
}

#[test]
fn stop_budget_exact_boundaries_are_independent_recomputed_on_binding_and_released_on_close() {
    for bind_first in [false, true] {
        let delivery = delivery();
        if bind_first {
            delivery.offer(&context(), &started(41, 7));
        }
        let first_payload = MAX_SCAN_DATA_BYTES
            - MAX_SCAN_ADVERTISEMENTS * MANUFACTURER_ENTRY_BYTES
            - (MAX_SCAN_ADVERTISEMENTS - 1);
        for marker in 0..MAX_SCAN_ADVERTISEMENTS {
            let bytes = if marker == 0 { first_payload } else { 1 };
            delivery.offer(&context(), &advertisement(7, marker as u8, bytes));
        }
        let quarter = "é".repeat(MAX_SCAN_STOP_DATA_BYTES / 8);
        let terminal = stop_with_error_data(
            7,
            quarter.clone(),
            Some(Box::new(PlatformErrorDetails {
                platform: Platform::Apple,
                code: Some(quarter.clone()),
                name: Some(quarter.clone()),
                message: Some(quarter),
                status: None,
                att_code: None,
            })),
        );
        delivery.offer(&context(), &terminal);
        assert_buffer(&delivery, MAX_SCAN_ADVERTISEMENTS, MAX_SCAN_DATA_BYTES);
        assert_eq!(
            delivery.snapshot().queued_stop_bytes,
            MAX_SCAN_STOP_DATA_BYTES
        );
        if !bind_first {
            delivery.offer(&context(), &started(41, 7));
        }
        assert_eq!(
            delivery.snapshot().queued_stop_bytes,
            MAX_SCAN_STOP_DATA_BYTES
        );
        for marker in 0..MAX_SCAN_ADVERTISEMENTS {
            let bytes = if marker == 0 { first_payload } else { 1 };
            assert_eq!(
                next_ready(&delivery),
                Ok(advertisement(7, marker as u8, bytes))
            );
        }
        assert_buffer(&delivery, 0, 0);
        assert_eq!(next_ready(&delivery).as_ref(), Ok(&terminal));
        assert_eq!(next_ready(&delivery).as_ref(), Ok(&terminal));
        // A latched terminal still owns its error strings even with no advertisements queued.
        assert_eq!(
            delivery.snapshot().queued_stop_bytes,
            MAX_SCAN_STOP_DATA_BYTES
        );
        let expected_error = if bind_first {
            delivery.close();
            ScanDeliveryError::OwnerRetired
        } else {
            delivery.detach();
            ScanDeliveryError::Detached
        };
        assert_buffer(&delivery, 0, 0);
        assert_eq!(delivery.snapshot().queued_stop_bytes, 0);
        assert_eq!(next_ready(&delivery), Err(expected_error));
    }

    let bytes_per_stop = MAX_SCAN_STOP_DATA_BYTES / 64;
    for keep_matching in [false, true] {
        let delivery = delivery();
        for index in 0..64 {
            let scan_id = if keep_matching && index == 63 {
                7
            } else {
                100 + index
            };
            delivery.offer(
                &context(),
                &stop_with_error_data(scan_id, "é".repeat(bytes_per_stop / 2), None),
            );
        }
        assert_eq!(
            delivery.snapshot().queued_stop_bytes,
            MAX_SCAN_STOP_DATA_BYTES
        );
        delivery.offer(&context(), &started(41, 7));
        let retained_bytes = if keep_matching { bytes_per_stop } else { 0 };
        assert_eq!(delivery.snapshot().queued_stop_bytes, retained_bytes);
        let terminal = if keep_matching {
            stop_with_error_data(7, "é".repeat(bytes_per_stop / 2), None)
        } else {
            {
                let mut next = pin!(delivery.next_event());
                assert_eq!(poll_once(next.as_mut()), Poll::Pending);
            }
            let terminal = stop_with_error_data(7, "é".repeat(MAX_SCAN_STOP_DATA_BYTES / 2), None);
            delivery.offer(&context(), &terminal);
            terminal
        };
        assert_eq!(next_ready(&delivery).as_ref(), Ok(&terminal));
        assert_eq!(
            delivery.snapshot().queued_stop_bytes,
            if keep_matching {
                bytes_per_stop
            } else {
                MAX_SCAN_STOP_DATA_BYTES
            }
        );
        delivery.detach();
        assert_eq!(delivery.snapshot().queued_stop_bytes, 0);
        assert_eq!(next_ready(&delivery), Err(ScanDeliveryError::Detached));
    }

    for owner_close in [false, true] {
        let delivery = delivery();
        delivery.offer(&context(), &failed_stop(8));
        assert_eq!(
            delivery.snapshot().queued_stop_bytes,
            "radio lost\0detail".len()
        );
        let expected_error = if owner_close {
            delivery.close();
            ScanDeliveryError::OwnerRetired
        } else {
            delivery.detach();
            ScanDeliveryError::Detached
        };
        assert_eq!(delivery.snapshot().queued_stop_bytes, 0);
        assert_eq!(next_ready(&delivery), Err(expected_error));
    }
}

#[test]
fn pending_stop_budget_is_separate_and_bound_terminal_needs_no_advertisement_slot() {
    let delivery = delivery();
    for marker in 0..MAX_SCAN_ADVERTISEMENTS {
        delivery.offer(&context(), &advertisement(7, marker as u8, 1));
    }
    for scan_id in 100..164 {
        delivery.offer(&context(), &stopped(scan_id));
    }
    assert_buffer(&delivery, 64, 64 * (MANUFACTURER_ENTRY_BYTES + 1));
    delivery.offer(&context(), &started(41, 7));
    delivery.offer(&context(), &stopped(8));
    delivery.offer(&context(), &advertisement(8, 99, MAX_SCAN_DATA_BYTES + 1));
    delivery.offer(&context(), &stopped(7));
    assert_buffer(&delivery, 64, 64 * (MANUFACTURER_ENTRY_BYTES + 1));
    for marker in 0..MAX_SCAN_ADVERTISEMENTS {
        assert_eq!(next_ready(&delivery), Ok(advertisement(7, marker as u8, 1)));
    }
    assert_buffer(&delivery, 0, 0);
    assert_eq!(next_ready(&delivery), Ok(stopped(7)));
    assert_eq!(next_ready(&delivery), Ok(stopped(7)));
}

#[test]
fn sixty_fifth_pending_stop_overflows_and_clears_advertisement_storage() {
    let delivery = delivery();
    delivery.offer(&context(), &advertisement(7, 1, 3));
    for scan_id in 100..164 {
        delivery.offer(&context(), &stopped(scan_id));
    }
    assert_buffer(&delivery, 1, MANUFACTURER_ENTRY_BYTES + 3);
    delivery.offer(&context(), &stopped(164));
    assert_latched_failure(&delivery, ScanDeliveryError::PendingEventOverflow);
}

#[test]
fn normal_stop_drains_earlier_advertisements_then_repeats_original_terminal() {
    let delivery = delivery();
    delivery.offer(&context(), &started(41, 7));
    let first = rich_advertisement(7);
    let second = advertisement(7, 2, 5);
    let terminal = failed_stop(7);
    delivery.offer(&context(), &first);
    delivery.offer(&context(), &second);
    delivery.offer(&context(), &terminal);
    for _ in 0..=MAX_SCAN_ADVERTISEMENTS {
        delivery.offer(&context(), &advertisement(7, 99, MAX_SCAN_DATA_BYTES + 1));
    }
    delivery.offer(&context(), &stopped(7));
    assert_eq!(next_ready(&delivery), Ok(first));
    assert_buffer(&delivery, 1, MANUFACTURER_ENTRY_BYTES + 5);
    assert_eq!(next_ready(&delivery), Ok(second));
    assert_buffer(&delivery, 0, 0);
    assert_eq!(next_ready(&delivery), Ok(terminal.clone()));
    assert_eq!(next_ready(&delivery), Ok(terminal));
}

#[test]
fn owner_close_discards_buffered_normal_stop_and_resolves_pending_pull() {
    let delivery = delivery();
    delivery.offer(&context(), &started(41, 7));
    delivery.offer(&context(), &advertisement(7, 1, 3));
    delivery.offer(&context(), &stopped(7));
    delivery.close();
    assert_buffer(&delivery, 0, 0);
    assert_eq!(next_ready(&delivery), Err(ScanDeliveryError::OwnerRetired));
    delivery.offer(&context(), &started(41, 7));
    delivery.offer(&context(), &advertisement(7, 2, 4));
    delivery.offer(&context(), &stopped(7));
    delivery.close();
    assert_buffer(&delivery, 0, 0);
    assert_eq!(next_ready(&delivery), Err(ScanDeliveryError::OwnerRetired));

    let pending_delivery = self::delivery();
    let mut pending = pin!(pending_delivery.next_event());
    assert_eq!(poll_once(pending.as_mut()), Poll::Pending);
    pending_delivery.close();
    assert_eq!(
        poll_once(pending.as_mut()),
        Poll::Ready(Err(ScanDeliveryError::OwnerRetired))
    );
    assert!(!pending_delivery.snapshot().pull_pending);
}

#[test]
fn detach_closes_only_that_observer_discards_storage_and_resolves_pending_pull() {
    let detached = delivery();
    let continuing = delivery();
    for observer in [&detached, &continuing] {
        observer.offer(&context(), &started(41, 7));
        observer.offer(&context(), &advertisement(7, 1, 3));
    }
    detached.detach();
    detached.detach();
    assert_buffer(&detached, 0, 0);
    assert_eq!(next_ready(&detached), Err(ScanDeliveryError::Detached));
    assert_eq!(next_ready(&continuing), Ok(advertisement(7, 1, 3)));
    for observer in [&detached, &continuing] {
        observer.offer(&context(), &advertisement(7, 2, 4));
        observer.offer(&context(), &stopped(7));
    }
    assert_buffer(&detached, 0, 0);
    assert_eq!(next_ready(&detached), Err(ScanDeliveryError::Detached));
    assert_eq!(next_ready(&continuing), Ok(advertisement(7, 2, 4)));
    assert_eq!(next_ready(&continuing), Ok(stopped(7)));

    let pending_delivery = delivery();
    let mut pending = pin!(pending_delivery.next_event());
    assert_eq!(poll_once(pending.as_mut()), Poll::Pending);
    pending_delivery.detach();
    assert_eq!(
        poll_once(pending.as_mut()),
        Poll::Ready(Err(ScanDeliveryError::Detached))
    );
    assert!(!pending_delivery.snapshot().pull_pending);
}

#[test]
fn single_pull_guard_survives_busy_future_drop_and_cancellation_does_not_consume_events() {
    let delivery = delivery();
    delivery.offer(&context(), &started(41, 7));
    {
        let _unpolled = delivery.next_event();
        assert!(!delivery.snapshot().pull_pending);
    }
    {
        let mut first = pin!(delivery.next_event());
        assert_eq!(poll_once(first.as_mut()), Poll::Pending);
        assert!(delivery.snapshot().pull_pending);
        {
            let mut second = pin!(delivery.next_event());
            assert_eq!(
                poll_once(second.as_mut()),
                Poll::Ready(Err(ScanDeliveryError::PullBusy))
            );
        }
        assert!(delivery.snapshot().pull_pending);
        assert_eq!(poll_once(first.as_mut()), Poll::Pending);
    }
    assert!(!delivery.snapshot().pull_pending);

    let expected = rich_advertisement(7);
    {
        let mut replacement = pin!(delivery.next_event());
        assert_eq!(poll_once(replacement.as_mut()), Poll::Pending);
        delivery.offer(&context(), &expected);
        // Cancellation after notification but before polling must leave the event available.
    }
    assert!(!delivery.snapshot().pull_pending);
    assert_eq!(next_ready(&delivery), Ok(expected));
    assert_buffer(&delivery, 0, 0);
    assert!(!delivery.snapshot().pull_pending);

    {
        let mut next = pin!(delivery.next_event());
        assert_eq!(poll_once(next.as_mut()), Poll::Pending);
        delivery.offer(&context(), &stopped(7));
        assert_eq!(poll_once(next.as_mut()), Poll::Ready(Ok(stopped(7))));
    }
    assert!(!delivery.snapshot().pull_pending);
    assert_eq!(next_ready(&delivery), Ok(stopped(7)));
}
