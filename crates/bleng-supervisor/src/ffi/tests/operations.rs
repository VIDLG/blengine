use std::{
    panic::{AssertUnwindSafe, catch_unwind},
    sync::{Arc, Mutex, mpsc},
    time::Duration,
};

use blehub::ScanOptions;

use super::{ForeignOwnerContext, Supervisor, SupervisorOperationError, SupervisorState};

fn context() -> ForeignOwnerContext {
    ForeignOwnerContext {
        owner_id: "bounded-operation-owner".to_owned(),
        backend_id: "bounded-operation-backend".to_owned(),
        owner_generation: 1,
        recovery_attempt: 1,
    }
}

#[test]
fn submission_rejects_mutex_contention_without_waiting_for_its_holder() {
    let supervisor = Arc::new(Supervisor {
        inner: Mutex::new(SupervisorState::Busy),
        close_view: Mutex::new(None),
    });
    let guard = supervisor.inner.lock().unwrap();
    let (tx, rx) = mpsc::channel();
    let caller = Arc::clone(&supervisor);
    let worker = std::thread::spawn(move || {
        tx.send(caller.begin_start_scan(context(), ScanOptions::default()))
            .unwrap();
    });
    let result = rx.recv_timeout(Duration::from_secs(1));
    // Always release and join before asserting; a regression to lock() must
    // fail this test, not leave a blocked worker or hang the test process.
    drop(guard);
    worker.join().unwrap();
    assert!(matches!(
        result,
        Ok(Err(SupervisorOperationError::SupervisorBusy))
    ));
}

#[test]
fn submission_distinguishes_a_taken_lifecycle_from_a_poisoned_supervisor() {
    let supervisor = Supervisor {
        inner: Mutex::new(SupervisorState::Busy),
        close_view: Mutex::new(None),
    };
    assert!(matches!(
        supervisor.begin_start_scan(context(), ScanOptions::default()),
        Err(SupervisorOperationError::SupervisorBusy)
    ));
    assert!(
        catch_unwind(AssertUnwindSafe(|| {
            let _guard = supervisor.inner.lock().unwrap();
            panic!("poison only this test's supervisor");
        }))
        .is_err()
    );
    assert!(matches!(
        supervisor.begin_start_scan(context(), ScanOptions::default()),
        Err(SupervisorOperationError::SupervisorUnavailable)
    ));
}
