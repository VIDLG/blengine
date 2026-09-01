use std::cell::{Cell, RefCell};

use blehub::{
    BluetoothUuid, CharacteristicProperties, CommandId, ConnectionId, ConnectionState,
    ConnectionStateResult, GattCharacteristic, GattCharacteristicId, GattDatabase, GattService,
    GattServiceId, GattSessionId, PeripheralId,
};

use super::{discovery::*, io::*, *};

#[test]
fn reservation_precedes_native_start_and_rejection_never_starts_native_work() {
    let calls = RefCell::new(Vec::new());
    let accepted = reserve_then_start(
        || {
            calls.borrow_mut().push("reserve");
            Ok(41_u64)
        },
        |reservation| {
            calls.borrow_mut().push("start");
            Ok::<_, &'static str>(reservation + 1)
        },
    );
    assert_eq!(calls.into_inner(), vec!["reserve", "start"]);
    assert_eq!(accepted, Ok((41, Ok(42))));

    let native_started = Cell::new(false);
    let rejected = reserve_then_start::<(), (), ()>(
        || {
            Err(CommandDispatchError::InvalidState {
                detail: "not connected".to_owned(),
            })
        },
        |_| {
            native_started.set(true);
            Ok(())
        },
    );
    assert!(matches!(
        rejected,
        Err(CommandDispatchError::InvalidState { .. })
    ));
    assert!(!native_started.get());
}

#[test]
fn attribute_ids_are_monotonic_across_kinds_and_checked_at_overflow() {
    let mut ids = MonotonicIdAllocator::new();
    assert_eq!(ids.allocate_many(2), Some(1..3));
    assert_eq!(ids.allocate_one(), Some(3));

    ids.next = u64::MAX;
    assert_eq!(ids.allocate_one(), None);
    assert_eq!(ids.next, u64::MAX);
}

#[test]
fn discovery_events_publish_complete_duplicate_instances_before_terminal() {
    let command = DiscoverGattCommand {
        command_id: CommandId { value: 7 },
        connection_id: ConnectionId { value: 8 },
        gatt_session_id: GattSessionId { value: 9 },
    };
    let duplicate_uuid = BluetoothUuid {
        value: "fff10000-0000-4000-8000-000000000000".to_owned(),
    };
    let properties = CharacteristicProperties {
        readable: true,
        writable_with_response: false,
        writable_without_response: false,
        notifiable: true,
        indicatable: true,
    };
    let database = GattDatabase {
        connection_id: command.connection_id,
        gatt_session_id: command.gatt_session_id,
        services: vec![GattService {
            id: GattServiceId { value: 10 },
            uuid: BluetoothUuid {
                value: "fff00000-0000-4000-8000-000000000000".to_owned(),
            },
            primary: Some(true),
            characteristics: vec![
                GattCharacteristic {
                    id: GattCharacteristicId { value: 11 },
                    uuid: duplicate_uuid.clone(),
                    properties,
                    descriptors: Vec::new(),
                },
                GattCharacteristic {
                    id: GattCharacteristicId { value: 12 },
                    uuid: duplicate_uuid,
                    properties,
                    descriptors: Vec::new(),
                },
            ],
        }],
    };

    let events = discovery_success_events(command, database.clone());
    assert_eq!(events.len(), 2);
    assert_eq!(
        events[0],
        BleEvent::GattDiscovered {
            command_id: command.command_id,
            database,
        }
    );
    assert_eq!(
        events[1],
        BleEvent::CommandCompleted {
            command_id: command.command_id,
            error: None,
        }
    );
}

#[test]
fn teardown_builder_orders_reserved_discovery_failure_before_disconnect_terminal() {
    let discovery_command = DiscoverGattCommand {
        command_id: CommandId { value: 31 },
        connection_id: ConnectionId { value: 32 },
        gatt_session_id: GattSessionId { value: 33 },
    };
    let connection = ConnectionCorrelation {
        generation: 34,
        connect_command_id: CommandId { value: 35 },
        connection_id: discovery_command.connection_id,
        peripheral_id: PeripheralId { value: 36 },
    };
    let mut gatt = ConnectionGattState::new();
    gatt.pending = Some(PendingDiscovery {
        correlation: DiscoveryCorrelation {
            connection,
            generation: 37,
            command: discovery_command,
        },
        stage: DiscoveryStage::Services,
        operation: PendingOperationState::Reserved,
        completion: None,
        driver_running: true,
        draft: DiscoveryDraft::new(),
    });
    let disconnected = BleEvent::ConnectionStateChanged {
        command_id: Some(CommandId { value: 38 }),
        connection_id: connection.connection_id,
        result: ConnectionStateResult::Ok {
            state: ConnectionState::Disconnected,
        },
    };
    let disconnect_terminal = BleEvent::CommandCompleted {
        command_id: CommandId { value: 38 },
        error: None,
    };

    let events = connection_teardown_events(
        Some(&gatt),
        "connection retired pending discovery",
        disconnected.clone(),
        Some(disconnect_terminal.clone()),
    );
    assert_eq!(events.len(), 3);
    assert!(matches!(
        &events[0],
        BleEvent::CommandCompleted {
            command_id,
            error: Some(BleEventError {
                category: BleErrorCategory::Connection,
                ..
            }),
        } if *command_id == discovery_command.command_id
    ));
    assert_eq!(events[1], disconnected);
    assert_eq!(events[2], disconnect_terminal);
}

#[test]
fn failed_discovery_has_only_one_terminal_and_no_partial_database() {
    let command = DiscoverGattCommand {
        command_id: CommandId { value: 21 },
        connection_id: ConnectionId { value: 22 },
        gatt_session_id: GattSessionId { value: 23 },
    };
    let error = simple_event_error(BleErrorCategory::Gatt, "descriptor discovery failed");

    assert_eq!(
        discovery_failure_event(command, error.clone()),
        BleEvent::CommandCompleted {
            command_id: command.command_id,
            error: Some(error),
        }
    );
}

#[test]
fn io_success_events_precede_their_single_terminal() {
    let read = read_command(41);
    let read_events =
        io_completion_events(IoCommand::Read(read), Ok(IoSuccess::Read(vec![0x10, 0x20])));
    assert_eq!(
        read_events,
        vec![
            BleEvent::CharacteristicRead {
                command_id: read.command_id,
                connection_id: read.connection_id,
                gatt_session_id: read.gatt_session_id,
                characteristic_id: read.characteristic_id,
                value: vec![0x10, 0x20],
            },
            BleEvent::CommandCompleted {
                command_id: read.command_id,
                error: None,
            },
        ]
    );

    let write = write_command(42, Vec::new(), WriteType::WithoutResponse);
    let write_events = io_completion_events(IoCommand::Write(write.clone()), Ok(IoSuccess::Write));
    assert_eq!(
        write_events,
        vec![
            BleEvent::CharacteristicWritten {
                command_id: write.command_id,
                connection_id: write.connection_id,
                gatt_session_id: write.gatt_session_id,
                characteristic_id: write.characteristic_id,
            },
            BleEvent::CommandCompleted {
                command_id: write.command_id,
                error: None,
            },
        ]
    );
}

#[test]
fn io_retirement_preserves_fifo_order_and_one_terminal_per_command() {
    let commands = vec![
        IoCommand::Read(read_command(51)),
        IoCommand::Write(write_command(52, vec![1], WriteType::WithResponse)),
        IoCommand::Read(read_command(53)),
    ];

    let events = io_failure_events(
        commands,
        BleErrorCategory::Connection,
        "connection retired queued I/O",
    );
    assert_eq!(events.len(), 3);
    for (event, expected_id) in events.iter().zip([51, 52, 53]) {
        assert!(matches!(
            event,
            BleEvent::CommandCompleted {
                command_id: CommandId { value },
                error: Some(BleEventError {
                    category: BleErrorCategory::Connection,
                    ..
                }),
            } if *value == expected_id
        ));
    }
}

#[test]
fn io_request_boundaries_and_properties_are_checked_before_acceptance() {
    let readable = CharacteristicProperties {
        readable: true,
        writable_with_response: false,
        writable_without_response: false,
        notifiable: false,
        indicatable: false,
    };
    let writable = CharacteristicProperties {
        readable: false,
        writable_with_response: true,
        writable_without_response: true,
        notifiable: false,
        indicatable: false,
    };

    assert_eq!(
        validate_io_property(&IoCommand::Read(read_command(61)), readable),
        Ok(())
    );
    assert!(matches!(
        validate_io_property(&IoCommand::Read(read_command(62)), writable),
        Err(CommandDispatchError::InvalidRequest { .. })
    ));
    assert_eq!(
        validate_io_property(
            &IoCommand::Write(write_command(63, Vec::new(), WriteType::WithResponse)),
            writable,
        ),
        Ok(())
    );
    assert_eq!(
        validate_io_property(
            &IoCommand::Write(write_command(
                64,
                vec![0; MAX_WRITE_VALUE_BYTES],
                WriteType::WithoutResponse,
            )),
            writable,
        ),
        Ok(())
    );
    assert!(matches!(
        validate_io_property(
            &IoCommand::Write(write_command(65, vec![1], WriteType::WithoutResponse)),
            readable,
        ),
        Err(CommandDispatchError::InvalidRequest { .. })
    ));

    assert_eq!(
        validate_io_shape(&IoCommand::Write(write_command(
            66,
            Vec::new(),
            WriteType::WithResponse,
        ))),
        Ok(())
    );
    assert_eq!(
        validate_io_shape(&IoCommand::Write(write_command(
            67,
            vec![0; MAX_WRITE_VALUE_BYTES],
            WriteType::WithResponse,
        ))),
        Ok(())
    );
    assert!(matches!(
        validate_io_shape(&IoCommand::Write(write_command(
            68,
            vec![0; MAX_WRITE_VALUE_BYTES + 1],
            WriteType::WithResponse,
        ))),
        Err(CommandDispatchError::InvalidRequest { .. })
    ));
}

#[test]
fn notification_mode_selection_prefers_notify_and_never_substitutes_strict_requests() {
    let both = notification_properties(true, true);
    let indicate_only = notification_properties(false, true);
    let neither = notification_properties(false, false);

    assert_eq!(
        select_notification_mode(NotificationModeRequest::PlatformDefault, both),
        ModeSelection::Supported(NotificationMode::Notification)
    );
    assert_eq!(
        select_notification_mode(NotificationModeRequest::PlatformDefault, indicate_only),
        ModeSelection::Supported(NotificationMode::Indication)
    );
    assert_eq!(
        select_notification_mode(NotificationModeRequest::RequireNotification, indicate_only),
        ModeSelection::StrictUnsupported(NotificationMode::Notification)
    );
    assert_eq!(
        select_notification_mode(NotificationModeRequest::RequireIndication, both),
        ModeSelection::Supported(NotificationMode::Indication)
    );
    assert_eq!(
        select_notification_mode(NotificationModeRequest::PlatformDefault, neither),
        ModeSelection::NoSupportedMode
    );
}

#[test]
fn notification_success_state_precedes_terminal_and_reports_exact_mode() {
    let command = notification_command(
        80,
        NotificationAction::Enable {
            mode: NotificationModeRequest::PlatformDefault,
        },
    );
    let correlation = subscription_correlation(command);
    let events = notification_enabled_events(SubscriptionIoCommand {
        command,
        correlation,
        action: SubscriptionIoAction::Enable {
            actual_mode: NotificationMode::Indication,
        },
    });

    assert_eq!(
        events,
        vec![
            BleEvent::NotificationStateChanged {
                command_id: command.command_id,
                connection_id: command.connection_id,
                gatt_session_id: command.gatt_session_id,
                subscription_id: command.subscription_id,
                characteristic_id: command.characteristic_id,
                state: NotificationState::Enabled {
                    actual_mode: Some(NotificationMode::Indication),
                },
            },
            BleEvent::CommandCompleted {
                command_id: command.command_id,
                error: None,
            },
        ]
    );

    let disable = notification_command(81, NotificationAction::Disable);
    assert!(matches!(
        notification_disabled_events(disable).as_slice(),
        [
            BleEvent::NotificationStateChanged {
                state: NotificationState::Disabled,
                ..
            },
            BleEvent::CommandCompleted { error: None, .. }
        ]
    ));
}

#[test]
fn cleanup_transition_rejects_reservations_and_drains_deferred_callbacks_once() {
    let mut gatt = ConnectionGattState::new();
    assert_eq!(ensure_gatt_cleanup_idle(&gatt), Ok(()));

    gatt.cleanup_in_progress = true;
    gatt.services_changed_pending = true;
    gatt.disconnect_pending = true;
    assert!(matches!(
        ensure_gatt_cleanup_idle(&gatt),
        Err(CommandDispatchError::BackendUnavailable { .. })
    ));

    assert_eq!(
        gatt.finish_cleanup(),
        DeferredCleanupCallbacks {
            services_changed: true,
            disconnected: true,
        }
    );
    assert!(!gatt.cleanup_in_progress);
    assert_eq!(gatt.finish_cleanup(), DeferredCleanupCallbacks::default());
}

#[test]
fn gatt_session_ids_collide_only_while_active_or_pending() {
    let connection = ConnectionCorrelation {
        generation: 90,
        connect_command_id: CommandId { value: 91 },
        connection_id: ConnectionId { value: 92 },
        peripheral_id: PeripheralId { value: 93 },
    };
    let session_id = GattSessionId { value: 94 };
    let other_session_id = GattSessionId { value: 95 };
    let command = DiscoverGattCommand {
        command_id: CommandId { value: 96 },
        connection_id: connection.connection_id,
        gatt_session_id: session_id,
    };
    let mut gatt = ConnectionGattState::new();

    assert!(!gatt_session_id_is_active_or_pending(&gatt, session_id));
    gatt.pending = Some(PendingDiscovery {
        correlation: DiscoveryCorrelation {
            connection,
            generation: 97,
            command,
        },
        stage: DiscoveryStage::Services,
        operation: PendingOperationState::Reserved,
        completion: None,
        driver_running: true,
        draft: DiscoveryDraft::new(),
    });
    assert!(gatt_session_id_is_active_or_pending(&gatt, session_id));
    assert!(!gatt_session_id_is_active_or_pending(
        &gatt,
        other_session_id
    ));

    gatt.pending = None;
    assert!(!gatt_session_id_is_active_or_pending(&gatt, session_id));

    gatt.active = Some(NativeGattMap {
        gatt_session_id: session_id,
        services: HashMap::new(),
        characteristics: HashMap::new(),
        descriptors: HashMap::new(),
    });
    assert!(gatt_session_id_is_active_or_pending(&gatt, session_id));

    gatt.active = None;
    assert!(!gatt_session_id_is_active_or_pending(&gatt, session_id));
}

#[test]
fn subscription_id_collision_uses_only_live_route_ids() {
    let subscription_id = SubscriptionId { value: 98 };
    let other_subscription_id = SubscriptionId { value: 99 };

    assert!(subscription_id_is_active_or_transitioning(
        [subscription_id],
        subscription_id,
    ));
    assert!(!subscription_id_is_active_or_transitioning(
        [other_subscription_id],
        subscription_id,
    ));
    assert!(!subscription_id_is_active_or_transitioning(
        std::iter::empty(),
        subscription_id,
    ));
}

#[test]
fn notification_routes_block_reads_and_only_matching_live_generations_receive_values() {
    let command = notification_command(
        82,
        NotificationAction::Enable {
            mode: NotificationModeRequest::PlatformDefault,
        },
    );
    let correlation = subscription_correlation(command);
    let stale = SubscriptionCorrelation {
        generation: correlation.generation + 1,
        ..correlation
    };

    for phase in [
        SubscriptionPhase::Enabling {
            command_id: command.command_id,
        },
        SubscriptionPhase::Active,
        SubscriptionPhase::Disabling {
            command_id: CommandId { value: 83 },
        },
    ] {
        assert!(subscription_phase_blocks_read(phase));
        assert!(correlation_accepts_notification(
            correlation,
            phase,
            correlation
        ));
        assert!(!correlation_accepts_notification(correlation, phase, stale));
    }
    assert!(!correlation_accepts_notification(
        correlation,
        SubscriptionPhase::Registering {
            command_id: command.command_id,
        },
        correlation,
    ));
    let disabled_pending_revoke = SubscriptionPhase::DisabledPendingRevoke;
    assert!(!subscription_phase_blocks_read(disabled_pending_revoke));
    assert!(!correlation_accepts_notification(
        correlation,
        disabled_pending_revoke,
        correlation,
    ));
    let revoking = SubscriptionPhase::Revoking {
        command_id: CommandId { value: 86 },
    };
    assert!(subscription_phase_blocks_read(revoking));
    assert!(!correlation_accepts_notification(
        correlation,
        revoking,
        correlation,
    ));
    assert!(io_command_is_conflicting_read(
        &IoCommand::Read(read_command(84)),
        command.characteristic_id,
    ));
    assert!(!io_command_is_conflicting_read(
        &IoCommand::Write(write_command(85, vec![1], WriteType::WithResponse)),
        command.characteristic_id,
    ));
}

#[test]
fn notification_completion_decisions_preserve_retry_and_stale_callback_rules() {
    let enable_id = CommandId { value: 86 };
    let disable_id = CommandId { value: 87 };
    let enable = SubscriptionIoAction::Enable {
        actual_mode: NotificationMode::Notification,
    };

    assert_eq!(
        subscription_completion_decision(
            SubscriptionPhase::Enabling {
                command_id: enable_id,
            },
            enable,
            true,
            enable_id,
        ),
        SubscriptionCompletionDecision::Activate
    );
    assert_eq!(
        subscription_completion_decision(
            SubscriptionPhase::Enabling {
                command_id: enable_id,
            },
            enable,
            false,
            enable_id,
        ),
        SubscriptionCompletionDecision::RemoveAfterEnableFailure
    );
    assert_eq!(
        subscription_completion_decision(
            SubscriptionPhase::Disabling {
                command_id: disable_id,
            },
            SubscriptionIoAction::Disable,
            false,
            disable_id,
        ),
        SubscriptionCompletionDecision::RestoreActive
    );
    assert_eq!(
        subscription_completion_decision(
            SubscriptionPhase::Disabling {
                command_id: disable_id,
            },
            SubscriptionIoAction::Disable,
            true,
            disable_id,
        ),
        SubscriptionCompletionDecision::RemoveAfterDisableSuccess
    );
    assert_eq!(
        subscription_completion_decision(
            SubscriptionPhase::Active,
            SubscriptionIoAction::Disable,
            true,
            disable_id,
        ),
        SubscriptionCompletionDecision::Stale
    );

    assert_eq!(
        notification_disable_decision(SubscriptionPhase::Active),
        NotificationDisableDecision::WriteCccd
    );
    assert_eq!(
        notification_disable_decision(SubscriptionPhase::DisabledPendingRevoke),
        NotificationDisableDecision::RetryCallbackRevoke
    );
    assert_eq!(
        notification_disable_decision(SubscriptionPhase::Revoking {
            command_id: disable_id,
        }),
        NotificationDisableDecision::Reject
    );

    for decision in [
        SubscriptionCompletionDecision::RemoveAfterDisableSuccess,
        SubscriptionCompletionDecision::RetryCallbackRevoke,
    ] {
        assert_eq!(
            subscription_removal_outcome(decision, true),
            SubscriptionRemovalOutcome::RemoveRoute
        );
        assert_eq!(
            subscription_removal_outcome(decision, false),
            SubscriptionRemovalOutcome::RetainDisabledPendingRevoke
        );
    }
    assert_eq!(
        subscription_removal_outcome(
            SubscriptionCompletionDecision::RemoveAfterEnableFailure,
            false,
        ),
        SubscriptionRemovalOutcome::RemoveRoute
    );
}

#[test]
fn notification_revoke_retry_uses_retry_command_event_shapes() {
    let retry = notification_command(88, NotificationAction::Disable);
    assert_eq!(
        notification_disabled_events(retry),
        vec![
            BleEvent::NotificationStateChanged {
                command_id: retry.command_id,
                connection_id: retry.connection_id,
                gatt_session_id: retry.gatt_session_id,
                subscription_id: retry.subscription_id,
                characteristic_id: retry.characteristic_id,
                state: NotificationState::Disabled,
            },
            BleEvent::CommandCompleted {
                command_id: retry.command_id,
                error: None,
            },
        ]
    );

    let error = simple_event_error(BleErrorCategory::Gatt, "callback revoke retry failed");
    assert_eq!(
        notification_revoke_failure_events(retry, error.clone()),
        vec![BleEvent::CommandCompleted {
            command_id: retry.command_id,
            error: Some(error),
        }]
    );
}

#[test]
fn cccd_values_are_exact() {
    assert_eq!(
        cccd_value(SubscriptionIoAction::Enable {
            actual_mode: NotificationMode::Notification,
        }),
        GattClientCharacteristicConfigurationDescriptorValue::Notify
    );
    assert_eq!(
        cccd_value(SubscriptionIoAction::Enable {
            actual_mode: NotificationMode::Indication,
        }),
        GattClientCharacteristicConfigurationDescriptorValue::Indicate
    );
    assert_eq!(
        cccd_value(SubscriptionIoAction::Disable),
        GattClientCharacteristicConfigurationDescriptorValue::None
    );
}

#[test]
fn write_options_and_communication_failures_map_without_collapsing_details() {
    assert_eq!(
        map_write_option(WriteType::WithResponse),
        GattWriteOption::WriteWithResponse
    );
    assert_eq!(
        map_write_option(WriteType::WithoutResponse),
        GattWriteOption::WriteWithoutResponse
    );
    assert_eq!(
        ensure_communication_success_with_protocol(
            GattCommunicationStatus::Success,
            None,
            "test I/O",
        ),
        Ok(())
    );

    let permission = ensure_communication_success_with_protocol(
        GattCommunicationStatus::AccessDenied,
        None,
        "test I/O",
    )
    .unwrap_err();
    assert_eq!(permission.category, BleErrorCategory::Permission);
    assert_eq!(
        permission.platform_details.unwrap().name,
        Some(format!("{:?}", GattCommunicationStatus::AccessDenied))
    );

    let unreachable = ensure_communication_success_with_protocol(
        GattCommunicationStatus::Unreachable,
        None,
        "test I/O",
    )
    .unwrap_err();
    assert_eq!(unreachable.category, BleErrorCategory::Connection);

    let protocol = ensure_communication_success_with_protocol(
        GattCommunicationStatus::ProtocolError,
        Some(0x0e),
        "test I/O",
    )
    .unwrap_err();
    assert_eq!(protocol.category, BleErrorCategory::Protocol);
    assert!(protocol.message.contains("ATT protocol error 0x0E"));
    assert_eq!(
        protocol.platform_details.unwrap().message.as_deref(),
        Some("ATT protocol error 0x0E")
    );
}

fn notification_properties(notifiable: bool, indicatable: bool) -> CharacteristicProperties {
    CharacteristicProperties {
        readable: true,
        writable_with_response: false,
        writable_without_response: false,
        notifiable,
        indicatable,
    }
}

fn notification_command(command_id: u64, action: NotificationAction) -> SetNotificationsCommand {
    SetNotificationsCommand {
        command_id: CommandId { value: command_id },
        connection_id: ConnectionId { value: 70 },
        gatt_session_id: GattSessionId { value: 71 },
        subscription_id: SubscriptionId { value: 73 },
        characteristic_id: GattCharacteristicId { value: 72 },
        action,
    }
}

fn subscription_correlation(command: SetNotificationsCommand) -> SubscriptionCorrelation {
    SubscriptionCorrelation {
        connection: ConnectionCorrelation {
            generation: 74,
            connect_command_id: CommandId { value: 75 },
            connection_id: command.connection_id,
            peripheral_id: PeripheralId { value: 76 },
        },
        generation: 77,
        gatt_session_id: command.gatt_session_id,
        subscription_id: command.subscription_id,
        characteristic_id: command.characteristic_id,
    }
}

fn read_command(command_id: u64) -> ReadCharacteristicCommand {
    ReadCharacteristicCommand {
        command_id: CommandId { value: command_id },
        connection_id: ConnectionId { value: 70 },
        gatt_session_id: GattSessionId { value: 71 },
        characteristic_id: GattCharacteristicId { value: 72 },
    }
}

fn write_command(
    command_id: u64,
    value: Vec<u8>,
    write_type: WriteType,
) -> WriteCharacteristicCommand {
    WriteCharacteristicCommand {
        command_id: CommandId { value: command_id },
        connection_id: ConnectionId { value: 70 },
        gatt_session_id: GattSessionId { value: 71 },
        characteristic_id: GattCharacteristicId { value: 72 },
        value,
        write_type,
    }
}
