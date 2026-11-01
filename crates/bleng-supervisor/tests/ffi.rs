#![cfg(feature = "ffi")]

use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use blehub::{BleBackend, BluetoothUuid, NotificationModeRequest, ScanOptions};
use blehub_testkit::FakeBackend;
use bleng::Bleng;
use bleng_supervisor::{
    ForeignOwnerContext, ForeignPlatformActionResult, ForeignPlatformOwnerSession,
    ForeignPlatformOwnerSessionFactory, ForeignPlatformRecoveryAction, ForeignRecoveryObservation,
    ForeignRecoveryPolicyConfig, ForeignRecoverySignal, ForeignRecoveryTarget,
    ForeignResourceCount, ForeignSupervisorConfig, Supervisor, SupervisorFfiError,
};
use bleng_supervisor::{OwnerGeneration, RecoveryAttemptId};

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

    fn start_event_reader(&self, _: Arc<Bleng>) -> Result<(), SupervisorFfiError> {
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
        _sequence: u64,
        _action: ForeignPlatformRecoveryAction,
    ) -> Result<ForeignPlatformActionResult, SupervisorFfiError> {
        Err(SupervisorFfiError::Platform {
            detail: "fake platform action is not scripted".to_owned(),
        })
    }
}

struct FakeForeignFactory {
    session: Mutex<Option<Arc<dyn ForeignPlatformOwnerSession>>>,
}

impl ForeignPlatformOwnerSessionFactory for FakeForeignFactory {
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
            old_link: None,
            expected_old_operation_ids: Vec::new(),
            allow_central_recreation: true,
            allow_fresh_owner: true,
        },
        recovery_target: recovery_target(),
        required_zero_resource_names: vec!["connections".to_owned(), "callbacks".to_owned()],
    }
}

#[test]
fn public_supervisor_owns_foreign_session_until_shutdown() {
    let (backend, handle) = FakeBackend::scripted([]);
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
    let resources = runtime.block_on(supervisor.shutdown()).unwrap();
    assert_eq!(resources.len(), 2);
    assert!(!supervisor.is_active().unwrap());
    assert_eq!(handle.dispose_count(), 1);
}

#[test]
fn public_supervisor_returns_fenced_observation_plan() {
    let (backend, _) = FakeBackend::scripted([]);
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
