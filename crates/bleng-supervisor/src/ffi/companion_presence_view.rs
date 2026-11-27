//! Product projection of the owner's companion-presence stream.
//!
//! This is a view over the delivery lane the projection already feeds, never a second reader: the
//! owner's one event reader publishes `CompanionPresenceChanged`, the projection files it into the
//! lanes, and a pull here only takes what its own lane holds. Nothing in this module can associate,
//! connect, change the adapter or touch any operation, and a pull deadline retires the pull — not
//! the stream.

use std::sync::Arc;

use bleng::Bleng;

use super::failure::{
    ForeignOperationFailure, ForeignOperationFailureDomain, bleng_failure, normalize_timeout,
};
use super::{ForeignOwnerContext, Supervisor};
use crate::companion_presence::{
    CompanionPresence, CompanionPresenceDelivery, CompanionPresenceDeliveryError,
    CompanionPresenceHandoff, CompanionPresenceSubmissionError,
};

#[derive(Clone, Copy, Debug, PartialEq, Eq, uniffi::Enum)]
pub enum ForeignCompanionPresenceEndReason {
    /// The engine was retired. This ends *observation*; it is not a claim about the association.
    OwnerClosed,
    /// The endpoint was detached. The stream keeps running for any other observer.
    Detached,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, uniffi::Record)]
pub struct ForeignCompanionPresenceEnd {
    pub reason: ForeignCompanionPresenceEndReason,
}

/// One presence transition for one associated peer.
///
/// `present` never means connected; `key` reuses the owner-scoped inventory key so it is comparable
/// with `peripheral.key()` for the same peer, and `local_name` is the platform's association label
/// when it has one.
#[derive(Clone, Debug, PartialEq, Eq, uniffi::Record)]
pub struct ForeignCompanionPresence {
    pub present: bool,
    pub key: String,
    pub local_name: Option<String>,
}

/// One settled presence observation: a transition, the end of observation, or a failure.
///
/// Exactly one arm is set. `ended` is retained like the scan lane's terminal, so a caller that polls
/// again learns it again instead of seeing a synthesized failure.
#[derive(Clone, Debug, PartialEq, Eq, uniffi::Record)]
pub struct ForeignCompanionPresenceObservation {
    pub presence: Option<ForeignCompanionPresence>,
    pub ended: Option<ForeignCompanionPresenceEnd>,
    pub failure: Option<ForeignOperationFailure>,
}

impl ForeignCompanionPresenceObservation {
    fn presence(presence: CompanionPresence) -> Self {
        Self {
            presence: Some(ForeignCompanionPresence {
                present: presence.present,
                key: presence.key,
                local_name: presence.local_name,
            }),
            ended: None,
            failure: None,
        }
    }

    fn ended(reason: ForeignCompanionPresenceEndReason) -> Self {
        Self {
            presence: None,
            ended: Some(ForeignCompanionPresenceEnd { reason }),
            failure: None,
        }
    }

    fn failed(failure: ForeignOperationFailure) -> Self {
        Self {
            presence: None,
            ended: None,
            failure: Some(failure),
        }
    }

    /// Retirement and detach end observation; they are not failures of the association.
    fn from_delivery_error(error: CompanionPresenceDeliveryError) -> Self {
        use ForeignOperationFailureDomain as Domain;
        match error {
            CompanionPresenceDeliveryError::OwnerRetired => {
                Self::ended(ForeignCompanionPresenceEndReason::OwnerClosed)
            }
            CompanionPresenceDeliveryError::Detached => {
                Self::ended(ForeignCompanionPresenceEndReason::Detached)
            }
            CompanionPresenceDeliveryError::PullBusy => Self::failed(ForeignOperationFailure::new(
                Domain::Delivery,
                "delivery.pull_busy",
                "another companion presence pull is pending",
            )),
            CompanionPresenceDeliveryError::Overflow => Self::failed(ForeignOperationFailure::new(
                Domain::Delivery,
                "delivery.presence_overflow",
                "companion presence budget exhausted",
            )),
            CompanionPresenceDeliveryError::Unavailable => {
                Self::failed(ForeignOperationFailure::new(
                    Domain::Delivery,
                    "delivery.unavailable",
                    "companion presence observation synchronization failed",
                ))
            }
        }
    }
}

/// Rust-created, owner-bound companion-presence observation capability.
///
/// Dropping it detaches observation only: it neither closes the owner nor touches the association.
#[derive(uniffi::Object)]
pub struct SupervisedCompanionPresenceObservation {
    bleng: Arc<Bleng>,
    delivery: Arc<CompanionPresenceDelivery>,
}

impl SupervisedCompanionPresenceObservation {
    pub(in crate::ffi) fn new(handoff: CompanionPresenceHandoff) -> Arc<Self> {
        Arc::new(Self {
            bleng: handoff.bleng,
            delivery: handoff.delivery,
        })
    }
}

impl Drop for SupervisedCompanionPresenceObservation {
    fn drop(&mut self) {
        // Observation lifetime only; no implicit owner close or association change.
        self.delivery.detach();
    }
}

#[uniffi::export]
impl SupervisedCompanionPresenceObservation {
    /// Ends observation only. The association, the engine and every other observer are untouched.
    pub fn detach(&self) {
        self.delivery.detach();
    }

    /// At most one pending pull. A deadline is the caller's bound and does not end the stream.
    pub async fn next_event(&self, timeout_ms: f64) -> ForeignCompanionPresenceObservation {
        let timeout_ms = match normalize_timeout(timeout_ms) {
            Ok(timeout_ms) => timeout_ms,
            Err(failure) => return ForeignCompanionPresenceObservation::failed(*failure),
        };
        // The deadline is applied on the owner's own runtime: a foreign caller may have neither
        // an executor nor a timer, and this lane has no operation whose wait would provide one.
        let delivery = Arc::clone(&self.delivery);
        let wait = self
            .bleng
            .observe_with_timeout(timeout_ms, async move { delivery.next_event().await })
            .await;
        match wait {
            Ok(Ok(presence)) => ForeignCompanionPresenceObservation::presence(presence),
            Ok(Err(error)) => ForeignCompanionPresenceObservation::from_delivery_error(error),
            Err(error) => ForeignCompanionPresenceObservation::failed(bleng_failure(
                ForeignOperationFailureDomain::Observation,
                error,
            )),
        }
    }
}

#[derive(Clone, uniffi::Record)]
pub struct ForeignCompanionPresenceBeginResult {
    pub presence_observation: Option<Arc<SupervisedCompanionPresenceObservation>>,
    pub failure: Option<ForeignOperationFailure>,
}

#[uniffi::export]
impl Supervisor {
    /// Opens one companion-presence observation for this owner. Observation only: opening, pulling,
    /// detaching or dropping it never associates, connects, or changes the adapter or the engine.
    pub fn begin_observe_companion_presence_view(
        self: Arc<Self>,
        context: ForeignOwnerContext,
    ) -> ForeignCompanionPresenceBeginResult {
        match self.submit_operation(context, |supervisor, context| {
            supervisor.observe_companion_presence(context)
        }) {
            Ok(handoff) => ForeignCompanionPresenceBeginResult {
                presence_observation: Some(SupervisedCompanionPresenceObservation::new(handoff)),
                failure: None,
            },
            Err(error) => ForeignCompanionPresenceBeginResult {
                presence_observation: None,
                failure: Some(presence_submission_failure(error)),
            },
        }
    }
}

fn presence_submission_failure(error: CompanionPresenceSubmissionError) -> ForeignOperationFailure {
    use ForeignOperationFailureDomain as Domain;
    match error {
        CompanionPresenceSubmissionError::Owner(error) => ForeignOperationFailure::new(
            Domain::Submission,
            "companion.presence_observation_rejected",
            &error.to_string(),
        ),
        CompanionPresenceSubmissionError::OwnerRetired => ForeignOperationFailure::new(
            Domain::Lifecycle,
            "companion.presence_owner_retired",
            "companion presence source owner retired",
        ),
        CompanionPresenceSubmissionError::Unavailable => ForeignOperationFailure::new(
            Domain::Submission,
            "companion.presence_observation_unavailable",
            "companion presence observation synchronization failed",
        ),
    }
}
