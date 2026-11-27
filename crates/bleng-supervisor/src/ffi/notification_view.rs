//! Product projection of one notification subscription.
//!
//! A subscription is a stream with a liveness rule, not another request/response receipt. Enable
//! and disable stay ordinary typed operations; the values between them arrive through a bounded
//! lane that fails closed rather than dropping, exactly like scan delivery.
//!
//! The subscription identity never reaches the caller: it is learned from this subscription's own
//! enable completion, and disable is addressed through it, so a stale capability cannot disable
//! someone else's subscription.

use std::sync::{Arc, Mutex};

use blehub::{NotificationMode, NotificationModeRequest, SubscriptionId};
use bleng::{DisableNotificationsOperation, EnableNotificationsOperation};

use super::failure::{
    ForeignOperationCommandResult, ForeignOperationFailure, ForeignOperationFailureDomain,
    bleng_failure, normalize_timeout,
};
use super::gatt_view::{GattBinding, SupervisedGattCharacteristic};
use crate::{
    NotificationDelivery, NotificationDeliveryError, NotificationEndReason, NotificationEvent,
    NotificationSource, NotificationSubmissionError, SupervisorOperationError,
};

/// Bounded enable submission. A present capability is not an active subscription.
#[derive(Clone, uniffi::Record)]
pub struct ForeignNotificationSubscribeBeginResult {
    pub notifications: Option<Arc<SupervisedNotifications>>,
    pub failure: Option<ForeignOperationFailure>,
}

impl ForeignNotificationSubscribeBeginResult {
    fn failed(failure: ForeignOperationFailure) -> Self {
        Self {
            notifications: None,
            failure: Some(failure),
        }
    }
}

/// Enable completion. `actual_mode` is `None` when the platform does not report it, which is not
/// the same as "notification".
#[derive(Clone, Debug, uniffi::Record)]
pub struct ForeignNotificationSubscribeResult {
    pub actual_mode: Option<NotificationMode>,
    pub failure: Option<ForeignOperationFailure>,
}

impl ForeignNotificationSubscribeResult {
    fn failed(failure: ForeignOperationFailure) -> Self {
        Self {
            actual_mode: None,
            failure: Some(failure),
        }
    }
}

#[derive(Clone, uniffi::Record)]
pub struct ForeignNotificationDisableResult {
    pub operation: Option<Arc<SupervisedNotificationDisable>>,
    pub failure: Option<ForeignOperationFailure>,
}

impl ForeignNotificationDisableResult {
    fn failed(failure: ForeignOperationFailure) -> Self {
        Self {
            operation: None,
            failure: Some(failure),
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, uniffi::Enum)]
pub enum ForeignNotificationEndReason {
    /// The subscription was disabled, by the caller or the peer.
    Disabled,
    /// The connection ended, so the subscription could not continue.
    ConnectionClosed,
    /// This owner retired.
    OwnerClosed,
    /// This observation endpoint could not keep up; only this lane ended.
    Overflow,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, uniffi::Record)]
pub struct ForeignNotificationEnd {
    pub reason: ForeignNotificationEndReason,
}

/// Read-only backlog diagnostics. Counts never establish native teardown or delivery of a value
/// that was already returned.
#[derive(Clone, Copy, Debug, PartialEq, Eq, uniffi::Record)]
pub struct ForeignNotificationDeliverySnapshot {
    pub queued_values: u64,
    pub queued_bytes: u64,
    pub bound: bool,
    pub pull_pending: bool,
}

/// One observation: a value, a terminal, or a failure. Never more than one.
#[derive(Clone, Debug, uniffi::Record)]
pub struct ForeignNotificationObservation {
    pub value: Option<Vec<u8>>,
    pub ended: Option<ForeignNotificationEnd>,
    pub failure: Option<ForeignOperationFailure>,
}

impl ForeignNotificationObservation {
    fn failed(failure: ForeignOperationFailure) -> Self {
        Self {
            value: None,
            ended: None,
            failure: Some(failure),
        }
    }

    fn ended(reason: ForeignNotificationEndReason) -> Self {
        Self {
            value: None,
            ended: Some(ForeignNotificationEnd { reason }),
            failure: None,
        }
    }
}

/// One subscription capability: the original enable receipt plus its bounded value lane.
#[derive(uniffi::Object)]
pub struct SupervisedNotifications {
    binding: GattBinding,
    enable: Arc<EnableNotificationsOperation>,
    delivery: Arc<NotificationDelivery>,
    /// Learned from this subscription's own enable completion; disable requires it.
    subscription: Mutex<Option<SubscriptionId>>,
}

#[derive(uniffi::Object)]
pub struct SupervisedNotificationDisable {
    operation: Arc<DisableNotificationsOperation>,
}

impl SupervisedNotifications {
    fn new(
        binding: GattBinding,
        enable: Arc<EnableNotificationsOperation>,
        delivery: Arc<NotificationDelivery>,
    ) -> Arc<Self> {
        Arc::new(Self {
            binding,
            enable,
            delivery,
            subscription: Mutex::new(None),
        })
    }
}

fn unavailable() -> ForeignOperationFailure {
    ForeignOperationFailure::new(
        ForeignOperationFailureDomain::Lifecycle,
        "supervisor.unavailable",
        "supervisor is unavailable",
    )
}

fn delivery_failure(error: NotificationDeliveryError) -> ForeignOperationFailure {
    let (code, message) = match error {
        NotificationDeliveryError::ValueOverflow => (
            "delivery.value_overflow",
            "notification value backlog exhausted",
        ),
        NotificationDeliveryError::ByteOverflow => (
            "delivery.byte_overflow",
            "notification value byte budget exhausted",
        ),
        NotificationDeliveryError::OwnerRetired => (
            "delivery.owner_retired",
            "notification source owner retired",
        ),
        NotificationDeliveryError::Detached => (
            "delivery.detached",
            "notification observation endpoint detached",
        ),
        NotificationDeliveryError::Unavailable => (
            "delivery.unavailable",
            "notification delivery synchronization failed",
        ),
        NotificationDeliveryError::ConflictingSource => (
            "delivery.conflicting_source",
            "notification state change named another attribute",
        ),
    };
    ForeignOperationFailure::new(ForeignOperationFailureDomain::Delivery, code, message)
}

impl From<NotificationSubmissionError> for ForeignOperationFailure {
    fn from(error: NotificationSubmissionError) -> Self {
        match error {
            NotificationSubmissionError::Owner { error } => error.into(),
            NotificationSubmissionError::OwnerRetired => ForeignOperationFailure::new(
                ForeignOperationFailureDomain::Lifecycle,
                "delivery.owner_retired",
                "notification source owner retired",
            ),
            NotificationSubmissionError::Unavailable => ForeignOperationFailure::new(
                ForeignOperationFailureDomain::Delivery,
                "delivery.unavailable",
                "notification delivery synchronization failed",
            ),
        }
    }
}

#[uniffi::export]
impl SupervisedGattCharacteristic {
    /// Submits one enable and registers its value lane before submission, so no value can be lost
    /// between the enable's state change and this capability becoming observable.
    pub fn begin_enable_notifications(
        &self,
        mode: NotificationModeRequest,
    ) -> ForeignNotificationSubscribeBeginResult {
        let binding = self.binding().clone();
        let Some(supervisor) = binding.connection.supervisor() else {
            return ForeignNotificationSubscribeBeginResult::failed(unavailable());
        };
        let source = NotificationSource {
            connection: binding.connection.connection,
            session: binding.session,
            characteristic: self.id(),
        };
        match supervisor.submit_operation::<_, NotificationSubmissionError>(
            binding.connection.context.clone(),
            |supervisor, context| supervisor.begin_notifications_view(context, source, mode),
        ) {
            Ok(handoff) => ForeignNotificationSubscribeBeginResult {
                notifications: Some(SupervisedNotifications::new(
                    binding,
                    handoff.operation,
                    handoff.delivery,
                )),
                failure: None,
            },
            Err(error) => ForeignNotificationSubscribeBeginResult::failed(error.into()),
        }
    }
}

#[uniffi::export]
impl SupervisedNotifications {
    /// Consumes the original enable result and records the subscription this lane belongs to.
    pub async fn wait_result(&self, timeout_ms: f64) -> ForeignNotificationSubscribeResult {
        let timeout_ms = match normalize_timeout(timeout_ms) {
            Ok(timeout_ms) => timeout_ms,
            Err(failure) => return ForeignNotificationSubscribeResult::failed(*failure),
        };
        match self.enable.wait(timeout_ms).await {
            Ok(receipt) => {
                *self
                    .subscription
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner()) = Some(receipt.subscription);
                ForeignNotificationSubscribeResult {
                    actual_mode: receipt.actual_mode,
                    failure: None,
                }
            }
            Err(error) => ForeignNotificationSubscribeResult::failed(bleng_failure(
                ForeignOperationFailureDomain::Operation,
                error,
            )),
        }
    }

    /// Cancel-safe pending pull. A timeout is the caller's deadline, not a native teardown.
    ///
    /// The deadline runs on this operation's own runtime, so a caller never needs its own timers
    /// and dropping the caller only aborts observation.
    pub async fn next_observation(&self, timeout_ms: f64) -> ForeignNotificationObservation {
        let timeout_ms = match normalize_timeout(timeout_ms) {
            Ok(timeout_ms) => timeout_ms,
            Err(failure) => return ForeignNotificationObservation::failed(*failure),
        };
        let delivery = Arc::clone(&self.delivery);
        let observation = self
            .enable
            .observe_with_timeout(timeout_ms, async move { delivery.next_event().await });
        match observation.await {
            Ok(Ok(NotificationEvent::Value { value })) => ForeignNotificationObservation {
                value: Some(value),
                ended: None,
                failure: None,
            },
            Ok(Ok(NotificationEvent::Ended(end))) => {
                ForeignNotificationObservation::ended(match end.reason {
                    NotificationEndReason::Disabled => ForeignNotificationEndReason::Disabled,
                    NotificationEndReason::ConnectionClosed => {
                        ForeignNotificationEndReason::ConnectionClosed
                    }
                    NotificationEndReason::OwnerClosed => ForeignNotificationEndReason::OwnerClosed,
                })
            }
            // Retirement is delivered as a terminal, never as a synthesized native disable.
            Ok(Err(NotificationDeliveryError::OwnerRetired)) => {
                ForeignNotificationObservation::ended(ForeignNotificationEndReason::OwnerClosed)
            }
            // A slow consumer ends its own observation lane; the native subscription and every
            // other operation keep running. This is not a runtime-wide fault.
            Ok(Err(
                NotificationDeliveryError::ValueOverflow | NotificationDeliveryError::ByteOverflow,
            )) => ForeignNotificationObservation::ended(ForeignNotificationEndReason::Overflow),
            Ok(Err(error)) => ForeignNotificationObservation::failed(delivery_failure(error)),
            Err(error) => ForeignNotificationObservation::failed(bleng_failure(
                ForeignOperationFailureDomain::Observation,
                error,
            )),
        }
    }

    pub fn begin_disable_view(&self) -> ForeignNotificationDisableResult {
        let subscription = *self
            .subscription
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let Some(subscription) = subscription else {
            return ForeignNotificationDisableResult::failed(ForeignOperationFailure::new(
                ForeignOperationFailureDomain::Submission,
                "notification.subscription_unknown",
                "no subscription has been observed for this capability",
            ));
        };
        let Some(supervisor) = self.binding.connection.supervisor() else {
            return ForeignNotificationDisableResult::failed(unavailable());
        };
        let binding = &self.binding;
        match supervisor.submit_operation::<_, SupervisorOperationError>(
            binding.connection.context.clone(),
            |supervisor, context| {
                supervisor.begin_disable_notifications(
                    context,
                    binding.connection.connection,
                    binding.session,
                    subscription,
                )
            },
        ) {
            Ok(operation) => ForeignNotificationDisableResult {
                operation: Some(Arc::new(SupervisedNotificationDisable { operation })),
                failure: None,
            },
            Err(error) => ForeignNotificationDisableResult::failed(error.into()),
        }
    }

    /// Ends observation only. The native subscription and an explicit disable stay independent.
    pub fn detach(&self) {
        self.delivery.detach();
    }

    pub fn delivery_snapshot(&self) -> ForeignNotificationDeliverySnapshot {
        let snapshot = self.delivery.snapshot();
        ForeignNotificationDeliverySnapshot {
            queued_values: snapshot.queued_values,
            queued_bytes: snapshot.queued_bytes,
            bound: snapshot.bound,
            pull_pending: snapshot.pull_pending,
        }
    }
}

#[uniffi::export]
impl SupervisedNotificationDisable {
    pub async fn wait_result(&self, timeout_ms: f64) -> ForeignOperationCommandResult {
        let timeout_ms = match normalize_timeout(timeout_ms) {
            Ok(timeout_ms) => timeout_ms,
            Err(failure) => {
                return ForeignOperationCommandResult::from_failure(Some(*failure));
            }
        };
        ForeignOperationCommandResult::from_failure(
            self.operation
                .wait(timeout_ms)
                .await
                .err()
                .map(|error| bleng_failure(ForeignOperationFailureDomain::Operation, error)),
        )
    }

    /// Requests cancellation without consuming the result; no remote rollback.
    pub async fn cancel_result(&self) -> ForeignOperationCommandResult {
        ForeignOperationCommandResult::from_failure(
            self.operation
                .cancel()
                .await
                .err()
                .map(|error| bleng_failure(ForeignOperationFailureDomain::Operation, error)),
        )
    }
}
