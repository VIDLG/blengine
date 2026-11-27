//! Native scan proxy. IDs and context stay below the forthcoming Nitro product projection.

use std::sync::{Arc, OnceLock, Weak};

use blehub::ScanSessionId;
use bleng::{BlengError, StartScanOperation, StopScanOperation};

use super::{ForeignOwnerContext, Supervisor};
use crate::{
    ScanDelivery, ScanDeliveryError, ScanEvent, ScanHandoff, ScanSubmissionError,
    SupervisorOperationError,
};

/// Distinguishes operation results, pull observation failures and delivery-local failures.
/// Expected errors retain their original typed cause; no matching of diagnostic strings.
/// Crate-internal: the exported record views convert this into a `ForeignOperationFailure`
/// code, so the FFI image declares no error type nobody can receive.
#[derive(Debug, thiserror::Error)]
pub(super) enum ScanSessionError {
    #[error("scan submission rejected: {error}")]
    Submission {
        #[from]
        error: SupervisorOperationError,
    },
    #[error("scan delivery failed: {error}")]
    Delivery { error: ScanDeliveryError },
    #[error("another scan delivery is still attached")]
    DeliveryBusy,
    #[error("no scan identity has been confirmed or observed")]
    StartNotObserved,
    #[error("scan operation failed: {error}")]
    Operation { error: BlengError },
    #[error("scan observation failed: {error}")]
    Observation { error: BlengError },
}

impl From<ScanSubmissionError> for ScanSessionError {
    fn from(error: ScanSubmissionError) -> Self {
        match error {
            ScanSubmissionError::Owner(error) => Self::Submission { error },
            ScanSubmissionError::DeliveryBusy => Self::DeliveryBusy,
            ScanSubmissionError::OwnerRetired => Self::Delivery {
                error: ScanDeliveryError::OwnerRetired,
            },
            ScanSubmissionError::Unavailable => Self::Delivery {
                error: ScanDeliveryError::Unavailable,
            },
        }
    }
}

/// Read-only native diagnostics. These counts do not establish native scan stop/resource-zero.
#[derive(Clone, Debug, PartialEq, Eq, uniffi::Record)]
pub struct ForeignScanDeliverySnapshot {
    pub queued_advertisements: u64,
    pub queued_bytes: u64,
    pub queued_stop_bytes: u64,
    pub pull_pending: bool,
}

/// Original start control/result and a bounded observation stream for one source owner.
///
/// This proxy retains neither the Supervisor nor Bleng object strongly. The original operation
/// keeps its executor alive, not its Hub open. Stop always revalidates the fixed source context.
#[derive(uniffi::Object)]
pub struct SupervisedScan {
    supervisor: Weak<Supervisor>,
    context: ForeignOwnerContext,
    operation: Arc<StartScanOperation>,
    delivery: Arc<ScanDelivery>,
    // The original wait may finish before the public reader consumes Started. This immutable
    // resource receipt enables stop without requiring that reader to have flushed first.
    confirmed_scan: OnceLock<ScanSessionId>,
}

impl SupervisedScan {
    pub(in crate::ffi) fn source_context(&self) -> &ForeignOwnerContext {
        &self.context
    }

    /// Weak host linkage for capabilities derived from this scan's advertisements.
    pub(in crate::ffi) fn source_supervisor(&self) -> Weak<Supervisor> {
        self.supervisor.clone()
    }

    pub(in crate::ffi) fn new(
        supervisor: &Arc<Supervisor>,
        context: ForeignOwnerContext,
        handoff: ScanHandoff,
    ) -> Arc<Self> {
        Arc::new(Self {
            supervisor: Arc::downgrade(supervisor),
            context,
            operation: handoff.operation,
            delivery: handoff.delivery,
            confirmed_scan: OnceLock::new(),
        })
    }
}

impl Drop for SupervisedScan {
    fn drop(&mut self) {
        // Observation lifetime only; no implicit BLE cancellation or stop.
        self.delivery.detach();
    }
}

/// Raw typed steps behind the exported record views.
///
/// Not exported: a foreign caller gets one spelling per behaviour (`start_result`,
/// `cancel_start_result`, `next_observation`, `begin_stop_view`), and these stay the
/// implementation those adapters wrap. They remain Rust-public because this crate's own
/// integration tests assert the typed results directly.
impl SupervisedScan {
    /// Consumes the original start result once, without exposing its raw resource identity.
    /// A deadline/invalid/duplicate wait does not detach delivery or request cancellation.
    /// Start-failure delivery is driven by BLeng's independent observer, not this wait.
    pub(super) async fn wait_until_started(&self, timeout_ms: u64) -> Result<(), ScanSessionError> {
        match self.operation.wait(timeout_ms).await {
            Ok(scan_id) => {
                let _ = self.confirmed_scan.set(scan_id);
                Ok(())
            }
            Err(error) => Err(ScanSessionError::Operation { error }),
        }
    }

    /// Original cancellation receipt, not evidence of scan teardown or start success.
    pub(super) async fn cancel_start(&self) -> Result<(), ScanSessionError> {
        self.operation
            .cancel()
            .await
            .map_err(|error| ScanSessionError::Operation { error })
    }

    /// Waits on the operation's existing owner executor, even from a foreign/timerless caller.
    /// Timeout retires this pull only. Already returned observations cannot be retracted.
    pub(super) async fn next_event(&self, timeout_ms: u64) -> Result<ScanEvent, ScanSessionError> {
        let delivery = Arc::clone(&self.delivery);
        self.operation
            .observe_with_timeout(timeout_ms, async move { delivery.next_event().await })
            .await
            .map_err(|error| ScanSessionError::Observation { error })?
            .map_err(|error| ScanSessionError::Delivery { error })
    }

    /// Bounded context-fenced submission of the original typed stop operation.
    /// Waiting, cancellation and asynchronous native outcomes retain their original semantics.
    pub(super) fn begin_stop(&self) -> Result<Arc<StopScanOperation>, ScanSessionError> {
        let supervisor = self
            .supervisor
            .upgrade()
            .ok_or(ScanSessionError::Delivery {
                error: ScanDeliveryError::OwnerRetired,
            })?;
        supervisor.submit_operation(self.context.clone(), |supervisor, context| {
            // Owner/context/phase failures precede a missing resource. Keep this borrowed
            // capability under the same submission lock through the bounded begin call.
            let owner = supervisor.operation_owner(context)?;
            let scan_id = self
                .confirmed_scan
                .get()
                .copied()
                .or_else(|| self.delivery.observed_scan_id())
                .ok_or(ScanSessionError::StartNotObserved)?;
            owner
                .begin_stop_scan(scan_id)
                .map_err(SupervisorOperationError::from)
                .map_err(ScanSessionError::from)
        })
    }
}

#[uniffi::export]
impl SupervisedScan {
    /// Ends observation only and releases its slot; the first failure remains observable.
    pub fn detach(&self) {
        self.delivery.detach();
    }

    /// Whether this lane has observed a confirmed start that has not ended.
    ///
    /// `false` means the engine does not know a scan is active: the start is unconfirmed, or a
    /// terminal (stop, failure, owner retirement, or this lane's own overflow) has been observed.
    /// It never claims the native scan has stopped.
    pub fn is_scanning(&self) -> bool {
        self.delivery.is_scanning()
    }

    pub fn observation_snapshot(&self) -> ForeignScanDeliverySnapshot {
        let snapshot = self.delivery.snapshot();
        ForeignScanDeliverySnapshot {
            // All counters are bounded far below u64::MAX on maintained platforms.
            queued_advertisements: snapshot.queued_advertisements as u64,
            queued_bytes: snapshot.queued_bytes as u64,
            queued_stop_bytes: snapshot.queued_stop_bytes as u64,
            pull_pending: snapshot.pull_pending,
        }
    }
}
