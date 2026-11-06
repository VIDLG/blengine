use std::{
    panic::{AssertUnwindSafe, catch_unwind},
    sync::{Arc, Mutex, mpsc},
    time::Duration,
};

use super::{Supervisor, SupervisorState};

fn runtime() -> tokio::runtime::Runtime {
    tokio::runtime::Builder::new_current_thread()
        .build()
        .unwrap()
}

#[test]
fn close_view_distinguishes_taken_inner_from_poison_without_caching_either() {
    let supervisor = Supervisor {
        inner: Mutex::new(SupervisorState::Busy),
        close_view: Mutex::new(None),
    };
    let busy = runtime().block_on(supervisor.shutdown_view());
    assert!(!busy.owner_closed);
    assert_eq!(busy.failure.unwrap().code, "supervisor_busy");
    assert!(supervisor.close_view.lock().unwrap().is_none());
    assert!(
        catch_unwind(AssertUnwindSafe(|| {
            let _guard = supervisor.inner.lock().unwrap();
            panic!("poison only this test's supervisor");
        }))
        .is_err()
    );
    let unavailable = runtime().block_on(supervisor.shutdown_view());
    assert!(!unavailable.owner_closed);
    let failure = unavailable.failure.unwrap();
    assert_eq!(failure.code, "supervisor_unavailable");
    assert_eq!(failure.message, "supervisor is unavailable");
    assert!(supervisor.close_view.lock().unwrap().is_none());
}

#[test]
fn close_view_reports_mutex_contention_without_waiting_for_its_holder() {
    let supervisor = Arc::new(Supervisor {
        inner: Mutex::new(SupervisorState::Busy),
        close_view: Mutex::new(None),
    });
    let guard = supervisor.inner.lock().unwrap();
    let (tx, rx) = mpsc::channel();
    let caller = Arc::clone(&supervisor);
    let worker = std::thread::spawn(move || {
        tx.send(runtime().block_on(caller.shutdown_view())).unwrap();
    });
    let result = rx.recv_timeout(Duration::from_secs(1));
    drop(guard);
    worker.join().unwrap();
    let view = result.expect("shutdown_view waited for the lifecycle mutex");
    assert!(!view.owner_closed);
    assert_eq!(view.failure.unwrap().code, "supervisor_busy");
    assert!(supervisor.close_view.lock().unwrap().is_none());
}
