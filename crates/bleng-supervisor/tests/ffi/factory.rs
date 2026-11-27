use std::sync::atomic::{AtomicUsize, Ordering};

use super::*;
use bleng_supervisor::{
    ForeignPlatformOwnerSessionFactoryAdapter, PlatformOwnerSessionFactory, RecoveryContext,
    SupervisorError,
};

struct RejectedSession {
    context: ForeignOwnerContext,
    disposals: Arc<AtomicUsize>,
    cleanup_fails: bool,
}

#[async_trait]
impl ForeignPlatformOwnerSession for RejectedSession {
    fn context(&self) -> ForeignOwnerContext {
        self.context.clone()
    }

    fn backend(&self) -> Arc<dyn BleBackend> {
        panic!("a rejected session must never enter backend transfer")
    }

    fn dispose_unassembled(&self) -> Result<(), SupervisorFfiError> {
        self.disposals.fetch_add(1, Ordering::SeqCst);
        if self.cleanup_fails {
            Err(SupervisorFfiError::Platform {
                detail: "scripted unassembled cleanup failure".to_owned(),
            })
        } else {
            Ok(())
        }
    }

    fn start_event_reader(
        &self,
        _: Arc<Bleng>,
        _: Arc<bleng_supervisor::SessionEventProjection>,
    ) -> Result<(), SupervisorFfiError> {
        panic!("a rejected session must never start its reader")
    }

    async fn wait_event_reader(&self) -> Result<(), SupervisorFfiError> {
        panic!("an unassembled session has no reader")
    }

    async fn resource_snapshot(&self) -> Result<Vec<ForeignResourceCount>, SupervisorFfiError> {
        panic!("rejection must not claim a resource-zero snapshot")
    }

    async fn execute_recovery_action(
        &self,
        _: u64,
        _: ForeignPlatformRecoveryAction,
    ) -> Result<ForeignPlatformActionResult, SupervisorFfiError> {
        panic!("a rejected session cannot execute recovery")
    }
}

#[test]
fn rejected_foreign_context_disposes_once_and_retains_primary_and_cleanup_errors() {
    for invalid in [false, true] {
        for cleanup_fails in [false, true] {
            for public_start in [false, true] {
                let mut actual = context();
                actual.owner_generation = if invalid { 0 } else { 2 };
                let primary = if invalid {
                    RecoveryContext::try_from(actual.clone())
                        .unwrap_err()
                        .to_string()
                } else {
                    SupervisorError::PlatformContextMismatch.to_string()
                };
                let disposals = Arc::new(AtomicUsize::new(0));
                // A retained foreign reference must not substitute for explicit disposal.
                let session = Arc::new(RejectedSession {
                    context: actual,
                    disposals: Arc::clone(&disposals),
                    cleanup_fails,
                });
                let factory = Arc::new(FakeForeignFactory {
                    session: Mutex::new(Some(session.clone())),
                });
                let error = if public_start {
                    let supervisor = Supervisor::new(factory, config_without_target()).unwrap();
                    let runtime = tokio::runtime::Builder::new_current_thread()
                        .enable_time()
                        .build()
                        .unwrap();
                    let error = runtime.block_on(async {
                        tokio::time::timeout(std::time::Duration::from_secs(3), supervisor.start())
                            .await
                            .unwrap()
                            .unwrap_err()
                    });
                    assert_eq!(supervisor.active_context().unwrap(), None);
                    assert_eq!(supervisor.last_retired_resources().unwrap(), None);
                    let close = runtime.block_on(supervisor.shutdown_view());
                    assert!(close.owner_closed && close.failure.is_some());
                    error.to_string()
                } else {
                    let adapter = ForeignPlatformOwnerSessionFactoryAdapter::new(factory);
                    let error = match adapter.create(&context().try_into().unwrap()) {
                        Ok(_) => panic!("invalid/mismatched context was accepted"),
                        Err(error) => error,
                    };
                    if cleanup_fails {
                        assert!(matches!(&error, SupervisorError::StartupCleanup { .. }));
                    } else if !invalid {
                        assert!(matches!(&error, SupervisorError::PlatformContextMismatch));
                    }
                    error.to_string()
                };
                assert_eq!(
                    disposals.load(Ordering::SeqCst),
                    1,
                    "invalid={invalid}, cleanup_fails={cleanup_fails}, public_start={public_start}"
                );
                assert!(error.contains(&primary), "missing primary: {error}");
                assert_eq!(
                    error.contains("scripted unassembled cleanup failure"),
                    cleanup_fails
                );
                drop(session);
                assert_eq!(disposals.load(Ordering::SeqCst), 1);
            }
        }
    }
}
