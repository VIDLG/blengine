use std::sync::Arc;

use blehub::*;
use blehub_testkit::*;

#[test]
fn complete_command_mismatch_rejects_without_emitting_events() {
    let expected = ConnectCommand {
        command_id: CommandId { value: 1 },
        connection_id: ConnectionId { value: 2 },
        peripheral_id: PeripheralId { value: 3 },
    };
    let actual = ConnectCommand {
        peripheral_id: PeripheralId { value: 4 },
        ..expected
    };
    let (backend, handle) = FakeBackend::scripted([EventScenario::complete(
        RecordedCommand::Connect(expected),
        vec![BleEvent::CommandCompleted {
            command_id: expected.command_id,
            error: None,
        }],
    )]);
    let sink = RecordingSink::new();
    let hub = BleHub::new(Arc::new(backend), Arc::new(sink.clone())).unwrap();

    assert!(matches!(
        hub.connect(actual),
        Err(CommandDispatchError::InvalidState { .. })
    ));
    assert!(sink.events().is_empty());
    assert_eq!(handle.pending_event_count(), 0);
    assert_eq!(handle.commands(), vec![RecordedCommand::Connect(actual)]);
}

#[test]
fn dispatch_rejection_emits_no_event() {
    let error = CommandDispatchError::BackendUnavailable {
        detail: "scripted rejection".to_owned(),
        platform_details: None,
    };
    let command = ConnectCommand {
        command_id: CommandId { value: 1 },
        connection_id: ConnectionId { value: 2 },
        peripheral_id: PeripheralId { value: 3 },
    };
    let (backend, _) = FakeBackend::scripted([EventScenario::reject(
        RecordedCommand::Connect(command),
        error.clone(),
    )]);
    let sink = RecordingSink::new();
    let hub = BleHub::new(Arc::new(backend), Arc::new(sink.clone())).unwrap();

    assert_eq!(hub.connect(command), Err(error));
    assert!(sink.events().is_empty());
}

#[test]
fn records_the_complete_canonicalized_backend_command() {
    let command = StartScanCommand {
        command_id: CommandId { value: 1 },
        scan_session_id: ScanSessionId { value: 2 },
        options: ScanOptions {
            service_uuids: vec![BluetoothUuid {
                value: "180D".to_owned(),
            }],
            name_prefix: Some("BleHub".to_owned()),
            company_id: Some(0x1234),
            allow_duplicates: true,
        },
    };
    let expected = StartScanCommand {
        command_id: CommandId { value: 1 },
        scan_session_id: ScanSessionId { value: 2 },
        options: ScanOptions {
            service_uuids: vec![BluetoothUuid {
                value: "0000180d-0000-1000-8000-00805f9b34fb".to_owned(),
            }],
            name_prefix: Some("BleHub".to_owned()),
            company_id: Some(0x1234),
            allow_duplicates: true,
        },
    };
    let (backend, handle) = FakeBackend::scripted([EventScenario::accept(
        RecordedCommand::StartScan(expected.clone()),
    )]);
    let hub = BleHub::new(Arc::new(backend), Arc::new(RecordingSink::new())).unwrap();

    hub.start_scan(command).unwrap();

    assert_eq!(
        handle.commands(),
        vec![RecordedCommand::StartScan(expected)]
    );
}

#[test]
fn late_event_after_dispose_is_closed_and_cleanup_is_once() {
    let (backend, handle) = FakeBackend::scripted([]);
    let sink = RecordingSink::new();
    let hub = BleHub::new(Arc::new(backend), Arc::new(sink.clone())).unwrap();
    let callback = handle.ingress_for_callback().unwrap();
    let event = BleEvent::ConnectionStateChanged {
        command_id: None,
        connection_id: ConnectionId { value: 9 },
        result: ConnectionStateResult::Ok {
            state: ConnectionState::Disconnected,
        },
    };

    hub.dispose().unwrap();
    hub.dispose().unwrap();

    assert_eq!(callback.push(event), EventIngressStatus::Closed);
    assert!(sink.events().is_empty());
    assert_eq!(handle.dispose_count(), 1);
}
