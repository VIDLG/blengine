#![cfg(feature = "ffi")]

use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use blehub::{
    AdapterSnapshot, AdapterState, AuthorizationState, BleBackend, BleEvent, BluetoothUuid,
    CommandId, InitializeCommand, NotificationModeRequest, ScanOptions, ScanSessionId,
    StartScanCommand,
};
use blehub_testkit::{EventScenario, FakeBackend, FakeBackendHandle, RecordedCommand};
use bleng::Bleng;
use bleng_supervisor::{
    ForeignExternalRecoverySignal, ForeignOwnerContext, ForeignPlatformActionResult,
    ForeignPlatformOwnerSession, ForeignPlatformOwnerSessionFactory, ForeignPlatformRecoveryAction,
    ForeignRecoveryAction, ForeignRecoveryLink, ForeignRecoveryObservation,
    ForeignRecoveryPolicyConfig, ForeignRecoverySignal, ForeignRecoveryTarget,
    ForeignResourceCount, ForeignSupervisorConfig, Supervisor, SupervisorFfiError,
    SupervisorOperationError,
};
use bleng_supervisor::{OwnerGeneration, RecoveryAttemptId};

fn scripted_backend() -> (FakeBackend, FakeBackendHandle) {
    FakeBackend::scripted([initialization_scenario()])
}

fn initialization_scenario() -> EventScenario {
    let initialize = InitializeCommand {
        command_id: CommandId { value: 1 },
    };
    EventScenario::emit_before_return(
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
    )
}

fn context() -> ForeignOwnerContext {
    ForeignOwnerContext {
        owner_id: "owner-ffi".to_owned(),
        backend_id: "backend-ffi".to_owned(),
        owner_generation: OwnerGeneration::new(1).unwrap().value(),
        recovery_attempt: RecoveryAttemptId::new(1).unwrap().value(),
    }
}

struct FakeForeignSession {
    context: ForeignOwnerContext,
    backend: Arc<dyn BleBackend>,
    resources: Vec<ForeignResourceCount>,
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
        _: Arc<Bleng>,
        _: Arc<bleng_supervisor::SessionEventProjection>,
    ) -> Result<(), SupervisorFfiError> {
        Ok(())
    }

    async fn wait_event_reader(&self) -> Result<(), SupervisorFfiError> {
        Ok(())
    }

    async fn resource_snapshot(&self) -> Result<Vec<ForeignResourceCount>, SupervisorFfiError> {
        Ok(self.resources.clone())
    }

    async fn execute_recovery_action(
        &self,
        sequence: u64,
        action: ForeignPlatformRecoveryAction,
    ) -> Result<ForeignPlatformActionResult, SupervisorFfiError> {
        let signal = match action {
            ForeignPlatformRecoveryAction::WaitForRadioOn => ForeignRecoverySignal::RadioOn,
            ForeignPlatformRecoveryAction::AttemptSameOwnerRecovery { .. } => {
                ForeignRecoverySignal::SameOwnerRecoveryFailed {
                    reason: "fake same-owner recovery failed".to_owned(),
                }
            }
            action => {
                return Err(SupervisorFfiError::Platform {
                    detail: format!("fake platform action is not scripted: {action:?}"),
                });
            }
        };
        Ok(ForeignPlatformActionResult { sequence, signal })
    }
}

struct FakeForeignFactory {
    session: Mutex<Option<Arc<dyn ForeignPlatformOwnerSession>>>,
}

impl ForeignPlatformOwnerSessionFactory for FakeForeignFactory {
    fn required_zero_resource_names(&self) -> Vec<String> {
        vec!["connections".to_owned(), "callbacks".to_owned()]
    }

    fn create(
        &self,
        _: ForeignOwnerContext,
    ) -> Result<Arc<dyn ForeignPlatformOwnerSession>, SupervisorFfiError> {
        self.session
            .lock()
            .unwrap()
            .take()
            .ok_or_else(|| SupervisorFfiError::Platform {
                detail: "fake factory exhausted".to_owned(),
            })
    }
}

fn recovery_target() -> ForeignRecoveryTarget {
    ForeignRecoveryTarget {
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

fn config() -> ForeignSupervisorConfig {
    ForeignSupervisorConfig {
        recovery: ForeignRecoveryPolicyConfig {
            initial_context: context(),
            old_link: Some(ForeignRecoveryLink {
                connection_id: "connection-old".to_owned(),
                gatt_session_id: "gatt-old".to_owned(),
                subscription_id: "subscription-old".to_owned(),
            }),
            expected_old_operation_ids: Vec::new(),
            allow_central_recreation: true,
            allow_fresh_owner: true,
        },
        recovery_target: Some(recovery_target()),
    }
}

fn config_without_target() -> ForeignSupervisorConfig {
    ForeignSupervisorConfig {
        recovery_target: None,
        ..config()
    }
}

#[path = "ffi/close.rs"]
mod close;
#[path = "ffi/factory.rs"]
mod factory;
#[path = "ffi/interruption.rs"]
mod interruption;

#[test]
fn public_operations_fence_context_before_dispatch_and_reuse_the_bleng_handle() {
    let scan = StartScanCommand {
        command_id: CommandId { value: 2 },
        scan_session_id: ScanSessionId { value: 1 },
        options: ScanOptions::default(),
    };
    let (backend, handle) = FakeBackend::scripted([
        initialization_scenario(),
        EventScenario::emit_before_return(
            RecordedCommand::StartScan(scan.clone()),
            vec![
                BleEvent::ScanStarted {
                    command_id: scan.command_id,
                    scan_id: scan.scan_session_id,
                },
                BleEvent::CommandCompleted {
                    command_id: scan.command_id,
                    error: None,
                },
            ],
        ),
    ]);
    let session: Arc<dyn ForeignPlatformOwnerSession> = Arc::new(FakeForeignSession {
        context: context(),
        backend: Arc::new(backend),
        resources: ["connections", "callbacks"]
            .map(|name| ForeignResourceCount {
                name: name.to_owned(),
                count: 0,
            })
            .to_vec(),
    });
    let supervisor = Supervisor::new(
        Arc::new(FakeForeignFactory {
            session: Mutex::new(Some(session)),
        }),
        config_without_target(),
    )
    .unwrap();
    assert!(matches!(
        supervisor.begin_start_scan(context(), ScanOptions::default()),
        Err(SupervisorOperationError::NoActiveSession)
    ));
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .unwrap();
    runtime.block_on(supervisor.start()).unwrap();
    let mut invalid = context();
    invalid.owner_generation = 0;
    assert!(matches!(
        supervisor.begin_start_scan(invalid, ScanOptions::default()),
        Err(SupervisorOperationError::InvalidContext { .. })
    ));
    for field in 0..4 {
        let mut stale = context();
        match field {
            0 => stale.owner_id.push_str("-old"),
            1 => stale.backend_id.push_str("-old"),
            2 => stale.owner_generation += 1,
            _ => stale.recovery_attempt += 1,
        }
        assert!(matches!(
            supervisor.begin_start_scan(stale, ScanOptions::default()),
            Err(SupervisorOperationError::StaleContext)
        ));
    }
    // None of the rejected calls consumed a command ID or reached the backend.
    assert_eq!(handle.commands().len(), 1);
    let operation: Arc<bleng::StartScanOperation> = supervisor
        .begin_start_scan(context(), scan.options.clone())
        .unwrap();
    assert_eq!(operation.command_id(), scan.command_id);
    assert_eq!(
        runtime.block_on(operation.wait(1_000)).unwrap(),
        scan.scan_session_id
    );
    assert!(matches!(
        runtime.block_on(operation.wait(1_000)),
        Err(bleng::BlengError::OperationResultConsumed)
    ));
    assert!(matches!(
        runtime.block_on(operation.cancel()),
        Err(bleng::BlengError::OperationAlreadyCompleted { command_id }) if command_id == scan.command_id
    ));
    supervisor
        .observe_external_signal(ForeignExternalRecoverySignal::RadioLost)
        .unwrap();
    assert!(matches!(
        supervisor.begin_start_scan(context(), ScanOptions::default()),
        Err(SupervisorOperationError::RecoveryNotReady)
    ));
    assert_eq!(handle.commands().len(), 2);
    runtime.block_on(supervisor.shutdown()).unwrap();
    assert!(matches!(
        supervisor.begin_start_scan(context(), ScanOptions::default()),
        Err(SupervisorOperationError::NoActiveSession)
    ));
    assert_eq!(handle.dispose_count(), 1);
    assert!(handle.contract_violations().is_empty());
}

#[test]
fn public_supervisor_allows_lifecycle_without_foreign_recovery_target() {
    let (backend, handle) = scripted_backend();
    let session: Arc<dyn ForeignPlatformOwnerSession> = Arc::new(FakeForeignSession {
        context: context(),
        backend: Arc::new(backend),
        resources: vec![
            ForeignResourceCount {
                name: "connections".to_owned(),
                count: 0,
            },
            ForeignResourceCount {
                name: "callbacks".to_owned(),
                count: 0,
            },
        ],
    });
    let factory: Arc<dyn ForeignPlatformOwnerSessionFactory> = Arc::new(FakeForeignFactory {
        session: Mutex::new(Some(session)),
    });
    let supervisor = Supervisor::new(factory, config_without_target()).unwrap();
    assert!(matches!(
        supervisor.observe_external_signal(ForeignExternalRecoverySignal::RadioLost),
        Err(SupervisorFfiError::NoActiveContext)
    ));
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .unwrap();

    runtime.block_on(supervisor.start()).unwrap();
    runtime.block_on(supervisor.shutdown()).unwrap();
    assert_eq!(handle.dispose_count(), 1);
}

#[test]
fn public_supervisor_configures_the_runtime_barrier_once() {
    let (backend, _) = scripted_backend();
    let session: Arc<dyn ForeignPlatformOwnerSession> = Arc::new(FakeForeignSession {
        context: context(),
        backend: Arc::new(backend),
        resources: vec![
            ForeignResourceCount {
                name: "connections".to_owned(),
                count: 0,
            },
            ForeignResourceCount {
                name: "callbacks".to_owned(),
                count: 0,
            },
        ],
    });
    let factory: Arc<dyn ForeignPlatformOwnerSessionFactory> = Arc::new(FakeForeignFactory {
        session: Mutex::new(Some(session)),
    });
    let mut configuration = config_without_target();
    configuration.recovery.old_link = None;
    let supervisor = Supervisor::new(factory, configuration).unwrap();
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .unwrap();

    runtime.block_on(supervisor.start()).unwrap();
    supervisor
        .configure_old_operation_barrier(None, Vec::new())
        .unwrap();
    assert!(matches!(
        supervisor.configure_old_operation_barrier(None, Vec::new()),
        Err(SupervisorFfiError::InvalidConfiguration { .. })
    ));
    assert!(matches!(
        supervisor.observe_external_signal(ForeignExternalRecoverySignal::RadioLost),
        Ok(plan) if plan.actions == vec![ForeignRecoveryAction::WaitForRadioOn]
    ));
}

#[test]
fn public_supervisor_owns_foreign_session_until_shutdown() {
    let (backend, handle) = scripted_backend();
    let session: Arc<dyn ForeignPlatformOwnerSession> = Arc::new(FakeForeignSession {
        context: context(),
        backend: Arc::new(backend),
        resources: vec![
            ForeignResourceCount {
                name: "connections".to_owned(),
                count: 0,
            },
            ForeignResourceCount {
                name: "callbacks".to_owned(),
                count: 0,
            },
        ],
    });
    let factory: Arc<dyn ForeignPlatformOwnerSessionFactory> = Arc::new(FakeForeignFactory {
        session: Mutex::new(Some(session)),
    });
    let supervisor = Supervisor::new(factory, config()).unwrap();
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .unwrap();

    runtime.block_on(supervisor.start()).unwrap();
    assert!(supervisor.is_active().unwrap());
    assert_eq!(supervisor.active_context().unwrap(), Some(context()));
    let plan = supervisor
        .observe_external_signal(ForeignExternalRecoverySignal::RadioLost)
        .unwrap();
    assert_eq!(plan.execution_context, context());
    let resources = runtime.block_on(supervisor.shutdown()).unwrap();
    assert_eq!(resources.len(), 2);
    assert!(!supervisor.is_active().unwrap());
    assert_eq!(supervisor.active_context().unwrap(), None);
    assert_eq!(handle.dispose_count(), 1);
}

#[test]
fn public_supervisor_drives_platform_actions_to_owner_boundary() {
    let (backend, handle) = scripted_backend();
    let session: Arc<dyn ForeignPlatformOwnerSession> = Arc::new(FakeForeignSession {
        context: context(),
        backend: Arc::new(backend),
        resources: vec![
            ForeignResourceCount {
                name: "connections".to_owned(),
                count: 0,
            },
            ForeignResourceCount {
                name: "callbacks".to_owned(),
                count: 0,
            },
        ],
    });
    let factory: Arc<dyn ForeignPlatformOwnerSessionFactory> = Arc::new(FakeForeignFactory {
        session: Mutex::new(Some(session)),
    });
    let mut config = config();
    config.recovery.allow_central_recreation = false;
    let supervisor = Supervisor::new(factory, config).unwrap();
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .unwrap();

    runtime.block_on(supervisor.start()).unwrap();
    supervisor
        .observe_external_signal(ForeignExternalRecoverySignal::RadioLost)
        .unwrap();
    let plan = runtime
        .block_on(supervisor.drive_platform_actions_until_boundary())
        .unwrap();
    assert!(matches!(
        plan.actions.as_slice(),
        [ForeignRecoveryAction::RetireOldOwner]
    ));
    runtime.block_on(supervisor.shutdown()).unwrap();
    assert_eq!(handle.dispose_count(), 1);
}

#[test]
fn public_supervisor_returns_fenced_observation_plan() {
    let (backend, _) = scripted_backend();
    let session: Arc<dyn ForeignPlatformOwnerSession> = Arc::new(FakeForeignSession {
        context: context(),
        backend: Arc::new(backend),
        resources: vec![
            ForeignResourceCount {
                name: "connections".to_owned(),
                count: 0,
            },
            ForeignResourceCount {
                name: "callbacks".to_owned(),
                count: 0,
            },
        ],
    });
    let factory: Arc<dyn ForeignPlatformOwnerSessionFactory> = Arc::new(FakeForeignFactory {
        session: Mutex::new(Some(session)),
    });
    let supervisor = Supervisor::new(factory, config()).unwrap();
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .unwrap();
    runtime.block_on(supervisor.start()).unwrap();

    let plan = supervisor
        .observe(ForeignRecoveryObservation {
            source_context: context(),
            signal: ForeignRecoverySignal::RadioLost,
        })
        .unwrap();

    assert_eq!(plan.sequence, 1);
    assert_eq!(plan.execution_context, context());
}
