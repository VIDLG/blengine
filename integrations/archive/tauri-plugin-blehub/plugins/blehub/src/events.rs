use std::sync::{
    Arc, Mutex,
    atomic::{AtomicBool, Ordering},
};

use blehub::{BleErrorCategory, BleEvent, BleEventError, BleEventSink, EventSinkError};
use futures_channel::mpsc::{Receiver, Sender};
use futures_util::StreamExt;
use tauri::async_runtime::JoinHandle;

use crate::projection::{ProjectedEvent, project_event};

pub(super) struct EventDelivery {
    closed: Mutex<bool>,
    overflow: AtomicBool,
    task: Mutex<Option<JoinHandle<()>>>,
}

struct ChannelSink {
    sender: Mutex<Sender<BleEvent>>,
    delivery: Arc<EventDelivery>,
}

impl BleEventSink for ChannelSink {
    fn on_event(&self, event: BleEvent) -> Result<(), EventSinkError> {
        let mut sender = self.sender.lock().map_err(|_| EventSinkError::Rejected {
            detail: "Tauri event channel mutex poisoned".to_owned(),
        })?;
        sender.try_send(event).map_err(|error| {
            if error.is_full() {
                self.delivery.overflow.store(true, Ordering::Release);
                // Closing the source wakes the task even if it drained the
                // queue just after try_send failed. Never silently lose a terminal.
                sender.close_channel();
            }
            EventSinkError::Rejected {
                detail: "Tauri event transport unavailable".to_owned(),
            }
        })
    }
}

pub(super) fn channel(
    capacity: usize,
) -> (
    Arc<EventDelivery>,
    Arc<dyn BleEventSink>,
    Receiver<BleEvent>,
) {
    // futures mpsc additionally reserves one slot for this single sender.
    let (sender, receiver) = futures_channel::mpsc::channel(capacity);
    let delivery = Arc::new(EventDelivery {
        closed: Mutex::new(false),
        overflow: AtomicBool::new(false),
        task: Mutex::new(None),
    });
    let sink = Arc::new(ChannelSink {
        sender: Mutex::new(sender),
        delivery: delivery.clone(),
    });
    (delivery, sink, receiver)
}

impl EventDelivery {
    pub(super) fn start(
        self: &Arc<Self>,
        mut receiver: Receiver<BleEvent>,
        emit: impl Fn(ProjectedEvent) -> bool + Send + 'static,
        on_failure: impl FnOnce() + Send + 'static,
    ) {
        let delivery = self.clone();
        let task = tauri::async_runtime::spawn(async move {
            while let Some(event) = receiver.next().await {
                if delivery.overflow.load(Ordering::Acquire) {
                    break;
                }
                if !delivery.emit(project_event(&event), &emit) {
                    break;
                }
            }
            if delivery.overflow.load(Ordering::Acquire) {
                let fault = BleEvent::BackendError {
                    error: BleEventError {
                        category: BleErrorCategory::Internal,
                        message: "Tauri event queue overflow; the Hub is being closed".to_owned(),
                        platform_details: None,
                    },
                };
                let _ = delivery.emit(project_event(&fault), &emit);
            }
            let was_open = !*delivery
                .closed
                .lock()
                .unwrap_or_else(|error| error.into_inner());
            let _ = delivery.stop();
            if was_open {
                on_failure();
            }
        });
        let closed = self
            .closed
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        if *closed {
            task.abort();
        } else {
            *self.task.lock().unwrap_or_else(|error| error.into_inner()) = Some(task);
        }
    }

    fn emit(&self, event: ProjectedEvent, emit: &impl Fn(ProjectedEvent) -> bool) -> bool {
        let closed = self
            .closed
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        if *closed {
            return false;
        }
        // Serialize the short Tauri emission boundary with shutdown. This lock
        // never crosses await or native cleanup. Rust event listeners must not
        // synchronously tear down this plugin from inside emit.
        emit(event)
    }

    pub(super) fn stop(&self) -> Option<JoinHandle<()>> {
        *self
            .closed
            .lock()
            .unwrap_or_else(|error| error.into_inner()) = true;
        let task = self
            .task
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .take();
        if let Some(task) = &task {
            task.abort();
        }
        task
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        sync::{Barrier, mpsc},
        time::Duration,
    };

    fn event(value: u64) -> BleEvent {
        BleEvent::CommandCompleted {
            command_id: blehub::CommandId { value },
            error: None,
        }
    }

    #[test]
    fn queue_overflow_faults_and_retires_instead_of_silently_losing_events() {
        let (delivery, sink, receiver) = channel(1);
        sink.on_event(event(1)).unwrap();
        sink.on_event(event(2)).unwrap();
        assert!(sink.on_event(event(3)).is_err());
        let (tx, rx) = mpsc::channel();
        let (closed_tx, closed_rx) = mpsc::channel();
        delivery.start(
            receiver,
            move |event| tx.send(event).is_ok(),
            move || {
                closed_tx.send(()).unwrap();
            },
        );
        let fault = rx.recv_timeout(Duration::from_secs(2)).unwrap();
        assert_eq!(
            serde_json::to_value(fault).unwrap()["kind"],
            "backend_error"
        );
        closed_rx.recv_timeout(Duration::from_secs(2)).unwrap();
        assert!(sink.on_event(event(4)).is_err());
    }

    #[test]
    fn stopping_idle_pump_releases_its_captured_owner() {
        struct Owner(mpsc::Sender<()>);
        impl Drop for Owner {
            fn drop(&mut self) {
                let _ = self.0.send(());
            }
        }
        let (delivery, _sink, receiver) = channel(1);
        let (tx, rx) = mpsc::channel();
        let owner = Owner(tx);
        delivery.start(
            receiver,
            move |_| {
                let _ = &owner;
                true
            },
            || {},
        );
        if let Some(task) = delivery.stop() {
            let _ = tauri::async_runtime::block_on(task);
        }
        rx.recv_timeout(Duration::from_secs(2)).unwrap();
    }

    #[test]
    fn close_waits_for_entered_emission_and_rejects_later_emission() {
        let (delivery, _sink, _receiver) = channel(1);
        let entered = Arc::new(Barrier::new(2));
        let release = Arc::new(Barrier::new(2));
        let thread_delivery = delivery.clone();
        let thread_entered = entered.clone();
        let thread_release = release.clone();
        let emitter = std::thread::spawn(move || {
            thread_delivery.emit(project_event(&event(1)), &|_| {
                thread_entered.wait();
                thread_release.wait();
                true
            })
        });
        entered.wait();
        let (tx, rx) = mpsc::channel();
        let closer_delivery = delivery.clone();
        let closer = std::thread::spawn(move || {
            closer_delivery.stop();
            tx.send(()).unwrap();
        });
        assert!(rx.recv_timeout(Duration::from_millis(30)).is_err());
        release.wait();
        assert!(emitter.join().unwrap());
        closer.join().unwrap();
        assert!(!delivery.emit(project_event(&event(2)), &|_| panic!("emitted after close")));
    }
}
