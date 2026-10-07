use crate::error::{
    BlengBusyReason, BlengContractFailure, BlengError, BlengNotReadyReason, BlengResource,
};
use crate::{cleanup, runtime, waiter};
use blehub::{
    BleErrorCategory, BleEventError, CommandDispatchError, Platform, PlatformErrorDetails,
};

fn event_error(category: BleErrorCategory) -> BleEventError {
    BleEventError {
        category,
        message: "test operation failure".to_owned(),
        platform_details: None,
    }
}

#[test]
fn timeout_boundary_rejects_only_zero() {
    assert!(matches!(
        crate::api::validate_timeout(0),
        Err(BlengError::InvalidTimeout)
    ));
    assert!(crate::api::validate_timeout(1).is_ok());
    assert!(crate::api::validate_timeout(86_400_000).is_ok());
    assert!(matches!(
        crate::api::validate_timeout(86_400_001),
        Err(BlengError::InvalidTimeout)
    ));
}

#[test]
fn private_runtime_timers_work_with_timerless_caller_runtimes() {
    let callers = [
        tokio::runtime::Builder::new_current_thread()
            .build()
            .unwrap(),
        tokio::runtime::Builder::new_multi_thread()
            .worker_threads(1)
            .build()
            .unwrap(),
    ];
    for caller in callers {
        caller.block_on(async {
            // Neither caller enables timers. next_event must create and drive
            // its timeout on BLeng's private runtime, not the caller's executor.
            let bleng = super::Bleng::new().unwrap();
            assert!(matches!(
                bleng.next_event(1).await,
                Err(BlengError::Timeout)
            ));
            bleng.shutdown().await.unwrap();
            drop(bleng);

            // Dropping the private runtime from an async caller must not panic
            // or stop the caller's runtime.
            assert_eq!(tokio::spawn(async { 42 }).await.unwrap(), 42);
        });
    }
}

#[test]
fn dropping_facade_waiter_aborts_private_task_and_releases_its_capture() {
    use std::{future::pending, time::Duration};
    use tokio::sync::oneshot;

    let caller = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .unwrap();
    caller.block_on(async {
        let bleng = super::Bleng::new().unwrap();
        let (started_tx, started_rx) = oneshot::channel();
        let (released_tx, released_rx) = oneshot::channel();
        let mut operation = Box::pin(bleng.spawn_on_runtime(async move {
            let _release = scopeguard::guard(released_tx, |signal| {
                let _ = signal.send(());
            });
            started_tx.send(()).unwrap();
            pending::<()>().await;
        }));
        tokio::time::timeout(Duration::from_secs(2), async {
            tokio::select! {
                started = started_rx => started.expect("private task must start"),
                outcome = &mut operation => panic!("operation completed before cancellation: {outcome:?}"),
            }
        })
        .await
        .expect("private task must enter before dropping its facade waiter");

        drop(operation);
        tokio::time::timeout(Duration::from_secs(2), released_rx)
            .await
            .expect("facade cancellation must release the private task's capture")
            .unwrap();
        bleng.shutdown().await.unwrap();
    });
}

#[test]
fn event_reader_errors_preserve_timeout_and_transport_categories() {
    assert!(matches!(
        BlengError::from(runtime::EventReadError::Timeout),
        BlengError::Timeout
    ));
    assert!(matches!(
        BlengError::from(runtime::EventReadError::Fault),
        BlengError::TransportFault
    ));
}

#[test]
fn waiter_errors_preserve_canonical_and_local_categories() {
    assert!(matches!(
        BlengError::from(waiter::WaiterError::Operation(event_error(
            BleErrorCategory::NotSupported
        ))),
        BlengError::Operation { error } if error.category == BleErrorCategory::NotSupported
    ));
    assert!(matches!(
        BlengError::from(waiter::WaiterError::DispatchRejected(
            CommandDispatchError::InvalidState {
                detail: "not initialized".to_owned(),
            }
        )),
        BlengError::DispatchRejected {
            error: CommandDispatchError::InvalidState { .. }
        }
    ));
    assert!(matches!(
        BlengError::from(waiter::WaiterError::BackendNotAssembled),
        BlengError::NotReady {
            reason: BlengNotReadyReason::BackendNotAssembled
        }
    ));
    assert!(matches!(
        BlengError::from(waiter::WaiterError::NotInitialized),
        BlengError::NotReady {
            reason: BlengNotReadyReason::NotInitialized
        }
    ));
    assert!(matches!(
        BlengError::from(waiter::WaiterError::ConnectionNotActive),
        BlengError::NotReady {
            reason: BlengNotReadyReason::ConnectionNotActive
        }
    ));
    assert!(matches!(
        BlengError::from(waiter::WaiterError::SubscriptionNotActive),
        BlengError::NotReady {
            reason: BlengNotReadyReason::SubscriptionNotActive
        }
    ));
    assert!(matches!(
        BlengError::from(waiter::WaiterError::ScanBusy),
        BlengError::Busy {
            reason: BlengBusyReason::Scan
        }
    ));
    assert!(matches!(
        BlengError::from(waiter::WaiterError::CapacityExceeded),
        BlengError::ResourceExhausted {
            resource: BlengResource::PendingWaiters
        }
    ));
    assert!(matches!(
        BlengError::from(waiter::WaiterError::InvalidCommandId),
        BlengError::ContractFailure {
            reason: BlengContractFailure::InvalidCommandId
        }
    ));
}

#[test]
fn assembly_and_cleanup_errors_preserve_canonical_errors() {
    let canonical = blehub::BleHubError::BackendCleanup {
        detail: "dispose failed".to_owned(),
        platform_details: Some(PlatformErrorDetails {
            platform: Platform::Test,
            code: Some("E_TEST".to_owned()),
            name: None,
            message: Some("test cleanup".to_owned()),
        }),
    };
    assert!(matches!(
        BlengError::from(runtime::AssemblyError::Backend(canonical.clone())),
        BlengError::Assembly { error } if error == canonical
    ));
    assert!(matches!(
        BlengError::from(cleanup::CleanupError::Backend(canonical.clone())),
        BlengError::Cleanup { error } if error == canonical
    ));
    assert!(matches!(
        BlengError::from(runtime::AssemblyError::Admission(
            cleanup::AssemblyAdmissionError::AdditionalHubRejected
        )),
        BlengError::Busy {
            reason: BlengBusyReason::BackendSlotOccupied
        }
    ));
    assert!(matches!(
        BlengError::from(runtime::AssemblyError::Admission(
            cleanup::AssemblyAdmissionError::DuplicateOwnedHub
        )),
        BlengError::ContractFailure {
            reason: BlengContractFailure::DuplicateHubOwnership
        }
    ));
}
