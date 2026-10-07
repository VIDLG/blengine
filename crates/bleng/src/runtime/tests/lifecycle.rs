use super::support::{
    Arc, AssemblyAdmissionError, BleEvent, BleHub, CommandDispatchError, CommandId, Duration,
    EventIngressStatus, EventReadError, EventScenario, FakeBackend, RecordedCommand, RuntimeOwner,
    ScanOptions, ScanSessionId, StartScanCommand, WaiterError, WaiterExpectation,
    command_completed, event,
};

#[test]
fn event_publication_rejects_a_closed_or_faulted_gate_before_enqueueing() {
    use crate::runtime::{BlengActor, RuntimeLifecycle};
    use tokio::sync::mpsc::{channel, error::TryRecvError};

    for faulted in [false, true] {
        let lifecycle = Arc::new(RuntimeLifecycle::new());
        let (event_tx, mut event_rx) = channel(1);
        let actor = BlengActor::new(event_tx, Arc::clone(&lifecycle));
        if faulted {
            lifecycle.fault();
        } else {
            lifecycle.close();
        }
        assert_eq!(
            actor.publish_event(&command_completed(1, None)),
            Err(if faulted {
                WaiterError::TransportFault
            } else {
                WaiterError::Closed
            })
        );
        assert_eq!(event_rx.try_recv(), Err(TryRecvError::Empty));
    }
}

#[test]
fn real_hub_scan_waiter_requires_domain_even_after_successful_terminal() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let command = RecordedCommand::StartScan(StartScanCommand {
        command_id: CommandId { value: 1 },
        scan_session_id: ScanSessionId { value: 1 },
        options: ScanOptions::default(),
    });
    let terminal = command_completed(1, None);
    let domain = BleEvent::ScanStarted {
        command_id: CommandId { value: 1 },
        scan_id: ScanSessionId { value: 1 },
    };
    let events = vec![terminal.clone(), domain.clone()];
    let (backend, handle) =
        FakeBackend::scripted([EventScenario::complete(command, events.clone())]);
    owner
        .attach_hub(BleHub::new(Arc::new(backend), owner.event_sink()).unwrap())
        .unwrap();
    let host = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    host.block_on(async {
        let mut start = Box::pin(owner.start_scan(ScanOptions::default(), Duration::from_secs(2)));
        tokio::time::timeout(Duration::from_secs(2), async {
            loop {
                tokio::select! {
                    result = &mut start => panic!("scan finished before callbacks: {result:?}"),
                    _ = tokio::task::yield_now() => {}
                }
                if handle.pending_event_count() == events.len() {
                    break;
                }
            }
        })
        .await
        .unwrap();
        assert_eq!(handle.advance(), Some(EventIngressStatus::Delivered));
        assert!(
            tokio::time::timeout(Duration::from_millis(20), &mut start)
                .await
                .is_err()
        );
        assert_eq!(handle.advance(), Some(EventIngressStatus::Delivered));
        assert_eq!(start.await, Ok(ScanSessionId { value: 1 }));
        for expected in events {
            assert_eq!(
                owner.next_event(Duration::from_secs(2)).await,
                Ok(Some(expected))
            );
        }
        owner.close().await.unwrap();
    });
    assert_eq!(handle.dispose_count(), 1);
}

#[test]
fn rejection_cancellation_timeout_and_close_settle_distinct_waiters() {
    let owner = RuntimeOwner::new().unwrap();
    let host = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    host.block_on(async {
        let id = CommandId { value: 1 };
        let ticket = owner
            .register_waiter(id, WaiterExpectation::TerminalOnly)
            .await
            .unwrap();
        let rejection = CommandDispatchError::InvalidState {
            detail: "rejected".to_owned(),
        };
        owner.resolve_dispatch(id, Err(rejection.clone()));
        assert_eq!(
            ticket
                .wait_until(tokio::time::Instant::now() + Duration::from_secs(2))
                .await,
            Err(WaiterError::DispatchRejected(rejection))
        );

        let cancelled = owner
            .register_waiter(CommandId { value: 2 }, WaiterExpectation::TerminalOnly)
            .await
            .unwrap();
        drop(cancelled);
        let timed_out = owner
            .register_waiter(CommandId { value: 3 }, WaiterExpectation::TerminalOnly)
            .await
            .unwrap();
        assert_eq!(
            timed_out.wait_until(tokio::time::Instant::now()).await,
            Err(WaiterError::Timeout)
        );
        let pending = owner
            .register_waiter(CommandId { value: 4 }, WaiterExpectation::TerminalOnly)
            .await
            .unwrap();
        owner.close().await.unwrap();
        assert_eq!(
            pending
                .wait_until(tokio::time::Instant::now() + Duration::from_secs(2))
                .await,
            Err(WaiterError::Closed)
        );
        assert!(matches!(
            owner
                .register_waiter(CommandId { value: 5 }, WaiterExpectation::TerminalOnly)
                .await,
            Err(WaiterError::Closed)
        ));
    });
}

#[test]
fn overflow_wakes_all_pending_waiters_without_success() {
    let owner = RuntimeOwner::new().unwrap();
    let host = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    host.block_on(async {
        let first = owner
            .register_waiter(CommandId { value: 1 }, WaiterExpectation::TerminalOnly)
            .await
            .unwrap();
        let second = owner
            .register_waiter(CommandId { value: 2 }, WaiterExpectation::TerminalOnly)
            .await
            .unwrap();
        owner.lifecycle.fault();
        assert_eq!(
            first
                .wait_until(tokio::time::Instant::now() + Duration::from_secs(2))
                .await,
            Err(WaiterError::TransportFault)
        );
        assert_eq!(
            second
                .wait_until(tokio::time::Instant::now() + Duration::from_secs(2))
                .await,
            Err(WaiterError::TransportFault)
        );
        owner.close().await.unwrap();
    });
}

#[test]
fn bounded_ingress_faults_instead_of_losing_key_events_silently() {
    let owner = RuntimeOwner::new().unwrap();
    let sink = owner.event_sink();
    for _ in 0..256 {
        let _ = sink.on_event(event());
        if owner.faulted_for_test() {
            break;
        }
    }
    let host = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    host.block_on(async {
        tokio::time::timeout(Duration::from_secs(2), async {
            while !owner.faulted_for_test() {
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("event transport must fault when no consumer drains it");
    });
    assert!(sink.on_event(event()).is_err());
    host.block_on(async {
        assert_eq!(
            owner.next_event(Duration::from_secs(2)).await,
            Err(EventReadError::Fault)
        );
        owner.close().await.unwrap();
        assert_eq!(
            owner.next_event(Duration::from_secs(2)).await,
            Err(EventReadError::Fault)
        );
    });
}

#[test]
fn overflow_disposes_the_owned_hub_without_explicit_close() {
    let owner = RuntimeOwner::new().unwrap();
    let (backend, handle) = FakeBackend::scripted([]);
    let hub = BleHub::new(Arc::new(backend), owner.event_sink()).unwrap();
    let late = handle.ingress_for_callback().unwrap();
    owner.attach_hub(hub).unwrap();
    for _ in 0..256 {
        let _ = handle.emit(event());
        if owner.faulted_for_test() {
            break;
        }
    }
    let host = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    host.block_on(async {
        tokio::time::timeout(Duration::from_secs(2), async {
            while !owner.faulted_for_test() {
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("ingress must fault when the receiver stalls");
        assert_eq!(
            owner.next_event(Duration::from_secs(2)).await,
            Err(EventReadError::Fault)
        );
        tokio::time::timeout(Duration::from_secs(2), owner.wait_for_actor_stop_for_test())
            .await
            .expect("overflow must stop the actor without explicit close");
        tokio::time::timeout(Duration::from_secs(2), owner.lifecycle.wait_for_cleanup())
            .await
            .expect("fault must schedule native cleanup")
            .unwrap();
        assert_eq!(owner.close().await, Ok(()));
        assert_eq!(owner.close().await, Ok(()));
    });
    assert_eq!(handle.dispose_count(), 1);
    assert_eq!(late.push(event()), EventIngressStatus::Closed);
}

#[test]
fn attachment_after_fault_is_rejected_and_retired_before_close_returns() {
    let owner = RuntimeOwner::new().unwrap();
    owner.lifecycle.fault();
    let (backend, handle) = FakeBackend::scripted([]);
    let hub = BleHub::new(Arc::new(backend), owner.event_sink()).unwrap();
    assert_eq!(owner.attach_hub(hub), Err(AssemblyAdmissionError::Closed));
    let host = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    host.block_on(async {
        assert_eq!(
            tokio::time::timeout(Duration::from_secs(2), owner.close())
                .await
                .unwrap(),
            Ok(())
        );
    });
    assert_eq!(handle.dispose_count(), 1);
}

#[test]
fn unexpected_actor_stop_fails_pending_waiter_and_disposes_hub() {
    let owner = RuntimeOwner::new().unwrap();
    let (backend, handle) = FakeBackend::scripted([]);
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
        owner.actor.kill();
        assert_eq!(
            tokio::time::timeout(
                Duration::from_secs(2),
                ticket.wait_until(tokio::time::Instant::now() + Duration::from_secs(2))
            )
            .await
            .unwrap(),
            Err(WaiterError::TransportFault)
        );
        assert_eq!(
            tokio::time::timeout(Duration::from_secs(2), owner.close())
                .await
                .unwrap(),
            Ok(())
        );
    });
    assert_eq!(handle.dispose_count(), 1);
}

#[test]
fn waiting_receiver_is_woken_on_transport_fault() {
    let owner = RuntimeOwner::new().unwrap();
    let lifecycle = Arc::clone(&owner.lifecycle);
    let host = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    host.block_on(async {
        let (result, ()) = tokio::join!(owner.next_event(Duration::from_secs(2)), async move {
            tokio::time::sleep(Duration::from_millis(10)).await;
            lifecycle.fault();
        });
        assert_eq!(result, Err(EventReadError::Fault));
    });
}

#[test]
fn drop_inside_an_async_host_does_not_block_on_runtime_drop() {
    let host = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    host.block_on(async {
        let owner = RuntimeOwner::new().unwrap();
        drop(owner);
        let owner = RuntimeOwner::new().unwrap();
        owner.close().await.unwrap();
        drop(owner);
    });
}

#[test]
fn close_is_idempotent_and_waits_for_actor_shutdown() {
    let owner = RuntimeOwner::new().expect("runtime should be constructible");
    let handle = owner.handle();
    handle.block_on(owner.close()).unwrap();
    assert!(owner.is_closed());
    handle.block_on(owner.close()).unwrap();
    assert!(owner.is_closed());
}
