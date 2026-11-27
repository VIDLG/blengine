use blehub::{
    AdapterSnapshot, AdapterState, AdvertisementObservation, AuthorizationState, BleErrorCategory,
    BleEvent, BleEventError, BluetoothUuid, CharacteristicProperties, CommandId, ConnectionId,
    ConnectionState, ConnectionStateResult, GattCharacteristic, GattCharacteristicId, GattDatabase,
    GattDescriptor, GattDescriptorId, GattService, GattServiceId, GattSessionId, ManufacturerData,
    NotificationMode, NotificationState, PeripheralId, Platform, PlatformErrorDetails,
    ScanSessionId, ScanStopReason, ScanStopResult, ServiceData, SubscriptionId,
};

fn uuid(value: &str) -> BluetoothUuid {
    BluetoothUuid {
        value: value.to_owned(),
    }
}

pub fn advertisement_observation_fixture() -> AdvertisementObservation {
    AdvertisementObservation {
        scan_session_id: ScanSessionId { value: 101 },
        peripheral_id: PeripheralId { value: 201 },
        local_name: Some("BleHub Fixture".to_owned()),
        advertised_services: vec![uuid("0000180d-0000-1000-8000-00805f9b34fb")],
        manufacturer_data: vec![ManufacturerData {
            company_id: 0x1234,
            payload: vec![0x01, 0x02],
        }],
        service_data: vec![ServiceData {
            service_uuid: uuid("0000180d-0000-1000-8000-00805f9b34fb"),
            payload: vec![0x03, 0x04],
        }],
        rssi: Some(-42),
        connectable: None,
    }
}

pub fn duplicate_gatt_database_fixture() -> GattDatabase {
    let service_uuid = uuid("0000180d-0000-1000-8000-00805f9b34fb");
    let characteristic_uuid = uuid("00002a37-0000-1000-8000-00805f9b34fb");
    let properties = CharacteristicProperties {
        readable: true,
        writable_with_response: false,
        writable_without_response: false,
        notifiable: true,
        indicatable: false,
    };

    GattDatabase {
        connection_id: ConnectionId { value: 301 },
        gatt_session_id: GattSessionId { value: 401 },
        services: vec![
            GattService {
                id: GattServiceId { value: 501 },
                uuid: service_uuid.clone(),
                primary: Some(true),
                characteristics: vec![GattCharacteristic {
                    id: GattCharacteristicId { value: 601 },
                    uuid: characteristic_uuid.clone(),
                    properties,
                    descriptors: vec![GattDescriptor {
                        id: GattDescriptorId { value: 701 },
                        uuid: uuid("00002902-0000-1000-8000-00805f9b34fb"),
                    }],
                }],
            },
            GattService {
                id: GattServiceId { value: 502 },
                uuid: service_uuid,
                primary: Some(false),
                characteristics: vec![GattCharacteristic {
                    id: GattCharacteristicId { value: 602 },
                    uuid: characteristic_uuid,
                    properties,
                    descriptors: vec![GattDescriptor {
                        id: GattDescriptorId { value: 702 },
                        uuid: uuid("00002902-0000-1000-8000-00805f9b34fb"),
                    }],
                }],
            },
        ],
    }
}

pub fn unified_event_catalog_fixture() -> Vec<BleEvent> {
    let advertisement = advertisement_observation_fixture();
    let database = duplicate_gatt_database_fixture();
    let command_id = CommandId { value: 1 };
    let connection_id = database.connection_id;
    let gatt_session_id = database.gatt_session_id;
    let characteristic_id = database.services[0].characteristics[0].id;
    let subscription_id = SubscriptionId { value: 801 };
    let error = BleEventError {
        category: BleErrorCategory::Gatt,
        message: "fixture failure".to_owned(),
        platform_details: Some(Box::new(PlatformErrorDetails {
            platform: Platform::Test,
            code: Some("fixture-code".to_owned()),
            name: Some("FixtureError".to_owned()),
            message: Some("fixture platform detail".to_owned()),
            status: None,
            att_code: None,
        })),
    };

    vec![
        BleEvent::AdapterSnapshotChanged {
            command_id: Some(command_id),
            snapshot: AdapterSnapshot {
                state: AdapterState::PoweredOn,
                scan_authorization: AuthorizationState::Authorized,
                connection_authorization: AuthorizationState::Authorized,
                name: Some("Fixture Adapter".to_owned()),
            },
        },
        BleEvent::ScanStarted {
            command_id,
            scan_id: advertisement.scan_session_id,
        },
        BleEvent::AdvertisementObserved { advertisement },
        BleEvent::ScanStopped {
            scan_id: ScanSessionId { value: 101 },
            result: ScanStopResult::Ok {
                reason: ScanStopReason::Requested { command_id },
            },
        },
        BleEvent::ScanStopped {
            scan_id: ScanSessionId { value: 102 },
            result: ScanStopResult::Err {
                error: error.clone(),
            },
        },
        BleEvent::ConnectionStateChanged {
            command_id: Some(command_id),
            connection_id,
            result: ConnectionStateResult::Ok {
                state: ConnectionState::Connected,
            },
        },
        BleEvent::ConnectionStateChanged {
            command_id: None,
            connection_id,
            result: ConnectionStateResult::Err {
                error: error.clone(),
            },
        },
        BleEvent::GattDiscovered {
            command_id,
            database,
        },
        BleEvent::CharacteristicRead {
            command_id,
            connection_id,
            gatt_session_id,
            characteristic_id,
            value: vec![0x10, 0x11],
        },
        BleEvent::CharacteristicWritten {
            command_id,
            connection_id,
            gatt_session_id,
            characteristic_id,
        },
        BleEvent::NotificationStateChanged {
            command_id,
            connection_id,
            gatt_session_id,
            subscription_id,
            characteristic_id,
            state: NotificationState::Enabled {
                actual_mode: Some(NotificationMode::Notification),
            },
        },
        BleEvent::NotificationReceived {
            connection_id,
            gatt_session_id,
            subscription_id,
            characteristic_id,
            value: vec![0x20, 0x21],
        },
        BleEvent::NotificationStateChanged {
            command_id,
            connection_id,
            gatt_session_id,
            subscription_id,
            characteristic_id,
            state: NotificationState::Disabled,
        },
        BleEvent::RssiRead {
            command_id,
            connection_id,
            rssi: -48,
        },
        BleEvent::CommandCompleted {
            command_id,
            error: None,
        },
        BleEvent::CommandCompleted {
            command_id: CommandId { value: 2 },
            error: Some(error.clone()),
        },
        BleEvent::BackendError { error },
    ]
}
