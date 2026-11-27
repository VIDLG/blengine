//! Bounded, single-pending-pull delivery for one notification subscription.
//!
//! A subscription is a *stream*, not a receipt: values arrive from the owner's existing single
//! event reader and a slow consumer must never silently lose one. Overflow therefore fails the
//! stream closed instead of dropping, which is the same policy as scan delivery.
//!
//! The lane is created when the enable is submitted, so values cannot slip in between the enable's
//! state change and a later registration: binding happens on the state change carrying this
//! lane's own enable command id.

use std::collections::VecDeque;
use std::sync::{
    Arc, Mutex, MutexGuard,
    atomic::{AtomicBool, Ordering},
};

use blehub::{
    BleEvent, CommandId, ConnectionId, ConnectionState, ConnectionStateResult,
    GattCharacteristicId, GattSessionId, SubscriptionId,
};
use bleng::EnableNotificationsOperation;
use tokio::sync::Notify;

use crate::SupervisorOperationError;
use crate::pull_guard::PullGuard;
use crate::recovery::RecoveryContext;

#[derive(Debug, thiserror::Error)]
pub enum NotificationSubmissionError {
    #[error("notification submission rejected: {error}")]
    Owner {
        #[from]
        error: SupervisorOperationError,
    },
    #[error("owner retired before notification submission")]
    OwnerRetired,
    #[error("notification delivery bookkeeping unavailable")]
    Unavailable,
}

/// The original enable operation plus the lane registered before it was submitted.
pub struct NotificationHandoff {
    pub operation: Arc<EnableNotificationsOperation>,
    pub delivery: Arc<NotificationDelivery>,
}

/// A consumer that cannot keep up is a failure of this stream, not a silent drop.
pub const MAX_NOTIFICATION_VALUES: usize = 64;
pub const MAX_NOTIFICATION_BYTES: u64 = 16 * 1024;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum NotificationDeliveryError {
    /// The backlog exceeded [`MAX_NOTIFICATION_VALUES`].
    ValueOverflow,
    /// The backlog exceeded [`MAX_NOTIFICATION_BYTES`].
    ByteOverflow,
    /// The owner retired; the subscription no longer exists.
    OwnerRetired,
    /// The caller released this observation endpoint.
    Detached,
    /// Delivery bookkeeping was poisoned or the owner dropped it.
    Unavailable,
    /// An event claimed this lane's enable command but named another attribute.
    ConflictingSource,
}

/// Which attribute/session this lane observes. Known at submit time, unlike the subscription.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct NotificationSource {
    pub connection: ConnectionId,
    pub session: GattSessionId,
    pub characteristic: GattCharacteristicId,
}

/// One delivered observation. `Ended` is a terminal, never a synthetic value.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum NotificationEvent {
    /// One characteristic value exactly as the backend reported it.
    Value { value: Vec<u8> },
    /// The subscription itself ended, so no further value can arrive.
    Ended(NotificationEnd),
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum NotificationEndReason {
    /// The peer/stack disabled the subscription, or the caller's disable reached it.
    Disabled,
    /// The connection ended, so the subscription cannot continue.
    ConnectionClosed,
    /// This owner retired.
    OwnerClosed,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct NotificationEnd {
    pub reason: NotificationEndReason,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct NotificationDeliverySnapshot {
    pub queued_values: u64,
    pub queued_bytes: u64,
    pub bound: bool,
    pub pull_pending: bool,
}

#[derive(Debug, Default)]
struct DeliveryState {
    /// `Some` once this lane observed its own enable complete.
    bound: Option<SubscriptionId>,
    values: VecDeque<Vec<u8>>,
    bytes: u64,
    /// First terminal wins: a later disable or close must not erase an overflow.
    terminal: Option<Result<NotificationEnd, NotificationDeliveryError>>,
    detached: bool,
}

impl DeliveryState {
    fn clear(&mut self) {
        self.values.clear();
        self.bytes = 0;
    }

    fn fail(&mut self, error: NotificationDeliveryError) {
        self.clear();
        if !matches!(self.terminal, Some(Err(_))) {
            self.terminal = Some(Err(error));
        }
    }

    fn end(&mut self, reason: NotificationEndReason) {
        self.clear();
        if self.terminal.is_none() {
            self.terminal = Some(Ok(NotificationEnd { reason }));
        }
    }
}

/// One owner/subscription-bound observation endpoint.
///
/// Only this owner's own events can produce values: every candidate is matched against the
/// connection, GATT session, characteristic and finally the subscription identity this lane
/// learned from its own enable completion.
///
/// The end of the stream is a retained fact, not a consumable receipt: reading it twice returns it
/// twice, so a consumer that polls on its own schedule can still learn why the subscription ended.
/// Exactly-once belongs to the original enable result (`SupervisedNotifications::wait_result`).
/// Retirement and detach replace the retained end with a definite failure; the first failure to
/// arrive is never overwritten by a later one.
pub struct NotificationDelivery {
    source_context: RecoveryContext,
    enable_command_id: CommandId,
    source: NotificationSource,
    state: Mutex<DeliveryState>,
    changed: Notify,
    pulling: AtomicBool,
}

impl std::fmt::Debug for NotificationDelivery {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("NotificationDelivery")
            .field("source_context", &self.source_context)
            .field("enable_command_id", &self.enable_command_id)
            .field("source", &self.source)
            .field("state", &self.state)
            .field("pulling", &self.pulling)
            .finish()
    }
}

impl NotificationDelivery {
    pub(crate) fn new(
        source_context: RecoveryContext,
        enable_command_id: CommandId,
        source: NotificationSource,
    ) -> Self {
        Self {
            source_context,
            enable_command_id,
            source,
            state: Mutex::new(DeliveryState::default()),
            changed: Notify::new(),
            pulling: AtomicBool::new(false),
        }
    }

    fn lock(&self) -> MutexGuard<'_, DeliveryState> {
        self.state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    /// Offers one canonical event. Only this lane's own source and subscription are accepted.
    pub(crate) fn offer(&self, event: &BleEvent) {
        let mut state = self.lock();
        if state.terminal.is_some() {
            return;
        }
        match event {
            BleEvent::NotificationStateChanged {
                command_id,
                connection_id,
                gatt_session_id,
                subscription_id,
                characteristic_id,
                state: notification,
            } => {
                let identity_matches = *connection_id == self.source.connection
                    && *gatt_session_id == self.source.session
                    && *characteristic_id == self.source.characteristic;
                if state.bound.is_none() {
                    // Bind only on this lane's own enable completion.
                    if *command_id != self.enable_command_id {
                        return;
                    }
                    if !identity_matches {
                        state.fail(NotificationDeliveryError::ConflictingSource);
                        drop(state);
                        self.changed.notify_one();
                        return;
                    }
                    state.bound = Some(*subscription_id);
                } else if state.bound != Some(*subscription_id) {
                    // A different subscription for this attribute is not this stream.
                    return;
                }
                if matches!(notification, blehub::NotificationState::Disabled) {
                    state.end(NotificationEndReason::Disabled);
                }
            }
            BleEvent::NotificationReceived {
                connection_id,
                gatt_session_id,
                subscription_id,
                characteristic_id,
                value,
            } => {
                if state.bound != Some(*subscription_id)
                    || *connection_id != self.source.connection
                    || *gatt_session_id != self.source.session
                    || *characteristic_id != self.source.characteristic
                {
                    return;
                }
                if state.values.len() >= MAX_NOTIFICATION_VALUES {
                    state.fail(NotificationDeliveryError::ValueOverflow);
                    drop(state);
                    self.changed.notify_one();
                    return;
                }
                let bytes = state.bytes.saturating_add(value.len() as u64);
                if bytes > MAX_NOTIFICATION_BYTES {
                    state.fail(NotificationDeliveryError::ByteOverflow);
                    drop(state);
                    self.changed.notify_one();
                    return;
                }
                state.bytes = bytes;
                state.values.push_back(value.clone());
            }
            BleEvent::ConnectionStateChanged {
                connection_id,
                result,
                ..
            } if *connection_id == self.source.connection
                && matches!(
                    result,
                    ConnectionStateResult::Err { .. }
                        | ConnectionStateResult::Ok {
                            state: ConnectionState::Disconnected
                        }
                ) =>
            {
                state.end(NotificationEndReason::ConnectionClosed);
            }
            _ => return,
        }
        drop(state);
        self.changed.notify_one();
    }

    /// The owner retired: this subscription cannot outlive it.
    pub(crate) fn close(&self) {
        self.lock().fail(NotificationDeliveryError::OwnerRetired);
        self.changed.notify_one();
    }

    pub(crate) fn fail_unavailable(&self) {
        self.lock().fail(NotificationDeliveryError::Unavailable);
        self.changed.notify_one();
    }

    /// Ends observation only. The native subscription and the original disable remain independent.
    pub fn detach(&self) {
        let mut state = self.lock();
        state.detached = true;
        state.fail(NotificationDeliveryError::Detached);
        drop(state);
        self.changed.notify_one();
    }

    /// Cancel-safe pending pull. Dropping this future never cancels the subscription.
    pub async fn next_event(&self) -> Result<NotificationEvent, NotificationDeliveryError> {
        let Some(_pull) = PullGuard::acquire(&self.pulling) else {
            return Err(NotificationDeliveryError::Unavailable);
        };
        loop {
            let changed = self.changed.notified();
            {
                let mut state = self.lock();
                if let Some(value) = state.values.pop_front() {
                    state.bytes -= value.len() as u64;
                    return Ok(NotificationEvent::Value { value });
                }
                if let Some(terminal) = state.terminal {
                    return terminal.map(NotificationEvent::Ended);
                }
            }
            changed.await;
        }
    }

    pub fn snapshot(&self) -> NotificationDeliverySnapshot {
        let state = self.lock();
        NotificationDeliverySnapshot {
            queued_values: state.values.len() as u64,
            queued_bytes: state.bytes,
            bound: state.bound.is_some(),
            pull_pending: self.pulling.load(Ordering::Acquire),
        }
    }
}
