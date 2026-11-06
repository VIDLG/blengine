use std::sync::{
    Arc, Mutex, Weak,
    atomic::{AtomicBool, Ordering},
};

use blehub::{AdvertisementObservation, BleEvent, CommandId, ScanSessionId, ScanStopResult};

use crate::recovery::RecoveryContext;
use crate::{ScanDelivery, ScanHandoff, ScanSubmissionError, SupervisorOperationError};
use bleng::StartScanOperation;

/// The scan-only subset of the owner's existing public event stream.
#[derive(Clone, Debug, PartialEq, Eq)]
#[cfg_attr(feature = "ffi", derive(uniffi::Enum))]
pub enum ScanEvent {
    Started {
        command_id: CommandId,
        scan_id: ScanSessionId,
    },
    Advertisement {
        advertisement: AdvertisementObservation,
    },
    Stopped {
        scan_id: ScanSessionId,
        result: ScanStopResult,
    },
}

/// An observation stamped with its immutable source owner, not the current active owner.
///
/// Delivery may outlive retirement. Downstream consumers must match the full source context;
/// numeric scan and peripheral IDs alone do not identify an owner lifetime.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ScanEventEnvelope {
    source_context: RecoveryContext,
    event: ScanEvent,
}

impl ScanEventEnvelope {
    pub fn source_context(&self) -> &RecoveryContext {
        &self.source_context
    }

    pub fn event(&self) -> &ScanEvent {
        &self.event
    }

    pub fn into_parts(self) -> (RecoveryContext, ScanEvent) {
        (self.source_context, self.event)
    }
}

/// Revocable, per-owner projection used by that owner's existing event reader.
///
/// Its source context is fixed at construction, never looked up through the active supervisor.
/// The single reader also offers observations to an optional bounded Rust scan delivery. No
/// second reader/runtime is created; the weak slot cannot keep a discarded delivery alive.
#[derive(Debug)]
pub struct SessionScanProjection {
    source_context: RecoveryContext,
    revoked: AtomicBool,
    delivery: Mutex<ScanDeliveries>,
    #[cfg(test)]
    pub(crate) install_pause: Mutex<Option<ScanInstallPause>>,
    #[cfg(test)]
    pub(crate) install_contention: Mutex<Option<std::sync::mpsc::SyncSender<()>>>,
}

#[cfg(test)]
#[derive(Debug)]
pub(crate) struct ScanInstallPause {
    pub(crate) submitted: std::sync::mpsc::SyncSender<CommandId>,
    pub(crate) release: std::sync::mpsc::Receiver<()>,
}

/// Owner-local revocation bookkeeping, not event routing by identity. Entries are weak and
/// pruned on each submission, so discarded handles do not accumulate historical tombstones.
#[derive(Debug, Default)]
struct ScanDeliveries {
    current: Weak<ScanDelivery>,
    live: Vec<Weak<ScanDelivery>>,
}

impl SessionScanProjection {
    pub(crate) fn submit_scan(
        &self,
        submit: impl FnOnce() -> Result<Arc<StartScanOperation>, SupervisorOperationError>,
    ) -> Result<ScanHandoff, ScanSubmissionError> {
        let mut slot = self
            .delivery
            .lock()
            .map_err(|_| ScanSubmissionError::Unavailable)?;
        if self.revoked.load(Ordering::Acquire) {
            return Err(ScanSubmissionError::OwnerRetired);
        }
        if let Some(delivery) = slot.current.upgrade()
            && !delivery.can_replace()
        {
            return Err(ScanSubmissionError::DeliveryBusy);
        }
        // Hold only across bounded, synchronous BLeng submission, never BLE/wait/foreign code.
        // A reader callback arriving before return cannot pass this lock until the command-bound
        // slot is installed. Submission failure leaves the earlier slot untouched.
        let operation = submit()?;
        #[cfg(test)]
        if let Some(pause) = self.install_pause.lock().unwrap().take() {
            pause.submitted.send(operation.command_id()).unwrap();
            pause
                .release
                .recv_timeout(std::time::Duration::from_secs(2))
                .expect("scan installation rendezvous was not released");
        }
        let delivery = Arc::new(ScanDelivery::new(
            self.source_context.clone(),
            operation.command_id(),
        ));
        delivery.observe_failure(&operation);
        slot.live.retain(|entry| entry.strong_count() > 0);
        slot.current = Arc::downgrade(&delivery);
        slot.live.push(Arc::downgrade(&delivery));
        Ok(ScanHandoff {
            operation,
            delivery,
        })
    }

    /// Maps exactly the three scan variants while preserving their complete payloads.
    ///
    /// Publication linearizes at the monotonic revocation flag's atomic read. A projection whose
    /// read precedes revocation may still return or be delivered afterward; retirement does not
    /// retract already returned envelopes. Downstream must fence delivery by the full context.
    /// Revocation is not evidence of scan stop, native BLE retirement or resource cleanup.
    pub fn project(&self, event: BleEvent) -> Option<ScanEventEnvelope> {
        if self.revoked.load(Ordering::Acquire) {
            return None;
        }
        let event = match event {
            BleEvent::ScanStarted {
                command_id,
                scan_id,
            } => ScanEvent::Started {
                command_id,
                scan_id,
            },
            BleEvent::AdvertisementObserved { advertisement } => {
                ScanEvent::Advertisement { advertisement }
            }
            BleEvent::ScanStopped { scan_id, result } => ScanEvent::Stopped { scan_id, result },
            _ => return None,
        };
        {
            #[cfg(test)]
            if let Some(contended) = self.install_contention.lock().unwrap().take() {
                assert!(matches!(
                    self.delivery.try_lock(),
                    Err(std::sync::TryLockError::WouldBlock)
                ));
                contended.send(()).unwrap();
            }
            let slot = self
                .delivery
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            if self.revoked.load(Ordering::Acquire) {
                return None;
            }
            if let Some(delivery) = slot.current.upgrade() {
                if self.delivery.is_poisoned() {
                    delivery.fail_unavailable();
                } else {
                    delivery.offer(&self.source_context, &event);
                }
            }
        }
        Some(ScanEventEnvelope {
            source_context: self.source_context.clone(),
            event,
        })
    }
}

/// The non-clone owner capability; retained reader endpoints cannot keep publication alive.
/// Dropping a canceled startup future or active owner revokes even without async cleanup.
pub(crate) struct ScanProjectionLease {
    projection: Arc<SessionScanProjection>,
}

impl ScanProjectionLease {
    pub(crate) fn new(source_context: RecoveryContext) -> Self {
        Self {
            projection: Arc::new(SessionScanProjection {
                source_context,
                revoked: AtomicBool::new(false),
                delivery: Mutex::new(ScanDeliveries::default()),
                #[cfg(test)]
                install_pause: Mutex::new(None),
                #[cfg(test)]
                install_contention: Mutex::new(None),
            }),
        }
    }

    pub(crate) fn projection(&self) -> Arc<SessionScanProjection> {
        Arc::clone(&self.projection)
    }

    pub(crate) fn revoke(&self) {
        let mut slot = self
            .projection
            .delivery
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        self.projection.revoked.store(true, Ordering::Release);
        for delivery in slot.live.drain(..).filter_map(|entry| entry.upgrade()) {
            delivery.close();
        }
        slot.current = Weak::new();
    }
}

impl Drop for ScanProjectionLease {
    fn drop(&mut self) {
        self.revoke();
    }
}
