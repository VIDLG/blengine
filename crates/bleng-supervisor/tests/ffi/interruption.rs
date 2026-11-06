use std::{
    future::Future,
    panic::{AssertUnwindSafe, catch_unwind},
    sync::atomic::{AtomicUsize, Ordering},
    time::Duration,
};

use super::*;
use tokio::sync::Notify;

const DEADLINE: Duration = Duration::from_secs(3);

#[derive(Default)]
struct Barrier {
    entered: Notify,
    release: Notify,
    calls: AtomicUsize,
}

impl Barrier {
    async fn wait(&self, panic_after_release: bool) {
        self.calls.fetch_add(1, Ordering::SeqCst);
        self.entered.notify_one();
        self.release.notified().await;
        assert!(!panic_after_release, "scripted foreign async panic");
    }
}

#[derive(Clone, Copy, Debug)]
enum Entry {
    CloseView,
    CloseSnapshot,
    Shutdown,
    Start,
    Action,
    Drive,
    Retire,
    Fresh,
}

struct InterruptedSession {
    session: FakeForeignSession,
    barrier: Arc<Barrier>,
    entry: Entry,
    panic_after_release: bool,
    snapshots: Arc<AtomicUsize>,
}

#[async_trait]
impl ForeignPlatformOwnerSession for InterruptedSession {
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
        if matches!(self.entry, Entry::Start | Entry::Fresh) {
            return Err(SupervisorFfiError::Platform {
                detail: "scripted reader startup failure".to_owned(),
            });
        }
        Ok(())
    }

    async fn wait_event_reader(&self) -> Result<(), SupervisorFfiError> {
        if !matches!(self.entry, Entry::CloseSnapshot) {
            self.barrier.wait(self.panic_after_release).await;
        }
        Ok(())
    }

    async fn resource_snapshot(&self) -> Result<Vec<ForeignResourceCount>, SupervisorFfiError> {
        self.snapshots.fetch_add(1, Ordering::SeqCst);
        if matches!(self.entry, Entry::CloseSnapshot) {
            self.barrier.wait(self.panic_after_release).await;
        }
        self.session.resource_snapshot().await
    }

    async fn execute_recovery_action(
        &self,
        sequence: u64,
        action: ForeignPlatformRecoveryAction,
    ) -> Result<ForeignPlatformActionResult, SupervisorFfiError> {
        self.barrier.wait(self.panic_after_release).await;
        self.session.execute_recovery_action(sequence, action).await
    }
}

struct LifecycleFactory(Mutex<std::collections::VecDeque<Arc<dyn ForeignPlatformOwnerSession>>>);

impl ForeignPlatformOwnerSessionFactory for LifecycleFactory {
    fn required_zero_resource_names(&self) -> Vec<String> {
        vec!["connections".to_owned(), "callbacks".to_owned()]
    }

    fn create(
        &self,
        _: ForeignOwnerContext,
    ) -> Result<Arc<dyn ForeignPlatformOwnerSession>, SupervisorFfiError> {
        Ok(self
            .0
            .lock()
            .unwrap()
            .pop_front()
            .expect("scripted session"))
    }
}

fn fresh_context() -> ForeignOwnerContext {
    ForeignOwnerContext {
        owner_id: "fresh-owner".to_owned(),
        backend_id: "fresh-backend".to_owned(),
        owner_generation: 2,
        recovery_attempt: 1,
    }
}

fn fixture(
    entry: Entry,
    panic_after_release: bool,
) -> (
    Arc<Supervisor>,
    FakeBackendHandle,
    Arc<Barrier>,
    Arc<AtomicUsize>,
) {
    let (backend, handle) = scripted_backend();
    let barrier = Arc::new(Barrier::default());
    let snapshots = Arc::new(AtomicUsize::new(0));
    let resources = ["connections", "callbacks"]
        .map(|name| ForeignResourceCount {
            name: name.to_owned(),
            count: 0,
        })
        .to_vec();
    let session: Arc<dyn ForeignPlatformOwnerSession> = Arc::new(InterruptedSession {
        session: FakeForeignSession {
            context: if matches!(entry, Entry::Fresh) {
                fresh_context()
            } else {
                context()
            },
            backend: Arc::new(backend),
            resources: resources.clone(),
        },
        barrier: Arc::clone(&barrier),
        entry,
        panic_after_release,
        snapshots: Arc::clone(&snapshots),
    });
    let mut configuration = config();
    configuration.recovery.allow_central_recreation = false;
    let mut sessions = std::collections::VecDeque::new();
    if matches!(entry, Entry::Fresh) {
        let (backend, _) = scripted_backend();
        sessions.push_back(Arc::new(FakeForeignSession {
            context: context(),
            backend: Arc::new(backend),
            resources,
        }) as Arc<dyn ForeignPlatformOwnerSession>);
    }
    sessions.push_back(session);
    let supervisor = Supervisor::new(
        Arc::new(LifecycleFactory(Mutex::new(sessions))),
        configuration,
    )
    .unwrap();
    (supervisor, handle, barrier, snapshots)
}

async fn enter(supervisor: &Supervisor, entry: Entry) {
    match entry {
        Entry::CloseView | Entry::CloseSnapshot => {
            let _ = supervisor.shutdown_view().await;
        }
        Entry::Shutdown => {
            let _ = supervisor.shutdown().await;
        }
        Entry::Start => {
            let _ = supervisor.start().await;
        }
        Entry::Action => {
            let _ = supervisor.execute_platform_action().await;
        }
        Entry::Drive => {
            let _ = supervisor.drive_platform_actions_until_boundary().await;
        }
        Entry::Retire => {
            let _ = supervisor.retire_for_recovery().await;
        }
        Entry::Fresh => {
            let _ = supervisor.create_fresh_owner(fresh_context()).await;
        }
    }
}

fn bounded<F: Future>(runtime: &tokio::runtime::Runtime, future: F) -> F::Output {
    runtime.block_on(async { tokio::time::timeout(DEADLINE, future).await.unwrap() })
}

#[test]
fn unpolled_futures_leave_ownership_available_and_completed_guards_restore_it() {
    let (supervisor, backend, barrier, snapshots) = fixture(Entry::Action, false);
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .unwrap();
    drop(Box::pin(supervisor.start()));
    assert_eq!(supervisor.active_context().unwrap(), None);
    bounded(&runtime, supervisor.start()).unwrap();
    drop(Box::pin(supervisor.shutdown_view()));
    drop(Box::pin(supervisor.shutdown()));
    assert_eq!(backend.dispose_count(), 0);
    assert_eq!(supervisor.active_context().unwrap(), Some(context()));
    supervisor
        .observe_external_signal(ForeignExternalRecoverySignal::RadioLost)
        .unwrap();
    let mut operation = Box::pin(supervisor.execute_platform_action());
    bounded(&runtime, async {
        tokio::select! {
            biased;
            result = &mut operation => panic!("action finished before barrier: {result:?}"),
            () = barrier.entered.notified() => {},
        }
    });
    let busy = bounded(&runtime, supervisor.shutdown_view());
    assert_eq!(busy.failure.unwrap().code, "supervisor_busy");
    barrier.release.notify_one();
    bounded(&runtime, operation).unwrap();
    assert_eq!(supervisor.active_context().unwrap(), Some(context()));
    barrier.release.notify_one();
    let view = bounded(&runtime, supervisor.shutdown_view());
    assert!(view.owner_closed);
    assert_eq!(view.failure, None);
    assert_eq!(bounded(&runtime, supervisor.shutdown_view()), view);
    assert_eq!(barrier.calls.load(Ordering::SeqCst), 2);
    assert_eq!(snapshots.load(Ordering::SeqCst), 1);
    assert_eq!(backend.dispose_count(), 1);
}

#[test]
fn interrupted_lifecycle_never_remains_busy_or_restores_a_partial_owner() {
    for entry in [
        Entry::CloseView,
        Entry::CloseSnapshot,
        Entry::Shutdown,
        Entry::Start,
        Entry::Action,
        Entry::Drive,
        Entry::Retire,
        Entry::Fresh,
    ] {
        for panic_after_release in [false, true] {
            let (supervisor, backend, barrier, snapshots) = fixture(entry, panic_after_release);
            let runtime = tokio::runtime::Builder::new_current_thread()
                .enable_time()
                .build()
                .unwrap();
            if !matches!(entry, Entry::Start) {
                bounded(&runtime, supervisor.start()).unwrap();
            }
            if matches!(
                entry,
                Entry::Action | Entry::Drive | Entry::Retire | Entry::Fresh
            ) {
                supervisor
                    .observe_external_signal(ForeignExternalRecoverySignal::RadioLost)
                    .unwrap();
            }
            if matches!(entry, Entry::Retire | Entry::Fresh) {
                supervisor
                    .observe_signal(ForeignRecoverySignal::RadioOn)
                    .unwrap();
                supervisor
                    .observe_signal(ForeignRecoverySignal::SameOwnerRecoveryFailed {
                        reason: "scripted retirement".to_owned(),
                    })
                    .unwrap();
            }
            if matches!(entry, Entry::Fresh) {
                bounded(&runtime, supervisor.retire_for_recovery()).unwrap();
            }
            let mut operation = Box::pin(enter(&supervisor, entry));
            bounded(&runtime, async {
                tokio::select! {
                    biased;
                    () = &mut operation => panic!("{entry:?} finished before its barrier"),
                    () = barrier.entered.notified() => {},
                }
            });
            let busy = bounded(&runtime, supervisor.shutdown_view());
            assert!(!busy.owner_closed);
            assert_eq!(busy.failure.unwrap().code, "supervisor_busy");
            assert!(matches!(
                supervisor.begin_start_scan(context(), ScanOptions::default()),
                Err(SupervisorOperationError::SupervisorBusy)
            ));
            if panic_after_release {
                barrier.release.notify_one();
                assert!(catch_unwind(AssertUnwindSafe(|| bounded(&runtime, operation))).is_err());
            } else {
                drop(operation);
            }
            assert!(
                matches!(
                    supervisor.begin_start_scan(context(), ScanOptions::default()),
                    Err(SupervisorOperationError::SupervisorUnavailable)
                ),
                "{entry:?}, panic={panic_after_release}"
            );
            assert!(supervisor.is_active().is_err());
            assert!(supervisor.active_context().is_err());
            assert!(supervisor.active_owner_start().is_err());
            assert!(supervisor.last_retired_resources().is_err());
            let view = bounded(&runtime, supervisor.shutdown_view());
            let logically_closed = matches!(
                entry,
                Entry::CloseView | Entry::CloseSnapshot | Entry::Shutdown
            );
            assert_eq!(view.owner_closed, logically_closed, "{entry:?}");
            assert_eq!(
                view.failure.as_ref().unwrap().code,
                if logically_closed {
                    "platform_failure"
                } else {
                    "supervisor_unavailable"
                }
            );
            for _ in 0..2 {
                assert!(bounded(&runtime, supervisor.start()).is_err());
                let error = bounded(&runtime, supervisor.shutdown()).unwrap_err();
                assert!(!error.to_string().contains("already in progress"));
                assert_eq!(bounded(&runtime, supervisor.shutdown_view()), view);
            }
            assert_eq!(
                snapshots.load(Ordering::SeqCst),
                usize::from(matches!(entry, Entry::CloseSnapshot))
            );
            assert_eq!(barrier.calls.load(Ordering::SeqCst), 1);
            // Dropping a facade future must not stop the original BLeng disposal worker.
            bounded(&runtime, async {
                while backend.dispose_count() == 0 {
                    tokio::task::yield_now().await;
                }
            });
            assert_eq!(backend.dispose_count(), 1);
        }
    }
}
