use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, OnceLock};

use crate::{BleBackend, BleEventSink, BleHub, BleHubError};

type AssemblyTicket = u64;

struct AssemblyRegistry {
    next_ticket: AtomicU64,
    entries: Mutex<HashMap<AssemblyTicket, AssemblyEntry>>,
}

enum AssemblyEntry {
    Pending { downstream: Arc<dyn BleEventSink> },
    Attaching,
    Attached { hub: Arc<BleHub> },
    Cancelled,
}

static REGISTRY: OnceLock<AssemblyRegistry> = OnceLock::new();

fn registry() -> &'static AssemblyRegistry {
    REGISTRY.get_or_init(|| AssemblyRegistry {
        next_ticket: AtomicU64::new(1),
        entries: Mutex::new(HashMap::new()),
    })
}

fn lock_entries() -> std::sync::MutexGuard<'static, HashMap<AssemblyTicket, AssemblyEntry>> {
    registry()
        .entries
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
}

/// Rust-owned guard for a short-lived, one-shot native backend assembly.
///
/// Dropping an unfinished guard cancels its ticket and disposes a Hub that was
/// attached but not transferred to the Rust caller. The registry is only used
/// for this construction handshake, never for event routing or BLE identity.
#[must_use = "dropping an unfinished backend assembly cancels it"]
pub struct BackendAssembly {
    ticket: AssemblyTicket,
    finished: bool,
}

impl BackendAssembly {
    pub fn new(downstream: Arc<dyn BleEventSink>) -> Self {
        let ticket = registry().next_ticket.fetch_add(1, Ordering::Relaxed);
        lock_entries().insert(ticket, AssemblyEntry::Pending { downstream });
        Self {
            ticket,
            finished: false,
        }
    }

    pub fn ticket(&self) -> u64 {
        self.ticket
    }

    pub fn finish(mut self) -> Result<Arc<BleHub>, BleHubError> {
        let result = take_assembled_hub(self.ticket);
        self.finished = result.is_ok();
        result
    }
}

impl Drop for BackendAssembly {
    fn drop(&mut self) {
        if !self.finished {
            cancel_backend_assembly(self.ticket);
        }
    }
}

/// Completes an assembly slot with a foreign backend and constructs the
/// canonical Rust-owned `BleHub`.
///
/// This is the only assembly function exposed through the Stable UniFFI
/// surface. The backend argument is therefore the generated foreign
/// `BleBackend` object, not a raw C pointer or an integration-specific proxy.
#[uniffi::export]
pub fn attach_backend(ticket: u64, backend: Arc<dyn BleBackend>) -> Result<(), BleHubError> {
    let downstream = {
        let mut entries = lock_entries();
        let Some(entry) = entries.get_mut(&ticket) else {
            return Err(BleHubError::BackendAssembly {
                detail: format!("unknown backend assembly ticket {ticket}"),
            });
        };
        let AssemblyEntry::Pending { downstream } = entry else {
            return Err(BleHubError::BackendAssembly {
                detail: format!("backend assembly ticket {ticket} was already consumed"),
            });
        };
        let downstream = Arc::clone(downstream);
        *entry = AssemblyEntry::Attaching;
        downstream
    };

    let hub = BleHub::new(backend, downstream);
    {
        let mut entries = lock_entries();
        if matches!(entries.get(&ticket), Some(AssemblyEntry::Attaching)) {
            return match hub {
                Ok(hub) => {
                    entries.insert(ticket, AssemblyEntry::Attached { hub });
                    Ok(())
                }
                Err(error) => {
                    entries.remove(&ticket);
                    Err(error)
                }
            };
        }
    }
    let entry = {
        let mut entries = lock_entries();
        entries.remove(&ticket)
    };
    match entry {
        Some(AssemblyEntry::Cancelled) | None => {
            if let Ok(hub) = hub
                && let Err(_error) = hub.dispose()
            {
                tracing::error!(
                    target: "blehub::assembly",
                    diagnostic = "cancelled_assembly_dispose_failed",
                    ticket,
                    error_class = "backend_cleanup",
                    "failed to dispose a hub assembled after cancellation"
                );
            }
            Err(BleHubError::BackendAssembly {
                detail: format!("backend assembly ticket {ticket} was cancelled"),
            })
        }
        Some(AssemblyEntry::Pending { .. })
        | Some(AssemblyEntry::Attached { .. })
        | Some(AssemblyEntry::Attaching) => {
            if let Ok(hub) = hub
                && let Err(_error) = hub.dispose()
            {
                tracing::error!(
                    target: "blehub::assembly",
                    diagnostic = "unexpected_assembly_state_dispose_failed",
                    ticket,
                    error_class = "backend_cleanup",
                    "failed to dispose a hub after an unexpected assembly state transition"
                );
            }
            Err(BleHubError::BackendAssembly {
                detail: format!("backend assembly ticket {ticket} changed state unexpectedly"),
            })
        }
    }
}

/// Takes the completed Rust-owned Hub from an assembly slot.
fn take_assembled_hub(ticket: AssemblyTicket) -> Result<Arc<BleHub>, BleHubError> {
    let entry = {
        let mut entries = lock_entries();
        entries.remove(&ticket)
    };
    match entry {
        Some(AssemblyEntry::Attached { hub }) => Ok(hub),
        Some(AssemblyEntry::Pending { .. }) => Err(BleHubError::BackendAssembly {
            detail: format!("backend assembly ticket {ticket} is not attached"),
        }),
        Some(AssemblyEntry::Attaching) => Err(BleHubError::BackendAssembly {
            detail: format!("backend assembly ticket {ticket} is still attaching"),
        }),
        Some(AssemblyEntry::Cancelled) => Err(BleHubError::BackendAssembly {
            detail: format!("backend assembly ticket {ticket} was cancelled"),
        }),
        None => Err(BleHubError::BackendAssembly {
            detail: format!("unknown backend assembly ticket {ticket}"),
        }),
    }
}

/// Cancels an assembly slot. A currently attaching backend observes the
/// cancellation when its canonical `BleHub::new` call returns and disposes any
/// successfully assembled Hub before returning the error.
fn cancel_backend_assembly(ticket: AssemblyTicket) {
    let entry = {
        let mut entries = lock_entries();
        match entries.remove(&ticket) {
            Some(AssemblyEntry::Attached { hub }) => Some(AssemblyEntry::Attached { hub }),
            Some(AssemblyEntry::Attaching) => {
                entries.insert(ticket, AssemblyEntry::Cancelled);
                None
            }
            other => other,
        }
    };
    let attached_hub = match entry {
        Some(AssemblyEntry::Attached { hub }) => Some(hub),
        Some(AssemblyEntry::Pending { .. }) | Some(AssemblyEntry::Cancelled) | None => None,
        Some(AssemblyEntry::Attaching) => unreachable!("attaching entry is restored above"),
    };

    if let Some(hub) = attached_hub
        && let Err(_error) = hub.dispose()
    {
        tracing::error!(
            target: "blehub::assembly",
            diagnostic = "cancelled_attached_hub_dispose_failed",
            ticket,
            error_class = "backend_cleanup",
            "failed to dispose a cancelled assembled hub"
        );
    }
}

#[cfg(test)]
mod tests {
    use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
    use std::sync::{Arc, Barrier, Mutex};

    use super::*;
    use crate::{
        BackendCapabilities, BackendEventIngressHandle, BleEvent, CommandDispatchError,
        ConnectCommand, DisconnectCommand, DiscoverGattCommand, EventSinkError, InitializeCommand,
        Platform, ReadCharacteristicCommand, ReadRssiCommand, SetNotificationsCommand,
        StartScanCommand, StopScanCommand, WriteCharacteristicCommand,
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

        fn set_notifications(
            &self,
            _: SetNotificationsCommand,
        ) -> Result<(), CommandDispatchError> {
            unreachable!()
        }

        fn read_rssi(&self, _: ReadRssiCommand) -> Result<(), CommandDispatchError> {
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
        let (sink, lock_was_free, nested_cancel_completed) = reentrant_drop_sink();
        let assembly = BackendAssembly::new(sink);
        drop(assembly);
        assert!(lock_was_free.load(Ordering::Acquire));
        assert!(nested_cancel_completed.load(Ordering::Acquire));
    }

    #[test]
    fn pending_finish_drops_sink_outside_registry_lock_and_completes_nested_cancel() {
        let (sink, lock_was_free, nested_cancel_completed) = reentrant_drop_sink();
        let assembly = BackendAssembly::new(Arc::clone(&sink));
        assert!(assembly.finish().is_err());
        drop(sink);
        assert!(lock_was_free.load(Ordering::Acquire));
        assert!(nested_cancel_completed.load(Ordering::Acquire));
    }

    #[test]
    fn dropping_attached_assembly_disposes_the_unclaimed_hub() {
        let dispose_count = Arc::new(AtomicUsize::new(0));
        let assembly = BackendAssembly::new(Arc::new(Sink));

        attach_backend(assembly.ticket(), backend(&dispose_count)).unwrap();
        drop(assembly);

        assert_eq!(dispose_count.load(Ordering::Relaxed), 1);
    }

    #[test]
    fn dropping_assembly_during_attach_disposes_the_cancelled_hub() {
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
}
