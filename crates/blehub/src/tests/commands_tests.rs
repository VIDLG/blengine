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
        ..ScanOptions::default()
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
fn scan_predicate_honors_exact_name_and_min_rssi() {
    let observation = observation();
    assert!(
        ScanOptions {
            android: None,
            exact_name: Some("BleHub Fixture".to_owned()),
            ..Default::default()
        }
        .matches(&observation)
    );
    assert!(
        !ScanOptions {
            android: None,
            exact_name: Some("BleHub".to_owned()),
            ..Default::default()
        }
        .matches(&observation)
    );
    assert!(
        ScanOptions {
            android: None,
            min_rssi: Some(-42),
            ..Default::default()
        }
        .matches(&observation)
    );
    assert!(
        !ScanOptions {
            android: None,
            min_rssi: Some(-41),
            ..Default::default()
        }
        .matches(&observation)
    );
    // Absent RSSI never satisfies a threshold.
    let mut without_rssi = observation;
    without_rssi.rssi = None;
    assert!(
        !ScanOptions {
            android: None,
            min_rssi: Some(-42),
            ..Default::default()
        }
        .matches(&without_rssi)
    );
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
        android: None,
        service_uuids: vec![uuid("0000180d-0000-1000-8000-00805f9b34fb")],
        name_prefix: Some("BleHub".to_owned()),
        company_id: Some(0x1234),
        exact_name: None,
        min_rssi: None,
        manufacturer_data: None,
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
            android: None,
            service_uuids: vec![BluetoothUuid {
                value: "180D".to_owned(),
            }],
            name_prefix: Some("BleHub".to_owned()),
            company_id: Some(0x1234),
            exact_name: None,
            min_rssi: None,
            manufacturer_data: None,
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
    command.options.name_prefix = Some("x".repeat(MAX_LOCAL_NAME_BYTES + 1));
    assert_invalid(command.validated(), "name_prefix exceeds 248 UTF-8 bytes");
}

#[test]
fn android_scan_tuning_is_validated_but_does_not_change_matching() {
    let base = StartScanCommand {
        command_id: CommandId { value: 1 },
        scan_session_id: ScanSessionId { value: 2 },
        options: ScanOptions {
            android: Some(AndroidScanOptions {
                scan_mode: Some(AndroidScanMode::LowPower),
                phy: Some(AndroidScanPhy::Le2m),
                report_delay_ms: Some(MAX_SCAN_REPORT_DELAY_MS),
                legacy: Some(false),
            }),
            ..Default::default()
        },
    };
    assert!(base.clone().validated().is_ok());

    let mut too_long = base.clone();
    too_long.options.android = Some(AndroidScanOptions {
        report_delay_ms: Some(MAX_SCAN_REPORT_DELAY_MS + 1),
        ..Default::default()
    });
    assert_invalid(
        too_long.validated(),
        "android.report_delay_ms must be at most 5000",
    );

    // Scanner hints never participate in the fixed match predicate.
    let observation = observation();
    assert!(base.options.matches(&observation));
    assert!(ScanOptions::default().matches(&observation));
}

#[test]
fn manufacturer_data_filter_matches_prefix_or_mask() {
    let observation = observation(); // company 0x1234, payload [1, 2]
    let filter = |data: &[u8], mask: Option<&[u8]>| ScanOptions {
        manufacturer_data: Some(ManufacturerDataFilter {
            company_id: 0x1234,
            data: data.to_vec(),
            mask: mask.map(<[u8]>::to_vec),
        }),
        ..ScanOptions::default()
    };

    // Prefix semantics: the payload must start with the filter bytes.
    assert!(filter(&[1], None).matches(&observation));
    assert!(filter(&[1, 2], None).matches(&observation));
    assert!(!filter(&[2], None).matches(&observation));
    assert!(!filter(&[1, 2, 3], None).matches(&observation));

    // A different company id never matches, even with identical bytes.
    let other_company = ScanOptions {
        manufacturer_data: Some(ManufacturerDataFilter {
            company_id: 0xffff,
            data: vec![1],
            mask: None,
        }),
        ..ScanOptions::default()
    };
    assert!(!other_company.matches(&observation));

    // Masked equality: only the masked bits are compared.
    assert!(filter(&[0x01], Some(&[0x0f])).matches(&observation));
    assert!(filter(&[0x03], Some(&[0xf0])).matches(&observation));
    assert!(!filter(&[0x03], Some(&[0x0f])).matches(&observation));
    assert!(!filter(&[0x01, 0x03], Some(&[0xff, 0x0f])).matches(&observation));
    // A mask that disagrees with its data length matches nothing, even unvalidated.
    assert!(!filter(&[0x01, 0x02], Some(&[0xff])).matches(&observation));
}

#[test]
fn manufacturer_data_filter_is_bounded_and_masks_match_their_data() {
    let mut scan = StartScanCommand {
        command_id: CommandId { value: 1 },
        scan_session_id: ScanSessionId { value: 2 },
        options: ScanOptions {
            manufacturer_data: Some(ManufacturerDataFilter {
                company_id: 0x1234,
                data: vec![1, 2],
                mask: Some(vec![0xff, 0x00]),
            }),
            ..ScanOptions::default()
        },
    };
    assert!(scan.clone().validated().is_ok());

    scan.options.manufacturer_data = Some(ManufacturerDataFilter {
        company_id: 0x1234,
        data: vec![1, 2],
        mask: Some(vec![0xff]),
    });
    assert_invalid(
        scan.clone().validated(),
        "manufacturer_data.mask must match data length",
    );

    scan.options.manufacturer_data = Some(ManufacturerDataFilter {
        company_id: 0x1234,
        data: vec![0; MAX_WRITE_VALUE_BYTES + 1],
        mask: None,
    });
    assert_invalid(scan.validated(), "manufacturer_data.data exceeds 512 bytes");
}

#[test]
fn exact_name_must_be_non_empty_and_bounded() {
    for name in [String::new(), "x".repeat(MAX_LOCAL_NAME_BYTES + 1)] {
        let scan = StartScanCommand {
            command_id: CommandId { value: 1 },
            scan_session_id: ScanSessionId { value: 2 },
            options: ScanOptions {
                exact_name: Some(name.clone()),
                ..ScanOptions::default()
            },
        };
        assert!(
            matches!(
                scan.validated(),
                Err(CommandDispatchError::InvalidRequest { .. })
            ),
            "exact_name {name:?} must be rejected"
        );
    }
}

#[test]
fn bond_pin_must_be_numeric_and_bounded() {
    let bond = |pin: Option<&str>| CreateBondCommand {
        command_id: CommandId { value: 1 },
        connection_id: ConnectionId { value: 2 },
        pin: pin.map(str::to_owned),
    };

    assert!(bond(None).validated().is_ok());
    assert!(bond(Some("0")).validated().is_ok());
    assert!(bond(Some("123456")).validated().is_ok());
    assert!(bond(Some(&"9".repeat(16))).validated().is_ok());

    for pin in [
        "".to_owned(),
        "12345678901234567".to_owned(),
        "12a456".to_owned(),
        "12 456".to_owned(),
        "１２３".to_owned(),
    ] {
        assert_invalid(
            bond(Some(&pin)).validated(),
            "pin must be 1 to 16 ASCII digits",
        );
    }
}

#[test]
fn companion_association_requires_a_service_and_bounded_labels() {
    let association = |service_uuids: Vec<BluetoothUuid>| AssociateCompanionDeviceCommand {
        command_id: CommandId { value: 1 },
        service_uuids,
        display_name: None,
        device_profile: None,
        notify_on_device_nearby: false,
    };

    // At least one service UUID: the platform chooser filters candidates on it.
    assert_invalid(
        association(Vec::new()).validated(),
        "a companion association requires at least one service UUID",
    );
    // Services are canonicalized like every other UUID list.
    let accepted = association(vec![uuid("180D")]).validated().unwrap();
    assert_eq!(
        accepted.service_uuids[0],
        BluetoothUuid::canonicalize("180d").unwrap()
    );
    assert_invalid(
        association(vec![BluetoothUuid {
            value: "not-a-uuid".to_owned(),
        }])
        .validated(),
        "service_uuids[0] is not a valid Bluetooth UUID",
    );

    // The chooser label obeys the same local-name rule as a scan filter.
    let mut named = association(vec![uuid("180d")]);
    named.display_name = Some(String::new());
    assert_invalid(named.validated(), "display_name must not be empty");
    let mut named = association(vec![uuid("180d")]);
    named.display_name = Some("x".repeat(MAX_LOCAL_NAME_BYTES + 1));
    assert_invalid(named.validated(), "display_name exceeds 248 UTF-8 bytes");

    // The host's profile identifier is its own bound.
    let mut profiled = association(vec![uuid("180d")]);
    profiled.device_profile = Some("p".repeat(MAX_COMPANION_PROFILE_BYTES + 1));
    assert_invalid(
        profiled.validated(),
        "device_profile must be 1 to 128 UTF-8 bytes",
    );
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
    assert_eq!(write.clone().validated(), Ok(write.clone()));
    write.value.push(0);
    assert_invalid(
        write.validated(),
        "value contains 513 bytes; maximum is 512",
    );
}
