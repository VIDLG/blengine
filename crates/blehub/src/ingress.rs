use std::{
    panic::{AssertUnwindSafe, catch_unwind},
    sync::{Arc, Mutex, Weak},
};

use crate::BleEvent;

#[derive(Clone, Debug, PartialEq, Eq, uniffi::Enum)]
pub enum EventIngressStatus {
    Delivered,
    Closed,
    InvalidEvent { message: String },
    DownstreamFailed { message: String },
}

#[derive(Clone, Debug, PartialEq, Eq, thiserror::Error, uniffi::Error)]
pub enum EventSinkError {
    #[error("downstream rejected the event: {detail}")]
    Rejected { detail: String },
    #[error("unexpected foreign event sink exception: {detail}")]
    UnexpectedCallback { detail: String },
}

impl From<uniffi::UnexpectedUniFFICallbackError> for EventSinkError {
    fn from(error: uniffi::UnexpectedUniFFICallbackError) -> Self {
        Self::UnexpectedCallback {
            detail: error.reason,
        }
    }
}

/// Synchronous event handoff into caller-owned execution.
///
/// Implementations must return promptly after accepting or rejecting the event,
/// must not block on BLE/backend progress, and must not synchronously call a
/// command or `dispose` on the same [`crate::BleHub`]. Such work must be queued
/// onto caller-owned execution and performed after `on_event` returns.
#[uniffi::export(foreign)]
pub trait BleEventSink: Send + Sync {
    fn on_event(&self, event: BleEvent) -> Result<(), EventSinkError>;
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum EventIngressPhase {
    Binding { push_attempted: bool },
    Active,
    Closed,
}

struct EventIngressInner {
    phase: EventIngressPhase,
    downstream: Option<Arc<dyn BleEventSink>>,
}

pub(crate) struct EventIngressState {
    inner: Mutex<EventIngressInner>,
}

impl EventIngressState {
    pub(crate) fn new(downstream: Arc<dyn BleEventSink>) -> Self {
        Self {
            inner: Mutex::new(EventIngressInner {
                phase: EventIngressPhase::Binding {
                    push_attempted: false,
                },
                downstream: Some(downstream),
            }),
        }
    }

    /// Opens delivery only after a successful, inert backend bind. A push
    /// attempted during bind is a contract violation and makes activation fail.
    /// The error text is diagnostic only; callers handle all failures uniformly.
    pub(crate) fn activate(&self) -> Result<(), &'static str> {
        let mut inner = match self.inner.lock() {
            Ok(inner) => inner,
            Err(error) => {
                tracing::error!(
                    target: "blehub::ingress",
                    diagnostic = "ingress_mutex_poisoned",
                    operation = "activate",
                    error_class = "mutex_poisoned",
                    "recovered poisoned event ingress mutex"
                );
                error.into_inner()
            }
        };
        match inner.phase {
            EventIngressPhase::Binding {
                push_attempted: false,
            } => {
                inner.phase = EventIngressPhase::Active;
                Ok(())
            }
            EventIngressPhase::Binding {
                push_attempted: true,
            } => Err("backend attempted to push an event while binding ingress"),
            EventIngressPhase::Active => {
                tracing::error!(
                    target: "blehub::ingress",
                    diagnostic = "ingress_activation_invalid_phase",
                    phase = "active",
                    "event ingress activation attempted from an invalid phase"
                );
                Err("event ingress is already active")
            }
            EventIngressPhase::Closed => {
                tracing::error!(
                    target: "blehub::ingress",
                    diagnostic = "ingress_activation_invalid_phase",
                    phase = "closed",
                    "event ingress activation attempted from an invalid phase"
                );
                Err("event ingress is closed")
            }
        }
    }

    fn push(&self, event: BleEvent) -> EventIngressStatus {
        let mut inner = match self.inner.lock() {
            Ok(inner) => inner,
            Err(error) => {
                let message = error.to_string();
                tracing::error!(
                    target: "blehub::ingress",
                    diagnostic = "ingress_mutex_poisoned",
                    operation = "push",
                    error_class = "mutex_poisoned",
                    "failed to acquire event ingress mutex"
                );
                return EventIngressStatus::DownstreamFailed { message };
            }
        };

        match &mut inner.phase {
            EventIngressPhase::Binding { push_attempted } => {
                *push_attempted = true;
                tracing::warn!(
                    target: "blehub::ingress",
                    diagnostic = "push_during_bind",
                    "backend attempted to push before event ingress activation"
                );
                return EventIngressStatus::Closed;
            }
            EventIngressPhase::Closed => return EventIngressStatus::Closed,
            EventIngressPhase::Active => {}
        }

        if let Err(message) = event.validate() {
            tracing::warn!(
                target: "blehub::ingress",
                diagnostic = "invalid_event",
                error_class = "wire_validation",
                "rejected backend event with an invalid wire invariant"
            );
            return EventIngressStatus::InvalidEvent { message };
        }

        let Some(downstream) = inner.downstream.as_deref() else {
            return EventIngressStatus::Closed;
        };

        match catch_unwind(AssertUnwindSafe(|| downstream.on_event(event))) {
            Ok(Ok(())) => EventIngressStatus::Delivered,
            Ok(Err(error)) => {
                let message = error.to_string();
                let diagnostic = match &error {
                    EventSinkError::UnexpectedCallback { .. } => "downstream_unexpected_callback",
                    EventSinkError::Rejected { .. } => "downstream_rejected",
                };
                tracing::warn!(
                    target: "blehub::ingress",
                    diagnostic,
                    error_class = match error {
                        EventSinkError::UnexpectedCallback { .. } => "unexpected_callback",
                        EventSinkError::Rejected { .. } => "rejected",
                    },
                    "downstream rejected an event delivery attempt"
                );
                EventIngressStatus::DownstreamFailed { message }
            }
            Err(_) => {
                let message = "downstream panicked while handling an event".to_owned();
                tracing::error!(
                    target: "blehub::ingress",
                    diagnostic = "downstream_panicked",
                    "contained downstream panic at the event ingress boundary"
                );
                EventIngressStatus::DownstreamFailed { message }
            }
        }
    }

    /// Closes the ingress, waits for any admitted delivery to return, and
    /// releases the downstream sink before returning.
    pub(crate) fn close(&self) {
        let mut inner = match self.inner.lock() {
            Ok(inner) => inner,
            Err(error) => {
                tracing::error!(
                    target: "blehub::ingress",
                    diagnostic = "ingress_mutex_poisoned",
                    operation = "close",
                    error_class = "mutex_poisoned",
                    "recovered poisoned event ingress mutex"
                );
                error.into_inner()
            }
        };
        inner.phase = EventIngressPhase::Closed;
        inner.downstream.take();
    }
}

/// Arc-backed at the UniFFI boundary, but does not keep BleHub's ingress state alive.
/// The backend owns the sole long-lived Arc root and must release it during dispose;
/// callback queues may hold temporary clones while work is pending or in flight.
#[derive(uniffi::Object)]
pub struct BackendEventIngressHandle {
    state: Weak<EventIngressState>,
}

impl BackendEventIngressHandle {
    pub(crate) fn new(state: Weak<EventIngressState>) -> Self {
        Self { state }
    }
}

#[uniffi::export]
impl BackendEventIngressHandle {
    pub fn push(&self, event: BleEvent) -> EventIngressStatus {
        self.state
            .upgrade()
            .map_or(EventIngressStatus::Closed, |state| state.push(event))
    }
}

#[cfg(test)]
#[path = "tests/ingress_tests.rs"]
mod tests;
