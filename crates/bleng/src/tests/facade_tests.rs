use crate::error::{
    BlengBusyReason, BlengContractFailure, BlengError, BlengNotReadyReason, BlengResource,
};
use crate::{cleanup, runtime, waiter};
use blehub::{
    AdapterSnapshot, AdapterState, AuthorizationState, BleErrorCategory, BleEvent, BleEventError,
    BluetoothUuid, CharacteristicProperties, CommandDispatchError, CommandId, ConnectCommand,
    ConnectionId, ConnectionState, ConnectionStateResult, DiscoverGattCommand, GattCharacteristic,
    GattCharacteristicId, GattDatabase, GattService, GattServiceId, GattSessionId,
    InitializeCommand, PeripheralId, Platform, PlatformErrorDetails, ReadCharacteristicCommand,
};
use blehub_testkit::{CancellationPolicy, EventScenario, FakeBackend, RecordedCommand};
use std::sync::Arc;

#[path = "scan_observation.rs"]
mod scan_observation;

#[test]
fn synchronous_submission_is_eager_bounded_and_cancel_wins_before_claim() {
    let host = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .unwrap();
    host.block_on(async {
        let bleng = super::Bleng::new().unwrap();
        let (backend, handle) = FakeBackend::scripted([]);
        bleng.assemble_backend(Arc::new(backend)).await.unwrap();
        let release = bleng.owner.hold_actor_for_test().await;
        let operation: Arc<super::InitializeOperation> = bleng.begin_initialize().unwrap();
        assert!(operation.cancel().await.is_ok());
        assert!(matches!(
            operation.wait(2000).await,
            Err(BlengError::Cancelled)
        ));
        assert!(matches!(
            operation.wait(2000).await,
            Err(BlengError::OperationResultConsumed)
        ));
        let mut queued = Vec::new();
        for _ in 1..64 {
            queued.push(bleng.begin_initialize().unwrap());
        }
        assert!(matches!(
            bleng.begin_initialize(),
            Err(BlengError::ResourceExhausted {
                resource: BlengResource::SubmissionQueue,
            })
        ));
        assert!(handle.commands().is_empty());
        // Delivery drop must not add retirement messages to an already-full mailbox.
        drop(queued);
        bleng.owner.request_close();
        release.notify_one();
        bleng.shutdown().await.unwrap();
        assert!(handle.commands().is_empty());
    });
}

#[test]
fn queued_delivery_drop_does_not_retract_eager_submission() {
    let host = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .unwrap();
    host.block_on(async {
        let (backend, handle) = FakeBackend::scripted([EventScenario::emit_before_return(
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
        )]);
        let bleng = super::Bleng::new().unwrap();
        bleng.assemble_backend(Arc::new(backend)).await.unwrap();
        let release = bleng.owner.hold_actor_for_test().await;
        let operation = bleng.begin_initialize().unwrap();
        drop(operation);
        release.notify_one();
        assert!(matches!(
            bleng.next_event(2000).await.unwrap(),
            Some(BleEvent::AdapterSnapshotChanged { .. })
        ));
        assert!(matches!(
            bleng.next_event(2000).await.unwrap(),
            Some(BleEvent::CommandCompleted { .. })
        ));
        assert_eq!(handle.commands().len(), 1);
        bleng.shutdown().await.unwrap();
    });
}

#[test]
fn completed_admission_errors_and_queued_cancellation_keep_typed_close_fault_precedence() {
    let host = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .unwrap();
    host.block_on(async {
        for cancelled in [false, true] {
            for fault in [false, true] {
                let (backend, handle) = FakeBackend::scripted([]);
                let bleng = super::Bleng::new().unwrap();
                bleng.assemble_backend(Arc::new(backend)).await.unwrap();
                let release = if cancelled { Some(bleng.owner.hold_actor_for_test().await) } else { None };
                let operation = bleng.begin_read_characteristic(1.into(), 2.into(), 30.into()).unwrap();
                if cancelled {
                    operation.cancel().await.unwrap();
                } else {
                    bleng.owner.read_settlement_barrier(operation.operation.result_slot.clone(), true).await;
                    assert!(matches!(operation.cancel().await,
                        Err(BlengError::OperationAlreadyCompleted { command_id }) if command_id == 1.into()));
                }
                if fault { bleng.owner.fault_read_runtime_for_test(); } else { bleng.owner.request_close(); }
                if let Some(release) = release { release.notify_one(); }
                let outcome = operation.wait(2000).await;
                if fault { assert!(matches!(outcome, Err(BlengError::TransportFault))); }
                else if cancelled { assert!(matches!(outcome, Err(BlengError::Cancelled))); }
                else { assert!(matches!(outcome, Err(BlengError::NotReady { reason: BlengNotReadyReason::NotInitialized }))); }
                assert!(matches!(operation.wait(2000).await, Err(BlengError::OperationResultConsumed)));
                bleng.shutdown().await.unwrap();
                assert!(handle.commands().is_empty());
            }
        }
    });
}

#[test]
fn typed_wait_budget_includes_private_runtime_scheduling_delay() {
    use std::{future::Future, sync::mpsc, task::Poll, time::Duration};

    let caller = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .unwrap();
    caller.block_on(async {
        let bleng = super::Bleng::new().unwrap();
        let (backend, handle) = FakeBackend::scripted([]);
        bleng.assemble_backend(Arc::new(backend)).await.unwrap();
        let operation = bleng
            .begin_read_characteristic(1.into(), 2.into(), 30.into())
            .unwrap();
        bleng
            .owner
            .read_settlement_barrier(operation.operation.result_slot.clone(), true)
            .await;

        // Occupy actual executor workers, before ResultSlot can compute a deadline.
        // Dropping the senders releases every worker even if an assertion panics.
        let executor = bleng.owner.handle();
        let mut releases = Vec::new();
        let (entered_tx, entered_rx) = mpsc::channel();
        let mut workers = Vec::new();
        for _ in 0..executor.metrics().num_workers() {
            let entered = entered_tx.clone();
            let (release, released) = mpsc::channel::<()>();
            releases.push(release);
            workers.push(executor.spawn(async move {
                entered.send(()).unwrap();
                released.recv_timeout(Duration::from_secs(5))
            }));
        }
        for _ in &workers {
            entered_rx
                .recv_timeout(Duration::from_secs(2))
                .expect("private runtime worker did not enter scheduling gate");
        }

        let mut wait = Box::pin(operation.wait(100));
        assert!(
            std::future::poll_fn(|cx| Poll::Ready(wait.as_mut().poll(cx)))
                .await
                .is_pending()
        );
        // This is an upper bound on the deadline computed at the initial poll.
        // The channel gate, not a guessed sleep, prevents the private task starting.
        let expired_by = tokio::time::Instant::now() + Duration::from_millis(100);
        tokio::time::sleep_until(expired_by).await;
        assert!(tokio::time::Instant::now() >= expired_by);
        drop(releases);
        for worker in workers {
            assert!(matches!(
                worker.await.unwrap(),
                Err(mpsc::RecvTimeoutError::Disconnected)
            ));
        }
        let result = tokio::time::timeout(Duration::from_secs(2), &mut wait)
            .await
            .expect("typed wait did not finish after releasing the executor");
        assert!(matches!(
            operation.wait(2000).await,
            Err(BlengError::OperationResultConsumed)
        ));
        bleng.shutdown().await.unwrap();
        assert!(handle.commands().is_empty());
        assert!(matches!(result, Err(BlengError::Timeout)), "{result:?}");
    });
}

#[test]
fn typed_wait_dropped_before_private_task_poll_stays_consumed() {
    assert_typed_wait_claim_before_private_task_poll(true);
}

#[test]
fn typed_concurrent_wait_claims_before_private_task_poll() {
    assert_typed_wait_claim_before_private_task_poll(false);
}

fn assert_typed_wait_claim_before_private_task_poll(drop_scheduled_wait: bool) {
    use blehub::*;
    use std::{
        future::Future,
        sync::{
            atomic::{AtomicUsize, Ordering},
            mpsc,
        },
        task::{Context, Poll, Waker},
        time::Duration,
    };

    struct CountCancellation {
        backend: FakeBackend,
        calls: Arc<AtomicUsize>,
    }

    impl BleBackend for CountCancellation {
        delegate::delegate! {
            to self.backend {
                fn capabilities(&self) -> Result<BackendCapabilities, BleHubError>;
                fn bind_event_ingress(&self, ingress: Arc<BackendEventIngressHandle>) -> Result<(), BleHubError>;
                fn initialize(&self, command: InitializeCommand) -> Result<(), CommandDispatchError>;
                fn start_scan(&self, command: StartScanCommand) -> Result<(), CommandDispatchError>;
                fn stop_scan(&self, command: StopScanCommand) -> Result<(), CommandDispatchError>;
                fn connect(&self, command: ConnectCommand) -> Result<(), CommandDispatchError>;
                fn disconnect(&self, command: DisconnectCommand) -> Result<(), CommandDispatchError>;
                fn discover_gatt(&self, command: DiscoverGattCommand) -> Result<(), CommandDispatchError>;
                fn read_characteristic(&self, command: ReadCharacteristicCommand) -> Result<(), CommandDispatchError>;
                fn write_characteristic(&self, command: WriteCharacteristicCommand) -> Result<(), CommandDispatchError>;
                fn set_notifications(&self, command: SetNotificationsCommand) -> Result<(), CommandDispatchError>;
                fn read_rssi(&self, command: ReadRssiCommand) -> Result<(), CommandDispatchError>;
                fn dispose(&self) -> Result<(), BleHubError>;
            }
        }

        fn cancel_operation(
            &self,
            request: CancelOperationRequest,
        ) -> Result<(), CommandDispatchError> {
            self.calls.fetch_add(1, Ordering::SeqCst);
            self.backend.cancel_operation(request)
        }
    }

    let caller = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .unwrap();
    caller.block_on(async {
        let (backend, handle) = FakeBackend::scripted_with_cancellation(
            [
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
                EventScenario::accept(RecordedCommand::StartScan(StartScanCommand {
                    command_id: 2.into(),
                    scan_session_id: 1.into(),
                    options: ScanOptions::default(),
                })),
            ],
            CancellationPolicy::Accept,
        );
        let calls = Arc::new(AtomicUsize::new(0));
        let bleng = super::Bleng::new().unwrap();
        bleng
            .assemble_backend(Arc::new(CountCancellation {
                backend,
                calls: Arc::clone(&calls),
            }))
            .await
            .unwrap();
        bleng.initialize(2000).await.unwrap();
        let operation = bleng.begin_start_scan(ScanOptions::default()).unwrap();
        tokio::time::timeout(Duration::from_secs(2), async {
            while handle.commands().len() != 2 {
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap();

        // Neither creating an unpolled wait nor rejecting its timeout owns delivery.
        drop(operation.wait(2000));
        for timeout in [0, 86_400_001, u64::MAX] {
            assert!(matches!(
                operation.wait(timeout).await,
                Err(BlengError::InvalidTimeout)
            ));
        }

        // Use the existing scheduling gate: no private task can poll until every
        // worker is released. Sender drop also releases them on assertion failure.
        let executor = bleng.owner.handle();
        let (entered_tx, entered_rx) = mpsc::channel();
        let mut releases = Vec::new();
        let mut workers = Vec::new();
        for _ in 0..executor.metrics().num_workers() {
            let entered = entered_tx.clone();
            let (release, released) = mpsc::channel::<()>();
            releases.push(release);
            workers.push(executor.spawn(async move {
                entered.send(()).unwrap();
                released.recv_timeout(Duration::from_secs(5))
            }));
        }
        for _ in &workers {
            entered_rx.recv_timeout(Duration::from_secs(2)).unwrap();
        }

        let mut first = Box::pin(operation.wait(2000));
        assert!(
            first
                .as_mut()
                .poll(&mut Context::from_waker(Waker::noop()))
                .is_pending()
        );
        let first = if drop_scheduled_wait {
            drop(first);
            None
        } else {
            Some(first)
        };
        let mut second = Box::pin(operation.wait(2000));
        assert!(
            matches!(
                second
                    .as_mut()
                    .poll(&mut Context::from_waker(Waker::noop())),
                Poll::Ready(Err(BlengError::OperationResultConsumed))
            ),
            "the public first poll must claim delivery before private scheduling"
        );
        drop(second);

        drop(releases);
        for worker in workers {
            assert!(matches!(
                worker.await.unwrap(),
                Err(mpsc::RecvTimeoutError::Disconnected)
            ));
        }

        assert_eq!(calls.load(Ordering::SeqCst), 0);
        if let Some(first) = first {
            operation.cancel().await.unwrap();
            assert_eq!(calls.load(Ordering::SeqCst), 1);
            assert!(matches!(first.await, Err(BlengError::Cancelled)));
        } else {
            // Caller retirement must leave native work and its public result alone.
            for event in [
                BleEvent::ScanStarted {
                    command_id: 2.into(),
                    scan_id: 1.into(),
                },
                BleEvent::CommandCompleted {
                    command_id: 2.into(),
                    error: None,
                },
            ] {
                assert_eq!(handle.emit(event), EventIngressStatus::Delivered);
            }
            tokio::time::timeout(Duration::from_secs(2), async {
                loop {
                    if let Some(BleEvent::CommandCompleted { command_id, error }) =
                        bleng.next_event(2000).await.unwrap()
                        && command_id == operation.command_id()
                    {
                        assert!(error.is_none());
                        break;
                    }
                }
            })
            .await
            .unwrap();
        }
        assert!(matches!(
            operation.wait(2000).await,
            Err(BlengError::OperationResultConsumed)
        ));
        bleng.shutdown().await.unwrap();
        assert_eq!(
            calls.load(Ordering::SeqCst),
            usize::from(!drop_scheduled_wait)
        );
        assert_eq!(handle.dispose_count(), 1);
    });
}

#[test]
fn queued_timeout_and_dropped_wait_do_not_disable_explicit_cancellation() {
    let host = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .unwrap();
    host.block_on(async {
        for drop_wait in [false, true] {
            let bleng = super::Bleng::new().unwrap();
            let (backend, handle) = FakeBackend::scripted([]);
            bleng.assemble_backend(Arc::new(backend)).await.unwrap();
            let release = bleng.owner.hold_actor_for_test().await;
            let operation = bleng.begin_read_characteristic(1.into(), 2.into(), 30.into()).unwrap();
            if drop_wait {
                let (entered, release_wait) = operation.operation.result_slot.pause_wait_for_test(false);
                let waiting = Arc::clone(&operation);
                let wait = tokio::spawn(async move { waiting.wait(2000).await });
                tokio::time::timeout(std::time::Duration::from_secs(2), entered).await.unwrap().unwrap();
                wait.abort();
                assert!(wait.await.unwrap_err().is_cancelled());
                drop(release_wait);
            } else {
                assert!(matches!(operation.wait(1).await, Err(BlengError::Timeout)));
            }
            operation.cancel().await.unwrap();
            assert!(matches!(operation.cancel().await,
                Err(BlengError::OperationAlreadyCompleted { command_id }) if command_id == 1.into()));
            assert!(matches!(operation.wait(2000).await, Err(BlengError::OperationResultConsumed)));
            bleng.owner.request_close();
            release.notify_one();
            bleng.shutdown().await.unwrap();
            assert!(handle.commands().is_empty());
        }
    });
}

fn event_error(category: BleErrorCategory) -> BleEventError {
    BleEventError {
        category,
        message: "test operation failure".to_owned(),
        platform_details: None,
    }
}

async fn read_operation_fixture() -> (Arc<super::Bleng>, blehub_testkit::FakeBackendHandle) {
    let initialize_id = CommandId { value: 1 };
    let connect_id = CommandId { value: 2 };
    let discover_id = CommandId { value: 3 };
    let read_id = CommandId { value: 4 };
    let completed_read_id = CommandId { value: 5 };
    let timed_out_read_id = CommandId { value: 6 };
    let dropped_read_id = CommandId { value: 7 };
    let connection_id = ConnectionId { value: 1 };
    let session_id = GattSessionId { value: 2 };
    let characteristic_id = GattCharacteristicId { value: 30 };
    let database = GattDatabase {
        connection_id,
        gatt_session_id: session_id,
        services: vec![GattService {
            id: GattServiceId { value: 1 },
            uuid: BluetoothUuid {
                value: "0000180d-0000-1000-8000-00805f9b34fb".to_owned(),
            },
            primary: Some(true),
            characteristics: vec![GattCharacteristic {
                id: characteristic_id,
                uuid: BluetoothUuid {
                    value: "00002a37-0000-1000-8000-00805f9b34fb".to_owned(),
                },
                properties: CharacteristicProperties {
                    readable: true,
                    writable_with_response: false,
                    writable_without_response: false,
                    notifiable: false,
                    indicatable: false,
                },
                descriptors: Vec::new(),
            }],
        }],
    };
    let (backend, handle) = FakeBackend::scripted_with_cancellation(
        [
            EventScenario::emit_before_return(
                RecordedCommand::Initialize(InitializeCommand {
                    command_id: initialize_id,
                }),
                vec![
                    BleEvent::AdapterSnapshotChanged {
                        command_id: Some(initialize_id),
                        snapshot: AdapterSnapshot {
                            state: AdapterState::PoweredOn,
                            scan_authorization: AuthorizationState::Authorized,
                            connection_authorization: AuthorizationState::Authorized,
                            name: Some("test adapter".to_owned()),
                        },
                    },
                    BleEvent::CommandCompleted {
                        command_id: initialize_id,
                        error: None,
                    },
                ],
            ),
            EventScenario::emit_before_return(
                RecordedCommand::Connect(ConnectCommand {
                    command_id: connect_id,
                    connection_id,
                    peripheral_id: PeripheralId { value: 10 },
                }),
                vec![
                    BleEvent::ConnectionStateChanged {
                        command_id: Some(connect_id),
                        connection_id,
                        result: ConnectionStateResult::Ok {
                            state: ConnectionState::Connected,
                        },
                    },
                    BleEvent::CommandCompleted {
                        command_id: connect_id,
                        error: None,
                    },
                ],
            ),
            EventScenario::emit_before_return(
                RecordedCommand::DiscoverGatt(DiscoverGattCommand {
                    command_id: discover_id,
                    connection_id,
                    gatt_session_id: session_id,
                }),
                vec![
                    BleEvent::GattDiscovered {
                        command_id: discover_id,
                        database,
                    },
                    BleEvent::CommandCompleted {
                        command_id: discover_id,
                        error: None,
                    },
                ],
            ),
            EventScenario::accept(RecordedCommand::ReadCharacteristic(
                ReadCharacteristicCommand {
                    command_id: read_id,
                    connection_id,
                    gatt_session_id: session_id,
                    characteristic_id,
                },
            )),
            EventScenario::accept(RecordedCommand::ReadCharacteristic(
                ReadCharacteristicCommand {
                    command_id: completed_read_id,
                    connection_id,
                    gatt_session_id: session_id,
                    characteristic_id,
                },
            )),
            EventScenario::accept(RecordedCommand::ReadCharacteristic(
                ReadCharacteristicCommand {
                    command_id: timed_out_read_id,
                    connection_id,
                    gatt_session_id: session_id,
                    characteristic_id,
                },
            )),
            EventScenario::accept(RecordedCommand::ReadCharacteristic(
                ReadCharacteristicCommand {
                    command_id: dropped_read_id,
                    connection_id,
                    gatt_session_id: session_id,
                    characteristic_id,
                },
            )),
        ],
        CancellationPolicy::Accept,
    );
    let bleng = super::Bleng::new().unwrap();
    bleng.assemble_backend(Arc::new(backend)).await.unwrap();
    bleng.initialize(2_000).await.unwrap();
    bleng
        .connect(PeripheralId { value: 10 }, 2_000)
        .await
        .unwrap();
    bleng.discover_gatt(connection_id, 2_000).await.unwrap();
    (bleng, handle)
}

#[test]
fn facade_convenience_keeps_one_deadline_while_typed_wait_gets_its_own_budget() {
    use std::time::Duration;
    use tokio::time::{Instant, sleep_until};

    let caller = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .unwrap();
    caller.block_on(async {
        for typed in [false, true] {
            let (backend, handle) = FakeBackend::scripted([EventScenario::accept(
                RecordedCommand::Initialize(InitializeCommand {
                    command_id: 1.into(),
                }),
            )]);
            let bleng = super::Bleng::new().unwrap();
            bleng.assemble_backend(Arc::new(backend)).await.unwrap();
            let release = bleng.owner.hold_actor_for_test().await;
            let release = scopeguard::guard(release, |release| release.notify_one());
            let started = Instant::now();
            let operation = async {
                if typed {
                    let operation = bleng.begin_initialize()?;
                    sleep_until(started + Duration::from_millis(200)).await;
                    operation.wait(350).await
                } else {
                    bleng.initialize(350).await
                }
            };
            let evidence = async {
                sleep_until(started + Duration::from_millis(200)).await;
                release.notify_one();
                sleep_until(started + Duration::from_millis(450)).await;
                assert_eq!(handle.commands().len(), 1);
                let snapshot = AdapterSnapshot {
                    state: AdapterState::PoweredOn,
                    scan_authorization: AuthorizationState::Authorized,
                    connection_authorization: AuthorizationState::Authorized,
                    name: None,
                };
                assert_eq!(
                    handle.emit(BleEvent::AdapterSnapshotChanged {
                        command_id: Some(1.into()),
                        snapshot,
                    }),
                    blehub::EventIngressStatus::Delivered
                );
                assert_eq!(
                    handle.emit(BleEvent::CommandCompleted {
                        command_id: 1.into(),
                        error: None,
                    }),
                    blehub::EventIngressStatus::Delivered
                );
            };
            let (result, ()) = tokio::time::timeout(Duration::from_secs(3), async {
                tokio::join!(operation, evidence)
            })
            .await
            .unwrap();
            if typed {
                assert!(
                    result.is_ok(),
                    "typed wait has an independent budget: {result:?}"
                );
            } else {
                assert!(
                    matches!(result, Err(BlengError::Timeout)),
                    "submission must spend the convenience budget, not reset it: {result:?}"
                );
            }
            bleng.shutdown().await.unwrap();
            assert_eq!(handle.dispose_count(), 1);
        }
    });
}

#[test]
fn typed_boundary_retired_wait_observes_late_native_completion_without_receiver() {
    let caller = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    caller.block_on(async {
        for dropped_wait in [false, true] {
            for failed in [false, true] {
                let (bleng, handle) = read_operation_fixture().await;
                let operation = bleng.begin_read_characteristic(1.into(), 2.into(), 30.into()).unwrap();
                if dropped_wait {
                    let (entered, release) = operation.operation.result_slot.pause_wait_for_test(false);
                    let waiting = Arc::clone(&operation);
                    let wait = tokio::spawn(async move { waiting.wait(2000).await });
                    tokio::time::timeout(std::time::Duration::from_secs(2), entered).await.unwrap().unwrap();
                    wait.abort();
                    assert!(wait.await.unwrap_err().is_cancelled());
                    drop(release);
                } else {
                    assert!(matches!(operation.wait(1).await, Err(BlengError::Timeout)));
                }
                assert!(matches!(operation.wait(2000).await, Err(BlengError::OperationResultConsumed)));
                // Retiring caller delivery does not end native work or disable cancellation.
                operation.cancel().await.unwrap();
                assert_eq!(handle.emit(BleEvent::CharacteristicRead {
                    command_id: 4.into(), connection_id: 1.into(), gatt_session_id: 2.into(),
                    characteristic_id: 30.into(), value: vec![0xde, 0xad],
                }), blehub::EventIngressStatus::Delivered);
                assert_eq!(handle.emit(BleEvent::CommandCompleted {
                    command_id: 4.into(), error: failed.then(|| event_error(BleErrorCategory::Internal)),
                }), blehub::EventIngressStatus::Delivered);
                // Public terminal plus the subsequent actor ask orders this after retirement.
                loop {
                    if matches!(bleng.next_event(2000).await.unwrap(), Some(BleEvent::CommandCompleted { command_id, .. }) if command_id == 4.into()) { break; }
                }
                assert!(matches!(operation.cancel().await, Err(BlengError::OperationAlreadyCompleted { command_id }) if command_id == 4.into()),
                    "dropped_wait={dropped_wait}, failed={failed}: actor completion must survive receiver retirement");
                assert!(matches!(operation.wait(2000).await, Err(BlengError::OperationResultConsumed)));
                bleng.shutdown().await.unwrap();
            }
        }
    });
}

#[test]
fn typed_boundary_last_facade_drop_closes_pending_handles_without_waiting_for_cleanup() {
    let caller = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    caller.block_on(async {
        let (bleng, handle) = read_operation_fixture().await;
        let surviving_facade = Arc::clone(&bleng);
        let operation = bleng
            .begin_read_characteristic(1.into(), 2.into(), 30.into())
            .unwrap();
        drop(bleng);
        assert!(
            surviving_facade.next_event(2000).await.unwrap().is_some(),
            "dropping a facade Arc clone must not close the surviving facade"
        );
        drop(surviving_facade);
        assert!(
            matches!(operation.wait(2000).await, Err(BlengError::Closed)),
            "a handle must not own the runtime lifecycle"
        );
        assert!(matches!(operation.cancel().await, Err(BlengError::Closed)));
        assert!(matches!(
            operation.wait(2000).await,
            Err(BlengError::OperationResultConsumed)
        ));
        tokio::time::timeout(std::time::Duration::from_secs(2), async {
            while handle.dispose_count() != 1 {
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("facade Drop must request the existing cleanup coordinator");
    });
}

#[test]
fn typed_boundary_facade_drop_preserves_unclaimed_settlement_but_not_lifecycle() {
    let caller = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    caller.block_on(async {
        for failed in [false, true] {
            let (bleng, handle) = read_operation_fixture().await;
            let operation = bleng
                .begin_read_characteristic(1.into(), 2.into(), 30.into())
                .unwrap();
            complete_unclaimed_read(
                &bleng,
                &handle,
                &operation,
                failed.then(|| event_error(BleErrorCategory::Internal)),
            )
            .await;
            drop(bleng);
            assert_unclaimed_read_result(&operation, failed).await;
            assert!(
                matches!(operation.cancel().await, Err(BlengError::Closed)),
                "retaining the executor must not retain an open Hub"
            );
            tokio::time::timeout(std::time::Duration::from_secs(2), async {
                while handle.dispose_count() != 1 {
                    tokio::task::yield_now().await;
                }
            })
            .await
            .unwrap();
        }
    });
}

#[test]
fn public_read_operation_wait_and_cancel_are_independent() {
    let caller = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    caller.block_on(async {
        let (bleng, handle) = read_operation_fixture().await;
        let read_id = CommandId { value: 4 };
        let completed_read_id = CommandId { value: 5 };
        let timed_out_read_id = CommandId { value: 6 };
        let connection_id = ConnectionId { value: 1 };
        let session_id = GattSessionId { value: 2 };
        let characteristic_id = GattCharacteristicId { value: 30 };
        let operation = bleng
            .begin_read_characteristic(connection_id, session_id, characteristic_id)
            .unwrap();
        let waiting_operation = Arc::clone(&operation);
        let wait = tokio::spawn(async move { waiting_operation.wait(2_000).await });
        tokio::time::timeout(std::time::Duration::from_secs(2), async {
            while handle.commands().len() < 4 {
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap();
        assert!(operation.cancel().await.is_ok());
        assert!(matches!(wait.await.unwrap(), Err(BlengError::Cancelled)));
        assert!(matches!(
            operation.wait(2_000).await,
            Err(BlengError::OperationResultConsumed)
        ));
        let _ = handle.emit(BleEvent::CharacteristicRead {
            command_id: read_id,
            connection_id,
            gatt_session_id: session_id,
            characteristic_id,
            value: vec![0xca, 0xfe],
        });
        let _ = handle.emit(BleEvent::CommandCompleted {
            command_id: read_id,
            error: None,
        });

        let completed = bleng
            .begin_read_characteristic(connection_id, session_id, characteristic_id)
            .unwrap();
        let completed_waiter = Arc::clone(&completed);
        let completed_wait = tokio::spawn(async move { completed_waiter.wait(2_000).await });
        tokio::time::timeout(std::time::Duration::from_secs(1), async {
            while handle.commands().len() < 5 {
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("second read dispatch did not become accepted");
        let _ = handle.emit(BleEvent::CharacteristicRead {
            command_id: completed_read_id,
            connection_id,
            gatt_session_id: session_id,
            characteristic_id,
            value: vec![0xde, 0xad],
        });
        let _ = handle.emit(BleEvent::CommandCompleted {
            command_id: completed_read_id,
            error: None,
        });
        assert!(matches!(
            completed_wait.await.unwrap(),
            Ok(value) if value == vec![0xde, 0xad]
        ));
        assert!(matches!(
            completed.cancel().await,
            Err(BlengError::OperationAlreadyCompleted { command_id })
                if command_id == completed_read_id
        ));

        let timed_out = bleng
            .begin_read_characteristic(connection_id, session_id, characteristic_id)
            .unwrap();
        assert!(matches!(timed_out.wait(1).await, Err(BlengError::Timeout)));
        assert!(timed_out.cancel().await.is_ok());
        let _ = handle.emit(BleEvent::CharacteristicRead {
            command_id: timed_out_read_id,
            connection_id,
            gatt_session_id: session_id,
            characteristic_id,
            value: vec![0xba, 0xbe],
        });
        let _ = handle.emit(BleEvent::CommandCompleted {
            command_id: timed_out_read_id,
            error: None,
        });

        let dropped = bleng
            .begin_read_characteristic(connection_id, session_id, characteristic_id)
            .unwrap();
        let mut dropped_wait = Box::pin(dropped.wait(2_000));
        tokio::select! {
            result = &mut dropped_wait => panic!("dropped waiter completed early: {result:?}"),
            _ = tokio::time::sleep(std::time::Duration::from_millis(20)) => {}
        }
        drop(dropped_wait);
        assert!(matches!(
            dropped.wait(1).await,
            Err(BlengError::OperationResultConsumed)
        ));
        assert!(dropped.cancel().await.is_ok());
        bleng.shutdown().await.unwrap();
    });
}

async fn complete_unclaimed_read(
    bleng: &super::Bleng,
    handle: &blehub_testkit::FakeBackendHandle,
    operation: &super::ReadOperation,
    error: Option<BleEventError>,
) {
    assert_eq!(
        handle.emit(BleEvent::CharacteristicRead {
            command_id: operation.operation.command_id,
            connection_id: ConnectionId { value: 1 },
            gatt_session_id: GattSessionId { value: 2 },
            characteristic_id: GattCharacteristicId { value: 30 },
            value: vec![0xde, 0xad],
        }),
        blehub::EventIngressStatus::Delivered
    );
    assert_eq!(
        handle.emit(BleEvent::CommandCompleted {
            command_id: operation.operation.command_id,
            error,
        }),
        blehub::EventIngressStatus::Delivered
    );
    bleng
        .owner
        .read_settlement_barrier(operation.operation.result_slot.clone(), true)
        .await;
}

async fn assert_unclaimed_read_result(operation: &super::ReadOperation, failed: bool) {
    let result = operation.wait(2_000).await;
    if failed {
        assert!(matches!(
            result,
            Err(BlengError::Operation { error })
                if error == event_error(BleErrorCategory::Internal)
        ));
    } else {
        assert!(matches!(result, Ok(value) if value == vec![0xde, 0xad]));
    }
    assert!(matches!(
        operation.wait(2_000).await,
        Err(BlengError::OperationResultConsumed)
    ));
}

#[test]
fn convenience_wait_keeps_close_and_fault_precedence_over_settled_results_and_deadlines() {
    use std::time::Duration;
    use tokio::time::Instant;

    let caller = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .unwrap();
    caller.block_on(async {
        for fault in [false, true] {
            for failed in [false, true] {
                for expired in [false, true] {
                    let (bleng, handle) = read_operation_fixture().await;
                    // Exercise the same submission/wait boundary as convenience,
                    // holding result delivery until after the lifecycle transition.
                    let original_deadline = super::completion_deadline(2000).unwrap();
                    let operation = super::ReadOperation {
                        operation: bleng
                            .submit_read_characteristic(1.into(), 2.into(), 30.into(), false)
                            .unwrap(),
                    };
                    complete_unclaimed_read(
                        &bleng,
                        &handle,
                        &operation,
                        failed.then(|| event_error(BleErrorCategory::Internal)),
                    )
                    .await;
                    if fault {
                        bleng.owner.fault_read_runtime_for_test();
                    } else {
                        bleng.shutdown().await.unwrap();
                    }
                    let deadline = if expired {
                        Instant::now() - Duration::from_millis(1)
                    } else {
                        original_deadline
                    };
                    let result = operation.operation.wait_until(deadline).await;
                    assert!(
                        matches!(result, Err(BlengError::TransportFault)) && fault
                            || matches!(result, Err(BlengError::Closed)) && !fault,
                        "fault={fault}, failed={failed}, expired={expired}: {result:?}"
                    );
                    assert!(matches!(
                        operation.wait(2000).await,
                        Err(BlengError::OperationResultConsumed)
                    ));
                    bleng.shutdown().await.unwrap();
                }
            }
        }
    });
}

#[test]
fn read_result_slot_preserves_completed_success_and_failure_after_shutdown() {
    let caller = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    caller.block_on(async {
        for failed in [false, true] {
            let (bleng, handle) = read_operation_fixture().await;
            let operation = bleng
                .begin_read_characteristic(
                    ConnectionId { value: 1 },
                    GattSessionId { value: 2 },
                    GattCharacteristicId { value: 30 },
                )
                .unwrap();
            complete_unclaimed_read(
                &bleng,
                &handle,
                &operation,
                failed.then(|| event_error(BleErrorCategory::Internal)),
            )
            .await;
            bleng.shutdown().await.unwrap();
            assert_unclaimed_read_result(&operation, failed).await;
        }
    });
}

#[test]
fn read_result_slot_cancel_completed_unclaimed_success_and_failure_keeps_result() {
    let caller = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    caller.block_on(async {
        for failed in [false, true] {
            let (bleng, handle) = read_operation_fixture().await;
            let operation = bleng
                .begin_read_characteristic(
                    ConnectionId { value: 1 },
                    GattSessionId { value: 2 },
                    GattCharacteristicId { value: 30 },
                )
                .unwrap();
            complete_unclaimed_read(
                &bleng,
                &handle,
                &operation,
                failed.then(|| event_error(BleErrorCategory::Internal)),
            )
            .await;
            for _ in 0..2 {
                assert!(matches!(
                    operation.cancel().await,
                    Err(BlengError::OperationAlreadyCompleted { command_id })
                        if command_id == operation.operation.command_id
                ));
            }
            assert_unclaimed_read_result(&operation, failed).await;
            assert!(matches!(
                operation.cancel().await,
                Err(BlengError::OperationAlreadyCompleted { command_id })
                    if command_id == operation.operation.command_id
            ));

            bleng.shutdown().await.unwrap();
        }
    });
}

#[test]
fn read_result_slot_cancel_observes_completion_while_wait_owns_ticket() {
    let caller = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    caller.block_on(async {
        for failed in [false, true] {
            for after_result in [false, true] {
                let (bleng, handle) = read_operation_fixture().await;
                let operation = bleng
                    .begin_read_characteristic(
                        ConnectionId { value: 1 },
                        GattSessionId { value: 2 },
                        GattCharacteristicId { value: 30 },
                    )
                    .unwrap();
                complete_unclaimed_read(
                    &bleng,
                    &handle,
                    &operation,
                    failed.then(|| event_error(BleErrorCategory::Internal)),
                )
                .await;
                let (entered, release) = operation
                    .operation
                    .result_slot
                    .pause_wait_for_test(after_result);
                let waiting_operation = Arc::clone(&operation);
                let wait = tokio::spawn(async move { waiting_operation.wait(2_000).await });
                tokio::time::timeout(std::time::Duration::from_secs(2), entered)
                    .await
                    .expect("wait did not enter the selected ticket-owned window")
                    .unwrap();
                assert!(!wait.is_finished());
                for _ in 0..2 {
                    let cancelled = operation.cancel().await;
                    assert!(
                        matches!(
                            &cancelled,
                            Err(BlengError::OperationAlreadyCompleted { command_id })
                                if *command_id == operation.operation.command_id
                        ),
                        "after_result={after_result}, failed={failed}: {cancelled:?}"
                    );
                }
                release.send(()).unwrap();
                let result = wait.await.unwrap();
                if failed {
                    assert!(matches!(result, Err(BlengError::Operation { error })
                        if error == event_error(BleErrorCategory::Internal)));
                } else {
                    assert!(matches!(result, Ok(value) if value == vec![0xde, 0xad]));
                }
                assert!(matches!(
                    operation.wait(2_000).await,
                    Err(BlengError::OperationResultConsumed)
                ));
                bleng.shutdown().await.unwrap();
            }
        }
    });
}

#[test]
fn read_result_slot_fault_overrides_completed_success_and_failure() {
    let caller = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    caller.block_on(async {
        for failed in [false, true] {
            let (bleng, handle) = read_operation_fixture().await;
            let operation = bleng
                .begin_read_characteristic(
                    ConnectionId { value: 1 },
                    GattSessionId { value: 2 },
                    GattCharacteristicId { value: 30 },
                )
                .unwrap();
            complete_unclaimed_read(
                &bleng,
                &handle,
                &operation,
                failed.then(|| event_error(BleErrorCategory::Internal)),
            )
            .await;
            bleng.owner.fault_read_runtime_for_test();
            let result = operation.wait(2_000).await;
            assert!(
                matches!(result, Err(BlengError::TransportFault)),
                "fault must override retained actor settlement, failed={failed}: {result:?}"
            );
            assert!(matches!(
                operation.wait(2_000).await,
                Err(BlengError::OperationResultConsumed)
            ));
            bleng.shutdown().await.unwrap();
        }
    });
}

#[test]
fn read_result_slot_fault_overrides_ready_result_before_waiter_resumes() {
    use std::{
        future::{Future, poll_fn},
        task::Poll,
        time::Duration,
    };

    let caller = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    caller.block_on(async {
        for failed in [false, true] {
            let (bleng, handle) = read_operation_fixture().await;
            let operation = bleng
                .begin_read_characteristic(
                    ConnectionId { value: 1 },
                    GattSessionId { value: 2 },
                    GattCharacteristicId { value: 30 },
                )
                .unwrap();
            let mut wait = Box::pin(operation.operation.result_slot.wait(Duration::from_secs(2)));
            let initial = poll_fn(|cx| Poll::Ready(wait.as_mut().poll(cx))).await;
            assert!(
                initial.is_pending(),
                "wait must enter the result select before completion"
            );
            complete_unclaimed_read(
                &bleng,
                &handle,
                &operation,
                failed.then(|| event_error(BleErrorCategory::Internal)),
            )
            .await;
            bleng.owner.fault_read_runtime_for_test();
            assert_eq!(wait.await, Err(waiter::WaiterError::TransportFault));
            assert!(matches!(
                operation.wait(2_000).await,
                Err(BlengError::OperationResultConsumed)
            ));
            bleng.shutdown().await.unwrap();
        }
    });
}

#[test]
fn read_result_slot_shutdown_pending_or_partial_evidence_is_closed() {
    let caller = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    caller.block_on(async {
        for partial in [None, Some(true), Some(false)] {
            let (bleng, handle) = read_operation_fixture().await;
            let operation = bleng
                .begin_read_characteristic(
                    ConnectionId { value: 1 },
                    GattSessionId { value: 2 },
                    GattCharacteristicId { value: 30 },
                )
                .unwrap();
            if let Some(domain) = partial {
                let event = if domain {
                    BleEvent::CharacteristicRead {
                        command_id: operation.operation.command_id,
                        connection_id: ConnectionId { value: 1 },
                        gatt_session_id: GattSessionId { value: 2 },
                        characteristic_id: GattCharacteristicId { value: 30 },
                        value: vec![0xde, 0xad],
                    }
                } else {
                    BleEvent::CommandCompleted {
                        command_id: operation.operation.command_id,
                        error: None,
                    }
                };
                assert_eq!(handle.emit(event), blehub::EventIngressStatus::Delivered);
            }
            bleng
                .owner
                .read_settlement_barrier(operation.operation.result_slot.clone(), false)
                .await;
            bleng.shutdown().await.unwrap();
            assert!(matches!(
                operation.wait(2_000).await,
                Err(BlengError::Closed)
            ));
            assert!(matches!(
                operation.wait(2_000).await,
                Err(BlengError::OperationResultConsumed)
            ));
        }
    });
}

#[test]
fn timeout_boundary_rejects_only_zero() {
    assert!(matches!(
        crate::facade::validate_timeout(0),
        Err(BlengError::InvalidTimeout)
    ));
    assert!(crate::facade::validate_timeout(1).is_ok());
    assert!(crate::facade::validate_timeout(86_400_000).is_ok());
    assert!(matches!(
        crate::facade::validate_timeout(86_400_001),
        Err(BlengError::InvalidTimeout)
    ));
}

#[test]
fn private_runtime_timers_work_with_timerless_caller_runtimes() {
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
            // Neither caller enables timers. Event and typed waits must drive
            // their timers on BLeng's private runtime, not the caller's executor.
            let bleng = super::Bleng::new().unwrap();
            assert!(matches!(
                bleng.next_event(1).await,
                Err(BlengError::Timeout)
            ));
            let (backend, handle) = FakeBackend::scripted([]);
            bleng.assemble_backend(Arc::new(backend)).await.unwrap();
            let owner = Arc::clone(&bleng.owner);
            let release = bleng
                .owner
                .run_task(async move { owner.hold_actor_for_test().await })
                .await
                .unwrap();
            let operation = bleng.begin_initialize().unwrap();
            assert!(matches!(operation.wait(1).await, Err(BlengError::Timeout)));
            operation.cancel().await.unwrap();
            bleng.owner.request_close();
            release.notify_one();
            bleng.shutdown().await.unwrap();
            assert!(handle.commands().is_empty());
            drop(bleng);

            // Dropping the private runtime from an async caller must not panic
            // or stop the caller's runtime.
            assert_eq!(tokio::spawn(async { 42 }).await.unwrap(), 42);
        });
    }
}

#[test]
fn panicked_private_task_faults_owner_and_reports_worker_failure() {
    let caller = tokio::runtime::Builder::new_current_thread()
        .build()
        .unwrap();
    caller.block_on(async {
        let bleng = super::Bleng::new().unwrap();
        let (backend, handle) = FakeBackend::scripted([]);
        bleng.assemble_backend(Arc::new(backend)).await.unwrap();
        let task_result = bleng
            .owner
            .run_task(async { panic!("private task failed") })
            .await;
        assert!(matches!(task_result, Err(runtime::TaskFailure::Panicked)));
        assert!(matches!(
            task_result.map_err(BlengError::from),
            Err(BlengError::WorkerStopped)
        ));
        assert!(matches!(
            bleng.next_event(100).await,
            Err(BlengError::TransportFault)
        ));
        assert!(matches!(
            bleng.begin_initialize(),
            Err(BlengError::TransportFault)
        ));
        bleng.shutdown().await.unwrap();
        assert_eq!(handle.dispose_count(), 1);
    });
}

#[test]
fn private_task_panic_after_waiter_drop_still_faults_owner() {
    use std::{sync::mpsc, time::Duration};
    use tokio::sync::oneshot;

    let caller = tokio::runtime::Builder::new_current_thread()
        .build()
        .unwrap();
    caller.block_on(async {
        let bleng = super::Bleng::new().unwrap();
        let (started_tx, started_rx) = oneshot::channel();
        let (release_tx, release_rx) = mpsc::channel();
        let mut wait = Box::pin(bleng.owner.run_task(async move {
            started_tx.send(()).unwrap();
            // Hold this poll across caller drop so abort cannot hide a concurrent panic.
            release_rx.recv_timeout(Duration::from_secs(2)).unwrap();
            panic!("private task panicked after waiter retirement");
        }));
        tokio::select! {
            started = started_rx => started.unwrap(),
            outcome = &mut wait => panic!("task finished before waiter retirement: {outcome:?}"),
        }
        drop(wait);
        release_tx.send(()).unwrap();
        assert!(matches!(
            bleng.next_event(2000).await,
            Err(BlengError::TransportFault)
        ));
        assert!(matches!(
            bleng.begin_initialize(),
            Err(BlengError::TransportFault)
        ));
        bleng.shutdown().await.unwrap();
    });
}

#[test]
fn private_task_destructor_panic_after_completion_is_not_success() {
    let caller = tokio::runtime::Builder::new_current_thread()
        .build()
        .unwrap();
    caller.block_on(async {
        let bleng = super::Bleng::new().unwrap();
        let task_result = bleng
            .owner
            .run_task(async {
                let _guard = scopeguard::guard((), |_| panic!("private task drop failed"));
                42
            })
            .await;
        assert!(matches!(task_result, Err(runtime::TaskFailure::Panicked)));
        assert!(matches!(
            task_result.map_err(BlengError::from),
            Err(BlengError::WorkerStopped)
        ));
        assert!(matches!(
            bleng.begin_initialize(),
            Err(BlengError::TransportFault)
        ));
        bleng.shutdown().await.unwrap();
    });
}

#[test]
fn private_task_poll_panic_and_later_destructor_panic_do_not_abort_runtime() {
    use std::{
        future::Future,
        pin::Pin,
        task::{Context, Poll},
    };

    struct PanicTask;
    impl Future for PanicTask {
        type Output = ();

        fn poll(self: Pin<&mut Self>, _: &mut Context<'_>) -> Poll<Self::Output> {
            panic!("private task poll failed");
        }
    }
    impl Drop for PanicTask {
        fn drop(&mut self) {
            panic!("private task drop failed");
        }
    }

    let caller = tokio::runtime::Builder::new_current_thread()
        .build()
        .unwrap();
    caller.block_on(async {
        let bleng = super::Bleng::new().unwrap();
        let result: Result<(), BlengError> = bleng
            .owner
            .run_task(PanicTask)
            .await
            .map_err(BlengError::from);
        assert!(matches!(result, Err(BlengError::WorkerStopped)));
        assert!(matches!(
            bleng.begin_initialize(),
            Err(BlengError::TransportFault)
        ));
        bleng.shutdown().await.unwrap();
    });
}

#[test]
fn unpolled_private_task_destructor_panic_after_waiter_drop_faults_owner() {
    use std::{future::pending, sync::mpsc, task::Poll, time::Duration};

    let caller = tokio::runtime::Builder::new_current_thread()
        .build()
        .unwrap();
    caller.block_on(async {
        let bleng = super::Bleng::new().unwrap();
        let (backend, handle) = FakeBackend::scripted([]);
        bleng.assemble_backend(Arc::new(backend)).await.unwrap();
        let executor = bleng.owner.handle();
        let mut releases = Vec::new();
        let (entered_tx, entered_rx) = mpsc::channel();
        let mut workers = Vec::new();
        for _ in 0..executor.metrics().num_workers() {
            let entered = entered_tx.clone();
            let (release, released) = mpsc::channel::<()>();
            releases.push(release);
            workers.push(executor.spawn(async move {
                entered.send(()).unwrap();
                released.recv_timeout(Duration::from_secs(5))
            }));
        }
        for _ in &workers {
            entered_rx
                .recv_timeout(Duration::from_secs(2))
                .expect("private runtime worker did not reach the scheduling gate");
        }

        let guard = scopeguard::guard((), |_| panic!("unpolled task destructor failed"));
        let mut wait = Box::pin(bleng.owner.run_task(async move {
            let _guard = guard;
            pending::<()>().await;
        }));
        assert!(
            std::future::poll_fn(|cx| Poll::Ready(wait.as_mut().poll(cx)))
                .await
                .is_pending()
        );
        drop(wait);
        drop(releases);
        for worker in workers {
            assert!(matches!(
                worker.await.unwrap(),
                Err(mpsc::RecvTimeoutError::Disconnected)
            ));
        }
        assert!(matches!(
            bleng.next_event(2000).await,
            Err(BlengError::TransportFault)
        ));
        bleng.shutdown().await.unwrap();
        assert_eq!(handle.dispose_count(), 1);
    });
}

#[test]
fn private_task_destructor_panic_after_waiter_drop_faults_owner() {
    use std::future::pending;
    use tokio::sync::oneshot;

    let caller = tokio::runtime::Builder::new_current_thread()
        .build()
        .unwrap();
    caller.block_on(async {
        let bleng = super::Bleng::new().unwrap();
        let (backend, handle) = FakeBackend::scripted([]);
        bleng.assemble_backend(Arc::new(backend)).await.unwrap();
        let (started_tx, started_rx) = oneshot::channel();
        let mut wait = Box::pin(bleng.owner.run_task(async move {
            let _guard = scopeguard::guard((), |_| panic!("private future destructor failed"));
            started_tx.send(()).unwrap();
            pending::<()>().await;
        }));
        tokio::select! {
            started = started_rx => started.unwrap(),
            outcome = &mut wait => panic!("task completed before waiter drop: {outcome:?}"),
        }
        drop(wait);
        assert!(matches!(
            bleng.next_event(2000).await,
            Err(BlengError::TransportFault)
        ));
        assert!(matches!(
            bleng.begin_initialize(),
            Err(BlengError::TransportFault)
        ));
        bleng.shutdown().await.unwrap();
        assert_eq!(handle.dispose_count(), 1);
    });
}

#[test]
fn dropping_facade_waiter_aborts_private_task_and_releases_its_capture() {
    use std::{future::pending, time::Duration};
    use tokio::sync::oneshot;

    let caller = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .unwrap();
    caller.block_on(async {
        let bleng = super::Bleng::new().unwrap();
        let (started_tx, started_rx) = oneshot::channel();
        let (released_tx, released_rx) = oneshot::channel();
        let mut operation = Box::pin(bleng.owner.run_task(async move {
            let _release = scopeguard::guard(released_tx, |signal| {
                let _ = signal.send(());
            });
            started_tx.send(()).unwrap();
            pending::<()>().await;
        }));
        tokio::time::timeout(Duration::from_secs(2), async {
            tokio::select! {
                started = started_rx => started.expect("private task must start"),
                outcome = &mut operation => panic!("operation completed before cancellation: {outcome:?}"),
            }
        })
        .await
        .expect("private task must enter before dropping its facade waiter");

        drop(operation);
        tokio::time::timeout(Duration::from_secs(2), released_rx)
            .await
            .expect("facade cancellation must release the private task's capture")
            .unwrap();
        assert!(matches!(bleng.next_event(1).await, Err(BlengError::Timeout)));
        bleng.shutdown().await.unwrap();
    });
}

#[test]
fn event_reader_errors_preserve_timeout_and_transport_categories() {
    assert!(matches!(
        BlengError::from(runtime::EventReadError::Timeout),
        BlengError::Timeout
    ));
    assert!(matches!(
        BlengError::from(runtime::EventReadError::Fault),
        BlengError::TransportFault
    ));
}

#[test]
fn waiter_errors_preserve_canonical_and_local_categories() {
    assert!(matches!(
        BlengError::from(waiter::WaiterError::Operation(event_error(
            BleErrorCategory::NotSupported
        ))),
        BlengError::Operation { error } if error.category == BleErrorCategory::NotSupported
    ));
    assert!(matches!(
        BlengError::from(waiter::WaiterError::DispatchRejected(
            CommandDispatchError::InvalidState {
                detail: "not initialized".to_owned(),
            }
        )),
        BlengError::DispatchRejected {
            error: CommandDispatchError::InvalidState { .. }
        }
    ));
    assert!(matches!(
        BlengError::from(waiter::WaiterError::BackendNotAssembled),
        BlengError::NotReady {
            reason: BlengNotReadyReason::BackendNotAssembled
        }
    ));
    assert!(matches!(
        BlengError::from(waiter::WaiterError::NotInitialized),
        BlengError::NotReady {
            reason: BlengNotReadyReason::NotInitialized
        }
    ));
    assert!(matches!(
        BlengError::from(waiter::WaiterError::ConnectionNotActive),
        BlengError::NotReady {
            reason: BlengNotReadyReason::ConnectionNotActive
        }
    ));
    assert!(matches!(
        BlengError::from(waiter::WaiterError::SubscriptionNotActive),
        BlengError::NotReady {
            reason: BlengNotReadyReason::SubscriptionNotActive
        }
    ));
    assert!(matches!(
        BlengError::from(waiter::WaiterError::ScanBusy),
        BlengError::Busy {
            reason: BlengBusyReason::Scan
        }
    ));
    assert!(matches!(
        BlengError::from(waiter::WaiterError::CapacityExceeded),
        BlengError::ResourceExhausted {
            resource: BlengResource::PendingWaiters
        }
    ));
    assert!(matches!(
        BlengError::from(waiter::WaiterError::InvalidCommandId),
        BlengError::ContractFailure {
            reason: BlengContractFailure::InvalidCommandId
        }
    ));
}

#[test]
fn assembly_and_cleanup_errors_preserve_canonical_errors() {
    let canonical = blehub::BleHubError::BackendCleanup {
        detail: "dispose failed".to_owned(),
        platform_details: Some(PlatformErrorDetails {
            platform: Platform::Test,
            code: Some("E_TEST".to_owned()),
            name: None,
            message: Some("test cleanup".to_owned()),
        }),
    };
    assert!(matches!(
        BlengError::from(runtime::AssemblyError::Backend(canonical.clone())),
        BlengError::Assembly { error } if error == canonical
    ));
    assert!(matches!(
        BlengError::from(cleanup::CleanupError::Backend(canonical.clone())),
        BlengError::Cleanup { error } if error == canonical
    ));
    assert!(matches!(
        BlengError::from(runtime::AssemblyError::Admission(
            cleanup::AssemblyAdmissionError::AdditionalHubRejected
        )),
        BlengError::Busy {
            reason: BlengBusyReason::BackendSlotOccupied
        }
    ));
    assert!(matches!(
        BlengError::from(runtime::AssemblyError::Admission(
            cleanup::AssemblyAdmissionError::DuplicateOwnedHub
        )),
        BlengError::ContractFailure {
            reason: BlengContractFailure::DuplicateHubOwnership
        }
    ));
}
