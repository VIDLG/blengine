mod support;

use std::sync::Arc;

use blehub::*;
use blehub_testkit::*;

use support::*;

#[test]
fn accepted_scenario_can_be_advanced_after_dispatch_returns() {
    let command_id = CommandId { value: 7 };
    let scan_session_id = ScanSessionId { value: 11 };
    let command = StartScanCommand {
        command_id,
        scan_session_id,
        options: ScanOptions::default(),
    };
    let (backend, handle) = FakeBackend::scripted([EventScenario::complete(
        RecordedCommand::StartScan(command.clone()),
        vec![
            scan_started(command_id, scan_session_id),
            succeeded(command_id),
        ],
    )]);
    let sink = RecordingSink::new();
    let hub = BleHub::new(Arc::new(backend), Arc::new(sink.clone())).unwrap();
    let mut projection = ReferenceProjection::default();
    assert!(projection.register_command(command_id));

    hub.start_scan(command).unwrap();

    assert!(sink.events().is_empty());
    assert!(projection.is_pending(command_id));
    assert_eq!(handle.pending_event_count(), 2);

    assert_eq!(handle.advance(), Some(EventIngressStatus::Delivered));
    assert_eq!(
        projection.apply(&sink.events()[0]),
        EventDisposition::Accepted
    );
    assert!(projection.is_pending(command_id));

    assert_eq!(handle.advance(), Some(EventIngressStatus::Delivered));
    assert_eq!(
        projection.apply(&sink.events()[1]),
        EventDisposition::Accepted
    );
    assert_eq!(
        projection.outcome(command_id),
        Some(ProjectedCommandOutcome::Succeeded)
    );
    assert_eq!(handle.advance(), None);
}

#[test]
fn deferred_sink_failure_consumes_the_attempt_without_retrying_it() {
    let command_id = CommandId { value: 7 };
    let scan_session_id = ScanSessionId { value: 11 };
    let command = StartScanCommand {
        command_id,
        scan_session_id,
        options: ScanOptions::default(),
    };
    let (backend, handle) = FakeBackend::scripted([EventScenario::complete(
        RecordedCommand::StartScan(command.clone()),
        vec![
            scan_started(command_id, scan_session_id),
            succeeded(command_id),
        ],
    )]);
    let sink = RecordingSink::new();
    let hub = BleHub::new(Arc::new(backend), Arc::new(sink.clone())).unwrap();

    hub.start_scan(command).unwrap();
    sink.set_behavior(SinkBehavior::Reject {
        message: "scripted sink rejection".to_owned(),
    });

    assert!(matches!(
        handle.advance(),
        Some(EventIngressStatus::DownstreamFailed { .. })
    ));
    assert!(sink.events().is_empty());
    assert_eq!(handle.pending_event_count(), 1);

    sink.set_behavior(SinkBehavior::Accept);

    assert_eq!(handle.advance(), Some(EventIngressStatus::Delivered));
    assert_eq!(sink.events(), vec![succeeded(command_id)]);
    assert_eq!(handle.pending_event_count(), 0);
    assert_eq!(handle.advance(), None);
    assert!(handle.contract_violations().is_empty());
}

#[test]
fn deferred_sink_panic_can_recover_to_accept_without_retrying_the_attempt() {
    let command_id = CommandId { value: 7 };
    let scan_session_id = ScanSessionId { value: 11 };
    let command = StartScanCommand {
        command_id,
        scan_session_id,
        options: ScanOptions::default(),
    };
    let (backend, handle) = FakeBackend::scripted([EventScenario::complete(
        RecordedCommand::StartScan(command.clone()),
        vec![
            scan_started(command_id, scan_session_id),
            succeeded(command_id),
        ],
    )]);
    let sink = RecordingSink::new();
    let hub = BleHub::new(Arc::new(backend), Arc::new(sink.clone())).unwrap();
    hub.start_scan(command).unwrap();
    sink.set_behavior(SinkBehavior::Panic);

    let failure = EventIngressStatus::DownstreamFailed {
        message: "downstream panicked while handling an event".to_owned(),
    };
    assert_eq!(handle.advance(), Some(failure.clone()));
    assert!(sink.events().is_empty());
    assert_eq!(handle.pending_event_count(), 1);

    sink.set_behavior(SinkBehavior::Accept);
    assert_eq!(handle.advance(), Some(EventIngressStatus::Delivered));
    assert_eq!(sink.events(), vec![succeeded(command_id)]);
    assert_eq!(
        handle.push_statuses(),
        vec![failure, EventIngressStatus::Delivered]
    );
    assert_eq!(handle.advance(), None);
    handle.finish_completion(command_id, CompletionExpectation::SucceededWithDomain);
    assert!(handle.contract_violations().is_empty());
}

#[test]
fn invalid_event_attempt_is_consumed_and_later_terminal_is_still_attempted() {
    let mut database = duplicate_gatt_database_fixture();
    database.services[1].id = database.services[0].id;
    let command_id = CommandId { value: 7 };
    let command = DiscoverGattCommand {
        command_id,
        connection_id: database.connection_id,
        gatt_session_id: database.gatt_session_id,
    };
    let (backend, handle) = FakeBackend::scripted([EventScenario::complete(
        RecordedCommand::DiscoverGatt(command),
        vec![
            BleEvent::GattDiscovered {
                command_id,
                database,
            },
            succeeded(command_id),
        ],
    )]);
    let sink = RecordingSink::new();
    let hub = BleHub::new(Arc::new(backend), Arc::new(sink.clone())).unwrap();

    hub.discover_gatt(command).unwrap();

    assert!(matches!(
        handle.advance(),
        Some(EventIngressStatus::InvalidEvent { .. })
    ));
    assert!(sink.events().is_empty());
    assert_eq!(handle.pending_event_count(), 1);
    assert_eq!(handle.advance(), Some(EventIngressStatus::Delivered));
    assert_eq!(sink.events(), vec![succeeded(command_id)]);
    assert!(handle.terminal_attempted(command_id));
    handle.finish_completion(command_id, CompletionExpectation::SucceededWithDomain);
    assert!(handle.contract_violations().is_empty());
}

#[test]
fn closed_status_discards_the_rest_of_a_deferred_batch() {
    let command_id = CommandId { value: 7 };
    let scan_session_id = ScanSessionId { value: 11 };
    let command = StartScanCommand {
        command_id,
        scan_session_id,
        options: ScanOptions::default(),
    };
    let (backend, handle) = FakeBackend::scripted([EventScenario::complete(
        RecordedCommand::StartScan(command.clone()),
        vec![
            scan_started(command_id, scan_session_id),
            succeeded(command_id),
        ],
    )]);
    let sink = RecordingSink::new();
    let hub = BleHub::new(Arc::new(backend), Arc::new(sink.clone())).unwrap();
    hub.start_scan(command).unwrap();

    hub.dispose().unwrap();

    assert_eq!(handle.advance(), Some(EventIngressStatus::Closed));
    assert_eq!(handle.pending_event_count(), 0);
    assert_eq!(handle.advance(), None);
    assert!(sink.events().is_empty());
}
