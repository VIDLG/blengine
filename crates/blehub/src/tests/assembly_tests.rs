use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Barrier, Mutex, MutexGuard, OnceLock};

use super::*;
use crate::{
    BackendCapabilities, BackendEventIngressHandle, BleEvent, CancelOperationRequest,
    CommandDispatchError, ConnectCommand, DisconnectCommand, DiscoverGattCommand, EventSinkError,
    InitializeCommand, Platform, ReadCharacteristicCommand, ReadRssiCommand,
    SetNotificationsCommand, StartScanCommand, StopScanCommand, WriteCharacteristicCommand,
};

struct Sink;

impl BleEventSink for Sink {
    fn on_event(&self, _: BleEvent) -> Result<(), EventSinkError> {
        Ok(())
    }
}

struct Backend {
    dispose_count: Arc<AtomicUsize>,
    capabilities_barriers: Option<(Arc<Barrier>, Arc<Barrier>)>,
}

struct ReentrantDropSink {
    nested_ticket: Mutex<Option<AssemblyTicket>>,
    lock_was_free: Arc<AtomicBool>,
    nested_cancel_completed: Arc<AtomicBool>,
}

impl BleEventSink for ReentrantDropSink {
    fn on_event(&self, _: BleEvent) -> Result<(), EventSinkError> {
        Ok(())
    }
}

static ASSEMBLY_TEST_LOCK: OnceLock<Mutex<()>> = OnceLock::new();

fn assembly_test_guard() -> MutexGuard<'static, ()> {
    ASSEMBLY_TEST_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
}

impl Drop for ReentrantDropSink {
    fn drop(&mut self) {
        if registry().entries.try_lock().is_ok() {
            self.lock_was_free.store(true, Ordering::Release);
        }
        if let Some(ticket) = self.nested_ticket.get_mut().unwrap().take() {
            cancel_backend_assembly(ticket);
            self.nested_cancel_completed.store(true, Ordering::Release);
        }
    }
}

impl BleBackend for Backend {
    fn capabilities(&self) -> Result<BackendCapabilities, BleHubError> {
        if let Some((entered, release)) = &self.capabilities_barriers {
            entered.wait();
            release.wait();
        }
        Ok(BackendCapabilities::new(Platform::Test))
    }

    fn bind_event_ingress(&self, _: Arc<BackendEventIngressHandle>) -> Result<(), BleHubError> {
        Ok(())
    }

    fn initialize(&self, _: InitializeCommand) -> Result<(), CommandDispatchError> {
        unreachable!()
    }

    fn start_scan(&self, _: StartScanCommand) -> Result<(), CommandDispatchError> {
        unreachable!()
    }

    fn stop_scan(&self, _: StopScanCommand) -> Result<(), CommandDispatchError> {
        unreachable!()
    }

    fn connect(&self, _: ConnectCommand) -> Result<(), CommandDispatchError> {
        unreachable!()
    }

    fn disconnect(&self, _: DisconnectCommand) -> Result<(), CommandDispatchError> {
        unreachable!()
    }

    fn discover_gatt(&self, _: DiscoverGattCommand) -> Result<(), CommandDispatchError> {
        unreachable!()
    }

    fn read_characteristic(
        &self,
        _: ReadCharacteristicCommand,
    ) -> Result<(), CommandDispatchError> {
        unreachable!()
    }

    fn write_characteristic(
        &self,
        _: WriteCharacteristicCommand,
    ) -> Result<(), CommandDispatchError> {
        unreachable!()
    }

    fn set_notifications(&self, _: SetNotificationsCommand) -> Result<(), CommandDispatchError> {
        unreachable!()
    }

    fn read_rssi(&self, _: ReadRssiCommand) -> Result<(), CommandDispatchError> {
        unreachable!()
    }

    fn cancel_operation(&self, _: CancelOperationRequest) -> Result<(), CommandDispatchError> {
        unreachable!()
    }

    fn dispose(&self) -> Result<(), BleHubError> {
        self.dispose_count.fetch_add(1, Ordering::Relaxed);
        Ok(())
    }
}

fn backend(dispose_count: &Arc<AtomicUsize>) -> Arc<dyn BleBackend> {
    Arc::new(Backend {
        dispose_count: Arc::clone(dispose_count),
        capabilities_barriers: None,
    })
}

fn blocking_backend(
    dispose_count: &Arc<AtomicUsize>,
    entered: Arc<Barrier>,
    release: Arc<Barrier>,
) -> Arc<dyn BleBackend> {
    Arc::new(Backend {
        dispose_count: Arc::clone(dispose_count),
        capabilities_barriers: Some((entered, release)),
    })
}

#[test]
fn dropping_pending_assembly_cancels_its_ticket() {
    let _test_guard = assembly_test_guard();
    let assembly = BackendAssembly::new(Arc::new(Sink));
    let ticket = assembly.ticket();

    drop(assembly);

    assert!(!lock_entries().contains_key(&ticket));
    assert!(matches!(
        take_assembled_hub(ticket),
        Err(BleHubError::BackendAssembly { .. })
    ));
}

#[test]
fn failed_finish_does_not_leave_a_pending_ticket() {
    let _test_guard = assembly_test_guard();
    let assembly = BackendAssembly::new(Arc::new(Sink));
    let ticket = assembly.ticket();

    assert!(matches!(
        assembly.finish(),
        Err(BleHubError::BackendAssembly { .. })
    ));
    assert!(!lock_entries().contains_key(&ticket));
}

fn reentrant_drop_sink() -> (Arc<dyn BleEventSink>, Arc<AtomicBool>, Arc<AtomicBool>) {
    let nested = BackendAssembly::new(Arc::new(Sink));
    let lock_was_free = Arc::new(AtomicBool::new(false));
    let nested_cancel_completed = Arc::new(AtomicBool::new(false));
    (
        Arc::new(ReentrantDropSink {
            nested_ticket: Mutex::new(Some(nested.ticket())),
            lock_was_free: Arc::clone(&lock_was_free),
            nested_cancel_completed: Arc::clone(&nested_cancel_completed),
        }),
        lock_was_free,
        nested_cancel_completed,
    )
}

#[test]
fn pending_cancel_drops_sink_outside_registry_lock_and_completes_nested_cancel() {
    let _test_guard = assembly_test_guard();
    let (sink, lock_was_free, nested_cancel_completed) = reentrant_drop_sink();
    let assembly = BackendAssembly::new(sink);
    drop(assembly);
    assert!(lock_was_free.load(Ordering::Acquire));
    assert!(nested_cancel_completed.load(Ordering::Acquire));
}

#[test]
fn pending_finish_drops_sink_outside_registry_lock_and_completes_nested_cancel() {
    let _test_guard = assembly_test_guard();
    let (sink, lock_was_free, nested_cancel_completed) = reentrant_drop_sink();
    let assembly = BackendAssembly::new(Arc::clone(&sink));
    assert!(assembly.finish().is_err());
    drop(sink);
    assert!(lock_was_free.load(Ordering::Acquire));
    assert!(nested_cancel_completed.load(Ordering::Acquire));
}

#[test]
fn dropping_attached_assembly_disposes_the_unclaimed_hub() {
    let _test_guard = assembly_test_guard();
    let dispose_count = Arc::new(AtomicUsize::new(0));
    let assembly = BackendAssembly::new(Arc::new(Sink));

    attach_backend(assembly.ticket(), backend(&dispose_count)).unwrap();
    drop(assembly);

    assert_eq!(dispose_count.load(Ordering::Relaxed), 1);
}

#[test]
fn dropping_assembly_during_attach_disposes_the_cancelled_hub() {
    let _test_guard = assembly_test_guard();
    let dispose_count = Arc::new(AtomicUsize::new(0));
    let entered = Arc::new(Barrier::new(2));
    let release = Arc::new(Barrier::new(2));
    let assembly = BackendAssembly::new(Arc::new(Sink));
    let ticket = assembly.ticket();
    let backend = blocking_backend(&dispose_count, Arc::clone(&entered), Arc::clone(&release));

    let attach = std::thread::spawn(move || attach_backend(ticket, backend));
    entered.wait();
    drop(assembly);
    release.wait();

    assert!(matches!(
        attach.join().unwrap(),
        Err(BleHubError::BackendAssembly { .. })
    ));
    assert_eq!(dispose_count.load(Ordering::Relaxed), 1);
    assert!(!lock_entries().contains_key(&ticket));
}

#[test]
fn finish_transfers_hub_ownership_out_of_the_guard() {
    let _test_guard = assembly_test_guard();
    let dispose_count = Arc::new(AtomicUsize::new(0));
    let assembly = BackendAssembly::new(Arc::new(Sink));

    attach_backend(assembly.ticket(), backend(&dispose_count)).unwrap();
    let hub = assembly.finish().unwrap();

    assert_eq!(dispose_count.load(Ordering::Relaxed), 0);
    hub.dispose().unwrap();
    assert_eq!(dispose_count.load(Ordering::Relaxed), 1);
}

#[test]
fn duplicate_unknown_and_late_attach_are_rejected() {
    let _test_guard = assembly_test_guard();
    let first_dispose_count = Arc::new(AtomicUsize::new(0));
    let rejected_dispose_count = Arc::new(AtomicUsize::new(0));
    let assembly = BackendAssembly::new(Arc::new(Sink));
    let ticket = assembly.ticket();
    attach_backend(ticket, backend(&first_dispose_count)).unwrap();

    assert!(matches!(
        attach_backend(ticket, backend(&rejected_dispose_count)),
        Err(BleHubError::BackendAssembly { .. })
    ));
    let hub = assembly.finish().unwrap();
    hub.dispose().unwrap();
    assert_eq!(first_dispose_count.load(Ordering::Relaxed), 1);

    assert!(matches!(
        attach_backend(u64::MAX, backend(&rejected_dispose_count)),
        Err(BleHubError::BackendAssembly { .. })
    ));

    let late = BackendAssembly::new(Arc::new(Sink));
    let late_ticket = late.ticket();
    drop(late);
    assert!(matches!(
        attach_backend(late_ticket, backend(&rejected_dispose_count)),
        Err(BleHubError::BackendAssembly { .. })
    ));
}
