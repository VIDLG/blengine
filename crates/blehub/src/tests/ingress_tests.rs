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

fn active_state(sink: Arc<dyn BleEventSink>) -> Arc<EventIngressState> {
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
    let state = Arc::new(EventIngressState::new(Arc::new(AcceptingSink)));
    let handle = BackendEventIngressHandle::new(Arc::downgrade(&state));

    assert_eq!(handle.push(scan_started()), EventIngressStatus::Closed);
    assert!(state.activate().is_err());
}

#[test]
fn activation_reports_non_binding_phase() {
    let state = Arc::new(EventIngressState::new(Arc::new(AcceptingSink)));

    state.activate().unwrap();
    assert!(state.activate().is_err());

    state.close();
    assert!(state.activate().is_err());
}

#[test]
fn handle_weak_reference_does_not_retain_ingress_state() {
    let state = Arc::new(EventIngressState::new(Arc::new(AcceptingSink)));
    let handle = BackendEventIngressHandle::new(Arc::downgrade(&state));

    assert_eq!(Arc::strong_count(&state), 1);
    drop(state);

    assert_eq!(handle.push(scan_started()), EventIngressStatus::Closed);
}

#[test]
fn downstream_panic_is_contained_without_poisoning_ingress() {
    let state = active_state(Arc::new(PanicOnceSink {
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
    let state = active_state(Arc::new(BlockingSink {
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
        let state = active_state(Arc::new(AcceptingSink));
        let handle = BackendEventIngressHandle::new(Arc::downgrade(&state));
        assert_eq!(handle.push(scan_started()), EventIngressStatus::Delivered);
        drop(state);
        assert_eq!(handle.push(scan_started()), EventIngressStatus::Closed);
    }
}
