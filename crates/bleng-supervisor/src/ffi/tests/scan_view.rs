use super::*;
use blehub::{BleErrorCategory, CommandDispatchError, Platform};
use bleng::{BlengBusyReason, BlengContractFailure, BlengNotReadyReason, BlengResource};

fn details() -> blehub::PlatformErrorDetails {
    blehub::PlatformErrorDetails {
        platform: Platform::Apple,
        code: Some("42".to_owned()),
        name: Some("SDKError".to_owned()),
        message: Some("SDK é\0message".to_owned()),
    }
}

#[test]
fn command_id_errors_are_redacted_at_both_typed_nesting_levels() {
    for id in [1, 9_007_199_254_740_999, u64::MAX] {
        let command_id = id.into();
        for (error, suffix) in [
            (
                BlengError::OperationNotFound { command_id },
                "operation_not_found",
            ),
            (
                BlengError::OperationAlreadyCompleted { command_id },
                "operation_already_completed",
            ),
            (
                BlengError::OperationNotCancellable { command_id },
                "operation_not_cancellable",
            ),
            (
                BlengError::DispatchRejected {
                    error: CommandDispatchError::OperationNotFound {
                        target_command_id: command_id,
                    },
                },
                "dispatch_rejected.operation_not_found",
            ),
            (
                BlengError::DispatchRejected {
                    error: CommandDispatchError::OperationAlreadyCompleted {
                        target_command_id: command_id,
                    },
                },
                "dispatch_rejected.operation_already_completed",
            ),
            (
                BlengError::DispatchRejected {
                    error: CommandDispatchError::OperationNotCancellable {
                        target_command_id: command_id,
                    },
                },
                "dispatch_rejected.operation_not_cancellable",
            ),
        ] {
            let failure = session_failure(ScanSessionError::Submission {
                error: SupervisorOperationError::Bleng { error },
            });
            assert_eq!(failure.code, format!("bleng.{suffix}"));
            assert!(!failure.message.contains(&id.to_string()));
            assert!(!failure.message.contains("CommandId"));
            assert!(failure.platform_details.is_none());
        }
    }
}

#[test]
fn every_nested_bleng_reason_retains_a_distinct_stable_code() {
    let domain = ForeignScanFailureDomain::Operation;
    let mut cases = vec![
        (BlengError::Closed, "closed"),
        (BlengError::Cancelled, "cancelled"),
        (
            BlengError::OperationResultConsumed,
            "operation_result_consumed",
        ),
        (BlengError::Disconnected, "disconnected"),
        (BlengError::InvalidTimeout, "invalid_timeout"),
        (BlengError::Timeout, "timeout"),
        (BlengError::TransportFault, "transport_fault"),
        (BlengError::WorkerStopped, "worker_stopped"),
    ];
    for (reason, suffix) in [
        (
            BlengNotReadyReason::BackendNotAssembled,
            "not_ready.backend_not_assembled",
        ),
        (
            BlengNotReadyReason::NotInitialized,
            "not_ready.not_initialized",
        ),
        (
            BlengNotReadyReason::ScanNotActive,
            "not_ready.scan_not_active",
        ),
        (
            BlengNotReadyReason::ConnectionNotActive,
            "not_ready.connection_not_active",
        ),
        (
            BlengNotReadyReason::GattNotReady,
            "not_ready.gatt_not_ready",
        ),
        (
            BlengNotReadyReason::SubscriptionNotActive,
            "not_ready.subscription_not_active",
        ),
    ] {
        cases.push((BlengError::NotReady { reason }, suffix));
    }
    for (reason, suffix) in [
        (BlengBusyReason::Scan, "busy.scan"),
        (BlengBusyReason::Peripheral, "busy.peripheral"),
        (BlengBusyReason::Gatt, "busy.gatt"),
        (BlengBusyReason::Operation, "busy.operation"),
        (
            BlengBusyReason::BackendSlotOccupied,
            "busy.backend_slot_occupied",
        ),
    ] {
        cases.push((BlengError::Busy { reason }, suffix));
    }
    for (resource, suffix) in [
        (
            BlengResource::PendingWaiters,
            "resource_exhausted.pending_waiters",
        ),
        (
            BlengResource::SubmissionQueue,
            "resource_exhausted.submission_queue",
        ),
        (BlengResource::Identifiers, "resource_exhausted.identifiers"),
    ] {
        cases.push((BlengError::ResourceExhausted { resource }, suffix));
    }
    for (reason, suffix) in [
        (
            BlengContractFailure::InvalidCommandId,
            "contract_failure.invalid_command_id",
        ),
        (
            BlengContractFailure::DuplicateHubOwnership,
            "contract_failure.duplicate_hub_ownership",
        ),
    ] {
        cases.push((BlengError::ContractFailure { reason }, suffix));
    }
    for (error, suffix) in cases {
        let failure = bleng_failure(domain, error);
        if suffix == "invalid_timeout" {
            assert_eq!(failure.domain, ForeignScanFailureDomain::Input);
            assert_eq!(failure.code, suffix);
        } else {
            assert_eq!(failure.domain, domain);
            assert_eq!(failure.code, format!("bleng.{suffix}"));
        }
        assert!(!failure.message.is_empty());
    }
}

#[test]
fn dispatch_and_hub_categories_preserve_the_original_detail_and_platform_fields() {
    for (error, suffix, message, platform_details) in [
        (
            CommandDispatchError::Closed,
            "closed",
            "BleHub is closed",
            None,
        ),
        (
            CommandDispatchError::InvalidRequest {
                detail: "request".to_owned(),
            },
            "invalid_request",
            "request",
            None,
        ),
        (
            CommandDispatchError::InvalidState {
                detail: "state".to_owned(),
            },
            "invalid_state",
            "state",
            None,
        ),
        (
            CommandDispatchError::ResourceExhausted {
                detail: "resource".to_owned(),
            },
            "resource_exhausted",
            "resource",
            None,
        ),
        (
            CommandDispatchError::BackendUnavailable {
                detail: "SDK detail".to_owned(),
                platform_details: Some(details()),
            },
            "backend_unavailable",
            "SDK detail",
            Some(details()),
        ),
        (
            CommandDispatchError::FfiContract {
                detail: "callback".to_owned(),
            },
            "ffi_contract",
            "callback",
            None,
        ),
    ] {
        let failure = bleng_failure(
            ForeignScanFailureDomain::Operation,
            BlengError::DispatchRejected { error },
        );
        assert_eq!(failure.code, format!("bleng.dispatch_rejected.{suffix}"));
        assert_eq!(failure.message, message);
        assert_eq!(failure.platform_details, platform_details);
    }
    for cleanup in [false, true] {
        for (error, suffix, message, platform_details) in [
            (
                blehub::BleHubError::BackendAssembly {
                    detail: "assembly".to_owned(),
                },
                "backend_assembly",
                "assembly",
                None,
            ),
            (
                blehub::BleHubError::BackendCleanup {
                    detail: "cleanup".to_owned(),
                    platform_details: Some(details()),
                },
                "backend_cleanup",
                "cleanup",
                Some(details()),
            ),
            (
                blehub::BleHubError::ResourceBusy {
                    detail: "busy".to_owned(),
                },
                "resource_busy",
                "busy",
                None,
            ),
            (
                blehub::BleHubError::InvalidConfiguration {
                    field: "input".to_owned(),
                    detail: "config".to_owned(),
                },
                "invalid_configuration",
                "config",
                None,
            ),
            (
                blehub::BleHubError::FfiContract {
                    detail: "ffi".to_owned(),
                },
                "ffi_contract",
                "ffi",
                None,
            ),
        ] {
            let (error, path) = if cleanup {
                (BlengError::Cleanup { error }, "cleanup")
            } else {
                (BlengError::Assembly { error }, "assembly")
            };
            let failure = bleng_failure(ForeignScanFailureDomain::Submission, error);
            assert_eq!(failure.code, format!("bleng.{path}.{suffix}"));
            assert_eq!(failure.message, message);
            assert_eq!(failure.platform_details, platform_details);
        }
    }
}

#[test]
fn every_ble_category_keeps_real_message_and_sdk_details() {
    for (category, suffix) in [
        (BleErrorCategory::Permission, "permission"),
        (BleErrorCategory::AdapterUnavailable, "adapter_unavailable"),
        (BleErrorCategory::InvalidState, "invalid_state"),
        (BleErrorCategory::NotFound, "not_found"),
        (BleErrorCategory::Timeout, "timeout"),
        (BleErrorCategory::Cancelled, "cancelled"),
        (BleErrorCategory::OperationAborted, "operation_aborted"),
        (BleErrorCategory::Connection, "connection"),
        (BleErrorCategory::Gatt, "gatt"),
        (BleErrorCategory::Protocol, "protocol"),
        (BleErrorCategory::NotSupported, "not_supported"),
        (BleErrorCategory::Backend, "backend"),
        (BleErrorCategory::Internal, "internal"),
    ] {
        let failure = bleng_failure(
            ForeignScanFailureDomain::Operation,
            BlengError::Operation {
                error: blehub::BleEventError {
                    category,
                    message: "real BLE é\0".to_owned(),
                    platform_details: Some(details()),
                },
            },
        );
        assert_eq!(failure.code, format!("bleng.operation.{suffix}"));
        assert_eq!(failure.message, "real BLE é\0");
        assert_eq!(failure.platform_details, Some(details()));
    }
}

#[test]
fn peripheral_identity_compares_every_fixed_context_field_not_only_numeric_id() {
    let context = ForeignOwnerContext {
        owner_id: "owner".to_owned(),
        backend_id: "backend".to_owned(),
        owner_generation: 1,
        recovery_attempt: 1,
    };
    let old = Arc::new(SupervisedPeripheral {
        context: context.clone(),
        id: 17.into(),
    });
    let same = Arc::new(SupervisedPeripheral {
        context: context.clone(),
        id: 17.into(),
    });
    assert!(old.same_peripheral(same));
    assert!(!old.same_peripheral(Arc::new(SupervisedPeripheral {
        context: context.clone(),
        id: 18.into()
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
            context: fresh,
            id: 17.into()
        })));
    }
}

#[test]
fn options_canonicalize_only_contract_filters_and_accept_company_endpoints() {
    for company_id in [None, Some(0.0), Some(-0.0), Some(65535.0)] {
        let options = ForeignScanOptions {
            service_uuids: vec!["180d".to_owned(), "12345678".to_owned()],
            name_prefix: Some("é".repeat(124)),
            company_id,
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
fn lifecycle_delivery_and_boundary_domains_are_not_collapsed() {
    use ForeignScanFailureDomain as Domain;
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
