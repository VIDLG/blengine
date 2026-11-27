use std::{
    collections::BTreeSet,
    future::{Future, poll_fn},
    mem::size_of,
    pin::Pin,
    sync::{
        Arc, Mutex, Weak,
        atomic::{AtomicBool, Ordering},
    },
    task::Poll,
    time::Duration,
};

use blehub::{
    AdapterSnapshot, AdapterState, AdvertisementObservation, AuthorizationState, BleBackend,
    BleEvent, BluetoothUuid, CommandId, EventIngressStatus, InitializeCommand, ManufacturerData,
    ScanOptions, ScanSessionId, ScanStopReason, ScanStopResult, ServiceData, StartScanCommand,
    StopScanCommand,
};
use blehub_testkit::{
    BackendContractViolation, EventScenario, FakeBackend, FakeBackendHandle, RecordedCommand,
};
use bleng::{Bleng, BlengBusyReason, BlengError};
use bleng_supervisor::{
    MAX_SCAN_ADVERTISEMENTS, MAX_SCAN_DATA_BYTES, OwnerBackendIdentity, OwnerGeneration,
    PlatformActionResult, PlatformOwnerSession, PlatformOwnerSessionFactory,
    PlatformRecoveryAction, RecoveryAttemptId, RecoveryContext, RecoveryLink, RecoveryObservation,
    RecoveryPolicyConfig, RecoverySignal, ResourceSnapshot, ScanDelivery, ScanDeliveryError,
    ScanEvent, ScanSubmissionError, SessionObservationProjection, SessionSupervisor,
    SupervisorConfig, SupervisorError, SupervisorOperationError,
};
use tokio::{
    sync::{mpsc, oneshot},
    task::JoinHandle,
};

type PlatformFuture<'a, T> = Pin<Box<dyn Future<Output = Result<T, SupervisorError>> + Send + 'a>>;
type ReaderTask = JoinHandle<Result<(), SupervisorError>>;

fn run(test: impl Future<Output = ()>) {
    tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .unwrap()
        .block_on(async {
            tokio::time::timeout(Duration::from_secs(5), test)
                .await
                .expect("scan delivery integration test exceeded its deadline");
        });
}

fn context() -> RecoveryContext {
    RecoveryContext::new(
        OwnerBackendIdentity::new("scan-owner", "scan-backend").unwrap(),
        OwnerGeneration::new(1).unwrap(),
        RecoveryAttemptId::new(1).unwrap(),
    )
}

struct FakeSession {
    context: RecoveryContext,
    backend: Arc<dyn BleBackend>,
    receipts: mpsc::UnboundedSender<BleEvent>,
    reader: Arc<Mutex<Option<ReaderTask>>>,
    owner: Arc<Mutex<Weak<Bleng>>>,
    joined: Arc<AtomicBool>,
}

impl PlatformOwnerSession for FakeSession {
    fn context(&self) -> &RecoveryContext {
        &self.context
    }

    fn backend(&self) -> Arc<dyn BleBackend> {
        Arc::clone(&self.backend)
    }

    fn dispose_unassembled(&self) -> Result<(), SupervisorError> {
        self.backend
            .dispose()
            .map_err(|error| SupervisorError::Platform {
                detail: error.to_string(),
            })
    }

    fn start_event_reader(
        &self,
        owner: Arc<Bleng>,
        projection: Arc<SessionObservationProjection>,
    ) -> Result<(), SupervisorError> {
        let mut reader = self.reader.lock().unwrap();
        assert!(reader.is_none(), "one public-stream reader per owner");
        *self.owner.lock().unwrap() = Arc::downgrade(&owner);
        let receipts = self.receipts.clone();
        let source = self.context.clone();
        *reader = Some(tokio::spawn(async move {
            loop {
                match owner.next_event(5000).await {
                    Ok(Some(event)) => {
                        // The receipt follows the real public-stream read AND projection.
                        if let Some(envelope) = projection.project(event.clone()) {
                            assert_eq!(envelope.source_context(), &source);
                        }
                        let _ = receipts.send(event);
                    }
                    Ok(None) | Err(BlengError::Closed) => return Ok(()),
                    Err(error) => {
                        return Err(SupervisorError::Platform {
                            detail: format!("public event reader failed: {error}"),
                        });
                    }
                }
            }
        }));
        Ok(())
    }

    fn wait_event_reader(&self) -> PlatformFuture<'_, ()> {
        Box::pin(async move {
            let reader = self.reader.lock().unwrap().take().expect("reader started");
            // Owner shutdown closes the stream; this is not a FIFO flush or an abort.
            reader.await.map_err(|error| SupervisorError::Platform {
                detail: format!("public event reader task failed: {error}"),
            })??;
            self.joined.store(true, Ordering::Release);
            Ok(())
        })
    }

    fn resource_snapshot(&self) -> PlatformFuture<'_, ResourceSnapshot> {
        Box::pin(async move {
            assert!(self.joined.load(Ordering::Acquire));
            Ok([("callbacks".to_owned(), 0)].into_iter().collect())
        })
    }

    fn execute_recovery_action(
        &self,
        _: u64,
        action: PlatformRecoveryAction,
    ) -> PlatformFuture<'_, PlatformActionResult> {
        Box::pin(async move {
            Err(SupervisorError::Platform {
                detail: format!("unexpected platform action: {action:?}"),
            })
        })
    }
}

struct FakeFactory(Mutex<Option<FakeSession>>);

impl PlatformOwnerSessionFactory for FakeFactory {
    fn required_zero_resource_names(&self) -> BTreeSet<String> {
        ["callbacks".to_owned()].into_iter().collect()
    }

    fn create(
        &self,
        requested: &RecoveryContext,
    ) -> Result<Box<dyn PlatformOwnerSession>, SupervisorError> {
        let session = self.0.lock().unwrap().take().expect("one owner requested");
        assert_eq!(requested, session.context());
        Ok(Box::new(session))
    }
}

struct Fixture {
    supervisor: SessionSupervisor,
    backend: FakeBackendHandle,
    receipts: mpsc::UnboundedReceiver<BleEvent>,
    reader: Arc<Mutex<Option<ReaderTask>>>,
    owner: Arc<Mutex<Weak<Bleng>>>,
    joined: Arc<AtomicBool>,
}

impl Fixture {
    fn new(operations: impl IntoIterator<Item = EventScenario>) -> Self {
        let initialize = EventScenario::emit_before_return(
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
                completed(1),
            ],
        );
        let (backend, handle) = FakeBackend::scripted([initialize].into_iter().chain(operations));
        let (receipts, receiver) = mpsc::unbounded_channel();
        let joined = Arc::new(AtomicBool::new(false));
        let reader = Arc::new(Mutex::new(None));
        let owner = Arc::new(Mutex::new(Weak::new()));
        let session = FakeSession {
            context: context(),
            backend: Arc::new(backend),
            receipts,
            reader: Arc::clone(&reader),
            owner: Arc::clone(&owner),
            joined: Arc::clone(&joined),
        };
        let mut supervisor = SessionSupervisor::new(
            Arc::new(FakeFactory(Mutex::new(Some(session)))),
            SupervisorConfig {
                recovery: RecoveryPolicyConfig {
                    initial_context: context(),
                    old_link: None,
                    expected_terminals: BTreeSet::new(),
                    allow_central_recreation: true,
                    allow_fresh_owner: true,
                },
                recovery_target: None,
            },
        )
        .unwrap();
        supervisor
            .configure_old_operation_barrier(None, BTreeSet::new())
            .unwrap();
        Self {
            supervisor,
            backend: handle,
            receipts: receiver,
            reader,
            owner,
            joined,
        }
    }

    async fn observed(&mut self, expected: BleEvent) {
        loop {
            let event = self.receipts.recv().await.expect("reader closed early");
            if event == expected {
                return;
            }
        }
    }

    async fn emit_and_observe(&mut self, event: BleEvent) {
        assert_eq!(
            self.backend.emit(event.clone()),
            EventIngressStatus::Delivered
        );
        self.observed(event).await;
    }

    async fn shutdown(&mut self) {
        self.shutdown_with_violations(&[]).await;
    }

    async fn shutdown_with_violations(&mut self, expected: &[BackendContractViolation]) {
        self.supervisor.shutdown().await.unwrap();
        assert!(self.joined.load(Ordering::Acquire));
        assert_eq!(self.backend.dispose_count(), 1);
        assert_eq!(self.backend.contract_violations(), expected);
    }
}

fn start(command: u64, scan: u64, events: Vec<BleEvent>) -> EventScenario {
    EventScenario::emit_before_return(
        RecordedCommand::StartScan(StartScanCommand {
            command_id: command.into(),
            scan_session_id: scan.into(),
            options: ScanOptions::default(),
        }),
        events,
    )
}

fn started(command: u64, scan: u64) -> BleEvent {
    BleEvent::ScanStarted {
        command_id: command.into(),
        scan_id: scan.into(),
    }
}

fn completed(command: u64) -> BleEvent {
    BleEvent::CommandCompleted {
        command_id: command.into(),
        error: None,
    }
}

fn stopped(command: u64, scan: u64) -> BleEvent {
    BleEvent::ScanStopped {
        scan_id: scan.into(),
        result: ScanStopResult::Ok {
            reason: ScanStopReason::Requested {
                command_id: command.into(),
            },
        },
    }
}

fn stop(command: u64, scan: u64) -> EventScenario {
    EventScenario::emit_before_return(
        RecordedCommand::StopScan(StopScanCommand {
            command_id: command.into(),
            scan_session_id: scan.into(),
        }),
        vec![stopped(command, scan), completed(command)],
    )
}

fn advertisement(scan: u64, marker: u8, bytes: usize) -> AdvertisementObservation {
    AdvertisementObservation {
        scan_session_id: scan.into(),
        peripheral_id: 17.into(),
        local_name: None,
        advertised_services: vec![],
        manufacturer_data: vec![ManufacturerData {
            company_id: 0x004c,
            payload: vec![marker; bytes],
        }],
        service_data: vec![],
        rssi: Some(-127),
        connectable: Some(false),
    }
}

fn observed_ad(advertisement: AdvertisementObservation) -> BleEvent {
    BleEvent::AdvertisementObserved { advertisement }
}

fn delivered_stop(command: u64, scan: u64) -> ScanEvent {
    let BleEvent::ScanStopped { scan_id, result } = stopped(command, scan) else {
        unreachable!()
    };
    ScanEvent::Stopped { scan_id, result }
}

fn assert_empty(delivery: &ScanDelivery) {
    let snapshot = delivery.snapshot();
    assert_eq!(snapshot.queued_advertisements, 0);
    assert_eq!(snapshot.queued_bytes, 0);
    assert!(!snapshot.pull_pending);
}

#[test]
fn callback_before_return_preserves_payload_and_terminal_then_isolates_the_next_scan() {
    run(async {
        let mut first = advertisement(1, 255, 3);
        let uuid = BluetoothUuid::canonicalize("180d").unwrap();
        let first_bytes = "é🦀\0peer".len()
            + 2 * (size_of::<BluetoothUuid>() + uuid.value.len())
            + 2 * size_of::<ManufacturerData>()
            + 6
            + size_of::<ServiceData>()
            + uuid.value.len()
            + 3;
        first.local_name = Some("é🦀\0peer".to_owned());
        first.advertised_services = vec![uuid.clone(), uuid.clone()];
        first.manufacturer_data.push(ManufacturerData {
            company_id: u16::MAX,
            payload: vec![0, 128, 255],
        });
        first.service_data = vec![ServiceData {
            service_uuid: uuid,
            payload: vec![0, 255, 128],
        }];
        let second = advertisement(2, 42, 2);
        let mut fixture = Fixture::new([
            start(
                2,
                1,
                vec![observed_ad(first.clone()), started(2, 1), completed(2)],
            ),
            stop(3, 1),
            start(4, 2, vec![started(4, 2), completed(4)]),
            stop(5, 2),
        ]);
        fixture.supervisor.start().await.unwrap();
        let old = fixture
            .supervisor
            .begin_scan_view(&context(), ScanOptions::default())
            .unwrap();
        assert_eq!(old.operation.command_id(), CommandId::from(2));
        assert_eq!(
            old.operation.wait(2000).await.unwrap(),
            ScanSessionId::from(1)
        );
        fixture.observed(completed(2)).await;
        assert_eq!(old.delivery.snapshot().queued_advertisements, 1);
        assert_eq!(old.delivery.snapshot().queued_bytes, first_bytes);
        let stop = fixture
            .supervisor
            .begin_stop_scan(&context(), 1.into())
            .unwrap();
        assert_eq!(stop.command_id(), CommandId::from(3));
        stop.wait(2000).await.unwrap();
        fixture.observed(completed(3)).await;
        assert_eq!(
            old.delivery.next_event().await.unwrap(),
            ScanEvent::Advertisement {
                advertisement: first
            }
        );
        assert_eq!(
            old.delivery.next_event().await.unwrap(),
            delivered_stop(3, 1)
        );

        let new = fixture
            .supervisor
            .begin_scan_view(&context(), ScanOptions::default())
            .unwrap();
        assert_eq!(new.operation.command_id(), CommandId::from(4));
        assert_eq!(
            new.operation.wait(2000).await.unwrap(),
            ScanSessionId::from(2)
        );
        fixture.observed(completed(4)).await;
        for stale in [
            observed_ad(advertisement(1, 99, 1)),
            started(2, 1),
            stopped(3, 1),
        ] {
            fixture.emit_and_observe(stale).await;
        }
        assert_empty(&new.delivery);
        fixture.emit_and_observe(observed_ad(second.clone())).await;
        assert_eq!(
            new.delivery.next_event().await.unwrap(),
            ScanEvent::Advertisement {
                advertisement: second
            }
        );
        assert_eq!(
            old.delivery.next_event().await.unwrap(),
            delivered_stop(3, 1)
        );
        fixture
            .supervisor
            .begin_stop_scan(&context(), 2.into())
            .unwrap()
            .wait(2000)
            .await
            .unwrap();
        fixture.observed(completed(5)).await;
        assert_eq!(
            new.delivery.next_event().await.unwrap(),
            delivered_stop(5, 2)
        );
        fixture
            .shutdown_with_violations(&[
                BackendContractViolation::EventAfterTerminalAttempt {
                    command_id: 2.into(),
                },
                BackendContractViolation::EventAfterTerminalAttempt {
                    command_id: 3.into(),
                },
            ])
            .await;
    });
}

#[test]
fn original_start_handle_keeps_independent_cancel_and_single_consumer_wait() {
    run(async {
        let mut fixture = Fixture::new([start(2, 1, vec![started(2, 1)]), stop(3, 1)]);
        fixture.supervisor.start().await.unwrap();
        let handoff = fixture
            .supervisor
            .begin_scan_view(&context(), ScanOptions::default())
            .unwrap();
        fixture.observed(started(2, 1)).await;
        assert!(matches!(handoff.operation.cancel().await,
            Err(BlengError::OperationNotCancellable { command_id }) if command_id == CommandId::from(2)));
        assert!(matches!(
            fixture
                .supervisor
                .begin_scan_view(&context(), ScanOptions::default()),
            Err(ScanSubmissionError::DeliveryBusy)
        ));
        // Neither the independent control result nor detaching consumes start's result.
        handoff.delivery.detach();
        fixture.emit_and_observe(completed(2)).await;
        assert_eq!(
            handoff.operation.wait(2000).await.unwrap(),
            ScanSessionId::from(1)
        );
        assert!(matches!(
            handoff.operation.wait(2000).await,
            Err(BlengError::OperationResultConsumed)
        ));
        assert_eq!(
            handoff.delivery.next_event().await,
            Err(ScanDeliveryError::Detached)
        );
        assert_eq!(fixture.backend.commands().len(), 2);
        let stop = fixture
            .supervisor
            .begin_stop_scan(&context(), 1.into())
            .unwrap();
        assert_eq!(stop.command_id(), CommandId::from(3));
        stop.wait(2000).await.unwrap();
        fixture.observed(completed(3)).await;
        fixture.shutdown().await;
    });
}

#[test]
fn detach_releases_delivery_slot_but_does_not_bypass_bleng_scan_admission() {
    run(async {
        let mut fixture =
            Fixture::new([start(2, 1, vec![started(2, 1), completed(2)]), stop(4, 1)]);
        fixture.supervisor.start().await.unwrap();
        let old = fixture
            .supervisor
            .begin_scan_view(&context(), ScanOptions::default())
            .unwrap();
        old.operation.wait(2000).await.unwrap();
        fixture.observed(completed(2)).await;
        old.delivery.detach();
        let rejected = fixture
            .supervisor
            .begin_scan_view(&context(), ScanOptions::default())
            .unwrap();
        assert_eq!(rejected.operation.command_id(), CommandId::from(3));
        assert!(matches!(
            rejected.operation.wait(2000).await,
            Err(BlengError::Busy {
                reason: BlengBusyReason::Scan
            })
        ));
        assert_eq!(fixture.backend.commands().len(), 2);
        assert_eq!(
            rejected.delivery.next_event().await,
            Err(ScanDeliveryError::StartFailed)
        );
        fixture
            .emit_and_observe(observed_ad(advertisement(1, 7, 1)))
            .await;
        // Admission failure retires observation without waiting for a scan event or
        // consuming the old scan's continuing advertisements.
        assert_empty(&rejected.delivery);
        assert_eq!(
            rejected.delivery.next_event().await,
            Err(ScanDeliveryError::StartFailed)
        );
        assert_eq!(
            old.delivery.next_event().await,
            Err(ScanDeliveryError::Detached)
        );
        let stop = fixture
            .supervisor
            .begin_stop_scan(&context(), 1.into())
            .unwrap();
        assert_eq!(stop.command_id(), CommandId::from(4));
        stop.wait(2000).await.unwrap();
        fixture.observed(completed(4)).await;
        assert_eq!(
            rejected.delivery.next_event().await,
            Err(ScanDeliveryError::StartFailed)
        );
        assert_empty(&rejected.delivery);
        fixture.shutdown().await;
    });
}

#[test]
fn overflow_is_delivery_local_and_keeps_reader_and_explicit_native_stop_alive() {
    run(async {
        assert_eq!(MAX_SCAN_ADVERTISEMENTS, 64);
        assert_eq!(MAX_SCAN_DATA_BYTES, 256 * 1024);
        for (count, bytes, expected) in [
            (
                MAX_SCAN_ADVERTISEMENTS,
                1,
                ScanDeliveryError::AdvertisementOverflow,
            ),
            (
                1,
                MAX_SCAN_DATA_BYTES - size_of::<ManufacturerData>(),
                ScanDeliveryError::ByteOverflow,
            ),
        ] {
            let mut fixture = Fixture::new([
                start(2, 1, vec![started(2, 1), completed(2)]),
                stop(3, 1),
                start(4, 2, vec![started(4, 2), completed(4)]),
                stop(5, 2),
            ]);
            fixture.supervisor.start().await.unwrap();
            let handoff = fixture
                .supervisor
                .begin_scan_view(&context(), ScanOptions::default())
                .unwrap();
            handoff.operation.wait(2000).await.unwrap();
            fixture.observed(completed(2)).await;
            for _ in 0..count {
                fixture
                    .emit_and_observe(observed_ad(advertisement(1, 7, bytes)))
                    .await;
            }
            assert_eq!(handoff.delivery.snapshot().queued_advertisements, count);
            assert_eq!(
                handoff.delivery.snapshot().queued_bytes,
                count * (size_of::<ManufacturerData>() + bytes)
            );
            fixture
                .emit_and_observe(observed_ad(advertisement(1, 8, 1)))
                .await;
            assert_eq!(handoff.delivery.next_event().await, Err(expected));
            assert_empty(&handoff.delivery);
            assert!(matches!(
                fixture
                    .supervisor
                    .begin_scan_view(&context(), ScanOptions::default()),
                Err(ScanSubmissionError::DeliveryBusy)
            ));
            assert_eq!(
                fixture.backend.commands().len(),
                2,
                "overflow must not submit stop"
            );
            // Receipt after failure proves the same reader is still projecting public events.
            fixture
                .emit_and_observe(observed_ad(advertisement(1, 9, 1)))
                .await;
            let stop = fixture
                .supervisor
                .begin_stop_scan(&context(), 1.into())
                .unwrap();
            assert_eq!(stop.command_id(), CommandId::from(3));
            stop.wait(2000).await.unwrap();
            fixture.observed(completed(3)).await;
            assert_eq!(handoff.delivery.next_event().await, Err(expected));
            assert!(matches!(
                fixture
                    .supervisor
                    .begin_scan_view(&context(), ScanOptions::default()),
                Err(ScanSubmissionError::DeliveryBusy)
            ));
            handoff.delivery.detach();
            assert_eq!(handoff.delivery.next_event().await, Err(expected));
            assert_empty(&handoff.delivery);
            let next = fixture
                .supervisor
                .begin_scan_view(&context(), ScanOptions::default())
                .expect("detached overflow must release the attached delivery slot");
            assert_eq!(next.operation.command_id(), CommandId::from(4));
            assert_eq!(
                next.operation.wait(2000).await.unwrap(),
                ScanSessionId::from(2)
            );
            fixture.observed(completed(4)).await;
            let next_advertisement = advertisement(2, 42, 1);
            fixture
                .emit_and_observe(observed_ad(next_advertisement.clone()))
                .await;
            assert_eq!(
                next.delivery.next_event().await.unwrap(),
                ScanEvent::Advertisement {
                    advertisement: next_advertisement,
                }
            );
            let stop = fixture
                .supervisor
                .begin_stop_scan(&context(), 2.into())
                .unwrap();
            assert_eq!(stop.command_id(), CommandId::from(5));
            stop.wait(2000).await.unwrap();
            fixture.observed(completed(5)).await;
            assert_eq!(
                next.delivery.next_event().await.unwrap(),
                delivered_stop(5, 2)
            );
            fixture.shutdown().await;
            assert_eq!(handoff.delivery.next_event().await, Err(expected));
        }
    });
}

#[test]
fn retirement_revokes_queued_data_in_both_replaced_and_current_deliveries() {
    run(async {
        for direct_drop in [false, true] {
            let mut fixture = Fixture::new([
                start(
                    2,
                    1,
                    vec![
                        observed_ad(advertisement(1, 7, 3)),
                        started(2, 1),
                        completed(2),
                    ],
                ),
                stop(3, 1),
                start(
                    4,
                    2,
                    vec![
                        observed_ad(advertisement(2, 8, 3)),
                        started(4, 2),
                        completed(4),
                    ],
                ),
            ]);
            fixture.supervisor.start().await.unwrap();
            let old = fixture
                .supervisor
                .begin_scan_view(&context(), ScanOptions::default())
                .unwrap();
            old.operation.wait(2000).await.unwrap();
            fixture.observed(completed(2)).await;
            fixture
                .supervisor
                .begin_stop_scan(&context(), 1.into())
                .unwrap()
                .wait(2000)
                .await
                .unwrap();
            fixture.observed(completed(3)).await;
            assert_eq!(old.delivery.snapshot().queued_advertisements, 1);

            // Keep A and its unread advertisement alive after B replaces the current route.
            let new = fixture
                .supervisor
                .begin_scan_view(&context(), ScanOptions::default())
                .unwrap();
            assert_eq!(new.operation.command_id(), CommandId::from(4));
            assert_eq!(
                new.operation.wait(2000).await.unwrap(),
                ScanSessionId::from(2)
            );
            fixture.observed(completed(4)).await;
            assert_eq!(old.delivery.snapshot().queued_advertisements, 1);
            assert_eq!(new.delivery.snapshot().queued_advertisements, 1);

            let cleanup = if direct_drop {
                let Fixture {
                    supervisor,
                    backend,
                    reader,
                    owner,
                    joined,
                    ..
                } = fixture;
                drop(supervisor);
                assert!(!joined.load(Ordering::Acquire));
                Some((backend, reader, owner))
            } else {
                fixture.shutdown().await;
                None
            };
            for delivery in [&old.delivery, &new.delivery] {
                assert_eq!(
                    delivery.next_event().await,
                    Err(ScanDeliveryError::OwnerRetired),
                    "retirement must discard unread data even in a replaced, stopped delivery"
                );
                assert_empty(delivery);
                assert_eq!(
                    delivery.next_event().await,
                    Err(ScanDeliveryError::OwnerRetired),
                    "the old Stopped result must not reappear after retirement"
                );
            }
            if let Some((backend, reader, owner)) = cleanup {
                // Drop proves revocation only. Retire the real owner and join its reader
                // explicitly, after the delivery assertions, without claiming a FIFO flush.
                let owner = owner
                    .lock()
                    .unwrap()
                    .upgrade()
                    .expect("reader retains owner");
                owner.shutdown().await.unwrap();
                let reader = reader.lock().unwrap().take().expect("reader started");
                reader.await.unwrap().unwrap();
                assert_eq!(backend.dispose_count(), 1);
                assert!(backend.contract_violations().is_empty());
            }
        }
    });
}

#[test]
fn shutdown_wakes_an_already_pending_pull_and_discards_queued_observations() {
    run(async {
        for pending in [true, false] {
            let mut fixture = Fixture::new([start(2, 1, vec![started(2, 1), completed(2)])]);
            fixture.supervisor.start().await.unwrap();
            let handoff = fixture
                .supervisor
                .begin_scan_view(&context(), ScanOptions::default())
                .unwrap();
            handoff.operation.wait(2000).await.unwrap();
            fixture.observed(completed(2)).await;
            if pending {
                let delivery = Arc::clone(&handoff.delivery);
                let (pending_tx, pending_rx) = oneshot::channel();
                let pull = tokio::spawn(async move {
                    let mut event = Box::pin(delivery.next_event());
                    poll_fn(|cx| {
                        assert!(event.as_mut().poll(cx).is_pending());
                        Poll::Ready(())
                    })
                    .await;
                    pending_tx.send(()).unwrap();
                    // Await in this task before yielding, so shutdown must wake its waker.
                    event.await
                });
                pending_rx.await.unwrap();
                assert!(handoff.delivery.snapshot().pull_pending);
                fixture.shutdown().await;
                assert_eq!(pull.await.unwrap(), Err(ScanDeliveryError::OwnerRetired));
            } else {
                fixture
                    .emit_and_observe(observed_ad(advertisement(1, 7, 3)))
                    .await;
                assert_eq!(handoff.delivery.snapshot().queued_advertisements, 1);
                fixture.shutdown().await;
                assert_eq!(
                    handoff.delivery.next_event().await,
                    Err(ScanDeliveryError::OwnerRetired)
                );
            }
            assert_empty(&handoff.delivery);
            assert_eq!(fixture.backend.commands().len(), 2);
        }
    });
}

#[test]
fn context_and_phase_rejection_precede_delivery_allocation_and_command_submission() {
    run(async {
        let mut fixture =
            Fixture::new([start(2, 1, vec![started(2, 1), completed(2)]), stop(3, 1)]);
        assert!(matches!(
            fixture
                .supervisor
                .begin_scan_view(&context(), ScanOptions::default()),
            Err(ScanSubmissionError::Owner(
                SupervisorOperationError::NoActiveSession
            ))
        ));
        assert!(fixture.backend.commands().is_empty());
        fixture.supervisor.start().await.unwrap();
        let initialized = fixture.backend.commands();
        for (owner, backend, generation, attempt) in [
            ("other-owner", "scan-backend", 1, 1),
            ("scan-owner", "other-backend", 1, 1),
            ("scan-owner", "scan-backend", 2, 1),
            ("scan-owner", "scan-backend", 1, 2),
        ] {
            let stale = RecoveryContext::new(
                OwnerBackendIdentity::new(owner, backend).unwrap(),
                OwnerGeneration::new(generation).unwrap(),
                RecoveryAttemptId::new(attempt).unwrap(),
            );
            assert!(matches!(
                fixture
                    .supervisor
                    .begin_scan_view(&stale, ScanOptions::default()),
                Err(ScanSubmissionError::Owner(
                    SupervisorOperationError::StaleContext
                ))
            ));
        }
        for signal in [RecoverySignal::RadioLost, RecoverySignal::RadioOn] {
            fixture
                .supervisor
                .observe(RecoveryObservation::new(context(), signal))
                .unwrap();
            assert!(matches!(
                fixture
                    .supervisor
                    .begin_scan_view(&context(), ScanOptions::default()),
                Err(ScanSubmissionError::Owner(
                    SupervisorOperationError::RecoveryNotReady
                ))
            ));
        }
        assert_eq!(fixture.backend.commands(), initialized);
        fixture
            .supervisor
            .observe(RecoveryObservation::new(
                context(),
                RecoverySignal::SameOwnerRecovered {
                    link: RecoveryLink::new("connection", "gatt", "subscription").unwrap(),
                },
            ))
            .unwrap();
        let handoff = fixture
            .supervisor
            .begin_scan_view(&context(), ScanOptions::default())
            .unwrap();
        // Exact scripted IDs prove rejected requests consumed neither command nor scan identity.
        assert_eq!(handoff.operation.command_id(), CommandId::from(2));
        assert_eq!(
            handoff.operation.wait(2000).await.unwrap(),
            ScanSessionId::from(1)
        );
        fixture.observed(completed(2)).await;
        fixture
            .supervisor
            .begin_stop_scan(&context(), 1.into())
            .unwrap()
            .wait(2000)
            .await
            .unwrap();
        fixture.observed(completed(3)).await;
        assert_eq!(
            handoff.delivery.next_event().await.unwrap(),
            delivered_stop(3, 1)
        );
        fixture.shutdown().await;
        assert!(matches!(
            fixture
                .supervisor
                .begin_scan_view(&context(), ScanOptions::default()),
            Err(ScanSubmissionError::Owner(
                SupervisorOperationError::NoActiveSession
            ))
        ));
        assert_eq!(fixture.backend.commands().len(), 3);
    });
}

/// The end of a scan is a retained fact, not a consumable receipt.
///
/// This is the counterpart to the operation result, which *is* single-consumer: a consumer that
/// polls on its own schedule must still be able to learn how the scan ended, and a second read must
/// not invent a different answer. Retirement is the one event allowed to replace it, because after
/// retirement the owner can no longer answer for the old scan at all.
#[test]
fn a_retained_scan_terminal_is_readable_more_than_once_until_retirement() {
    run(async {
        let mut fixture =
            Fixture::new([start(2, 1, vec![started(2, 1), completed(2)]), stop(3, 1)]);
        fixture.supervisor.start().await.unwrap();
        let handoff = fixture
            .supervisor
            .begin_scan_view(&context(), ScanOptions::default())
            .unwrap();
        handoff.operation.wait(2000).await.unwrap();
        fixture.observed(completed(2)).await;
        fixture
            .supervisor
            .begin_stop_scan(&context(), 1.into())
            .unwrap()
            .wait(2000)
            .await
            .unwrap();
        fixture.observed(completed(3)).await;

        let terminal = delivered_stop(3, 1);
        for _ in 0..3 {
            assert_eq!(
                handoff.delivery.next_event().await.unwrap(),
                terminal.clone()
            );
        }
        // Retention is not a second copy: the terminal never competes for advertisement capacity.
        assert_empty(&handoff.delivery);

        fixture.shutdown().await;
        for _ in 0..2 {
            assert_eq!(
                handoff.delivery.next_event().await,
                Err(ScanDeliveryError::OwnerRetired),
                "retirement replaces the retained terminal and stays definite"
            );
        }
    });
}
