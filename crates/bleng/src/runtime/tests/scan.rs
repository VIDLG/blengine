use super::support::*;

#[test]
fn start_scan_allocates_ids_and_accepts_synchronous_events_before_dispatch_returns() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let id = CommandId { value: 1 };
    let scan = ScanSessionId { value: 2 };
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
        (false, requested_stop(3)),
        (true, requested_stop(3)),
        (
            false,
            BleEvent::ScanStopped {
                scan_id: ScanSessionId { value: 2 },
                result: ScanStopResult::Ok {
                    reason: ScanStopReason::SystemSuspended,
                },
            },
        ),
    ] {
        let owner = RuntimeOwner::new_assuming_initialized().unwrap();
        let terminal = BleEvent::CommandCompleted {
            command_id: CommandId { value: 3 },
            error: None,
        };
        let mut events = vec![stopped.clone(), terminal.clone()];
        if terminal_first {
            events.reverse();
        }
        let (backend, handle) = FakeBackend::scripted([
            started_scan(),
            EventScenario::emit_before_return(stop_command(3), events.clone()),
            EventScenario::emit_before_return(
                RecordedCommand::StartScan(StartScanCommand {
                    command_id: CommandId { value: 4 },
                    scan_session_id: ScanSessionId { value: 5 },
                    options: ScanOptions::default(),
                }),
                vec![
                    BleEvent::ScanStarted {
                        command_id: CommandId { value: 4 },
                        scan_id: ScanSessionId { value: 5 },
                    },
                    BleEvent::CommandCompleted {
                        command_id: CommandId { value: 4 },
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
            assert_eq!(session, ScanSessionId { value: 2 });
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
                Ok(ScanSessionId { value: 5 })
            );
            owner.close().await.unwrap();
        });
        assert_eq!(handle.commands().len(), 3);
        assert_eq!(handle.dispose_count(), 1);
    }
}

#[test]
fn rejected_stop_keeps_scan_active_and_discards_predispatch_events() {
    for with_callback in [false, true] {
        let owner = RuntimeOwner::new_assuming_initialized().unwrap();
        let rejection = CommandDispatchError::InvalidState {
            detail: "stop rejected".to_owned(),
        };
        let scenario = if with_callback {
            EventScenario::emit_before_reject(
                stop_command(3),
                vec![
                    requested_stop(3),
                    BleEvent::CommandCompleted {
                        command_id: CommandId { value: 3 },
                        error: None,
                    },
                ],
                rejection.clone(),
            )
        } else {
            EventScenario::reject(stop_command(3), rejection.clone())
        };
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
                if with_callback {
                    Err(WaiterError::TransportFault)
                } else {
                    Err(WaiterError::DispatchRejected(rejection))
                }
            );
            if with_callback {
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
fn native_stop_error_retires_scan_and_cannot_poison_next_session() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let error = BleEventError {
        category: BleErrorCategory::Backend,
        message: "stop failed".to_owned(),
        platform_details: None,
    };
    let (backend, handle) = FakeBackend::scripted([
        started_scan(),
        EventScenario::emit_before_return(
            stop_command(3),
            vec![
                BleEvent::ScanStopped {
                    scan_id: ScanSessionId { value: 2 },
                    result: ScanStopResult::Err {
                        error: error.clone(),
                    },
                },
                BleEvent::CommandCompleted {
                    command_id: CommandId { value: 3 },
                    error: None,
                },
            ],
        ),
        EventScenario::emit_before_return(
            RecordedCommand::StartScan(StartScanCommand {
                command_id: CommandId { value: 4 },
                scan_session_id: ScanSessionId { value: 5 },
                options: ScanOptions::default(),
            }),
            vec![
                BleEvent::ScanStarted {
                    command_id: CommandId { value: 4 },
                    scan_id: ScanSessionId { value: 5 },
                },
                BleEvent::CommandCompleted {
                    command_id: CommandId { value: 4 },
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
            Ok(ScanSessionId { value: 5 })
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
            stop_command(3),
            vec![BleEvent::CommandCompleted {
                command_id: CommandId { value: 3 },
                error: Some(error.clone()),
            }],
        ),
        EventScenario::emit_before_return(
            stop_command(4),
            vec![
                requested_stop(4),
                BleEvent::CommandCompleted {
                    command_id: CommandId { value: 4 },
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
                command_id: CommandId { value: 3 },
                scan_session_id: ScanSessionId { value: 4 },
                options: ScanOptions::default(),
            }),
            vec![
                BleEvent::ScanStarted {
                    command_id: CommandId { value: 3 },
                    scan_id: ScanSessionId { value: 4 },
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
            Ok(ScanSessionId { value: 4 })
        );
        owner.close().await.unwrap();
    });
    assert_eq!(handle.commands().len(), 2);
}

#[test]
fn timed_out_stop_waits_for_matching_late_native_stop() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let (backend, handle) = FakeBackend::scripted([
        started_scan(),
        EventScenario::accept(stop_command(3)),
        EventScenario::emit_before_return(
            RecordedCommand::StartScan(StartScanCommand {
                command_id: CommandId { value: 4 },
                scan_session_id: ScanSessionId { value: 5 },
                options: ScanOptions::default(),
            }),
            vec![
                BleEvent::ScanStarted {
                    command_id: CommandId { value: 4 },
                    scan_id: ScanSessionId { value: 5 },
                },
                BleEvent::CommandCompleted {
                    command_id: CommandId { value: 4 },
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
            handle.emit(requested_stop(3)),
            EventIngressStatus::Delivered
        );
        assert_eq!(
            handle.emit(BleEvent::CommandCompleted {
                command_id: CommandId { value: 3 },
                error: None,
            }),
            EventIngressStatus::Delivered
        );
        assert_eq!(
            owner
                .start_scan(ScanOptions::default(), Duration::from_secs(2))
                .await,
            Ok(ScanSessionId { value: 5 })
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
            scan_session_id: ScanSessionId { value: 2 },
            options: ScanOptions::default(),
        })),
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
        assert_eq!(
            owner
                .stop_scan(ScanSessionId { value: 2 }, Duration::from_secs(1))
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
                .stop_scan(ScanSessionId { value: 2 }, Duration::from_secs(1))
                .await,
            Err(WaiterError::ScanBusy)
        );
        for event in [
            BleEvent::ScanStarted {
                command_id: CommandId { value: 1 },
                scan_id: ScanSessionId { value: 2 },
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
                .stop_scan(ScanSessionId { value: 2 }, Duration::from_secs(2))
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
fn test_registration_does_not_collide_with_actor_allocated_scan_ids() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let options = ScanOptions::default();
    let id = CommandId { value: 2 };
    let session = ScanSessionId { value: 3 };
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
fn synchronous_events_cannot_turn_a_rejected_dispatch_into_success() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let id = CommandId { value: 1 };
    let scan = ScanSessionId { value: 2 };
    let options = ScanOptions::default();
    let error = CommandDispatchError::InvalidState {
        detail: "rejected after callback".to_owned(),
    };
    let (backend, handle) = FakeBackend::scripted([EventScenario::emit_before_reject(
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
        error.clone(),
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
                owner.start_scan(options, Duration::from_secs(1))
            )
            .await
            .unwrap(),
            Err(WaiterError::DispatchRejected(error))
        );
        assert_eq!(
            owner.next_event(Duration::from_millis(30)).await,
            Err(EventReadError::Timeout),
            "rejected scan must not publish buffered success events"
        );
        owner.close().await.unwrap();
    });
    assert_eq!(handle.dispose_count(), 1);
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
    let scan = ScanSessionId { value: 2 };
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
    let first = ScanSessionId { value: 2 };
    let options = ScanOptions::default();
    let (backend, handle) = FakeBackend::scripted([
        EventScenario::accept(RecordedCommand::StartScan(StartScanCommand {
            command_id: CommandId { value: 1 },
            scan_session_id: first,
            options: options.clone(),
        })),
        EventScenario::emit_before_return(
            RecordedCommand::StartScan(StartScanCommand {
                command_id: CommandId { value: 3 },
                scan_session_id: ScanSessionId { value: 4 },
                options: options.clone(),
            }),
            vec![
                BleEvent::ScanStarted {
                    command_id: CommandId { value: 3 },
                    scan_id: ScanSessionId { value: 4 },
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
            tokio::time::timeout(
                Duration::from_secs(2),
                owner.start_scan(options, Duration::from_secs(1))
            )
            .await
            .unwrap(),
            Ok(ScanSessionId { value: 4 })
        );
        owner.close().await.unwrap();
    });
    assert_eq!(handle.dispose_count(), 1);
    assert_eq!(handle.commands().len(), 2);
}
