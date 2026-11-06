//! Bounded observation delivery, independent of BLeng operation results and native scan control.
//!
//! The existing owner reader is the only producer of BLE observations; start failure is marked
//! independently by BLeng's non-consuming observer. Before the matching Started event, observations
//! are staged under the same advertisement budgets; bounded stop staging handles end-before-start.
//! No runtime, timer, BLE command or background task is owned by this module.

use std::{
    collections::VecDeque,
    mem::size_of,
    sync::{
        Arc, Mutex, MutexGuard, OnceLock,
        atomic::{AtomicBool, Ordering},
    },
};

use blehub::{
    AdvertisementObservation, BluetoothUuid, CommandId, ManufacturerData, ScanSessionId,
    ScanStopResult, ServiceData,
};
use bleng::{OperationFailureObserver, OperationFailureRegistration, StartScanOperation};
use tokio::sync::Notify;

use crate::{RecoveryContext, ScanEvent, SupervisorOperationError};

#[derive(Debug, thiserror::Error)]
pub enum ScanSubmissionError {
    #[error("scan submission rejected: {0}")]
    Owner(#[from] SupervisorOperationError),
    #[error("another scan delivery is still attached")]
    DeliveryBusy,
    #[error("scan source owner retired")]
    OwnerRetired,
    #[error("scan delivery synchronization failed")]
    Unavailable,
}

pub const MAX_SCAN_ADVERTISEMENTS: usize = 64;
pub const MAX_SCAN_DATA_BYTES: usize = 256 * 1024;
// Separate terminal storage cannot be crowded out by a full advertisement queue.
const MAX_SCAN_STOP_DATA_BYTES: usize = 256 * 1024;
const MAX_PENDING_STOP_EVENTS: usize = 64;

/// Native Rust handoff; the operation retains its original single-consumer result and cancel.
/// Holding or dropping the observation endpoint neither stops nor cancels native scanning.
pub struct ScanHandoff {
    pub operation: Arc<StartScanOperation>,
    pub delivery: Arc<ScanDelivery>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, thiserror::Error)]
#[cfg_attr(feature = "ffi", derive(uniffi::Error))]
pub enum ScanDeliveryError {
    #[error("another scan pull is pending")]
    PullBusy,
    #[error("scan start result failed; observation detached without native rollback")]
    StartFailed,
    #[error("scan advertisement count budget exhausted")]
    AdvertisementOverflow,
    #[error("scan advertisement data budget exhausted")]
    ByteOverflow,
    #[error("scan stop error data budget exhausted")]
    StopDataOverflow,
    #[error("scan end-before-start staging budget exhausted")]
    PendingEventOverflow,
    #[error("scan source owner retired")]
    OwnerRetired,
    #[error("scan observation endpoint detached")]
    Detached,
    #[error("scan delivery synchronization failed")]
    Unavailable,
    #[error("scan start identity conflicts with its earlier binding")]
    ConflictingStart,
}

/// A point-in-time delivery snapshot, not native activity, operation completion or resource-zero.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ScanDeliverySnapshot {
    pub queued_advertisements: usize,
    pub queued_bytes: usize,
    pub queued_stop_bytes: usize,
    pub pull_pending: bool,
}

#[derive(Debug)]
enum Route {
    Pending(VecDeque<ScanEvent>),
    Bound(ScanSessionId),
}

#[derive(Debug)]
struct DeliveryState {
    route: Route,
    advertisements: VecDeque<AdvertisementObservation>,
    bytes: usize,
    stop_bytes: usize,
    detached: bool,
    terminal: Option<Result<(ScanSessionId, ScanStopResult), ScanDeliveryError>>,
}

impl DeliveryState {
    fn clear_buffer(&mut self) {
        if let Route::Pending(events) = &mut self.route {
            events.clear();
        }
        self.advertisements.clear();
        self.bytes = 0;
        self.stop_bytes = 0;
    }

    fn fail(&mut self, error: ScanDeliveryError) {
        self.clear_buffer();
        // A later close/stop must never conceal an earlier delivery failure.
        if !matches!(self.terminal, Some(Err(_))) {
            self.terminal = Some(Err(error));
        }
    }

    fn advertisement_count(&self) -> usize {
        match &self.route {
            Route::Pending(events) => events
                .iter()
                .filter(|event| matches!(event, ScanEvent::Advertisement { .. }))
                .count(),
            Route::Bound(_) => self.advertisements.len(),
        }
    }

    fn admit_advertisement(&mut self, advertisement: &AdvertisementObservation) -> bool {
        if self.advertisement_count() == MAX_SCAN_ADVERTISEMENTS {
            self.fail(ScanDeliveryError::AdvertisementOverflow);
            return false;
        }
        let bytes = self
            .bytes
            .saturating_add(advertisement_bytes(advertisement));
        if bytes > MAX_SCAN_DATA_BYTES {
            self.fail(ScanDeliveryError::ByteOverflow);
            return false;
        }
        self.bytes = bytes;
        true
    }

    fn offer_bound(&mut self, event: &ScanEvent) {
        if self.terminal.is_some() {
            return;
        }
        let Route::Bound(scan_id) = self.route else {
            unreachable!("bound replay requires a bound route")
        };
        match event {
            ScanEvent::Advertisement { advertisement }
                if advertisement.scan_session_id == scan_id =>
            {
                if !self.admit_advertisement(advertisement) {
                    return;
                }
                self.advertisements.push_back(advertisement.clone());
            }
            ScanEvent::Stopped {
                scan_id: source,
                result,
            } if *source == scan_id => {
                // A terminal never competes with advertisements for queue capacity.
                let bytes = stop_data_bytes(result);
                if bytes > MAX_SCAN_STOP_DATA_BYTES {
                    self.fail(ScanDeliveryError::StopDataOverflow);
                    return;
                }
                self.stop_bytes = bytes;
                self.terminal = Some(Ok((scan_id, result.clone())));
            }
            _ => {}
        }
    }
}

/// One owner/command-bound, single-pending-pull observation endpoint.
///
/// Only the owner's projection can produce observations. Normal stop drains earlier matching
/// advertisements then retains its result. Retirement/detach/failure clears undelivered data.
/// Already returned observations cannot be retracted; consumers must keep their source ownership.
pub struct ScanDelivery {
    source_context: RecoveryContext,
    start_command_id: CommandId,
    state: Mutex<DeliveryState>,
    changed: Notify,
    pulling: AtomicBool,
    start_failed: AtomicBool,
    start_failure_registration: OnceLock<OperationFailureRegistration>,
}

impl std::fmt::Debug for ScanDelivery {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ScanDelivery")
            .field("source_context", &self.source_context)
            .field("start_command_id", &self.start_command_id)
            .field("state", &self.state)
            .field("pulling", &self.pulling)
            .field("start_failed", &self.start_failed)
            .finish_non_exhaustive()
    }
}

impl OperationFailureObserver for ScanDelivery {
    fn operation_failed(&self) {
        // BLeng may call synchronously during registration or while delivery is locked.
        // Only mark and wake here; ordinary delivery entry points reconcile under state.
        self.start_failed.store(true, Ordering::Release);
        self.changed.notify_one();
    }
}

impl ScanDelivery {
    pub(crate) fn new(source_context: RecoveryContext, start_command_id: CommandId) -> Self {
        Self {
            source_context,
            start_command_id,
            state: Mutex::new(DeliveryState {
                route: Route::Pending(VecDeque::new()),
                advertisements: VecDeque::new(),
                bytes: 0,
                stop_bytes: 0,
                detached: false,
                terminal: None,
            }),
            changed: Notify::new(),
            pulling: AtomicBool::new(false),
            start_failed: AtomicBool::new(false),
            start_failure_registration: OnceLock::new(),
        }
    }

    pub(crate) fn observe_failure(self: &Arc<Self>, operation: &StartScanOperation) {
        let observer: Arc<dyn OperationFailureObserver> = self.clone();
        // No delivery lock: late registration can synchronously replay failure. The
        // delivery itself owns the receiver lifetime; BLeng retains only its Weak.
        let Some(registration) = operation.observe_failure(Arc::downgrade(&observer)) else {
            self.fail_unavailable();
            return;
        };
        if self.start_failure_registration.set(registration).is_err() {
            self.fail_unavailable();
        }
    }

    fn lock(&self) -> MutexGuard<'_, DeliveryState> {
        let mut state = self.state.lock().unwrap_or_else(|poisoned| {
            let mut state = poisoned.into_inner();
            state.fail(ScanDeliveryError::Unavailable);
            self.changed.notify_one();
            state
        });
        if self.start_failed.load(Ordering::Acquire) && !state.detached {
            state.detached = true;
            state.fail(ScanDeliveryError::StartFailed);
        }
        state
    }

    pub(crate) fn offer(&self, source: &RecoveryContext, event: &ScanEvent) {
        if source != &self.source_context {
            return;
        }
        let mut state = self.lock();
        if state.terminal.is_some() {
            // Keep the resource capability discoverable for explicit stop even after delivery
            // overflow. This does not restore publication or overwrite the first failure.
            if let ScanEvent::Started {
                command_id,
                scan_id,
            } = event
                && *command_id == self.start_command_id
                && matches!(state.route, Route::Pending(_))
            {
                state.route = Route::Bound(*scan_id);
            }
            return;
        }
        match event {
            ScanEvent::Started {
                command_id,
                scan_id,
            } => {
                if *command_id != self.start_command_id {
                    return;
                }
                match &state.route {
                    Route::Bound(bound) if bound != scan_id => {
                        state.fail(ScanDeliveryError::ConflictingStart);
                    }
                    Route::Bound(_) => return,
                    Route::Pending(_) => {
                        let Route::Pending(pending) =
                            std::mem::replace(&mut state.route, Route::Bound(*scan_id))
                        else {
                            unreachable!("pending route was checked")
                        };
                        state.bytes = 0;
                        state.stop_bytes = 0;
                        for event in pending {
                            state.offer_bound(&event);
                        }
                    }
                }
            }
            _ if matches!(state.route, Route::Bound(_)) => state.offer_bound(event),
            ScanEvent::Advertisement { advertisement } => {
                if !state.admit_advertisement(advertisement) {
                    drop(state);
                    self.changed.notify_one();
                    return;
                }
                if let Route::Pending(pending) = &mut state.route {
                    pending.push_back(event.clone());
                }
            }
            ScanEvent::Stopped { result, .. } => {
                let bytes = state.stop_bytes.saturating_add(stop_data_bytes(result));
                let Route::Pending(pending) = &mut state.route else {
                    unreachable!("bound route was handled")
                };
                let stops = pending
                    .iter()
                    .filter(|event| matches!(event, ScanEvent::Stopped { .. }))
                    .count();
                if stops == MAX_PENDING_STOP_EVENTS {
                    state.fail(ScanDeliveryError::PendingEventOverflow);
                } else if bytes > MAX_SCAN_STOP_DATA_BYTES {
                    state.fail(ScanDeliveryError::StopDataOverflow);
                } else {
                    pending.push_back(event.clone());
                    state.stop_bytes = bytes;
                }
            }
        }
        drop(state);
        self.changed.notify_one();
    }

    pub(crate) fn fail_unavailable(&self) {
        self.lock().fail(ScanDeliveryError::Unavailable);
        self.changed.notify_one();
    }

    pub(crate) fn close(&self) {
        self.lock().fail(ScanDeliveryError::OwnerRetired);
        self.changed.notify_one();
    }

    /// Ends observation only. Native stop/cancel and the original result remain independent.
    pub fn detach(&self) {
        let mut state = self.lock();
        state.detached = true;
        state.fail(ScanDeliveryError::Detached);
        drop(state);
        self.changed.notify_one();
    }

    #[cfg(feature = "ffi")]
    pub(crate) fn observed_scan_id(&self) -> Option<ScanSessionId> {
        match self.lock().route {
            Route::Bound(scan_id) => Some(scan_id),
            Route::Pending(_) => None,
        }
    }

    pub(crate) fn can_replace(&self) -> bool {
        let state = self.lock();
        state.detached || matches!(state.terminal, Some(Ok(_)))
    }

    /// Cancel-safe pending pull, usable without a Tokio runtime. The caller owns observation
    /// deadlines by dropping this future; doing so never cancels the BLE operation or scanning.
    /// Started is deliberately not delivered here: use the original operation's typed wait.
    pub async fn next_event(&self) -> Result<ScanEvent, ScanDeliveryError> {
        self.pulling
            .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .map_err(|_| ScanDeliveryError::PullBusy)?;
        let _pull = PullGuard(&self.pulling);
        loop {
            // notify_one retains a permit even when this Notified has not yet been polled.
            let changed = self.changed.notified();
            {
                let mut state = self.lock();
                if let Some(advertisement) = state.advertisements.pop_front() {
                    state.bytes -= advertisement_bytes(&advertisement);
                    return Ok(ScanEvent::Advertisement { advertisement });
                }
                if let Some(terminal) = &state.terminal {
                    return terminal
                        .clone()
                        .map(|(scan_id, result)| ScanEvent::Stopped { scan_id, result });
                }
            }
            changed.await;
        }
    }

    pub fn snapshot(&self) -> ScanDeliverySnapshot {
        let state = self.lock();
        ScanDeliverySnapshot {
            queued_advertisements: state.advertisement_count(),
            queued_bytes: state.bytes,
            queued_stop_bytes: state.stop_bytes,
            pull_pending: self.pulling.load(Ordering::Acquire),
        }
    }
}

struct PullGuard<'a>(&'a AtomicBool);

impl Drop for PullGuard<'_> {
    fn drop(&mut self) {
        self.0.store(false, Ordering::Release);
    }
}

fn stop_data_bytes(result: &ScanStopResult) -> usize {
    let ScanStopResult::Err { error } = result else {
        return 0;
    };
    let mut bytes = error.message.len();
    if let Some(details) = &error.platform_details {
        for value in [&details.code, &details.name, &details.message]
            .into_iter()
            .flatten()
        {
            bytes = bytes.saturating_add(value.len());
        }
    }
    bytes
}

fn advertisement_bytes(advertisement: &AdvertisementObservation) -> usize {
    // Empty strings/payloads still own Vec element storage. Charge it before clone so an
    // arbitrarily long list of empty entries cannot bypass the data budget.
    let mut bytes = advertisement
        .local_name
        .as_ref()
        .map_or(0, String::len)
        .saturating_add(
            advertisement
                .advertised_services
                .len()
                .saturating_mul(size_of::<BluetoothUuid>()),
        )
        .saturating_add(
            advertisement
                .manufacturer_data
                .len()
                .saturating_mul(size_of::<ManufacturerData>()),
        )
        .saturating_add(
            advertisement
                .service_data
                .len()
                .saturating_mul(size_of::<ServiceData>()),
        );
    for uuid in &advertisement.advertised_services {
        bytes = bytes.saturating_add(uuid.value.len());
    }
    for manufacturer in &advertisement.manufacturer_data {
        bytes = bytes.saturating_add(manufacturer.payload.len());
    }
    for service in &advertisement.service_data {
        bytes = bytes
            .saturating_add(service.service_uuid.value.len())
            .saturating_add(service.payload.len());
    }
    bytes
}

#[cfg(test)]
#[path = "tests/scan_delivery.rs"]
mod tests;
