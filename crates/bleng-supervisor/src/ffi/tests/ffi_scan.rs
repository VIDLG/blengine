#![cfg(feature = "ffi")]

use std::{
    collections::VecDeque,
    future::{Future, poll_fn},
    mem::size_of,
    panic::{AssertUnwindSafe, catch_unwind, resume_unwind},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, AtomicUsize, Ordering},
        mpsc as std_mpsc,
    },
    task::Poll,
    thread,
    time::Duration,
};

use super::scan_session::ScanSessionError;
use crate::{
    ForeignOperationFailure, ForeignOperationFailureDomain, ForeignOwnerContext,
    ForeignPlatformActionResult, ForeignPlatformOwnerSession, ForeignPlatformOwnerSessionFactory,
    ForeignPlatformRecoveryAction, ForeignRecoveryAction, ForeignRecoveryLink,
    ForeignRecoveryPolicyConfig, ForeignRecoverySignal, ForeignResourceCount,
    ForeignScanDeliverySnapshot, ForeignScanOptions, ForeignSupervisorConfig, ScanDeliveryError,
    ScanEvent, SessionEventProjection, SupervisedPeripheral, SupervisedScan, Supervisor,
    SupervisorFfiError, SupervisorOperationError,
};
use async_trait::async_trait;
use blehub::{
    AdapterSnapshot, AdapterState, AdvertisementObservation, AuthorizationState, BleBackend,
    BleErrorCategory, BleEvent, BleEventError, BluetoothUuid, CommandDispatchError, CommandId,
    ConnectCommand, ConnectionPriority, ConnectionState, ConnectionStateResult, DisconnectCommand,
    DiscoverGattCommand, EventIngressStatus, InitializeCommand, ManufacturerData, MtuCommand,
    MtuInfo, ReadCharacteristicCommand, ReadDescriptorCommand, ReadRssiCommand, ScanOptions,
    ScanStopReason, ScanStopResult, ServiceData, SetConnectionPriorityCommand, StartScanCommand,
    StopScanCommand, WriteCharacteristicCommand, WriteDescriptorCommand, WriteType,
};
use blehub_testkit::{EventScenario, FakeBackend, FakeBackendHandle, RecordedCommand};
use bleng::{Bleng, BlengBusyReason, BlengError};
use tokio::{
    sync::{mpsc, oneshot},
    task::JoinHandle,
};

const WAIT_MS: u64 = 2_000;
// A stuck delivery must hit the external watchdog, not pass by reaching its own timeout.
const PENDING_PULL_MS: u64 = 60_000;
const INVALID_LONG_TIMEOUT_MS: u64 = 24 * 60 * 60 * 1_000 + 1;

fn run<F: Future<Output = ()>>(test: impl FnOnce() -> F) {
    let (cancel_tx, cancel_rx) = oneshot::channel();
    let (finished_tx, finished_rx) = std_mpsc::sync_channel(1);
    let watchdog = thread::spawn(move || {
        if finished_rx.recv_timeout(Duration::from_secs(10)).is_err() {
            let _ = cancel_tx.send(());
        }
    });
    let result = catch_unwind(AssertUnwindSafe(|| {
        // Deliberately no enable_time(): these are foreign-caller executor tests.
        let runtime = tokio::runtime::Builder::new_current_thread()
            .build()
            .unwrap();
        runtime.block_on(async {
            tokio::select! {
                biased;
                _ = cancel_rx => panic!("external FFI scan test deadline expired"),
                () = test() => {},
            }
        });
    }));
    // Both success and panic drop the runtime/tasks and join the finite watchdog.
    let _ = finished_tx.send(());
    watchdog.join().expect("FFI scan watchdog failed to join");
    if let Err(panic) = result {
        resume_unwind(panic);
    }
}

fn scan_options() -> ForeignScanOptions {
    ForeignScanOptions {
        service_uuids: vec![],
        name_prefix: None,
        exact_name: None,
        company_id: None,
        min_rssi: None,
        manufacturer_data: None,
        android: None,
        allow_duplicates: false,
    }
}

fn assert_failure(
    failure: Option<ForeignOperationFailure>,
    domain: ForeignOperationFailureDomain,
    code: &str,
) {
    let failure = failure.expect("expected projected failure");
    assert_eq!(failure.domain, domain);
    assert_eq!(failure.code, code);
}

fn connect(command: u64, connection: u64, peripheral: u64, events: Vec<BleEvent>) -> EventScenario {
    EventScenario::emit_before_return(
        RecordedCommand::Connect(ConnectCommand {
            auto_connect: false,
            command_id: command.into(),
            connection_id: connection.into(),
            peripheral_id: peripheral.into(),
        }),
        events,
    )
}

fn connected(command: u64, connection: u64) -> BleEvent {
    BleEvent::ConnectionStateChanged {
        command_id: Some(command.into()),
        connection_id: connection.into(),
        result: ConnectionStateResult::Ok {
            state: ConnectionState::Connected,
        },
    }
}

fn advertised() -> Vec<EventScenario> {
    vec![
        start(
            2,
            1,
            vec![
                started(2, 1),
                observed_ad(advertisement(1, 0)),
                completed(2),
            ],
        ),
        connect(3, 2, 17, vec![connected(3, 2), completed(3)]),
    ]
}

/// The shared preamble: one observed peripheral with a live connection capability.
async fn begin_connection(fixture: &mut Fixture) -> Arc<crate::SupervisedConnection> {
    let result = Arc::clone(fixture.supervisor()).begin_scan_view(context(), scan_options());
    let scan = result.scan.expect("scan submission");
    assert!(scan.start_result(WAIT_MS as f64).await.failure.is_none());
    fixture.source.observed(completed(2)).await;
    let peripheral = scan
        .next_observation(WAIT_MS as f64)
        .await
        .advertisement
        .expect("advertisement")
        .peripheral;
    peripheral
        .begin_connect(false)
        .connect
        .expect("connect submission")
        .wait_result(WAIT_MS as f64)
        .await
        .connection
        .expect("connection capability")
}

fn context() -> ForeignOwnerContext {
    ForeignOwnerContext {
        owner_id: "ffi-scan-owner".to_owned(),
        backend_id: "ffi-scan-backend".to_owned(),
        owner_generation: 1,
        recovery_attempt: 1,
    }
}

fn config() -> ForeignSupervisorConfig {
    ForeignSupervisorConfig {
        recovery: ForeignRecoveryPolicyConfig {
            initial_context: context(),
            old_link: None,
            expected_old_operation_ids: vec![],
            allow_central_recreation: false,
            allow_fresh_owner: true,
        },
        recovery_target: None,
    }
}

fn adapter_ready() -> BleEvent {
    BleEvent::AdapterSnapshotChanged {
        command_id: None,
        snapshot: AdapterSnapshot {
            state: AdapterState::PoweredOn,
            scan_authorization: AuthorizationState::Authorized,
            connection_authorization: AuthorizationState::Authorized,
            name: None,
        },
    }
}

fn initialization() -> EventScenario {
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
            completed(1),
        ],
    )
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

fn stop(command: u64, scan: u64, events: Vec<BleEvent>) -> EventScenario {
    EventScenario::emit_before_return(
        RecordedCommand::StopScan(StopScanCommand {
            command_id: command.into(),
            scan_session_id: scan.into(),
        }),
        events,
    )
}

fn successful_stop(command: u64, scan: u64) -> EventScenario {
    stop(
        command,
        scan,
        vec![stopped(command, scan), completed(command)],
    )
}

fn delivered_stop(command: u64, scan: u64) -> ScanEvent {
    let BleEvent::ScanStopped { scan_id, result } = stopped(command, scan) else {
        unreachable!()
    };
    ScanEvent::Stopped { scan_id, result }
}

fn advertisement(scan: u64, marker: u8) -> AdvertisementObservation {
    AdvertisementObservation {
        scan_session_id: scan.into(),
        peripheral_id: 17.into(),
        local_name: None,
        advertised_services: vec![],
        manufacturer_data: vec![ManufacturerData {
            company_id: 0x004c,
            payload: vec![marker, 0, 255],
        }],
        service_data: vec![],
        rssi: Some(-127),
        connectable: Some(false),
    }
}

fn observed_ad(advertisement: AdvertisementObservation) -> BleEvent {
    BleEvent::AdvertisementObserved { advertisement }
}

enum ReaderRequest {
    Through(BleEvent),
    DrainAndJoin,
}

type ReaderTask = JoinHandle<Result<(), SupervisorFfiError>>;

struct ReaderControl {
    requests: mpsc::UnboundedSender<ReaderRequest>,
    task: Mutex<Option<ReaderTask>>,
    starts: AtomicUsize,
    joined: AtomicBool,
}

impl ReaderControl {
    async fn join(&self) -> Result<(), SupervisorFfiError> {
        let _ = self.requests.send(ReaderRequest::DrainAndJoin);
        let task = self
            .task
            .lock()
            .unwrap()
            .take()
            .expect("reader started once");
        task.await.map_err(|error| SupervisorFfiError::Platform {
            detail: format!("single public reader task failed: {error}"),
        })??;
        self.joined.store(true, Ordering::Release);
        Ok(())
    }
}

struct FakeForeignSession {
    context: ForeignOwnerContext,
    backend: Arc<dyn BleBackend>,
    requests: Mutex<Option<mpsc::UnboundedReceiver<ReaderRequest>>>,
    receipts: mpsc::UnboundedSender<BleEvent>,
    reader: Arc<ReaderControl>,
}

#[async_trait]
impl ForeignPlatformOwnerSession for FakeForeignSession {
    fn context(&self) -> ForeignOwnerContext {
        self.context.clone()
    }

    fn backend(&self) -> Arc<dyn BleBackend> {
        Arc::clone(&self.backend)
    }

    fn dispose_unassembled(&self) -> Result<(), SupervisorFfiError> {
        self.backend
            .dispose()
            .map_err(|error| SupervisorFfiError::Platform {
                detail: error.to_string(),
            })
    }

    fn start_event_reader(
        &self,
        owner: Arc<Bleng>,
        projection: Arc<SessionEventProjection>,
    ) -> Result<(), SupervisorFfiError> {
        assert_eq!(self.reader.starts.fetch_add(1, Ordering::AcqRel), 0);
        let mut requests = self.requests.lock().unwrap().take().expect("one reader");
        let receipts = self.receipts.clone();
        let source = self.context.clone();
        let owner = Arc::downgrade(&owner);
        *self.reader.task.lock().unwrap() = Some(tokio::spawn(async move {
            // Demand controls scheduling only: every receipt still follows the real sole
            // public-stream read and project(). An idle fake reader must not retain Bleng
            // and thereby hide whether dropping Supervisor actually retires its backend.
            while let Some(request) = requests.recv().await {
                let Some(owner) = owner.upgrade() else {
                    assert!(matches!(request, ReaderRequest::DrainAndJoin));
                    return Ok(());
                };
                loop {
                    match owner.next_event(WAIT_MS).await {
                        Ok(Some(event)) => {
                            if let Some(envelope) = projection.project(event.clone()) {
                                assert_eq!(envelope.source_context, source);
                            }
                            let reached = matches!(
                                &request,
                                ReaderRequest::Through(expected) if *expected == event
                            );
                            let _ = receipts.send(event);
                            if reached {
                                break;
                            }
                        }
                        Ok(None) | Err(BlengError::Closed) => {
                            assert!(matches!(request, ReaderRequest::DrainAndJoin));
                            return Ok(());
                        }
                        Err(error) => {
                            return Err(SupervisorFfiError::Platform {
                                detail: format!("single public reader failed: {error}"),
                            });
                        }
                    }
                }
            }
            Ok(())
        }));
        Ok(())
    }

    async fn wait_event_reader(&self) -> Result<(), SupervisorFfiError> {
        self.reader.join().await
    }

    async fn resource_snapshot(&self) -> Result<Vec<ForeignResourceCount>, SupervisorFfiError> {
        assert!(self.reader.joined.load(Ordering::Acquire));
        Ok(vec![ForeignResourceCount {
            name: "callbacks".to_owned(),
            count: 0,
        }])
    }

    async fn execute_recovery_action(
        &self,
        _: u64,
        action: ForeignPlatformRecoveryAction,
    ) -> Result<ForeignPlatformActionResult, SupervisorFfiError> {
        Err(SupervisorFfiError::Platform {
            detail: format!("unexpected platform action: {action:?}"),
        })
    }
}

struct FakeForeignFactory(Mutex<VecDeque<Arc<dyn ForeignPlatformOwnerSession>>>);

impl ForeignPlatformOwnerSessionFactory for FakeForeignFactory {
    fn required_zero_resource_names(&self) -> Vec<String> {
        vec!["callbacks".to_owned()]
    }

    fn create(
        &self,
        requested: ForeignOwnerContext,
    ) -> Result<Arc<dyn ForeignPlatformOwnerSession>, SupervisorFfiError> {
        let session = self.0.lock().unwrap().pop_front().expect("scripted owner");
        assert_eq!(session.context(), requested);
        Ok(session)
    }
}

struct Source {
    context: ForeignOwnerContext,
    backend: FakeBackendHandle,
    receipts: mpsc::UnboundedReceiver<BleEvent>,
    reader: Arc<ReaderControl>,
}

impl Source {
    fn new(
        context: ForeignOwnerContext,
        operations: impl IntoIterator<Item = EventScenario>,
    ) -> (Self, Arc<dyn ForeignPlatformOwnerSession>) {
        let (backend, handle) =
            FakeBackend::scripted([initialization()].into_iter().chain(operations));
        let (requests, request_rx) = mpsc::unbounded_channel();
        let (receipts, receipt_rx) = mpsc::unbounded_channel();
        let reader = Arc::new(ReaderControl {
            requests,
            task: Mutex::new(None),
            starts: AtomicUsize::new(0),
            joined: AtomicBool::new(false),
        });
        let session = Arc::new(FakeForeignSession {
            context: context.clone(),
            backend: Arc::new(backend),
            requests: Mutex::new(Some(request_rx)),
            receipts,
            reader: Arc::clone(&reader),
        });
        (
            Self {
                context,
                backend: handle,
                receipts: receipt_rx,
                reader,
            },
            session,
        )
    }

    async fn observed(&mut self, expected: BleEvent) {
        self.reader
            .requests
            .send(ReaderRequest::Through(expected.clone()))
            .unwrap();
        loop {
            if self.receipts.recv().await.expect("reader closed early") == expected {
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

    fn assert_retired(&self) {
        assert_eq!(self.reader.starts.load(Ordering::Acquire), 1);
        assert!(self.reader.joined.load(Ordering::Acquire));
        assert_eq!(self.backend.dispose_count(), 1);
        assert!(self.backend.contract_violations().is_empty());
    }
}

struct Fixture {
    supervisor: Option<Arc<Supervisor>>,
    source: Source,
}

impl Fixture {
    fn new(operations: impl IntoIterator<Item = EventScenario>) -> Self {
        let (source, session) = Source::new(context(), operations);
        Self {
            supervisor: Some(
                Supervisor::new(
                    Arc::new(FakeForeignFactory(Mutex::new([session].into()))),
                    config(),
                )
                .unwrap(),
            ),
            source,
        }
    }

    fn supervisor(&self) -> &Arc<Supervisor> {
        self.supervisor.as_ref().expect("supervisor not dropped")
    }

    fn begin_scan(&self) -> Arc<SupervisedScan> {
        Arc::clone(self.supervisor())
            .begin_scan_view(self.source.context.clone(), scan_options())
            .scan
            .expect("scan submission accepted")
    }

    async fn shutdown(&self) {
        self.supervisor().shutdown().await.unwrap();
        self.source.assert_retired();
    }
}

async fn assert_started(scan: &SupervisedScan) {
    assert!(matches!(scan.wait_until_started(WAIT_MS).await, Ok(())));
}

fn assert_empty(scan: &SupervisedScan) {
    let snapshot: ForeignScanDeliverySnapshot = scan.observation_snapshot();
    let counts: (u64, u64, u64) = (
        snapshot.queued_advertisements,
        snapshot.queued_bytes,
        snapshot.queued_stop_bytes,
    );
    assert_eq!(counts, (0, 0, 0));
    assert!(!snapshot.pull_pending);
}

fn assert_delivery(result: Result<ScanEvent, ScanSessionError>, expected: ScanDeliveryError) {
    match result {
        Err(ScanSessionError::Delivery { error }) => assert_eq!(error, expected),
        other => panic!("expected delivery failure {expected:?}, got {other:?}"),
    }
}

async fn poll_pending<F: Future>(future: std::pin::Pin<&mut F>) {
    let mut future = future;
    poll_fn(|cx| {
        assert!(future.as_mut().poll(cx).is_pending());
        Poll::Ready(())
    })
    .await;
}

async fn await_pull_pending(scan: &SupervisedScan) {
    while !scan.observation_snapshot().pull_pending {
        tokio::task::yield_now().await;
    }
}

#[test]
fn callback_before_return_preserves_payload_budgets_and_original_typed_stop() {
    run(|| async {
        let mut ad = advertisement(1, 128);
        let uuid = BluetoothUuid::canonicalize("180d").unwrap();
        ad.local_name = Some("é🦀\0peer".to_owned());
        ad.advertised_services = vec![uuid.clone(), uuid.clone()];
        ad.service_data = vec![ServiceData {
            service_uuid: uuid.clone(),
            payload: vec![0, 255, 128],
        }];
        let bytes = "é🦀\0peer".len()
            + 2 * (size_of::<BluetoothUuid>() + uuid.value.len())
            + size_of::<ManufacturerData>()
            + 3
            + size_of::<ServiceData>()
            + uuid.value.len()
            + 3;
        let mut fixture = Fixture::new([
            start(
                2,
                1,
                vec![observed_ad(ad.clone()), started(2, 1), completed(2)],
            ),
            successful_stop(3, 1),
        ]);
        fixture.supervisor().start().await.unwrap();
        let scan = fixture.begin_scan();
        assert_started(&scan).await;
        // No ScanStarted has been projected yet: stop must use the privately saved result.
        let stop: Arc<bleng::StopScanOperation> = scan.begin_stop().unwrap();
        fixture.source.observed(completed(2)).await;
        let snapshot = scan.observation_snapshot();
        assert_eq!(snapshot.queued_advertisements, 1_u64);
        assert_eq!(snapshot.queued_bytes, bytes as u64);
        assert_eq!(snapshot.queued_stop_bytes, 0_u64);
        assert!(!snapshot.pull_pending);
        assert_eq!(stop.command_id(), CommandId::from(3));
        assert!(matches!(stop.wait(WAIT_MS).await, Ok(())));
        fixture.source.observed(completed(3)).await;
        assert!(matches!(
            stop.wait(WAIT_MS).await,
            Err(BlengError::OperationResultConsumed)
        ));
        assert!(matches!(stop.cancel().await,
            Err(BlengError::OperationAlreadyCompleted { command_id }) if command_id == 3.into()));
        let event: ScanEvent = scan.next_event(WAIT_MS).await.unwrap();
        assert_eq!(event, ScanEvent::Advertisement { advertisement: ad });
        assert_eq!(
            scan.next_event(WAIT_MS).await.unwrap(),
            delivered_stop(3, 1)
        );
        assert_empty(&scan);
        fixture.shutdown().await;
    });
}

#[test]
fn timerless_pull_timeout_releases_its_reader_and_later_receipted_advertisement_succeeds() {
    run(|| async {
        let mut fixture = Fixture::new([start(2, 1, vec![started(2, 1), completed(2)])]);
        fixture.supervisor().start().await.unwrap();
        let scan = fixture.begin_scan();
        assert_started(&scan).await;
        fixture.source.observed(completed(2)).await;
        for timeout in [0, INVALID_LONG_TIMEOUT_MS] {
            assert!(matches!(
                scan.next_event(timeout).await,
                Err(ScanSessionError::Observation {
                    error: BlengError::InvalidTimeout
                })
            ));
            assert_empty(&scan);
        }
        assert!(matches!(
            scan.next_event(1).await,
            Err(ScanSessionError::Observation {
                error: BlengError::Timeout
            })
        ));
        assert_empty(&scan);
        assert_eq!(fixture.source.backend.commands().len(), 2);

        let first = advertisement(1, 7);
        fixture
            .source
            .emit_and_observe(observed_ad(first.clone()))
            .await;
        assert_eq!(
            scan.next_event(WAIT_MS).await.unwrap(),
            ScanEvent::Advertisement {
                advertisement: first
            }
        );
        assert_empty(&scan);

        let mut pending = Box::pin(scan.next_event(PENDING_PULL_MS));
        poll_pending(pending.as_mut()).await;
        await_pull_pending(&scan).await;
        assert_delivery(scan.next_event(WAIT_MS).await, ScanDeliveryError::PullBusy);
        assert!(scan.observation_snapshot().pull_pending);
        let second = advertisement(1, 8);
        fixture
            .source
            .emit_and_observe(observed_ad(second.clone()))
            .await;
        assert_eq!(
            pending.await.unwrap(),
            ScanEvent::Advertisement {
                advertisement: second
            }
        );
        assert_empty(&scan);
        fixture.shutdown().await;
    });
}

#[test]
fn invalid_and_duplicate_start_waits_do_not_detach_or_corrupt_delivery() {
    run(|| async {
        let ad = advertisement(1, 42);
        let mut fixture = Fixture::new([
            start(
                2,
                1,
                vec![started(2, 1), observed_ad(ad.clone()), completed(2)],
            ),
            successful_stop(3, 1),
        ]);
        fixture.supervisor().start().await.unwrap();
        let scan = fixture.begin_scan();
        fixture.source.observed(completed(2)).await;
        for timeout in [0, INVALID_LONG_TIMEOUT_MS] {
            assert!(matches!(
                scan.wait_until_started(timeout).await,
                Err(ScanSessionError::Operation {
                    error: BlengError::InvalidTimeout
                })
            ));
            assert_eq!(scan.observation_snapshot().queued_advertisements, 1);
        }
        assert_started(&scan).await;
        assert!(matches!(
            scan.wait_until_started(WAIT_MS).await,
            Err(ScanSessionError::Operation {
                error: BlengError::OperationResultConsumed
            })
        ));
        assert!(matches!(scan.cancel_start().await,
            Err(ScanSessionError::Operation {
                error: BlengError::OperationAlreadyCompleted { command_id }
            }) if command_id == 2.into()));
        let second = Arc::clone(fixture.supervisor()).begin_scan_view(context(), scan_options());
        assert!(second.scan.is_none());
        assert_failure(
            second.failure,
            ForeignOperationFailureDomain::Delivery,
            "delivery.busy",
        );
        assert_eq!(fixture.source.backend.commands().len(), 2);
        assert_eq!(
            scan.next_event(WAIT_MS).await.unwrap(),
            ScanEvent::Advertisement { advertisement: ad }
        );
        let stop: Arc<bleng::StopScanOperation> = scan.begin_stop().unwrap();
        stop.wait(WAIT_MS).await.unwrap();
        fixture.source.observed(completed(3)).await;
        assert_eq!(
            scan.next_event(WAIT_MS).await.unwrap(),
            delivered_stop(3, 1)
        );
        fixture.shutdown().await;
    });
}

#[test]
fn start_timeout_keeps_delivery_and_original_cancel_then_observed_started_enables_stop() {
    run(|| async {
        let mut fixture = Fixture::new([
            start(2, 1, vec![adapter_ready()]),
            stop(3, 1, vec![adapter_ready()]),
        ]);
        fixture.supervisor().start().await.unwrap();
        let scan = fixture.begin_scan();
        // Prove dispatch before testing cancellation; no ScanStarted or terminal exists yet.
        fixture.source.observed(adapter_ready()).await;
        assert!(matches!(
            scan.begin_stop(),
            Err(ScanSessionError::StartNotObserved)
        ));
        assert!(matches!(
            scan.wait_until_started(1).await,
            Err(ScanSessionError::Operation {
                error: BlengError::Timeout
            })
        ));
        assert!(matches!(
            scan.begin_stop(),
            Err(ScanSessionError::StartNotObserved)
        ));
        assert!(matches!(
            scan.wait_until_started(WAIT_MS).await,
            Err(ScanSessionError::Operation {
                error: BlengError::OperationResultConsumed
            })
        ));
        assert!(matches!(scan.cancel_start().await,
            Err(ScanSessionError::Operation {
                error: BlengError::OperationNotCancellable { command_id }
            }) if command_id == 2.into()));
        let second = Arc::clone(fixture.supervisor()).begin_scan_view(context(), scan_options());
        assert!(second.scan.is_none());
        assert_failure(
            second.failure,
            ForeignOperationFailureDomain::Delivery,
            "delivery.busy",
        );
        assert_eq!(
            fixture.source.backend.commands().len(),
            2,
            "a wait timeout must not dispatch stop or replace the original receipt"
        );
        fixture.source.emit_and_observe(started(2, 1)).await;
        fixture.source.emit_and_observe(completed(2)).await;
        let ad = advertisement(1, 99);
        fixture
            .source
            .emit_and_observe(observed_ad(ad.clone()))
            .await;
        assert_eq!(
            scan.next_event(WAIT_MS).await.unwrap(),
            ScanEvent::Advertisement { advertisement: ad }
        );

        let stop: Arc<bleng::StopScanOperation> = scan.begin_stop().unwrap();
        assert_eq!(stop.command_id(), CommandId::from(3));
        fixture.source.observed(adapter_ready()).await;
        assert!(matches!(
            stop.wait(0).await,
            Err(BlengError::InvalidTimeout)
        ));
        assert!(matches!(stop.cancel().await,
            Err(BlengError::OperationNotCancellable { command_id }) if command_id == 3.into()));
        fixture.source.emit_and_observe(stopped(3, 1)).await;
        fixture.source.emit_and_observe(completed(3)).await;
        assert!(matches!(stop.wait(WAIT_MS).await, Ok(())));
        assert!(matches!(
            stop.wait(WAIT_MS).await,
            Err(BlengError::OperationResultConsumed)
        ));
        assert_eq!(
            scan.next_event(WAIT_MS).await.unwrap(),
            delivered_stop(3, 1)
        );
        fixture.shutdown().await;
    });
}

#[test]
fn actual_start_failure_wakes_pending_pull_preserves_operation_error_and_releases_slot() {
    run(|| async {
        let failure = BleEventError {
            category: BleErrorCategory::Backend,
            message: "original native scan failure é\0".to_owned(),
            platform_details: None,
        };
        let failed = BleEvent::CommandCompleted {
            command_id: 2.into(),
            error: Some(failure.clone()),
        };
        let mut fixture = Fixture::new([
            start(2, 1, vec![adapter_ready()]),
            start(3, 2, vec![started(3, 2), completed(3)]),
            successful_stop(4, 2),
        ]);
        fixture.supervisor().start().await.unwrap();
        let scan = fixture.begin_scan();
        fixture.source.observed(adapter_ready()).await;
        let mut pending = Box::pin(scan.next_event(PENDING_PULL_MS));
        poll_pending(pending.as_mut()).await;
        await_pull_pending(&scan).await;
        fixture.source.emit_and_observe(failed).await;
        match scan.wait_until_started(WAIT_MS).await {
            Err(ScanSessionError::Operation {
                error: BlengError::Operation { error },
            }) => {
                assert_eq!(error, failure);
            }
            other => panic!("original start error was rewritten: {other:?}"),
        }
        assert_delivery(pending.await, ScanDeliveryError::StartFailed);
        assert_empty(&scan);
        assert!(matches!(
            scan.wait_until_started(WAIT_MS).await,
            Err(ScanSessionError::Operation {
                error: BlengError::OperationResultConsumed
            })
        ));
        assert_delivery(
            scan.next_event(WAIT_MS).await,
            ScanDeliveryError::StartFailed,
        );
        assert_eq!(fixture.source.backend.commands().len(), 2);

        let next = fixture.begin_scan();
        assert_started(&next).await;
        fixture.source.observed(completed(3)).await;
        let ad = advertisement(2, 11);
        fixture
            .source
            .emit_and_observe(observed_ad(ad.clone()))
            .await;
        assert_eq!(
            next.next_event(WAIT_MS).await.unwrap(),
            ScanEvent::Advertisement { advertisement: ad }
        );
        next.begin_stop().unwrap().wait(WAIT_MS).await.unwrap();
        fixture.source.observed(completed(4)).await;
        assert_eq!(
            next.next_event(WAIT_MS).await.unwrap(),
            delivered_stop(4, 2)
        );
        assert_delivery(
            scan.next_event(WAIT_MS).await,
            ScanDeliveryError::StartFailed,
        );
        fixture.shutdown().await;
    });
}

enum StartWaitDisposition {
    Unclaimed,
    TimedOut,
    Dropped,
}

async fn late_start_failure_without_result_consumer(disposition: StartWaitDisposition) {
    let failure = BleEventError {
        category: BleErrorCategory::Backend,
        message: "late native failure without Started/Stopped".to_owned(),
        platform_details: None,
    };
    let mut fixture = Fixture::new([
        start(2, 1, vec![adapter_ready()]),
        start(3, 2, vec![started(3, 2), completed(3)]),
        successful_stop(4, 2),
    ]);
    fixture.supervisor().start().await.unwrap();
    let scan = fixture.begin_scan();
    fixture.source.observed(adapter_ready()).await;
    match disposition {
        StartWaitDisposition::Unclaimed => {}
        StartWaitDisposition::TimedOut => assert!(matches!(
            scan.wait_until_started(1).await,
            Err(ScanSessionError::Operation {
                error: BlengError::Timeout,
            })
        )),
        StartWaitDisposition::Dropped => {
            let mut wait = Box::pin(scan.wait_until_started(PENDING_PULL_MS));
            poll_pending(wait.as_mut()).await;
            drop(wait);
        }
    }
    if !matches!(disposition, StartWaitDisposition::Unclaimed) {
        // Prove the result slot was consumed before the backend reports failure.
        assert!(matches!(
            scan.wait_until_started(WAIT_MS).await,
            Err(ScanSessionError::Operation {
                error: BlengError::OperationResultConsumed,
            })
        ));
    }
    let mut pull = Box::pin(scan.next_event(WAIT_MS));
    poll_pending(pull.as_mut()).await;
    await_pull_pending(&scan).await;
    fixture
        .source
        .emit_and_observe(BleEvent::CommandCompleted {
            command_id: 2.into(),
            error: Some(failure.clone()),
        })
        .await;
    assert_delivery(pull.await, ScanDeliveryError::StartFailed);
    assert_empty(&scan);
    if matches!(disposition, StartWaitDisposition::Unclaimed) {
        match scan.wait_until_started(WAIT_MS).await {
            Err(ScanSessionError::Operation {
                error: BlengError::Operation { error },
            }) => assert_eq!(error, failure),
            other => panic!("observation consumed or rewrote the original result: {other:?}"),
        }
    }
    assert_eq!(fixture.source.backend.commands().len(), 2);
    assert_eq!(fixture.source.backend.dispose_count(), 0);
    let next = fixture.begin_scan();
    assert_started(&next).await;
    fixture.source.observed(completed(3)).await;
    next.begin_stop().unwrap().wait(WAIT_MS).await.unwrap();
    fixture.source.observed(completed(4)).await;
    assert_delivery(
        scan.next_event(WAIT_MS).await,
        ScanDeliveryError::StartFailed,
    );
    fixture.shutdown().await;
}

#[test]
fn late_start_failure_without_wait_releases_delivery() {
    run(|| late_start_failure_without_result_consumer(StartWaitDisposition::Unclaimed));
}

#[test]
fn late_start_failure_after_wait_timeout_releases_delivery() {
    run(|| late_start_failure_without_result_consumer(StartWaitDisposition::TimedOut));
}

#[test]
fn late_start_failure_after_wait_drop_releases_delivery() {
    run(|| late_start_failure_without_result_consumer(StartWaitDisposition::Dropped));
}

#[test]
fn failed_start_releases_admission_without_any_wait_pull_or_snapshot() {
    run(|| async {
        let failure = BleEventError {
            category: BleErrorCategory::Backend,
            message: "failure with no observation consumer".to_owned(),
            platform_details: None,
        };
        let terminal = BleEvent::CommandCompleted {
            command_id: 2.into(),
            error: Some(failure.clone()),
        };
        let mut fixture = Fixture::new([
            start(2, 1, vec![terminal.clone()]),
            start(3, 2, vec![started(3, 2), completed(3)]),
            successful_stop(4, 2),
        ]);
        fixture.supervisor().start().await.unwrap();
        let failed = fixture.begin_scan();
        fixture.source.observed(terminal).await;
        // Publication precedes settlement within the actor. Receipt of a subsequent
        // event fences that settlement without consuming or inspecting the start slot.
        fixture.source.emit_and_observe(adapter_ready()).await;
        // No method on the failed handle has run. Admission itself must reconcile
        // the failure flag, rather than depending on a pull/wait/snapshot side effect.
        let next = fixture.begin_scan();
        assert_started(&next).await;
        fixture.source.observed(completed(3)).await;
        match failed.wait_until_started(WAIT_MS).await {
            Err(ScanSessionError::Operation {
                error: BlengError::Operation { error },
            }) => assert_eq!(error, failure),
            other => panic!("original unclaimed error was lost: {other:?}"),
        }
        assert_delivery(
            failed.next_event(WAIT_MS).await,
            ScanDeliveryError::StartFailed,
        );
        next.begin_stop().unwrap().wait(WAIT_MS).await.unwrap();
        fixture.source.observed(completed(4)).await;
        fixture.shutdown().await;
    });
}

#[test]
fn admission_rejection_without_public_event_or_start_wait_releases_delivery() {
    run(|| async {
        let mut fixture = Fixture::new([
            start(2, 1, vec![started(2, 1), completed(2)]),
            successful_stop(4, 1),
            start(5, 2, vec![started(5, 2), completed(5)]),
            successful_stop(6, 2),
        ]);
        fixture.supervisor().start().await.unwrap();
        let old = fixture.begin_scan();
        assert_started(&old).await;
        fixture.source.observed(completed(2)).await;
        old.detach();
        let rejected = fixture.begin_scan();
        // Command 3 is rejected by actor admission, never reaching the backend. An
        // explicit stop of the old scan is independent of the rejected result slot.
        let stop = old.begin_stop().unwrap();
        assert_eq!(stop.command_id(), 4.into());
        stop.wait(WAIT_MS).await.unwrap();
        fixture.source.observed(completed(4)).await;
        assert_eq!(fixture.source.backend.commands().len(), 3);
        assert_delivery(
            rejected.next_event(WAIT_MS).await,
            ScanDeliveryError::StartFailed,
        );
        assert_empty(&rejected);
        // Observation must not steal the typed admission error.
        assert!(matches!(
            rejected.wait_until_started(WAIT_MS).await,
            Err(ScanSessionError::Operation {
                error: BlengError::Busy {
                    reason: BlengBusyReason::Scan
                },
            })
        ));
        let next = fixture.begin_scan();
        assert_started(&next).await;
        fixture.source.observed(completed(5)).await;
        next.begin_stop().unwrap().wait(WAIT_MS).await.unwrap();
        fixture.source.observed(completed(6)).await;
        fixture.shutdown().await;
    });
}

#[test]
fn dispatch_rejection_without_public_event_or_start_wait_releases_delivery() {
    run(|| async {
        let failure = CommandDispatchError::InvalidState {
            detail: "synchronous scan rejection without callback".to_owned(),
        };
        let mut fixture = Fixture::new([
            EventScenario::reject(
                RecordedCommand::StartScan(StartScanCommand {
                    command_id: 2.into(),
                    scan_session_id: 1.into(),
                    options: ScanOptions::default(),
                }),
                failure.clone(),
            ),
            start(3, 2, vec![started(3, 2), completed(3)]),
            successful_stop(4, 2),
        ]);
        fixture.supervisor().start().await.unwrap();
        let rejected = fixture.begin_scan();
        // No synthetic terminal or public wait is allowed to reveal this rejection.
        assert_delivery(
            rejected.next_event(WAIT_MS).await,
            ScanDeliveryError::StartFailed,
        );
        assert_empty(&rejected);
        match rejected.wait_until_started(WAIT_MS).await {
            Err(ScanSessionError::Operation {
                error: BlengError::DispatchRejected { error },
            }) => assert_eq!(error, failure),
            other => panic!("original dispatch error was consumed or rewritten: {other:?}"),
        }
        assert_eq!(fixture.source.backend.commands().len(), 2);
        assert_eq!(fixture.source.backend.dispose_count(), 0);
        let next = fixture.begin_scan();
        assert_started(&next).await;
        fixture.source.observed(completed(3)).await;
        next.begin_stop().unwrap().wait(WAIT_MS).await.unwrap();
        fixture.source.observed(completed(4)).await;
        fixture.shutdown().await;
    });
}

#[test]
fn successful_start_and_failed_stop_terminals_do_not_end_delivery() {
    run(|| async {
        let failure = BleEventError {
            category: BleErrorCategory::Backend,
            message: "stop failed without ScanStopped".to_owned(),
            platform_details: None,
        };
        let failed_stop = BleEvent::CommandCompleted {
            command_id: 3.into(),
            error: Some(failure.clone()),
        };
        let mut fixture = Fixture::new([
            start(2, 1, vec![started(2, 1), completed(2)]),
            stop(3, 1, vec![failed_stop.clone()]),
            successful_stop(4, 1),
        ]);
        fixture.supervisor().start().await.unwrap();
        let scan = fixture.begin_scan();
        fixture.source.observed(completed(2)).await;
        assert!(matches!(
            scan.next_event(1).await,
            Err(ScanSessionError::Observation {
                error: BlengError::Timeout
            })
        ));
        let stop = scan.begin_stop().unwrap();
        fixture.source.observed(failed_stop).await;
        match stop.wait(WAIT_MS).await {
            Err(BlengError::Operation { error }) => assert_eq!(error, failure),
            other => panic!("stop result was rewritten: {other:?}"),
        }
        let ad = advertisement(1, 73);
        fixture
            .source
            .emit_and_observe(observed_ad(ad.clone()))
            .await;
        assert_eq!(
            scan.next_event(WAIT_MS).await.unwrap(),
            ScanEvent::Advertisement { advertisement: ad }
        );
        assert_started(&scan).await;
        let second = Arc::clone(fixture.supervisor()).begin_scan_view(context(), scan_options());
        assert!(second.scan.is_none());
        assert_failure(
            second.failure,
            ForeignOperationFailureDomain::Delivery,
            "delivery.busy",
        );
        scan.begin_stop().unwrap().wait(WAIT_MS).await.unwrap();
        fixture.source.observed(completed(4)).await;
        assert_eq!(
            scan.next_event(WAIT_MS).await.unwrap(),
            delivered_stop(4, 1)
        );
        fixture.shutdown().await;
    });
}

#[test]
fn detach_and_last_scan_drop_release_only_observation_not_native_scan_admission() {
    run(|| async {
        for direct_drop in [false, true] {
            let mut fixture = Fixture::new([
                start(2, 1, vec![started(2, 1), completed(2)]),
                successful_stop(4, 1),
            ]);
            fixture.supervisor().start().await.unwrap();
            let scan = fixture.begin_scan();
            assert_started(&scan).await;
            fixture.source.observed(completed(2)).await;
            if direct_drop {
                let weak_scan = Arc::downgrade(&scan);
                drop(scan);
                assert!(weak_scan.upgrade().is_none());
            } else {
                let mut pending = Box::pin(scan.next_event(PENDING_PULL_MS));
                poll_pending(pending.as_mut()).await;
                await_pull_pending(&scan).await;
                scan.detach();
                scan.detach();
                assert_delivery(pending.await, ScanDeliveryError::Detached);
                assert_empty(&scan);
                assert!(matches!(scan.cancel_start().await,
                    Err(ScanSessionError::Operation {
                        error: BlengError::OperationAlreadyCompleted { command_id }
                    }) if command_id == 2.into()));
                drop(scan);
            }
            assert_eq!(
                fixture.source.backend.commands().len(),
                2,
                "detach/drop must not submit a BLE stop"
            );
            let rejected = fixture.begin_scan();
            assert!(matches!(
                rejected.wait_until_started(WAIT_MS).await,
                Err(ScanSessionError::Operation {
                    error: BlengError::Busy {
                        reason: BlengBusyReason::Scan
                    }
                })
            ));
            assert_delivery(
                rejected.next_event(WAIT_MS).await,
                ScanDeliveryError::StartFailed,
            );
            assert_eq!(
                fixture.source.backend.commands().len(),
                2,
                "releasing a delivery slot must not bypass actual BLeng scan admission"
            );
            // The test knows this native ID; no new resource accessor is added to the FFI scan.
            let stop: Arc<bleng::StopScanOperation> = fixture
                .supervisor()
                .begin_stop_scan(context(), 1.into())
                .unwrap();
            assert_eq!(stop.command_id(), CommandId::from(4));
            stop.wait(WAIT_MS).await.unwrap();
            fixture.source.observed(completed(4)).await;
            fixture.shutdown().await;
        }
    });
}

#[test]
fn shutdown_and_supervisor_drop_retire_old_and_current_deliveries_without_scan_keepalive() {
    run(|| async {
        for direct_drop in [false, true] {
            for pending_pull in [false, true] {
                let mut fixture = Fixture::new([
                    start(
                        2,
                        1,
                        vec![
                            started(2, 1),
                            observed_ad(advertisement(1, 7)),
                            completed(2),
                        ],
                    ),
                    successful_stop(3, 1),
                    start(
                        4,
                        2,
                        vec![
                            started(4, 2),
                            observed_ad(advertisement(2, 8)),
                            completed(4),
                        ],
                    ),
                ]);
                fixture.supervisor().start().await.unwrap();
                let old = fixture.begin_scan();
                assert_started(&old).await;
                fixture.source.observed(completed(2)).await;
                old.begin_stop().unwrap().wait(WAIT_MS).await.unwrap();
                fixture.source.observed(completed(3)).await;
                let current = fixture.begin_scan();
                assert_started(&current).await;
                fixture.source.observed(completed(4)).await;
                assert_eq!(old.observation_snapshot().queued_advertisements, 1);
                assert_eq!(current.observation_snapshot().queued_advertisements, 1);
                let mut pending = if pending_pull {
                    assert!(matches!(
                        current.next_event(WAIT_MS).await,
                        Ok(ScanEvent::Advertisement { .. })
                    ));
                    Some(Box::pin(current.next_event(PENDING_PULL_MS)))
                } else {
                    None
                };
                if let Some(pull) = pending.as_mut() {
                    poll_pending(pull.as_mut()).await;
                    await_pull_pending(&current).await;
                }
                if direct_drop {
                    let supervisor = fixture.supervisor.take().unwrap();
                    let weak_supervisor = Arc::downgrade(&supervisor);
                    drop(supervisor);
                    assert!(
                        weak_supervisor.upgrade().is_none(),
                        "SupervisedScan must hold only a weak Supervisor reference"
                    );
                } else {
                    fixture.shutdown().await;
                }
                if let Some(pull) = pending {
                    assert_delivery(pull.await, ScanDeliveryError::OwnerRetired);
                }
                for scan in [&old, &current] {
                    assert_delivery(
                        scan.next_event(WAIT_MS).await,
                        ScanDeliveryError::OwnerRetired,
                    );
                    assert_delivery(
                        scan.next_event(WAIT_MS).await,
                        ScanDeliveryError::OwnerRetired,
                    );
                    assert_empty(scan);
                    if direct_drop {
                        assert!(matches!(
                            scan.begin_stop(),
                            Err(ScanSessionError::Delivery {
                                error: ScanDeliveryError::OwnerRetired
                            })
                        ));
                    } else {
                        assert!(matches!(
                            scan.begin_stop(),
                            Err(ScanSessionError::Submission {
                                error: SupervisorOperationError::NoActiveSession
                            })
                        ));
                    }
                }
                if direct_drop {
                    // Join the sole reader, but do not call owner.shutdown(): disposal must
                    // follow the production owner drop, not a test's hidden cleanup owner.
                    fixture.source.reader.join().await.unwrap();
                    while fixture.source.backend.dispose_count() == 0 {
                        tokio::task::yield_now().await;
                    }
                    fixture.source.assert_retired();
                }
                assert_eq!(
                    fixture.source.backend.commands().len(),
                    4,
                    "retirement must not synthesize an extra stop operation"
                );
            }
        }
    });
}

#[test]
fn submission_fences_every_context_field_and_old_stop_never_borrows_fresh_owner_context() {
    run(|| async {
        let (mut old_source, old_session) =
            Source::new(context(), [start(2, 1, vec![started(2, 1), completed(2)])]);
        let fresh_context = ForeignOwnerContext {
            owner_id: "ffi-scan-owner-fresh".to_owned(),
            backend_id: "ffi-scan-backend-fresh".to_owned(),
            owner_generation: 2,
            recovery_attempt: 1,
        };
        let (mut fresh_source, fresh_session) = Source::new(
            fresh_context.clone(),
            [
                start(2, 1, vec![started(2, 1), completed(2)]),
                successful_stop(3, 1),
            ],
        );
        let supervisor = Supervisor::new(
            Arc::new(FakeForeignFactory(Mutex::new(
                [old_session, fresh_session].into(),
            ))),
            config(),
        )
        .unwrap();
        let before_start = Arc::clone(&supervisor).begin_scan_view(context(), scan_options());
        assert!(before_start.scan.is_none());
        assert_failure(
            before_start.failure,
            ForeignOperationFailureDomain::Lifecycle,
            "supervisor.no_active_session",
        );
        assert!(old_source.backend.commands().is_empty());
        supervisor.start().await.unwrap();
        let mut invalid = context();
        invalid.owner_generation = 0;
        let rejected = Arc::clone(&supervisor).begin_scan_view(invalid, scan_options());
        assert!(rejected.scan.is_none());
        assert_failure(
            rejected.failure,
            ForeignOperationFailureDomain::Input,
            "supervisor.invalid_context",
        );
        for field in 0..4 {
            let mut stale = context();
            match field {
                0 => stale.owner_id.push_str("-stale"),
                1 => stale.backend_id.push_str("-stale"),
                2 => stale.owner_generation += 1,
                _ => stale.recovery_attempt += 1,
            }
            let rejected = Arc::clone(&supervisor).begin_scan_view(stale, scan_options());
            assert!(rejected.scan.is_none());
            assert_failure(
                rejected.failure,
                ForeignOperationFailureDomain::Lifecycle,
                "supervisor.stale_context",
            );
        }
        assert_eq!(old_source.backend.commands().len(), 1);
        let old = Arc::clone(&supervisor)
            .begin_scan_view(context(), scan_options())
            .scan
            .expect("scan submission accepted");
        assert_started(&old).await;
        old_source.observed(completed(2)).await;
        supervisor
            .configure_old_operation_barrier(None, vec![])
            .unwrap();
        supervisor
            .observe_signal(ForeignRecoverySignal::RadioLost)
            .unwrap();
        let not_ready = Arc::clone(&supervisor).begin_scan_view(context(), scan_options());
        assert!(not_ready.scan.is_none());
        assert_failure(
            not_ready.failure,
            ForeignOperationFailureDomain::Lifecycle,
            "supervisor.recovery_not_ready",
        );
        assert!(matches!(
            old.begin_stop(),
            Err(ScanSessionError::Submission {
                error: SupervisorOperationError::RecoveryNotReady
            })
        ));
        supervisor
            .observe_signal(ForeignRecoverySignal::RadioOn)
            .unwrap();
        let plan = supervisor
            .observe_signal(ForeignRecoverySignal::SameOwnerRecoveryFailed {
                reason: "scripted owner replacement, no physical radio control".to_owned(),
            })
            .unwrap();
        assert!(matches!(
            plan.actions.as_slice(),
            [ForeignRecoveryAction::RetireOldOwner]
        ));
        supervisor.retire_for_recovery().await.unwrap();
        old_source.assert_retired();
        supervisor
            .create_fresh_owner(fresh_context.clone())
            .await
            .unwrap();
        supervisor
            .observe_signal(ForeignRecoverySignal::FreshOwnerRecovered {
                link: ForeignRecoveryLink {
                    connection_id: "fresh-connection".to_owned(),
                    gatt_session_id: "fresh-gatt".to_owned(),
                    subscription_id: "fresh-subscription".to_owned(),
                },
            })
            .unwrap();
        assert_eq!(
            supervisor.active_context().unwrap(),
            Some(fresh_context.clone())
        );
        let stale_after_recovery =
            Arc::clone(&supervisor).begin_scan_view(context(), scan_options());
        assert!(stale_after_recovery.scan.is_none());
        assert_failure(
            stale_after_recovery.failure,
            ForeignOperationFailureDomain::Lifecycle,
            "supervisor.stale_context",
        );
        let fresh = Arc::clone(&supervisor)
            .begin_scan_view(fresh_context, scan_options())
            .scan
            .expect("fresh scan submission accepted");
        assert_started(&fresh).await;
        fresh_source.observed(completed(2)).await;
        // Both owners used scan ID 1. Replacing the context would stop the NEW scan.
        assert!(matches!(
            old.begin_stop(),
            Err(ScanSessionError::Submission {
                error: SupervisorOperationError::StaleContext
            })
        ));
        assert_eq!(fresh_source.backend.commands().len(), 2);
        assert_delivery(
            old.next_event(WAIT_MS).await,
            ScanDeliveryError::OwnerRetired,
        );
        let ad = advertisement(1, 123);
        fresh_source.emit_and_observe(observed_ad(ad.clone())).await;
        assert_eq!(
            fresh.next_event(WAIT_MS).await.unwrap(),
            ScanEvent::Advertisement { advertisement: ad }
        );
        fresh.begin_stop().unwrap().wait(WAIT_MS).await.unwrap();
        fresh_source.observed(completed(3)).await;
        assert_eq!(
            fresh.next_event(WAIT_MS).await.unwrap(),
            delivered_stop(3, 1)
        );
        supervisor.shutdown().await.unwrap();
        fresh_source.assert_retired();
    });
}

#[path = "ffi_scan/view.rs"]
mod scan_view;

#[path = "ffi_scan/connection_view.rs"]
mod connection_view;

#[path = "ffi_scan/connection_end.rs"]
mod connection_end;

#[path = "ffi_scan/gatt_view.rs"]
mod gatt_view;

#[path = "ffi_scan/notification_view.rs"]
mod notification_view;
