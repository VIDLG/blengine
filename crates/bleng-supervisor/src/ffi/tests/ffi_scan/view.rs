use super::*;
use crate::ForeignScanEndReason;

fn begin(fixture: &Fixture) -> Arc<SupervisedScan> {
    let result = Arc::clone(fixture.supervisor()).begin_scan_view(context(), scan_options());
    assert!(result.failure.is_none());
    result.scan.expect("exactly the scan branch")
}

fn invalid_timeouts() -> [f64; 11] {
    [
        f64::NAN,
        f64::INFINITY,
        f64::NEG_INFINITY,
        -1.0,
        -0.5,
        0.5,
        0.0,
        INVALID_LONG_TIMEOUT_MS as f64,
        18_446_744_073_709_549_568.0,
        u64::MAX as f64,
        f64::MAX,
    ]
}

#[test]
fn options_follow_owner_gates_and_invalid_inputs_allocate_no_command_or_delivery() {
    run(|| async {
        let mut fixture = Fixture::new([start(2, 1, vec![started(2, 1), completed(2)])]);
        let mut invalid = scan_options();
        invalid.company_id = Some(f64::NAN);
        let result = Arc::clone(fixture.supervisor()).begin_scan_view(context(), invalid.clone());
        assert!(result.scan.is_none());
        assert_failure(
            result.failure,
            ForeignOperationFailureDomain::Lifecycle,
            "supervisor.no_active_session",
        );
        fixture.supervisor().start().await.unwrap();
        let mut stale = context();
        stale.owner_generation += 1;
        let result = Arc::clone(fixture.supervisor()).begin_scan_view(stale, invalid.clone());
        assert!(result.scan.is_none());
        assert_failure(
            result.failure,
            ForeignOperationFailureDomain::Lifecycle,
            "supervisor.stale_context",
        );
        fixture
            .supervisor()
            .configure_old_operation_barrier(None, vec![])
            .unwrap();
        fixture
            .supervisor()
            .observe_signal(ForeignRecoverySignal::RadioLost)
            .unwrap();
        let result = Arc::clone(fixture.supervisor()).begin_scan_view(context(), invalid);
        assert!(result.scan.is_none());
        assert_failure(
            result.failure,
            ForeignOperationFailureDomain::Lifecycle,
            "supervisor.recovery_not_ready",
        );
        fixture
            .supervisor()
            .observe_signal(ForeignRecoverySignal::RadioOn)
            .unwrap();
        // Same-owner recovery needs no product recovery target for this policy receipt.
        fixture
            .supervisor()
            .observe_signal(ForeignRecoverySignal::SameOwnerRecovered {
                link: ForeignRecoveryLink {
                    connection_id: "test-link".to_owned(),
                    gatt_session_id: "test-gatt".to_owned(),
                    subscription_id: "test-subscription".to_owned(),
                },
            })
            .unwrap();
        for value in [f64::NAN, f64::INFINITY, -1.0, 0.5, 65536.0, f64::MAX] {
            let mut invalid = scan_options();
            invalid.company_id = Some(value);
            let result = Arc::clone(fixture.supervisor()).begin_scan_view(context(), invalid);
            assert!(result.scan.is_none());
            assert_failure(
                result.failure,
                ForeignOperationFailureDomain::Input,
                "invalid_company_id",
            );
        }
        for (uuids, prefix, detail) in [
            (
                vec!["not-a-uuid".to_owned()],
                None,
                "service_uuids[0] is not a valid Bluetooth UUID",
            ),
            (vec![], Some(String::new()), "name_prefix must not be empty"),
            (
                vec![],
                Some("é".repeat(125)),
                "name_prefix exceeds 248 UTF-8 bytes",
            ),
        ] {
            let mut invalid = scan_options();
            invalid.service_uuids = uuids;
            invalid.name_prefix = prefix;
            let result = Arc::clone(fixture.supervisor()).begin_scan_view(context(), invalid);
            assert!(result.scan.is_none());
            // One canonical rule owns the filter semantics; this boundary only reports it, and the
            // canonical detail still names the offending field.
            let failure = result.failure.expect("expected projected failure");
            assert_eq!(failure.domain, ForeignOperationFailureDomain::Input);
            assert_eq!(failure.code, "invalid_scan_options");
            assert!(failure.message.contains(detail), "{failure:?}");
        }
        assert_eq!(fixture.source.backend.commands().len(), 1);
        // The scripted start still has command 2 / scan 1: rejected input consumed neither ID.
        let scan = begin(&fixture);
        assert!(scan.start_result(WAIT_MS as f64).await.failure.is_none());
        fixture.source.observed(completed(2)).await;
        fixture.shutdown().await;
    });
}

#[test]
fn projected_original_slots_bytes_optionals_and_requested_stop_are_preserved() {
    run(|| async {
        let mut ad = advertisement(1, 128);
        ad.local_name = Some("é🦀\0peer".to_owned());
        ad.advertised_services = vec![BluetoothUuid::canonicalize("180d").unwrap()];
        ad.service_data = vec![ServiceData {
            service_uuid: BluetoothUuid::canonicalize("180f").unwrap(),
            payload: vec![0, 255, 128],
        }];
        ad.rssi = None;
        ad.connectable = Some(false);
        let mut fixture = Fixture::new([
            start(
                2,
                1,
                vec![started(2, 1), observed_ad(ad.clone()), completed(2)],
            ),
            successful_stop(3, 1),
        ]);
        fixture.supervisor().start().await.unwrap();
        let scan = begin(&fixture);
        fixture.source.observed(completed(2)).await;
        for timeout in invalid_timeouts() {
            assert_failure(
                scan.start_result(timeout).await.failure,
                ForeignOperationFailureDomain::Input,
                "invalid_timeout",
            );
            let observation = scan.next_observation(timeout).await;
            assert!(observation.advertisement.is_none() && observation.ended.is_none());
            assert_failure(
                observation.failure,
                ForeignOperationFailureDomain::Input,
                "invalid_timeout",
            );
            assert_eq!(scan.observation_snapshot().queued_advertisements, 1);
            assert!(!scan.observation_snapshot().pull_pending);
        }
        assert!(
            scan.start_result((INVALID_LONG_TIMEOUT_MS - 1) as f64)
                .await
                .failure
                .is_none()
        );
        assert_failure(
            scan.cancel_start_result().await.failure,
            ForeignOperationFailureDomain::Operation,
            "bleng.operation_already_completed",
        );
        let observation = scan.next_observation(WAIT_MS as f64).await;
        assert!(observation.failure.is_none() && observation.ended.is_none());
        let projected = observation.advertisement.unwrap();
        assert_eq!(projected.local_name, ad.local_name);
        assert_eq!(
            projected.advertised_services,
            vec![ad.advertised_services[0].value.clone()]
        );
        assert_eq!(projected.manufacturer_data, ad.manufacturer_data);
        assert_eq!(projected.service_data, ad.service_data);
        assert_eq!(projected.rssi, None);
        assert_eq!(projected.connectable, Some(false));
        let empty = AdvertisementObservation {
            local_name: None,
            advertised_services: vec![],
            manufacturer_data: vec![],
            service_data: vec![],
            rssi: Some(-127),
            connectable: None,
            ..ad.clone()
        };
        fixture.source.emit_and_observe(observed_ad(empty)).await;
        let same = scan
            .next_observation(WAIT_MS as f64)
            .await
            .advertisement
            .unwrap();
        assert!(
            projected
                .peripheral
                .same_peripheral(Arc::clone(&same.peripheral))
        );
        assert_eq!(projected.peripheral.key(), same.peripheral.key());
        assert!(!projected.peripheral.key().is_empty());
        assert!(same.local_name.is_none() && same.connectable.is_none());
        assert!(same.manufacturer_data.is_empty() && same.service_data.is_empty());
        assert_eq!(same.rssi, Some(-127));
        let stop = scan.begin_stop_view();
        assert!(stop.failure.is_none());
        let stop = stop.operation.unwrap();
        for timeout in invalid_timeouts() {
            assert_failure(
                stop.wait_result(timeout).await.failure,
                ForeignOperationFailureDomain::Input,
                "invalid_timeout",
            );
        }
        assert!(
            stop.wait_result((INVALID_LONG_TIMEOUT_MS - 1) as f64)
                .await
                .failure
                .is_none()
        );
        assert_failure(
            stop.cancel_result().await.failure,
            ForeignOperationFailureDomain::Operation,
            "bleng.operation_already_completed",
        );
        assert_failure(
            stop.wait_result(WAIT_MS as f64).await.failure,
            ForeignOperationFailureDomain::Operation,
            "bleng.operation_result_consumed",
        );
        fixture.source.observed(completed(3)).await;
        let ended = scan.next_observation(WAIT_MS as f64).await;
        assert!(ended.advertisement.is_none() && ended.failure.is_none());
        let ended = ended.ended.unwrap();
        assert_eq!(ended.reason, ForeignScanEndReason::Requested);
        assert!(ended.failure.is_none());
        fixture.shutdown().await;
    });
}

#[test]
fn projected_start_failure_is_not_timeout_and_releases_only_its_delivery() {
    run(|| async {
        let error = BleEventError {
            category: BleErrorCategory::Backend,
            message: "real native failure é\0".to_owned(),
            platform_details: None,
        };
        let failed = BleEvent::CommandCompleted {
            command_id: 2.into(),
            error: Some(error.clone()),
        };
        let mut fixture = Fixture::new([
            start(2, 1, vec![adapter_ready()]),
            start(3, 2, vec![started(3, 2), completed(3)]),
        ]);
        fixture.supervisor().start().await.unwrap();
        let scan = begin(&fixture);
        fixture.source.observed(adapter_ready()).await;
        let pending = scan.next_observation(PENDING_PULL_MS as f64);
        tokio::pin!(pending);
        poll_pending(pending.as_mut()).await;
        await_pull_pending(&scan).await;
        fixture.source.emit_and_observe(failed).await;
        let failure = scan.start_result(WAIT_MS as f64).await.failure.unwrap();
        assert_eq!(failure.code, "bleng.operation.backend");
        assert_eq!(failure.message, error.message);
        let observation = pending.await;
        assert!(observation.advertisement.is_none() && observation.ended.is_none());
        assert_failure(
            observation.failure,
            ForeignOperationFailureDomain::Delivery,
            "delivery.start_failed",
        );
        let next = begin(&fixture);
        assert!(next.start_result(WAIT_MS as f64).await.failure.is_none());
        fixture.source.observed(completed(3)).await;
        fixture.shutdown().await;
    });
    run(|| async {
        let mut fixture = Fixture::new([start(2, 1, vec![adapter_ready()])]);
        fixture.supervisor().start().await.unwrap();
        let scan = begin(&fixture);
        fixture.source.observed(adapter_ready()).await;
        assert_failure(
            scan.start_result(1.0).await.failure,
            ForeignOperationFailureDomain::Operation,
            "bleng.timeout",
        );
        let result = Arc::clone(fixture.supervisor()).begin_scan_view(context(), scan_options());
        assert!(result.scan.is_none());
        assert_failure(
            result.failure,
            ForeignOperationFailureDomain::Delivery,
            "delivery.busy",
        );
        let observation = scan.next_observation(1.0).await;
        assert!(observation.ended.is_none() && observation.advertisement.is_none());
        assert_failure(
            observation.failure,
            ForeignOperationFailureDomain::Observation,
            "bleng.timeout",
        );
        assert_failure(
            scan.cancel_start_result().await.failure,
            ForeignOperationFailureDomain::Operation,
            "bleng.operation_not_cancellable",
        );
        fixture.shutdown().await;
    });
}

#[test]
fn failed_and_suspended_native_ends_and_owner_retirement_are_distinct() {
    run(|| async {
        let mut fixture = Fixture::new([start(2, 1, vec![started(2, 1), completed(2)])]);
        fixture.supervisor().start().await.unwrap();
        let scan = begin(&fixture);
        assert!(scan.start_result(WAIT_MS as f64).await.failure.is_none());
        fixture.source.observed(completed(2)).await;
        let error = BleEventError {
            category: BleErrorCategory::Permission,
            message: "native stop failure".to_owned(),
            platform_details: Some(Box::new(blehub::PlatformErrorDetails {
                platform: blehub::Platform::Apple,
                code: Some("42".to_owned()),
                name: Some("SDKError".to_owned()),
                message: Some("native SDK details".to_owned()),
                status: Some(42),
                att_code: None,
            })),
        };
        fixture
            .source
            .emit_and_observe(BleEvent::ScanStopped {
                scan_id: 1.into(),
                result: ScanStopResult::Err {
                    error: error.clone(),
                },
            })
            .await;
        let result = scan.next_observation(WAIT_MS as f64).await;
        assert!(result.advertisement.is_none() && result.failure.is_none());
        let end = result.ended.unwrap();
        assert_eq!(end.reason, ForeignScanEndReason::Failed);
        let failure = end.failure.unwrap();
        assert_eq!(failure.domain, ForeignOperationFailureDomain::Operation);
        assert_eq!(failure.code, "ble.permission");
        assert_eq!(failure.message, error.message);
        assert_eq!(
            failure.platform_details.as_ref(),
            error.platform_details.as_deref()
        );
        fixture.shutdown().await;
        let result = scan.next_observation(WAIT_MS as f64).await;
        assert!(result.advertisement.is_none() && result.failure.is_none());
        let end = result.ended.unwrap();
        assert_eq!(end.reason, ForeignScanEndReason::OwnerClosed);
        assert!(end.failure.is_none());
        assert_eq!(fixture.source.backend.commands().len(), 2);
    });
    run(|| async {
        let mut fixture = Fixture::new([start(2, 1, vec![started(2, 1), completed(2)])]);
        fixture.supervisor().start().await.unwrap();
        let scan = begin(&fixture);
        assert!(scan.start_result(WAIT_MS as f64).await.failure.is_none());
        fixture.source.observed(completed(2)).await;
        fixture
            .source
            .emit_and_observe(BleEvent::ScanStopped {
                scan_id: 1.into(),
                result: ScanStopResult::Ok {
                    reason: ScanStopReason::SystemSuspended,
                },
            })
            .await;
        let result = scan.next_observation(WAIT_MS as f64).await;
        assert!(result.advertisement.is_none() && result.failure.is_none());
        let end = result.ended.unwrap();
        assert_eq!(end.reason, ForeignScanEndReason::SystemSuspended);
        assert!(end.failure.is_none());
        fixture.shutdown().await;
    });
}

#[test]
fn projected_first_overflow_survives_detach_and_owner_close() {
    run(|| async {
        let mut fixture = Fixture::new([start(2, 1, vec![started(2, 1), completed(2)])]);
        fixture.supervisor().start().await.unwrap();
        let scan = begin(&fixture);
        assert!(scan.start_result(WAIT_MS as f64).await.failure.is_none());
        fixture.source.observed(completed(2)).await;
        for marker in 0..=crate::MAX_SCAN_ADVERTISEMENTS {
            fixture
                .source
                .emit_and_observe(observed_ad(advertisement(1, marker as u8)))
                .await;
        }
        scan.detach();
        fixture.shutdown().await;
        let result = scan.next_observation(WAIT_MS as f64).await;
        assert!(result.advertisement.is_none() && result.failure.is_none());
        assert_eq!(result.ended.unwrap().reason, ForeignScanEndReason::Overflow);
    });
}

#[test]
fn retained_opaque_is_owner_scoped_and_keeps_no_supervisor_or_native_owner_alive() {
    run(|| async {
        let mut peripherals = vec![];
        for generation in [1, 2] {
            let mut source_context = context();
            source_context.owner_generation = generation;
            let (mut source, session) = Source::new(
                source_context.clone(),
                [start(
                    2,
                    1,
                    vec![
                        started(2, 1),
                        observed_ad(advertisement(1, 0)),
                        completed(2),
                    ],
                )],
            );
            let mut config = config();
            config.recovery.initial_context = source_context.clone();
            let supervisor = Supervisor::new(
                Arc::new(FakeForeignFactory(Mutex::new([session].into()))),
                config,
            )
            .unwrap();
            supervisor.start().await.unwrap();
            let result = Arc::clone(&supervisor).begin_scan_view(source_context, scan_options());
            assert!(result.failure.is_none());
            let scan = result.scan.unwrap();
            assert!(scan.start_result(WAIT_MS as f64).await.failure.is_none());
            source.observed(completed(2)).await;
            let result = scan.next_observation(WAIT_MS as f64).await;
            assert!(result.failure.is_none() && result.ended.is_none());
            peripherals.push(result.advertisement.unwrap().peripheral);
            // Keep both scan and peripheral across owner drop: neither may pin native ownership.
            let weak = Arc::downgrade(&supervisor);
            drop(supervisor);
            assert!(weak.upgrade().is_none());
            source.reader.join().await.unwrap();
            while source.backend.dispose_count() == 0 {
                tokio::task::yield_now().await;
            }
            source.assert_retired();
            let result = scan.next_observation(WAIT_MS as f64).await;
            assert!(result.advertisement.is_none() && result.failure.is_none());
            assert_eq!(
                result.ended.unwrap().reason,
                ForeignScanEndReason::OwnerClosed
            );
        }
        assert!(!peripherals[0].same_peripheral(Arc::clone(&peripherals[1])));
        let weak = Arc::downgrade(&peripherals[0]);
        drop(peripherals);
        assert!(
            weak.upgrade().is_none(),
            "there must be no permanent peripheral cache"
        );
    });
}

#[test]
fn projected_pull_uses_the_one_existing_reader_and_invalid_f64_cannot_steal_it() {
    run(|| async {
        let mut fixture = Fixture::new([start(2, 1, vec![started(2, 1), completed(2)])]);
        fixture.supervisor().start().await.unwrap();
        let scan = begin(&fixture);
        assert!(scan.start_result(WAIT_MS as f64).await.failure.is_none());
        fixture.source.observed(completed(2)).await;
        let mut pending = Box::pin(scan.next_observation(PENDING_PULL_MS as f64));
        poll_pending(pending.as_mut()).await;
        await_pull_pending(&scan).await;
        for timeout in invalid_timeouts() {
            let result = scan.next_observation(timeout).await;
            assert!(result.advertisement.is_none() && result.ended.is_none());
            assert_failure(
                result.failure,
                ForeignOperationFailureDomain::Input,
                "invalid_timeout",
            );
            assert!(scan.observation_snapshot().pull_pending);
        }
        let result = scan.next_observation(WAIT_MS as f64).await;
        assert!(result.advertisement.is_none() && result.ended.is_none());
        assert_failure(
            result.failure,
            ForeignOperationFailureDomain::Delivery,
            "delivery.pull_busy",
        );
        fixture
            .source
            .emit_and_observe(observed_ad(advertisement(1, 99)))
            .await;
        let result = pending.await;
        assert!(
            result.advertisement.is_some() && result.ended.is_none() && result.failure.is_none()
        );
        assert_empty(&scan);
        assert_eq!(fixture.source.reader.starts.load(Ordering::Acquire), 1);
        fixture.shutdown().await;
    });
}

#[test]
fn projected_options_dispatch_only_the_canonical_blehub_predicate() {
    run(|| async {
        let canonical = ScanOptions {
            android: None,
            service_uuids: vec![BluetoothUuid::canonicalize("180d").unwrap()],
            name_prefix: Some("é🦀".to_owned()),
            exact_name: None,
            company_id: Some(u16::MAX),
            min_rssi: None,
            manufacturer_data: None,
            allow_duplicates: true,
        };
        let mut fixture = Fixture::new([EventScenario::emit_before_return(
            RecordedCommand::StartScan(StartScanCommand {
                command_id: 2.into(),
                scan_session_id: 1.into(),
                options: canonical,
            }),
            vec![started(2, 1), completed(2)],
        )]);
        fixture.supervisor().start().await.unwrap();
        let result = Arc::clone(fixture.supervisor()).begin_scan_view(
            context(),
            ForeignScanOptions {
                android: None,
                service_uuids: vec!["180d".to_owned()],
                name_prefix: Some("é🦀".to_owned()),
                exact_name: None,
                company_id: Some(65535.0),
                min_rssi: None,
                manufacturer_data: None,
                allow_duplicates: true,
            },
        );
        assert!(result.failure.is_none());
        let scan = result.scan.unwrap();
        assert!(scan.start_result(WAIT_MS as f64).await.failure.is_none());
        fixture.source.observed(completed(2)).await;
        fixture.shutdown().await;
    });
}

#[test]
fn adopt_parses_an_owner_scoped_key_and_rejects_anything_else() {
    let fixture = Fixture::new([]);
    let adopted = Arc::clone(fixture.supervisor()).adopt_peripheral(context(), "42".to_owned());
    assert!(adopted.failure.is_none());
    assert_eq!(
        adopted.peripheral.expect("exactly one peripheral").key(),
        "42"
    );

    let rejected = Arc::clone(fixture.supervisor()).adopt_peripheral(context(), "nope".to_owned());
    assert!(rejected.peripheral.is_none());
    assert_failure(
        rejected.failure,
        ForeignOperationFailureDomain::Input,
        "invalid_peripheral_key",
    );
}
