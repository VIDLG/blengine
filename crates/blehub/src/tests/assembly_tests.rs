use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, OnceLock, mpsc};
use std::time::Duration;

use super::*;
use crate::{
    AssociateCompanionDeviceCommand, BackendCapabilities, BackendEventIngressHandle, BleEvent,
    CancelOperationRequest, CommandDispatchError, ConnectCommand, DisconnectCommand,
    DiscoverGattCommand, EventSinkError, InitializeCommand, Platform, PlatformErrorDetails,
    QueryDevicesCommand, ReadCharacteristicCommand, ReadRssiCommand, RefreshGattCacheCommand,
    SetAdapterEnabledCommand, SetNotificationsCommand, StartScanCommand, StopScanCommand,
    WriteCharacteristicCommand,
};

struct Sink;

impl BleEventSink for Sink {
    fn on_event(&self, _: BleEvent) -> Result<(), EventSinkError> {
        Ok(())
    }
}

struct Backend {
    dispose_count: Arc<AtomicUsize>,
    capabilities_gate: Option<AttachGate>,
    capabilities_result: Result<BackendCapabilities, BleHubError>,
    dispose_result: Result<(), BleHubError>,
    panic_in_capabilities: bool,
}

impl Backend {
    fn new(dispose_count: &Arc<AtomicUsize>) -> Self {
        Self {
            dispose_count: Arc::clone(dispose_count),
            capabilities_gate: None,
            capabilities_result: Ok(BackendCapabilities::new(Platform::Test)),
            dispose_result: Ok(()),
            panic_in_capabilities: false,
        }
    }
}

struct AttachGate {
    entered: mpsc::Sender<()>,
    release: Mutex<mpsc::Receiver<()>>,
}

struct GateControl {
    entered: mpsc::Receiver<()>,
    release: mpsc::Sender<()>,
}

impl GateControl {
    fn wait_until_entered(&self) {
        self.entered.recv_timeout(Duration::from_secs(5)).unwrap();
    }
}

impl Drop for GateControl {
    fn drop(&mut self) {
        // Release the worker even when a test assertion unwinds.
        let _ = self.release.send(());
    }
}

fn attach_gate() -> (AttachGate, GateControl) {
    let (entered_tx, entered_rx) = mpsc::channel();
    let (release_tx, release_rx) = mpsc::channel();
    (
        AttachGate {
            entered: entered_tx,
            release: Mutex::new(release_rx),
        },
        GateControl {
            entered: entered_rx,
            release: release_tx,
        },
    )
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
        let Ok(entries) = registry().entries.try_lock() else {
            // Report a lock-boundary regression without deadlocking in Drop.
            return;
        };
        self.lock_was_free.store(true, Ordering::Release);
        let ticket = self.nested_ticket.get_mut().unwrap().take().unwrap();
        let was_pending = matches!(entries.get(&ticket), Some(AssemblyEntry::Pending { .. }));
        drop(entries);
        cancel_backend_assembly(ticket);
        self.nested_cancel_completed.store(
            was_pending && !lock_entries().contains_key(&ticket),
            Ordering::Release,
        );
    }
}

impl BleBackend for Backend {
    fn capabilities(&self) -> Result<BackendCapabilities, BleHubError> {
        if let Some(gate) = &self.capabilities_gate {
            gate.entered.send(()).unwrap();
            gate.release
                .lock()
                .unwrap()
                .recv_timeout(Duration::from_secs(5))
                .unwrap();
        }
        assert!(!self.panic_in_capabilities, "controlled assembly panic");
        self.capabilities_result.clone()
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

    fn set_adapter_enabled(&self, _: SetAdapterEnabledCommand) -> Result<(), CommandDispatchError> {
        unreachable!()
    }

    fn query_devices(&self, _: QueryDevicesCommand) -> Result<(), CommandDispatchError> {
        unreachable!()
    }

    fn associate_companion_device(
        &self,
        _: AssociateCompanionDeviceCommand,
    ) -> Result<(), CommandDispatchError> {
        unreachable!()
    }

    fn refresh_gatt_cache(&self, _: RefreshGattCacheCommand) -> Result<(), CommandDispatchError> {
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

    fn mtu(&self, _: crate::MtuCommand) -> Result<(), CommandDispatchError> {
        unreachable!()
    }

    fn set_connection_priority(
        &self,
        _: crate::SetConnectionPriorityCommand,
    ) -> Result<(), CommandDispatchError> {
        unreachable!()
    }

    fn create_bond(&self, _: crate::CreateBondCommand) -> Result<(), CommandDispatchError> {
        unreachable!()
    }

    fn remove_bond(&self, _: crate::RemoveBondCommand) -> Result<(), CommandDispatchError> {
        unreachable!()
    }

    fn read_descriptor(&self, _: crate::ReadDescriptorCommand) -> Result<(), CommandDispatchError> {
        unreachable!()
    }

    fn write_descriptor(
        &self,
        _: crate::WriteDescriptorCommand,
    ) -> Result<(), CommandDispatchError> {
        unreachable!()
    }

    fn cancel_operation(&self, _: CancelOperationRequest) -> Result<(), CommandDispatchError> {
        unreachable!()
    }

    fn dispose(&self) -> Result<(), BleHubError> {
        self.dispose_count.fetch_add(1, Ordering::Relaxed);
        self.dispose_result.clone()
    }
}

fn backend(dispose_count: &Arc<AtomicUsize>) -> Arc<dyn BleBackend> {
    Arc::new(Backend::new(dispose_count))
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

fn reentrant_drop_sink() -> (
    Arc<dyn BleEventSink>,
    Arc<AtomicBool>,
    Arc<AtomicBool>,
    BackendAssembly,
) {
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
        nested,
    )
}

#[test]
fn pending_cancel_drops_sink_outside_registry_lock_and_completes_nested_cancel() {
    let _test_guard = assembly_test_guard();
    let (sink, lock_was_free, nested_cancel_completed, _nested) = reentrant_drop_sink();
    let assembly = BackendAssembly::new(sink);
    drop(assembly);
    assert!(lock_was_free.load(Ordering::Acquire));
    assert!(nested_cancel_completed.load(Ordering::Acquire));
}

#[test]
fn pending_finish_drops_sink_outside_registry_lock_and_completes_nested_cancel() {
    let _test_guard = assembly_test_guard();
    let (sink, lock_was_free, nested_cancel_completed, _nested) = reentrant_drop_sink();
    let assembly = BackendAssembly::new(sink);
    assert!(assembly.finish().is_err());
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
    let (gate, control) = attach_gate();
    let assembly = BackendAssembly::new(Arc::new(Sink));
    let ticket = assembly.ticket();
    let backend = Arc::new(Backend {
        capabilities_gate: Some(gate),
        ..Backend::new(&dispose_count)
    });

    let attach = std::thread::spawn(move || attach_backend(ticket, backend));
    control.wait_until_entered();
    drop(assembly);
    drop(control);

    assert!(matches!(
        attach.join().unwrap(),
        Err(BleHubError::BackendAssembly { .. })
    ));
    assert_eq!(dispose_count.load(Ordering::Relaxed), 1);
    assert!(!lock_entries().contains_key(&ticket));
}

fn cleanup_error() -> BleHubError {
    BleHubError::BackendCleanup {
        detail: "scripted cleanup failure".to_owned(),
        platform_details: Some(Box::new(PlatformErrorDetails {
            platform: Platform::Test,
            code: Some("cleanup-code".to_owned()),
            name: Some("CleanupFailure".to_owned()),
            message: Some("native cleanup detail".to_owned()),
            status: None,
            att_code: None,
        })),
    }
}

fn assert_interrupted_attach(
    early_finish: bool,
    capabilities_result: Result<BackendCapabilities, BleHubError>,
    dispose_result: Result<(), BleHubError>,
) {
    let dispose_count = Arc::new(AtomicUsize::new(0));
    let (gate, control) = attach_gate();
    let assembly = BackendAssembly::new(Arc::new(Sink));
    let ticket = assembly.ticket();
    // The registry must preserve the canonical constructor's error, including
    // its rollback accounting, rather than reinterpret it as cancellation.
    let expected = if capabilities_result.is_err() {
        BleHub::new(
            Arc::new(Backend {
                capabilities_result: capabilities_result.clone(),
                dispose_result: dispose_result.clone(),
                ..Backend::new(&Arc::new(AtomicUsize::new(0)))
            }),
            Arc::new(Sink),
        )
        .err()
        .expect("scripted constructor must fail")
    } else {
        dispose_result
            .clone()
            .err()
            .unwrap_or(BleHubError::BackendAssembly {
                detail: format!("backend assembly ticket {ticket} was cancelled"),
            })
    };
    let backend = Arc::new(Backend {
        capabilities_gate: Some(gate),
        capabilities_result,
        dispose_result,
        ..Backend::new(&dispose_count)
    });
    let attach = std::thread::spawn(move || attach_backend(ticket, backend));
    control.wait_until_entered();
    if early_finish {
        assert!(matches!(
            assembly.finish(),
            Err(BleHubError::BackendAssembly { detail }) if detail.contains("still attaching")
        ));
    } else {
        drop(assembly);
    }
    drop(control);
    let result = attach.join().unwrap();
    assert!(!lock_entries().contains_key(&ticket));
    assert_eq!(dispose_count.load(Ordering::Relaxed), 1);
    assert_eq!(result, Err(expected));
}

fn assert_constructor_errors_survive_interruption(early_finish: bool) {
    for primary in [
        BleHubError::FfiContract {
            detail: "scripted capabilities rejection".to_owned(),
        },
        cleanup_error(),
    ] {
        for cleanup in [Ok(()), Err(cleanup_error())] {
            assert_interrupted_attach(early_finish, Err(primary.clone()), cleanup);
        }
    }
}

#[test]
fn cancelled_attach_preserves_constructor_errors() {
    let _test_guard = assembly_test_guard();
    assert_constructor_errors_survive_interruption(false);
}

#[test]
fn early_finish_preserves_constructor_errors() {
    let _test_guard = assembly_test_guard();
    assert_constructor_errors_survive_interruption(true);
}

#[test]
fn cancelled_attach_preserves_new_hub_cleanup_error() {
    let _test_guard = assembly_test_guard();
    assert_interrupted_attach(
        false,
        Ok(BackendCapabilities::new(Platform::Test)),
        Err(cleanup_error()),
    );
}

#[test]
fn early_finish_preserves_new_hub_cleanup_error() {
    let _test_guard = assembly_test_guard();
    assert_interrupted_attach(
        true,
        Ok(BackendCapabilities::new(Platform::Test)),
        Err(cleanup_error()),
    );
}

#[test]
fn early_finish_with_successful_cleanup_reports_cancellation() {
    let _test_guard = assembly_test_guard();
    assert_interrupted_attach(true, Ok(BackendCapabilities::new(Platform::Test)), Ok(()));
}

fn assert_attach_panic_leaves_no_ticket(cancel_before_panic: bool) {
    let dispose_count = Arc::new(AtomicUsize::new(0));
    let (gate, control) = attach_gate();
    let mut assembly = Some(BackendAssembly::new(Arc::new(Sink)));
    let ticket = assembly.as_ref().unwrap().ticket();
    let backend = Arc::new(Backend {
        capabilities_gate: Some(gate),
        panic_in_capabilities: true,
        ..Backend::new(&dispose_count)
    });
    let attach = std::thread::spawn(move || attach_backend(ticket, backend));
    control.wait_until_entered();
    if cancel_before_panic {
        drop(assembly.take());
    }
    drop(control);
    let panic = attach.join().expect_err("backend panic must propagate");
    assert_eq!(
        panic.downcast_ref::<&str>(),
        Some(&"controlled assembly panic")
    );
    let entry_survived_unwind = lock_entries().contains_key(&ticket);
    drop(assembly);
    let entry_survived_cancel = lock_entries().contains_key(&ticket);
    assert_eq!(dispose_count.load(Ordering::Relaxed), 0);
    assert!(
        !entry_survived_unwind,
        "attach unwind left a registry entry"
    );
    assert!(!entry_survived_cancel, "guard drop left a registry entry");
}

#[test]
fn attach_panic_before_cancellation_propagates_without_leaving_a_ticket() {
    let _test_guard = assembly_test_guard();
    assert_attach_panic_leaves_no_ticket(false);
}

#[test]
fn attach_panic_after_cancellation_propagates_without_leaving_a_ticket() {
    let _test_guard = assembly_test_guard();
    assert_attach_panic_leaves_no_ticket(true);
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
