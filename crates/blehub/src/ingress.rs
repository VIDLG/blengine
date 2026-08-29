use std::{
    panic::{AssertUnwindSafe, catch_unwind},
    sync::{Mutex, Weak},
};

use crate::{BleEvent, EventSinkError};

#[derive(Clone, Debug, PartialEq, Eq, uniffi::Enum)]
pub enum EventIngressStatus {
    Delivered,
    Closed,
    InvalidEvent { message: String },
    DownstreamFailed { message: String },
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

#[derive(Clone, Copy, Debug, PartialEq, Eq, thiserror::Error)]
pub(crate) enum EventIngressActivationError {
    #[error("backend attempted to push an event while binding ingress")]
    PushAttemptedDuringBind,
    #[error("event ingress is already active")]
    AlreadyActive,
    #[error("event ingress is closed")]
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
    pub(crate) fn activate(&self) -> Result<(), EventIngressActivationError> {
        let mut inner = self
            .inner
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        match inner.phase {
            EventIngressPhase::Binding {
                push_attempted: false,
            } => {
                inner.phase = EventIngressPhase::Active;
                Ok(())
            }
            EventIngressPhase::Binding {
                push_attempted: true,
            } => Err(EventIngressActivationError::PushAttemptedDuringBind),
            EventIngressPhase::Active => Err(EventIngressActivationError::AlreadyActive),
            EventIngressPhase::Closed => Err(EventIngressActivationError::Closed),
        }
    }

    fn push(&self, event: BleEvent) -> EventIngressStatus {
        let mut inner = match self.inner.lock() {
            Ok(inner) => inner,
            Err(error) => {
                return EventIngressStatus::DownstreamFailed {
                    message: error.to_string(),
                };
            }
        };

        match &mut inner.phase {
            EventIngressPhase::Binding { push_attempted } => {
                *push_attempted = true;
                return EventIngressStatus::Closed;
            }
            EventIngressPhase::Closed => return EventIngressStatus::Closed,
            EventIngressPhase::Active => {}
        }

        let Some(downstream) = inner.downstream.as_deref() else {
            return EventIngressStatus::Closed;
        };

        if let Err(error) = event.validate_shape() {
            return EventIngressStatus::InvalidEvent {
                message: error.to_string(),
            };
        }

        match catch_unwind(AssertUnwindSafe(|| downstream.on_event(event))) {
            Ok(Ok(())) => EventIngressStatus::Delivered,
            Ok(Err(error)) => EventIngressStatus::DownstreamFailed {
                message: error.to_string(),
            },
            Err(_) => EventIngressStatus::DownstreamFailed {
                message: "downstream panicked while handling an event".to_owned(),
            },
        }
    }

    /// Closes the ingress, waits for any admitted delivery to return, and
    /// releases the downstream sink before returning.
    pub(crate) fn close(&self) {
        let mut inner = self
            .inner
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
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

    use crate::{BleEventPayload, CommandId, ScanSessionId};

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
        BleEvent {
            payload: BleEventPayload::ScanStarted {
                command_id: CommandId::new(1),
                scan_id: ScanSessionId::new(2),
            },
        }
    }

    #[test]
    fn push_during_binding_is_closed_and_prevents_activation() {
        let state = Arc::new(EventIngressState::new(Box::new(AcceptingSink)));
        let handle = BackendEventIngressHandle::new(Arc::downgrade(&state));

        assert_eq!(handle.push(scan_started()), EventIngressStatus::Closed);
        assert_eq!(
            state.activate(),
            Err(EventIngressActivationError::PushAttemptedDuringBind)
        );
    }

    #[test]
    fn activation_reports_non_binding_phase() {
        let state = Arc::new(EventIngressState::new(Box::new(AcceptingSink)));

        state.activate().unwrap();
        assert_eq!(
            state.activate(),
            Err(EventIngressActivationError::AlreadyActive)
        );

        state.close();
        assert_eq!(state.activate(), Err(EventIngressActivationError::Closed));
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
    fn invalid_event_is_rejected_before_downstream_delivery() {
        let state = active_state(Box::new(AcceptingSink));
        let handle = BackendEventIngressHandle::new(Arc::downgrade(&state));
        let invalid = BleEvent {
            payload: BleEventPayload::ScanStarted {
                command_id: CommandId::new(0),
                scan_id: ScanSessionId::new(2),
            },
        };

        assert!(matches!(
            handle.push(invalid),
            EventIngressStatus::InvalidEvent { .. }
        ));
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
