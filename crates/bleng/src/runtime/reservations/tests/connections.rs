use blehub::{
    BleErrorCategory, BleEvent, BleEventError, BluetoothUuid, CharacteristicProperties, CommandId,
    ConnectionId, ConnectionState, ConnectionStateResult, GattCharacteristic, GattCharacteristicId,
    GattDatabase, GattService, GattServiceId, GattSessionId, NotificationMode,
    NotificationModeRequest, NotificationState, PeripheralId, SubscriptionId,
};

use crate::{
    runtime::reservations::{ConnectionReservations, DispatchEffect, GattAccess, ReconcileFault},
    waiter::{WaiterError, WaiterSuccess},
};

const PERIPHERAL: PeripheralId = PeripheralId { value: 1 };
const CONNECTION: ConnectionId = ConnectionId { value: 10 };
const SESSION: GattSessionId = GattSessionId { value: 20 };
const CHARACTERISTIC: GattCharacteristicId = GattCharacteristicId { value: 30 };
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
        .buffer_before_dispatch(event)
        .unwrap()
        .expect("fixture dispatch was already accepted");
    connections.record_event(&event).unwrap();
    if let BleEvent::CommandCompleted { command_id, error } = event {
        connections.record_terminal(command_id, &error.map_or(Ok(()), Err));
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
    let resolution = connections.resolve_dispatch(command_id, &Ok(())).unwrap();
    assert!(matches!(resolution.effect, DispatchEffect::None));
    assert!(resolution.replay.is_empty());
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
        connections.waiter_result(2.into()),
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
                assert_eq!(
                    connections.buffer_before_dispatch(event.clone()).unwrap(),
                    None
                );
                let connect = connections.resolve_dispatch(3.into(), &Ok(())).unwrap();
                assert!(matches!(connect.effect, DispatchEffect::None));
                assert!(
                    connect.replay.is_empty(),
                    "connect stole {operation:?} loss"
                );
                let operation_resolution = connections.resolve_dispatch(4.into(), &Ok(())).unwrap();
                assert!(matches!(operation_resolution.effect, DispatchEffect::None));
                assert_eq!(operation_resolution.replay, vec![event], "{operation:?}");
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
        assert_eq!(
            connections.buffer_before_dispatch(event.clone()).unwrap(),
            None
        );
        let resolution = connections.resolve_dispatch(1.into(), &Ok(())).unwrap();
        assert!(matches!(resolution.effect, DispatchEffect::None));
        assert_eq!(resolution.replay, vec![event]);
        for event in resolution.replay {
            record(&mut connections, event);
        }
        terminal(&mut connections, 1.into(), Ok(()));
        assert_eq!(connections.waiter_result(1.into()), None);
        assert_eq!(
            connections.validate_gatt_discovery(CONNECTION),
            Err(WaiterError::ConnectionNotActive)
        );
    }
}

#[test]
fn gatt_admission_checks_stale_targets_before_io_and_discovery_busy() {
    for operation in [
        Operation::Discovery,
        Operation::Read,
        Operation::Write,
        Operation::Subscription,
    ] {
        let mut connections = ready_connections();
        reserve_operation(&mut connections, operation);
        let busy = if matches!(operation, Operation::Discovery) {
            WaiterError::GattBusy
        } else {
            WaiterError::OperationBusy
        };
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
            (SESSION, CHARACTERISTIC, busy),
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
    }
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
            assert_eq!(connections.waiter_result(4.into()), pending_or_failure);
            terminal(
                &mut connections,
                4.into(),
                if success_first { failure } else { Ok(()) },
            );
            assert_eq!(connections.waiter_result(4.into()), pending_or_failure);
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
                connections.waiter_result(4.into()),
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

#[test]
fn enabled_unknown_mode_is_default_only_and_strict_duplicates_are_validated() {
    let mut connections = ready_connections();
    reserve_operation(&mut connections, Operation::Subscription);
    accept(&mut connections, 4.into());
    record(&mut connections, enabled(4.into(), None));
    assert_eq!(connections.waiter_result(4.into()), None);
    terminal(&mut connections, 4.into(), Ok(()));
    assert_eq!(
        connections.waiter_result(4.into()),
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
                    .buffer_before_dispatch(enabled(4.into(), actual_mode))
                    .unwrap()
                    .unwrap();
                assert!(matches!(
                    connections.record_event(&invalid),
                    Err(ReconcileFault::UnexpectedNotificationMode)
                ));
                assert_eq!(connections.waiter_result(4.into()), None);
            }
        }
    }
}
