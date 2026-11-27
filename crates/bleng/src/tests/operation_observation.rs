use std::sync::{
    Arc, Mutex,
    atomic::{AtomicUsize, Ordering},
};

use crate::{OperationFailureObserver, operation_observation::OperationOutcomeObservation};

#[derive(Default)]
struct Receiver(AtomicUsize);

impl OperationFailureObserver for Receiver {
    fn operation_failed(&self) {
        self.0.fetch_add(1, Ordering::SeqCst);
    }
}

#[test]
fn single_registration_is_weak_revocable_and_replays_failure_outside_lock() {
    let observation = Arc::new(OperationOutcomeObservation::default());
    let receiver = Arc::new(Receiver::default());
    let sink: Arc<dyn OperationFailureObserver> = receiver.clone();
    let token = observation.register(Arc::downgrade(&sink)).unwrap();
    assert!(observation.register(Arc::downgrade(&sink)).is_none());
    drop(token);
    observation.record_completion(false, false);
    assert_eq!(receiver.0.load(Ordering::SeqCst), 0);
    let token = observation.register(Arc::downgrade(&sink)).unwrap();
    assert_eq!(receiver.0.load(Ordering::SeqCst), 1);
    observation.record_completion(false, false);
    assert_eq!(receiver.0.load(Ordering::SeqCst), 1);
    drop(sink);
    let weak = Arc::downgrade(&receiver);
    drop(receiver);
    assert!(
        weak.upgrade().is_none(),
        "registration must not own its receiver"
    );
    drop(token);

    struct Reentrant {
        observation: Arc<OperationOutcomeObservation>,
        calls: Mutex<usize>,
    }
    impl OperationFailureObserver for Reentrant {
        fn operation_failed(&self) {
            // Fail promptly instead of hanging if notification moves under the summary lock.
            drop(
                self.observation
                    .0
                    .try_lock()
                    .expect("notification must be outside the summary lock"),
            );
            self.observation.record_completion(true, false);
            *self.calls.lock().unwrap() += 1;
        }
    }
    let sink = Arc::new(Reentrant {
        observation: observation.clone(),
        calls: Mutex::new(0),
    });
    let erased: Arc<dyn OperationFailureObserver> = sink.clone();
    let _token = observation.register(Arc::downgrade(&erased)).unwrap();
    assert_eq!(*sink.calls.lock().unwrap(), 1);
}

#[test]
fn registration_racing_completion_notifies_once_outside_lock() {
    use std::{sync::Barrier, thread};

    struct LockCheckingReceiver {
        observation: Arc<OperationOutcomeObservation>,
        calls: AtomicUsize,
    }

    impl OperationFailureObserver for LockCheckingReceiver {
        fn operation_failed(&self) {
            drop(
                self.observation
                    .0
                    .try_lock()
                    .expect("notification must be outside the summary lock"),
            );
            self.calls.fetch_add(1, Ordering::SeqCst);
        }
    }

    for _ in 0..32 {
        let observation = Arc::new(OperationOutcomeObservation::default());
        let receiver = Arc::new(LockCheckingReceiver {
            observation: Arc::clone(&observation),
            calls: AtomicUsize::new(0),
        });
        let sink: Arc<dyn OperationFailureObserver> = receiver.clone();
        let barrier = Arc::new(Barrier::new(2));
        let writer = Arc::clone(&observation);
        let ready = Arc::clone(&barrier);
        let completion = thread::spawn(move || {
            ready.wait();
            writer.record_completion(false, false);
        });
        barrier.wait();
        let _token = observation.register(Arc::downgrade(&sink)).unwrap();
        completion.join().unwrap();
        assert_eq!(receiver.calls.load(Ordering::SeqCst), 1);
        observation.record_completion(false, false);
        assert_eq!(receiver.calls.load(Ordering::SeqCst), 1);
    }
}

#[test]
fn pending_native_summary_waits_for_control_and_success_is_immutable() {
    for native_success in [false, true] {
        for cancel_accepted in [false, true] {
            let observation = OperationOutcomeObservation::default();
            observation.record_completion(native_success, true);
            let receiver = Arc::new(Receiver::default());
            let sink: Arc<dyn OperationFailureObserver> = receiver.clone();
            let token = observation.register(Arc::downgrade(&sink)).unwrap();
            assert_eq!(receiver.0.load(Ordering::SeqCst), 0);
            observation.resolve_cancellation(cancel_accepted);
            let failed = cancel_accepted || !native_success;
            assert_eq!(receiver.0.load(Ordering::SeqCst), usize::from(failed));
            observation.record_completion(!native_success, false);
            observation.resolve_cancellation(true);
            assert_eq!(receiver.0.load(Ordering::SeqCst), usize::from(failed));
            drop(token);
            let _late = observation.register(Arc::downgrade(&sink)).unwrap();
            assert_eq!(receiver.0.load(Ordering::SeqCst), 2 * usize::from(failed));
        }
    }
}

#[test]
fn revocation_does_not_wait_for_an_admitted_callback_or_disable_its_replacement() {
    use std::{sync::mpsc, thread, time::Duration};

    struct PausedReceiver {
        entered: mpsc::SyncSender<()>,
        release: Mutex<mpsc::Receiver<()>>,
    }

    impl OperationFailureObserver for PausedReceiver {
        fn operation_failed(&self) {
            self.entered.try_send(()).unwrap();
            self.release
                .lock()
                .unwrap()
                .recv_timeout(Duration::from_secs(5))
                .expect("test did not release the admitted callback");
        }
    }

    let observation = Arc::new(OperationOutcomeObservation::default());
    let (entered_tx, entered_rx) = mpsc::sync_channel(1);
    let (release_tx, release_rx) = mpsc::sync_channel(1);
    let release = scopeguard::guard(release_tx, |sender| {
        let _ = sender.try_send(());
    });
    let receiver = Arc::new(PausedReceiver {
        entered: entered_tx,
        release: Mutex::new(release_rx),
    });
    let old_weak = Arc::downgrade(&receiver);
    let erased: Arc<dyn OperationFailureObserver> = receiver.clone();
    let token = observation.register(Arc::downgrade(&erased)).unwrap();
    let writer = Arc::clone(&observation);
    let notification = thread::spawn(move || writer.record_completion(false, false));
    entered_rx.recv_timeout(Duration::from_secs(5)).unwrap();

    // The old callback is already admitted. Revocation must not wait for it,
    // and the single listener slot must become available immediately.
    drop(token);
    drop(erased);
    drop(receiver);
    assert!(
        old_weak.upgrade().is_some(),
        "the admitted callback owns its receiver"
    );
    let replacement = Arc::new(Receiver::default());
    let erased: Arc<dyn OperationFailureObserver> = replacement.clone();
    let replacement_token = observation.register(Arc::downgrade(&erased)).unwrap();
    assert_eq!(replacement.0.load(Ordering::SeqCst), 1);
    assert!(observation.register(Arc::downgrade(&erased)).is_none());

    drop(release);
    notification.join().unwrap();
    assert!(
        old_weak.upgrade().is_none(),
        "notification must release its receiver"
    );
    assert!(
        observation.register(Arc::downgrade(&erased)).is_none(),
        "the old callback must not revoke its replacement"
    );
    observation.record_completion(false, false);
    assert_eq!(replacement.0.load(Ordering::SeqCst), 1);
    drop(replacement_token);
    let _late = observation.register(Arc::downgrade(&erased)).unwrap();
    assert_eq!(replacement.0.load(Ordering::SeqCst), 2);
}
