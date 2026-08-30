mod support;

use std::sync::Arc;

use blehub::*;
use blehub_testkit::*;

use support::*;

#[test]
fn synchronous_events_before_return_are_delivered_and_projected() {
    let command_id = CommandId { value: 7 };
    let scan_session_id = ScanSessionId { value: 11 };
    let command = StartScanCommand {
        command_id,
        scan_session_id,
        options: ScanOptions::default(),
    };
    let events = vec![
        scan_started(command_id, scan_session_id),
        succeeded(command_id),
    ];
    let (backend, handle) = FakeBackend::scripted([EventScenario::emit_before_return(
        RecordedCommand::StartScan(command.clone()),
        events,
    )]);
    let sink = RecordingSink::new();
    let hub = BleHub::new(Arc::new(backend), Box::new(sink.clone())).unwrap();
    let mut projection = ReferenceProjection::default();
    assert!(projection.register_command(command_id));

    hub.start_scan(command).unwrap();

    let delivered = sink.events();
    assert_eq!(delivered.len(), 2);
    for event in &delivered {
        projection.apply(event);
    }
    assert_eq!(
        projection.outcome(command_id),
        Some(ProjectedCommandOutcome::Succeeded)
    );
    assert_eq!(
        handle.push_statuses(),
        vec![EventIngressStatus::Delivered, EventIngressStatus::Delivered]
    );
}

#[test]
fn passive_adapter_snapshot_and_unscoped_backend_error_are_delivered_as_facts() {
    let scenario = catalog::passive_adapter_and_backend_error();
    let (backend, handle) = FakeBackend::scripted(Vec::<EventScenario>::new());
    let sink = RecordingSink::new();
    let _hub = BleHub::new(Arc::new(backend), Box::new(sink.clone())).unwrap();
    let mut projection = ReferenceProjection::default();

    assert_eq!(
        handle.emit(scenario.adapter_changed.clone()),
        EventIngressStatus::Delivered
    );
    assert_eq!(
        projection.apply(&scenario.adapter_changed),
        EventDisposition::Accepted
    );
    assert_eq!(
        handle.emit(scenario.backend_error.clone()),
        EventIngressStatus::Delivered
    );
    assert_eq!(
        projection.apply(&scenario.backend_error),
        EventDisposition::Accepted
    );
    assert_eq!(
        sink.events(),
        vec![scenario.adapter_changed, scenario.backend_error]
    );
    assert!(handle.contract_violations().is_empty());
}

#[test]
fn passive_disconnect_wins_the_state_race_without_a_second_disconnected_event() {
    let scenario = catalog::passive_disconnect_race();
    let disconnect_id = scenario.disconnect.command_id;
    let (backend, handle) = FakeBackend::scripted(scenario.backend_scenarios.clone());
    let sink = RecordingSink::new();
    let hub = BleHub::new(Arc::new(backend), Box::new(sink.clone())).unwrap();
    let mut projection = ReferenceProjection::default();
    projection.activate_connection(scenario.disconnect.connection_id);
    assert!(projection.register(RecordedCommand::Disconnect(scenario.disconnect)));

    hub.disconnect(scenario.disconnect).unwrap();
    assert_eq!(
        handle.emit(scenario.passive_disconnected.clone()),
        EventIngressStatus::Delivered
    );
    assert_eq!(
        projection.apply(&scenario.passive_disconnected),
        EventDisposition::Accepted
    );
    assert_eq!(handle.advance_all(), vec![EventIngressStatus::Delivered]);
    assert_eq!(
        projection.apply(&succeeded(disconnect_id)),
        EventDisposition::Accepted
    );
    handle.finish_completion(disconnect_id, CompletionExpectation::SucceededWithoutDomain);

    assert_eq!(
        sink.events(),
        vec![scenario.passive_disconnected, succeeded(disconnect_id)]
    );
    assert!(handle.contract_violations().is_empty());
}

#[test]
fn notification_enable_value_disable_and_late_value_keep_contract_order() {
    let scenario = catalog::notification_lifecycle();
    let connection_id = scenario.enable.connection_id;
    let gatt_session_id = scenario.enable.gatt_session_id;
    let enable_id = scenario.enable.command_id;
    let disable_id = scenario.disable.command_id;
    let (backend, handle) = FakeBackend::scripted(scenario.backend_scenarios.clone());
    let sink = RecordingSink::new();
    let hub = BleHub::new(Arc::new(backend), Box::new(sink.clone())).unwrap();
    let mut projection = ReferenceProjection::default();
    projection.activate_connection(connection_id);
    projection.activate_gatt_session(connection_id, gatt_session_id);
    assert!(projection.register(RecordedCommand::SetNotifications(scenario.enable)));

    hub.set_notifications(scenario.enable).unwrap();
    assert_eq!(handle.advance(), Some(EventIngressStatus::Delivered));
    assert_eq!(
        projection.apply(&sink.events()[0]),
        EventDisposition::Accepted
    );
    assert_eq!(handle.advance(), Some(EventIngressStatus::Delivered));
    assert_eq!(
        projection.apply(&sink.events()[1]),
        EventDisposition::Accepted
    );
    handle.finish_completion(enable_id, CompletionExpectation::SucceededWithDomain);

    assert_eq!(
        handle.emit(scenario.value.clone()),
        EventIngressStatus::Delivered
    );
    assert_eq!(
        projection.apply(&sink.events()[2]),
        EventDisposition::Accepted
    );

    assert!(projection.register(RecordedCommand::SetNotifications(scenario.disable)));
    hub.set_notifications(scenario.disable).unwrap();
    assert_eq!(handle.advance(), Some(EventIngressStatus::Delivered));
    assert_eq!(
        projection.apply(&sink.events()[3]),
        EventDisposition::Accepted
    );
    assert_eq!(handle.advance(), Some(EventIngressStatus::Delivered));
    assert_eq!(
        projection.apply(&sink.events()[4]),
        EventDisposition::Accepted
    );
    handle.finish_completion(disable_id, CompletionExpectation::SucceededWithDomain);

    assert_eq!(
        handle.emit(scenario.value.clone()),
        EventIngressStatus::Delivered
    );
    assert_eq!(projection.apply(&sink.events()[5]), EventDisposition::Stale);
    assert_eq!(
        sink.events(),
        vec![
            scenario.enabled,
            succeeded(enable_id),
            scenario.value.clone(),
            scenario.disabled,
            succeeded(disable_id),
            scenario.value,
        ]
    );
    assert!(handle.contract_violations().is_empty());
}

#[test]
fn native_gatt_invalidation_rejects_old_handles_and_isolates_late_callbacks() {
    let scenario = catalog::gatt_invalidation_isolation();
    let old_discover_id = scenario.old_discover.command_id;
    let old_enable_id = scenario.old_enable.command_id;
    let rejected_old_read_id = scenario.rejected_old_read.command_id;
    let new_discover_id = scenario.new_discover.command_id;
    let new_read_id = scenario.new_read.command_id;
    let (backend, handle) = FakeBackend::scripted(scenario.backend_scenarios.clone());
    let sink = RecordingSink::new();
    let hub = BleHub::new(Arc::new(backend), Box::new(sink.clone())).unwrap();
    let mut projection = ReferenceProjection::default();
    projection.activate_connection(scenario.old_discover.connection_id);

    assert!(projection.register(RecordedCommand::DiscoverGatt(scenario.old_discover)));
    hub.discover_gatt(scenario.old_discover).unwrap();
    assert_eq!(handle.advance(), Some(EventIngressStatus::Delivered));
    assert_eq!(
        projection.apply(&scenario.old_database),
        EventDisposition::Accepted
    );
    assert_eq!(handle.advance(), Some(EventIngressStatus::Delivered));
    assert_eq!(
        projection.apply(&succeeded(old_discover_id)),
        EventDisposition::Accepted
    );
    handle.finish_completion(old_discover_id, CompletionExpectation::SucceededWithDomain);

    assert!(projection.register(RecordedCommand::SetNotifications(scenario.old_enable)));
    hub.set_notifications(scenario.old_enable).unwrap();
    assert_eq!(handle.advance(), Some(EventIngressStatus::Delivered));
    assert_eq!(
        projection.apply(&scenario.old_enabled),
        EventDisposition::Accepted
    );
    assert_eq!(handle.advance(), Some(EventIngressStatus::Delivered));
    assert_eq!(
        projection.apply(&succeeded(old_enable_id)),
        EventDisposition::Accepted
    );
    handle.finish_completion(old_enable_id, CompletionExpectation::SucceededWithDomain);

    assert!(projection.register(RecordedCommand::ReadCharacteristic(
        scenario.rejected_old_read
    )));
    assert_eq!(
        hub.read_characteristic(scenario.rejected_old_read),
        Err(scenario.invalidated_rejection.clone())
    );
    assert!(projection.dispatch_rejected(rejected_old_read_id, scenario.invalidated_rejection));
    projection.invalidate_gatt_session(
        scenario.rejected_old_read.connection_id,
        scenario.rejected_old_read.gatt_session_id,
    );

    assert!(projection.register(RecordedCommand::DiscoverGatt(scenario.new_discover)));
    hub.discover_gatt(scenario.new_discover).unwrap();
    assert_eq!(handle.advance(), Some(EventIngressStatus::Delivered));
    assert_eq!(
        projection.apply(&scenario.new_database),
        EventDisposition::Accepted
    );
    assert_eq!(handle.advance(), Some(EventIngressStatus::Delivered));
    assert_eq!(
        projection.apply(&succeeded(new_discover_id)),
        EventDisposition::Accepted
    );
    handle.finish_completion(new_discover_id, CompletionExpectation::SucceededWithDomain);

    assert!(projection.register(RecordedCommand::ReadCharacteristic(scenario.new_read)));
    hub.read_characteristic(scenario.new_read).unwrap();
    assert_eq!(
        handle.emit(scenario.late_old_value.clone()),
        EventIngressStatus::Delivered
    );
    assert_eq!(
        projection.apply(&scenario.late_old_value),
        EventDisposition::Stale
    );
    assert!(projection.is_pending(new_read_id));
    assert!(!handle.terminal_attempted(new_read_id));

    assert_eq!(handle.advance(), Some(EventIngressStatus::Delivered));
    assert_eq!(
        projection.apply(&scenario.new_read_value),
        EventDisposition::Accepted
    );
    assert_eq!(handle.advance(), Some(EventIngressStatus::Delivered));
    assert_eq!(
        projection.apply(&succeeded(new_read_id)),
        EventDisposition::Accepted
    );
    handle.finish_completion(new_read_id, CompletionExpectation::SucceededWithDomain);

    assert_eq!(
        scenario.rejected_old_read.characteristic_id,
        scenario.new_read.characteristic_id
    );
    assert_ne!(
        scenario.rejected_old_read.gatt_session_id,
        scenario.new_read.gatt_session_id
    );
    assert!(handle.contract_violations().is_empty());
}

#[test]
fn notification_values_keep_passive_semantics_across_enable_disable_and_read() {
    let scenario = catalog::notification_callback_timing();
    let enable_id = scenario.enable.command_id;
    let disable_id = scenario.disable.command_id;
    let read_id = scenario.read.command_id;
    let (backend, handle) = FakeBackend::scripted(scenario.backend_scenarios.clone());
    let sink = RecordingSink::new();
    let hub = BleHub::new(Arc::new(backend), Box::new(sink.clone())).unwrap();
    let mut projection = ReferenceProjection::default();
    projection.activate_connection(scenario.enable.connection_id);
    projection.activate_gatt_session(
        scenario.enable.connection_id,
        scenario.enable.gatt_session_id,
    );
    assert!(projection.register(RecordedCommand::SetNotifications(scenario.enable)));

    hub.set_notifications(scenario.enable).unwrap();
    assert_eq!(
        handle.emit(scenario.early_value.clone()),
        EventIngressStatus::Delivered
    );
    assert_eq!(
        projection.apply(&scenario.early_value),
        EventDisposition::Stale
    );
    assert!(projection.is_pending(enable_id));
    assert!(!handle.terminal_attempted(enable_id));

    assert_eq!(handle.advance(), Some(EventIngressStatus::Delivered));
    assert_eq!(
        projection.apply(&scenario.enabled),
        EventDisposition::Accepted
    );
    assert_eq!(handle.advance(), Some(EventIngressStatus::Delivered));
    assert_eq!(
        projection.apply(&succeeded(enable_id)),
        EventDisposition::Accepted
    );
    handle.finish_completion(enable_id, CompletionExpectation::SucceededWithDomain);

    assert!(projection.register(RecordedCommand::SetNotifications(scenario.disable)));
    hub.set_notifications(scenario.disable).unwrap();
    assert_eq!(
        handle.emit(scenario.disabling_value.clone()),
        EventIngressStatus::Delivered
    );
    assert_eq!(
        projection.apply(&scenario.disabling_value),
        EventDisposition::Accepted
    );
    assert!(!handle.terminal_attempted(disable_id));

    assert_eq!(handle.advance(), Some(EventIngressStatus::Delivered));
    assert_eq!(
        projection.apply(&scenario.disabled),
        EventDisposition::Accepted
    );
    assert_eq!(handle.advance(), Some(EventIngressStatus::Delivered));
    assert_eq!(
        projection.apply(&succeeded(disable_id)),
        EventDisposition::Accepted
    );
    handle.finish_completion(disable_id, CompletionExpectation::SucceededWithDomain);

    assert!(projection.register(RecordedCommand::ReadCharacteristic(scenario.read)));
    hub.read_characteristic(scenario.read).unwrap();
    assert_eq!(
        handle.emit(scenario.late_value.clone()),
        EventIngressStatus::Delivered
    );
    assert_eq!(
        projection.apply(&scenario.late_value),
        EventDisposition::Stale
    );
    assert!(projection.is_pending(read_id));
    assert!(!handle.terminal_attempted(read_id));

    assert_eq!(handle.advance(), Some(EventIngressStatus::Delivered));
    assert_eq!(
        projection.apply(&scenario.read_value),
        EventDisposition::Accepted
    );
    assert_eq!(handle.advance(), Some(EventIngressStatus::Delivered));
    assert_eq!(
        projection.apply(&succeeded(read_id)),
        EventDisposition::Accepted
    );
    handle.finish_completion(read_id, CompletionExpectation::SucceededWithDomain);

    assert_eq!(
        sink.events(),
        vec![
            scenario.early_value,
            scenario.enabled,
            succeeded(enable_id),
            scenario.disabling_value,
            scenario.disabled,
            succeeded(disable_id),
            scenario.late_value,
            scenario.read_value,
            succeeded(read_id),
        ]
    );
    assert!(handle.contract_violations().is_empty());
}

#[test]
fn failed_notification_enable_never_activates_passive_value_routing() {
    let scenario = catalog::failed_notification_enable();
    let command_id = scenario.enable.command_id;
    let (backend, handle) = FakeBackend::scripted(scenario.backend_scenarios.clone());
    let sink = RecordingSink::new();
    let hub = BleHub::new(Arc::new(backend), Box::new(sink.clone())).unwrap();
    let mut projection = ReferenceProjection::default();
    projection.activate_connection(scenario.enable.connection_id);
    projection.activate_gatt_session(
        scenario.enable.connection_id,
        scenario.enable.gatt_session_id,
    );
    assert!(projection.register(RecordedCommand::SetNotifications(scenario.enable)));

    hub.set_notifications(scenario.enable).unwrap();
    assert_eq!(
        handle.emit(scenario.early_value.clone()),
        EventIngressStatus::Delivered
    );
    assert_eq!(
        projection.apply(&scenario.early_value),
        EventDisposition::Stale
    );
    assert!(projection.is_pending(command_id));
    assert!(!handle.terminal_attempted(command_id));

    assert_eq!(handle.advance(), Some(EventIngressStatus::Delivered));
    assert_eq!(
        projection.apply(&scenario.terminal),
        EventDisposition::Accepted
    );
    handle.finish_completion(command_id, CompletionExpectation::Failed);

    assert_eq!(
        handle.emit(scenario.late_value.clone()),
        EventIngressStatus::Delivered
    );
    assert_eq!(
        projection.apply(&scenario.late_value),
        EventDisposition::Stale
    );
    assert_eq!(
        projection.outcome(command_id),
        match &scenario.terminal {
            BleEvent::CommandCompleted {
                error: Some(error), ..
            } => Some(ProjectedCommandOutcome::Failed(error.clone())),
            _ => unreachable!(),
        }
    );
    assert_eq!(
        sink.events(),
        vec![scenario.early_value, scenario.terminal, scenario.late_value,]
    );
    assert!(handle.contract_violations().is_empty());
}
