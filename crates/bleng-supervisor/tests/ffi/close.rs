use std::sync::atomic::{AtomicUsize, Ordering};

use super::*;

const PRIVATE_DETAIL: &str = "private-owner/backend/resource-identity";

#[derive(Clone, Copy)]
enum Fault {
    None,
    Start,
    Reader,
    Resources,
    MissingResource,
    Snapshot,
    ReaderAndResources,
    YieldReader,
}

struct CloseSession {
    session: FakeForeignSession,
    fault: Fault,
    reader_drains: Arc<AtomicUsize>,
    snapshots: Arc<AtomicUsize>,
}

#[async_trait]
impl ForeignPlatformOwnerSession for CloseSession {
    fn context(&self) -> ForeignOwnerContext {
        self.session.context()
    }

    fn backend(&self) -> Arc<dyn BleBackend> {
        self.session.backend()
    }

    fn dispose_unassembled(&self) -> Result<(), SupervisorFfiError> {
        self.session.dispose_unassembled()
    }

    fn start_event_reader(
        &self,
        _: Arc<Bleng>,
        _: Arc<bleng_supervisor::ScanEventProjection>,
    ) -> Result<(), SupervisorFfiError> {
        if matches!(self.fault, Fault::Start) {
            return Err(SupervisorFfiError::Platform {
                detail: PRIVATE_DETAIL.to_owned(),
            });
        }
        Ok(())
    }

    async fn wait_event_reader(&self) -> Result<(), SupervisorFfiError> {
        self.reader_drains.fetch_add(1, Ordering::Relaxed);
        if matches!(self.fault, Fault::YieldReader) {
            tokio::task::yield_now().await;
        }
        if matches!(self.fault, Fault::Reader | Fault::ReaderAndResources) {
            return Err(SupervisorFfiError::Platform {
                detail: PRIVATE_DETAIL.to_owned(),
            });
        }
        Ok(())
    }

    async fn resource_snapshot(&self) -> Result<Vec<ForeignResourceCount>, SupervisorFfiError> {
        self.snapshots.fetch_add(1, Ordering::Relaxed);
        if matches!(self.fault, Fault::Snapshot) {
            return Err(SupervisorFfiError::Platform {
                detail: PRIVATE_DETAIL.to_owned(),
            });
        }
        self.session.resource_snapshot().await
    }

    async fn execute_recovery_action(
        &self,
        sequence: u64,
        action: ForeignPlatformRecoveryAction,
    ) -> Result<ForeignPlatformActionResult, SupervisorFfiError> {
        self.session.execute_recovery_action(sequence, action).await
    }
}

fn close_supervisor(
    fault: Fault,
) -> (
    Arc<Supervisor>,
    FakeBackendHandle,
    Arc<AtomicUsize>,
    Arc<AtomicUsize>,
) {
    let (backend, handle) = scripted_backend();
    let reader_drains = Arc::new(AtomicUsize::new(0));
    let snapshots = Arc::new(AtomicUsize::new(0));
    let mut resources = vec![
        ForeignResourceCount {
            name: "connections".to_owned(),
            count: 0,
        },
        ForeignResourceCount {
            name: "callbacks".to_owned(),
            count: 0,
        },
    ];
    if matches!(fault, Fault::Resources | Fault::ReaderAndResources) {
        resources.push(ForeignResourceCount {
            name: PRIVATE_DETAIL.to_owned(),
            count: 1,
        });
    }
    if matches!(fault, Fault::MissingResource) {
        resources.pop();
    }
    let session = Arc::new(CloseSession {
        session: FakeForeignSession {
            context: context(),
            backend: Arc::new(backend),
            resources,
        },
        fault,
        reader_drains: Arc::clone(&reader_drains),
        snapshots: Arc::clone(&snapshots),
    });
    let supervisor = Supervisor::new(
        Arc::new(FakeForeignFactory {
            session: Mutex::new(Some(session)),
        }),
        config_without_target(),
    )
    .unwrap();
    (supervisor, handle, reader_drains, snapshots)
}

fn runtime() -> tokio::runtime::Runtime {
    tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .unwrap()
}

#[test]
fn close_without_start_is_terminal_but_not_resource_zero() {
    let (supervisor, backend, drains, snapshots) = close_supervisor(Fault::None);
    let runtime = runtime();
    let view = runtime.block_on(supervisor.shutdown_view());
    assert!(view.owner_closed);
    let failure = view.failure.as_ref().unwrap();
    assert_eq!(failure.code, "no_active_session");
    assert_eq!(failure.message, "supervisor has no active session");
    assert_eq!(runtime.block_on(supervisor.shutdown_view()), view);
    assert!(runtime.block_on(supervisor.start()).is_err());
    assert_eq!(supervisor.last_retired_resources().unwrap(), None);
    assert_eq!(backend.dispose_count(), 0);
    assert_eq!(drains.load(Ordering::Relaxed), 0);
    assert_eq!(snapshots.load(Ordering::Relaxed), 0);
}

#[test]
fn close_after_real_start_failure_does_not_replace_history_or_repeat_cleanup() {
    let (supervisor, backend, drains, snapshots) = close_supervisor(Fault::Start);
    let runtime = runtime();
    let startup_error = runtime.block_on(supervisor.start()).unwrap_err();
    assert!(startup_error.to_string().contains(PRIVATE_DETAIL));
    let view = runtime.block_on(supervisor.shutdown_view());
    assert!(view.owner_closed);
    assert_eq!(view.failure.as_ref().unwrap().code, "no_active_session");
    assert_eq!(runtime.block_on(supervisor.shutdown_view()), view);
    assert!(startup_error.to_string().contains(PRIVATE_DETAIL));
    assert_eq!(supervisor.last_retired_resources().unwrap(), None);
    assert_eq!(backend.dispose_count(), 1);
    assert_eq!(drains.load(Ordering::Relaxed), 1);
    assert_eq!(snapshots.load(Ordering::Relaxed), 1);
}

#[test]
fn close_preserves_first_success_or_cleanup_failure_across_both_entry_points() {
    for (fault, expected) in [
        (Fault::None, None),
        (
            Fault::Reader,
            Some(("platform_failure", "platform session operation failed")),
        ),
        (
            Fault::Resources,
            Some((
                "native_resources_not_zero",
                "native resources remained after retirement",
            )),
        ),
        (
            Fault::MissingResource,
            Some((
                "missing_resource",
                "required resource is missing from the retirement snapshot",
            )),
        ),
        (
            Fault::Snapshot,
            Some(("platform_failure", "platform session operation failed")),
        ),
        (
            Fault::ReaderAndResources,
            Some((
                "retirement_cleanup_failed",
                "owner retirement had multiple failures",
            )),
        ),
    ] {
        for original_first in [false, true] {
            let (supervisor, backend, drains, snapshots) = close_supervisor(fault);
            let runtime = runtime();
            runtime.block_on(supervisor.start()).unwrap();
            if original_first {
                let result = runtime.block_on(supervisor.shutdown());
                assert_eq!(result.is_err(), expected.is_some());
                if matches!(
                    fault,
                    Fault::Reader | Fault::Resources | Fault::Snapshot | Fault::ReaderAndResources
                ) {
                    assert!(result.unwrap_err().to_string().contains(PRIVATE_DETAIL));
                }
            }
            let view = runtime.block_on(supervisor.shutdown_view());
            assert!(view.owner_closed);
            assert_eq!(
                view.failure
                    .as_ref()
                    .map(|failure| (failure.code.as_str(), failure.message.as_str())),
                expected,
            );
            assert_eq!(runtime.block_on(supervisor.shutdown_view()), view);
            assert!(runtime.block_on(supervisor.shutdown()).is_err());
            assert_eq!(runtime.block_on(supervisor.shutdown_view()), view);
            assert_eq!(backend.dispose_count(), 1);
            assert_eq!(drains.load(Ordering::Relaxed), 1);
            assert_eq!(snapshots.load(Ordering::Relaxed), 1);
            if expected.is_some() {
                assert_eq!(supervisor.last_retired_resources().unwrap(), None);
            }
        }
    }
}

#[test]
fn in_flight_close_is_busy_and_does_not_cache_a_retryable_failure() {
    for original_first in [false, true] {
        let (supervisor, backend, drains, snapshots) = close_supervisor(Fault::YieldReader);
        let runtime = runtime();
        runtime.block_on(async {
            supervisor.start().await.unwrap();
            tokio::time::timeout(std::time::Duration::from_secs(2), async {
                tokio::join!(biased;
                    async {
                        if original_first {
                            supervisor.shutdown().await.unwrap();
                        } else {
                            let view = supervisor.shutdown_view().await;
                            assert!(view.owner_closed);
                            assert_eq!(view.failure, None);
                        }
                    },
                    async {
                        while drains.load(Ordering::Relaxed) == 0 {
                            tokio::task::yield_now().await;
                        }
                        let busy = supervisor.shutdown_view().await;
                        assert!(!busy.owner_closed);
                        let failure = busy.failure.unwrap();
                        assert_eq!(failure.code, "supervisor_busy");
                        assert_eq!(failure.message, "supervisor operation is already in progress");
                    }
                );
            })
            .await
            .expect("close interleaving timed out");
        });
        let view = runtime.block_on(supervisor.shutdown_view());
        assert!(view.owner_closed);
        assert_eq!(view.failure, None);
        assert_eq!(backend.dispose_count(), 1);
        assert_eq!(drains.load(Ordering::Relaxed), 1);
        assert_eq!(snapshots.load(Ordering::Relaxed), 1);
    }
}

#[test]
fn original_shutdown_without_start_records_the_terminal_failure() {
    let (supervisor, backend, _, _) = close_supervisor(Fault::None);
    let runtime = runtime();
    assert!(runtime.block_on(supervisor.shutdown()).is_err());
    let view = runtime.block_on(supervisor.shutdown_view());
    assert!(view.owner_closed);
    assert_eq!(view.failure.as_ref().unwrap().code, "no_active_session");
    assert_eq!(runtime.block_on(supervisor.shutdown_view()), view);
    assert_eq!(backend.dispose_count(), 0);
}
