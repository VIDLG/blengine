use super::{
    AssemblyAdmissionError, CleanupError, CleanupStartResult, HubCleanup, HubRegistration,
};
use blehub::{
    BackendCapabilities, BackendEventIngressHandle, BleBackend, BleEvent, BleEventSink, BleHub,
    BleHubError, CommandDispatchError, ConnectCommand, DisconnectCommand, DiscoverGattCommand,
    EventSinkError, InitializeCommand, ReadCharacteristicCommand, ReadRssiCommand,
    SetNotificationsCommand, StartScanCommand, StopScanCommand, WriteCharacteristicCommand,
};
use blehub_testkit::FakeBackend;

use std::{
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
        mpsc::{self, Receiver, SyncSender},
    },
    time::Duration,
};

impl HubCleanup {
    // Test fixtures need a direct attach path so cleanup behavior can be tested
    // independently from backend assembly admission. Keep that bypass in the
    // test module rather than exposing it from production cleanup code.
    pub(crate) fn attach(&self, hub: Arc<BleHub>) -> Result<(), AssemblyAdmissionError> {
        self.shared
            .register_owned_hub(hub, HubRegistration::Unadmitted, || true)
    }
}

struct Sink;

impl BleEventSink for Sink {
    fn on_event(&self, _: BleEvent) -> Result<(), EventSinkError> {
        Ok(())
    }
}

struct BlockingDisposeBackend {
    inner: FakeBackend,
    entered: SyncSender<()>,
    release: std::sync::Mutex<Receiver<()>>,
    attempts: Arc<AtomicUsize>,
    error: BleHubError,
}

impl BleBackend for BlockingDisposeBackend {
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
        }
    }

    fn dispose(&self) -> Result<(), BleHubError> {
        self.attempts.fetch_add(1, Ordering::SeqCst);
        self.entered
            .send(())
            .expect("dispose entry receiver dropped");
        self.release
            .lock()
            .unwrap()
            .recv_timeout(Duration::from_secs(3))
            .expect("test did not release blocked dispose");
        self.inner.dispose()?;
        Err(self.error.clone())
    }
}

struct ReleaseOnDrop(Option<SyncSender<()>>);

impl ReleaseOnDrop {
    fn release(&mut self) {
        if let Some(sender) = self.0.take() {
            let _ = sender.try_send(());
        }
    }
}

impl Drop for ReleaseOnDrop {
    fn drop(&mut self) {
        self.release();
    }
}

#[test]
fn rejected_hub_before_admitted_primary_does_not_lose_primary_cleanup_slot() {
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    let cleanup = Arc::new(HubCleanup::new());
    let admission = cleanup.begin_admission(|| true).unwrap();
    let (rejected_backend, rejected_handle) = FakeBackend::scripted([]);
    let rejected = BleHub::new(Arc::new(rejected_backend), Arc::new(Sink)).unwrap();
    assert_eq!(
        cleanup.attach(rejected),
        Err(AssemblyAdmissionError::AdditionalHubRejected)
    );

    let (primary_backend, primary_handle) = FakeBackend::scripted([]);
    let primary = BleHub::new(Arc::new(primary_backend), Arc::new(Sink)).unwrap();
    admission.commit(primary, || true).unwrap();
    cleanup.start_cleanup();
    runtime.block_on(async {
        for _ in 0..2 {
            assert_eq!(
                tokio::time::timeout(Duration::from_secs(2), cleanup.wait_for_completion()).await,
                Ok(Ok(()))
            );
        }
    });
    assert_eq!(rejected_handle.dispose_count(), 1);
    assert_eq!(primary_handle.dispose_count(), 1);
}

#[test]
fn empty_close_is_repeatable_after_a_cancelled_waiter() {
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    let cleanup = HubCleanup::new();
    let pending = runtime.block_on(async {
        tokio::time::timeout(std::time::Duration::ZERO, cleanup.wait_for_completion()).await
    });
    assert!(pending.is_err());
    cleanup.start_cleanup();
    cleanup.start_cleanup();
    runtime.block_on(async {
        assert_eq!(cleanup.wait_for_completion().await, Ok(()));
        assert_eq!(cleanup.wait_for_completion().await, Ok(()));
    });
}

#[test]
fn blocking_dispose_does_not_block_start_or_runtime_and_shares_error_after_cancellation() {
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(1)
        .enable_time()
        .build()
        .unwrap();
    let cleanup = Arc::new(HubCleanup::new());
    let (backend, backend_handle) = FakeBackend::scripted([]);
    let (entered_tx, entered_rx) = mpsc::sync_channel(1);
    let (release_tx, release_rx) = mpsc::sync_channel(1);
    let mut release = ReleaseOnDrop(Some(release_tx));
    let attempts = Arc::new(AtomicUsize::new(0));
    let error = BleHubError::BackendCleanup {
        detail: "bounded disposal failure".to_owned(),
        platform_details: None,
    };
    let hub = BleHub::new(
        Arc::new(BlockingDisposeBackend {
            inner: backend,
            entered: entered_tx,
            release: std::sync::Mutex::new(release_rx),
            attempts: Arc::clone(&attempts),
            error: error.clone(),
        }),
        Arc::new(Sink),
    )
    .unwrap();
    assert!(cleanup.attach(hub).is_ok());

    let (started_tx, started_rx) = mpsc::sync_channel(1);
    let starter_cleanup = Arc::clone(&cleanup);
    let starter = std::thread::spawn(move || {
        starter_cleanup.start_cleanup();
        let _ = started_tx.send(());
    });
    entered_rx
        .recv_timeout(Duration::from_secs(2))
        .expect("dispose did not enter");
    started_rx
        .recv_timeout(Duration::from_millis(500))
        .expect("start blocked on dispose");
    starter.join().unwrap();

    runtime.block_on(async {
        tokio::time::timeout(Duration::from_millis(500), async {
            tokio::spawn(async { tokio::task::yield_now().await })
                .await
                .unwrap();
        })
        .await
        .expect("async runtime stalled during dispose");
        assert!(
            tokio::time::timeout(Duration::from_millis(30), cleanup.wait_for_completion())
                .await
                .is_err()
        );
    });
    assert_eq!(attempts.load(Ordering::SeqCst), 1);
    assert_eq!(backend_handle.dispose_count(), 0);

    release.release();
    runtime.block_on(async {
        let expected = Err(CleanupError::Backend(error));
        assert_eq!(
            tokio::time::timeout(Duration::from_secs(2), cleanup.wait_for_completion())
                .await
                .expect("first waiter timed out"),
            expected
        );
        assert_eq!(
            tokio::time::timeout(Duration::from_secs(2), cleanup.wait_for_completion())
                .await
                .expect("second waiter timed out"),
            expected
        );
    });
    assert_eq!(attempts.load(Ordering::SeqCst), 1);
    assert_eq!(backend_handle.dispose_count(), 1);
}

#[test]
fn discarded_disposal_worker_preserves_root_and_failure_remains_observable() {
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .unwrap();
    let cleanup = HubCleanup::new();
    let (backend, handle) = FakeBackend::scripted([]);
    let hub = BleHub::new(Arc::new(backend), Arc::new(Sink)).unwrap();
    let identity = Arc::downgrade(&hub);
    cleanup.attach(hub).unwrap();
    let CleanupStartResult::Started {
        primary: Some(disposal),
    } = cleanup.shared.state.lock().unwrap().start_cleanup()
    else {
        panic!("primary disposal target missing");
    };
    let slot = disposal.slot;

    // Builder::spawn drops the supplied closure when it cannot create a thread.
    // Exercise that ownership path without exhausting actual OS resources.
    drop(disposal.into_worker(Arc::clone(&cleanup.shared)));
    assert_eq!(handle.dispose_count(), 0);
    assert_eq!(identity.strong_count(), 1);
    cleanup
        .shared
        .record_disposal_result(slot, Err(CleanupError::WorkerStopped));
    runtime.block_on(async {
        for _ in 0..2 {
            assert_eq!(
                tokio::time::timeout(Duration::from_secs(2), cleanup.wait_for_completion())
                    .await
                    .expect("failed disposal slot did not settle"),
                Err(CleanupError::WorkerStopped)
            );
        }
    });

    // The failure policy intentionally leaks the root allocation. Release this
    // fixture's native resources explicitly without changing the recorded error.
    identity.upgrade().unwrap().dispose().unwrap();
    assert_eq!(handle.dispose_count(), 1);
}

#[test]
fn duplicate_owned_hub_is_rejected_without_disposing_the_live_backend() {
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    let cleanup = HubCleanup::new();
    let (backend, handle) = FakeBackend::scripted([]);
    let hub = BleHub::new(Arc::new(backend), Arc::new(Sink)).unwrap();
    cleanup.attach(Arc::clone(&hub)).unwrap();
    assert_eq!(
        cleanup.attach(hub),
        Err(AssemblyAdmissionError::DuplicateOwnedHub)
    );
    assert_eq!(handle.dispose_count(), 0);
    cleanup.start_cleanup();
    runtime.block_on(async {
        assert_eq!(
            tokio::time::timeout(Duration::from_secs(2), cleanup.wait_for_completion())
                .await
                .unwrap(),
            Ok(())
        );
    });
    assert_eq!(handle.dispose_count(), 1);
}

#[test]
fn rejected_second_hub_is_tracked_and_its_error_is_repeatable() {
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    let cleanup = HubCleanup::new();
    let (primary_backend, primary_handle) = FakeBackend::scripted([]);
    let primary = BleHub::new(Arc::new(primary_backend), Arc::new(Sink)).unwrap();
    cleanup.attach(primary).unwrap();

    let (backend, handle) = FakeBackend::scripted([]);
    let (entered_tx, entered_rx) = mpsc::sync_channel(1);
    let (release_tx, release_rx) = mpsc::sync_channel(1);
    let mut release = ReleaseOnDrop(Some(release_tx));
    let error = BleHubError::BackendCleanup {
        detail: "rejected assembly cleanup failed".to_owned(),
        platform_details: None,
    };
    let rejected = BleHub::new(
        Arc::new(BlockingDisposeBackend {
            inner: backend,
            entered: entered_tx,
            release: std::sync::Mutex::new(release_rx),
            attempts: Arc::new(AtomicUsize::new(0)),
            error: error.clone(),
        }),
        Arc::new(Sink),
    )
    .unwrap();
    assert_eq!(
        cleanup.attach(Arc::clone(&rejected)),
        Err(AssemblyAdmissionError::AdditionalHubRejected)
    );
    assert_eq!(
        cleanup.attach(rejected),
        Err(AssemblyAdmissionError::DuplicateOwnedHub)
    );
    entered_rx.recv_timeout(Duration::from_secs(2)).unwrap();
    cleanup.start_cleanup();
    runtime.block_on(async {
        assert!(
            tokio::time::timeout(Duration::from_millis(30), cleanup.wait_for_completion())
                .await
                .is_err()
        );
    });
    release.release();
    runtime.block_on(async {
        for _ in 0..2 {
            assert_eq!(
                tokio::time::timeout(Duration::from_secs(2), cleanup.wait_for_completion())
                    .await
                    .unwrap(),
                Err(CleanupError::Backend(error.clone()))
            );
        }
    });
    assert_eq!(primary_handle.dispose_count(), 1);
    assert_eq!(handle.dispose_count(), 1);
}

#[test]
fn late_cleanup_error_is_observable_after_an_earlier_successful_close() {
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    let cleanup = HubCleanup::new();
    cleanup.start_cleanup();
    runtime.block_on(async { assert_eq!(cleanup.wait_for_completion().await, Ok(())) });

    let (backend, handle) = FakeBackend::scripted([]);
    let (entered_tx, entered_rx) = mpsc::sync_channel(1);
    let (release_tx, release_rx) = mpsc::sync_channel(1);
    let mut release = ReleaseOnDrop(Some(release_tx));
    let error = BleHubError::BackendCleanup {
        detail: "late assembly cleanup failed".to_owned(),
        platform_details: None,
    };
    let late = BleHub::new(
        Arc::new(BlockingDisposeBackend {
            inner: backend,
            entered: entered_tx,
            release: std::sync::Mutex::new(release_rx),
            attempts: Arc::new(AtomicUsize::new(0)),
            error: error.clone(),
        }),
        Arc::new(Sink),
    )
    .unwrap();
    assert_eq!(cleanup.attach(late), Err(AssemblyAdmissionError::Closed));
    entered_rx.recv_timeout(Duration::from_secs(2)).unwrap();
    runtime.block_on(async {
        assert!(
            tokio::time::timeout(Duration::from_millis(30), cleanup.wait_for_completion())
                .await
                .is_err()
        );
    });
    release.release();
    runtime.block_on(async {
        for _ in 0..2 {
            assert_eq!(
                tokio::time::timeout(Duration::from_secs(2), cleanup.wait_for_completion())
                    .await
                    .unwrap(),
                Err(CleanupError::Backend(error.clone()))
            );
        }
    });
    assert_eq!(handle.dispose_count(), 1);
}

#[test]
fn attach_racing_close_is_accounted_for() {
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    let cleanup = Arc::new(HubCleanup::new());
    let (backend, handle) = FakeBackend::scripted([]);
    let hub = BleHub::new(Arc::new(backend), Arc::new(Sink)).unwrap();
    let (ready_tx, ready_rx) = mpsc::sync_channel(0);
    let other = Arc::clone(&cleanup);
    let attach = std::thread::spawn(move || {
        ready_tx.send(()).unwrap();
        other.attach(hub)
    });
    ready_rx.recv_timeout(Duration::from_secs(2)).unwrap();
    cleanup.start_cleanup();
    let outcome = attach.join().unwrap();
    assert!(matches!(
        outcome,
        Ok(()) | Err(AssemblyAdmissionError::Closed)
    ));
    runtime.block_on(async {
        assert_eq!(
            tokio::time::timeout(Duration::from_secs(2), cleanup.wait_for_completion())
                .await
                .unwrap(),
            Ok(())
        );
    });
    assert_eq!(handle.dispose_count(), 1);
}

#[test]
fn cancelled_waiter_does_not_cancel_disposal_or_allow_late_attachment() {
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    let cleanup = HubCleanup::new();
    let (backend, backend_handle) = FakeBackend::scripted([]);
    let hub = BleHub::new(Arc::new(backend), Arc::new(Sink)).unwrap();
    assert!(cleanup.attach(hub).is_ok());
    let cancelled = runtime.block_on(async {
        tokio::time::timeout(std::time::Duration::ZERO, cleanup.wait_for_completion()).await
    });
    assert!(cancelled.is_err());
    cleanup.start_cleanup();
    runtime.block_on(async {
        assert_eq!(cleanup.wait_for_completion().await, Ok(()));
        assert_eq!(cleanup.wait_for_completion().await, Ok(()));
    });
    assert_eq!(backend_handle.dispose_count(), 1);

    let (late_backend, late_handle) = FakeBackend::scripted([]);
    let late_hub = BleHub::new(Arc::new(late_backend), Arc::new(Sink)).unwrap();
    assert_eq!(
        cleanup.attach(late_hub),
        Err(AssemblyAdmissionError::Closed)
    );
    runtime.block_on(async {
        assert_eq!(
            tokio::time::timeout(Duration::from_secs(2), cleanup.wait_for_completion())
                .await
                .unwrap(),
            Ok(())
        );
    });
    assert_eq!(late_handle.dispose_count(), 1);
}
