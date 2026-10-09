use super::support::{
    Arc, BleErrorCategory, BleEvent, BleEventError, BleHub, CommandDispatchError, CommandId,
    Duration, EventIngressStatus, EventReadError, EventScenario, FakeBackend, HoldActor, Notify,
    RecordedCommand, RuntimeOwner, ScanOptions, ScanSessionId, ScanStopReason, ScanStopResult,
    StartScanCommand, WaiterError, WaiterExpectation, WaiterSuccess, command_completed,
    requested_stop, started_scan, stop_command,
};

use std::panic::{AssertUnwindSafe, catch_unwind};

use crate::runtime::reservations::ScanState;

#[test]
fn scan_state_retains_unsettled_start_and_ignores_stale_rejection() {
    let mut state = ScanState::default();
    state.reserve_scan_start(1.into(), 10.into());
    let rejection = Err(CommandDispatchError::InvalidState {
        detail: "scan start rejected".to_owned(),
    });
    assert!(state.resolve_dispatch(99.into(), &rejection).is_none());
    assert_eq!(state.require_scan_available(), Err(WaiterError::ScanBusy));
    assert!(!state.record_terminal(99.into(), &Ok(())));

    state.resolve_dispatch(1.into(), &Ok(())).unwrap();
    state.record_event(&BleEvent::ScanStopped {
        scan_id: 10.into(),
        result: ScanStopResult::Ok {
            reason: ScanStopReason::SystemSuspended,
        },
    });
    assert!(state.record_terminal(1.into(), &Ok(())));
    state.retire_completed();
    assert_eq!(state.waiter_result(1.into()), None);
    assert_eq!(state.require_scan_available(), Err(WaiterError::ScanBusy));
    assert_eq!(
        state.validate_scan_stop(10.into()),
        Err(WaiterError::ScanNotActive)
    );

    // Native stop does not release a start whose correlated domain arrives late.
    state.record_event(&BleEvent::ScanStarted {
        command_id: 1.into(),
        scan_id: 10.into(),
    });
    assert_eq!(
        state.waiter_result(1.into()),
        Some(Ok(WaiterSuccess::ScanStarted(10.into())))
    );
    state.retire_completed();
    assert_eq!(state.require_scan_available(), Ok(()));

    state.reserve_scan_start(2.into(), 20.into());
    assert!(state.resolve_dispatch(1.into(), &rejection).is_none());
    assert_eq!(state.require_scan_available(), Err(WaiterError::ScanBusy));
    assert_eq!(
        state.validate_scan_stop(20.into()),
        Err(WaiterError::ScanBusy)
    );
    assert_eq!(
        state.validate_scan_stop(10.into()),
        Err(WaiterError::ScanNotActive)
    );
    state.resolve_dispatch(2.into(), &rejection).unwrap();
    assert_eq!(state.require_scan_available(), Ok(()));
    assert_eq!(
        state.validate_scan_stop(20.into()),
        Err(WaiterError::ScanNotActive)
    );
}

#[test]
fn scan_installation_cannot_replace_a_session_or_bypass_stop_admission() {
    let mut state = ScanState::default();
    state.reserve_scan_start(1.into(), 10.into());
    assert!(
        catch_unwind(AssertUnwindSafe(|| {
            state.reserve_scan_start(99.into(), 99.into());
        }))
        .is_err()
    );

    // Each missing start prerequisite must reject installation without a stop slot.
    for stage in 0..3 {
        assert!(
            catch_unwind(AssertUnwindSafe(|| {
                state.reserve_scan_stop(2.into());
            }))
            .is_err()
        );
        assert_eq!(state.stop_command(), None);
        match stage {
            0 => {
                state.resolve_dispatch(1.into(), &Ok(())).unwrap();
            }
            1 => state.record_event(&BleEvent::ScanStarted {
                command_id: 1.into(),
                scan_id: 10.into(),
            }),
            _ => assert!(state.record_terminal(1.into(), &Ok(()))),
        }
    }
    assert_eq!(
        state.waiter_result(1.into()),
        Some(Ok(WaiterSuccess::ScanStarted(10.into())))
    );

    state.reserve_scan_stop(2.into());
    assert!(
        catch_unwind(AssertUnwindSafe(|| {
            state.reserve_scan_stop(3.into());
        }))
        .is_err()
    );
    assert_eq!(state.stop_command(), Some(2.into()));
    assert!(!state.record_terminal(3.into(), &Ok(())));

    state.resolve_dispatch(2.into(), &Ok(())).unwrap();
    state.record_event(&BleEvent::ScanStopped {
        scan_id: 10.into(),
        result: ScanStopResult::Ok {
            reason: ScanStopReason::Requested {
                command_id: 2.into(),
            },
        },
    });
    assert!(state.record_terminal(2.into(), &Ok(())));
    assert_eq!(
        state.waiter_result(2.into()),
        Some(Ok(WaiterSuccess::ScanStopped(10.into())))
    );
    state.retire_completed();
    assert_eq!(state.require_scan_available(), Ok(()));
}

#[test]
fn start_scan_allocates_ids_and_accepts_synchronous_events_before_dispatch_returns() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let id = CommandId { value: 1 };
    let scan = ScanSessionId { value: 1 };
    let options = ScanOptions::default();
    let domain = BleEvent::ScanStarted {
        command_id: id,
        scan_id: scan,
    };
    let terminal = BleEvent::CommandCompleted {
        command_id: id,
        error: None,
    };
    let (backend, handle) = FakeBackend::scripted([EventScenario::emit_before_return(
        RecordedCommand::StartScan(StartScanCommand {
            command_id: id,
            scan_session_id: scan,
            options: options.clone(),
        }),
        vec![terminal.clone(), domain.clone()],
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
            tokio::time::timeout(
                Duration::from_secs(2),
                owner.start_scan(options.clone(), Duration::from_secs(1))
            )
            .await
            .unwrap(),
            Ok(scan)
        );
        assert_eq!(
            owner.next_event(Duration::from_secs(2)).await,
            Ok(Some(terminal))
        );
        assert_eq!(
            owner.next_event(Duration::from_secs(2)).await,
            Ok(Some(domain))
        );
        assert_eq!(
            owner.start_scan(options, Duration::from_millis(20)).await,
            Err(WaiterError::ScanBusy)
        );
        owner.close().await.unwrap();
    });
    assert_eq!(handle.dispose_count(), 1);
    assert_eq!(handle.commands().len(), 1);
}

#[test]
fn stop_scan_requires_matching_domain_and_terminal_then_allows_new_scan() {
    for (terminal_first, stopped) in [
        (false, requested_stop(2)),
        (true, requested_stop(2)),
        (
            false,
            BleEvent::ScanStopped {
                scan_id: ScanSessionId { value: 1 },
                result: ScanStopResult::Ok {
                    reason: ScanStopReason::SystemSuspended,
                },
            },
        ),
    ] {
        let owner = RuntimeOwner::new_assuming_initialized().unwrap();
        let terminal = BleEvent::CommandCompleted {
            command_id: CommandId { value: 2 },
            error: None,
        };
        let mut events = vec![stopped.clone(), terminal.clone()];
        if terminal_first {
            events.reverse();
        }
        let (backend, handle) = FakeBackend::scripted([
            started_scan(),
            EventScenario::emit_before_return(stop_command(2), events.clone()),
            EventScenario::emit_before_return(
                RecordedCommand::StartScan(StartScanCommand {
                    command_id: CommandId { value: 3 },
                    scan_session_id: ScanSessionId { value: 2 },
                    options: ScanOptions::default(),
                }),
                vec![
                    BleEvent::ScanStarted {
                        command_id: CommandId { value: 3 },
                        scan_id: ScanSessionId { value: 2 },
                    },
                    BleEvent::CommandCompleted {
                        command_id: CommandId { value: 3 },
                        error: None,
                    },
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
            let session = owner
                .start_scan(ScanOptions::default(), Duration::from_secs(2))
                .await
                .unwrap();
            assert_eq!(session, ScanSessionId { value: 1 });
            assert_eq!(
                owner.stop_scan(session, Duration::from_secs(2)).await,
                Ok(())
            );
            assert_eq!(
                owner.next_event(Duration::from_secs(2)).await,
                Ok(Some(BleEvent::ScanStarted {
                    command_id: CommandId { value: 1 },
                    scan_id: session,
                }))
            );
            assert_eq!(
                owner.next_event(Duration::from_secs(2)).await,
                Ok(Some(BleEvent::CommandCompleted {
                    command_id: CommandId { value: 1 },
                    error: None,
                }))
            );
            for expected in events {
                assert_eq!(
                    owner.next_event(Duration::from_secs(2)).await,
                    Ok(Some(expected))
                );
            }
            assert_eq!(
                owner
                    .start_scan(ScanOptions::default(), Duration::from_secs(2))
                    .await,
                Ok(ScanSessionId { value: 2 })
            );
            owner.close().await.unwrap();
        });
        assert_eq!(handle.commands().len(), 3);
        assert_eq!(handle.dispose_count(), 1);
    }
}

#[test]
fn foreign_scan_error_cannot_retire_suspended_stop_awaiting_terminal() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let suspended = BleEvent::ScanStopped {
        scan_id: 1.into(),
        result: ScanStopResult::Ok {
            reason: ScanStopReason::SystemSuspended,
        },
    };
    let foreign_error = BleEvent::ScanStopped {
        scan_id: 99.into(),
        result: ScanStopResult::Err {
            error: BleEventError {
                category: BleErrorCategory::Backend,
                message: "stale failure from another scan".to_owned(),
                platform_details: None,
            },
        },
    };
    let (backend, handle) = FakeBackend::scripted([
        started_scan(),
        EventScenario::emit_before_return(stop_command(2), vec![suspended.clone()]),
        EventScenario::emit_before_return(
            RecordedCommand::StartScan(StartScanCommand {
                command_id: 3.into(),
                scan_session_id: 2.into(),
                options: ScanOptions::default(),
            }),
            vec![
                BleEvent::ScanStarted {
                    command_id: 3.into(),
                    scan_id: 2.into(),
                },
                command_completed(3, None),
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
        let deadline = Duration::from_secs(2);
        let session = owner
            .start_scan(ScanOptions::default(), deadline)
            .await
            .unwrap();
        for expected in [
            BleEvent::ScanStarted {
                command_id: 1.into(),
                scan_id: session,
            },
            command_completed(1, None),
        ] {
            assert_eq!(owner.next_event(deadline).await, Ok(Some(expected)));
        }

        let stopping = owner.stop_scan(session, deadline);
        tokio::pin!(stopping);
        tokio::select! {
            result = &mut stopping => panic!("stop completed before its terminal: {result:?}"),
            event = owner.next_event(deadline) => assert_eq!(event, Ok(Some(suspended))),
        }
        assert_eq!(
            owner.start_scan(ScanOptions::default(), deadline).await,
            Err(WaiterError::ScanBusy)
        );
        assert_eq!(
            handle.emit(foreign_error.clone()),
            EventIngressStatus::Delivered
        );
        tokio::select! {
            result = &mut stopping => panic!("foreign scan error completed stop: {result:?}"),
            event = owner.next_event(deadline) => assert_eq!(event, Ok(Some(foreign_error))),
        }
        assert_eq!(
            owner.start_scan(ScanOptions::default(), deadline).await,
            Err(WaiterError::ScanBusy),
            "a foreign scan error must not release the current stop reservation"
        );
        assert!(
            tokio::time::timeout(Duration::from_millis(20), &mut stopping)
                .await
                .is_err()
        );
        assert_eq!(handle.commands().len(), 2);

        let terminal = command_completed(2, None);
        assert_eq!(handle.emit(terminal.clone()), EventIngressStatus::Delivered);
        assert_eq!(
            tokio::time::timeout(deadline, &mut stopping).await.unwrap(),
            Ok(())
        );
        assert_eq!(owner.next_event(deadline).await, Ok(Some(terminal)));
        assert_eq!(
            owner.start_scan(ScanOptions::default(), deadline).await,
            Ok(2.into())
        );
        assert!(!owner.faulted_for_test());
        tokio::time::timeout(deadline, owner.close())
            .await
            .unwrap()
            .unwrap();
    });
    assert_eq!(handle.commands().len(), 3);
    assert_eq!(handle.dispose_count(), 1);
}

#[test]
fn rejected_stop_keeps_scan_active_unless_requested_evidence_contradicts_rejection() {
    let suspended = BleEvent::ScanStopped {
        scan_id: 1.into(),
        result: ScanStopResult::Ok {
            reason: ScanStopReason::SystemSuspended,
        },
    };
    for callbacks in [
        vec![],
        vec![requested_stop(2), command_completed(2, None)],
        vec![suspended.clone(), requested_stop(2)],
        vec![requested_stop(2), suspended],
    ] {
        let contradictory = !callbacks.is_empty();
        let owner = RuntimeOwner::new_assuming_initialized().unwrap();
        let rejection = CommandDispatchError::InvalidState {
            detail: "stop rejected".to_owned(),
        };
        let scenario =
            EventScenario::emit_before_reject(stop_command(2), callbacks, rejection.clone());
        let (backend, handle) = FakeBackend::scripted([started_scan(), scenario]);
        owner
            .attach_hub(BleHub::new(Arc::new(backend), owner.event_sink()).unwrap())
            .unwrap();
        let host = tokio::runtime::Builder::new_multi_thread()
            .enable_time()
            .build()
            .unwrap();
        host.block_on(async {
            let session = owner
                .start_scan(ScanOptions::default(), Duration::from_secs(2))
                .await
                .unwrap();
            assert_eq!(
                owner.stop_scan(session, Duration::from_secs(2)).await,
                if contradictory {
                    Err(WaiterError::TransportFault)
                } else {
                    Err(WaiterError::DispatchRejected(rejection))
                }
            );
            if contradictory {
                assert!(owner.faulted_for_test());
                assert_eq!(
                    owner.next_event(Duration::from_secs(2)).await,
                    Err(EventReadError::Fault)
                );
            } else {
                assert_eq!(
                    owner
                        .start_scan(ScanOptions::default(), Duration::from_millis(20))
                        .await,
                    Err(WaiterError::ScanBusy)
                );
            }
            owner.close().await.unwrap();
        });
        assert_eq!(handle.commands().len(), 2);
        assert_eq!(handle.dispose_count(), 1);
    }
}

#[test]
fn passive_scan_end_before_rejected_stop_preserves_rejection_and_allows_restart() {
    for result in [
        ScanStopResult::Ok {
            reason: ScanStopReason::SystemSuspended,
        },
        ScanStopResult::Err {
            error: BleEventError {
                category: BleErrorCategory::AdapterUnavailable,
                message: "adapter stopped scanning independently".to_owned(),
                platform_details: None,
            },
        },
    ] {
        let owner = RuntimeOwner::new_assuming_initialized().unwrap();
        let rejection = CommandDispatchError::InvalidState {
            detail: "scan already ended before stop dispatch".to_owned(),
        };
        let stopped = BleEvent::ScanStopped {
            scan_id: 1.into(),
            result,
        };
        let restarted = BleEvent::ScanStarted {
            command_id: 3.into(),
            scan_id: 2.into(),
        };
        let (backend, handle) = FakeBackend::scripted([
            started_scan(),
            EventScenario::emit_before_reject(
                stop_command(2),
                vec![
                    command_completed(2, None),
                    stopped.clone(),
                    command_completed(2, None),
                ],
                rejection.clone(),
            ),
            EventScenario::emit_before_return(
                RecordedCommand::StartScan(StartScanCommand {
                    command_id: 3.into(),
                    scan_session_id: 2.into(),
                    options: ScanOptions::default(),
                }),
                vec![restarted.clone(), command_completed(3, None)],
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
            let timeout = Duration::from_secs(2);
            let session = owner
                .start_scan(ScanOptions::default(), timeout)
                .await
                .unwrap();
            for expected in [
                BleEvent::ScanStarted {
                    command_id: 1.into(),
                    scan_id: session,
                },
                command_completed(1, None),
            ] {
                assert_eq!(owner.next_event(timeout).await, Ok(Some(expected)));
            }
            assert_eq!(
                owner.stop_scan(session, timeout).await,
                Err(WaiterError::DispatchRejected(rejection))
            );
            assert_eq!(owner.next_event(timeout).await, Ok(Some(stopped.clone())));
            assert!(!owner.faulted_for_test());
            assert_eq!(
                owner.stop_scan(session, timeout).await,
                Err(WaiterError::ScanNotActive)
            );
            assert_eq!(
                owner.start_scan(ScanOptions::default(), timeout).await,
                Ok(2.into())
            );
            for expected in [restarted, command_completed(3, None)] {
                assert_eq!(owner.next_event(timeout).await, Ok(Some(expected)));
            }
            // Old-session teardown and the rejected command's late terminal cannot
            // retire the replacement scan or turn its rejection into success.
            for stale in [stopped, requested_stop(2), command_completed(2, None)] {
                assert_eq!(handle.emit(stale.clone()), EventIngressStatus::Delivered);
                assert_eq!(owner.next_event(timeout).await, Ok(Some(stale)));
            }
            assert_eq!(
                owner.start_scan(ScanOptions::default(), timeout).await,
                Err(WaiterError::ScanBusy)
            );
            assert!(!owner.faulted_for_test());
            tokio::time::timeout(timeout, owner.close())
                .await
                .unwrap()
                .unwrap();
        });
        assert_eq!(handle.commands().len(), 3);
        assert_eq!(handle.dispose_count(), 1);
    }
}

#[test]
fn native_stop_error_retires_scan_and_cannot_poison_next_session() {
    for (include_terminal, terminal_first) in [(false, false), (true, false), (true, true)] {
        let owner = RuntimeOwner::new_assuming_initialized().unwrap();
        let error = BleEventError {
            category: BleErrorCategory::Backend,
            message: "stop failed".to_owned(),
            platform_details: None,
        };
        let mut stop_events = vec![BleEvent::ScanStopped {
            scan_id: 1.into(),
            result: ScanStopResult::Err {
                error: error.clone(),
            },
        }];
        if include_terminal {
            stop_events.push(command_completed(2, None));
        }
        if terminal_first {
            stop_events.reverse();
        }
        let (backend, handle) = FakeBackend::scripted([
            started_scan(),
            EventScenario::emit_before_return(stop_command(2), stop_events),
            EventScenario::emit_before_return(
                RecordedCommand::StartScan(StartScanCommand {
                    command_id: CommandId { value: 3 },
                    scan_session_id: ScanSessionId { value: 2 },
                    options: ScanOptions::default(),
                }),
                vec![
                    BleEvent::ScanStarted {
                        command_id: CommandId { value: 3 },
                        scan_id: ScanSessionId { value: 2 },
                    },
                    BleEvent::CommandCompleted {
                        command_id: CommandId { value: 3 },
                        error: None,
                    },
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
            let session = owner
                .start_scan(ScanOptions::default(), Duration::from_secs(2))
                .await
                .unwrap();
            assert_eq!(
                owner
                    .stop_scan(ScanSessionId { value: 99 }, Duration::from_secs(1))
                    .await,
                Err(WaiterError::ScanNotActive)
            );
            assert_eq!(
                owner.stop_scan(session, Duration::from_secs(2)).await,
                Err(WaiterError::Operation(error))
            );
            assert_eq!(
                owner.stop_scan(session, Duration::from_millis(20)).await,
                Err(WaiterError::ScanNotActive)
            );
            assert_eq!(
                owner
                    .start_scan(ScanOptions::default(), Duration::from_secs(2))
                    .await,
                Ok(ScanSessionId { value: 2 })
            );
            // Missing or duplicate old terminals must not release the new scan slot.
            assert_eq!(
                handle.emit(command_completed(2, None)),
                EventIngressStatus::Delivered
            );
            assert_eq!(
                handle.emit(BleEvent::ScanStopped {
                    scan_id: session,
                    result: ScanStopResult::Err {
                        error: BleEventError {
                            category: BleErrorCategory::Backend,
                            message: "late duplicate".to_owned(),
                            platform_details: None,
                        },
                    },
                }),
                EventIngressStatus::Delivered
            );
            assert_eq!(
                owner
                    .start_scan(ScanOptions::default(), Duration::from_millis(20))
                    .await,
                Err(WaiterError::ScanBusy)
            );
            owner.close().await.unwrap();
        });
        assert_eq!(handle.commands().len(), 3);
    }
}

#[test]
fn failed_stop_terminal_without_native_stop_keeps_scan_for_retry() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let error = BleEventError {
        category: BleErrorCategory::Backend,
        message: "stop invocation failed".to_owned(),
        platform_details: None,
    };
    let (backend, handle) = FakeBackend::scripted([
        started_scan(),
        EventScenario::emit_before_return(
            stop_command(2),
            vec![BleEvent::CommandCompleted {
                command_id: CommandId { value: 2 },
                error: Some(error.clone()),
            }],
        ),
        EventScenario::emit_before_return(
            stop_command(3),
            vec![
                requested_stop(3),
                BleEvent::CommandCompleted {
                    command_id: CommandId { value: 3 },
                    error: None,
                },
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
        let session = owner
            .start_scan(ScanOptions::default(), Duration::from_secs(2))
            .await
            .unwrap();
        assert_eq!(
            owner.stop_scan(session, Duration::from_secs(2)).await,
            Err(WaiterError::Operation(error))
        );
        assert_eq!(
            owner
                .start_scan(ScanOptions::default(), Duration::from_millis(20))
                .await,
            Err(WaiterError::ScanBusy)
        );
        assert_eq!(
            owner.stop_scan(session, Duration::from_secs(2)).await,
            Ok(())
        );
        owner.close().await.unwrap();
    });
    assert_eq!(handle.commands().len(), 3);
}

#[test]
fn passive_scan_error_retires_session_without_a_stop_command() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let (backend, handle) = FakeBackend::scripted([
        started_scan(),
        EventScenario::emit_before_return(
            RecordedCommand::StartScan(StartScanCommand {
                command_id: CommandId { value: 2 },
                scan_session_id: ScanSessionId { value: 2 },
                options: ScanOptions::default(),
            }),
            vec![
                BleEvent::ScanStarted {
                    command_id: CommandId { value: 2 },
                    scan_id: ScanSessionId { value: 2 },
                },
                BleEvent::CommandCompleted {
                    command_id: CommandId { value: 2 },
                    error: None,
                },
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
        let session = owner
            .start_scan(ScanOptions::default(), Duration::from_secs(2))
            .await
            .unwrap();
        let stopped = BleEvent::ScanStopped {
            scan_id: session,
            result: ScanStopResult::Err {
                error: BleEventError {
                    category: BleErrorCategory::AdapterUnavailable,
                    message: "adapter lost".to_owned(),
                    platform_details: None,
                },
            },
        };
        assert_eq!(handle.emit(stopped.clone()), EventIngressStatus::Delivered);
        for _ in 0..2 {
            assert!(
                owner
                    .next_event(Duration::from_secs(2))
                    .await
                    .unwrap()
                    .is_some()
            );
        }
        assert_eq!(
            owner.next_event(Duration::from_secs(2)).await,
            Ok(Some(stopped))
        );
        assert_eq!(
            owner
                .start_scan(ScanOptions::default(), Duration::from_secs(2))
                .await,
            Ok(ScanSessionId { value: 2 })
        );
        owner.close().await.unwrap();
    });
    assert_eq!(handle.commands().len(), 2);
}

#[test]
fn native_stop_retains_live_start_until_domain_and_terminal_settle() {
    for terminal_first in [false, true] {
        for stop_result in [
            ScanStopResult::Err {
                error: BleEventError {
                    category: BleErrorCategory::AdapterUnavailable,
                    message: "scan ended before start completion".to_owned(),
                    platform_details: None,
                },
            },
            ScanStopResult::Ok {
                reason: ScanStopReason::SystemSuspended,
            },
        ] {
            let owner = RuntimeOwner::new_assuming_initialized().unwrap();
            let domain = BleEvent::ScanStarted {
                command_id: 1.into(),
                scan_id: 1.into(),
            };
            let terminal = command_completed(1, None);
            let (first, last) = if terminal_first {
                (terminal, domain)
            } else {
                (domain, terminal)
            };
            let stopped = BleEvent::ScanStopped {
                scan_id: 1.into(),
                result: stop_result,
            };
            let (backend, handle) = FakeBackend::scripted([
                EventScenario::emit_before_return(
                    RecordedCommand::StartScan(StartScanCommand {
                        command_id: 1.into(),
                        scan_session_id: 1.into(),
                        options: ScanOptions::default(),
                    }),
                    vec![first.clone()],
                ),
                EventScenario::emit_before_return(
                    RecordedCommand::StartScan(StartScanCommand {
                        command_id: 2.into(),
                        scan_session_id: 2.into(),
                        options: ScanOptions::default(),
                    }),
                    vec![
                        BleEvent::ScanStarted {
                            command_id: 2.into(),
                            scan_id: 2.into(),
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
                let deadline = Duration::from_secs(2);
                let starting = owner.start_scan(ScanOptions::default(), deadline);
                tokio::pin!(starting);
                tokio::select! {
                    result = &mut starting => panic!("start completed with incomplete evidence: {result:?}"),
                    event = owner.next_event(deadline) => assert_eq!(event, Ok(Some(first))),
                }
                assert_eq!(handle.emit(stopped.clone()), EventIngressStatus::Delivered);
                tokio::select! {
                    result = &mut starting => panic!("native stop prematurely settled start: {result:?}"),
                    event = owner.next_event(deadline) => assert_eq!(event, Ok(Some(stopped))),
                }
                assert_eq!(
                    owner.start_scan(ScanOptions::default(), deadline).await,
                    Err(WaiterError::ScanBusy),
                    "native stop must retain unresolved start evidence (terminal_first={terminal_first})"
                );
                assert!(
                    tokio::time::timeout(Duration::from_millis(20), &mut starting)
                        .await
                        .is_err()
                );
                assert_eq!(handle.commands().len(), 1);

                assert_eq!(handle.emit(last.clone()), EventIngressStatus::Delivered);
                assert_eq!(
                    tokio::time::timeout(deadline, &mut starting).await.unwrap(),
                    Ok(1.into()),
                    "the live start waiter must settle before its stopped session retires"
                );
                assert_eq!(owner.next_event(deadline).await, Ok(Some(last)));
                assert_eq!(
                    owner.stop_scan(1.into(), deadline).await,
                    Err(WaiterError::ScanNotActive)
                );
                assert_eq!(
                    owner.start_scan(ScanOptions::default(), deadline).await,
                    Ok(2.into())
                );
                assert!(!owner.faulted_for_test());
                tokio::time::timeout(deadline, owner.close()).await.unwrap().unwrap();
            });
            assert_eq!(handle.commands().len(), 2);
            assert_eq!(handle.dispose_count(), 1);
        }
    }
}

#[test]
fn timed_out_stop_waits_for_matching_late_native_stop() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let (backend, handle) = FakeBackend::scripted([
        started_scan(),
        EventScenario::accept(stop_command(2)),
        EventScenario::emit_before_return(
            RecordedCommand::StartScan(StartScanCommand {
                command_id: CommandId { value: 3 },
                scan_session_id: ScanSessionId { value: 2 },
                options: ScanOptions::default(),
            }),
            vec![
                BleEvent::ScanStarted {
                    command_id: CommandId { value: 3 },
                    scan_id: ScanSessionId { value: 2 },
                },
                BleEvent::CommandCompleted {
                    command_id: CommandId { value: 3 },
                    error: None,
                },
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
        let session = owner
            .start_scan(ScanOptions::default(), Duration::from_secs(2))
            .await
            .unwrap();
        assert_eq!(
            owner.stop_scan(session, Duration::from_millis(20)).await,
            Err(WaiterError::Timeout)
        );
        assert_eq!(
            owner
                .start_scan(ScanOptions::default(), Duration::from_millis(20))
                .await,
            Err(WaiterError::ScanBusy)
        );
        assert_eq!(
            handle.emit(requested_stop(99)),
            EventIngressStatus::Delivered
        );
        assert_eq!(
            owner.stop_scan(session, Duration::from_millis(20)).await,
            Err(WaiterError::ScanBusy)
        );
        assert_eq!(
            handle.emit(requested_stop(2)),
            EventIngressStatus::Delivered
        );
        assert_eq!(
            handle.emit(BleEvent::CommandCompleted {
                command_id: CommandId { value: 2 },
                error: None,
            }),
            EventIngressStatus::Delivered
        );
        assert_eq!(
            owner
                .start_scan(ScanOptions::default(), Duration::from_secs(2))
                .await,
            Ok(ScanSessionId { value: 2 })
        );
        owner.close().await.unwrap();
    });
    assert_eq!(handle.commands().len(), 3);
}

#[test]
fn stop_requires_completed_start_and_never_dispatches_for_unknown_session() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let (backend, handle) = FakeBackend::scripted([
        EventScenario::accept(RecordedCommand::StartScan(StartScanCommand {
            command_id: CommandId { value: 1 },
            scan_session_id: ScanSessionId { value: 1 },
            options: ScanOptions::default(),
        })),
        EventScenario::emit_before_return(
            stop_command(2),
            vec![
                requested_stop(2),
                BleEvent::CommandCompleted {
                    command_id: CommandId { value: 2 },
                    error: None,
                },
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
            owner
                .stop_scan(ScanSessionId { value: 1 }, Duration::from_secs(1))
                .await,
            Err(WaiterError::ScanNotActive)
        );
        assert_eq!(
            owner
                .start_scan(ScanOptions::default(), Duration::from_millis(20))
                .await,
            Err(WaiterError::Timeout)
        );
        assert_eq!(
            owner
                .stop_scan(ScanSessionId { value: 1 }, Duration::from_secs(1))
                .await,
            Err(WaiterError::ScanBusy)
        );
        for event in [
            BleEvent::ScanStarted {
                command_id: CommandId { value: 1 },
                scan_id: ScanSessionId { value: 1 },
            },
            BleEvent::CommandCompleted {
                command_id: CommandId { value: 1 },
                error: None,
            },
        ] {
            assert_eq!(handle.emit(event.clone()), EventIngressStatus::Delivered);
            // Drain only the start events; stop cannot launch until both have arrived.
            assert_eq!(
                owner.next_event(Duration::from_secs(2)).await,
                Ok(Some(event))
            );
        }
        assert_eq!(
            owner
                .stop_scan(ScanSessionId { value: 1 }, Duration::from_secs(2))
                .await,
            Ok(())
        );
        owner.close().await.unwrap();
    });
    assert_eq!(handle.commands().len(), 2);
}

#[test]
fn cancelling_stop_ask_before_actor_handles_it_never_dispatches() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let (backend, handle) = FakeBackend::scripted([started_scan()]);
    owner
        .attach_hub(BleHub::new(Arc::new(backend), owner.event_sink()).unwrap())
        .unwrap();
    let host = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    host.block_on(async {
        let session = owner
            .start_scan(ScanOptions::default(), Duration::from_secs(2))
            .await
            .unwrap();
        let entered = Arc::new(Notify::new());
        let release = Arc::new(Notify::new());
        owner
            .actor
            .tell(HoldActor {
                entered: Arc::clone(&entered),
                release: Arc::clone(&release),
            })
            .try_send()
            .unwrap();
        entered.notified().await;
        assert!(
            tokio::time::timeout(
                Duration::from_millis(20),
                owner.stop_scan(session, Duration::from_secs(1))
            )
            .await
            .is_err()
        );
        release.notify_one();
        tokio::time::timeout(Duration::from_secs(2), owner.close())
            .await
            .unwrap()
            .unwrap();
    });
    assert_eq!(handle.commands().len(), 1);
    assert_eq!(handle.dispose_count(), 1);
}

#[test]
fn test_waiter_registration_advances_only_command_ids() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let options = ScanOptions::default();
    let id = CommandId { value: 2 };
    let session = ScanSessionId { value: 1 };
    let (backend, handle) = FakeBackend::scripted([EventScenario::emit_before_return(
        RecordedCommand::StartScan(StartScanCommand {
            command_id: id,
            scan_session_id: session,
            options: options.clone(),
        }),
        vec![
            BleEvent::ScanStarted {
                command_id: id,
                scan_id: session,
            },
            BleEvent::CommandCompleted {
                command_id: id,
                error: None,
            },
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
        let ticket = owner
            .register_waiter(CommandId { value: 1 }, WaiterExpectation::TerminalOnly)
            .await
            .unwrap();
        drop(ticket);
        assert_eq!(
            owner.start_scan(options, Duration::from_secs(2)).await,
            Ok(session)
        );
        owner.close().await.unwrap();
    });
    assert_eq!(handle.commands().len(), 1);
}

#[test]
fn rejected_scan_start_faults_on_buffered_events_but_plain_rejection_does_not() {
    for with_callback in [false, true] {
        let owner = RuntimeOwner::new_assuming_initialized().unwrap();
        let id = CommandId { value: 1 };
        let scan = ScanSessionId { value: 1 };
        let options = ScanOptions::default();
        let error = CommandDispatchError::InvalidState {
            detail: "scan start rejected".to_owned(),
        };
        let command = RecordedCommand::StartScan(StartScanCommand {
            command_id: id,
            scan_session_id: scan,
            options: options.clone(),
        });
        let scenario = if with_callback {
            EventScenario::emit_before_reject(
                command,
                vec![
                    BleEvent::ScanStarted {
                        command_id: id,
                        scan_id: scan,
                    },
                    BleEvent::CommandCompleted {
                        command_id: id,
                        error: None,
                    },
                ],
                error.clone(),
            )
        } else {
            EventScenario::reject(command, error.clone())
        };
        let (backend, handle) = FakeBackend::scripted([scenario]);
        owner
            .attach_hub(BleHub::new(Arc::new(backend), owner.event_sink()).unwrap())
            .unwrap();
        let host = tokio::runtime::Builder::new_multi_thread()
            .enable_time()
            .build()
            .unwrap();
        host.block_on(async {
            assert_eq!(
                tokio::time::timeout(
                    Duration::from_secs(2),
                    owner.start_scan(options, Duration::from_secs(1))
                )
                .await
                .unwrap(),
                if with_callback {
                    Err(WaiterError::TransportFault)
                } else {
                    Err(WaiterError::DispatchRejected(error))
                }
            );
            if with_callback {
                assert!(owner.faulted_for_test());
                assert_eq!(
                    owner.next_event(Duration::from_secs(2)).await,
                    Err(EventReadError::Fault),
                    "rejected scan must not publish buffered success events"
                );
            } else {
                assert!(!owner.faulted_for_test());
                assert_eq!(
                    owner.next_event(Duration::from_millis(30)).await,
                    Err(EventReadError::Timeout)
                );
            }
            owner.close().await.unwrap();
        });
        assert_eq!(handle.dispose_count(), 1);
    }
}

#[test]
fn cancelling_begin_ask_before_actor_handles_it_does_not_dispatch() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let options = ScanOptions::default();
    let (backend, handle) = FakeBackend::scripted([]);
    owner
        .attach_hub(BleHub::new(Arc::new(backend), owner.event_sink()).unwrap())
        .unwrap();
    let host = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    host.block_on(async {
        let entered = Arc::new(Notify::new());
        let release = Arc::new(Notify::new());
        owner
            .actor
            .tell(HoldActor {
                entered: Arc::clone(&entered),
                release: Arc::clone(&release),
            })
            .try_send()
            .unwrap();
        entered.notified().await;
        assert!(
            tokio::time::timeout(
                Duration::from_millis(20),
                owner.start_scan(options.clone(), Duration::from_secs(1))
            )
            .await
            .is_err()
        );
        release.notify_one();
        // The queued ask may be dropped or handled, but must not start native work.
        tokio::time::timeout(Duration::from_secs(2), owner.close())
            .await
            .unwrap()
            .unwrap();
    });
    assert_eq!(handle.commands().len(), 0);
    assert_eq!(handle.dispose_count(), 1);
}

#[test]
fn duplicate_failure_after_success_cannot_release_a_live_scan_slot() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let id = CommandId { value: 1 };
    let scan = ScanSessionId { value: 1 };
    let options = ScanOptions::default();
    let (backend, handle) = FakeBackend::scripted([EventScenario::emit_before_return(
        RecordedCommand::StartScan(StartScanCommand {
            command_id: id,
            scan_session_id: scan,
            options: options.clone(),
        }),
        vec![
            BleEvent::ScanStarted {
                command_id: id,
                scan_id: scan,
            },
            BleEvent::CommandCompleted {
                command_id: id,
                error: None,
            },
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
            owner
                .start_scan(options.clone(), Duration::from_secs(2))
                .await,
            Ok(scan)
        );
        assert!(
            owner
                .next_event(Duration::from_secs(2))
                .await
                .unwrap()
                .is_some()
        );
        assert!(
            owner
                .next_event(Duration::from_secs(2))
                .await
                .unwrap()
                .is_some()
        );
        let duplicate = BleEvent::CommandCompleted {
            command_id: id,
            error: Some(BleEventError {
                category: BleErrorCategory::Backend,
                message: "late duplicate".to_owned(),
                platform_details: None,
            }),
        };
        assert_eq!(
            handle.emit(duplicate.clone()),
            EventIngressStatus::Delivered
        );
        assert_eq!(
            owner.next_event(Duration::from_secs(2)).await,
            Ok(Some(duplicate))
        );
        assert_eq!(
            owner.start_scan(options, Duration::from_millis(20)).await,
            Err(WaiterError::ScanBusy)
        );
        owner.close().await.unwrap();
    });
    assert_eq!(handle.commands().len(), 1);
}

#[test]
fn timeout_keeps_scan_slot_until_matching_native_stop() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let first = ScanSessionId { value: 1 };
    let options = ScanOptions::default();
    let (backend, handle) = FakeBackend::scripted([
        EventScenario::accept(RecordedCommand::StartScan(StartScanCommand {
            command_id: CommandId { value: 1 },
            scan_session_id: first,
            options: options.clone(),
        })),
        EventScenario::emit_before_return(
            RecordedCommand::StartScan(StartScanCommand {
                command_id: CommandId { value: 2 },
                scan_session_id: ScanSessionId { value: 2 },
                options: options.clone(),
            }),
            vec![
                BleEvent::ScanStarted {
                    command_id: CommandId { value: 2 },
                    scan_id: ScanSessionId { value: 2 },
                },
                BleEvent::CommandCompleted {
                    command_id: CommandId { value: 2 },
                    error: None,
                },
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
            owner
                .start_scan(options.clone(), Duration::from_millis(20))
                .await,
            Err(WaiterError::Timeout)
        );
        assert_eq!(
            owner
                .start_scan(options.clone(), Duration::from_millis(20))
                .await,
            Err(WaiterError::ScanBusy)
        );
        let stopped = BleEvent::ScanStopped {
            scan_id: first,
            result: ScanStopResult::Ok {
                reason: ScanStopReason::SystemSuspended,
            },
        };
        assert_eq!(handle.emit(stopped.clone()), EventIngressStatus::Delivered);
        assert_eq!(
            owner.next_event(Duration::from_secs(2)).await,
            Ok(Some(stopped))
        );
        assert_eq!(
            owner
                .start_scan(options.clone(), Duration::from_millis(20))
                .await,
            Err(WaiterError::ScanBusy),
            "timeout and native stop must not discard unresolved start evidence"
        );
        for event in [
            BleEvent::ScanStarted {
                command_id: 1.into(),
                scan_id: first,
            },
            command_completed(1, None),
        ] {
            assert_eq!(handle.emit(event.clone()), EventIngressStatus::Delivered);
            assert_eq!(
                owner.next_event(Duration::from_secs(2)).await,
                Ok(Some(event))
            );
        }
        assert_eq!(
            tokio::time::timeout(
                Duration::from_secs(2),
                owner.start_scan(options, Duration::from_secs(1))
            )
            .await
            .unwrap(),
            Ok(ScanSessionId { value: 2 })
        );
        owner.close().await.unwrap();
    });
    assert_eq!(handle.dispose_count(), 1);
    assert_eq!(handle.commands().len(), 2);
}
