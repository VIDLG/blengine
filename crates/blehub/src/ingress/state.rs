use std::sync::{
    Mutex,
    atomic::{AtomicBool, Ordering},
};

use crate::{BleEvent, BleEventSink, EventIngressStatus};

pub(crate) struct EventIngressState {
    closed: AtomicBool,
    downstream: Mutex<Option<Box<dyn BleEventSink>>>,
}

impl EventIngressState {
    pub(crate) fn new(downstream: Box<dyn BleEventSink>) -> Self {
        Self {
            closed: AtomicBool::new(false),
            downstream: Mutex::new(Some(downstream)),
        }
    }

    pub(crate) fn push(&self, event: BleEvent) -> EventIngressStatus {
        if self.closed.load(Ordering::Acquire) {
            return EventIngressStatus::Closed;
        }

        let downstream = match self.downstream.lock() {
            Ok(downstream) => downstream,
            Err(error) => {
                return EventIngressStatus::DownstreamFailed {
                    message: error.to_string(),
                };
            }
        };

        if self.closed.load(Ordering::Acquire) {
            return EventIngressStatus::Closed;
        }

        let Some(downstream) = downstream.as_deref() else {
            return EventIngressStatus::Closed;
        };

        if let Err(error) = event.validate_shape() {
            return EventIngressStatus::InvalidEvent {
                message: error.to_string(),
            };
        }

        match downstream.on_event(event) {
            Ok(()) => EventIngressStatus::Delivered,
            Err(error) => EventIngressStatus::DownstreamFailed {
                message: error.to_string(),
            },
        }
    }

    pub(crate) fn close_and_drain(&self) {
        self.closed.store(true, Ordering::Release);
        let mut downstream = self
            .downstream
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        downstream.take();
    }
}
