use blehub::{
    BleErrorCategory, BleEvent, BleEventError, BluetoothUuid, CharacteristicProperties,
    CommandDispatchError, CommandId, ConnectionId, ConnectionState, ConnectionStateResult,
    GattCharacteristic, GattCharacteristicId, GattDatabase, GattDescriptorId, GattService,
    GattServiceId, GattSessionId, NotificationMode, NotificationModeRequest, NotificationState,
    PeripheralId, SubscriptionId,
};

use crate::{
    runtime::reservations::{
        CancellationDecision, ConnectionReservations, DispatchAction, GattAccess,
        ReconciliationFault, ReservationCoordinator,
    },
    waiter::{WaiterError, WaiterSuccess},
};

const PERIPHERAL: PeripheralId = PeripheralId { value: 1 };
const CONNECTION: ConnectionId = ConnectionId { value: 10 };
const SESSION: GattSessionId = GattSessionId { value: 20 };
const CHARACTERISTIC: GattCharacteristicId = GattCharacteristicId { value: 30 };
const DESCRIPTOR: GattDescriptorId = GattDescriptorId { value: 31 };
const SUBSCRIPTION: SubscriptionId = SubscriptionId { value: 40 };

fn connection_event(
    command_id: Option<CommandId>,
    connection_id: ConnectionId,
    state: ConnectionState,
) -> BleEvent {
    BleEvent::ConnectionStateChanged {
        command_id,
        connection_id,
        result: ConnectionStateResult::Ok { state },
    }
}

fn record(connections: &mut ConnectionReservations, event: BleEvent) {
    let event = connections
        .gate_event(event)
        .unwrap()
        .expect("fixture dispatch was already accepted");
    connections.record_event(&event).unwrap();
    if let BleEvent::CommandCompleted { command_id, error } = event {
        connections.record_terminal_for_conn(CONNECTION, command_id, &error.map_or(Ok(()), Err));
    }
}

fn terminal(
    connections: &mut ConnectionReservations,
    command_id: CommandId,
    result: Result<(), BleEventError>,
) {
    record(
        connections,
        BleEvent::CommandCompleted {
            command_id,
            error: result.err(),
        },
    );
}

fn accept(connections: &mut ConnectionReservations, command_id: CommandId) {
    let resolution = connections
        .resolve_dispatch_for_conn(CONNECTION, command_id, &Ok(()))
        .unwrap();
    assert!(matches!(resolution.action, DispatchAction::NoAction));
    assert!(resolution.events_to_replay.is_empty());
}

fn database() -> GattDatabase {
    GattDatabase {
        connection_id: CONNECTION,
        gatt_session_id: SESSION,
        services: vec![GattService {
            id: GattServiceId { value: 1 },
            uuid: BluetoothUuid {
                value: "0000fff0-0000-1000-8000-00805f9b34fb".to_owned(),
            },
            primary: Some(true),
            characteristics: vec![GattCharacteristic {
                id: CHARACTERISTIC,
                uuid: BluetoothUuid {
                    value: "0000fff1-0000-1000-8000-00805f9b34fb".to_owned(),
                },
                properties: CharacteristicProperties {
                    readable: true,
                    writable_with_response: true,
                    writable_without_response: true,
                    notifiable: true,
                    indicatable: true,
                },
                descriptors: Vec::new(),
            }],
        }],
    }
}

fn install_ready_connection(connections: &mut ConnectionReservations) {
    connections.reserve_connection(PERIPHERAL, 1.into(), CONNECTION);
    accept(connections, 1.into());
    record(
        connections,
        connection_event(Some(1.into()), CONNECTION, ConnectionState::Connected),
    );
    terminal(connections, 1.into(), Ok(()));
    assert_eq!(
        connections.validate_gatt_discovery(CONNECTION),
        Ok(PERIPHERAL)
    );
    connections.reserve_gatt_discovery(PERIPHERAL, 2.into(), SESSION);
    accept(connections, 2.into());
    record(
        connections,
        BleEvent::GattDiscovered {
            command_id: 2.into(),
            database: database(),
        },
    );
    terminal(connections, 2.into(), Ok(()));
    assert_eq!(
        connections.waiter_result_for_conn(CONNECTION, 2.into()),
        Some(Ok(WaiterSuccess::GattDiscovered(database())))
    );
    connections.retire_completed();
}

fn ready_connections() -> ConnectionReservations {
    let mut connections = ConnectionReservations::default();
    install_ready_connection(&mut connections);
    connections
}

#[derive(Clone, Copy, Debug)]
enum Operation {
    Discovery,
    Rssi,
    Read,
    Write,
    Subscription,
}

fn reserve_operation(connections: &mut ConnectionReservations, operation: Operation) {
    let command_id = 4.into();
    match operation {
        Operation::Discovery => {
            assert_eq!(
                connections.validate_gatt_discovery(CONNECTION),
                Ok(PERIPHERAL)
            );
            connections.reserve_gatt_discovery(PERIPHERAL, command_id, 21.into());
        }
        Operation::Rssi => {
            assert_eq!(connections.validate_rssi(CONNECTION), Ok(PERIPHERAL));
            connections.reserve_rssi(PERIPHERAL, command_id);
        }
        Operation::Read | Operation::Write => {
            assert_eq!(
                connections.validate_gatt_access(
                    CONNECTION,
                    SESSION,
                    CHARACTERISTIC,
                    if matches!(operation, Operation::Read) {
                        GattAccess::Read
                    } else {
                        GattAccess::Write
                    },
                ),
                Ok(PERIPHERAL)
            );
            if matches!(operation, Operation::Read) {
                connections.reserve_read(PERIPHERAL, command_id, SESSION, CHARACTERISTIC);
            } else {
                connections.reserve_write(PERIPHERAL, command_id, SESSION, CHARACTERISTIC);
            }
        }
        Operation::Subscription => {
            assert_eq!(
                connections.validate_notification_enable(CONNECTION, SESSION, CHARACTERISTIC),
                Ok(PERIPHERAL)
            );
            connections.reserve_notification_enable(
                PERIPHERAL,
                command_id,
                SUBSCRIPTION,
                SESSION,
                CHARACTERISTIC,
                NotificationModeRequest::PlatformDefault,
            );
        }
    }
}

fn enabled(command_id: CommandId, actual_mode: Option<NotificationMode>) -> BleEvent {
    BleEvent::NotificationStateChanged {
        command_id,
        connection_id: CONNECTION,
        gatt_session_id: SESSION,
        subscription_id: SUBSCRIPTION,
        characteristic_id: CHARACTERISTIC,
        state: NotificationState::Enabled { actual_mode },
    }
}

#[test]
fn pre_ack_operation_priority_is_global_across_connections() {
    for operation in [
        Operation::Discovery,
        Operation::Rssi,
        Operation::Read,
        Operation::Write,
        Operation::Subscription,
    ] {
        // Fresh hash seeds and both insertion orders exercise either traversal order
        // without inspecting the collection or relying on its private representation.
        for _ in 0..32 {
            for pending_first in [false, true] {
                let mut connections = ConnectionReservations::default();
                if pending_first {
                    connections.reserve_connection(2.into(), 3.into(), 11.into());
                }
                install_ready_connection(&mut connections);
                if !pending_first {
                    connections.reserve_connection(2.into(), 3.into(), 11.into());
                }
                reserve_operation(&mut connections, operation);

                // Command identifies the pending connect on a different record;
                // connection identifies an operation's loss route, which wins globally.
                let event =
                    connection_event(Some(3.into()), CONNECTION, ConnectionState::Disconnected);
                assert_eq!(connections.gate_event(event.clone()).unwrap(), None);
                let connect = connections
                    .resolve_dispatch_for_conn(11.into(), 3.into(), &Ok(()))
                    .unwrap();
                assert!(matches!(connect.action, DispatchAction::NoAction));
                assert!(
                    connect.events_to_replay.is_empty(),
                    "connect stole {operation:?} loss"
                );
                let operation_resolution = connections
                    .resolve_dispatch_for_conn(CONNECTION, 4.into(), &Ok(()))
                    .unwrap();
                assert!(matches!(
                    operation_resolution.action,
                    DispatchAction::NoAction
                ));
                assert_eq!(
                    operation_resolution.events_to_replay,
                    vec![event],
                    "{operation:?}"
                );
            }
        }
    }
}

#[test]
fn full_child_state_gate_faults_without_falling_back_to_foreign_connect() {
    for operation in [
        Operation::Discovery,
        Operation::Rssi,
        Operation::Read,
        Operation::Write,
        Operation::Subscription,
    ] {
        let mut connections = ready_connections();
        connections.reserve_connection(2.into(), 3.into(), 11.into());
        reserve_operation(&mut connections, operation);
        let event = connection_event(Some(3.into()), CONNECTION, ConnectionState::Disconnected);
        for _ in 0..64 {
            assert_eq!(connections.gate_event(event.clone()).unwrap(), None);
        }
        assert!(matches!(
            connections.gate_event(event.clone()),
            Err(ReconciliationFault::BufferOverflow)
        ));

        // Overflow must neither spill into the foreign connect nor append a 65th event.
        let foreign_connect = connections
            .resolve_dispatch_for_conn(11.into(), 3.into(), &Ok(()))
            .unwrap();
        assert!(foreign_connect.events_to_replay.is_empty(), "{operation:?}");
        let child = connections
            .resolve_dispatch_for_conn(CONNECTION, 4.into(), &Ok(()))
            .unwrap();
        assert_eq!(child.events_to_replay, vec![event; 64], "{operation:?}");
    }
}

#[test]
fn state_gate_prefers_affected_connection_when_both_connects_match() {
    for reverse in [false, true] {
        let mut connections = ConnectionReservations::default();
        let mut roots = [
            (PERIPHERAL, 1.into(), CONNECTION),
            (2.into(), 3.into(), 11.into()),
        ];
        if reverse {
            roots.reverse();
        }
        for (peripheral_id, command_id, connection_id) in roots {
            connections.reserve_connection(peripheral_id, command_id, connection_id);
        }
        let completed = BleEvent::CommandCompleted {
            command_id: 1.into(),
            error: None,
        };
        assert_eq!(connections.gate_event(completed.clone()).unwrap(), None);
        let event = connection_event(Some(3.into()), CONNECTION, ConnectionState::Connected);
        assert_eq!(connections.gate_event(event.clone()).unwrap(), None);
        let foreign_connect = connections
            .resolve_dispatch_for_conn(11.into(), 3.into(), &Ok(()))
            .unwrap();
        assert!(foreign_connect.events_to_replay.is_empty());
        let affected_connect = connections
            .resolve_dispatch_for_conn(CONNECTION, 1.into(), &Ok(()))
            .unwrap();
        assert_eq!(affected_connect.events_to_replay, vec![completed, event]);
    }
}

#[test]
fn disconnect_recording_uses_the_same_lifecycle_identity_as_foreign_filtering() {
    for command_id in [
        None,
        Some(1.into()),
        Some(4.into()),
        Some(5.into()),
        Some(99.into()),
    ] {
        let mut connections = ready_connections();
        connections.reserve_rssi(PERIPHERAL, 5.into());
        accept(&mut connections, 5.into());
        assert_eq!(connections.validate_disconnect(CONNECTION), Ok(PERIPHERAL));
        connections.reserve_disconnect(PERIPHERAL, 4.into());
        accept(&mut connections, 4.into());

        let event = connection_event(command_id, CONNECTION, ConnectionState::Disconnected);
        let foreign = command_id.is_some_and(|id| id != 1.into() && id != 4.into());
        assert_eq!(connections.is_foreign_disconnect(&event), foreign);
        // Exercise the reservation recorder directly, without the actor's foreign-event guard.
        // An owned I/O command is still foreign to connect/disconnect lifecycle evidence.
        connections.record_event(&event).unwrap();
        assert_eq!(
            connections.validate_rssi(CONNECTION),
            Err(if foreign {
                WaiterError::OperationBusy
            } else {
                WaiterError::ConnectionNotActive
            }),
            "command={command_id:?}"
        );
        assert!(
            connections
                .accepted_command_ids(CONNECTION)
                .contains(&5.into())
        );
    }
}

#[test]
fn connection_state_gate_honors_affected_scope_with_registered_command_routes() {
    use crate::runtime::command::CommandRoute;

    for operation in [
        Operation::Discovery,
        Operation::Rssi,
        Operation::Read,
        Operation::Write,
        Operation::Subscription,
    ] {
        for command_id in [None, Some(1.into()), Some(3.into()), Some(99.into())] {
            let mut reservations = ReservationCoordinator::default();
            install_ready_connection(&mut reservations.connections);
            reservations.reserve_connection(2.into(), 3.into(), 11.into());
            reserve_operation(&mut reservations.connections, operation);
            for (command_id, connection_id) in [(1.into(), CONNECTION), (3.into(), 11.into())] {
                reservations
                    .register_command(command_id, CommandRoute::Connection { connection_id });
            }

            let event = connection_event(command_id, CONNECTION, ConnectionState::Disconnected);
            assert_eq!(reservations.gate_event(event.clone()).unwrap(), None);
            let other_connect = reservations.resolve_dispatch(3.into(), &Ok(()));
            assert!(
                other_connect.events_to_replay.is_empty(),
                "{operation:?}, command={command_id:?}: unrelated connect stole the event"
            );
            let resolution = reservations
                .connections
                .resolve_dispatch_for_conn(CONNECTION, 4.into(), &Ok(()))
                .unwrap();
            assert!(matches!(resolution.action, DispatchAction::NoAction));
            assert_eq!(
                resolution.events_to_replay,
                vec![event],
                "{operation:?}, command={command_id:?}"
            );
        }
    }
}

#[test]
fn connection_dispatch_rejection_removes_only_the_released_command_entry() {
    use crate::runtime::command::CommandRoute;
    use crate::waiter::WaiterExpectation;

    // Every connection operation reaches the same root; the route carries only that root, so this
    // loop also proves the expectation -> route mapping stays root-shaped for all of them.
    let root = CommandRoute::Connection {
        connection_id: CONNECTION,
    };
    for expectation in [
        WaiterExpectation::Connected(CONNECTION),
        WaiterExpectation::Disconnected(CONNECTION),
        WaiterExpectation::GattDiscovered(CONNECTION, SESSION),
        WaiterExpectation::RssiRead(CONNECTION),
        WaiterExpectation::MtuRead(CONNECTION),
        WaiterExpectation::ConnectionPriority(CONNECTION),
        WaiterExpectation::BondState(CONNECTION),
        WaiterExpectation::BondRemoval(CONNECTION),
        WaiterExpectation::GattCacheRefreshed(CONNECTION),
        WaiterExpectation::CharacteristicRead(CONNECTION, SESSION, CHARACTERISTIC),
        WaiterExpectation::CharacteristicWritten(CONNECTION, SESSION, CHARACTERISTIC),
        WaiterExpectation::DescriptorRead(CONNECTION, SESSION, CHARACTERISTIC, DESCRIPTOR),
        WaiterExpectation::DescriptorWritten(CONNECTION, SESSION, CHARACTERISTIC, DESCRIPTOR),
        WaiterExpectation::NotificationsEnabled(CONNECTION, SESSION, SUBSCRIPTION, CHARACTERISTIC),
        WaiterExpectation::NotificationsDisabled(CONNECTION, SESSION, SUBSCRIPTION, CHARACTERISTIC),
    ] {
        assert_eq!(CommandRoute::from(expectation), root);
        let mut reservations = ReservationCoordinator::default();
        let is_connect = matches!(expectation, WaiterExpectation::Connected(_));
        if !is_connect {
            install_ready_connection(&mut reservations.connections);
            reservations.register_command(1.into(), root);
        }
        let mut siblings = Vec::new();
        if matches!(
            expectation,
            WaiterExpectation::RssiRead(_)
                | WaiterExpectation::CharacteristicRead(..)
                | WaiterExpectation::CharacteristicWritten(..)
        ) {
            // All three maps retain accepted siblings while only command 4 is rejected.
            reservations.reserve_rssi(PERIPHERAL, 5.into());
            accept(&mut reservations.connections, 5.into());
            reservations.reserve_read(PERIPHERAL, 6.into(), SESSION, CHARACTERISTIC);
            accept(&mut reservations.connections, 6.into());
            reservations.reserve_write(PERIPHERAL, 7.into(), SESSION, CHARACTERISTIC);
            accept(&mut reservations.connections, 7.into());
            for command_id in [CommandId::from(5), CommandId::from(6), CommandId::from(7)] {
                reservations.register_command(command_id, root);
                siblings.push((
                    command_id,
                    root,
                    reservations.attach_completion_observer(command_id),
                ));
            }
        }
        match expectation {
            WaiterExpectation::Connected(_) => {
                reservations.reserve_connection(PERIPHERAL, 4.into(), CONNECTION);
            }
            WaiterExpectation::Disconnected(_) => {
                reservations.reserve_disconnect(PERIPHERAL, 4.into());
            }
            WaiterExpectation::GattDiscovered(..) => {
                reserve_operation(&mut reservations.connections, Operation::Discovery);
            }
            WaiterExpectation::RssiRead(_) => {
                reserve_operation(&mut reservations.connections, Operation::Rssi);
            }
            WaiterExpectation::MtuRead(_) => {
                reservations.reserve_mtu(PERIPHERAL, 4.into());
            }
            WaiterExpectation::ConnectionPriority(_) => {
                reservations.reserve_set_connection_priority(PERIPHERAL, 4.into());
            }
            WaiterExpectation::BondState(_) => {
                reservations.reserve_create_bond(PERIPHERAL, 4.into());
            }
            WaiterExpectation::BondRemoval(_) => {
                reservations.reserve_remove_bond(PERIPHERAL, 4.into());
            }
            WaiterExpectation::GattCacheRefreshed(_) => {
                reservations.reserve_refresh_gatt_cache(PERIPHERAL, 4.into());
            }
            WaiterExpectation::CharacteristicRead(..) => {
                reserve_operation(&mut reservations.connections, Operation::Read);
            }
            WaiterExpectation::CharacteristicWritten(..) => {
                reserve_operation(&mut reservations.connections, Operation::Write);
            }
            WaiterExpectation::DescriptorRead(..) => {
                reservations.reserve_descriptor_read(
                    PERIPHERAL,
                    4.into(),
                    SESSION,
                    CHARACTERISTIC,
                    DESCRIPTOR,
                );
            }
            WaiterExpectation::DescriptorWritten(..) => {
                reservations.reserve_descriptor_write(
                    PERIPHERAL,
                    4.into(),
                    SESSION,
                    CHARACTERISTIC,
                    DESCRIPTOR,
                );
            }
            WaiterExpectation::NotificationsEnabled(..) => {
                reserve_operation(&mut reservations.connections, Operation::Subscription);
            }
            WaiterExpectation::NotificationsDisabled(..) => {
                reservations.reserve_notification_enable(
                    PERIPHERAL,
                    3.into(),
                    SUBSCRIPTION,
                    SESSION,
                    CHARACTERISTIC,
                    NotificationModeRequest::PlatformDefault,
                );
                accept(&mut reservations.connections, 3.into());
                record(&mut reservations.connections, enabled(3.into(), None));
                terminal(&mut reservations.connections, 3.into(), Ok(()));
                reservations.connections.retire_completed();
                reservations.reserve_notification_disable(PERIPHERAL, 4.into(), SUBSCRIPTION);
            }
            _ => unreachable!("only connection operations are enumerated here"),
        }
        reservations.register_command(4.into(), root);
        let completion = reservations.attach_completion_observer(4.into());
        let rejection = Err(CommandDispatchError::InvalidState {
            detail: "fixture native rejection".into(),
        });

        // A directory route alone does not prove that a reservation consumed this result.
        reservations.register_command(99.into(), root);
        let unmatched = reservations.resolve_dispatch(99.into(), &rejection);
        assert!(matches!(unmatched.action, DispatchAction::NoAction));
        assert_eq!(reservations.commands.route(99.into()), Some(root));
        assert_eq!(reservations.commands.route(4.into()), Some(root));
        assert!(!completion.is_completed());

        let resolution = reservations.resolve_dispatch(4.into(), &rejection);
        assert!(matches!(resolution.action, DispatchAction::NoAction));
        assert!(resolution.events_to_replay.is_empty());
        assert!(completion.is_completed(), "{expectation:?}");
        assert_eq!(
            reservations.commands.route(4.into()),
            None,
            "{expectation:?}: rejected reservation left a command route"
        );
        assert!(reservations.command_completion(4.into()).is_none());
        let duplicate = reservations.resolve_dispatch(4.into(), &rejection);
        assert!(matches!(duplicate.action, DispatchAction::NoAction));
        assert!(duplicate.events_to_replay.is_empty());
        for (command_id, sibling_route, completion) in &siblings {
            assert_eq!(
                reservations.commands.route(*command_id),
                Some(*sibling_route)
            );
            assert!(reservations.command_completion(*command_id).is_some());
            assert!(!completion.is_completed());
            assert_eq!(
                reservations
                    .connections
                    .waiter_result_for_conn(CONNECTION, *command_id),
                None
            );
        }
        assert_eq!(reservations.commands.route(99.into()), Some(root));
        reservations.remove_commands([99.into()]);
        reservations.advance_and_retire().unwrap();
        assert!(reservations.commands.route(4.into()).is_none());

        if is_connect {
            assert_eq!(
                reservations.require_peripheral_available(PERIPHERAL),
                Ok(())
            );
        } else {
            assert_eq!(reservations.commands.route(1.into()), Some(root));
            assert_eq!(reservations.validate_rssi(CONNECTION), Ok(PERIPHERAL));
            if matches!(expectation, WaiterExpectation::NotificationsDisabled(..)) {
                assert_eq!(
                    reservations.validate_notification_disable(CONNECTION, SESSION, SUBSCRIPTION),
                    Ok((PERIPHERAL, CHARACTERISTIC))
                );
            } else {
                assert_eq!(
                    reservations.validate_gatt_access(
                        CONNECTION,
                        SESSION,
                        CHARACTERISTIC,
                        GattAccess::Read,
                    ),
                    Ok(PERIPHERAL),
                    "{expectation:?}: rejection changed the committed database or left a busy slot"
                );
                assert_eq!(
                    reservations.validate_notification_disable(CONNECTION, SESSION, SUBSCRIPTION),
                    Err(WaiterError::SubscriptionNotActive)
                );
            }
        }
    }
}
#[test]
fn pre_ack_connect_fallback_is_broad_but_replayed_evidence_is_exact() {
    for (command_id, connection_id) in [(Some(99.into()), CONNECTION), (Some(1.into()), 99.into())]
    {
        let mut connections = ConnectionReservations::default();
        connections.reserve_connection(PERIPHERAL, 1.into(), CONNECTION);
        let event = connection_event(command_id, connection_id, ConnectionState::Connected);
        assert_eq!(connections.gate_event(event.clone()).unwrap(), None);
        let resolution = connections
            .resolve_dispatch_for_conn(CONNECTION, 1.into(), &Ok(()))
            .unwrap();
        assert!(matches!(resolution.action, DispatchAction::NoAction));
        assert_eq!(resolution.events_to_replay, vec![event]);
        for event in resolution.events_to_replay {
            record(&mut connections, event);
        }
        terminal(&mut connections, 1.into(), Ok(()));
        assert_eq!(
            connections.waiter_result_for_conn(CONNECTION, 1.into()),
            None
        );
        assert_eq!(
            connections.validate_gatt_discovery(CONNECTION),
            Err(WaiterError::ConnectionNotActive)
        );
    }
}

#[test]
fn gatt_admission_checks_stale_targets_and_preserves_native_lane_barriers() {
    for operation in [
        Operation::Discovery,
        Operation::Read,
        Operation::Write,
        Operation::Subscription,
    ] {
        let mut connections = ready_connections();
        reserve_operation(&mut connections, operation);
        accept(&mut connections, 4.into());
        for (session, characteristic, expected) in [
            (
                GattSessionId::from(99),
                CHARACTERISTIC,
                WaiterError::GattNotReady,
            ),
            (
                SESSION,
                GattCharacteristicId::from(99),
                WaiterError::GattNotReady,
            ),
        ] {
            for access in [
                GattAccess::Read,
                GattAccess::Write,
                GattAccess::EnableNotifications,
            ] {
                assert_eq!(
                    connections.validate_gatt_access(CONNECTION, session, characteristic, access),
                    Err(expected.clone()),
                    "{operation:?}, access={access:?}, session={session:?}, characteristic={characteristic:?}"
                );
            }
            assert_eq!(
                connections.validate_notification_enable(CONNECTION, session, characteristic),
                Err(expected),
                "{operation:?} notification enable"
            );
        }

        let expected_access = match operation {
            Operation::Discovery => [
                (GattAccess::Read, WaiterError::GattBusy),
                (GattAccess::Write, WaiterError::GattBusy),
                (GattAccess::EnableNotifications, WaiterError::GattBusy),
            ],
            Operation::Read | Operation::Write => [
                (GattAccess::Read, WaiterError::OperationBusy),
                (GattAccess::Write, WaiterError::OperationBusy),
                (GattAccess::EnableNotifications, WaiterError::OperationBusy),
            ],
            Operation::Subscription | Operation::Rssi => [
                (GattAccess::Read, WaiterError::OperationBusy),
                (GattAccess::Write, WaiterError::OperationBusy),
                (GattAccess::EnableNotifications, WaiterError::OperationBusy),
            ],
        };
        for (access, expected) in expected_access {
            let actual =
                connections.validate_gatt_access(CONNECTION, SESSION, CHARACTERISTIC, access);
            if matches!(operation, Operation::Read | Operation::Write)
                && matches!(access, GattAccess::Read | GattAccess::Write)
            {
                assert_eq!(actual, Ok(PERIPHERAL), "{operation:?}, access={access:?}");
            } else {
                assert_eq!(actual, Err(expected), "{operation:?}, access={access:?}");
            }
        }
        let expected_enable = if matches!(operation, Operation::Discovery) {
            WaiterError::GattBusy
        } else {
            WaiterError::OperationBusy
        };
        assert_eq!(
            connections.validate_notification_enable(CONNECTION, SESSION, CHARACTERISTIC),
            Err(expected_enable),
            "{operation:?} notification enable"
        );
    }
}

#[test]
fn characteristic_admission_keeps_pre_dispatch_handoff_exclusive() {
    let mut connections = ready_connections();
    connections.reserve_read(PERIPHERAL, 4.into(), SESSION, CHARACTERISTIC);
    assert_eq!(
        connections.validate_gatt_access(CONNECTION, SESSION, CHARACTERISTIC, GattAccess::Read),
        Err(WaiterError::OperationBusy)
    );
    assert_eq!(
        connections.validate_rssi(CONNECTION),
        Err(WaiterError::OperationBusy)
    );

    accept(&mut connections, 4.into());
    assert_eq!(
        connections.validate_gatt_access(CONNECTION, SESSION, CHARACTERISTIC, GattAccess::Read),
        Ok(PERIPHERAL)
    );
    assert_eq!(
        connections.validate_gatt_access(CONNECTION, SESSION, CHARACTERISTIC, GattAccess::Write),
        Ok(PERIPHERAL)
    );
}

#[test]
fn native_lane_capacity_counts_io_and_subscription_transitions() {
    let mut connections = ready_connections();
    // RSSI occupies the shared budget without conflicting with the subscription target.
    for offset in 0..1_023_u64 {
        let command_id = CommandId::from(1_000 + offset);
        assert_eq!(connections.validate_rssi(CONNECTION), Ok(PERIPHERAL));
        connections.reserve_rssi(PERIPHERAL, command_id);
        accept(&mut connections, command_id);
    }

    assert_eq!(
        connections.validate_notification_enable(CONNECTION, SESSION, CHARACTERISTIC),
        Ok(PERIPHERAL)
    );
    connections.reserve_notification_enable(
        PERIPHERAL,
        9_999.into(),
        SUBSCRIPTION,
        SESSION,
        CHARACTERISTIC,
        NotificationModeRequest::PlatformDefault,
    );
    accept(&mut connections, 9_999.into());
    assert_eq!(
        connections.validate_rssi(CONNECTION),
        Err(WaiterError::OperationBusy)
    );

    record(&mut connections, enabled(9_999.into(), None));
    terminal(&mut connections, 9_999.into(), Ok(()));
    assert_eq!(connections.retire_completed(), vec![9_999.into()]);
    // The active route survives, but its completed enable no longer consumes capacity.
    assert_eq!(connections.validate_rssi(CONNECTION), Ok(PERIPHERAL));
    assert_eq!(
        connections.validate_notification_disable(CONNECTION, SESSION, SUBSCRIPTION),
        Ok((PERIPHERAL, CHARACTERISTIC))
    );
    connections.reserve_notification_disable(PERIPHERAL, 10_000.into(), SUBSCRIPTION);
    assert_eq!(
        connections.validate_rssi(CONNECTION),
        Err(WaiterError::OperationBusy)
    );
    let rejection = CommandDispatchError::InvalidState {
        detail: "queue full in test".to_owned(),
    };
    let resolution = connections
        .resolve_dispatch_for_conn(CONNECTION, 10_000.into(), &Err(rejection))
        .unwrap();
    assert!(matches!(resolution.action, DispatchAction::NoAction));
    assert!(resolution.events_to_replay.is_empty());
    assert_eq!(connections.validate_rssi(CONNECTION), Ok(PERIPHERAL));
    assert_eq!(
        connections.validate_notification_disable(CONNECTION, SESSION, SUBSCRIPTION),
        Ok((PERIPHERAL, CHARACTERISTIC))
    );
}

#[test]
fn disable_admission_checks_subscription_before_disconnected_connection() {
    for disabling in [false, true] {
        let mut connections = ready_connections();
        reserve_operation(&mut connections, Operation::Subscription);
        accept(&mut connections, 4.into());
        record(&mut connections, enabled(4.into(), None));
        terminal(&mut connections, 4.into(), Ok(()));
        connections.retire_completed();
        assert_eq!(
            connections.validate_notification_disable(CONNECTION, SESSION, SUBSCRIPTION),
            Ok((PERIPHERAL, CHARACTERISTIC))
        );
        if disabling {
            connections.reserve_notification_disable(PERIPHERAL, 5.into(), SUBSCRIPTION);
            accept(&mut connections, 5.into());
        }
        record(
            &mut connections,
            connection_event(None, CONNECTION, ConnectionState::Disconnected),
        );
        // Check before retirement, while the disconnected lifecycle still owns routes.
        for (session, subscription) in [
            (GattSessionId::from(99), SUBSCRIPTION),
            (SESSION, 99.into()),
        ] {
            assert_eq!(
                connections.validate_notification_disable(CONNECTION, session, subscription),
                Err(WaiterError::SubscriptionNotActive)
            );
        }
        assert_eq!(
            connections.validate_notification_disable(CONNECTION, SESSION, SUBSCRIPTION),
            Err(if disabling {
                WaiterError::OperationBusy
            } else {
                WaiterError::ConnectionNotActive
            })
        );
        assert_eq!(
            connections.validate_notification_enable(CONNECTION, 99.into(), 99.into()),
            Err(WaiterError::ConnectionNotActive)
        );
    }
}

#[test]
fn io_keeps_first_terminal_while_domain_is_missing() {
    for operation in [Operation::Read, Operation::Write, Operation::Rssi] {
        for success_first in [false, true] {
            let mut connections = ready_connections();
            reserve_operation(&mut connections, operation);
            accept(&mut connections, 4.into());
            let error = BleEventError {
                category: BleErrorCategory::Gatt,
                message: "first terminal must remain authoritative".to_owned(),
                platform_details: None,
            };
            let failure = Err(error.clone());
            terminal(
                &mut connections,
                4.into(),
                if success_first {
                    Ok(())
                } else {
                    failure.clone()
                },
            );
            let pending_or_failure = if success_first {
                None
            } else {
                Some(Err(WaiterError::Operation(error.clone())))
            };
            assert_eq!(
                connections.waiter_result_for_conn(CONNECTION, 4.into()),
                pending_or_failure
            );
            terminal(
                &mut connections,
                4.into(),
                if success_first { failure } else { Ok(()) },
            );
            assert_eq!(
                connections.waiter_result_for_conn(CONNECTION, 4.into()),
                pending_or_failure
            );
            if success_first {
                connections.retire_completed();
                assert!(
                    connections
                        .accepted_command_ids(CONNECTION)
                        .contains(&4.into())
                );
            }
            let (event, success) = if matches!(operation, Operation::Read) {
                (
                    BleEvent::CharacteristicRead {
                        command_id: 4.into(),
                        connection_id: CONNECTION,
                        gatt_session_id: SESSION,
                        characteristic_id: CHARACTERISTIC,
                        value: vec![0xde, 0xad],
                    },
                    WaiterSuccess::CharacteristicRead {
                        connection_id: CONNECTION,
                        gatt_session_id: SESSION,
                        characteristic_id: CHARACTERISTIC,
                        value: vec![0xde, 0xad],
                    },
                )
            } else if matches!(operation, Operation::Write) {
                (
                    BleEvent::CharacteristicWritten {
                        command_id: 4.into(),
                        connection_id: CONNECTION,
                        gatt_session_id: SESSION,
                        characteristic_id: CHARACTERISTIC,
                    },
                    WaiterSuccess::CharacteristicWritten(CONNECTION, SESSION, CHARACTERISTIC),
                )
            } else {
                (
                    BleEvent::RssiRead {
                        command_id: 4.into(),
                        connection_id: CONNECTION,
                        rssi: -55,
                    },
                    WaiterSuccess::RssiRead(CONNECTION, -55),
                )
            };
            record(&mut connections, event);
            assert_eq!(
                connections.waiter_result_for_conn(CONNECTION, 4.into()),
                Some(if success_first {
                    Ok(success)
                } else {
                    Err(WaiterError::Operation(error))
                })
            );
            connections.retire_completed();
            assert!(
                !connections
                    .accepted_command_ids(CONNECTION)
                    .contains(&4.into())
            );
        }
    }
}

fn io_callback(operation: Operation, command_id: CommandId) -> (BleEvent, WaiterSuccess) {
    match operation {
        Operation::Rssi => (
            BleEvent::RssiRead {
                command_id,
                connection_id: CONNECTION,
                rssi: -(command_id.value as i16),
            },
            WaiterSuccess::RssiRead(CONNECTION, -(command_id.value as i16)),
        ),
        Operation::Read => (
            BleEvent::CharacteristicRead {
                command_id,
                connection_id: CONNECTION,
                gatt_session_id: SESSION,
                characteristic_id: CHARACTERISTIC,
                value: vec![command_id.value as u8],
            },
            WaiterSuccess::CharacteristicRead {
                connection_id: CONNECTION,
                gatt_session_id: SESSION,
                characteristic_id: CHARACTERISTIC,
                value: vec![command_id.value as u8],
            },
        ),
        Operation::Write => (
            BleEvent::CharacteristicWritten {
                command_id,
                connection_id: CONNECTION,
                gatt_session_id: SESSION,
                characteristic_id: CHARACTERISTIC,
            },
            WaiterSuccess::CharacteristicWritten(CONNECTION, SESSION, CHARACTERISTIC),
        ),
        _ => panic!("fixture requires an ordinary I/O operation"),
    }
}

#[test]
fn io_retirement_collects_completed_entries_without_releasing_incomplete_siblings() {
    let mut connections = ready_connections();
    let mut entries = Vec::new();
    let mut next_command_id = 4_u64;
    for operation in [Operation::Rssi, Operation::Read, Operation::Write] {
        for complete in [true, false] {
            let command_id = CommandId::from(next_command_id);
            next_command_id += 1;
            match operation {
                Operation::Rssi => connections.reserve_rssi(PERIPHERAL, command_id),
                Operation::Read => {
                    connections.reserve_read(PERIPHERAL, command_id, SESSION, CHARACTERISTIC);
                }
                Operation::Write => {
                    connections.reserve_write(PERIPHERAL, command_id, SESSION, CHARACTERISTIC);
                }
                _ => unreachable!(),
            }
            accept(&mut connections, command_id);
            let completion = connections
                .completion_observer_for_conn(CONNECTION, command_id)
                .unwrap();
            // Completed entries mix success and terminal failure; siblings have
            // successful terminals but must retain their missing domain evidence.
            if complete && matches!(operation, Operation::Read) {
                terminal(
                    &mut connections,
                    command_id,
                    Err(BleEventError {
                        category: BleErrorCategory::Gatt,
                        message: "read failed without a value".into(),
                        platform_details: None,
                    }),
                );
            } else {
                terminal(&mut connections, command_id, Ok(()));
                if complete {
                    record(&mut connections, io_callback(operation, command_id).0);
                }
            }
            assert_eq!(
                connections
                    .waiter_result_for_conn(CONNECTION, command_id)
                    .is_some(),
                complete
            );
            entries.push((command_id, operation, completion, complete));
        }
    }

    let mut retired = connections.retire_completed();
    retired.sort_unstable_by_key(|command_id| command_id.value);
    assert_eq!(retired, vec![4.into(), 6.into(), 8.into()]);
    for (command_id, operation, completion, complete) in &entries {
        assert_eq!(completion.is_completed(), *complete);
        assert_eq!(
            connections
                .completion_observer_for_conn(CONNECTION, *command_id)
                .is_some(),
            !complete
        );
        if !complete {
            let (event, success) = io_callback(*operation, *command_id);
            record(&mut connections, event);
            assert_eq!(
                connections.waiter_result_for_conn(CONNECTION, *command_id),
                Some(Ok(success))
            );
        }
    }

    let mut retired = connections.retire_completed();
    retired.sort_unstable_by_key(|command_id| command_id.value);
    assert_eq!(retired, vec![5.into(), 7.into(), 9.into()]);
    assert!(
        entries
            .iter()
            .all(|(_, _, completion, _)| completion.is_completed())
    );
    assert!(connections.retire_completed().is_empty());
}

#[test]
fn io_callbacks_isolate_commands_and_identity_before_ordered_dispatch_replay() {
    for operation in [Operation::Rssi, Operation::Read, Operation::Write] {
        for terminal_first in [false, true] {
            let mut connections = ready_connections();
            for command_id in [CommandId::from(5), CommandId::from(6)] {
                match operation {
                    Operation::Rssi => {
                        assert_eq!(connections.validate_rssi(CONNECTION), Ok(PERIPHERAL));
                        connections.reserve_rssi(PERIPHERAL, command_id);
                    }
                    Operation::Read | Operation::Write => {
                        assert_eq!(
                            connections.validate_gatt_access(
                                CONNECTION,
                                SESSION,
                                CHARACTERISTIC,
                                if matches!(operation, Operation::Read) {
                                    GattAccess::Read
                                } else {
                                    GattAccess::Write
                                },
                            ),
                            Ok(PERIPHERAL)
                        );
                        if matches!(operation, Operation::Read) {
                            connections.reserve_read(
                                PERIPHERAL,
                                command_id,
                                SESSION,
                                CHARACTERISTIC,
                            );
                        } else {
                            connections.reserve_write(
                                PERIPHERAL,
                                command_id,
                                SESSION,
                                CHARACTERISTIC,
                            );
                        }
                    }
                    _ => unreachable!(),
                }
                accept(&mut connections, command_id);
            }
            reserve_operation(&mut connections, operation);

            // Each malformed callback must pass through without entering any slot's evidence.
            let mut foreign = vec![io_callback(operation, 99.into()).0];
            for command_id in [5.into(), 6.into(), 4.into()] {
                for wrong_field in 0..3 {
                    let mut event = io_callback(operation, command_id).0;
                    match &mut event {
                        BleEvent::RssiRead { connection_id, .. } => {
                            if wrong_field != 0 {
                                continue;
                            }
                            *connection_id = 99.into();
                        }
                        BleEvent::CharacteristicRead {
                            connection_id,
                            gatt_session_id,
                            characteristic_id,
                            ..
                        }
                        | BleEvent::CharacteristicWritten {
                            connection_id,
                            gatt_session_id,
                            characteristic_id,
                            ..
                        } => match wrong_field {
                            0 => *connection_id = 99.into(),
                            1 => *gatt_session_id = 99.into(),
                            _ => *characteristic_id = 99.into(),
                        },
                        _ => unreachable!(),
                    }
                    foreign.push(event);
                }
            }
            for event in foreign {
                assert_eq!(
                    connections.gate_event(event.clone()).unwrap(),
                    Some(event.clone()),
                    "{operation:?}: foreign callback was buffered: {event:?}"
                );
                connections.record_event(&event).unwrap();
            }
            for command_id in [5.into(), 6.into()] {
                terminal(&mut connections, command_id, Ok(()));
                assert_eq!(
                    connections.waiter_result_for_conn(CONNECTION, command_id),
                    None
                );
            }

            let (callback, success) = io_callback(operation, 4.into());
            let completed = BleEvent::CommandCompleted {
                command_id: 4.into(),
                error: None,
            };
            let pending_events = if terminal_first {
                vec![completed, callback]
            } else {
                vec![callback, completed]
            };
            for event in &pending_events {
                assert_eq!(connections.gate_event(event.clone()).unwrap(), None);
            }

            // Accepted callbacks must still locate their own command, not the pending same-kind slot.
            for command_id in [6.into(), 5.into()] {
                let (event, expected) = io_callback(operation, command_id);
                record(&mut connections, event);
                assert_eq!(
                    connections.waiter_result_for_conn(CONNECTION, command_id),
                    Some(Ok(expected)),
                    "{operation:?}: accepted callback reached the wrong command"
                );
                if command_id == CommandId::from(6) {
                    assert_eq!(
                        connections.waiter_result_for_conn(CONNECTION, 5.into()),
                        None,
                        "{operation:?}: command 6 callback also completed command 5"
                    );
                } else {
                    assert_eq!(
                        connections.waiter_result_for_conn(CONNECTION, 6.into()),
                        Some(Ok(io_callback(operation, 6.into()).1)),
                        "{operation:?}: command 5 callback changed command 6's result"
                    );
                }
                assert_eq!(
                    connections.waiter_result_for_conn(CONNECTION, 4.into()),
                    None
                );
                let resolution =
                    connections.resolve_dispatch_for_conn(CONNECTION, command_id, &Ok(()));
                if let Some(resolution) = resolution {
                    assert!(matches!(resolution.action, DispatchAction::NoAction));
                    assert!(resolution.events_to_replay.is_empty());
                }
            }
            assert!(
                connections
                    .resolve_dispatch_for_conn(CONNECTION, 99.into(), &Ok(()))
                    .is_none()
            );
            assert!(
                connections
                    .resolve_dispatch_for_conn(99.into(), 4.into(), &Ok(()))
                    .is_none()
            );
            let resolution = connections
                .resolve_dispatch_for_conn(CONNECTION, 4.into(), &Ok(()))
                .unwrap();
            assert!(matches!(resolution.action, DispatchAction::NoAction));
            assert_eq!(resolution.events_to_replay, pending_events);
            assert_eq!(
                connections.waiter_result_for_conn(CONNECTION, 4.into()),
                None
            );
            for (index, event) in resolution.events_to_replay.into_iter().enumerate() {
                record(&mut connections, event);
                assert_eq!(
                    connections.waiter_result_for_conn(CONNECTION, 4.into()),
                    (index == 1).then(|| Ok(success.clone())),
                    "{operation:?}, terminal_first={terminal_first}: replay settled out of order"
                );
            }
        }
    }
}

#[test]
fn pending_io_rejection_replays_passive_loss_without_consuming_accepted_commands() {
    for operation in [Operation::Rssi, Operation::Read, Operation::Write] {
        for commanded_loss in [false, true] {
            let mut connections = ready_connections();
            assert_eq!(connections.validate_rssi(CONNECTION), Ok(PERIPHERAL));
            connections.reserve_rssi(PERIPHERAL, 5.into());
            accept(&mut connections, 5.into());
            reserve_operation(&mut connections, operation);

            if commanded_loss {
                let event =
                    connection_event(Some(1.into()), CONNECTION, ConnectionState::Disconnected);
                assert_eq!(connections.gate_event(event).unwrap(), None);
            }
            let losses = vec![
                BleEvent::ConnectionStateChanged {
                    command_id: None,
                    connection_id: CONNECTION,
                    result: ConnectionStateResult::Err {
                        error: BleEventError {
                            category: BleErrorCategory::Connection,
                            message: "passive loss while I/O dispatch is pending".into(),
                            platform_details: None,
                        },
                    },
                },
                connection_event(None, CONNECTION, ConnectionState::Disconnected),
            ];
            for event in &losses {
                assert_eq!(connections.gate_event(event.clone()).unwrap(), None);
            }
            let (callback, success) = io_callback(Operation::Rssi, 5.into());
            record(&mut connections, callback);
            terminal(&mut connections, 5.into(), Ok(()));
            assert_eq!(
                connections.waiter_result_for_conn(CONNECTION, 5.into()),
                Some(Ok(success.clone()))
            );
            let rejection = Err(CommandDispatchError::InvalidState {
                detail: "native I/O submission rejected after connection loss".into(),
            });
            let resolution = connections
                .resolve_dispatch_for_conn(CONNECTION, 4.into(), &rejection)
                .unwrap();
            if commanded_loss {
                assert!(matches!(
                    resolution.action,
                    DispatchAction::FaultRuntime(ReconciliationFault::DispatchEvidenceConflict)
                ));
            } else {
                assert!(matches!(resolution.action, DispatchAction::NoAction));
            }
            assert_eq!(resolution.events_to_replay, losses);
            assert_eq!(connections.validate_rssi(CONNECTION), Ok(PERIPHERAL));
            assert_eq!(
                connections.waiter_result_for_conn(CONNECTION, 4.into()),
                None
            );
            for event in resolution.events_to_replay {
                record(&mut connections, event);
            }
            assert_eq!(
                connections.validate_rssi(CONNECTION),
                Err(WaiterError::ConnectionNotActive)
            );
            assert_eq!(
                connections.waiter_result_for_conn(CONNECTION, 5.into()),
                Some(Ok(success))
            );
        }
    }
}

#[test]
fn cancellation_routes_only_cancellable_connection_operations_and_waits_for_native_terminal() {
    use crate::runtime::reservations::CancellationDecision;

    let mut connections = ready_connections();
    connections.reserve_read(PERIPHERAL, 4.into(), SESSION, CHARACTERISTIC);
    assert!(matches!(
        connections.request_cancellation_for_conn(CONNECTION, 4.into()),
        Some(CancellationDecision::Accepted(_))
    ));
    assert!(!connections.try_claim_cancellation_dispatch_for_conn(CONNECTION, 4.into()));

    accept(&mut connections, 4.into());
    assert!(connections.try_claim_cancellation_dispatch_for_conn(CONNECTION, 4.into()));
    assert!(!connections.try_claim_cancellation_dispatch_for_conn(CONNECTION, 4.into()));
    connections.retire_completed();
    assert!(matches!(
        connections.request_cancellation_for_conn(CONNECTION, 4.into()),
        Some(CancellationDecision::AlreadyRequested(_))
    ));
    terminal(
        &mut connections,
        4.into(),
        Err(BleEventError {
            category: BleErrorCategory::Cancelled,
            message: "cancelled".to_owned(),
            platform_details: None,
        }),
    );
    connections.retire_completed();
    assert!(matches!(
        connections.request_cancellation_for_conn(CONNECTION, 4.into()),
        Some(CancellationDecision::AlreadyRequested(_))
    ));
    assert!(connections.try_resolve_cancellation_for_conn(CONNECTION, 4.into(), Ok(())));
    connections.retire_completed();
    assert!(
        connections
            .request_cancellation_for_conn(CONNECTION, 4.into())
            .is_none()
    );

    let mut connections = ready_connections();
    connections.reserve_disconnect(PERIPHERAL, 4.into());
    assert!(matches!(
        connections.request_cancellation_for_conn(CONNECTION, 4.into()),
        Some(CancellationDecision::NotCancellable)
    ));
    assert!(
        connections
            .request_cancellation_for_conn(CONNECTION, 99.into())
            .is_none()
    );
}

#[test]
fn subscription_retirement_removes_command_routes_independently_of_subscription_routes() {
    use crate::runtime::command::CommandRoute;

    for enable in [true, false] {
        for succeeded in [true, false] {
            let mut reservations = ReservationCoordinator::default();
            install_ready_connection(&mut reservations.connections);
            let root_route = CommandRoute::Connection {
                connection_id: CONNECTION,
            };
            reservations.register_command(1.into(), root_route);

            // Disable starts from an active subscription, with no enable command retained.
            if !enable {
                reserve_operation(&mut reservations.connections, Operation::Subscription);
                accept(&mut reservations.connections, 4.into());
                record(&mut reservations.connections, enabled(4.into(), None));
                terminal(&mut reservations.connections, 4.into(), Ok(()));
                reservations.connections.retire_completed();
            }

            let command_id: CommandId = if enable { 4.into() } else { 5.into() };
            if enable {
                reserve_operation(&mut reservations.connections, Operation::Subscription);
            } else {
                assert_eq!(
                    reservations.validate_notification_disable(CONNECTION, SESSION, SUBSCRIPTION),
                    Ok((PERIPHERAL, CHARACTERISTIC))
                );
                reservations.reserve_notification_disable(PERIPHERAL, command_id, SUBSCRIPTION);
            }
            let route = CommandRoute::Connection {
                connection_id: CONNECTION,
            };
            reservations.register_command(command_id, route);
            let completion = reservations.attach_completion_observer(command_id);
            let resolution = reservations.resolve_dispatch(command_id, &Ok(()));
            assert!(matches!(resolution.action, DispatchAction::NoAction));
            assert!(resolution.events_to_replay.is_empty());

            // An enable cancellation receipt must survive until its control result arrives.
            let receipt = if enable {
                let Some(CancellationDecision::Accepted(receipt)) =
                    reservations.request_cancellation(command_id)
                else {
                    panic!("enable cancellation must be admitted");
                };
                assert!(reservations.try_claim_cancellation_dispatch(command_id));
                Some(receipt)
            } else {
                None
            };

            if succeeded {
                let event = if enable {
                    enabled(command_id, None)
                } else {
                    BleEvent::NotificationStateChanged {
                        command_id,
                        connection_id: CONNECTION,
                        gatt_session_id: SESSION,
                        subscription_id: SUBSCRIPTION,
                        characteristic_id: CHARACTERISTIC,
                        state: NotificationState::Disabled,
                    }
                };
                reservations.record_event_evidence(&event).unwrap();
            }
            reservations.advance_and_retire().unwrap();
            assert_eq!(reservations.commands.route(command_id), Some(route));
            assert!(!completion.is_completed());

            let error = (!succeeded).then(|| BleEventError {
                category: BleErrorCategory::Internal,
                message: "subscription transition failed".into(),
                platform_details: None,
            });
            reservations.record_terminal(command_id, error);
            assert_eq!(
                reservations.waiter_result(command_id).unwrap().is_ok(),
                succeeded
            );
            if let Some(receipt) = receipt {
                reservations.advance_and_retire().unwrap();
                assert_eq!(reservations.commands.route(command_id), Some(route));
                assert!(!completion.is_completed());
                assert!(matches!(receipt.has_changed(), Ok(false)));
                let result = Err(WaiterError::OperationNotCancellable(command_id));
                assert!(
                    reservations
                        .resolve_cancellation_and_release_public_events(command_id, result.clone())
                        .is_empty()
                );
                assert_eq!(*receipt.borrow(), Some(result));
            }

            reservations.advance_and_retire().unwrap();
            assert_eq!(
                reservations.commands.route(command_id),
                None,
                "enable={enable}, succeeded={succeeded}: retired transition retained its route"
            );
            assert!(reservations.command_completion(command_id).is_none());
            assert!(completion.is_completed());
            assert_eq!(reservations.commands.route(1.into()), Some(root_route));
            assert_eq!(reservations.validate_rssi(CONNECTION), Ok(PERIPHERAL));

            // Successful enable and failed disable both preserve an active subscription.
            let subscription_remains_active = if enable { succeeded } else { !succeeded };
            if subscription_remains_active {
                assert_eq!(
                    reservations.validate_notification_disable(CONNECTION, SESSION, SUBSCRIPTION),
                    Ok((PERIPHERAL, CHARACTERISTIC))
                );
            } else {
                assert_eq!(
                    reservations.validate_notification_disable(CONNECTION, SESSION, SUBSCRIPTION),
                    Err(WaiterError::SubscriptionNotActive)
                );
                assert_eq!(
                    reservations.validate_notification_enable(CONNECTION, SESSION, CHARACTERISTIC),
                    Ok(PERIPHERAL)
                );
            }
            reservations.advance_and_retire().unwrap();
            assert!(reservations.commands.route(command_id).is_none());
        }
    }
}

#[test]
fn terminal_retirement_preserves_pending_cancellation_receipts_across_connection_operations() {
    use crate::runtime::reservations::CancellationDecision;

    for operation in [
        Operation::Discovery,
        Operation::Rssi,
        Operation::Read,
        Operation::Write,
        Operation::Subscription,
    ] {
        for disconnected in [false, true] {
            let mut connections = ready_connections();
            reserve_operation(&mut connections, operation);
            accept(&mut connections, 4.into());
            let receipt = match connections.request_cancellation_for_conn(CONNECTION, 4.into()) {
                Some(CancellationDecision::Accepted(receipt)) => receipt,
                _ => panic!("{operation:?}: cancellation must be admitted"),
            };
            assert!(connections.try_claim_cancellation_dispatch_for_conn(CONNECTION, 4.into()));
            terminal(
                &mut connections,
                4.into(),
                Err(BleEventError {
                    category: BleErrorCategory::Cancelled,
                    message: "cancelled before control returned".to_owned(),
                    platform_details: None,
                }),
            );
            if disconnected {
                record(
                    &mut connections,
                    connection_event(None, CONNECTION, ConnectionState::Disconnected),
                );
            }
            connections.retire_completed();
            assert!(
                matches!(receipt.has_changed(), Ok(false)),
                "{operation:?}, disconnected={disconnected}: receipt owner must survive"
            );
            assert_eq!(
                connections.require_peripheral_available(PERIPHERAL),
                Err(WaiterError::PeripheralBusy)
            );
            assert!(connections.try_resolve_cancellation_for_conn(CONNECTION, 4.into(), Ok(())));
            assert_eq!(*receipt.borrow(), Some(Ok(())));
            connections.retire_completed();
            connections.retire_completed();
            assert!(
                connections
                    .request_cancellation_for_conn(CONNECTION, 4.into())
                    .is_none()
            );
            assert_eq!(
                connections.require_peripheral_available(PERIPHERAL),
                if disconnected {
                    Ok(())
                } else {
                    Err(WaiterError::PeripheralBusy)
                }
            );
            if !disconnected {
                assert_eq!(
                    connections.validate_gatt_discovery(CONNECTION),
                    Ok(PERIPHERAL)
                );
            }
        }
    }
}

#[test]
fn failed_connect_retirement_preserves_pending_cancellation_receipt() {
    use crate::runtime::reservations::CancellationDecision;

    let mut connections = ConnectionReservations::default();
    connections.reserve_connection(PERIPHERAL, 1.into(), CONNECTION);
    accept(&mut connections, 1.into());
    let receipt = match connections.request_cancellation_for_conn(CONNECTION, 1.into()) {
        Some(CancellationDecision::Accepted(receipt)) => receipt,
        _ => panic!("pending connect must be cancellable"),
    };
    assert!(connections.try_claim_cancellation_dispatch_for_conn(CONNECTION, 1.into()));
    terminal(
        &mut connections,
        1.into(),
        Err(BleEventError {
            category: BleErrorCategory::Cancelled,
            message: "connect cancelled before control returned".to_owned(),
            platform_details: None,
        }),
    );
    connections.retire_completed();
    assert!(matches!(receipt.has_changed(), Ok(false)));
    assert_eq!(
        connections.require_peripheral_available(PERIPHERAL),
        Err(WaiterError::PeripheralBusy)
    );
    // Retaining a failed root for its control receipt must not admit new native work.
    assert_eq!(
        connections.validate_disconnect(CONNECTION),
        Err(WaiterError::ConnectionNotActive)
    );
    assert!(connections.try_resolve_cancellation_for_conn(CONNECTION, 1.into(), Ok(())));
    connections.retire_completed();
    assert_eq!(*receipt.borrow(), Some(Ok(())));
    assert_eq!(connections.require_peripheral_available(PERIPHERAL), Ok(()));
    assert!(
        connections
            .request_cancellation_for_conn(CONNECTION, 1.into())
            .is_none()
    );
}

#[test]
fn enabled_unknown_mode_is_default_only_and_strict_duplicates_are_validated() {
    let mut connections = ready_connections();
    reserve_operation(&mut connections, Operation::Subscription);
    accept(&mut connections, 4.into());
    record(&mut connections, enabled(4.into(), None));
    assert_eq!(
        connections.waiter_result_for_conn(CONNECTION, 4.into()),
        None
    );
    terminal(&mut connections, 4.into(), Ok(()));
    assert_eq!(
        connections.waiter_result_for_conn(CONNECTION, 4.into()),
        Some(Ok(WaiterSuccess::NotificationsEnabled {
            connection_id: CONNECTION,
            gatt_session_id: SESSION,
            subscription_id: SUBSCRIPTION,
            characteristic_id: CHARACTERISTIC,
            actual_mode: None,
        }))
    );
    connections.retire_completed();
    assert_eq!(
        connections.validate_notification_disable(CONNECTION, SESSION, SUBSCRIPTION),
        Ok((PERIPHERAL, CHARACTERISTIC))
    );

    for (request, valid, wrong) in [
        (
            NotificationModeRequest::RequireNotification,
            NotificationMode::Notification,
            NotificationMode::Indication,
        ),
        (
            NotificationModeRequest::RequireIndication,
            NotificationMode::Indication,
            NotificationMode::Notification,
        ),
    ] {
        for actual_mode in [None, Some(wrong)] {
            for valid_first in [false, true] {
                let mut connections = ready_connections();
                assert_eq!(
                    connections.validate_notification_enable(CONNECTION, SESSION, CHARACTERISTIC),
                    Ok(PERIPHERAL)
                );
                connections.reserve_notification_enable(
                    PERIPHERAL,
                    4.into(),
                    SUBSCRIPTION,
                    SESSION,
                    CHARACTERISTIC,
                    request,
                );
                accept(&mut connections, 4.into());
                if valid_first {
                    record(&mut connections, enabled(4.into(), Some(valid)));
                }
                let invalid = connections
                    .gate_event(enabled(4.into(), actual_mode))
                    .unwrap()
                    .unwrap();
                assert!(matches!(
                    connections.record_event(&invalid),
                    Err(ReconciliationFault::UnexpectedNotificationMode)
                ));
                assert_eq!(
                    connections.waiter_result_for_conn(CONNECTION, 4.into()),
                    None
                );
            }
        }
    }
}

#[test]
fn passive_loss_is_withheld_behind_unresolved_bond_dispatch() {
    let mut connections = ready_connections();
    assert_eq!(connections.validate_create_bond(CONNECTION), Ok(PERIPHERAL));
    connections.reserve_create_bond(PERIPHERAL, 4.into());

    // Bond dispatch is unresolved, so a passive teardown observed before its dispatch result
    // must queue behind that gate instead of publishing ahead of the bond's own early evidence.
    let loss = connection_event(None, CONNECTION, ConnectionState::Disconnected);
    assert_eq!(connections.gate_event(loss.clone()).unwrap(), None);

    let resolution = connections
        .resolve_dispatch_for_conn(CONNECTION, 4.into(), &Ok(()))
        .unwrap();
    assert_eq!(resolution.events_to_replay, vec![loss]);

    // With no unresolved gate left, the next loss passes straight through.
    let later = connection_event(None, CONNECTION, ConnectionState::Disconnected);
    assert_eq!(connections.gate_event(later.clone()).unwrap(), Some(later));
}
