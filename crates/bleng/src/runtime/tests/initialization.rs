use super::support::*;

#[test]
fn initialize_requires_correlated_snapshot_and_terminal_in_either_order() {
    for terminal_first in [false, true] {
        let owner = RuntimeOwner::new().unwrap();
        let domain = init_snapshot_event(Some(1));
        let terminal = command_completed(1, None);
        let events = if terminal_first {
            vec![terminal.clone(), domain.clone()]
        } else {
            vec![domain.clone(), terminal.clone()]
        };
        let (backend, handle) =
            FakeBackend::scripted([EventScenario::complete(init_command(1), events.clone())]);
        owner
            .attach_hub(BleHub::new(Arc::new(backend), owner.event_sink()).unwrap())
            .unwrap();
        let host = tokio::runtime::Builder::new_multi_thread()
            .enable_time()
            .build()
            .unwrap();
        host.block_on(async {
            let mut initialize = Box::pin(owner.initialize(Duration::from_secs(2)));
            tokio::time::timeout(Duration::from_secs(2), async {
                loop {
                    tokio::select! {
                        result = &mut initialize => panic!("init finished before callbacks: {result:?}"),
                        _ = tokio::task::yield_now() => {}
                    }
                    if handle.commands() == vec![init_command(1)] && handle.pending_event_count() == 2 {
                        break;
                    }
                }
            })
            .await
            .unwrap();
            assert_eq!(handle.advance(), Some(EventIngressStatus::Delivered));
            assert!(tokio::time::timeout(Duration::from_millis(20), &mut initialize)
                .await
                .is_err());
            assert_eq!(handle.advance(), Some(EventIngressStatus::Delivered));
            assert_eq!(initialize.await, Ok(init_snapshot()));
            for expected in events {
                assert_eq!(
                    owner.next_event(Duration::from_secs(2)).await,
                    Ok(Some(expected))
                );
            }
            assert!(owner.initialize(Duration::from_millis(20)).await.is_err());
            owner.close().await.unwrap();
        });
        assert_eq!(handle.commands(), vec![init_command(1)]);
        assert_eq!(handle.dispose_count(), 1);
    }
}

#[test]
fn initialize_accepts_synchronous_callbacks_but_not_uncorrelated_snapshots() {
    for terminal_first in [false, true] {
        let owner = RuntimeOwner::new().unwrap();
        let domain = init_snapshot_event(Some(1));
        let terminal = command_completed(1, None);
        let events = if terminal_first {
            vec![
                init_snapshot_event(None),
                init_snapshot_event(Some(99)),
                terminal.clone(),
                domain.clone(),
            ]
        } else {
            vec![
                init_snapshot_event(None),
                init_snapshot_event(Some(99)),
                domain.clone(),
                terminal.clone(),
            ]
        };
        let (backend, handle) = FakeBackend::scripted([EventScenario::emit_before_return(
            init_command(1),
            events.clone(),
        )]);
        owner
            .attach_hub(BleHub::new(Arc::new(backend), owner.event_sink()).unwrap())
            .unwrap();
        let host = tokio::runtime::Builder::new_multi_thread()
            .enable_time()
            .build()
            .unwrap();
        host.block_on(async {
            assert_eq!(
                owner.initialize(Duration::from_secs(2)).await,
                Ok(init_snapshot())
            );
            for expected in events {
                assert_eq!(
                    owner.next_event(Duration::from_secs(2)).await,
                    Ok(Some(expected))
                );
            }
            owner.close().await.unwrap();
        });
        assert_eq!(handle.commands(), vec![init_command(1)]);
        assert_eq!(handle.dispose_count(), 1);
    }
}

#[test]
fn initialize_ignores_uncorrelated_snapshots_even_after_own_terminal() {
    let owner = RuntimeOwner::new().unwrap();
    let events = vec![
        init_snapshot_event(None),
        init_snapshot_event(Some(42)),
        command_completed(1, None),
    ];
    let (backend, handle) = FakeBackend::scripted([EventScenario::emit_before_return(
        init_command(1),
        events.clone(),
    )]);
    owner
        .attach_hub(BleHub::new(Arc::new(backend), owner.event_sink()).unwrap())
        .unwrap();
    let host = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    host.block_on(async {
        assert_eq!(
            owner.initialize(Duration::from_millis(100)).await,
            Err(WaiterError::Timeout)
        );
        for expected in events {
            assert_eq!(
                owner.next_event(Duration::from_secs(2)).await,
                Ok(Some(expected))
            );
        }
        assert_eq!(
            handle.emit(init_snapshot_event(Some(1))),
            EventIngressStatus::Delivered
        );
        assert_eq!(
            owner.next_event(Duration::from_secs(2)).await,
            Ok(Some(init_snapshot_event(Some(1))))
        );
        assert!(owner.initialize(Duration::from_millis(20)).await.is_err());
        owner.close().await.unwrap();
    });
    assert_eq!(handle.commands(), vec![init_command(1)]);
    assert_eq!(handle.dispose_count(), 1);
}

#[test]
fn initialize_failure_cannot_be_retried() {
    let owner = RuntimeOwner::new().unwrap();
    let error = BleEventError {
        category: BleErrorCategory::AdapterUnavailable,
        message: "initialization failed".to_owned(),
        platform_details: None,
    };
    let (backend, handle) = FakeBackend::scripted([EventScenario::emit_before_return(
        init_command(1),
        vec![
            init_snapshot_event(Some(1)),
            command_completed(1, Some(error.clone())),
        ],
    )]);
    owner
        .attach_hub(BleHub::new(Arc::new(backend), owner.event_sink()).unwrap())
        .unwrap();
    let host = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    host.block_on(async {
        assert_eq!(
            owner.initialize(Duration::from_secs(2)).await,
            Err(WaiterError::TransportFault)
        );
        assert!(owner.faulted_for_test());
        tokio::time::timeout(Duration::from_secs(2), owner.wait_for_actor_stop_for_test())
            .await
            .expect("transport fault did not stop the actor");
        assert_eq!(
            owner.initialize(Duration::from_millis(20)).await,
            Err(WaiterError::TransportFault)
        );
        owner.close().await.unwrap();
        assert_eq!(handle.dispose_count(), 1);
    });
    assert_eq!(handle.commands(), vec![init_command(1)]);
}

#[test]
fn initialize_terminal_error_after_timeout_faults_and_disposes_hub() {
    let owner = RuntimeOwner::new().unwrap();
    let error = BleEventError {
        category: BleErrorCategory::AdapterUnavailable,
        message: "late initialization failure".to_owned(),
        platform_details: None,
    };
    let (backend, handle) = FakeBackend::scripted([EventScenario::complete(
        init_command(1),
        vec![command_completed(1, Some(error))],
    )]);
    owner
        .attach_hub(BleHub::new(Arc::new(backend), owner.event_sink()).unwrap())
        .unwrap();
    let host = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    host.block_on(async {
        assert_eq!(
            owner.initialize(Duration::from_millis(100)).await,
            Err(WaiterError::Timeout)
        );
        assert!(!owner.faulted_for_test());
        assert_eq!(handle.advance(), Some(EventIngressStatus::Delivered));
        tokio::time::timeout(Duration::from_secs(2), async {
            while !owner.faulted_for_test() {
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap();
        assert_eq!(
            owner.initialize(Duration::from_millis(20)).await,
            Err(WaiterError::TransportFault)
        );
        owner.close().await.unwrap();
        assert_eq!(handle.dispose_count(), 1);
    });
    assert_eq!(handle.commands(), vec![init_command(1)]);
}

#[test]
fn cancelled_initialize_does_not_allow_a_second_attempt() {
    let owner = RuntimeOwner::new().unwrap();
    let (backend, handle) = FakeBackend::scripted([EventScenario::accept(init_command(1))]);
    owner
        .attach_hub(BleHub::new(Arc::new(backend), owner.event_sink()).unwrap())
        .unwrap();
    let host = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    host.block_on(async {
        let mut initialize = Box::pin(owner.initialize(Duration::from_secs(2)));
        tokio::time::timeout(Duration::from_secs(2), async {
            loop {
                tokio::select! {
                    result = &mut initialize => panic!("init finished without callbacks: {result:?}"),
                    _ = tokio::task::yield_now() => {}
                }
                if handle.commands() == vec![init_command(1)] {
                    break;
                }
            }
        })
        .await
        .unwrap();
        assert!(tokio::time::timeout(Duration::from_millis(20), &mut initialize)
            .await
            .is_err());
        assert!(owner.initialize(Duration::from_millis(20)).await.is_err());
        drop(initialize);
        assert!(owner.initialize(Duration::from_millis(20)).await.is_err());
        assert_eq!(handle.emit(init_snapshot_event(Some(1))), EventIngressStatus::Delivered);
        assert_eq!(handle.emit(command_completed(1, None)), EventIngressStatus::Delivered);
        assert!(owner.initialize(Duration::from_millis(20)).await.is_err());
        owner.close().await.unwrap();
    });
    assert_eq!(handle.commands(), vec![init_command(1)]);
    assert_eq!(handle.dispose_count(), 1);
}

#[test]
fn initialize_callbacks_before_dispatch_rejection_fail_closed() {
    let owner = RuntimeOwner::new().unwrap();
    let rejection = CommandDispatchError::InvalidState {
        detail: "initialization rejected after callback".to_owned(),
    };
    let (backend, handle) = FakeBackend::scripted([EventScenario::emit_before_reject(
        init_command(1),
        vec![init_snapshot_event(Some(1)), command_completed(1, None)],
        rejection,
    )]);
    owner
        .attach_hub(BleHub::new(Arc::new(backend), owner.event_sink()).unwrap())
        .unwrap();
    let host = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    host.block_on(async {
        assert_eq!(
            owner.initialize(Duration::from_secs(2)).await,
            Err(WaiterError::TransportFault)
        );
        assert!(owner.faulted_for_test());
        assert_eq!(
            owner.initialize(Duration::from_millis(20)).await,
            Err(WaiterError::TransportFault)
        );
        owner.close().await.unwrap();
    });
    assert_eq!(handle.commands(), vec![init_command(1)]);
    assert_eq!(handle.dispose_count(), 1);
}

#[test]
fn initialize_clean_dispatch_rejection_retires_hub_without_faulting() {
    let owner = RuntimeOwner::new().unwrap();
    let rejection = CommandDispatchError::InvalidState {
        detail: "initialization rejected without callbacks".to_owned(),
    };
    let (backend, handle) =
        FakeBackend::scripted([EventScenario::reject(init_command(1), rejection.clone())]);
    owner
        .attach_hub(BleHub::new(Arc::new(backend), owner.event_sink()).unwrap())
        .unwrap();
    let host = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    host.block_on(async {
        assert_eq!(
            owner.initialize(Duration::from_secs(2)).await,
            Err(WaiterError::DispatchRejected(rejection))
        );
        assert!(!owner.faulted_for_test());
        assert_eq!(
            owner.initialize(Duration::from_millis(20)).await,
            Err(WaiterError::BackendNotAssembled)
        );
        assert_eq!(
            owner
                .start_scan(ScanOptions::default(), Duration::from_millis(20))
                .await,
            Err(WaiterError::BackendNotAssembled)
        );
        assert_eq!(
            owner
                .connect(PeripheralId { value: 10 }, Duration::from_millis(20))
                .await,
            Err(WaiterError::BackendNotAssembled)
        );
        owner.close().await.unwrap();
        assert_eq!(handle.dispose_count(), 1);
    });
    assert_eq!(handle.commands(), vec![init_command(1)]);
    assert!(handle.push_statuses().is_empty());
}

#[test]
fn operations_require_completed_initialization_and_preserve_next_ids() {
    let owner = RuntimeOwner::new().unwrap();
    let options = ScanOptions::default();
    let scan_command = RecordedCommand::StartScan(StartScanCommand {
        command_id: CommandId { value: 2 },
        scan_session_id: ScanSessionId { value: 3 },
        options: options.clone(),
    });
    let (backend, handle) = FakeBackend::scripted([
        EventScenario::complete(
            init_command(1),
            vec![init_snapshot_event(Some(1)), command_completed(1, None)],
        ),
        EventScenario::emit_before_return(
            scan_command.clone(),
            vec![
                BleEvent::ScanStarted {
                    command_id: CommandId { value: 2 },
                    scan_id: ScanSessionId { value: 3 },
                },
                command_completed(2, None),
            ],
        ),
    ]);
    owner
        .attach_hub(BleHub::new(Arc::new(backend), owner.event_sink()).unwrap())
        .unwrap();
    let host = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    host.block_on(async {
        assert_eq!(
            owner.start_scan(options.clone(), Duration::from_secs(1)).await,
            Err(WaiterError::NotInitialized)
        );
        assert_eq!(
            owner.connect(PeripheralId { value: 10 }, Duration::from_secs(1)).await,
            Err(WaiterError::NotInitialized)
        );
        assert!(handle.commands().is_empty());

        let mut initialize = Box::pin(owner.initialize(Duration::from_secs(2)));
        tokio::time::timeout(Duration::from_secs(2), async {
            loop {
                tokio::select! {
                    result = &mut initialize => panic!("init finished before callbacks: {result:?}"),
                    _ = tokio::task::yield_now() => {}
                }
                if handle.commands() == vec![init_command(1)] && handle.pending_event_count() == 2 {
                    break;
                }
            }
        })
        .await
        .unwrap();
        assert_eq!(
            owner.start_scan(options.clone(), Duration::from_secs(1)).await,
            Err(WaiterError::NotInitialized)
        );
        assert_eq!(
            owner.connect(PeripheralId { value: 10 }, Duration::from_secs(1)).await,
            Err(WaiterError::NotInitialized)
        );
        assert_eq!(handle.commands(), vec![init_command(1)]);
        assert_eq!(handle.advance(), Some(EventIngressStatus::Delivered));
        assert_eq!(
            owner.start_scan(options.clone(), Duration::from_secs(1)).await,
            Err(WaiterError::NotInitialized)
        );
        assert_eq!(handle.advance(), Some(EventIngressStatus::Delivered));
        assert_eq!(initialize.await, Ok(init_snapshot()));
        assert_eq!(
            owner.start_scan(options, Duration::from_secs(2)).await,
            Ok(ScanSessionId { value: 3 })
        );
        assert_eq!(handle.commands(), vec![init_command(1), scan_command]);
        owner.close().await.unwrap();
    });
    assert_eq!(handle.dispose_count(), 1);
}

#[test]
fn powered_off_and_denied_snapshot_still_completes_initialization() {
    let owner = RuntimeOwner::new().unwrap();
    let snapshot = AdapterSnapshot {
        state: AdapterState::PoweredOff,
        scan_authorization: AuthorizationState::Denied,
        connection_authorization: AuthorizationState::Denied,
        name: None,
    };
    let (backend, handle) = FakeBackend::scripted([EventScenario::emit_before_return(
        init_command(1),
        vec![
            BleEvent::AdapterSnapshotChanged {
                command_id: Some(CommandId { value: 1 }),
                snapshot: snapshot.clone(),
            },
            command_completed(1, None),
        ],
    )]);
    owner
        .attach_hub(BleHub::new(Arc::new(backend), owner.event_sink()).unwrap())
        .unwrap();
    let host = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    host.block_on(async {
        assert_eq!(owner.initialize(Duration::from_secs(2)).await, Ok(snapshot));
        assert!(!owner.faulted_for_test());
        owner.close().await.unwrap();
    });
    assert_eq!(handle.commands(), vec![init_command(1)]);
    assert_eq!(handle.dispose_count(), 1);
}
