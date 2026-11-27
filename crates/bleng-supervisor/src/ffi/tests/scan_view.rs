use super::*;
use crate::{ScanDeliveryError, SupervisorOperationError};
use bleng::BlengError;
use std::sync::{Arc, Weak};

#[test]
fn peripheral_identity_compares_every_fixed_context_field_not_only_numeric_id() {
    let context = ForeignOwnerContext {
        owner_id: "owner".to_owned(),
        backend_id: "backend".to_owned(),
        owner_generation: 1,
        recovery_attempt: 1,
    };
    let old = Arc::new(SupervisedPeripheral {
        supervisor: Weak::new(),
        context: context.clone(),
        id: 17.into(),
        local_name: None,
    });
    let same = Arc::new(SupervisedPeripheral {
        supervisor: Weak::new(),
        context: context.clone(),
        id: 17.into(),
        local_name: None,
    });
    assert!(old.same_peripheral(same));
    assert!(!old.same_peripheral(Arc::new(SupervisedPeripheral {
        supervisor: Weak::new(),
        context: context.clone(),
        id: 18.into(),
        local_name: None,
    })));
    for field in 0..4 {
        let mut fresh = context.clone();
        match field {
            0 => fresh.owner_id.push_str("-fresh"),
            1 => fresh.backend_id.push_str("-fresh"),
            2 => fresh.owner_generation += 1,
            _ => fresh.recovery_attempt += 1,
        }
        assert!(!old.same_peripheral(Arc::new(SupervisedPeripheral {
            supervisor: Weak::new(),
            context: fresh,
            id: 17.into(),
            local_name: None,
        })));
    }
}

#[test]
fn peripheral_local_name_is_the_advertisement_name_and_absent_when_unknown() {
    let context = ForeignOwnerContext {
        owner_id: "owner".to_owned(),
        backend_id: "backend".to_owned(),
        owner_generation: 1,
        recovery_attempt: 1,
    };
    let named = SupervisedPeripheral {
        supervisor: Weak::new(),
        context: context.clone(),
        id: 17.into(),
        local_name: Some("BleHub Peripheral".to_owned()),
    };
    assert_eq!(named.local_name().as_deref(), Some("BleHub Peripheral"));
    let adopted = SupervisedPeripheral {
        supervisor: Weak::new(),
        context,
        id: 18.into(),
        local_name: None,
    };
    assert_eq!(adopted.local_name(), None);
}

#[test]
fn options_canonicalize_only_contract_filters_and_accept_company_endpoints() {
    for company_id in [None, Some(0.0), Some(-0.0), Some(65535.0)] {
        let options = ForeignScanOptions {
            android: None,
            service_uuids: vec!["180d".to_owned(), "12345678".to_owned()],
            name_prefix: Some("é".repeat(124)),
            exact_name: None,
            company_id,
            min_rssi: None,
            manufacturer_data: None,
            allow_duplicates: true,
        }
        .validate()
        .unwrap();
        assert_eq!(options.company_id, company_id.map(|value| value as u16));
        assert_eq!(
            options.service_uuids[0],
            blehub::BluetoothUuid::canonicalize("180d").unwrap()
        );
        assert_eq!(
            options.service_uuids[1],
            blehub::BluetoothUuid::canonicalize("12345678").unwrap()
        );
        assert!(options.allow_duplicates);
    }
}

#[test]
fn android_scan_tuning_is_bounded_at_the_ffi_boundary() {
    let base = ForeignScanOptions {
        service_uuids: vec![],
        name_prefix: None,
        exact_name: None,
        company_id: None,
        min_rssi: None,
        manufacturer_data: None,
        android: Some(ForeignAndroidScanOptions {
            scan_mode: Some(blehub::AndroidScanMode::LowLatency),
            phy: Some(blehub::AndroidScanPhy::Le1m),
            report_delay_ms: Some(5000.0),
            legacy: Some(false),
        }),
        allow_duplicates: false,
    };
    let options = base.clone().validate().unwrap();
    let android = options.android.unwrap();
    assert_eq!(android.report_delay_ms, Some(5000));
    assert_eq!(android.scan_mode, Some(blehub::AndroidScanMode::LowLatency));
    assert_eq!(android.phy, Some(blehub::AndroidScanPhy::Le1m));

    let mut too_long = base;
    too_long.android = Some(ForeignAndroidScanOptions {
        report_delay_ms: Some(5001.0),
        ..Default::default()
    });
    // The cap itself belongs to the canonical filter rule, not to this conversion.
    let failure = too_long.validate().unwrap_err();
    assert_eq!(failure.code, "invalid_scan_options");
    assert!(failure.message.contains("at most 5000"), "{failure:?}");
}

#[test]
fn lifecycle_delivery_and_boundary_domains_are_not_collapsed() {
    use ForeignOperationFailureDomain as Domain;
    for (error, code, domain) in [
        (
            SupervisorOperationError::NoActiveSession,
            "supervisor.no_active_session",
            Domain::Lifecycle,
        ),
        (
            SupervisorOperationError::StaleContext,
            "supervisor.stale_context",
            Domain::Lifecycle,
        ),
        (
            SupervisorOperationError::RecoveryNotReady,
            "supervisor.recovery_not_ready",
            Domain::Lifecycle,
        ),
        (
            SupervisorOperationError::SupervisorBusy,
            "supervisor.busy",
            Domain::Lifecycle,
        ),
        (
            SupervisorOperationError::SupervisorUnavailable,
            "supervisor.unavailable",
            Domain::Lifecycle,
        ),
        (
            SupervisorOperationError::InvalidContext {
                detail: "original context cause".to_owned(),
            },
            "supervisor.invalid_context",
            Domain::Input,
        ),
    ] {
        let failure = session_failure(ScanSessionError::Submission { error });
        assert_eq!(failure.code, code);
        assert_eq!(failure.domain, domain);
    }
    for (error, code) in [
        (ScanDeliveryError::PullBusy, "pull_busy"),
        (ScanDeliveryError::StartFailed, "start_failed"),
        (
            ScanDeliveryError::AdvertisementOverflow,
            "advertisement_overflow",
        ),
        (ScanDeliveryError::ByteOverflow, "byte_overflow"),
        (ScanDeliveryError::StopDataOverflow, "stop_data_overflow"),
        (
            ScanDeliveryError::PendingEventOverflow,
            "pending_event_overflow",
        ),
        (ScanDeliveryError::OwnerRetired, "owner_retired"),
        (ScanDeliveryError::Detached, "detached"),
        (ScanDeliveryError::Unavailable, "unavailable"),
        (ScanDeliveryError::ConflictingStart, "conflicting_start"),
    ] {
        let failure = session_failure(ScanSessionError::Delivery { error });
        assert_eq!(failure.code, format!("delivery.{code}"));
        assert_eq!(failure.domain, Domain::Delivery);
    }
    let failure = session_failure(ScanSessionError::DeliveryBusy);
    assert_eq!(
        (failure.domain, failure.code.as_str()),
        (Domain::Delivery, "delivery.busy")
    );
    let failure = session_failure(ScanSessionError::StartNotObserved);
    assert_eq!(
        (failure.domain, failure.code.as_str()),
        (Domain::Submission, "scan.start_not_observed")
    );
    for (error, domain) in [
        (
            ScanSessionError::Submission {
                error: SupervisorOperationError::Bleng {
                    error: BlengError::Timeout,
                },
            },
            Domain::Submission,
        ),
        (
            ScanSessionError::Operation {
                error: BlengError::Timeout,
            },
            Domain::Operation,
        ),
        (
            ScanSessionError::Observation {
                error: BlengError::Timeout,
            },
            Domain::Observation,
        ),
    ] {
        let failure = session_failure(error);
        assert_eq!(failure.domain, domain);
        assert_eq!(failure.code, "bleng.timeout");
    }
    for error in [
        ScanSessionError::Submission {
            error: SupervisorOperationError::Bleng {
                error: BlengError::InvalidTimeout,
            },
        },
        ScanSessionError::Operation {
            error: BlengError::InvalidTimeout,
        },
        ScanSessionError::Observation {
            error: BlengError::InvalidTimeout,
        },
    ] {
        let failure = session_failure(error);
        assert_eq!(
            (failure.domain, failure.code.as_str()),
            (Domain::Input, "invalid_timeout")
        );
    }
}
