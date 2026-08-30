mod support;

use std::sync::Arc;

use blehub::*;
use blehub_testkit::*;

use support::*;

#[test]
fn full_success_flow_uses_every_non_notification_command_through_the_real_ingress() {
    let initialize_id = CommandId { value: 1 };
    let start_id = CommandId { value: 2 };
    let stop_id = CommandId { value: 3 };
    let connect_id = CommandId { value: 4 };
    let discover_id = CommandId { value: 5 };
    let read_id = CommandId { value: 6 };
    let write_with_response_id = CommandId { value: 7 };
    let write_without_response_id = CommandId { value: 8 };
    let rssi_id = CommandId { value: 9 };
    let disconnect_id = CommandId { value: 10 };
    let scan_session_id = ScanSessionId { value: 100 };
    let peripheral_id = PeripheralId { value: 200 };
    let connection_id = ConnectionId { value: 300 };
    let gatt_session_id = GattSessionId { value: 400 };

    let initialize = InitializeCommand {
        command_id: initialize_id,
    };
    let start = StartScanCommand {
        command_id: start_id,
        scan_session_id,
        options: ScanOptions::default(),
    };
    let stop = StopScanCommand {
        command_id: stop_id,
        scan_session_id,
    };
    let connect = ConnectCommand {
        command_id: connect_id,
        connection_id,
        peripheral_id,
    };
    let mut database = duplicate_gatt_database_fixture();
    database.connection_id = connection_id;
    database.gatt_session_id = gatt_session_id;
    let characteristic_id = database.services[0].characteristics[0].id;
    let discover = DiscoverGattCommand {
        command_id: discover_id,
        connection_id,
        gatt_session_id,
    };
    let read = ReadCharacteristicCommand {
        command_id: read_id,
        connection_id,
        gatt_session_id,
        characteristic_id,
    };
    let write_with_response = WriteCharacteristicCommand {
        command_id: write_with_response_id,
        connection_id,
        gatt_session_id,
        characteristic_id,
        value: vec![1],
        write_type: WriteType::WithResponse,
    };
    let write_without_response = WriteCharacteristicCommand {
        command_id: write_without_response_id,
        value: vec![2],
        write_type: WriteType::WithoutResponse,
        ..write_with_response.clone()
    };
    let read_rssi = ReadRssiCommand {
        command_id: rssi_id,
        connection_id,
    };
    let disconnect = DisconnectCommand {
        command_id: disconnect_id,
        connection_id,
    };

    let (backend, handle) = FakeBackend::scripted([
        EventScenario::complete(
            RecordedCommand::Initialize(initialize),
            vec![
                BleEvent::AdapterSnapshotChanged {
                    command_id: Some(initialize_id),
                    snapshot: AdapterSnapshot {
                        state: AdapterState::PoweredOn,
                        scan_authorization: AuthorizationState::Authorized,
                        connection_authorization: AuthorizationState::Authorized,
                        name: Some("Test Adapter".to_owned()),
                    },
                },
                succeeded(initialize_id),
            ],
        ),
        EventScenario::complete(
            RecordedCommand::StartScan(start.clone()),
            vec![scan_started(start_id, scan_session_id), succeeded(start_id)],
        ),
        EventScenario::complete(
            RecordedCommand::StopScan(stop),
            vec![
                BleEvent::ScanStopped {
                    scan_id: scan_session_id,
                    result: ScanStopResult::Ok {
                        reason: ScanStopReason::Requested {
                            command_id: stop_id,
                        },
                    },
                },
                succeeded(stop_id),
            ],
        ),
        EventScenario::complete(
            RecordedCommand::Connect(connect),
            vec![
                BleEvent::ConnectionStateChanged {
                    command_id: Some(connect_id),
                    connection_id,
                    result: ConnectionStateResult::Ok {
                        state: ConnectionState::Connected,
                    },
                },
                succeeded(connect_id),
            ],
        ),
        EventScenario::complete(
            RecordedCommand::DiscoverGatt(discover),
            vec![
                BleEvent::GattDiscovered {
                    command_id: discover_id,
                    database: database.clone(),
                },
                succeeded(discover_id),
            ],
        ),
        EventScenario::complete(
            RecordedCommand::ReadCharacteristic(read),
            vec![
                BleEvent::CharacteristicRead {
                    command_id: read_id,
                    connection_id,
                    gatt_session_id,
                    characteristic_id,
                    value: vec![3],
                },
                succeeded(read_id),
            ],
        ),
        EventScenario::complete(
            RecordedCommand::WriteCharacteristic(write_with_response.clone()),
            vec![
                BleEvent::CharacteristicWritten {
                    command_id: write_with_response_id,
                    connection_id,
                    gatt_session_id,
                    characteristic_id,
                },
                succeeded(write_with_response_id),
            ],
        ),
        EventScenario::complete(
            RecordedCommand::WriteCharacteristic(write_without_response.clone()),
            vec![
                BleEvent::CharacteristicWritten {
                    command_id: write_without_response_id,
                    connection_id,
                    gatt_session_id,
                    characteristic_id,
                },
                succeeded(write_without_response_id),
            ],
        ),
        EventScenario::complete(
            RecordedCommand::ReadRssi(read_rssi),
            vec![
                BleEvent::RssiRead {
                    command_id: rssi_id,
                    connection_id,
                    rssi: -42,
                },
                succeeded(rssi_id),
            ],
        ),
        EventScenario::complete(
            RecordedCommand::Disconnect(disconnect),
            vec![
                BleEvent::ConnectionStateChanged {
                    command_id: Some(disconnect_id),
                    connection_id,
                    result: ConnectionStateResult::Ok {
                        state: ConnectionState::Disconnected,
                    },
                },
                succeeded(disconnect_id),
            ],
        ),
    ]);
    let sink = RecordingSink::new();
    let hub = BleHub::new(Arc::new(backend), Box::new(sink.clone())).unwrap();

    hub.initialize(initialize).unwrap();
    advance_success(&handle, initialize_id);
    hub.start_scan(start.clone()).unwrap();
    advance_success(&handle, start_id);
    let mut advertisement = advertisement_observation_fixture();
    advertisement.scan_session_id = scan_session_id;
    advertisement.peripheral_id = peripheral_id;
    assert_eq!(
        handle.emit(BleEvent::AdvertisementObserved { advertisement }),
        EventIngressStatus::Delivered
    );
    hub.stop_scan(stop).unwrap();
    advance_success(&handle, stop_id);
    hub.connect(connect).unwrap();
    advance_success(&handle, connect_id);
    hub.discover_gatt(discover).unwrap();
    advance_success(&handle, discover_id);
    hub.read_characteristic(read).unwrap();
    advance_success(&handle, read_id);
    hub.write_characteristic(write_with_response.clone())
        .unwrap();
    advance_success(&handle, write_with_response_id);
    hub.write_characteristic(write_without_response.clone())
        .unwrap();
    advance_success(&handle, write_without_response_id);
    hub.read_rssi(read_rssi).unwrap();
    advance_success(&handle, rssi_id);
    hub.disconnect(disconnect).unwrap();
    advance_success(&handle, disconnect_id);

    assert_eq!(sink.events().len(), 21);
    assert_eq!(
        handle.commands(),
        vec![
            RecordedCommand::Initialize(initialize),
            RecordedCommand::StartScan(start),
            RecordedCommand::StopScan(stop),
            RecordedCommand::Connect(connect),
            RecordedCommand::DiscoverGatt(discover),
            RecordedCommand::ReadCharacteristic(read),
            RecordedCommand::WriteCharacteristic(write_with_response),
            RecordedCommand::WriteCharacteristic(write_without_response),
            RecordedCommand::ReadRssi(read_rssi),
            RecordedCommand::Disconnect(disconnect),
        ]
    );
    assert!(handle.contract_violations().is_empty());
}

#[test]
fn initialization_requires_one_successful_attempt_and_reports_the_initial_snapshot() {
    let scenario = catalog::initialization_rules();
    let initialize_id = scenario.initialize.command_id;
    let (backend, handle) = FakeBackend::scripted(scenario.backend_scenarios.clone());
    let sink = RecordingSink::new();
    let hub = BleHub::new(Arc::new(backend), Box::new(sink.clone())).unwrap();

    assert_eq!(
        hub.start_scan(scenario.pre_initialize_scan),
        Err(scenario.rejection.clone())
    );
    assert!(sink.events().is_empty());

    hub.initialize(scenario.initialize).unwrap();
    assert_eq!(handle.advance_all(), vec![EventIngressStatus::Delivered; 2]);
    handle.finish_completion(initialize_id, CompletionExpectation::SucceededWithDomain);

    assert_eq!(
        hub.initialize(scenario.repeated_initialize),
        Err(scenario.rejection)
    );
    assert_eq!(
        sink.events(),
        vec![scenario.snapshot, succeeded(initialize_id)]
    );
    assert!(handle.contract_violations().is_empty());
}

#[test]
fn failed_initialization_cannot_be_retried_in_the_same_backend_lifecycle() {
    let scenario = catalog::failed_initialization_cannot_retry();
    let initialize_id = scenario.initialize.command_id;
    let (backend, handle) = FakeBackend::scripted(scenario.backend_scenarios.clone());
    let sink = RecordingSink::new();
    let hub = BleHub::new(Arc::new(backend), Box::new(sink.clone())).unwrap();

    hub.initialize(scenario.initialize).unwrap();
    assert_eq!(handle.advance_all(), vec![EventIngressStatus::Delivered]);
    handle.finish_completion(initialize_id, CompletionExpectation::Failed);

    assert_eq!(
        hub.initialize(scenario.retry),
        Err(scenario.retry_rejection)
    );
    assert_eq!(sink.events(), vec![scenario.terminal]);
    assert!(handle.contract_violations().is_empty());
}

#[test]
fn scan_exclusivity_allows_dispatch_rejection_or_terminal_failure_and_passive_stop() {
    let scenario = catalog::scan_exclusivity();
    let first_id = scenario.first.command_id;
    let first_scan_id = scenario.first.scan_session_id;
    let accepted_conflict_id = scenario.accepted_conflict.command_id;
    let (backend, handle) = FakeBackend::scripted(scenario.backend_scenarios.clone());
    let sink = RecordingSink::new();
    let hub = BleHub::new(Arc::new(backend), Box::new(sink.clone())).unwrap();

    hub.start_scan(scenario.first).unwrap();
    assert_eq!(handle.advance_all(), vec![EventIngressStatus::Delivered; 2]);
    handle.finish_completion(first_id, CompletionExpectation::SucceededWithDomain);

    assert_eq!(
        hub.start_scan(scenario.rejected_conflict),
        Err(scenario.rejection)
    );
    assert_eq!(sink.events().len(), 2);

    hub.start_scan(scenario.accepted_conflict).unwrap();
    assert_eq!(handle.advance_all(), vec![EventIngressStatus::Delivered]);
    handle.finish_completion(accepted_conflict_id, CompletionExpectation::Failed);

    assert_eq!(
        handle.emit(scenario.suspended.clone()),
        EventIngressStatus::Delivered
    );
    assert_eq!(
        sink.events(),
        vec![
            scan_started(first_id, first_scan_id),
            succeeded(first_id),
            scenario.accepted_failure,
            scenario.suspended,
        ]
    );
    assert!(handle.contract_violations().is_empty());
}

#[test]
fn advertisement_identity_is_stable_across_scans_without_merging_observations() {
    let scenario = catalog::advertisement_identity_across_scans();
    let first_start_id = scenario.first_start.command_id;
    let first_stop_id = scenario.first_stop.command_id;
    let second_start_id = scenario.second_start.command_id;
    let (backend, handle) = FakeBackend::scripted(scenario.backend_scenarios.clone());
    let sink = RecordingSink::new();
    let hub = BleHub::new(Arc::new(backend), Box::new(sink.clone())).unwrap();

    hub.start_scan(scenario.first_start).unwrap();
    assert_eq!(handle.advance_all(), vec![EventIngressStatus::Delivered; 2]);
    handle.finish_completion(first_start_id, CompletionExpectation::SucceededWithDomain);
    assert_eq!(
        handle.emit(BleEvent::AdvertisementObserved {
            advertisement: scenario.first_observation.clone(),
        }),
        EventIngressStatus::Delivered
    );

    hub.stop_scan(scenario.first_stop).unwrap();
    assert_eq!(handle.advance_all(), vec![EventIngressStatus::Delivered; 2]);
    handle.finish_completion(first_stop_id, CompletionExpectation::SucceededWithDomain);

    hub.start_scan(scenario.second_start).unwrap();
    assert_eq!(handle.advance_all(), vec![EventIngressStatus::Delivered; 2]);
    handle.finish_completion(second_start_id, CompletionExpectation::SucceededWithDomain);
    assert_eq!(
        handle.emit(BleEvent::AdvertisementObserved {
            advertisement: scenario.second_observation.clone(),
        }),
        EventIngressStatus::Delivered
    );

    assert_eq!(
        scenario.first_observation.peripheral_id,
        scenario.second_observation.peripheral_id
    );
    assert_ne!(
        scenario.first_observation.scan_session_id,
        scenario.second_observation.scan_session_id
    );
    assert!(scenario.second_observation.local_name.is_none());
    assert!(scenario.second_observation.advertised_services.is_empty());
    assert!(scenario.second_observation.manufacturer_data.is_empty());
    assert!(scenario.second_observation.service_data.is_empty());
    assert!(scenario.second_observation.rssi.is_none());
    assert!(scenario.second_observation.connectable.is_none());
    let events = sink.events();
    assert_eq!(
        events[2],
        BleEvent::AdvertisementObserved {
            advertisement: scenario.first_observation,
        }
    );
    assert_eq!(
        events[7],
        BleEvent::AdvertisementObserved {
            advertisement: scenario.second_observation,
        }
    );
    assert!(handle.contract_violations().is_empty());
}

#[test]
fn one_peripheral_connection_preserves_ids_across_both_conflict_paths() {
    let scenario = catalog::connection_exclusivity();
    let first_id = scenario.first.command_id;
    let accepted_conflict_id = scenario.accepted_conflict.command_id;
    let expected_commands = vec![
        RecordedCommand::Connect(scenario.first),
        RecordedCommand::Connect(scenario.rejected_conflict),
        RecordedCommand::Connect(scenario.accepted_conflict),
    ];
    let (backend, handle) = FakeBackend::scripted(scenario.backend_scenarios.clone());
    let sink = RecordingSink::new();
    let hub = BleHub::new(Arc::new(backend), Box::new(sink.clone())).unwrap();

    hub.connect(scenario.first).unwrap();
    assert_eq!(handle.advance_all(), vec![EventIngressStatus::Delivered; 2]);
    handle.finish_completion(first_id, CompletionExpectation::SucceededWithDomain);

    assert_eq!(
        hub.connect(scenario.rejected_conflict),
        Err(scenario.rejection)
    );
    assert_eq!(sink.events().len(), 2);

    hub.connect(scenario.accepted_conflict).unwrap();
    assert_eq!(handle.advance_all(), vec![EventIngressStatus::Delivered]);
    handle.finish_completion(accepted_conflict_id, CompletionExpectation::Failed);

    assert_eq!(
        sink.events(),
        vec![
            scenario.connected,
            succeeded(first_id),
            scenario.accepted_failure,
        ]
    );
    assert_eq!(handle.commands(), expected_commands);
    assert!(handle.contract_violations().is_empty());
}

#[test]
fn proven_pending_connect_cancellation_uses_cancelled_for_the_original_command() {
    let scenario = catalog::proven_connect_cancellation();
    let connect_id = scenario.connect.command_id;
    let disconnect_id = scenario.disconnect.command_id;
    let (backend, handle) = FakeBackend::scripted(scenario.backend_scenarios.clone());
    let sink = RecordingSink::new();
    let hub = BleHub::new(Arc::new(backend), Box::new(sink.clone())).unwrap();

    hub.connect(scenario.connect).unwrap();
    assert!(handle.advance_all().is_empty());

    hub.disconnect(scenario.disconnect).unwrap();
    assert_eq!(handle.advance_all(), vec![EventIngressStatus::Delivered; 3]);
    handle.finish_completion(connect_id, CompletionExpectation::Failed);
    handle.finish_completion(disconnect_id, CompletionExpectation::SucceededWithDomain);

    assert!(matches!(
        &scenario.connect_terminal,
        BleEvent::CommandCompleted {
            error: Some(BleEventError {
                category: BleErrorCategory::Cancelled,
                ..
            }),
            ..
        }
    ));
    assert_eq!(
        sink.events(),
        vec![
            scenario.connect_terminal,
            scenario.disconnected,
            succeeded(disconnect_id),
        ]
    );
    assert!(handle.contract_violations().is_empty());
}

#[test]
fn independent_pending_connect_failure_is_not_relabelled_as_cancelled() {
    let scenario = catalog::independent_connect_failure();
    let connect_id = scenario.connect.command_id;
    let disconnect_id = scenario.disconnect.command_id;
    let (backend, handle) = FakeBackend::scripted(scenario.backend_scenarios.clone());
    let sink = RecordingSink::new();
    let hub = BleHub::new(Arc::new(backend), Box::new(sink.clone())).unwrap();

    hub.connect(scenario.connect).unwrap();
    hub.disconnect(scenario.disconnect).unwrap();
    assert_eq!(handle.advance_all(), vec![EventIngressStatus::Delivered; 4]);
    handle.finish_completion(connect_id, CompletionExpectation::Failed);
    handle.finish_completion(disconnect_id, CompletionExpectation::SucceededWithDomain);

    assert!(matches!(
        &scenario.connect_terminal,
        BleEvent::CommandCompleted {
            error: Some(BleEventError {
                category: BleErrorCategory::Connection,
                ..
            }),
            ..
        }
    ));
    assert_eq!(
        sink.events(),
        vec![
            scenario.connect_failed,
            scenario.connect_terminal,
            scenario.disconnected,
            succeeded(disconnect_id),
        ]
    );
    assert!(handle.contract_violations().is_empty());
}

#[test]
fn unconfirmed_connect_cancellation_leaves_the_connect_pending_and_late() {
    let scenario = catalog::unconfirmed_connect_cancellation();
    let connect_id = scenario.connect.command_id;
    let disconnect_id = scenario.disconnect.command_id;
    let (backend, handle) = FakeBackend::scripted(scenario.backend_scenarios.clone());
    let sink = RecordingSink::new();
    let hub = BleHub::new(Arc::new(backend), Box::new(sink.clone())).unwrap();
    let mut projection = ReferenceProjection::default();
    assert!(projection.register(RecordedCommand::Connect(scenario.connect)));
    assert!(projection.register(RecordedCommand::Disconnect(scenario.disconnect)));

    hub.connect(scenario.connect).unwrap();
    hub.disconnect(scenario.disconnect).unwrap();
    assert_eq!(handle.advance_all(), vec![EventIngressStatus::Delivered; 2]);
    handle.finish_completion(disconnect_id, CompletionExpectation::SucceededWithDomain);
    assert_eq!(
        projection.apply(&scenario.disconnected),
        EventDisposition::Accepted
    );
    assert_eq!(
        projection.apply(&succeeded(disconnect_id)),
        EventDisposition::Accepted
    );

    assert!(projection.timeout(connect_id));
    assert_eq!(
        handle.emit(scenario.late_connected.clone()),
        EventIngressStatus::Delivered
    );
    assert_eq!(
        handle.emit(scenario.late_terminal.clone()),
        EventIngressStatus::Delivered
    );
    handle.finish_completion(connect_id, CompletionExpectation::SucceededWithDomain);

    assert_eq!(
        projection.apply(&scenario.late_connected),
        EventDisposition::Late
    );
    assert_eq!(
        projection.apply(&scenario.late_terminal),
        EventDisposition::Late
    );
    assert_eq!(
        projection.outcome(connect_id),
        Some(ProjectedCommandOutcome::TimedOut)
    );
    assert_eq!(
        sink.events(),
        vec![
            scenario.disconnected,
            succeeded(disconnect_id),
            scenario.late_connected,
            scenario.late_terminal,
        ]
    );
    assert!(handle.contract_violations().is_empty());
}

#[test]
fn completed_repeated_and_unknown_disconnects_have_distinct_dispatch_results() {
    let scenario = catalog::disconnect_cleanup();
    let disconnect_id = scenario.disconnect.command_id;
    let (backend, handle) = FakeBackend::scripted(scenario.backend_scenarios.clone());
    let sink = RecordingSink::new();
    let hub = BleHub::new(Arc::new(backend), Box::new(sink.clone())).unwrap();

    hub.disconnect(scenario.disconnect).unwrap();
    assert_eq!(handle.advance_all(), vec![EventIngressStatus::Delivered; 2]);
    handle.finish_completion(disconnect_id, CompletionExpectation::SucceededWithDomain);

    assert_eq!(
        hub.disconnect(scenario.repeated),
        Err(scenario.rejection.clone())
    );
    assert_eq!(hub.disconnect(scenario.unknown), Err(scenario.rejection));
    assert_eq!(
        sink.events(),
        vec![scenario.disconnected, succeeded(disconnect_id)]
    );
    assert!(handle.contract_violations().is_empty());
}

#[test]
fn repeated_stop_succeeds_without_a_second_scan_stopped_event() {
    let scenario = catalog::repeated_stop();
    let first_id = scenario.first.command_id;
    let repeated_id = scenario.repeated.command_id;
    let (backend, handle) = FakeBackend::scripted(scenario.backend_scenarios.clone());
    let sink = RecordingSink::new();
    let hub = BleHub::new(Arc::new(backend), Box::new(sink.clone())).unwrap();

    hub.stop_scan(scenario.first).unwrap();
    assert_eq!(handle.advance_all(), vec![EventIngressStatus::Delivered; 2]);
    handle.finish_completion(first_id, CompletionExpectation::SucceededWithDomain);

    hub.stop_scan(scenario.repeated).unwrap();
    assert_eq!(handle.advance_all(), vec![EventIngressStatus::Delivered]);
    handle.finish_completion(repeated_id, CompletionExpectation::SucceededWithoutDomain);

    assert_eq!(
        sink.events(),
        vec![
            scenario.stopped,
            succeeded(first_id),
            succeeded(repeated_id)
        ]
    );
    assert!(handle.contract_violations().is_empty());
}

#[test]
fn gatt_discovery_is_complete_database_or_terminal_only_failure() {
    let scenario = catalog::gatt_discovery_all_or_error();
    let success_id = scenario.success.command_id;
    let failure_id = scenario.failure.command_id;
    let (backend, handle) = FakeBackend::scripted(scenario.backend_scenarios.clone());
    let sink = RecordingSink::new();
    let hub = BleHub::new(Arc::new(backend), Box::new(sink.clone())).unwrap();

    hub.discover_gatt(scenario.success).unwrap();
    assert_eq!(handle.advance_all(), vec![EventIngressStatus::Delivered; 2]);
    handle.finish_completion(success_id, CompletionExpectation::SucceededWithDomain);

    hub.discover_gatt(scenario.failure).unwrap();
    assert_eq!(handle.advance_all(), vec![EventIngressStatus::Delivered]);
    handle.finish_completion(failure_id, CompletionExpectation::Failed);

    assert_eq!(
        sink.events(),
        vec![
            scenario.discovered,
            succeeded(success_id),
            scenario.failure_terminal,
        ]
    );
    assert!(handle.contract_violations().is_empty());
}

#[test]
fn invalidated_gatt_handle_rejects_before_acceptance_with_zero_events() {
    let command = ReadCharacteristicCommand {
        command_id: CommandId { value: 1 },
        connection_id: ConnectionId { value: 2 },
        gatt_session_id: GattSessionId { value: 3 },
        characteristic_id: GattCharacteristicId { value: 4 },
    };
    let error = CommandDispatchError::InvalidState {
        detail: "GATT session has been invalidated".to_owned(),
    };
    let (backend, handle) = FakeBackend::scripted([EventScenario::reject(
        RecordedCommand::ReadCharacteristic(command),
        error.clone(),
    )]);
    let sink = RecordingSink::new();
    let hub = BleHub::new(Arc::new(backend), Box::new(sink.clone())).unwrap();

    assert_eq!(hub.read_characteristic(command), Err(error));
    assert!(sink.events().is_empty());
    assert_eq!(
        handle.commands(),
        vec![RecordedCommand::ReadCharacteristic(command)]
    );
    assert!(handle.contract_violations().is_empty());
}

#[test]
fn read_and_subscription_lifecycles_are_mutually_exclusive_until_disable_completes() {
    let scenario = catalog::read_subscription_conflicts();
    let pending_read_id = scenario.pending_read.command_id;
    let enable_id = scenario.enable.command_id;
    let disable_id = scenario.disable.command_id;
    let (backend, handle) = FakeBackend::scripted(scenario.backend_scenarios.clone());
    let sink = RecordingSink::new();
    let hub = BleHub::new(Arc::new(backend), Box::new(sink.clone())).unwrap();

    hub.read_characteristic(scenario.pending_read).unwrap();
    assert_eq!(
        hub.set_notifications(scenario.enable_during_pending_read),
        Err(scenario.overlap_rejection.clone())
    );
    assert!(sink.events().is_empty());
    assert_eq!(handle.advance_all(), vec![EventIngressStatus::Delivered; 2]);
    handle.finish_completion(pending_read_id, CompletionExpectation::SucceededWithDomain);

    hub.set_notifications(scenario.enable).unwrap();
    assert_eq!(
        hub.read_characteristic(scenario.read_during_pending_enable),
        Err(scenario.overlap_rejection.clone())
    );
    assert_eq!(sink.events().len(), 2);
    assert_eq!(handle.advance_all(), vec![EventIngressStatus::Delivered; 2]);
    handle.finish_completion(enable_id, CompletionExpectation::SucceededWithDomain);

    assert_eq!(
        hub.read_characteristic(scenario.read_during_active_subscription),
        Err(scenario.overlap_rejection.clone())
    );
    assert_eq!(sink.events().len(), 4);

    hub.set_notifications(scenario.disable).unwrap();
    assert_eq!(
        hub.read_characteristic(scenario.read_during_pending_disable),
        Err(scenario.overlap_rejection)
    );
    assert_eq!(sink.events().len(), 4);
    assert_eq!(handle.advance_all(), vec![EventIngressStatus::Delivered; 2]);
    handle.finish_completion(disable_id, CompletionExpectation::SucceededWithDomain);

    assert_eq!(
        hub.set_notifications(scenario.repeated_disable),
        Err(scenario.inactive_rejection.clone())
    );
    assert_eq!(
        hub.set_notifications(scenario.unknown_disable),
        Err(scenario.inactive_rejection)
    );
    assert_eq!(
        sink.events(),
        vec![
            scenario.read_completed,
            succeeded(pending_read_id),
            scenario.enabled,
            succeeded(enable_id),
            scenario.disabled,
            succeeded(disable_id),
        ]
    );
    assert!(handle.contract_violations().is_empty());
}

#[test]
fn read_after_disable_may_be_allowed_or_conservatively_rejected() {
    let scenario = catalog::read_after_disable();
    let allowed_id = scenario.allowed.command_id;
    let (allowed_backend, allowed_handle) =
        FakeBackend::scripted(scenario.allowed_backend_scenarios.clone());
    let allowed_sink = RecordingSink::new();
    let allowed_hub =
        BleHub::new(Arc::new(allowed_backend), Box::new(allowed_sink.clone())).unwrap();

    allowed_hub.read_characteristic(scenario.allowed).unwrap();
    assert_eq!(
        allowed_handle.advance_all(),
        vec![EventIngressStatus::Delivered; 2]
    );
    allowed_handle.finish_completion(allowed_id, CompletionExpectation::SucceededWithDomain);
    assert_eq!(
        allowed_sink.events(),
        vec![scenario.allowed_event, succeeded(allowed_id)]
    );
    assert!(allowed_handle.contract_violations().is_empty());

    let (conservative_backend, conservative_handle) =
        FakeBackend::scripted(scenario.conservative_backend_scenarios.clone());
    let conservative_sink = RecordingSink::new();
    let conservative_hub = BleHub::new(
        Arc::new(conservative_backend),
        Box::new(conservative_sink.clone()),
    )
    .unwrap();

    assert_eq!(
        conservative_hub.read_characteristic(scenario.conservative),
        Err(scenario.conservative_rejection)
    );
    assert!(conservative_sink.events().is_empty());
    assert!(conservative_handle.contract_violations().is_empty());
}

#[test]
fn capability_dependent_operations_use_terminal_events_instead_of_dispatch_rejection() {
    let capabilities = BackendCapabilities {
        supports_read_rssi: false,
        supports_required_notification: false,
        supports_required_indication: false,
        reports_actual_notification_mode: false,
        ..BackendCapabilities::new(Platform::Test)
    };
    let connection_id = ConnectionId { value: 2 };
    let gatt_session_id = GattSessionId { value: 3 };
    let characteristic_id = GattCharacteristicId { value: 4 };
    let unsupported_rssi = ReadRssiCommand {
        command_id: CommandId { value: 5 },
        connection_id,
    };
    let strict_notification = SetNotificationsCommand {
        command_id: CommandId { value: 6 },
        connection_id,
        gatt_session_id,
        subscription_id: SubscriptionId { value: 7 },
        characteristic_id,
        action: NotificationAction::Enable {
            mode: NotificationModeRequest::RequireIndication,
        },
    };
    let default_notification = SetNotificationsCommand {
        command_id: CommandId { value: 8 },
        subscription_id: SubscriptionId { value: 9 },
        action: NotificationAction::Enable {
            mode: NotificationModeRequest::PlatformDefault,
        },
        ..strict_notification
    };
    let unsupported = |command_id| BleEvent::CommandCompleted {
        command_id,
        error: Some(BleEventError {
            category: BleErrorCategory::NotSupported,
            message: "capability is not supported".to_owned(),
            platform_details: None,
        }),
    };
    let enabled_without_actual_mode = BleEvent::NotificationStateChanged {
        command_id: default_notification.command_id,
        connection_id,
        gatt_session_id,
        subscription_id: default_notification.subscription_id,
        characteristic_id,
        state: NotificationState::Enabled { actual_mode: None },
    };
    let (backend, handle) = FakeBackend::scripted_with_capabilities(
        capabilities.clone(),
        [
            EventScenario::complete(
                RecordedCommand::ReadRssi(unsupported_rssi),
                vec![unsupported(unsupported_rssi.command_id)],
            ),
            EventScenario::complete(
                RecordedCommand::SetNotifications(strict_notification),
                vec![unsupported(strict_notification.command_id)],
            ),
            EventScenario::complete(
                RecordedCommand::SetNotifications(default_notification),
                vec![
                    enabled_without_actual_mode.clone(),
                    succeeded(default_notification.command_id),
                ],
            ),
        ],
    );
    let sink = RecordingSink::new();
    let hub = BleHub::new(Arc::new(backend), Box::new(sink.clone())).unwrap();
    assert_eq!(hub.capabilities(), capabilities);

    hub.read_rssi(unsupported_rssi).unwrap();
    assert_eq!(handle.advance_all(), vec![EventIngressStatus::Delivered]);
    handle.finish_completion(unsupported_rssi.command_id, CompletionExpectation::Failed);

    hub.set_notifications(strict_notification).unwrap();
    assert_eq!(handle.advance_all(), vec![EventIngressStatus::Delivered]);
    handle.finish_completion(
        strict_notification.command_id,
        CompletionExpectation::Failed,
    );

    hub.set_notifications(default_notification).unwrap();
    assert_eq!(handle.advance_all(), vec![EventIngressStatus::Delivered; 2]);
    handle.finish_completion(
        default_notification.command_id,
        CompletionExpectation::SucceededWithDomain,
    );

    assert_eq!(sink.events().len(), 4);
    assert_eq!(sink.events()[2], enabled_without_actual_mode);
    assert!(handle.contract_violations().is_empty());
}
