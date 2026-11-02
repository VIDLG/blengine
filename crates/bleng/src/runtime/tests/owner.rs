use crate::{
    runtime::{
        BlengActor, EventReadError, RuntimeOwner, RuntimePhase,
        actor::BeginInitializeAfterCleanup,
        messages::{BeginConnect, OperationSpec},
        shared::OperationSubmission,
    },
    waiter::{WaiterError, WaiterSuccess},
};
use blehub::{
    AdapterSnapshot, AdapterState, AuthorizationState, BleEvent, CommandId, ConnectionId,
    EventIngressStatus, InitializeCommand, ScanOptions, ScanSessionId, StartScanCommand,
};
use blehub_testkit::{EventScenario, FakeBackend, RecordedCommand};

use kameo::prelude::{Context, Message};
use std::{sync::Arc, time::Duration};

// Bypass reservation reconciliation to exercise the owner's last result boundary.
struct BeginWithUnexpectedSuccess {
    submission: OperationSubmission<ConnectionId>,
    success: WaiterSuccess,
}

impl OperationSpec for BeginWithUnexpectedSuccess {
    type Resource = ConnectionId;
    type Output = ConnectionId;
    type Correlation = ();

    fn correlation(&self) -> Self::Correlation {}

    fn decode_success(
        correlation: (),
        resource: ConnectionId,
        success: WaiterSuccess,
    ) -> Option<ConnectionId> {
        BeginConnect::decode_success(correlation, resource, success)
    }
}

impl Message<BeginWithUnexpectedSuccess> for BlengActor {
    type Reply = ();

    async fn handle(
        &mut self,
        message: BeginWithUnexpectedSuccess,
        _: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        let submission = message.submission.clone();
        let result = async {
            assert!(message.submission.try_claim());
            message.submission.resource.set(1.into()).unwrap();
            message.submission.state.result_tx.send(Ok(message.success));
            Ok(1.into())
        }
        .await;
        submission.resolve(result);
    }
}

#[test]
fn unexpected_success_variant_or_resource_faults_owner_and_disposes_hub() {
    for success in [
        WaiterSuccess::ScanStarted(1.into()),
        WaiterSuccess::Connected(2.into()),
    ] {
        let owner = RuntimeOwner::new().unwrap();
        let (backend, handle) = FakeBackend::scripted([]);
        let host = tokio::runtime::Builder::new_multi_thread()
            .enable_time()
            .build()
            .unwrap();
        host.block_on(async {
            owner.assemble_backend(Arc::new(backend)).await.unwrap();
            let result = owner
                .run_operation(Duration::from_secs(2), |submission| {
                    BeginWithUnexpectedSuccess {
                        submission,
                        success,
                    }
                })
                .await;
            assert_eq!(result, Err(WaiterError::TransportFault));
            assert_eq!(owner.lifecycle.phase(), RuntimePhase::Faulted);
            tokio::time::timeout(Duration::from_secs(2), owner.close())
                .await
                .unwrap()
                .unwrap();
        });
        assert!(handle.commands().is_empty());
        assert_eq!(handle.dispose_count(), 1);
    }
}

// Test-only submission latency separates the admission budget from result delivery.
struct BeginDelayedResult {
    submission: OperationSubmission<ConnectionId>,
    started: tokio::time::Instant,
}

impl OperationSpec for BeginDelayedResult {
    type Resource = ConnectionId;
    type Output = ConnectionId;
    type Correlation = ();

    fn correlation(&self) {}

    fn decode_success(
        correlation: (),
        resource: ConnectionId,
        success: WaiterSuccess,
    ) -> Option<ConnectionId> {
        BeginConnect::decode_success(correlation, resource, success)
    }
}

impl Message<BeginDelayedResult> for BlengActor {
    type Reply = ();

    async fn handle(
        &mut self,
        message: BeginDelayedResult,
        _: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        let submission = message.submission.clone();
        let result = async {
            assert!(message.submission.try_claim());
            let result_state = Arc::clone(&message.submission.state);
            tokio::spawn(async move {
                tokio::time::sleep_until(message.started + Duration::from_millis(450)).await;
                result_state
                    .result_tx
                    .send(Ok(WaiterSuccess::Connected(1.into())));
            });
            tokio::time::sleep_until(message.started + Duration::from_millis(200)).await;
            Ok(1.into())
        }
        .await;
        submission.resolve(result);
    }
}

#[test]
fn convenience_operation_spends_one_absolute_deadline_across_handoff_and_wait() {
    let owner = RuntimeOwner::new().unwrap();
    let (backend, _) = FakeBackend::scripted([]);
    let host = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .unwrap();
    host.block_on(async {
        owner.assemble_backend(Arc::new(backend)).await.unwrap();
        let started = tokio::time::Instant::now();
        let result = owner
            .run_operation(Duration::from_millis(350), |submission| {
                BeginDelayedResult {
                    submission,
                    started,
                }
            })
            .await;
        assert_eq!(
            result,
            Err(WaiterError::Timeout),
            "a fresh 350ms wait budget would incorrectly receive success at 450ms"
        );
        assert_eq!(owner.lifecycle.phase(), RuntimePhase::Open);
        owner.close().await.unwrap();
    });
}

struct InitializeBeforeClaim {
    submission: OperationSubmission,
    entered: tokio::sync::oneshot::Sender<()>,
    release: tokio::sync::oneshot::Receiver<()>,
}

impl OperationSpec for InitializeBeforeClaim {
    type Resource = ();
    type Output = AdapterSnapshot;
    type Correlation = ();
    fn correlation(&self) {}
    fn decode_success(_: (), _: (), success: WaiterSuccess) -> Option<AdapterSnapshot> {
        crate::runtime::messages::BeginInitialize::decode_success((), (), success)
    }
}

impl Message<InitializeBeforeClaim> for BlengActor {
    type Reply = ();
    async fn handle(&mut self, message: InitializeBeforeClaim, ctx: &mut Context<Self, ()>) {
        let submission = message.submission.clone();
        let result = async {
            self.require_open()?;
            self.require_not_started()?;
            let id = self.register_operation_waiter(
                &submission,
                crate::waiter::WaiterExpectation::Initialized,
            )?;
            message.entered.send(()).unwrap();
            message.release.await.unwrap();
            self.launch_dispatch(
                ctx,
                &submission,
                id,
                |reservations| reservations.reserve_initialize(id),
                move |hub| hub.initialize(InitializeCommand { command_id: id }),
            )?;
            Ok(())
        }
        .await;
        submission.resolve(result);
    }
}

#[test]
fn cancellation_after_registration_before_claim_delivers_immediately_and_prevents_dispatch() {
    for close in [false, true] {
        let host = tokio::runtime::Builder::new_current_thread()
            .enable_time()
            .build()
            .unwrap();
        let owner = RuntimeOwner::new().unwrap();
        let (backend, handle) = FakeBackend::scripted([]);
        host.block_on(async {
            owner.assemble_backend(Arc::new(backend)).await.unwrap();
            let (entered, entered_rx) = tokio::sync::oneshot::channel();
            let (release_tx, release) = tokio::sync::oneshot::channel();
            let pending = owner
                .begin_operation(|submission| InitializeBeforeClaim {
                    submission,
                    entered,
                    release,
                })
                .unwrap();
            tokio::time::timeout(Duration::from_secs(2), entered_rx)
                .await
                .unwrap()
                .unwrap();
            let (id, control, slot) = pending.into_parts();
            assert_eq!(id, CommandId::from(1));
            assert_eq!(
                tokio::time::timeout(Duration::from_secs(2), control.cancel(id))
                    .await
                    .unwrap(),
                Ok(())
            );
            // The actor still owns a registered waiter and is held before claim.
            // Cancellation must use the sole original sender, not wait for its mailbox.
            if close {
                owner.request_close();
            }
            assert_eq!(
                tokio::time::timeout(Duration::from_secs(2), slot.wait(Duration::from_secs(2)))
                    .await
                    .unwrap(),
                Err(WaiterError::Cancelled)
            );
            release_tx.send(()).unwrap();
            owner.close().await.unwrap();
            assert!(handle.commands().is_empty());
            assert_eq!(handle.dispose_count(), 1);
        });
    }
}

#[test]
fn dispatch_claim_winner_uses_native_not_cancellable_policy_even_before_native_entry() {
    let host = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .unwrap();
    let owner = RuntimeOwner::new().unwrap();
    let (backend, handle) = FakeBackend::scripted([EventScenario::accept(
        RecordedCommand::Initialize(InitializeCommand {
            command_id: 1.into(),
        }),
    )]);
    host.block_on(async {
        owner.assemble_backend(Arc::new(backend)).await.unwrap();
        let (entered_tx, entered_rx) = tokio::sync::oneshot::channel();
        let (release_tx, release_rx) = std::sync::mpsc::sync_channel(1);
        let pending = owner
            .begin_operation(|submission| {
                crate::runtime::actor::BeginInitializeWithBlockedReservation {
                    submission,
                    entered_tx,
                    release_rx,
                }
            })
            .unwrap();
        tokio::time::timeout(Duration::from_secs(2), entered_rx)
            .await
            .unwrap()
            .unwrap();
        let (id, control, slot) = pending.into_parts();
        assert!(handle.commands().is_empty());
        release_tx.send(()).unwrap();
        assert_eq!(
            tokio::time::timeout(Duration::from_secs(2), control.cancel(id))
                .await
                .unwrap(),
            Err(WaiterError::OperationNotCancellable(id))
        );
        tokio::time::timeout(Duration::from_secs(2), async {
            while handle.commands().is_empty() {
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap();
        assert_eq!(handle.commands().len(), 1);
        assert_eq!(
            slot.wait(Duration::from_millis(1)).await,
            Err(WaiterError::Timeout)
        );
        assert_eq!(
            control.cancel(id).await,
            Err(WaiterError::OperationNotCancellable(id))
        );
        owner.close().await.unwrap();
    });
}

#[test]
fn concurrent_synchronous_entries_share_one_non_reusing_allocator_and_bounded_mailbox() {
    let host = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .unwrap();
    let owner = Arc::new(RuntimeOwner::new().unwrap());
    let (backend, handle) = FakeBackend::scripted([]);
    host.block_on(async {
        owner.assemble_backend(Arc::new(backend)).await.unwrap();
        let release = owner.hold_actor_for_test().await;
        let start = Arc::new(std::sync::Barrier::new(8));
        let threads: Vec<_> = (0..8)
            .map(|_| {
                let owner = Arc::clone(&owner);
                let start = Arc::clone(&start);
                std::thread::spawn(move || {
                    start.wait();
                    (0..8)
                        .map(|_| {
                            owner
                                .begin_operation(|submission| {
                                    crate::runtime::messages::BeginInitialize { submission }
                                })
                                .unwrap()
                        })
                        .collect::<Vec<_>>()
                })
            })
            .collect();
        let entries: Vec<_> = threads
            .into_iter()
            .flat_map(|thread| thread.join().unwrap())
            .map(|entry| entry.into_parts())
            .collect();
        let mut ids: Vec<_> = entries.iter().map(|entry| entry.0.value).collect();
        ids.sort_unstable();
        assert_eq!(ids, (1..=64).collect::<Vec<_>>());
        assert!(matches!(
            owner.begin_operation(|submission| crate::runtime::messages::BeginInitialize {
                submission
            }),
            Err(WaiterError::SubmissionQueueFull)
        ));
        for (id, control, slot) in entries {
            assert_eq!(control.cancel(id).await, Ok(()));
            assert_eq!(
                slot.wait(Duration::from_secs(2)).await,
                Err(WaiterError::Cancelled)
            );
        }
        // Cancellation/delivery retirement does not remove queued entries or
        // manufacture spare capacity; no task or tombstone exists per failed try_send.
        assert!(matches!(
            owner.begin_operation(|submission| crate::runtime::messages::BeginInitialize {
                submission
            }),
            Err(WaiterError::SubmissionQueueFull)
        ));
        owner.request_close();
        release.notify_one();
        owner.close().await.unwrap();
        assert!(handle.commands().is_empty());
        assert_eq!(handle.dispose_count(), 1);
    });
}

#[test]
fn local_admission_error_wins_against_cancellation_that_already_observed_pending() {
    let host = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .unwrap();
    let owner = RuntimeOwner::new().unwrap();
    let (backend, handle) = FakeBackend::scripted([]);
    host.block_on(async {
        owner.assemble_backend(Arc::new(backend)).await.unwrap();
        let release = owner.hold_actor_for_test().await;
        let mut entry = None;
        let pending = owner
            .begin_operation(|submission| {
                entry = Some(submission.clone());
                crate::runtime::messages::BeginInitialize { submission }
            })
            .unwrap();
        let entry = entry.unwrap();
        // Model cancel() observing pending, then actor rejection winning before
        // its cancellation CAS. It must not return a false acceptance receipt.
        assert!(!entry.state.is_locally_settled());
        entry.state.reject(WaiterError::NotInitialized);
        assert!(!entry.state.try_cancel_before_claim());
        let (id, control, slot) = pending.into_parts();
        assert_eq!(
            slot.wait(Duration::from_secs(2)).await,
            Err(WaiterError::NotInitialized)
        );
        assert_eq!(
            control.cancel(id).await,
            Err(WaiterError::OperationAlreadyCompleted(id))
        );
        owner.request_close();
        release.notify_one();
        owner.close().await.unwrap();
        assert!(handle.commands().is_empty());
    });
}

fn snapshot() -> AdapterSnapshot {
    AdapterSnapshot {
        state: AdapterState::PoweredOn,
        scan_authorization: AuthorizationState::Authorized,
        connection_authorization: AuthorizationState::Authorized,
        name: None,
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
                owner.run_operation(deadline, |submission| BeginInitializeAfterCleanup {
                    submission,
                    phase,
                    expected_error: expected_error.clone(),
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
