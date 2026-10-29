use crate::runtime::messages::{
    BeginConnect, BeginDisableNotifications, BeginDisconnect, BeginDiscoverGatt,
    BeginEnableNotifications, BeginInitialize, BeginRead, BeginRssi, BeginScan, BeginStop,
    BeginWrite, OperationSpec,
};
use crate::waiter::WaiterSuccess;
use blehub::{
    AdapterSnapshot, AdapterState, AuthorizationState, BluetoothUuid, ConnectionId,
    GattCharacteristicId, GattDatabase, GattService, GattServiceId, GattSessionId,
    NotificationMode, ScanSessionId, SubscriptionId,
};

const CONNECTION: ConnectionId = ConnectionId { value: 11 };
const SESSION: GattSessionId = GattSessionId { value: 22 };
const CHARACTERISTIC: GattCharacteristicId = GattCharacteristicId { value: 33 };
const SUBSCRIPTION: SubscriptionId = SubscriptionId { value: 44 };
const SCAN: ScanSessionId = ScanSessionId { value: 55 };

#[test]
fn initialization_moves_snapshot_and_allocating_operations_require_their_resource() {
    let snapshot = AdapterSnapshot {
        state: AdapterState::PoweredOn,
        scan_authorization: AuthorizationState::Authorized,
        connection_authorization: AuthorizationState::Authorized,
        name: Some("adapter payload".into()),
    };
    let name_ptr = snapshot.name.as_ref().unwrap().as_ptr();
    let decoded =
        BeginInitialize::decode_success((), (), WaiterSuccess::Initialized(snapshot)).unwrap();
    assert_eq!(decoded.state, AdapterState::PoweredOn);
    assert_eq!(decoded.scan_authorization, AuthorizationState::Authorized);
    assert_eq!(
        decoded.connection_authorization,
        AuthorizationState::Authorized
    );
    assert_eq!(decoded.name.as_deref(), Some("adapter payload"));
    assert_eq!(decoded.name.as_ref().unwrap().as_ptr(), name_ptr);
    assert_eq!(
        BeginInitialize::decode_success((), (), WaiterSuccess::ScanStarted(SCAN)),
        None
    );

    assert_eq!(
        BeginScan::decode_success((), SCAN, WaiterSuccess::ScanStarted(SCAN)),
        Some(SCAN)
    );
    assert_eq!(
        BeginScan::decode_success(
            (),
            ScanSessionId { value: 56 },
            WaiterSuccess::ScanStarted(SCAN),
        ),
        None
    );
    assert_eq!(
        BeginScan::decode_success((), SCAN, WaiterSuccess::ScanStopped(SCAN)),
        None
    );
    assert_eq!(
        BeginConnect::decode_success((), CONNECTION, WaiterSuccess::Connected(CONNECTION)),
        Some(CONNECTION)
    );
    assert_eq!(
        BeginConnect::decode_success(
            (),
            ConnectionId { value: 12 },
            WaiterSuccess::Connected(CONNECTION),
        ),
        None
    );
    assert_eq!(
        BeginConnect::decode_success((), CONNECTION, WaiterSuccess::Disconnected(CONNECTION)),
        None
    );
}

#[test]
fn connection_and_scan_operations_reject_foreign_identities_and_rssi_keeps_its_value() {
    assert_eq!(
        BeginDisconnect::decode_success(
            ConnectionId { value: 12 },
            (),
            WaiterSuccess::Disconnected(CONNECTION),
        ),
        None
    );
    assert_eq!(
        BeginStop::decode_success(
            ScanSessionId { value: 56 },
            (),
            WaiterSuccess::ScanStopped(SCAN),
        ),
        None
    );
    assert_eq!(
        BeginRssi::decode_success(CONNECTION, (), WaiterSuccess::RssiRead(CONNECTION, -73)),
        Some(-73)
    );
    assert_eq!(
        BeginRssi::decode_success(
            ConnectionId { value: 12 },
            (),
            WaiterSuccess::RssiRead(CONNECTION, -73),
        ),
        None
    );
    assert_eq!(
        BeginRssi::decode_success(CONNECTION, (), WaiterSuccess::Disconnected(CONNECTION)),
        None
    );
}

#[test]
fn discovery_requires_connection_and_allocated_session_and_moves_database() {
    for (connection_id, gatt_session_id) in [
        (ConnectionId { value: 12 }, SESSION),
        (CONNECTION, GattSessionId { value: 23 }),
    ] {
        assert_eq!(
            BeginDiscoverGatt::decode_success(
                CONNECTION,
                SESSION,
                WaiterSuccess::GattDiscovered(GattDatabase {
                    connection_id,
                    gatt_session_id,
                    services: vec![],
                }),
            ),
            None
        );
    }
    assert_eq!(
        BeginDiscoverGatt::decode_success(
            CONNECTION,
            SESSION,
            WaiterSuccess::Connected(CONNECTION),
        ),
        None
    );
    let database = GattDatabase {
        connection_id: CONNECTION,
        gatt_session_id: SESSION,
        services: vec![GattService {
            id: GattServiceId { value: 66 },
            uuid: BluetoothUuid::canonicalize("180f").unwrap(),
            primary: Some(true),
            characteristics: vec![],
        }],
    };
    let services_ptr = database.services.as_ptr();
    let decoded = BeginDiscoverGatt::decode_success(
        CONNECTION,
        SESSION,
        WaiterSuccess::GattDiscovered(database),
    )
    .unwrap();
    assert_eq!(decoded.connection_id, CONNECTION);
    assert_eq!(decoded.gatt_session_id, SESSION);
    assert_eq!(decoded.services.as_ptr(), services_ptr);
    assert_eq!(decoded.services.len(), 1);
    assert_eq!(decoded.services[0].id, GattServiceId { value: 66 });
    assert_eq!(
        decoded.services[0].uuid,
        BluetoothUuid::canonicalize("180f").unwrap()
    );
    assert_eq!(decoded.services[0].primary, Some(true));
}

#[test]
fn characteristic_operations_check_each_identity_axis_and_move_read_payload() {
    let correlation = (CONNECTION, SESSION, CHARACTERISTIC);
    for (connection_id, gatt_session_id, characteristic_id) in [
        (ConnectionId { value: 12 }, SESSION, CHARACTERISTIC),
        (CONNECTION, GattSessionId { value: 23 }, CHARACTERISTIC),
        (CONNECTION, SESSION, GattCharacteristicId { value: 34 }),
    ] {
        assert_eq!(
            BeginRead::decode_success(
                correlation,
                (),
                WaiterSuccess::CharacteristicRead {
                    connection_id,
                    gatt_session_id,
                    characteristic_id,
                    value: vec![0xde, 0xad],
                },
            ),
            None
        );
        assert_eq!(
            BeginWrite::decode_success(
                correlation,
                (),
                WaiterSuccess::CharacteristicWritten(
                    connection_id,
                    gatt_session_id,
                    characteristic_id,
                ),
            ),
            None
        );
        assert_eq!(
            BeginEnableNotifications::decode_success(
                correlation,
                SUBSCRIPTION,
                WaiterSuccess::NotificationsEnabled {
                    connection_id,
                    gatt_session_id,
                    subscription_id: SUBSCRIPTION,
                    characteristic_id,
                    actual_mode: Some(NotificationMode::Notification),
                },
            ),
            None
        );
    }
    assert_eq!(
        BeginRead::decode_success(
            correlation,
            (),
            WaiterSuccess::CharacteristicWritten(CONNECTION, SESSION, CHARACTERISTIC),
        ),
        None
    );
    let value = vec![0xde, 0xad, 0xbe, 0xef];
    let value_ptr = value.as_ptr();
    let decoded = BeginRead::decode_success(
        correlation,
        (),
        WaiterSuccess::CharacteristicRead {
            connection_id: CONNECTION,
            gatt_session_id: SESSION,
            characteristic_id: CHARACTERISTIC,
            value,
        },
    )
    .unwrap();
    assert_eq!(decoded, [0xde, 0xad, 0xbe, 0xef]);
    assert_eq!(decoded.as_ptr(), value_ptr);
}

#[test]
fn enable_requires_allocated_subscription_and_preserves_optional_actual_mode() {
    let correlation = (CONNECTION, SESSION, CHARACTERISTIC);
    for actual_mode in [
        None,
        Some(NotificationMode::Notification),
        Some(NotificationMode::Indication),
    ] {
        assert_eq!(
            BeginEnableNotifications::decode_success(
                correlation,
                SUBSCRIPTION,
                WaiterSuccess::NotificationsEnabled {
                    connection_id: CONNECTION,
                    gatt_session_id: SESSION,
                    subscription_id: SUBSCRIPTION,
                    characteristic_id: CHARACTERISTIC,
                    actual_mode,
                },
            ),
            Some((SUBSCRIPTION, actual_mode))
        );
    }
    assert_eq!(
        BeginEnableNotifications::decode_success(
            correlation,
            SubscriptionId { value: 45 },
            WaiterSuccess::NotificationsEnabled {
                connection_id: CONNECTION,
                gatt_session_id: SESSION,
                subscription_id: SUBSCRIPTION,
                characteristic_id: CHARACTERISTIC,
                actual_mode: None,
            },
        ),
        None
    );
    assert_eq!(
        BeginEnableNotifications::decode_success(
            correlation,
            SUBSCRIPTION,
            WaiterSuccess::NotificationsDisabled(CONNECTION, SESSION, SUBSCRIPTION, CHARACTERISTIC),
        ),
        None
    );
}

#[test]
fn disable_checks_each_request_identity_but_does_not_require_characteristic() {
    let correlation = (CONNECTION, SESSION, SUBSCRIPTION);
    for (connection, session, subscription) in [
        (ConnectionId { value: 12 }, SESSION, SUBSCRIPTION),
        (CONNECTION, GattSessionId { value: 23 }, SUBSCRIPTION),
        (CONNECTION, SESSION, SubscriptionId { value: 45 }),
    ] {
        assert_eq!(
            BeginDisableNotifications::decode_success(
                correlation,
                (),
                WaiterSuccess::NotificationsDisabled(
                    connection,
                    session,
                    subscription,
                    CHARACTERISTIC
                ),
            ),
            None
        );
    }
    for characteristic in [CHARACTERISTIC, GattCharacteristicId { value: 34 }] {
        assert_eq!(
            BeginDisableNotifications::decode_success(
                correlation,
                (),
                WaiterSuccess::NotificationsDisabled(
                    CONNECTION,
                    SESSION,
                    SUBSCRIPTION,
                    characteristic
                ),
            ),
            Some(())
        );
    }
}

#[test]
fn unit_outputs_do_not_make_distinct_operations_interchangeable() {
    for operation in 0..4 {
        for (variant, success) in [
            WaiterSuccess::ScanStopped(SCAN),
            WaiterSuccess::Disconnected(CONNECTION),
            WaiterSuccess::CharacteristicWritten(CONNECTION, SESSION, CHARACTERISTIC),
            WaiterSuccess::NotificationsDisabled(CONNECTION, SESSION, SUBSCRIPTION, CHARACTERISTIC),
        ]
        .into_iter()
        .enumerate()
        {
            let decoded = match operation {
                0 => BeginStop::decode_success(SCAN, (), success),
                1 => BeginDisconnect::decode_success(CONNECTION, (), success),
                2 => BeginWrite::decode_success((CONNECTION, SESSION, CHARACTERISTIC), (), success),
                _ => BeginDisableNotifications::decode_success(
                    (CONNECTION, SESSION, SUBSCRIPTION),
                    (),
                    success,
                ),
            };
            assert_eq!(
                decoded,
                (operation == variant).then_some(()),
                "operation {operation}, variant {variant}"
            );
        }
    }
}
