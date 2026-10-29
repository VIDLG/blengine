use super::support::{
    Arc, BleHub, CancellationPolicy, Duration, EventIngressStatus, EventScenario, FakeBackend,
    GattCharacteristicId, PeripheralId, RuntimeOwner, WaiterError, connect_scenario, read_command,
    read_discovery, read_terminal, read_value,
};

#[test]
fn native_cancellation_rejection_does_not_cancel_the_original_waiter() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let (backend, handle) = FakeBackend::scripted_with_cancellation(
        [
            connect_scenario(1, 1, 10),
            read_discovery(2),
            EventScenario::accept(read_command(3)),
        ],
        CancellationPolicy::Reject,
    );
    owner
        .attach_hub(BleHub::new(Arc::new(backend), owner.event_sink()).unwrap())
        .unwrap();
    let host = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();

    host.block_on(async {
        let connection = owner
            .connect(PeripheralId { value: 10 }, Duration::from_secs(2))
            .await
            .unwrap();
        let session = owner
            .discover_gatt_session(connection, Duration::from_secs(2))
            .await
            .unwrap();
        let mut read = Box::pin(owner.read_characteristic(
            connection,
            session,
            GattCharacteristicId { value: 30 },
            Duration::from_secs(2),
        ));

        wait_until_read_is_accepted(&handle, &mut read).await;
        assert_eq!(
            owner.cancel_operation(3.into()).await,
            Err(WaiterError::OperationNotCancellable(3.into()))
        );
        assert_eq!(handle.commands().len(), 3);

        assert_eq!(
            handle.emit(read_value(3, &[0xde, 0xad])),
            EventIngressStatus::Delivered
        );
        assert_eq!(
            handle.emit(read_terminal(3, None)),
            EventIngressStatus::Delivered
        );
        assert_eq!(read.await, Ok(vec![0xde, 0xad]));
        owner.close().await.unwrap();
    });
}

#[test]
fn waiting_and_cancelling_share_control_without_consuming_the_result_slot() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let (backend, handle) = FakeBackend::scripted_with_cancellation(
        [
            connect_scenario(1, 1, 10),
            read_discovery(2),
            EventScenario::accept(read_command(3)),
        ],
        CancellationPolicy::Accept,
    );
    owner
        .attach_hub(BleHub::new(Arc::new(backend), owner.event_sink()).unwrap())
        .unwrap();
    let host = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();

    host.block_on(async {
        let connection = owner
            .connect(PeripheralId { value: 10 }, Duration::from_secs(2))
            .await
            .unwrap();
        let session = owner
            .discover_gatt_session(connection, Duration::from_secs(2))
            .await
            .unwrap();
        let ticket = owner
            .begin_operation(|submission| crate::runtime::messages::BeginRead {
                submission,
                connection,
                session,
                characteristic: GattCharacteristicId { value: 30 },
            })
            .unwrap();
        let (command_id, control, result_slot) = ticket.into_parts();
        let wait = tokio::spawn(async move { result_slot.wait(Duration::from_secs(2)).await });

        wait_until_command_is_accepted(&handle).await;
        tokio::task::yield_now().await;
        assert!(
            !wait.is_finished(),
            "the result waiter must still be pending"
        );
        assert_eq!(control.cancel(command_id).await, Ok(()));
        assert_eq!(wait.await.unwrap(), Err(WaiterError::Cancelled));
        owner.close().await.unwrap();
    });
}

#[test]
fn accepted_native_cancellation_finishes_waiter_but_keeps_late_native_evidence_safe() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let (backend, handle) = FakeBackend::scripted_with_cancellation(
        [
            connect_scenario(1, 1, 10),
            read_discovery(2),
            EventScenario::accept(read_command(3)),
        ],
        CancellationPolicy::Accept,
    );
    owner
        .attach_hub(BleHub::new(Arc::new(backend), owner.event_sink()).unwrap())
        .unwrap();
    let host = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();

    host.block_on(async {
        let connection = owner
            .connect(PeripheralId { value: 10 }, Duration::from_secs(2))
            .await
            .unwrap();
        let session = owner
            .discover_gatt_session(connection, Duration::from_secs(2))
            .await
            .unwrap();
        let mut read = Box::pin(owner.read_characteristic(
            connection,
            session,
            GattCharacteristicId { value: 30 },
            Duration::from_secs(2),
        ));

        wait_until_read_is_accepted(&handle, &mut read).await;
        assert_eq!(owner.cancel_operation(3.into()).await, Ok(()));
        assert_eq!(read.await, Err(WaiterError::Cancelled));

        // The backend may still report the original operation's late evidence.
        // It must not revive the cancelled caller waiter or fault the owner.
        assert_eq!(
            handle.emit(read_value(3, &[0xca, 0xfe])),
            EventIngressStatus::Delivered
        );
        assert_eq!(
            handle.emit(read_terminal(3, None)),
            EventIngressStatus::Delivered
        );
        owner.close().await.unwrap();
    });
}

#[test]
fn completed_close_pending_cancel_preserves_success() {
    completed_close_with_pending_control(false, false, &[true]);
}

#[test]
fn completed_close_pending_cancel_preserves_typed_failure() {
    completed_close_with_pending_control(false, true, &[true]);
}

#[test]
fn completed_close_pending_cancel_unclaimed_success_survives_actor_teardown() {
    completed_close_with_pending_control(false, false, &[false]);
}

#[test]
fn completed_close_pending_cancel_unclaimed_failure_survives_actor_teardown() {
    completed_close_with_pending_control(false, true, &[false]);
}

#[test]
fn completed_close_pending_cancel_fault_still_overrides_original_result() {
    for failed in [false, true] {
        completed_close_with_pending_control(true, failed, &[true, false]);
    }
}

fn completed_close_with_pending_control(faulted: bool, failed: bool, waiting_states: &[bool]) {
    use super::support::{
        BackendCapabilities, BackendEventIngressHandle, BleBackend, BleErrorCategory,
        BleEventError, BleHubError, CancelOperationRequest, CommandDispatchError, ConnectCommand,
        DisconnectCommand, DiscoverGattCommand, HoldActor, InitializeCommand, Mutex, Notify,
        ReadCharacteristicCommand, ReadRssiCommand, SetNotificationsCommand, StartScanCommand,
        StopScanCommand, WriteCharacteristicCommand, mpsc,
    };
    use crate::runtime::messages::BeginRead;
    use std::{
        future::{Future, poll_fn},
        task::Poll,
    };

    struct GatedCancellation {
        inner: FakeBackend,
        entered: Arc<Notify>,
        release: Mutex<mpsc::Receiver<()>>,
    }

    impl BleBackend for GatedCancellation {
        delegate::delegate! {
            to self.inner {
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
            self.entered.notify_one();
            self.release
                .lock()
                .unwrap()
                .recv_timeout(Duration::from_secs(5))
                .expect("test must release the native cancellation call");
            self.inner.cancel_operation(request)
        }
    }

    for &waiting_before_close in waiting_states {
        let owner = RuntimeOwner::new_assuming_initialized().unwrap();
        let (backend, handle) = FakeBackend::scripted_with_cancellation(
            [
                connect_scenario(1, 1, 10),
                read_discovery(2),
                EventScenario::accept(read_command(3)),
            ],
            CancellationPolicy::Reject,
        );
        let entered = Arc::new(Notify::new());
        let (release_tx, release_rx) = mpsc::channel();
        let mut native_release = Some(scopeguard::guard(release_tx, |tx| {
            let _ = tx.send(());
        }));
        let host = tokio::runtime::Builder::new_multi_thread()
            .enable_time()
            .build()
            .unwrap();
        host.block_on(async {
            owner
                .assemble_backend(Arc::new(GatedCancellation {
                    inner: backend,
                    entered: Arc::clone(&entered),
                    release: Mutex::new(release_rx),
                }))
                .await
                .unwrap();
            let connection = owner
                .connect(10.into(), Duration::from_secs(2))
                .await
                .unwrap();
            let session = owner
                .discover_gatt_session(connection, Duration::from_secs(2))
                .await
                .unwrap();
            let pending = owner
                .begin_operation(|submission| BeginRead {
                    submission,
                    connection,
                    session,
                    characteristic: 30.into(),
                })
                .unwrap();
            let (id, control, slot) = pending.into_parts();
            tokio::time::timeout(Duration::from_secs(2), async {
                while handle.commands().len() < 3 {
                    tokio::task::yield_now().await;
                }
            })
            .await
            .unwrap();
            let mut cancel = Box::pin(control.cancel(id));
            tokio::time::timeout(Duration::from_secs(2), async {
                tokio::select! {
                    _ = entered.notified() => {},
                    result = &mut cancel => panic!("control returned before release: {result:?}"),
                }
            })
            .await
            .unwrap();
            let error = failed.then(|| BleEventError {
                category: BleErrorCategory::Internal,
                message: "completed read before shutdown and cancellation receipt".into(),
                platform_details: None,
            });
            assert_eq!(
                handle.emit(read_value(3, &[0, 255, 128])),
                EventIngressStatus::Delivered
            );
            assert_eq!(
                handle.emit(read_terminal(3, error.clone())),
                EventIngressStatus::Delivered
            );
            owner.read_settlement_barrier(slot.clone(), true).await;
            assert!(
                poll_fn(|cx| Poll::Ready(cancel.as_mut().poll(cx)))
                    .await
                    .is_pending()
            );
            let mut wait = Box::pin(slot.wait(Duration::from_secs(2)));
            if waiting_before_close {
                assert!(
                    poll_fn(|cx| Poll::Ready(wait.as_mut().poll(cx)))
                        .await
                        .is_pending()
                );
            }
            // Separate the lifecycle wake from actor result publication. This
            // catches returning Closed early even if close() later sends the result.
            let actor_entered = Arc::new(Notify::new());
            let actor_release = Arc::new(Notify::new());
            let release_actor =
                scopeguard::guard(Arc::clone(&actor_release), |release| release.notify_one());
            owner
                .actor
                .tell(HoldActor {
                    entered: Arc::clone(&actor_entered),
                    release: actor_release,
                })
                .try_send()
                .unwrap();
            tokio::time::timeout(Duration::from_secs(2), actor_entered.notified())
                .await
                .unwrap();
            if faulted {
                owner.lifecycle.fault();
            } else {
                owner.request_close();
            }
            let expected_control = Err(if faulted {
                WaiterError::TransportFault
            } else {
                WaiterError::Closed
            });
            if waiting_before_close {
                let control_result = tokio::time::timeout(Duration::from_secs(2), &mut cancel)
                    .await
                    .unwrap();
                assert_eq!(control_result, expected_control);
            }
            if !faulted && waiting_before_close {
                assert!(
                    poll_fn(|cx| Poll::Ready(wait.as_mut().poll(cx)))
                        .await
                        .is_pending(),
                    "completed result must not become Closed before actor teardown releases it"
                );
            }
            drop(release_actor);
            if !waiting_before_close {
                // Keep the native receipt unresolved until no actor handler can
                // process it; only then release the call so dispose can complete.
                tokio::time::timeout(Duration::from_secs(2), owner.wait_for_actor_stop_for_test())
                    .await
                    .expect("actor must stop before the native cancellation is released");
                drop(native_release.take());
                tokio::time::timeout(Duration::from_secs(2), owner.close())
                    .await
                    .unwrap()
                    .unwrap();
                // Both lifecycle change and receipt-sender closure are ready;
                // the pending control must still report the lifecycle reason.
                assert_eq!(
                    tokio::time::timeout(Duration::from_secs(2), &mut cancel)
                        .await
                        .unwrap(),
                    expected_control
                );
            }
            let result = tokio::time::timeout(Duration::from_secs(2), &mut wait)
                .await
                .unwrap();
            let expected = if faulted {
                Err(WaiterError::TransportFault)
            } else if let Some(error) = error {
                Err(WaiterError::Operation(error))
            } else {
                Ok(vec![0, 255, 128])
            };
            assert_eq!(
                result, expected,
                "failed={failed}, waiting_before_close={waiting_before_close}"
            );
            assert_eq!(
                slot.wait(Duration::from_secs(2)).await,
                Err(WaiterError::OperationResultConsumed)
            );
            assert_eq!(
                control.cancel(id).await,
                Err(if faulted {
                    WaiterError::TransportFault
                } else {
                    WaiterError::Closed
                })
            );
            drop(native_release.take());
            tokio::time::timeout(Duration::from_secs(2), owner.close())
                .await
                .unwrap()
                .unwrap();
            assert_eq!(handle.dispose_count(), 1);
            assert_eq!(handle.emit(read_value(3, &[9])), EventIngressStatus::Closed);
        });
    }
}

async fn wait_until_read_is_accepted(
    handle: &blehub_testkit::FakeBackendHandle,
    read: &mut std::pin::Pin<Box<impl std::future::Future<Output = Result<Vec<u8>, WaiterError>>>>,
) {
    tokio::time::timeout(Duration::from_secs(1), async {
        loop {
            tokio::select! {
                result = read.as_mut() => panic!("read completed before cancellation: {result:?}"),
                _ = tokio::task::yield_now() => {}
            }
            if handle.commands().len() == 3 {
                break;
            }
        }
    })
    .await
    .expect("read dispatch did not become accepted");
}

async fn wait_until_command_is_accepted(handle: &blehub_testkit::FakeBackendHandle) {
    tokio::time::timeout(Duration::from_secs(1), async {
        while handle.commands().len() != 3 {
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("read dispatch did not become accepted");
}
