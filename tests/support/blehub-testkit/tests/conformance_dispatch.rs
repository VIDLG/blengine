mod support;

use std::sync::Arc;

use blehub::*;
use blehub_testkit::*;

use support::*;

#[test]
fn rejected_terminal_attempt_is_not_retried_or_counted_as_missing() {
    let command_id = CommandId { value: 7 };
    let command = StartScanCommand {
        command_id,
        scan_session_id: ScanSessionId { value: 11 },
        options: ScanOptions::default(),
    };
    let terminal = failed(command_id);
    let (backend, handle) = FakeBackend::scripted([EventScenario::complete(
        RecordedCommand::StartScan(command.clone()),
        vec![terminal.clone(), terminal.clone()],
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
    assert!(handle.terminal_attempted(command_id));
    handle.finish_completion(command_id, CompletionExpectation::Failed);
    assert!(handle.contract_violations().is_empty());
    assert!(sink.events().is_empty());

    sink.set_behavior(SinkBehavior::Accept);

    assert_eq!(handle.advance(), Some(EventIngressStatus::Delivered));
    assert_eq!(sink.events(), vec![terminal]);
    assert_eq!(
        handle.contract_violations(),
        vec![BackendContractViolation::DuplicateTerminalAttempt { command_id }]
    );
}

#[test]
fn command_event_target_mismatch_is_reported_but_not_filtered_by_the_bridge() {
    let command_id = CommandId { value: 7 };
    let command = ReadCharacteristicCommand {
        command_id,
        connection_id: ConnectionId { value: 2 },
        gatt_session_id: GattSessionId { value: 3 },
        characteristic_id: GattCharacteristicId { value: 4 },
    };
    let mismatched = BleEvent::CharacteristicRead {
        command_id,
        connection_id: command.connection_id,
        gatt_session_id: GattSessionId { value: 99 },
        characteristic_id: command.characteristic_id,
        value: vec![1, 2, 3],
    };
    let (backend, handle) = FakeBackend::scripted([EventScenario::complete(
        RecordedCommand::ReadCharacteristic(command),
        vec![mismatched.clone(), succeeded(command_id)],
    )]);
    let sink = RecordingSink::new();
    let hub = BleHub::new(Arc::new(backend), Arc::new(sink.clone())).unwrap();

    hub.read_characteristic(command).unwrap();

    assert_eq!(handle.advance_all(), vec![EventIngressStatus::Delivered; 2]);
    assert_eq!(sink.events(), vec![mismatched, succeeded(command_id)]);
    assert_eq!(
        handle.contract_violations(),
        vec![BackendContractViolation::CommandEventMismatch {
            command_id,
            operation: CommandKind::ReadCharacteristic,
            event: CommandEventKind::CharacteristicRead,
        }]
    );
}

#[test]
fn completion_oracle_reports_missing_domain_and_terminal_attempts() {
    let command_id = CommandId { value: 7 };
    let command = StartScanCommand {
        command_id,
        scan_session_id: ScanSessionId { value: 11 },
        options: ScanOptions::default(),
    };
    let (backend, handle) = FakeBackend::scripted([EventScenario::accept(
        RecordedCommand::StartScan(command.clone()),
    )]);
    let hub = BleHub::new(Arc::new(backend), Arc::new(RecordingSink::new())).unwrap();

    hub.start_scan(command).unwrap();
    handle.finish_completion(command_id, CompletionExpectation::SucceededWithDomain);

    assert_eq!(
        handle.contract_violations(),
        vec![
            BackendContractViolation::MissingDomainEventAttempt {
                command_id,
                operation: CommandKind::StartScan,
            },
            BackendContractViolation::MissingTerminalAttempt { command_id },
        ]
    );
}

#[test]
fn event_emission_before_dispatch_rejection_is_flagged_as_a_contract_violation() {
    let command_id = CommandId { value: 7 };
    let scan_session_id = ScanSessionId { value: 11 };
    let error = CommandDispatchError::BackendUnavailable {
        detail: "rejected after emission".to_owned(),
        platform_details: None,
    };
    let command = StartScanCommand {
        command_id,
        scan_session_id,
        options: ScanOptions::default(),
    };
    let (backend, handle) = FakeBackend::scripted([EventScenario::emit_before_reject(
        RecordedCommand::StartScan(command.clone()),
        vec![scan_started(command_id, scan_session_id)],
        error.clone(),
    )]);
    let sink = RecordingSink::new();
    let hub = BleHub::new(Arc::new(backend), Arc::new(sink.clone())).unwrap();

    assert_eq!(hub.start_scan(command), Err(error));
    assert_eq!(sink.events().len(), 1);
    assert_eq!(
        handle.contract_violations(),
        vec![
            BackendContractViolation::EventEmissionBeforeDispatchRejection {
                operation: CommandKind::StartScan,
                emitted_event_count: 1,
            }
        ]
    );
}

#[test]
fn tracker_flags_events_after_terminal_and_duplicate_terminal_attempts() {
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
            succeeded(command_id),
            scan_started(command_id, scan_session_id),
            succeeded(command_id),
        ],
    )]);
    let hub = BleHub::new(Arc::new(backend), Arc::new(RecordingSink::new())).unwrap();

    hub.start_scan(command).unwrap();
    assert_eq!(handle.advance_all(), vec![EventIngressStatus::Delivered; 3]);
    assert_eq!(
        handle.contract_violations(),
        vec![
            BackendContractViolation::EventAfterTerminalAttempt { command_id },
            BackendContractViolation::DuplicateTerminalAttempt { command_id },
        ]
    );
}

#[test]
fn tracker_flags_event_emitted_after_dispatch_rejection() {
    let command_id = CommandId { value: 7 };
    let command = StartScanCommand {
        command_id,
        scan_session_id: ScanSessionId { value: 11 },
        options: ScanOptions::default(),
    };
    let error = CommandDispatchError::InvalidState {
        detail: "scripted rejection".to_owned(),
    };
    let (backend, handle) = FakeBackend::scripted([EventScenario::reject(
        RecordedCommand::StartScan(command.clone()),
        error.clone(),
    )]);
    let sink = RecordingSink::new();
    let hub = BleHub::new(Arc::new(backend), Arc::new(sink.clone())).unwrap();

    assert_eq!(hub.start_scan(command), Err(error));
    assert_eq!(
        handle.emit(succeeded(command_id)),
        EventIngressStatus::Delivered
    );
    assert_eq!(sink.events(), vec![succeeded(command_id)]);
    assert_eq!(
        handle.contract_violations(),
        vec![BackendContractViolation::EventAfterDispatchRejection { command_id }]
    );
}
