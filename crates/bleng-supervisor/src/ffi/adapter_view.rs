//! Product projection of the owner's adapter-state stream.
//!
//! This is a view over the delivery lane the projection already feeds, never a second reader: the
//! owner's one event reader publishes `AdapterSnapshotChanged`, the projection files it into the
//! lanes, and a pull here only takes what its own lane holds. Nothing in this module can change the
//! adapter, the engine or any operation, and a pull deadline retires the pull — not the stream.

use std::sync::Arc;

use blehub::AdapterSnapshot;
use bleng::Bleng;

use super::failure::{
    ForeignOperationFailure, ForeignOperationFailureDomain, bleng_failure, normalize_timeout,
};
use super::{ForeignOwnerContext, Supervisor};
use crate::adapter_delivery::{
    AdapterDelivery, AdapterDeliveryError, AdapterHandoff, AdapterSubmissionError,
};

#[derive(Clone, Copy, Debug, PartialEq, Eq, uniffi::Enum)]
pub enum ForeignAdapterEndReason {
    /// The engine was retired. This ends *observation*; it is not a claim about the radio.
    OwnerClosed,
    /// The endpoint was detached. The stream keeps running for any other observer.
    Detached,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, uniffi::Record)]
pub struct ForeignAdapterEnd {
    pub reason: ForeignAdapterEndReason,
}

/// One settled adapter observation: a snapshot, the end of observation, or a failure.
///
/// Exactly one arm is set. `ended` is retained like the scan lane's terminal, so a caller that polls
/// again learns it again instead of seeing a synthesized failure.
#[derive(Clone, Debug, PartialEq, Eq, uniffi::Record)]
pub struct ForeignAdapterObservation {
    pub snapshot: Option<AdapterSnapshot>,
    pub ended: Option<ForeignAdapterEnd>,
    pub failure: Option<ForeignOperationFailure>,
}

impl ForeignAdapterObservation {
    fn snapshot(snapshot: AdapterSnapshot) -> Self {
        Self {
            snapshot: Some(snapshot),
            ended: None,
            failure: None,
        }
    }

    fn ended(reason: ForeignAdapterEndReason) -> Self {
        Self {
            snapshot: None,
            ended: Some(ForeignAdapterEnd { reason }),
            failure: None,
        }
    }

    fn failed(failure: ForeignOperationFailure) -> Self {
        Self {
            snapshot: None,
            ended: None,
            failure: Some(failure),
        }
    }

    /// Retirement and detach end observation; they are not failures of the radio.
    fn from_delivery_error(error: AdapterDeliveryError) -> Self {
        use ForeignOperationFailureDomain as Domain;
        match error {
            AdapterDeliveryError::OwnerRetired => Self::ended(ForeignAdapterEndReason::OwnerClosed),
            AdapterDeliveryError::Detached => Self::ended(ForeignAdapterEndReason::Detached),
            AdapterDeliveryError::PullBusy => Self::failed(ForeignOperationFailure::new(
                Domain::Delivery,
                "delivery.pull_busy",
                "another adapter observation pull is pending",
            )),
            AdapterDeliveryError::Overflow => Self::failed(ForeignOperationFailure::new(
                Domain::Delivery,
                "delivery.snapshot_overflow",
                "adapter snapshot budget exhausted",
            )),
            AdapterDeliveryError::Unavailable => Self::failed(ForeignOperationFailure::new(
                Domain::Delivery,
                "delivery.unavailable",
                "adapter observation synchronization failed",
            )),
        }
    }
}

/// Rust-created, owner-bound adapter observation capability.
///
/// Dropping it detaches observation only: it neither closes the owner nor touches the radio.
#[derive(uniffi::Object)]
pub struct SupervisedAdapterObservation {
    bleng: Arc<Bleng>,
    delivery: Arc<AdapterDelivery>,
}

impl SupervisedAdapterObservation {
    pub(in crate::ffi) fn new(handoff: AdapterHandoff) -> Arc<Self> {
        Arc::new(Self {
            bleng: handoff.bleng,
            delivery: handoff.delivery,
        })
    }
}

impl Drop for SupervisedAdapterObservation {
    fn drop(&mut self) {
        // Observation lifetime only; no implicit owner close or adapter change.
        self.delivery.detach();
    }
}

#[uniffi::export]
impl SupervisedAdapterObservation {
    /// Ends observation only. The adapter, the engine and every other observer are untouched.
    pub fn detach(&self) {
        self.delivery.detach();
    }

    /// At most one pending pull. A deadline is the caller's bound and does not end the stream.
    pub async fn next_snapshot(&self, timeout_ms: f64) -> ForeignAdapterObservation {
        let timeout_ms = match normalize_timeout(timeout_ms) {
            Ok(timeout_ms) => timeout_ms,
            Err(failure) => return ForeignAdapterObservation::failed(*failure),
        };
        // The deadline is applied on the owner's own runtime: a foreign caller may have neither
        // an executor nor a timer, and this lane has no operation whose wait would provide one.
        let delivery = Arc::clone(&self.delivery);
        let wait = self
            .bleng
            .observe_with_timeout(timeout_ms, async move { delivery.next_event().await })
            .await;
        match wait {
            Ok(Ok(snapshot)) => ForeignAdapterObservation::snapshot(snapshot),
            Ok(Err(error)) => ForeignAdapterObservation::from_delivery_error(error),
            Err(error) => ForeignAdapterObservation::failed(bleng_failure(
                ForeignOperationFailureDomain::Observation,
                error,
            )),
        }
    }
}

#[derive(Clone, uniffi::Record)]
pub struct ForeignAdapterObservationBeginResult {
    pub adapter_observation: Option<Arc<SupervisedAdapterObservation>>,
    pub failure: Option<ForeignOperationFailure>,
}

#[uniffi::export]
impl Supervisor {
    /// Opens one adapter-state observation for this owner. Observation only: opening, pulling,
    /// detaching or dropping it never changes the radio or the engine's recovery.
    pub fn begin_observe_adapter_view(
        self: Arc<Self>,
        context: ForeignOwnerContext,
    ) -> ForeignAdapterObservationBeginResult {
        match self.submit_operation(context, |supervisor, context| {
            supervisor.observe_adapter(context)
        }) {
            Ok(delivery) => ForeignAdapterObservationBeginResult {
                adapter_observation: Some(SupervisedAdapterObservation::new(delivery)),
                failure: None,
            },
            Err(error) => ForeignAdapterObservationBeginResult {
                adapter_observation: None,
                failure: Some(adapter_submission_failure(error)),
            },
        }
    }
}

fn adapter_submission_failure(error: AdapterSubmissionError) -> ForeignOperationFailure {
    use ForeignOperationFailureDomain as Domain;
    match error {
        AdapterSubmissionError::Owner(error) => ForeignOperationFailure::new(
            Domain::Submission,
            "adapter.observation_rejected",
            &error.to_string(),
        ),
        AdapterSubmissionError::OwnerRetired => ForeignOperationFailure::new(
            Domain::Lifecycle,
            "adapter.owner_retired",
            "adapter source owner retired",
        ),
        AdapterSubmissionError::Unavailable => ForeignOperationFailure::new(
            Domain::Submission,
            "adapter.observation_unavailable",
            "adapter observation synchronization failed",
        ),
    }
}
