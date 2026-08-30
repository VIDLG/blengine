use std::{
    panic::{AssertUnwindSafe, catch_unwind},
    sync::{Mutex, Weak},
};

use crate::BleEvent;

#[derive(Clone, Debug, PartialEq, Eq, uniffi::Enum)]
pub enum EventIngressStatus {
    Delivered,
    Closed,
    InvalidEvent { message: String },
    DownstreamFailed { message: String },
}

#[derive(Clone, Debug, PartialEq, Eq, thiserror::Error)]
#[error("downstream rejected the event: {message}")]
pub struct EventSinkError {
    pub message: String,
}

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
    downstream: Option<Box<dyn BleEventSink>>,
}

pub(crate) struct EventIngressState {
    inner: Mutex<EventIngressInner>,
}

impl EventIngressState {
    pub(crate) fn new(downstream: Box<dyn BleEventSink>) -> Self {
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
                    detail = %error,
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
                    detail = %message,
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
                detail = %message,
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
                tracing::warn!(
                    target: "blehub::ingress",
                    diagnostic = "downstream_rejected",
                    detail = %message,
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
                    detail = %error,
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
mod tests {
    use std::{
        sync::{
            Arc, Barrier,
            atomic::{AtomicBool, Ordering},
            mpsc,
        },
        thread,
        time::Duration,
    };

    use crate::{CommandId, ScanSessionId};

    use super::*;

    struct AcceptingSink;

    impl BleEventSink for AcceptingSink {
        fn on_event(&self, _: BleEvent) -> Result<(), EventSinkError> {
            Ok(())
        }
    }

    struct PanicOnceSink {
        panicked: AtomicBool,
    }

    impl BleEventSink for PanicOnceSink {
        fn on_event(&self, _: BleEvent) -> Result<(), EventSinkError> {
            if !self.panicked.swap(true, Ordering::AcqRel) {
                panic!("intentional downstream panic");
            }
            Ok(())
        }
    }

    struct BlockingSink {
        entered: Arc<Barrier>,
        release: Arc<Barrier>,
    }

    impl BleEventSink for BlockingSink {
        fn on_event(&self, _: BleEvent) -> Result<(), EventSinkError> {
            self.entered.wait();
            self.release.wait();
            Ok(())
        }
    }

    fn active_state(sink: Box<dyn BleEventSink>) -> Arc<EventIngressState> {
        let state = Arc::new(EventIngressState::new(sink));
        state.activate().unwrap();
        state
    }

    fn scan_started() -> BleEvent {
        BleEvent::ScanStarted {
            command_id: CommandId { value: 1 },
            scan_id: ScanSessionId { value: 2 },
        }
    }

    #[test]
    fn push_during_binding_is_closed_and_prevents_activation() {
        let state = Arc::new(EventIngressState::new(Box::new(AcceptingSink)));
        let handle = BackendEventIngressHandle::new(Arc::downgrade(&state));

        assert_eq!(handle.push(scan_started()), EventIngressStatus::Closed);
        assert!(state.activate().is_err());
    }

    #[test]
    fn activation_reports_non_binding_phase() {
        let state = Arc::new(EventIngressState::new(Box::new(AcceptingSink)));

        state.activate().unwrap();
        assert!(state.activate().is_err());

        state.close();
        assert!(state.activate().is_err());
    }

    #[test]
    fn handle_weak_reference_does_not_retain_ingress_state() {
        let state = Arc::new(EventIngressState::new(Box::new(AcceptingSink)));
        let handle = BackendEventIngressHandle::new(Arc::downgrade(&state));

        assert_eq!(Arc::strong_count(&state), 1);
        drop(state);

        assert_eq!(handle.push(scan_started()), EventIngressStatus::Closed);
    }

    #[test]
    fn downstream_panic_is_contained_without_poisoning_ingress() {
        let state = active_state(Box::new(PanicOnceSink {
            panicked: AtomicBool::new(false),
        }));
        let handle = BackendEventIngressHandle::new(Arc::downgrade(&state));

        assert_eq!(
            handle.push(scan_started()),
            EventIngressStatus::DownstreamFailed {
                message: "downstream panicked while handling an event".to_owned(),
            }
        );
        assert_eq!(handle.push(scan_started()), EventIngressStatus::Delivered);
        state.close();
    }

    #[test]
    fn close_waits_for_in_flight_delivery() {
        let entered = Arc::new(Barrier::new(2));
        let release = Arc::new(Barrier::new(2));
        let state = active_state(Box::new(BlockingSink {
            entered: Arc::clone(&entered),
            release: Arc::clone(&release),
        }));
        let handle = Arc::new(BackendEventIngressHandle::new(Arc::downgrade(&state)));

        let push_handle = Arc::clone(&handle);
        let push = thread::spawn(move || push_handle.push(scan_started()));
        entered.wait();

        let close_state = Arc::clone(&state);
        let (closed_tx, closed_rx) = mpsc::channel();
        let close = thread::spawn(move || {
            close_state.close();
            closed_tx.send(()).unwrap();
        });
        assert!(closed_rx.recv_timeout(Duration::from_millis(20)).is_err());

        release.wait();
        assert_eq!(push.join().unwrap(), EventIngressStatus::Delivered);
        closed_rx.recv_timeout(Duration::from_secs(1)).unwrap();
        close.join().unwrap();
        assert_eq!(handle.push(scan_started()), EventIngressStatus::Closed);
    }

    #[test]
    fn ten_thousand_direct_handle_lifecycles_return_to_closed() {
        for _ in 0..10_000 {
            let state = active_state(Box::new(AcceptingSink));
            let handle = BackendEventIngressHandle::new(Arc::downgrade(&state));
            assert_eq!(handle.push(scan_started()), EventIngressStatus::Delivered);
            drop(state);
            assert_eq!(handle.push(scan_started()), EventIngressStatus::Closed);
        }
    }
}
