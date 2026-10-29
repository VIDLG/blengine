use std::{
    sync::{
        Arc, Barrier, Mutex,
        atomic::{AtomicBool, AtomicUsize, Ordering},
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

struct GatedConcurrentSink {
    active: AtomicUsize,
    maximum_active: AtomicUsize,
    entered: mpsc::Sender<usize>,
    releases: Vec<Mutex<mpsc::Receiver<()>>>,
}

impl BleEventSink for GatedConcurrentSink {
    fn on_event(&self, event: BleEvent) -> Result<(), EventSinkError> {
        let BleEvent::CommandCompleted { command_id, .. } = event else {
            panic!("unexpected concurrent ingress event: {event:?}");
        };
        let active = self.active.fetch_add(1, Ordering::SeqCst) + 1;
        self.maximum_active.fetch_max(active, Ordering::SeqCst);
        self.entered.send(command_id.value as usize).unwrap();
        // Each callback has its own gate; the fixture must not serialize sink calls itself.
        self.releases[command_id.value as usize]
            .lock()
            .unwrap()
            .recv_timeout(Duration::from_secs(5))
            .unwrap();
        self.active.fetch_sub(1, Ordering::SeqCst);
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
fn concurrent_handle_pushes_never_overlap_sink_calls() {
    const PUSHES: usize = 5;
    let (entered_tx, entered_rx) = mpsc::channel();
    let (started_tx, started_rx) = mpsc::sync_channel(0);
    let (done_tx, done_rx) = mpsc::channel();
    let (release_txs, release_rxs): (Vec<_>, Vec<_>) = (0..PUSHES).map(|_| mpsc::channel()).unzip();
    let sink = Arc::new(GatedConcurrentSink {
        active: AtomicUsize::new(0),
        maximum_active: AtomicUsize::new(0),
        entered: entered_tx,
        releases: release_rxs.into_iter().map(Mutex::new).collect(),
    });
    let state = active_state(sink.clone());
    let handle = Arc::new(BackendEventIngressHandle::new(Arc::downgrade(&state)));
    let mut pushes = Vec::new();
    for id in 0..PUSHES {
        let handle = Arc::clone(&handle);
        let started_tx = started_tx.clone();
        let done_tx = done_tx.clone();
        pushes.push(thread::spawn(move || {
            started_tx.send(id).unwrap();
            let status = handle.push(BleEvent::CommandCompleted {
                command_id: CommandId { value: id as u64 },
                error: None,
            });
            done_tx.send((id, status)).unwrap();
        }));
        assert_eq!(started_rx.recv_timeout(Duration::from_secs(5)).unwrap(), id);
        if id == 0 {
            assert_eq!(entered_rx.recv_timeout(Duration::from_secs(5)).unwrap(), 0);
        }
    }

    // All competing callers have rendezvoused while the first callback holds its gate.
    assert_eq!(
        entered_rx.recv_timeout(Duration::from_millis(100)),
        Err(mpsc::RecvTimeoutError::Timeout)
    );
    assert_eq!(done_rx.try_recv(), Err(mpsc::TryRecvError::Empty));
    assert_eq!(sink.active.load(Ordering::SeqCst), 1);
    release_txs[0].send(()).unwrap();
    let mut entered_ids = vec![0];
    for _ in 1..PUSHES {
        let id = entered_rx.recv_timeout(Duration::from_secs(5)).unwrap();
        entered_ids.push(id);
        release_txs[id].send(()).unwrap();
    }
    let mut completed_ids = Vec::new();
    for _ in 0..PUSHES {
        let (id, status) = done_rx.recv_timeout(Duration::from_secs(5)).unwrap();
        assert_eq!(status, EventIngressStatus::Delivered);
        completed_ids.push(id);
    }
    for push in pushes {
        push.join().unwrap();
    }
    entered_ids.sort_unstable();
    completed_ids.sort_unstable();
    assert_eq!(entered_ids, (0..PUSHES).collect::<Vec<_>>());
    assert_eq!(completed_ids, entered_ids);
    assert_eq!(sink.maximum_active.load(Ordering::SeqCst), 1);
    assert_eq!(sink.active.load(Ordering::SeqCst), 0);
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
