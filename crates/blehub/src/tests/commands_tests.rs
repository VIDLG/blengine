use super::*;
use crate::ManufacturerData;

fn uuid(value: &str) -> BluetoothUuid {
    BluetoothUuid {
        value: value.to_owned(),
    }
}

fn observation() -> AdvertisementObservation {
    AdvertisementObservation {
        scan_session_id: ScanSessionId { value: 1 },
        peripheral_id: PeripheralId { value: 2 },
        local_name: Some("BleHub Fixture".to_owned()),
        advertised_services: vec![uuid("0000180d-0000-1000-8000-00805f9b34fb")],
        manufacturer_data: vec![ManufacturerData {
            company_id: 0x1234,
            payload: vec![1, 2],
        }],
        service_data: Vec::new(),
        rssi: Some(-42),
        connectable: None,
    }
}

fn assert_invalid<T>(result: Result<T, CommandDispatchError>, detail: &str) {
    assert_eq!(
        result.err(),
        Some(CommandDispatchError::InvalidRequest {
            detail: detail.to_owned(),
        })
    );
}

#[test]
fn scan_predicate_uses_any_service_and_and_between_filter_groups() {
    let observation = observation();
    let matching = ScanOptions {
        service_uuids: vec![
            uuid("0000180f-0000-1000-8000-00805f9b34fb"),
            uuid("0000180d-0000-1000-8000-00805f9b34fb"),
        ],
        name_prefix: Some("BleHub".to_owned()),
        company_id: Some(0x1234),
        allow_duplicates: false,
    };
    assert!(matching.matches(&observation));

    for non_matching in [
        ScanOptions {
            service_uuids: vec![uuid("0000180f-0000-1000-8000-00805f9b34fb")],
            ..matching.clone()
        },
        ScanOptions {
            name_prefix: Some("blehub".to_owned()),
            ..matching.clone()
        },
        ScanOptions {
            company_id: Some(0xffff),
            ..matching.clone()
        },
    ] {
        assert!(!non_matching.matches(&observation));
    }
}

#[test]
fn required_missing_observation_fields_do_not_match() {
    let observation = AdvertisementObservation {
        local_name: None,
        advertised_services: Vec::new(),
        manufacturer_data: Vec::new(),
        ..observation()
    };
    let options = ScanOptions {
        service_uuids: vec![uuid("0000180d-0000-1000-8000-00805f9b34fb")],
        name_prefix: Some("BleHub".to_owned()),
        company_id: Some(0x1234),
        allow_duplicates: true,
    };
    assert!(!options.matches(&observation));
    assert!(ScanOptions::default().matches(&observation));
}

#[test]
fn scan_options_enforce_wire_guards() {
    let valid = StartScanCommand {
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
    let canonical = valid.clone().validated().unwrap();
    assert_eq!(
        canonical.options.service_uuids,
        vec![BluetoothUuid {
            value: "0000180d-0000-1000-8000-00805f9b34fb".to_owned(),
        }]
    );

    let mut command = valid.clone();
    command.options.service_uuids = vec![BluetoothUuid {
        value: String::new(),
    }];
    assert_invalid(
        command.validated(),
        "service_uuids[0] is not a valid Bluetooth UUID",
    );

    let mut command = valid.clone();
    command.options.name_prefix = Some(String::new());
    assert_invalid(command.validated(), "name_prefix must not be empty");

    let mut command = valid;
    command.options.name_prefix = Some("x".repeat(MAX_SCAN_NAME_PREFIX_BYTES + 1));
    assert_invalid(command.validated(), "name_prefix exceeds 248 UTF-8 bytes");
}

#[test]
fn write_value_enforces_wire_guard() {
    let mut write = WriteCharacteristicCommand {
        command_id: CommandId { value: 1 },
        connection_id: ConnectionId { value: 2 },
        gatt_session_id: GattSessionId { value: 3 },
        characteristic_id: GattCharacteristicId { value: 4 },
        value: vec![0; MAX_WRITE_VALUE_BYTES],
        write_type: WriteType::WithResponse,
    };
    assert_eq!(write.validate(), Ok(()));
    write.value.push(0);
    assert_invalid(write.validate(), "value contains 513 bytes; maximum is 512");
}
