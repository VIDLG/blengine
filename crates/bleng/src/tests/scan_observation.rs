use std::{
    future::{Future, pending, poll_fn},
    sync::{Arc, mpsc},
    task::{Context, Poll, Wake, Waker},
    thread,
    time::{Duration, Instant},
};

use blehub::{
    AdapterSnapshot, AdapterState, AuthorizationState, BleEvent, EventIngressStatus,
    InitializeCommand, ScanOptions, StartScanCommand,
};
use blehub_testkit::{EventScenario, FakeBackend, FakeBackendHandle, RecordedCommand};

use crate::{Bleng, BlengError};

// Model a foreign executor with neither a Tokio runtime nor a timer driver.
fn block_on_without_runtime<T>(future: impl Future<Output = T>) -> T {
    struct Caller(thread::Thread);
    impl Wake for Caller {
        fn wake(self: Arc<Self>) {
            self.0.unpark();
        }
    }

    assert!(tokio::runtime::Handle::try_current().is_err());
    let waker = Waker::from(Arc::new(Caller(thread::current())));
    let mut context = Context::from_waker(&waker);
    let mut future = std::pin::pin!(future);
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        assert!(Instant::now() < deadline, "foreign-executor wait stalled");
        if let Poll::Ready(output) = future.as_mut().poll(&mut context) {
            return output;
        }
        thread::park_timeout(deadline.saturating_duration_since(Instant::now()));
    }
}

fn scan_fixture(complete_scan: bool) -> (Arc<Bleng>, FakeBackendHandle) {
    let scan = RecordedCommand::StartScan(StartScanCommand {
        command_id: 2.into(),
        scan_session_id: 1.into(),
        options: ScanOptions::default(),
    });
    scan_fixture_with_scenario(if complete_scan {
        EventScenario::emit_before_return(
            scan,
            vec![
                BleEvent::ScanStarted {
                    command_id: 2.into(),
                    scan_id: 1.into(),
                },
                BleEvent::CommandCompleted {
                    command_id: 2.into(),
                    error: None,
                },
            ],
        )
    } else {
        EventScenario::accept(scan)
    })
}

fn scan_fixture_with_scenario(scan: EventScenario) -> (Arc<Bleng>, FakeBackendHandle) {
    let (backend, handle) = FakeBackend::scripted([
        EventScenario::emit_before_return(
            RecordedCommand::Initialize(InitializeCommand {
                command_id: 1.into(),
            }),
            vec![
                BleEvent::AdapterSnapshotChanged {
                    command_id: Some(1.into()),
                    snapshot: AdapterSnapshot {
                        state: AdapterState::PoweredOn,
                        scan_authorization: AuthorizationState::Authorized,
                        connection_authorization: AuthorizationState::Authorized,
                        name: None,
                    },
                },
                BleEvent::CommandCompleted {
                    command_id: 1.into(),
                    error: None,
                },
            ],
        ),
        scan,
    ]);
    let bleng = Bleng::new().unwrap();
    block_on_without_runtime(bleng.assemble_backend(Arc::new(backend))).unwrap();
    block_on_without_runtime(bleng.initialize(2000)).unwrap();
    (bleng, handle)
}

#[test]
fn timeout_and_success_without_caller_runtime_preserve_start_result() {
    let (bleng, handle) = scan_fixture(true);
    let owner = Arc::clone(&bleng.owner);
    let release = block_on_without_runtime(
        bleng
            .owner
            .run_task(async move { owner.hold_actor_for_test().await }),
    )
    .unwrap();
    let operation = bleng.begin_start_scan(ScanOptions::default()).unwrap();

    assert!(matches!(
        block_on_without_runtime(operation.observe_with_timeout(10, pending::<()>())),
        Err(BlengError::Timeout)
    ));
    let value = block_on_without_runtime(operation.observe_with_timeout(86_400_000, async {
        assert!(tokio::runtime::Handle::try_current().is_ok());
        tokio::time::sleep(Duration::from_millis(1)).await;
        String::from("observed")
    }))
    .unwrap();
    assert_eq!(value, "observed");
    assert_eq!(handle.commands().len(), 1);
    assert_eq!(handle.dispose_count(), 0);

    release.notify_one();
    assert_eq!(
        block_on_without_runtime(operation.wait(2000)).unwrap(),
        1.into()
    );
    assert!(matches!(
        block_on_without_runtime(operation.wait(2000)),
        Err(BlengError::OperationResultConsumed)
    ));
    assert_eq!(handle.commands().len(), 2);
    block_on_without_runtime(bleng.shutdown()).unwrap();
    assert_eq!(handle.dispose_count(), 1);
    assert!(handle.contract_violations().is_empty());
}

#[test]
fn timeout_uses_private_timer_with_timerless_caller_runtimes() {
    let callers = [
        tokio::runtime::Builder::new_current_thread()
            .build()
            .unwrap(),
        tokio::runtime::Builder::new_multi_thread()
            .worker_threads(1)
            .build()
            .unwrap(),
    ];
    for caller in callers {
        caller.block_on(async {
            let bleng = Bleng::new().unwrap();
            let (backend, _) = FakeBackend::scripted([]);
            bleng.assemble_backend(Arc::new(backend)).await.unwrap();
            let operation = bleng.begin_start_scan(ScanOptions::default()).unwrap();
            assert!(matches!(
                operation.observe_with_timeout(1, pending::<()>()).await,
                Err(BlengError::Timeout)
            ));
            bleng.shutdown().await.unwrap();
        });
    }
}

#[test]
fn invalid_timeout_does_not_poll_observation_or_consume_start_result() {
    use std::sync::atomic::{AtomicUsize, Ordering};

    let (bleng, handle) = scan_fixture(true);
    let operation = bleng.begin_start_scan(ScanOptions::default()).unwrap();
    let polls = Arc::new(AtomicUsize::new(0));
    for timeout_ms in [0, 86_400_001, u64::MAX] {
        let polled = Arc::clone(&polls);
        assert!(matches!(
            block_on_without_runtime(operation.observe_with_timeout(
                timeout_ms,
                poll_fn(move |_| {
                    polled.fetch_add(1, Ordering::SeqCst);
                    Poll::Ready(())
                }),
            )),
            Err(BlengError::InvalidTimeout)
        ));
    }
    assert_eq!(polls.load(Ordering::SeqCst), 0);
    assert_eq!(
        block_on_without_runtime(operation.wait(2000)).unwrap(),
        1.into()
    );
    block_on_without_runtime(bleng.shutdown()).unwrap();
    assert_eq!(handle.dispose_count(), 1);
}

#[test]
fn dropping_observation_preserves_start_result_and_explicit_cancel() {
    for cancel in [false, true] {
        let (bleng, handle) = scan_fixture(true);
        let owner = Arc::clone(&bleng.owner);
        let release = block_on_without_runtime(
            bleng
                .owner
                .run_task(async move { owner.hold_actor_for_test().await }),
        )
        .unwrap();
        let operation = bleng.begin_start_scan(ScanOptions::default()).unwrap();
        let (started_tx, started_rx) = mpsc::channel();
        let (dropped_tx, dropped_rx) = mpsc::channel();
        let mut observation = Box::pin(operation.observe_with_timeout(2000, async move {
            let _guard = scopeguard::guard(dropped_tx, |tx| tx.send(()).unwrap());
            started_tx.send(()).unwrap();
            pending::<()>().await;
        }));
        assert!(
            observation
                .as_mut()
                .poll(&mut Context::from_waker(Waker::noop()))
                .is_pending()
        );
        started_rx.recv_timeout(Duration::from_secs(2)).unwrap();
        drop(observation);
        dropped_rx.recv_timeout(Duration::from_secs(2)).unwrap();
        assert_eq!(handle.commands().len(), 1);
        assert_eq!(handle.dispose_count(), 0);

        if cancel {
            block_on_without_runtime(operation.cancel()).unwrap();
            assert!(matches!(
                block_on_without_runtime(operation.wait(2000)),
                Err(BlengError::Cancelled)
            ));
            release.notify_one();
        } else {
            release.notify_one();
            assert_eq!(
                block_on_without_runtime(operation.wait(2000)).unwrap(),
                1.into()
            );
        }
        assert!(matches!(
            block_on_without_runtime(operation.wait(2000)),
            Err(BlengError::OperationResultConsumed)
        ));
        block_on_without_runtime(bleng.shutdown()).unwrap();
        assert_eq!(handle.commands().len(), if cancel { 1 } else { 2 });
        assert_eq!(handle.dispose_count(), 1);
        assert!(handle.contract_violations().is_empty());
    }
}

#[test]
fn pending_observation_and_handle_do_not_keep_hub_lifecycle_open() {
    let (bleng, handle) = scan_fixture(false);
    let operation = bleng.begin_start_scan(ScanOptions::default()).unwrap();
    block_on_without_runtime(async {
        while handle.commands().len() != 2 {
            tokio::task::yield_now().await;
        }
    });
    let (started_tx, started_rx) = mpsc::channel();
    let (dropped_tx, dropped_rx) = mpsc::channel();
    let mut observation = Box::pin(operation.observe_with_timeout(86_400_000, async move {
        let _guard = scopeguard::guard(dropped_tx, |tx| tx.send(()).unwrap());
        started_tx.send(()).unwrap();
        pending::<()>().await;
    }));
    assert!(
        observation
            .as_mut()
            .poll(&mut Context::from_waker(Waker::noop()))
            .is_pending()
    );
    started_rx.recv_timeout(Duration::from_secs(2)).unwrap();

    let weak = Arc::downgrade(&bleng);
    drop(bleng);
    assert!(weak.upgrade().is_none());
    assert!(matches!(
        block_on_without_runtime(operation.wait(2000)),
        Err(BlengError::Closed)
    ));
    assert!(matches!(
        block_on_without_runtime(operation.cancel()),
        Err(BlengError::Closed)
    ));
    block_on_without_runtime(async {
        while handle.dispose_count() != 1 {
            tokio::task::yield_now().await;
        }
    });
    let late = BleEvent::CommandCompleted {
        command_id: operation.command_id(),
        error: None,
    };
    assert_eq!(handle.emit(late), EventIngressStatus::Closed);
    assert_eq!(handle.commands().len(), 2);
    assert!(
        observation
            .as_mut()
            .poll(&mut Context::from_waker(Waker::noop()))
            .is_pending()
    );
    drop(observation);
    dropped_rx.recv_timeout(Duration::from_secs(2)).unwrap();
    assert_eq!(handle.dispose_count(), 1);
}

#[derive(Default)]
struct FailureCounter(std::sync::atomic::AtomicUsize);

impl crate::OperationFailureObserver for FailureCounter {
    fn operation_failed(&self) {
        self.0.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
    }
}

fn failure_count(counter: &FailureCounter) -> usize {
    counter.0.load(std::sync::atomic::Ordering::SeqCst)
}

#[test]
fn scan_start_failure_without_wait_or_after_timeout_or_drop_is_observed_and_replayed() {
    for wait_mode in 0..3 {
        let (bleng, handle) = scan_fixture(false);
        let operation = bleng.begin_start_scan(ScanOptions::default()).unwrap();
        let counter = Arc::new(FailureCounter::default());
        let sink: Arc<dyn crate::OperationFailureObserver> = counter.clone();
        let token = operation.observe_failure(Arc::downgrade(&sink)).unwrap();
        if wait_mode == 1 {
            assert!(matches!(
                block_on_without_runtime(operation.wait(1)),
                Err(BlengError::Timeout)
            ));
        } else if wait_mode == 2 {
            let mut wait = Box::pin(operation.wait(2000));
            assert!(
                wait.as_mut()
                    .poll(&mut Context::from_waker(Waker::noop()))
                    .is_pending()
            );
            drop(wait);
        }
        assert_eq!(
            failure_count(&counter),
            0,
            "caller retirement is not start failure"
        );
        let error = blehub::BleEventError {
            category: blehub::BleErrorCategory::Internal,
            message: "late start failure".into(),
            platform_details: None,
        };
        assert_eq!(
            handle.emit(BleEvent::CommandCompleted {
                command_id: operation.command_id(),
                error: Some(error.clone()),
            }),
            EventIngressStatus::Delivered
        );
        // Order after dispatch reconciliation and settlement without consuming wait.
        block_on_without_runtime(async {
            loop {
                if matches!(bleng.next_event(2000).await.unwrap(), Some(BleEvent::CommandCompleted { command_id, .. }) if command_id == operation.command_id())
                {
                    break;
                }
            }
            let owner = Arc::clone(&bleng.owner);
            bleng
                .owner
                .run_task(async move {
                    owner.hold_actor_for_test().await.notify_one();
                })
                .await
                .unwrap();
        });
        assert_eq!(failure_count(&counter), 1);
        drop(token);
        let _late = operation.observe_failure(Arc::downgrade(&sink)).unwrap();
        assert_eq!(failure_count(&counter), 2);
        let result = block_on_without_runtime(operation.wait(2000));
        if wait_mode == 0 {
            assert!(
                matches!(result, Err(BlengError::Operation { error: actual }) if actual == error)
            );
        } else {
            assert!(matches!(result, Err(BlengError::OperationResultConsumed)));
        }
        block_on_without_runtime(bleng.shutdown()).unwrap();
    }
}

#[test]
fn scan_start_failure_without_ble_events_is_observed_for_admission_and_dispatch_rejection() {
    struct FailureSignal(mpsc::Sender<()>);
    impl crate::OperationFailureObserver for FailureSignal {
        fn operation_failed(&self) {
            self.0.send(()).unwrap();
        }
    }
    for dispatch in [false, true] {
        let (bleng, handle) = if dispatch {
            scan_fixture_with_scenario(EventScenario::reject(
                RecordedCommand::StartScan(StartScanCommand {
                    command_id: 2.into(),
                    scan_session_id: 1.into(),
                    options: ScanOptions::default(),
                }),
                blehub::CommandDispatchError::InvalidState {
                    detail: "start rejected".into(),
                },
            ))
        } else {
            let (backend, handle) = FakeBackend::scripted([]);
            let bleng = Bleng::new().unwrap();
            block_on_without_runtime(bleng.assemble_backend(Arc::new(backend))).unwrap();
            (bleng, handle)
        };
        let operation = bleng.begin_start_scan(ScanOptions::default()).unwrap();
        // Registration may race the rejection; both early notification and late
        // replay must work without a typed wait or BLE terminal.
        let (failed, failure) = mpsc::channel();
        let signal: Arc<dyn crate::OperationFailureObserver> = Arc::new(FailureSignal(failed));
        let _registration = operation.observe_failure(Arc::downgrade(&signal)).unwrap();
        failure
            .recv_timeout(Duration::from_secs(2))
            .expect("failure must arrive without a typed wait or BLE terminal");
        // Only after observation has completed do we consume the precise typed error.
        let result = block_on_without_runtime(operation.wait(2000));
        if dispatch {
            assert!(matches!(result, Err(BlengError::DispatchRejected { .. })));
            assert_eq!(handle.commands().len(), 2);
        } else {
            assert!(matches!(
                result,
                Err(BlengError::NotReady {
                    reason: crate::BlengNotReadyReason::NotInitialized
                })
            ));
            assert!(handle.commands().is_empty());
        }
        block_on_without_runtime(bleng.shutdown()).unwrap();
    }
}

#[test]
fn scan_start_lifecycle_rejection_after_queued_close_does_not_notify() {
    assert_queued_scan_lifecycle_rejection(false);
}

#[test]
fn scan_start_lifecycle_rejection_after_queued_fault_does_not_notify() {
    assert_queued_scan_lifecycle_rejection(true);
}

fn assert_queued_scan_lifecycle_rejection(fault: bool) {
    let (bleng, handle) = scan_fixture(false);
    let owner = Arc::clone(&bleng.owner);
    let release = block_on_without_runtime(
        bleng
            .owner
            .run_task(async move { owner.hold_actor_for_test().await }),
    )
    .unwrap();
    let operation = bleng.begin_start_scan(ScanOptions::default()).unwrap();
    let counter = Arc::new(FailureCounter::default());
    let sink: Arc<dyn crate::OperationFailureObserver> = counter.clone();
    let early = operation.observe_failure(Arc::downgrade(&sink)).unwrap();
    if fault {
        bleng.owner.fault_read_runtime_for_test();
    } else {
        bleng.owner.request_close();
    }
    release.notify_one();
    // Join the actor, not just the lifecycle-aware wait: the queued BeginScan must
    // have executed its rejection before testing both notification and late replay.
    block_on_without_runtime(bleng.shutdown()).unwrap();
    let result = block_on_without_runtime(operation.wait(2000));
    if fault {
        assert!(matches!(result, Err(BlengError::TransportFault)));
    } else {
        assert!(matches!(result, Err(BlengError::Closed)));
    }
    assert_eq!(
        handle.commands().len(),
        1,
        "only fixture initialization may dispatch"
    );
    assert_eq!(handle.dispose_count(), 1);
    let early_count = failure_count(&counter);
    drop(early);
    let _late = operation.observe_failure(Arc::downgrade(&sink)).unwrap();
    assert_eq!((early_count, failure_count(&counter)), (0, 0));
}

#[test]
fn scan_start_failure_preclaim_cancel_does_not_dispatch_or_consume_wait() {
    let (bleng, handle) = scan_fixture(false);
    let owner = Arc::clone(&bleng.owner);
    let release = block_on_without_runtime(
        bleng
            .owner
            .run_task(async move { owner.hold_actor_for_test().await }),
    )
    .unwrap();
    let operation = bleng.begin_start_scan(ScanOptions::default()).unwrap();
    let counter = Arc::new(FailureCounter::default());
    let sink: Arc<dyn crate::OperationFailureObserver> = counter.clone();
    let _token = operation.observe_failure(Arc::downgrade(&sink)).unwrap();
    block_on_without_runtime(operation.cancel()).unwrap();
    let count = failure_count(&counter);
    release.notify_one();
    assert!(matches!(
        block_on_without_runtime(operation.wait(2000)),
        Err(BlengError::Cancelled)
    ));
    block_on_without_runtime(bleng.shutdown()).unwrap();
    assert_eq!(count, 1);
    assert_eq!(handle.commands().len(), 1);
}

#[test]
fn observation_task_panic_keeps_worker_failure_mapping_and_cleanup() {
    let (bleng, handle) = scan_fixture(false);
    let operation = bleng.begin_start_scan(ScanOptions::default()).unwrap();
    assert!(matches!(
        block_on_without_runtime(operation.observe_with_timeout(2000, async {
            panic!("observation task failed");
        })),
        Err(BlengError::WorkerStopped)
    ));
    assert!(matches!(
        block_on_without_runtime(operation.wait(2000)),
        Err(BlengError::TransportFault)
    ));
    block_on_without_runtime(bleng.shutdown()).unwrap();
    assert_eq!(handle.dispose_count(), 1);
}
