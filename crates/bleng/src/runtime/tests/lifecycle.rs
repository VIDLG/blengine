use super::support::{
    Arc, AssemblyAdmissionError, BleEvent, BleHub, CommandDispatchError, CommandId, Duration,
    EventIngressStatus, EventReadError, EventScenario, FakeBackend, Notify, RecordedCommand,
    RuntimeOwner, ScanOptions, ScanSessionId, StartScanCommand, WaiterError, WaiterExpectation,
    command_completed, event, mpsc, oneshot,
};
use crate::runtime::actor::HoldActor;
use kameo::prelude::{Context, Message};

struct PanicActor;

impl Message<PanicActor> for crate::runtime::BlengActor {
    type Reply = ();

    async fn handle(&mut self, _: PanicActor, _: &mut Context<Self, Self::Reply>) {
        panic!("test actor handler failure");
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
fn waiter_deadline_with_full_mailbox_retires_delivery_without_fault_in_both_paths() {
    use crate::runtime::messages::DispatchResolved;
    use kameo::error::SendError;
    use std::{future::Future, task::Poll};

    struct ReleaseActorOnDrop(Arc<Notify>);

    impl Drop for ReleaseActorOnDrop {
        fn drop(&mut self) {
            self.0.notify_one();
        }
    }

    let host = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    // Exercise the timer branch first so the old pre-expired failure cannot skip it.
    for pre_expired in [false, true] {
        let owner = RuntimeOwner::new().unwrap();
        host.block_on(async {
            let ticket = tokio::time::timeout(
                Duration::from_secs(2),
                owner.register_waiter(CommandId { value: 1 }, WaiterExpectation::TerminalOnly),
            )
            .await
            .unwrap()
            .unwrap();
            let entered = Arc::new(Notify::new());
            let release = Arc::new(Notify::new());
            let release_actor = ReleaseActorOnDrop(Arc::clone(&release));
            owner
                .actor
                .tell(HoldActor {
                    entered: Arc::clone(&entered),
                    release,
                })
                .try_send()
                .unwrap();
            tokio::time::timeout(Duration::from_secs(2), entered.notified())
                .await
                .expect("actor must enter the hold before filling its mailbox");

            let mut full = false;
            for _ in 0..256 {
                match owner
                    .actor
                    .tell(DispatchResolved {
                        id: CommandId { value: u64::MAX },
                        result: Ok(()),
                    })
                    .try_send()
                {
                    Ok(()) => {}
                    Err(SendError::MailboxFull(_)) => {
                        full = true;
                        break;
                    }
                    Err(error) => panic!("unexpected mailbox error: {error:?}"),
                }
            }
            assert!(full, "held actor mailbox must be full");
            assert!(
                !owner.faulted_for_test(),
                "raw tell must not fault lifecycle"
            );

            let deadline = if pre_expired {
                tokio::time::Instant::now() - Duration::from_millis(1)
            } else {
                tokio::time::Instant::now() + Duration::from_millis(100)
            };
            let mut wait = Box::pin(ticket.wait_until(deadline));
            if !pre_expired {
                let initial = std::future::poll_fn(|cx| Poll::Ready(wait.as_mut().poll(cx))).await;
                assert!(
                    initial.is_pending(),
                    "future deadline must enter timer wait"
                );
            }
            let result = tokio::time::timeout(Duration::from_secs(2), wait).await;
            let faulted = owner.faulted_for_test();
            drop(release_actor);
            tokio::time::timeout(Duration::from_secs(2), owner.close())
                .await
                .expect("released actor must close")
                .unwrap();

            assert!(
                !faulted,
                "delivery-only timeout must not need mailbox capacity"
            );
            assert_eq!(
                result.expect("waiter deadline must settle"),
                Err(WaiterError::Timeout),
                "pre_expired={pre_expired}"
            );
        });
    }
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
fn actor_panic_after_close_request_is_not_reported_as_success() {
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
        let release = owner.hold_actor_for_test().await;
        owner.actor.tell(PanicActor).try_send().unwrap();
        owner.request_close();
        release.notify_one();
        assert_eq!(
            tokio::time::timeout(Duration::from_secs(2), owner.close())
                .await
                .expect("close must finish after actor panic"),
            Err(crate::cleanup::CleanupError::WorkerStopped)
        );
    });
    assert_eq!(handle.dispose_count(), 1);
}

#[test]
fn coordinator_abort_fails_close_callers_and_retires_owned_hub() {
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
        let _release = owner.hold_actor_for_test().await;
        owner.coordinator_abort.abort();
        for _ in 0..2 {
            assert_eq!(
                tokio::time::timeout(Duration::from_secs(2), owner.close())
                    .await
                    .expect("coordinator failure must wake close callers"),
                Err(crate::cleanup::CleanupError::WorkerStopped)
            );
        }
        tokio::time::timeout(Duration::from_secs(2), owner.lifecycle.wait_for_cleanup())
            .await
            .expect("failed coordinator must still retire the owned Hub")
            .unwrap();
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
fn close_waits_for_actor_teardown_after_mailbox_closes() {
    struct ReleaseRegistryOnDrop {
        release: mpsc::Sender<()>,
        thread: Option<std::thread::JoinHandle<()>>,
    }

    impl Drop for ReleaseRegistryOnDrop {
        fn drop(&mut self) {
            let _ = self.release.send(());
            if let Some(thread) = self.thread.take() {
                let _ = thread.join();
            }
        }
    }

    let owner = Arc::new(RuntimeOwner::new().unwrap());
    let host = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    host.block_on(async {
        let _ticket = tokio::time::timeout(
            Duration::from_secs(2),
            owner.register_waiter(CommandId { value: 1 }, WaiterExpectation::TerminalOnly),
        )
        .await
        .expect("actor must start before the registry is locked")
        .unwrap();
        let (acquired_tx, acquired_rx) = oneshot::channel();
        let (release_tx, release_rx) = mpsc::channel();
        let registry_thread = std::thread::spawn(move || {
            let registry = kameo::registry::ACTOR_REGISTRY
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner);
            let _ = acquired_tx.send(());
            // Sender drop and a bounded fallback both release this process-global lock.
            let _ = release_rx.recv_timeout(Duration::from_secs(10));
            drop(registry);
        });
        let release_registry = ReleaseRegistryOnDrop {
            release: release_tx,
            thread: Some(registry_thread),
        };
        tokio::time::timeout(Duration::from_secs(2), acquired_rx)
            .await
            .expect("registry thread must acquire the lock")
            .unwrap();

        tokio::time::timeout(Duration::from_secs(2), owner.actor.stop_gracefully())
            .await
            .expect("normal actor stop must be accepted")
            .unwrap();
        tokio::time::timeout(Duration::from_secs(2), owner.actor.wait_for_shutdown())
            .await
            .expect("mailbox must close even while actor unregister is blocked");
        // Start close with the mailbox already closed: an old coordinator must not
        // look pending merely because its wake-up is on the blocked actor worker.
        let close_owner = Arc::clone(&owner);
        let mut close = tokio::spawn(async move { close_owner.close().await });
        tokio::time::timeout(Duration::from_secs(2), owner.lifecycle.wait_for_cleanup())
            .await
            .expect("cleanup without a Hub must already finish")
            .unwrap();
        let before_release = tokio::time::timeout(Duration::from_millis(50), &mut close).await;
        drop(release_registry);

        assert!(
            before_release.is_err(),
            "close completed before actor teardown, with registry still locked: {before_release:?}"
        );
        tokio::time::timeout(Duration::from_secs(2), close)
            .await
            .expect("close must finish after actor unregister is released")
            .unwrap()
            .unwrap();
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
