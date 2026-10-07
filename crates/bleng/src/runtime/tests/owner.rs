use crate::{
    runtime::{EventReadError, RuntimeOwner},
    waiter::WaiterError,
};
use blehub::{
    AdapterSnapshot, AdapterState, AuthorizationState, BleEvent, CommandId, EventIngressStatus,
    InitializeCommand, ScanOptions, ScanSessionId, StartScanCommand,
};
use blehub_testkit::{EventScenario, FakeBackend, RecordedCommand};
use std::{sync::Arc, time::Duration};

fn snapshot() -> AdapterSnapshot {
    AdapterSnapshot {
        state: AdapterState::PoweredOn,
        scan_authorization: AuthorizationState::Authorized,
        connection_authorization: AuthorizationState::Authorized,
        name: None,
    }
}

#[test]
fn shared_owner_assembles_and_runs_real_initialization_and_scan() {
    let (backend, handle) = FakeBackend::scripted([
        EventScenario::emit_before_return(
            RecordedCommand::Initialize(InitializeCommand {
                command_id: CommandId { value: 1 },
            }),
            vec![
                BleEvent::AdapterSnapshotChanged {
                    command_id: Some(CommandId { value: 1 }),
                    snapshot: snapshot(),
                },
                BleEvent::CommandCompleted {
                    command_id: CommandId { value: 1 },
                    error: None,
                },
            ],
        ),
        EventScenario::emit_before_return(
            RecordedCommand::StartScan(StartScanCommand {
                command_id: CommandId { value: 2 },
                scan_session_id: ScanSessionId { value: 3 },
                options: ScanOptions::default(),
            }),
            vec![
                BleEvent::ScanStarted {
                    command_id: CommandId { value: 2 },
                    scan_id: ScanSessionId { value: 3 },
                },
                BleEvent::CommandCompleted {
                    command_id: CommandId { value: 2 },
                    error: None,
                },
            ],
        ),
    ]);
    let owner = Arc::new(RuntimeOwner::new().unwrap());
    let host = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    host.block_on(async {
        owner.assemble_backend(Arc::new(backend)).await.unwrap();
        let late = handle.ingress_for_callback().unwrap();
        assert_eq!(
            owner.initialize(Duration::from_secs(2)).await,
            Ok(snapshot())
        );
        assert_eq!(
            owner
                .start_scan(ScanOptions::default(), Duration::from_secs(2))
                .await,
            Ok(ScanSessionId { value: 3 })
        );
        assert_eq!(owner.close().await, Ok(()));
        assert_eq!(owner.close().await, Ok(()));
        assert_eq!(
            late.push(BleEvent::ScanStarted {
                command_id: CommandId { value: 2 },
                scan_id: ScanSessionId { value: 3 },
            }),
            EventIngressStatus::Closed
        );
    });
    assert_eq!(handle.dispose_count(), 1);
}

#[test]
fn bounded_event_reader_times_out_without_faulting_the_owner() {
    let (backend, _handle) = FakeBackend::scripted([]);
    let owner = Arc::new(RuntimeOwner::new().unwrap());
    let host = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    host.block_on(async {
        owner.assemble_backend(Arc::new(backend)).await.unwrap();
        assert_eq!(
            owner.next_event(Duration::from_millis(5)).await,
            Err(EventReadError::Timeout)
        );
        assert_eq!(
            owner.next_event(Duration::from_millis(5)).await,
            Err(EventReadError::Timeout)
        );
        owner.close().await.unwrap();
    });
}

#[test]
fn bounded_event_reader_reports_close_as_end_of_stream() {
    let (backend, _handle) = FakeBackend::scripted([]);
    let owner = Arc::new(RuntimeOwner::new().unwrap());
    let host = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    host.block_on(async {
        owner.assemble_backend(Arc::new(backend)).await.unwrap();
        owner.close().await.unwrap();
        assert_eq!(owner.next_event(Duration::from_millis(5)).await, Ok(None));
    });
}

#[test]
fn concurrent_close_retires_pending_operation_and_blocked_event_reader() {
    let (backend, handle) = FakeBackend::scripted([EventScenario::emit_before_return(
        RecordedCommand::Initialize(InitializeCommand {
            command_id: CommandId { value: 1 },
        }),
        vec![],
    )]);
    let owner = Arc::new(RuntimeOwner::new().unwrap());
    let host = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    host.block_on(async {
        owner.assemble_backend(Arc::new(backend)).await.unwrap();
        let initializing = {
            let owner = Arc::clone(&owner);
            tokio::spawn(async move { owner.initialize(Duration::from_secs(2)).await })
        };
        let reading = {
            let owner = Arc::clone(&owner);
            tokio::spawn(async move { owner.next_event(Duration::from_secs(2)).await })
        };
        tokio::time::timeout(Duration::from_secs(1), async {
            while handle.commands().is_empty() {
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap();
        assert_eq!(
            tokio::time::timeout(Duration::from_secs(1), async {
                tokio::join!(owner.close(), owner.close())
            })
            .await,
            Ok((Ok(()), Ok(())))
        );
        assert_eq!(initializing.await.unwrap(), Err(WaiterError::Closed));
        assert_eq!(reading.await.unwrap(), Ok(None));
        assert_eq!(
            owner.initialize(Duration::from_millis(20)).await,
            Err(WaiterError::Closed)
        );
    });
    assert_eq!(handle.dispose_count(), 1);
}
