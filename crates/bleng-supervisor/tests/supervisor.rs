use std::{
    collections::{BTreeMap, BTreeSet, VecDeque},
    sync::{
        Arc, Barrier, Mutex,
        atomic::{AtomicUsize, Ordering},
    },
};

use blehub::{
    AdapterSnapshot, AdapterState, AdvertisementObservation, AuthorizationState, BleBackend,
    BleErrorCategory, BleEvent, BleEventError, BluetoothUuid, CommandDispatchError, CommandId,
    ConnectCommand, InitializeCommand, ManufacturerData, NotificationModeRequest, Platform,
    PlatformErrorDetails, ScanOptions, ScanStopReason, ScanStopResult, ServiceData,
    StartScanCommand, WriteType,
};
use blehub_testkit::{
    CancellationPolicy, EventScenario, FakeBackend, FakeBackendHandle, RecordedCommand,
};
use bleng::BlengError;
use bleng_supervisor::{
    OwnerBackendIdentity, OwnerGeneration, RecoveryAction, RecoveryAttemptId, RecoveryContext,
    RecoveryLink, RecoveryObservation, RecoveryOperationId, RecoveryPhase, RecoveryPlan,
    RecoveryPolicyConfig, RecoverySignal,
};
use bleng_supervisor::{
    PlatformOwnerSession, PlatformOwnerSessionFactory, RecoveryTarget, ResourceSnapshot, ScanEvent,
    SessionObservationProjection, SessionSupervisor, SupervisorConfig, SupervisorError,
    SupervisorOperationError,
};

fn scan_started() -> BleEvent {
    BleEvent::ScanStarted {
        command_id: 41.into(),
        scan_id: 7.into(),
    }
}

fn captured_projection(
    captured: &Mutex<Option<Arc<SessionObservationProjection>>>,
) -> Arc<SessionObservationProjection> {
    captured.lock().unwrap().clone().expect("reader started")
}

fn scan_advertisement() -> AdvertisementObservation {
    let heart_rate = BluetoothUuid::canonicalize("180d").unwrap();
    let custom = BluetoothUuid::canonicalize("12345678-1234-5678-9ABC-DEF012345678").unwrap();
    AdvertisementObservation {
        scan_session_id: 7.into(),
        peripheral_id: 17.into(),
        local_name: Some("peer\0name".to_owned()),
        advertised_services: vec![heart_rate.clone(), custom.clone(), heart_rate.clone()],
        manufacturer_data: vec![
            ManufacturerData {
                company_id: 0x004c,
                payload: vec![0, 255, 128, 1],
            },
            ManufacturerData {
                company_id: 0x004c,
                payload: vec![],
            },
            ManufacturerData {
                company_id: u16::MAX,
                payload: vec![7, 0],
            },
        ],
        service_data: vec![
            ServiceData {
                service_uuid: custom,
                payload: vec![0, 255, 128],
            },
            ServiceData {
                service_uuid: heart_rate,
                payload: vec![],
            },
        ],
        rssi: Some(-127),
        connectable: Some(false),
    }
}

#[test]
fn event_projection_preserves_payloads_stops_and_excludes_unrelated_terminals() {
    let (session, _) = fake_session(context(), &[("connections", 0), ("callbacks", 0)]);
    let projection = Arc::clone(&session.event_projection);
    let mut supervisor = supervisor(session);
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .unwrap();
    runtime.block_on(supervisor.start()).unwrap();
    let projection = captured_projection(&projection);

    let started = projection.project(scan_started()).unwrap();
    assert_eq!(started.source_context(), &context());
    assert_eq!(
        started.event(),
        &ScanEvent::Started {
            command_id: 41.into(),
            scan_id: 7.into()
        }
    );
    assert_eq!(
        started.into_parts(),
        (
            context(),
            ScanEvent::Started {
                command_id: 41.into(),
                scan_id: 7.into()
            }
        )
    );

    let full = scan_advertisement();
    assert_eq!(
        full.advertised_services[1].value,
        "12345678-1234-5678-9abc-def012345678"
    );
    let unknown = AdvertisementObservation {
        local_name: None,
        advertised_services: vec![],
        manufacturer_data: vec![],
        service_data: vec![],
        rssi: None,
        connectable: None,
        ..full.clone()
    };
    let empty = AdvertisementObservation {
        local_name: Some(String::new()),
        rssi: Some(0),
        connectable: Some(true),
        ..unknown.clone()
    };
    for advertisement in [full, unknown, empty] {
        let envelope = projection
            .project(BleEvent::AdvertisementObserved {
                advertisement: advertisement.clone(),
            })
            .unwrap();
        assert_eq!(
            envelope.into_parts(),
            (context(), ScanEvent::Advertisement { advertisement })
        );
    }

    let error = BleEventError {
        category: BleErrorCategory::AdapterUnavailable,
        message: "radio lost\0detail".to_owned(),
        platform_details: Some(Box::new(PlatformErrorDetails {
            platform: Platform::Apple,
            code: Some("6".to_owned()),
            name: None,
            message: Some(String::new()),
            status: Some(6),
            att_code: None,
        })),
    };
    for result in [
        ScanStopResult::Ok {
            reason: ScanStopReason::Requested {
                command_id: 42.into(),
            },
        },
        ScanStopResult::Ok {
            reason: ScanStopReason::SystemSuspended,
        },
        ScanStopResult::Err {
            error: error.clone(),
        },
    ] {
        let envelope = projection
            .project(BleEvent::ScanStopped {
                scan_id: 7.into(),
                result: result.clone(),
            })
            .unwrap();
        assert_eq!(
            envelope.into_parts(),
            (
                context(),
                ScanEvent::Stopped {
                    scan_id: 7.into(),
                    result
                }
            )
        );
    }
    for unrelated in [
        BleEvent::CommandCompleted {
            command_id: 41.into(),
            error: None,
        },
        BleEvent::CommandCompleted {
            command_id: 42.into(),
            error: Some(error.clone()),
        },
        BleEvent::BackendError { error },
        BleEvent::RssiRead {
            command_id: 41.into(),
            connection_id: 7.into(),
            rssi: -45,
        },
        BleEvent::AdapterSnapshotChanged {
            command_id: Some(41.into()),
            snapshot: AdapterSnapshot {
                state: AdapterState::PoweredOff,
                scan_authorization: AuthorizationState::Authorized,
                connection_authorization: AuthorizationState::Authorized,
                name: None,
            },
        },
    ] {
        assert!(projection.project(unrelated).is_none());
    }
    runtime.block_on(supervisor.shutdown()).unwrap();
    assert!(projection.project(scan_started()).is_none());
    assert!(
        projection
            .project(BleEvent::AdvertisementObserved {
                advertisement: scan_advertisement()
            })
            .is_none()
    );
    assert!(
        projection
            .project(BleEvent::ScanStopped {
                scan_id: 7.into(),
                result: ScanStopResult::Ok {
                    reason: ScanStopReason::SystemSuspended
                }
            })
            .is_none()
    );
}

#[test]
fn event_projection_never_relabels_any_context_component() {
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .unwrap();
    let mut retained = Vec::new();
    for source_context in std::iter::once(context()).chain(changed_contexts()) {
        let (session, _) = fake_session(
            source_context.clone(),
            &[("connections", 0), ("callbacks", 0)],
        );
        let captured = Arc::clone(&session.event_projection);
        let mut supervisor = supervisor_with_sessions(
            vec![session],
            RecoveryPolicyConfig {
                initial_context: source_context.clone(),
                ..policy_config()
            },
        );
        runtime.block_on(supervisor.start()).unwrap();
        let projection = captured_projection(&captured);
        let envelope = projection
            .project(BleEvent::AdvertisementObserved {
                advertisement: scan_advertisement(),
            })
            .unwrap();
        runtime.block_on(supervisor.shutdown()).unwrap();
        assert!(projection.project(scan_started()).is_none());
        retained.push((source_context, envelope));
    }
    for (source_context, envelope) in retained {
        assert_eq!(
            envelope.into_parts(),
            (
                source_context,
                ScanEvent::Advertisement {
                    advertisement: scan_advertisement()
                }
            )
        );
    }
}

#[test]
fn event_projection_drop_race_preserves_returned_envelopes_but_revokes_future_projection() {
    let (session, _) = fake_session(context(), &[("connections", 0), ("callbacks", 0)]);
    let captured = Arc::clone(&session.event_projection);
    let mut supervisor = supervisor(session);
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .unwrap();
    runtime.block_on(supervisor.start()).unwrap();
    let projection = captured_projection(&captured);
    let before_retirement = projection.project(scan_started()).unwrap();
    let barrier = Arc::new(Barrier::new(2));
    let worker_projection = Arc::clone(&projection);
    let worker_barrier = Arc::clone(&barrier);
    let worker = std::thread::spawn(move || {
        worker_barrier.wait();
        let racing = (0..64)
            .map(|_| worker_projection.project(scan_started()))
            .collect::<Vec<_>>();
        worker_barrier.wait();
        assert!(worker_projection.project(scan_started()).is_none());
        racing
    });
    barrier.wait();
    drop(supervisor);
    barrier.wait();
    for envelope in worker
        .join()
        .unwrap()
        .into_iter()
        .flatten()
        .chain([before_retirement])
    {
        assert_eq!(envelope.source_context(), &context());
        assert_eq!(
            envelope.event(),
            &ScanEvent::Started {
                command_id: 41.into(),
                scan_id: 7.into()
            }
        );
    }
    assert!(projection.project(scan_started()).is_none());
}

#[test]
fn canceled_startup_future_revokes_a_retained_projection_without_claiming_cleanup() {
    let (mut session, _) = fake_session(context(), &[("connections", 0), ("callbacks", 0)]);
    let (backend, handle) = FakeBackend::scripted([EventScenario::accept(
        RecordedCommand::Initialize(InitializeCommand {
            command_id: 1.into(),
        }),
    )]);
    session.backend = Arc::new(backend);
    let captured = Arc::clone(&session.event_projection);
    let reader_drains = Arc::clone(&session.reader_drains);
    let mut supervisor = supervisor(session);
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .unwrap();
    runtime.block_on(async {
        let mut startup = std::pin::pin!(supervisor.start());
        tokio::select! {
            result = &mut startup => panic!("startup unexpectedly finished: {result:?}"),
            _ = wait_for_dispatches(&handle, 1) => {
                assert!(captured_projection(&captured).project(scan_started()).is_some());
            }
        }
    });
    assert!(!supervisor.is_active());
    assert!(
        captured_projection(&captured)
            .project(scan_started())
            .is_none()
    );
    assert_eq!(reader_drains.load(Ordering::Relaxed), 0);
}

#[test]
fn panicking_reader_start_revokes_a_retained_projection() {
    let (mut session, _) = fake_session(context(), &[("connections", 0), ("callbacks", 0)]);
    session.panic_event_reader_start = true;
    let captured = Arc::clone(&session.event_projection);
    let mut supervisor = supervisor(session);
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .unwrap();
    assert!(
        std::panic::catch_unwind(std::panic::AssertUnwindSafe(
            || runtime.block_on(supervisor.start())
        ))
        .is_err()
    );
    assert!(
        captured_projection(&captured)
            .project(scan_started())
            .is_none()
    );
    assert!(!supervisor.is_active());
}

fn context() -> RecoveryContext {
    RecoveryContext::new(
        OwnerBackendIdentity::new("owner-1", "backend-1").unwrap(),
        OwnerGeneration::new(1).unwrap(),
        RecoveryAttemptId::new(1).unwrap(),
    )
}

fn recovery_target() -> RecoveryTarget {
    RecoveryTarget {
        scan_options: ScanOptions::default(),
        service_uuid: BluetoothUuid {
            value: "0000180d-0000-1000-8000-00805f9b34fb".to_owned(),
        },
        characteristic_uuid: BluetoothUuid {
            value: "00002a37-0000-1000-8000-00805f9b34fb".to_owned(),
        },
        notification_mode: NotificationModeRequest::PlatformDefault,
        expected_value: vec![0x01],
    }
}

struct FakeSession {
    context: RecoveryContext,
    backend: Arc<dyn BleBackend>,
    snapshot: ResourceSnapshot,
    fail_event_reader_start: bool,
    wrong_action_sequence: bool,
    snapshot_calls: Arc<AtomicUsize>,
    event_projection: Arc<Mutex<Option<Arc<SessionObservationProjection>>>>,
    reader_drains: Arc<AtomicUsize>,
    panic_event_reader_start: bool,
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
        _owner: Arc<bleng::Bleng>,
        event_projection: Arc<SessionObservationProjection>,
    ) -> Result<(), SupervisorError> {
        assert!(event_projection.project(scan_started()).is_some());
        *self.event_projection.lock().unwrap() = Some(event_projection);
        assert!(
            !self.panic_event_reader_start,
            "event reader start panicked"
        );
        if self.fail_event_reader_start {
            return Err(SupervisorError::Platform {
                detail: "event reader start failed".to_owned(),
            });
        }
        Ok(())
    }

    fn wait_event_reader(
        &self,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<(), SupervisorError>> + Send + '_>>
    {
        Box::pin(async {
            if let Some(projection) = self.event_projection.lock().unwrap().as_ref() {
                assert!(
                    projection.project(scan_started()).is_none(),
                    "projection must be revoked before reader drain"
                );
            }
            self.reader_drains.fetch_add(1, Ordering::Relaxed);
            Ok::<(), SupervisorError>(())
        })
    }

    fn resource_snapshot(
        &self,
    ) -> std::pin::Pin<
        Box<
            dyn std::future::Future<Output = Result<ResourceSnapshot, SupervisorError>> + Send + '_,
        >,
    > {
        self.snapshot_calls.fetch_add(1, Ordering::Relaxed);
        let snapshot = self.snapshot.clone();
        Box::pin(async move { Ok(snapshot) })
    }

    fn execute_recovery_action(
        &self,
        sequence: u64,
        action: bleng_supervisor::PlatformRecoveryAction,
    ) -> std::pin::Pin<
        Box<
            dyn std::future::Future<
                    Output = Result<bleng_supervisor::PlatformActionResult, SupervisorError>,
                > + Send
                + '_,
        >,
    > {
        Box::pin(async move {
            let signal = match action {
                bleng_supervisor::PlatformRecoveryAction::WaitForRadioOn => RecoverySignal::RadioOn,
                bleng_supervisor::PlatformRecoveryAction::AwaitOldOperationTerminals {
                    remaining,
                } => {
                    let operation_id = remaining
                        .into_iter()
                        .next()
                        .expect("terminal barrier must contain an operation");
                    RecoverySignal::OldOperationTerminal { operation_id }
                }
                bleng_supervisor::PlatformRecoveryAction::AttemptSameOwnerRecovery { .. } => {
                    RecoverySignal::SameOwnerRecoveryFailed {
                        reason: "fake same-owner recovery failed".to_owned(),
                    }
                }
                bleng_supervisor::PlatformRecoveryAction::AttemptFreshOwnerRecovery { .. } => {
                    RecoverySignal::FreshOwnerRecovered {
                        link: RecoveryLink::new(
                            "connection-fresh",
                            "gatt-fresh",
                            "subscription-fresh",
                        )
                        .unwrap(),
                    }
                }
                action => {
                    return Err(SupervisorError::Platform {
                        detail: format!("fake action not scripted: {action:?}"),
                    });
                }
            };
            Ok(bleng_supervisor::PlatformActionResult {
                sequence: if self.wrong_action_sequence {
                    sequence + 1
                } else {
                    sequence
                },
                signal,
            })
        })
    }
}

struct FakeFactory {
    next: Mutex<VecDeque<FakeSession>>,
    required_zero_resource_names: BTreeSet<String>,
}

impl PlatformOwnerSessionFactory for FakeFactory {
    fn required_zero_resource_names(&self) -> BTreeSet<String> {
        self.required_zero_resource_names.clone()
    }

    fn create(
        &self,
        _: &RecoveryContext,
    ) -> Result<Box<dyn PlatformOwnerSession>, SupervisorError> {
        self.next
            .lock()
            .unwrap()
            .pop_front()
            .map(|session| Box::new(session) as Box<dyn PlatformOwnerSession>)
            .ok_or_else(|| SupervisorError::Platform {
                detail: "fake factory exhausted".to_owned(),
            })
    }
}

fn fake_factory(sessions: Vec<FakeSession>) -> FakeFactory {
    FakeFactory {
        next: Mutex::new(sessions.into_iter().collect()),
        required_zero_resource_names: ["connections", "callbacks"]
            .into_iter()
            .map(str::to_owned)
            .collect(),
    }
}

fn fake_session(
    session_context: RecoveryContext,
    resources: &[(&str, u64)],
) -> (FakeSession, blehub_testkit::FakeBackendHandle) {
    fake_session_with_initialization_failure(session_context, resources, false)
}

fn fake_session_with_initialization_failure(
    session_context: RecoveryContext,
    resources: &[(&str, u64)],
    initialization_failure: bool,
) -> (FakeSession, blehub_testkit::FakeBackendHandle) {
    fake_session_with_operations(
        session_context,
        resources,
        initialization_failure,
        [],
        CancellationPolicy::Reject,
    )
}

fn fake_session_with_operations(
    session_context: RecoveryContext,
    resources: &[(&str, u64)],
    initialization_failure: bool,
    operations: impl IntoIterator<Item = EventScenario>,
    cancellation_policy: CancellationPolicy,
) -> (FakeSession, FakeBackendHandle) {
    let initialize = InitializeCommand {
        command_id: CommandId { value: 1 },
    };
    let scenarios = if initialization_failure {
        vec![EventScenario::reject(
            RecordedCommand::Initialize(initialize),
            CommandDispatchError::InvalidState {
                detail: "scripted initialization failure".to_owned(),
            },
        )]
    } else {
        vec![EventScenario::emit_before_return(
            RecordedCommand::Initialize(initialize),
            vec![
                BleEvent::AdapterSnapshotChanged {
                    command_id: Some(initialize.command_id),
                    snapshot: AdapterSnapshot {
                        state: AdapterState::PoweredOn,
                        scan_authorization: AuthorizationState::Authorized,
                        connection_authorization: AuthorizationState::Authorized,
                        name: None,
                    },
                },
                BleEvent::CommandCompleted {
                    command_id: initialize.command_id,
                    error: None,
                },
            ],
        )]
    };
    let (backend, handle) = FakeBackend::scripted_with_cancellation(
        scenarios.into_iter().chain(operations),
        cancellation_policy,
    );
    (
        FakeSession {
            context: session_context,
            backend: Arc::new(backend),
            snapshot: resources
                .iter()
                .map(|(name, count)| ((*name).to_owned(), *count))
                .collect::<BTreeMap<_, _>>(),
            fail_event_reader_start: false,
            wrong_action_sequence: false,
            snapshot_calls: Arc::new(AtomicUsize::new(0)),
            event_projection: Arc::new(Mutex::new(None)),
            reader_drains: Arc::new(AtomicUsize::new(0)),
            panic_event_reader_start: false,
        },
        handle,
    )
}

fn policy_config() -> RecoveryPolicyConfig {
    RecoveryPolicyConfig {
        initial_context: context(),
        old_link: None,
        expected_terminals: BTreeSet::new(),
        allow_central_recreation: true,
        allow_fresh_owner: true,
    }
}

fn policy_without_central_recreation() -> RecoveryPolicyConfig {
    RecoveryPolicyConfig {
        allow_central_recreation: false,
        ..policy_config()
    }
}

fn supervisor(session: FakeSession) -> SessionSupervisor {
    supervisor_with_sessions(vec![session], policy_config())
}

fn supervisor_with_sessions(
    sessions: Vec<FakeSession>,
    config: RecoveryPolicyConfig,
) -> SessionSupervisor {
    supervisor_with_expected_terminals(sessions, config, [])
}

fn supervisor_with_expected_terminals(
    sessions: Vec<FakeSession>,
    config: RecoveryPolicyConfig,
    expected_terminals: impl IntoIterator<Item = RecoveryOperationId>,
) -> SessionSupervisor {
    supervisor_with_target(sessions, config, expected_terminals, recovery_target()).unwrap()
}

fn supervisor_with_target(
    sessions: Vec<FakeSession>,
    mut config: RecoveryPolicyConfig,
    expected_terminals: impl IntoIterator<Item = RecoveryOperationId>,
    target: RecoveryTarget,
) -> Result<SessionSupervisor, SupervisorError> {
    config.expected_terminals = expected_terminals.into_iter().collect();
    let configure_empty_barrier = config.old_link.is_none() && config.expected_terminals.is_empty();
    let mut supervisor = SessionSupervisor::new(
        Arc::new(fake_factory(sessions)),
        SupervisorConfig {
            recovery: config,
            recovery_target: Some(target),
        },
    )?;
    if configure_empty_barrier {
        supervisor.configure_old_operation_barrier(None, BTreeSet::new())?;
    }
    Ok(supervisor)
}

fn assert_submission_rejected(
    supervisor: &SessionSupervisor,
    request_context: &RecoveryContext,
    expected: SupervisorOperationError,
) {
    let errors = [
        supervisor
            .begin_start_scan(request_context, ScanOptions::default())
            .err(),
        supervisor.begin_stop_scan(request_context, 99.into()).err(),
        supervisor
            .begin_connect_view(request_context, 17.into())
            .err()
            .map(SupervisorOperationError::from),
        supervisor
            .begin_disconnect(request_context, 99.into())
            .err(),
        supervisor
            .begin_discover_gatt(request_context, 99.into())
            .err(),
        supervisor
            .begin_read_characteristic(request_context, 99.into(), 98.into(), 97.into())
            .err(),
        supervisor
            .begin_write_characteristic(
                request_context,
                99.into(),
                98.into(),
                97.into(),
                vec![7, 8],
                WriteType::WithResponse,
            )
            .err(),
        supervisor
            .begin_enable_notifications(
                request_context,
                99.into(),
                98.into(),
                97.into(),
                NotificationModeRequest::PlatformDefault,
            )
            .err(),
        supervisor
            .begin_disable_notifications(request_context, 99.into(), 98.into(), 96.into())
            .err(),
        supervisor.begin_read_rssi(request_context, 99.into()).err(),
    ];
    for error in errors {
        let error = error.expect("context guard must reject before BLeng admission");
        assert_eq!(
            std::mem::discriminant(&error),
            std::mem::discriminant(&expected),
            "{error:?}"
        );
    }
}

fn changed_contexts() -> [RecoveryContext; 4] {
    [
        RecoveryContext::new(
            OwnerBackendIdentity::new("other-owner", "backend-1").unwrap(),
            OwnerGeneration::new(1).unwrap(),
            RecoveryAttemptId::new(1).unwrap(),
        ),
        RecoveryContext::new(
            OwnerBackendIdentity::new("owner-1", "other-backend").unwrap(),
            OwnerGeneration::new(1).unwrap(),
            RecoveryAttemptId::new(1).unwrap(),
        ),
        RecoveryContext::new(
            context().owner_backend_identity().clone(),
            OwnerGeneration::new(2).unwrap(),
            RecoveryAttemptId::new(1).unwrap(),
        ),
        RecoveryContext::new(
            context().owner_backend_identity().clone(),
            OwnerGeneration::new(1).unwrap(),
            RecoveryAttemptId::new(2).unwrap(),
        ),
    ]
}

async fn wait_for_dispatches(backend: &FakeBackendHandle, count: usize) {
    tokio::time::timeout(std::time::Duration::from_secs(2), async {
        while backend.commands().len() < count {
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("backend dispatch did not arrive");
}

#[test]
fn typed_submission_context_guard_precedes_allocation_and_backend_dispatch() {
    let commands = [
        RecordedCommand::StartScan(StartScanCommand {
            command_id: 2.into(),
            scan_session_id: 1.into(),
            options: ScanOptions::default(),
        }),
        RecordedCommand::Connect(ConnectCommand {
            auto_connect: false,
            command_id: 3.into(),
            connection_id: 2.into(),
            peripheral_id: 17.into(),
        }),
    ];
    let (session, backend) = fake_session_with_operations(
        context(),
        &[("connections", 0), ("callbacks", 0)],
        false,
        commands.clone().into_iter().map(EventScenario::accept),
        CancellationPolicy::Reject,
    );
    let mut supervisor = supervisor(session);
    assert_submission_rejected(
        &supervisor,
        &changed_contexts()[0],
        SupervisorOperationError::NoActiveSession,
    );
    assert!(backend.commands().is_empty());
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .unwrap();
    runtime.block_on(supervisor.start()).unwrap();
    let initialized = backend.commands();

    for stale in changed_contexts() {
        assert_submission_rejected(&supervisor, &stale, SupervisorOperationError::StaleContext);
    }
    assert_eq!(backend.commands(), initialized);

    // No caller runtime is entered during synchronous typed submission.
    let scan: Arc<bleng::StartScanOperation> = supervisor
        .begin_start_scan(&context(), ScanOptions::default())
        .unwrap();
    // The scripted backend is ordered, but independent blocking dispatches are not.
    // Observe the first dispatch before submitting the next; neither submission
    // itself enters a caller runtime or waits for native completion.
    runtime.block_on(wait_for_dispatches(&backend, 2));
    let connect: Arc<bleng::ConnectOperation> = supervisor
        .begin_connect_view(&context(), 17.into())
        .unwrap()
        .operation;
    assert_eq!(scan.command_id(), CommandId::from(2));
    assert_eq!(connect.command_id(), CommandId::from(3));
    runtime.block_on(wait_for_dispatches(&backend, 3));
    assert_eq!(&backend.commands()[1..], &commands);
    assert!(backend.contract_violations().is_empty());

    runtime.block_on(supervisor.shutdown()).unwrap();
    assert!(matches!(
        runtime.block_on(scan.wait(2000)),
        Err(BlengError::Closed)
    ));
    assert!(matches!(
        runtime.block_on(connect.wait(2000)),
        Err(BlengError::Closed)
    ));
    assert_submission_rejected(
        &supervisor,
        &changed_contexts()[0],
        SupervisorOperationError::NoActiveSession,
    );
    assert_eq!(backend.commands().len(), 3);
    assert_eq!(backend.dispose_count(), 1);
}

#[test]
fn typed_submission_rejects_loss_waiting_recovery_and_terminal_failures() {
    for terminal in [
        RecoverySignal::PlatformTimedOut {
            reason: "scripted timeout".to_owned(),
        },
        RecoverySignal::ManualReconnectRequired {
            reason: "scripted manual reconnect".to_owned(),
        },
    ] {
        let (session, backend) = fake_session(context(), &[("connections", 0), ("callbacks", 0)]);
        let operation_id = RecoveryOperationId::new("old-operation").unwrap();
        let mut supervisor = supervisor_with_expected_terminals(
            vec![session],
            policy_config(),
            [operation_id.clone()],
        );
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_time()
            .build()
            .unwrap();
        runtime.block_on(supervisor.start()).unwrap();
        let initialized = backend.commands();
        for (signal, phase) in [
            (RecoverySignal::RadioLost, RecoveryPhase::RadioLost),
            (RecoverySignal::RadioOn, RecoveryPhase::WaitingForRadioOn),
            (
                RecoverySignal::OldOperationTerminal { operation_id },
                RecoveryPhase::RecoveringSameOwner,
            ),
        ] {
            supervisor
                .observe(RecoveryObservation::new(context(), signal))
                .unwrap();
            assert_eq!(supervisor.policy().phase(), phase);
            assert_submission_rejected(
                &supervisor,
                &context(),
                SupervisorOperationError::RecoveryNotReady,
            );
            assert_submission_rejected(
                &supervisor,
                &changed_contexts()[3],
                SupervisorOperationError::StaleContext,
            );
        }
        supervisor
            .observe(RecoveryObservation::new(context(), terminal))
            .unwrap();
        assert!(matches!(
            supervisor.policy().phase(),
            RecoveryPhase::PlatformTimeout | RecoveryPhase::ManualReconnectRequired
        ));
        assert_submission_rejected(
            &supervisor,
            &context(),
            SupervisorOperationError::RecoveryNotReady,
        );
        assert_submission_rejected(
            &supervisor,
            &changed_contexts()[0],
            SupervisorOperationError::StaleContext,
        );
        assert_eq!(backend.commands(), initialized);
        runtime.block_on(supervisor.shutdown()).unwrap();
    }
}

#[test]
fn typed_submission_resumes_after_same_owner_recovery_without_consuming_rejected_ids() {
    let command = RecordedCommand::Connect(ConnectCommand {
        auto_connect: false,
        command_id: 2.into(),
        connection_id: 1.into(),
        peripheral_id: 17.into(),
    });
    let (session, backend) = fake_session_with_operations(
        context(),
        &[("connections", 0), ("callbacks", 0)],
        false,
        [EventScenario::accept(command.clone())],
        CancellationPolicy::Reject,
    );
    let mut supervisor = supervisor(session);
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .unwrap();
    runtime.block_on(supervisor.start()).unwrap();
    for signal in [RecoverySignal::RadioLost, RecoverySignal::RadioOn] {
        supervisor
            .observe(RecoveryObservation::new(context(), signal))
            .unwrap();
        assert_submission_rejected(
            &supervisor,
            &context(),
            SupervisorOperationError::RecoveryNotReady,
        );
    }
    supervisor
        .observe(RecoveryObservation::new(
            context(),
            RecoverySignal::SameOwnerRecovered {
                link: RecoveryLink::new("new-connection", "new-gatt", "new-subscription").unwrap(),
            },
        ))
        .unwrap();
    assert_eq!(supervisor.policy().phase(), RecoveryPhase::Recovered);
    // Same-owner recovery keeps its context; only fresh-owner recovery replaces it.
    assert_eq!(supervisor.active_context(), Some(&context()));
    for stale in changed_contexts() {
        assert_submission_rejected(&supervisor, &stale, SupervisorOperationError::StaleContext);
    }
    let connect = supervisor
        .begin_connect_view(&context(), 17.into())
        .unwrap()
        .operation;
    assert_eq!(connect.command_id(), CommandId::from(2));
    runtime.block_on(wait_for_dispatches(&backend, 2));
    assert_eq!(&backend.commands()[1..], &[command]);
    runtime.block_on(supervisor.shutdown()).unwrap();
    assert!(matches!(
        runtime.block_on(connect.wait(2000)),
        Err(BlengError::Closed)
    ));
}

#[test]
fn typed_handles_preserve_unwaited_completion_and_independent_cancel_results_after_shutdown() {
    for cancellation_policy in [CancellationPolicy::Accept, CancellationPolicy::Reject] {
        let commands = [
            RecordedCommand::StartScan(StartScanCommand {
                command_id: 2.into(),
                scan_session_id: 1.into(),
                options: ScanOptions::default(),
            }),
            RecordedCommand::Connect(ConnectCommand {
                auto_connect: false,
                command_id: 3.into(),
                connection_id: 2.into(),
                peripheral_id: 17.into(),
            }),
        ];
        let (session, backend) = fake_session_with_operations(
            context(),
            &[("connections", 0), ("callbacks", 0)],
            false,
            [
                EventScenario::emit_before_return(
                    commands[0].clone(),
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
                ),
                EventScenario::accept(commands[1].clone()),
            ],
            cancellation_policy,
        );
        let mut supervisor = supervisor(session);
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_time()
            .build()
            .unwrap();
        runtime.block_on(supervisor.start()).unwrap();
        let completed = supervisor
            .begin_start_scan(&context(), ScanOptions::default())
            .unwrap();
        runtime.block_on(wait_for_dispatches(&backend, 2));
        // Observe completion without consuming the result. Scan cancellation after dispatch
        // is locally non-cancellable, so polling this receipt cannot alter the operation.
        runtime.block_on(async {
            tokio::time::timeout(std::time::Duration::from_secs(2), async {
                loop {
                    match completed.cancel().await {
                        Err(BlengError::OperationAlreadyCompleted { command_id }) => {
                            assert_eq!(command_id, completed.command_id());
                            break;
                        }
                        Err(BlengError::OperationNotCancellable { command_id }) => {
                            assert_eq!(command_id, completed.command_id());
                            tokio::task::yield_now().await;
                        }
                        other => panic!("unexpected scan cancellation receipt: {other:?}"),
                    }
                }
            })
            .await
            .expect("scan did not complete before shutdown");
        });
        let pending = supervisor
            .begin_connect_view(&context(), 17.into())
            .unwrap()
            .operation;
        runtime.block_on(wait_for_dispatches(&backend, 3));
        let cancellation = runtime.block_on(pending.cancel());
        match cancellation_policy {
            CancellationPolicy::Accept => cancellation.unwrap(),
            CancellationPolicy::Reject => assert!(
                matches!(cancellation, Err(BlengError::OperationNotCancellable { command_id }) if command_id == pending.command_id())
            ),
        }
        runtime.block_on(supervisor.shutdown()).unwrap();
        assert_eq!(
            runtime.block_on(completed.wait(2000)).unwrap(),
            blehub::ScanSessionId::from(1)
        );
        let result = runtime.block_on(pending.wait(2000));
        match cancellation_policy {
            CancellationPolicy::Accept => assert!(matches!(result, Err(BlengError::Cancelled))),
            CancellationPolicy::Reject => assert!(matches!(result, Err(BlengError::Closed))),
        }
        assert!(matches!(
            runtime.block_on(completed.wait(2000)),
            Err(BlengError::OperationResultConsumed)
        ));
        assert!(matches!(
            runtime.block_on(pending.wait(2000)),
            Err(BlengError::OperationResultConsumed)
        ));
        assert_eq!(completed.command_id(), CommandId::from(2));
        assert_eq!(pending.command_id(), CommandId::from(3));
        assert_eq!(&backend.commands()[1..], &commands);
        assert!(backend.contract_violations().is_empty());
        assert_eq!(backend.dispose_count(), 1);
    }
}

#[test]
fn runtime_barrier_configuration_is_one_shot_before_recovery() {
    let (session, _) = fake_session(context(), &[("connections", 0), ("callbacks", 0)]);
    let mut supervisor = supervisor_without_target(vec![session], policy_config()).unwrap();
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .unwrap();

    runtime.block_on(supervisor.start()).unwrap();
    assert!(matches!(
        supervisor.configure_old_operation_barrier(None, BTreeSet::new()),
        Err(SupervisorError::RecoveryConfiguration(
            bleng_supervisor::RecoveryError::OldOperationBarrierAlreadyConfigured
        ))
    ));
}

#[test]
fn supervisor_requires_resource_names_for_retirement_validation() {
    let error = SessionSupervisor::new(
        Arc::new(FakeFactory {
            next: Mutex::new(VecDeque::new()),
            required_zero_resource_names: BTreeSet::new(),
        }),
        SupervisorConfig {
            recovery: policy_config(),
            recovery_target: Some(recovery_target()),
        },
    );
    let error = match error {
        Ok(_) => panic!("an empty retirement contract must be rejected"),
        Err(error) => error,
    };
    assert!(matches!(
        error,
        SupervisorError::RequiredResourcesNotConfigured
    ));

    let error = SessionSupervisor::new(
        Arc::new(FakeFactory {
            next: Mutex::new(VecDeque::new()),
            required_zero_resource_names: [" ".to_owned()].into_iter().collect(),
        }),
        SupervisorConfig {
            recovery: policy_config(),
            recovery_target: Some(recovery_target()),
        },
    );
    let error = match error {
        Ok(_) => panic!("blank resource names must not weaken the retirement contract"),
        Err(error) => error,
    };
    assert!(matches!(error, SupervisorError::BlankRequiredResourceName));
}

fn drive_platform_actions(
    runtime: &tokio::runtime::Runtime,
    supervisor: &mut SessionSupervisor,
) -> RecoveryPlan {
    runtime
        .block_on(supervisor.drive_platform_actions_until_boundary())
        .unwrap()
}

fn supervisor_without_target(
    sessions: Vec<FakeSession>,
    config: RecoveryPolicyConfig,
) -> Result<SessionSupervisor, SupervisorError> {
    let configure_empty_barrier = config.old_link.is_none() && config.expected_terminals.is_empty();
    let mut supervisor = SessionSupervisor::new(
        Arc::new(fake_factory(sessions)),
        SupervisorConfig {
            recovery: config,
            recovery_target: None,
        },
    )?;
    if configure_empty_barrier {
        supervisor.configure_old_operation_barrier(None, BTreeSet::new())?;
    }
    Ok(supervisor)
}

#[test]
fn recovery_target_canonicalizes_short_uuid_forms() {
    let mut target = recovery_target();
    target.service_uuid.value = "180d".to_owned();
    target.characteristic_uuid.value = "2a37".to_owned();
    target.scan_options.service_uuids = vec![BluetoothUuid {
        value: "180f".to_owned(),
    }];

    let target = target.canonicalized().unwrap();
    assert_eq!(
        target.service_uuid.value,
        "0000180d-0000-1000-8000-00805f9b34fb"
    );
    assert_eq!(
        target.characteristic_uuid.value,
        "00002a37-0000-1000-8000-00805f9b34fb"
    );
    assert_eq!(
        target.scan_options.service_uuids[0].value,
        "0000180f-0000-1000-8000-00805f9b34fb"
    );
}

#[test]
fn assembly_failure_still_snapshots_and_disposes_the_backend() {
    let (mut session, backend) = fake_session(context(), &[("connections", 0), ("callbacks", 0)]);
    session.fail_event_reader_start = true;
    let projection = Arc::clone(&session.event_projection);
    let reader_drains = Arc::clone(&session.reader_drains);
    let snapshot_calls = Arc::clone(&session.snapshot_calls);
    let mut supervisor = supervisor(session);
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .unwrap();

    assert!(matches!(
        runtime.block_on(supervisor.start()),
        Err(SupervisorError::Platform { .. })
    ));
    assert!(!supervisor.is_active());
    assert_eq!(snapshot_calls.load(Ordering::Relaxed), 1);
    assert_eq!(reader_drains.load(Ordering::Relaxed), 1);
    assert!(
        captured_projection(&projection)
            .project(scan_started())
            .is_none()
    );
    assert_eq!(backend.dispose_count(), 1);
}

#[test]
fn initialization_failure_still_drains_and_disposes_the_owner() {
    let (session, backend) = fake_session_with_initialization_failure(
        context(),
        &[("connections", 0), ("callbacks", 0)],
        true,
    );
    let projection = Arc::clone(&session.event_projection);
    let reader_drains = Arc::clone(&session.reader_drains);
    let snapshot_calls = Arc::clone(&session.snapshot_calls);
    let mut supervisor = supervisor(session);
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .unwrap();

    let error = runtime
        .block_on(supervisor.start())
        .expect_err("initialization should fail");

    assert!(matches!(error, SupervisorError::Bleng(_)));
    assert!(!supervisor.is_active());
    assert_eq!(snapshot_calls.load(Ordering::Relaxed), 1);
    assert_eq!(reader_drains.load(Ordering::Relaxed), 1);
    assert!(
        captured_projection(&projection)
            .project(scan_started())
            .is_none()
    );
    assert_eq!(backend.dispose_count(), 1);
}

#[test]
fn fresh_owner_is_assembled_only_after_old_owner_retirement() {
    let (old_session, old_backend) = fake_session_with_operations(
        context(),
        &[("connections", 0), ("callbacks", 0)],
        false,
        [EventScenario::accept(RecordedCommand::Connect(
            ConnectCommand {
                auto_connect: false,
                command_id: 2.into(),
                connection_id: 1.into(),
                peripheral_id: 17.into(),
            },
        ))],
        CancellationPolicy::Reject,
    );
    let fresh_context = RecoveryContext::new(
        OwnerBackendIdentity::new("owner-2", "backend-2").unwrap(),
        OwnerGeneration::new(2).unwrap(),
        RecoveryAttemptId::new(1).unwrap(),
    );
    let (fresh_session, fresh_backend) = fake_session_with_operations(
        fresh_context.clone(),
        &[("connections", 0), ("callbacks", 0)],
        false,
        [EventScenario::accept(RecordedCommand::StartScan(
            StartScanCommand {
                command_id: 2.into(),
                scan_session_id: 1.into(),
                options: ScanOptions::default(),
            },
        ))],
        CancellationPolicy::Reject,
    );
    let old_projection = Arc::clone(&old_session.event_projection);
    let fresh_projection = Arc::clone(&fresh_session.event_projection);
    let mut supervisor = supervisor_with_sessions(
        vec![old_session, fresh_session],
        policy_without_central_recreation(),
    );
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .unwrap();

    runtime.block_on(supervisor.start()).unwrap();
    let old_projection = captured_projection(&old_projection);
    let old_advertisement = old_projection
        .project(BleEvent::AdvertisementObserved {
            advertisement: scan_advertisement(),
        })
        .unwrap();
    let old_pending = supervisor
        .begin_connect_view(&context(), 17.into())
        .unwrap()
        .operation;
    runtime.block_on(wait_for_dispatches(&old_backend, 2));
    supervisor
        .observe(RecoveryObservation::new(
            context(),
            RecoverySignal::RadioLost,
        ))
        .unwrap();
    let plan = drive_platform_actions(&runtime, &mut supervisor);
    assert!(matches!(
        plan.actions().as_slice(),
        [RecoveryAction::RetireOldOwner]
    ));

    let plan = runtime.block_on(supervisor.retire_for_recovery()).unwrap();
    assert!(old_projection.project(scan_started()).is_none());
    assert!(matches!(
        runtime.block_on(old_pending.wait(2000)),
        Err(BlengError::Closed)
    ));
    assert_eq!(old_pending.command_id(), CommandId::from(2));
    assert_submission_rejected(
        &supervisor,
        &context(),
        SupervisorOperationError::NoActiveSession,
    );
    assert!(matches!(
        plan.actions().as_slice(),
        [RecoveryAction::CreateFreshOwner]
    ));

    let plan = runtime
        .block_on(supervisor.create_fresh_owner(fresh_context.clone()))
        .unwrap();
    assert!(matches!(
        plan.actions().as_slice(),
        [RecoveryAction::AttemptFreshOwnerRecovery]
    ));
    assert_eq!(supervisor.active_context(), Some(&fresh_context));
    let fresh_projection = captured_projection(&fresh_projection);
    let fresh_advertisement = fresh_projection
        .project(BleEvent::AdvertisementObserved {
            advertisement: scan_advertisement(),
        })
        .unwrap();
    assert_eq!(old_advertisement.event(), fresh_advertisement.event());
    assert_eq!(old_advertisement.source_context(), &context());
    assert_eq!(fresh_advertisement.source_context(), &fresh_context);
    assert_ne!(
        old_advertisement.source_context(),
        fresh_advertisement.source_context()
    );
    assert!(
        old_projection
            .project(BleEvent::AdvertisementObserved {
                advertisement: scan_advertisement(),
            })
            .is_none()
    );
    assert_submission_rejected(
        &supervisor,
        &fresh_context,
        SupervisorOperationError::RecoveryNotReady,
    );
    assert_submission_rejected(
        &supervisor,
        &context(),
        SupervisorOperationError::StaleContext,
    );
    let plan = drive_platform_actions(&runtime, &mut supervisor);
    assert!(plan.actions().is_empty());
    assert_eq!(supervisor.policy().phase(), RecoveryPhase::Recovered);
    assert_submission_rejected(
        &supervisor,
        &context(),
        SupervisorOperationError::StaleContext,
    );
    let fresh_scan = supervisor
        .begin_start_scan(&fresh_context, ScanOptions::default())
        .unwrap();
    assert_eq!(fresh_scan.command_id(), CommandId::from(2));
    runtime.block_on(wait_for_dispatches(&fresh_backend, 2));
    runtime.block_on(supervisor.shutdown()).unwrap();
    assert!(matches!(
        runtime.block_on(fresh_scan.wait(2000)),
        Err(BlengError::Closed)
    ));
    assert_eq!(old_backend.commands().len(), 2);
    assert_eq!(fresh_backend.commands().len(), 2);
    assert!(old_backend.contract_violations().is_empty());
    assert!(fresh_backend.contract_violations().is_empty());
    assert_eq!(old_backend.dispose_count(), 1);
    assert_eq!(fresh_backend.dispose_count(), 1);
    assert!(fresh_projection.project(scan_started()).is_none());
}

#[test]
fn closed_query_distinguishes_unstarted_active_and_failed_cleanup() {
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .unwrap();
    for start in [false, true] {
        let (session, backend) = fake_session(context(), &[("connections", 1), ("callbacks", 0)]);
        let mut supervisor = supervisor(session);
        assert!(!supervisor.is_closed());
        if start {
            runtime.block_on(supervisor.start()).unwrap();
            assert!(!supervisor.is_closed());
        }
        let error = runtime.block_on(supervisor.shutdown()).unwrap_err();
        assert!(matches!(
            (start, error),
            (false, SupervisorError::NoActiveSession)
                | (true, SupervisorError::NativeResourcesNotZero { .. })
        ));
        assert!(supervisor.is_closed());
        assert!(matches!(
            runtime.block_on(supervisor.shutdown()),
            Err(SupervisorError::NoActiveSession)
        ));
        assert!(supervisor.is_closed());
        assert_eq!(backend.dispose_count(), usize::from(start));
    }
}

#[test]
fn start_assembles_one_owner_and_shutdown_disposes_backend_once() {
    let (session, backend) = fake_session(context(), &[("connections", 0), ("callbacks", 0)]);
    let projection = Arc::clone(&session.event_projection);
    let reader_drains = Arc::clone(&session.reader_drains);
    let mut supervisor = supervisor(session);
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .unwrap();

    runtime.block_on(supervisor.start()).unwrap();
    assert!(supervisor.is_active());
    assert_eq!(supervisor.active_context(), Some(&context()));

    let snapshot = runtime.block_on(supervisor.shutdown()).unwrap();
    assert!(supervisor.is_closed());
    assert_eq!(snapshot.get("connections"), Some(&0));
    assert!(!supervisor.is_active());
    assert_eq!(reader_drains.load(Ordering::Relaxed), 1);
    assert!(
        captured_projection(&projection)
            .project(scan_started())
            .is_none()
    );
    assert_eq!(backend.dispose_count(), 1);
}

#[test]
fn initial_start_is_one_shot_even_after_shutdown() {
    let (session, backend) = fake_session(context(), &[("connections", 0), ("callbacks", 0)]);
    let mut supervisor = supervisor(session);
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .unwrap();

    runtime.block_on(supervisor.start()).unwrap();
    runtime.block_on(supervisor.shutdown()).unwrap();
    assert!(matches!(
        runtime.block_on(supervisor.start()),
        Err(SupervisorError::AlreadyStarted)
    ));
    assert!(matches!(
        supervisor.observe(RecoveryObservation::new(
            context(),
            RecoverySignal::RadioLost,
        )),
        Err(SupervisorError::NoActiveSession)
    ));
    assert_eq!(backend.dispose_count(), 1);
}

#[test]
fn shutdown_rejects_nonzero_resources_after_bleng_cleanup() {
    let (session, backend) = fake_session(context(), &[("connections", 1), ("callbacks", 0)]);
    let mut supervisor = supervisor(session);
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .unwrap();

    runtime.block_on(supervisor.start()).unwrap();
    assert!(matches!(
        runtime.block_on(supervisor.shutdown()),
        Err(SupervisorError::NativeResourcesNotZero { .. })
    ));
    assert_eq!(backend.dispose_count(), 1);
}

#[test]
fn observation_before_initial_owner_is_rejected_without_advancing_policy() {
    let (session, _) = fake_session(context(), &[("connections", 0), ("callbacks", 0)]);
    let mut supervisor = supervisor(session);

    assert!(matches!(
        supervisor.observe(RecoveryObservation::new(
            context(),
            RecoverySignal::RadioLost,
        )),
        Err(SupervisorError::NoActiveSession)
    ));
    assert_eq!(supervisor.policy().phase(), RecoveryPhase::Active);
}

#[test]
fn observation_uses_the_policy_and_returns_a_fenced_plan() {
    let (session, _) = fake_session(context(), &[("connections", 0), ("callbacks", 0)]);
    let mut supervisor = supervisor(session);
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .unwrap();
    runtime.block_on(supervisor.start()).unwrap();

    let plan = supervisor
        .observe(RecoveryObservation::new(
            context(),
            RecoverySignal::RadioLost,
        ))
        .unwrap();

    assert_eq!(plan.execution_context(), &context());
    assert_eq!(plan.sequence(), 1);
    assert_eq!(supervisor.policy().phase(), RecoveryPhase::RadioLost);
}

#[test]
fn lifecycle_without_recovery_target_rejects_only_recovery_execution() {
    let (session, backend) = fake_session(context(), &[("connections", 0), ("callbacks", 0)]);
    let mut supervisor =
        supervisor_without_target(vec![session], policy_without_central_recreation()).unwrap();
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .unwrap();

    runtime.block_on(supervisor.start()).unwrap();
    supervisor
        .observe(RecoveryObservation::new(
            context(),
            RecoverySignal::RadioLost,
        ))
        .unwrap();
    runtime
        .block_on(supervisor.execute_platform_action())
        .unwrap();
    let error = runtime
        .block_on(supervisor.execute_platform_action())
        .expect_err("missing recovery target must reject platform execution");
    assert!(matches!(
        error,
        SupervisorError::RecoveryTargetMissing { .. }
    ));

    runtime.block_on(supervisor.shutdown()).unwrap();
    assert_eq!(backend.dispose_count(), 1);
}

#[test]
fn invalid_recovery_target_is_rejected_before_platform_execution() {
    let (session, _backend) = fake_session(context(), &[("connections", 0), ("callbacks", 0)]);
    let mut target = recovery_target();
    target.service_uuid.value.clear();

    assert!(matches!(
        supervisor_with_target(
            vec![session],
            policy_without_central_recreation(),
            [],
            target,
        ),
        Err(SupervisorError::InvalidRecoveryTarget(_))
    ));
}

#[test]
fn terminal_barrier_is_drained_through_the_platform_action_executor() {
    let (session, backend) = fake_session(context(), &[("connections", 0), ("callbacks", 0)]);
    let mut supervisor = supervisor_with_expected_terminals(
        vec![session],
        policy_without_central_recreation(),
        [RecoveryOperationId::new("operation-1").unwrap()],
    );
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .unwrap();

    runtime.block_on(supervisor.start()).unwrap();
    supervisor
        .observe(RecoveryObservation::new(
            context(),
            RecoverySignal::RadioLost,
        ))
        .unwrap();

    let plan = runtime
        .block_on(supervisor.drive_platform_actions_until_boundary())
        .unwrap();
    assert!(matches!(
        plan.actions().as_slice(),
        [bleng_supervisor::RecoveryAction::RetireOldOwner]
    ));

    runtime.block_on(supervisor.shutdown()).unwrap();
    assert_eq!(backend.dispose_count(), 1);
}

#[test]
fn platform_action_executor_feeds_signal_back_into_the_policy() {
    let (session, backend) = fake_session(context(), &[("connections", 0), ("callbacks", 0)]);
    let mut supervisor =
        supervisor_with_sessions(vec![session], policy_without_central_recreation());
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .unwrap();

    runtime.block_on(supervisor.start()).unwrap();
    supervisor
        .observe(RecoveryObservation::new(
            context(),
            RecoverySignal::RadioLost,
        ))
        .unwrap();

    let plan = runtime
        .block_on(supervisor.execute_platform_action())
        .unwrap();
    assert!(matches!(
        plan.actions().as_slice(),
        [bleng_supervisor::RecoveryAction::AttemptSameOwnerRecovery {
            after_central_recreation: false
        }]
    ));

    let plan = runtime
        .block_on(supervisor.execute_platform_action())
        .unwrap();
    assert!(matches!(
        plan.actions().as_slice(),
        [bleng_supervisor::RecoveryAction::RetireOldOwner]
    ));

    runtime.block_on(supervisor.shutdown()).unwrap();
    assert_eq!(backend.dispose_count(), 1);
}

#[test]
fn platform_error_consumes_pending_action_without_implicit_retry() {
    let (session, _backend) = fake_session(context(), &[("connections", 0), ("callbacks", 0)]);
    let mut supervisor = supervisor_with_sessions(vec![session], policy_config());
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .unwrap();

    runtime.block_on(supervisor.start()).unwrap();
    supervisor
        .observe(RecoveryObservation::new(
            context(),
            RecoverySignal::RadioLost,
        ))
        .unwrap();
    runtime
        .block_on(supervisor.execute_platform_action())
        .unwrap();
    let plan = runtime
        .block_on(supervisor.execute_platform_action())
        .unwrap();
    assert!(matches!(
        plan.actions().as_slice(),
        [bleng_supervisor::RecoveryAction::RecreateCentral]
    ));

    assert!(matches!(
        runtime.block_on(supervisor.execute_platform_action()),
        Err(SupervisorError::Platform { .. })
    ));
    assert!(matches!(
        runtime.block_on(supervisor.execute_platform_action()),
        Err(SupervisorError::RecoveryActionNotPending { .. })
    ));
}

#[test]
fn stale_platform_action_result_is_rejected_before_policy_advances() {
    let (mut session, backend) = fake_session(context(), &[("connections", 0), ("callbacks", 0)]);
    session.wrong_action_sequence = true;
    let mut supervisor = supervisor(session);
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .unwrap();

    runtime.block_on(supervisor.start()).unwrap();
    supervisor
        .observe(RecoveryObservation::new(
            context(),
            RecoverySignal::RadioLost,
        ))
        .unwrap();

    assert!(matches!(
        runtime.block_on(supervisor.execute_platform_action()),
        Err(SupervisorError::RecoveryActionSequenceMismatch {
            expected: 1,
            received: 2
        })
    ));
    assert_eq!(supervisor.policy().phase(), RecoveryPhase::RadioLost);
    runtime.block_on(supervisor.shutdown()).unwrap();
    assert_eq!(backend.dispose_count(), 1);
}

#[test]
fn retirement_requires_the_current_plan_to_authorize_old_owner_retirement() {
    let (session, backend) = fake_session(context(), &[("connections", 0), ("callbacks", 0)]);
    let mut supervisor = supervisor(session);
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .unwrap();

    runtime.block_on(supervisor.start()).unwrap();
    supervisor
        .observe(RecoveryObservation::new(
            context(),
            RecoverySignal::RadioLost,
        ))
        .unwrap();
    supervisor
        .observe(RecoveryObservation::new(context(), RecoverySignal::RadioOn))
        .unwrap();

    assert!(matches!(
        runtime.block_on(supervisor.retire_for_recovery()),
        Err(SupervisorError::RecoveryActionNotPending {
            action: "RetireOldOwner"
        })
    ));
    runtime.block_on(supervisor.shutdown()).unwrap();
    assert_eq!(backend.dispose_count(), 1);
}

#[test]
fn context_mismatch_is_rejected_before_backend_assembly_and_disposed_once() {
    let (session, backend) = fake_session(
        RecoveryContext::new(
            OwnerBackendIdentity::new("owner-other", "backend-other").unwrap(),
            OwnerGeneration::new(2).unwrap(),
            RecoveryAttemptId::new(1).unwrap(),
        ),
        &[("connections", 0), ("callbacks", 0)],
    );
    let mut supervisor = supervisor(session);
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .unwrap();

    assert!(matches!(
        runtime.block_on(supervisor.start()),
        Err(SupervisorError::PlatformContextMismatch)
    ));
    assert!(!supervisor.is_active());
    assert_eq!(backend.dispose_count(), 1);
}
