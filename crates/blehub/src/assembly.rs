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

struct AttachInProgress {
    ticket: AssemblyTicket,
    active: bool,
}

impl Drop for AttachInProgress {
    fn drop(&mut self) {
        if !self.active {
            return;
        }
        // An unwinding backend call has no completing side left to retire its
        // marker. Do not recover the panic, run cleanup, or take an attached Hub.
        let _entry = {
            let mut entries = lock_entries();
            if matches!(
                entries.get(&self.ticket),
                Some(AssemblyEntry::Attaching | AssemblyEntry::Cancelled)
            ) {
                entries.remove(&self.ticket)
            } else {
                None
            }
        };
    }
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
/// Cancellation or an early `finish` does not override constructor or cleanup
/// errors. Plain cancellation is returned only after successful construction
/// and disposal of the unclaimed Hub.
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

    let mut attaching = AttachInProgress {
        ticket,
        active: true,
    };
    let hub = BleHub::new(backend, downstream);
    let entry = {
        let mut entries = lock_entries();
        if matches!(entries.get(&ticket), Some(AssemblyEntry::Attaching)) {
            attaching.active = false;
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
        entries.remove(&ticket)
    };
    attaching.active = false;
    let detail = match entry {
        Some(AssemblyEntry::Cancelled) | None => {
            format!("backend assembly ticket {ticket} was cancelled")
        }
        Some(AssemblyEntry::Pending { .. })
        | Some(AssemblyEntry::Attached { .. })
        | Some(AssemblyEntry::Attaching) => {
            format!("backend assembly ticket {ticket} changed state unexpectedly")
        }
    };
    // Cancellation cannot erase a constructor/rollback error or a failed
    // cleanup. Only a successfully cleaned-up Hub reports plain cancellation.
    let hub = hub?;
    hub.dispose()?;
    Err(BleHubError::BackendAssembly { detail })
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
    let hub = match entry {
        Some(AssemblyEntry::Attached { hub }) => hub,
        Some(AssemblyEntry::Pending { .. }) | Some(AssemblyEntry::Cancelled) | None => return,
        Some(AssemblyEntry::Attaching) => unreachable!("attaching entry is restored above"),
    };

    if let Err(_error) = hub.dispose() {
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
#[path = "tests/assembly_tests.rs"]
mod tests;
