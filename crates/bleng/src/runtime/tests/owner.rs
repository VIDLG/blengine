use crate::{
    runtime::{BlengActor, EventReadError, RuntimeOwner, RuntimePhase, owner::BeginHandoff},
    waiter::{WaiterError, WaiterExpectation},
};
use blehub::{
    AdapterSnapshot, AdapterState, AuthorizationState, BleEvent, CommandId, EventIngressStatus,
    InitializeCommand, ScanOptions, ScanSessionId, StartScanCommand,
};
use blehub_testkit::{EventScenario, FakeBackend, RecordedCommand};
use kameo::prelude::{Context, Message};
use std::{sync::Arc, time::Duration};

fn snapshot() -> AdapterSnapshot {
    AdapterSnapshot {
        state: AdapterState::PoweredOn,
        scan_authorization: AuthorizationState::Authorized,
        connection_authorization: AuthorizationState::Authorized,
        name: None,
    }
}

struct BeginInitializeAfterCleanup {
    handoff: BeginHandoff,
    phase: RuntimePhase,
    expected_error: WaiterError,
}

impl Message<BeginInitializeAfterCleanup> for BlengActor {
    type Reply = Result<(), WaiterError>;

    async fn handle(
        &mut self,
        message: BeginInitializeAfterCleanup,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        self.require_open()?;
        self.initialization.require_not_started()?;
        let (command_id, mut result_rx) = self.register_waiter(WaiterExpectation::Initialized)?;
        match message.phase {
            RuntimePhase::Open => self.lifecycle.start_cleanup(),
            RuntimePhase::Closed => {
                self.lifecycle.close();
                self.lifecycle.start_cleanup();
            }
            RuntimePhase::Faulted => self.lifecycle.fault(),
        }
        let error = self
            .launch_dispatch(
                ctx,
                &message.handoff,
                command_id,
                |_| panic!("cleanup before launch must not reserve initialization"),
                |_| panic!("cleanup before launch must not dispatch a backend command"),
            )
            .expect_err("cleanup before launch must reject dispatch");
        assert_eq!(error, message.expected_error);
        assert_eq!(result_rx.try_recv(), Ok(Err(error.clone())));
        assert_eq!(self.initialization.require_not_started(), Ok(()));
        assert_eq!(self.lifecycle.phase(), message.phase);
        // A handler error leaves guard retirement to begin_operation, without acknowledging launch.
        Err(error)
    }
}

#[test]
fn cleanup_before_launch_finishes_registered_waiter_without_reserving_or_false_faulting() {
    for (phase, expected_error) in [
        (RuntimePhase::Open, WaiterError::BackendNotAssembled),
        (RuntimePhase::Closed, WaiterError::Closed),
        (RuntimePhase::Faulted, WaiterError::TransportFault),
    ] {
        let (backend, handle) = FakeBackend::scripted([]);
        let owner = RuntimeOwner::new().unwrap();
        let host = tokio::runtime::Builder::new_multi_thread()
            .enable_time()
            .build()
            .unwrap();
        host.block_on(async {
            let deadline = Duration::from_secs(2);
            tokio::time::timeout(deadline, owner.assemble_backend(Arc::new(backend)))
                .await
                .expect("backend assembly timed out")
                .unwrap();
            let result = tokio::time::timeout(
                deadline,
                owner.begin_operation(deadline, |actor, handoff| {
                    let message = BeginInitializeAfterCleanup {
                        handoff,
                        phase,
                        expected_error: expected_error.clone(),
                    };
                    async move { actor.ask(message).await }
                }),
            )
            .await
            .expect("cleanup-before-launch begin timed out");
            assert_eq!(result, Err(expected_error), "{phase:?}");
            assert_eq!(owner.lifecycle.phase(), phase, "guard must not false-fault");
            assert!(handle.commands().is_empty(), "{phase:?} must not dispatch");
            tokio::time::timeout(deadline, owner.close())
                .await
                .expect("owner close timed out")
                .unwrap();
        });
        assert!(handle.commands().is_empty(), "{phase:?} must not dispatch");
        assert_eq!(handle.dispose_count(), 1, "{phase:?}");
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
                scan_session_id: ScanSessionId { value: 1 },
                options: ScanOptions::default(),
            }),
            vec![
                BleEvent::ScanStarted {
                    command_id: CommandId { value: 2 },
                    scan_id: ScanSessionId { value: 1 },
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
            Ok(ScanSessionId { value: 1 })
        );
        assert_eq!(owner.close().await, Ok(()));
        assert_eq!(owner.close().await, Ok(()));
        assert_eq!(
            late.push(BleEvent::ScanStarted {
                command_id: CommandId { value: 2 },
                scan_id: ScanSessionId { value: 1 },
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
