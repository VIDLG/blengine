use std::{
    collections::{BTreeMap, BTreeSet, VecDeque},
    sync::{
        Arc, Mutex,
        atomic::{AtomicUsize, Ordering},
    },
};

use blehub::{BleBackend, BluetoothUuid, NotificationModeRequest, ScanOptions};
use blehub_testkit::FakeBackend;
use bleng_supervisor::{
    OwnerBackendIdentity, OwnerGeneration, RecoveryAttemptId, RecoveryContext, RecoveryObservation,
    RecoveryOperationId, RecoveryPhase, RecoveryPolicyConfig, RecoverySignal,
};
use bleng_supervisor::{
    PlatformOwnerSession, PlatformOwnerSessionFactory, RecoveryTarget, ResourceSnapshot,
    SessionSupervisor, SupervisorConfig, SupervisorError,
};

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

    fn start_event_reader(&self, _owner: Arc<bleng::Bleng>) -> Result<(), SupervisorError> {
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
        Box::pin(async { Ok::<(), SupervisorError>(()) })
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
}

impl PlatformOwnerSessionFactory for FakeFactory {
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

fn fake_session(
    session_context: RecoveryContext,
    resources: &[(&str, u64)],
) -> (FakeSession, blehub_testkit::FakeBackendHandle) {
    let (backend, handle) = FakeBackend::scripted([]);
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
    SessionSupervisor::new(
        Arc::new(FakeFactory {
            next: Mutex::new(sessions.into_iter().collect()),
        }),
        SupervisorConfig {
            recovery: config,
            required_zero_resource_names: ["connections".to_owned(), "callbacks".to_owned()]
                .into_iter()
                .collect(),
            recovery_target: target,
        },
    )
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
    assert_eq!(backend.dispose_count(), 1);
}

#[test]
fn fresh_owner_is_assembled_only_after_old_owner_retirement() {
    let (old_session, old_backend) =
        fake_session(context(), &[("connections", 0), ("callbacks", 0)]);
    let fresh_context = RecoveryContext::new(
        OwnerBackendIdentity::new("owner-2", "backend-2").unwrap(),
        OwnerGeneration::new(2).unwrap(),
        RecoveryAttemptId::new(1).unwrap(),
    );
    let (fresh_session, fresh_backend) = fake_session(
        fresh_context.clone(),
        &[("connections", 0), ("callbacks", 0)],
    );
    let mut supervisor = supervisor_with_sessions(
        vec![old_session, fresh_session],
        policy_without_central_recreation(),
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
    supervisor
        .observe(RecoveryObservation::new(context(), RecoverySignal::RadioOn))
        .unwrap();
    supervisor
        .observe(RecoveryObservation::new(
            context(),
            RecoverySignal::SameOwnerRecoveryFailed {
                reason: "same owner unavailable".to_owned(),
            },
        ))
        .unwrap();
    let plan = runtime.block_on(supervisor.retire_for_recovery()).unwrap();
    assert!(matches!(
        plan.actions().as_slice(),
        [bleng_supervisor::RecoveryAction::CreateFreshOwner]
    ));

    let plan = runtime
        .block_on(supervisor.create_fresh_owner(fresh_context.clone()))
        .unwrap();
    assert!(matches!(
        plan.actions().as_slice(),
        [bleng_supervisor::RecoveryAction::AttemptFreshOwnerRecovery]
    ));
    assert_eq!(supervisor.active_context(), Some(&fresh_context));
    runtime.block_on(supervisor.shutdown()).unwrap();
    assert_eq!(old_backend.dispose_count(), 1);
    assert_eq!(fresh_backend.dispose_count(), 1);
}

#[test]
fn start_assembles_one_owner_and_shutdown_disposes_backend_once() {
    let (session, backend) = fake_session(context(), &[("connections", 0), ("callbacks", 0)]);
    let mut supervisor = supervisor(session);
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .unwrap();

    runtime.block_on(supervisor.start()).unwrap();
    assert!(supervisor.is_active());
    assert_eq!(supervisor.active_context(), Some(&context()));

    let snapshot = runtime.block_on(supervisor.shutdown()).unwrap();
    assert_eq!(snapshot.get("connections"), Some(&0));
    assert!(!supervisor.is_active());
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
        .block_on(supervisor.execute_platform_action())
        .unwrap();
    assert!(matches!(
        plan.actions().as_slice(),
        [bleng_supervisor::RecoveryAction::AwaitOldOperationTerminals { .. }]
    ));

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
