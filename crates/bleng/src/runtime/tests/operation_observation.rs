use super::*;
use crate::{
    OperationFailureObserver,
    runtime::{CommandRoute, reservations::CancellationDecision, shared::OperationSubmission},
    waiter::{WaiterOutcome, WaiterSender, WaiterSuccess},
};
use blehub::{
    BleErrorCategory, BleEventError, CommandId, ConnectionState, ConnectionStateResult,
    ScanSessionId,
};
use std::sync::atomic::{AtomicUsize, Ordering};
use tokio::sync::{mpsc, oneshot};

#[derive(Default)]
struct Counter(AtomicUsize);
impl OperationFailureObserver for Counter {
    fn operation_failed(&self) {
        self.0.fetch_add(1, Ordering::SeqCst);
    }
}

fn fixture() -> (
    BlengActor,
    OperationSubmission<ScanSessionId>,
    oneshot::Receiver<WaiterOutcome>,
    mpsc::Receiver<BleEvent>,
) {
    let (mut actor, submission, result, receiver) = pending_fixture();
    actor.reservations.resolve_dispatch(1.into(), &Ok(()));
    (actor, submission, result, receiver)
}

fn pending_fixture() -> (
    BlengActor,
    OperationSubmission<ScanSessionId>,
    oneshot::Receiver<WaiterOutcome>,
    mpsc::Receiver<BleEvent>,
) {
    let (events, receiver) = mpsc::channel(64);
    let mut actor = BlengActor::with_ids(
        events,
        Arc::new(RuntimeLifecycle::new()),
        Arc::new(IdGenerator::default()),
    );
    let (sender, result) = WaiterSender::new();
    let submission = OperationSubmission::new(1.into(), sender);
    submission.enable_outcome_observation();
    actor
        .register_operation_waiter(&submission, WaiterExpectation::ScanStarted(10.into()))
        .unwrap();
    assert!(submission.try_claim());
    actor.reservations.reserve_scan_start(1.into(), 10.into());
    submission.observe_completion(actor.reservations.attach_completion_observer(1.into()));
    submission.resolve(Ok(10.into()));
    (actor, submission, result, receiver)
}

fn terminal(success: bool) -> BleEvent {
    BleEvent::CommandCompleted {
        command_id: 1.into(),
        error: (!success).then(|| BleEventError {
            category: BleErrorCategory::Internal,
            message: "scan failed".into(),
            platform_details: None,
        }),
    }
}

fn prune(actor: &mut BlengActor, receiver: oneshot::Receiver<WaiterOutcome>) {
    drop(receiver);
    let _other = actor
        .waiters
        .register(99.into(), WaiterExpectation::Initialized)
        .unwrap();
    assert!(
        !actor.waiters.contains(1.into()),
        "exercise real capacity-admission pruning"
    );
}

fn connected_fixture() -> (BlengActor, mpsc::Receiver<BleEvent>) {
    let (events, receiver) = mpsc::channel(64);
    let mut actor = BlengActor::with_ids(
        events,
        Arc::new(RuntimeLifecycle::new()),
        Arc::new(IdGenerator::default()),
    );
    let (connect_id, mut connected) = actor
        .register_waiter(WaiterExpectation::Connected(10.into()))
        .unwrap();
    actor
        .reservations
        .test_connections()
        .reserve_connection(20.into(), connect_id, 10.into());
    actor.reservations.resolve_dispatch(connect_id, &Ok(()));
    actor.handle_ingress_event(BleEvent::ConnectionStateChanged {
        command_id: Some(connect_id),
        connection_id: 10.into(),
        result: ConnectionStateResult::Ok {
            state: ConnectionState::Connected,
        },
    });
    actor.handle_ingress_event(rssi_terminal(connect_id, true));
    assert_eq!(
        connected.try_recv(),
        Ok(Ok(WaiterSuccess::Connected(10.into())))
    );

    (actor, receiver)
}

fn rssi_fixture() -> (
    BlengActor,
    OperationSubmission,
    oneshot::Receiver<WaiterOutcome>,
    mpsc::Receiver<BleEvent>,
) {
    let (mut actor, receiver) = connected_fixture();
    let peripheral = actor.reservations.validate_rssi(10.into()).unwrap();
    let (sender, result) = WaiterSender::new();
    let submission = OperationSubmission::new(2.into(), sender);
    // RSSI has no public observer API; opt in only through the internal submission.
    submission.enable_outcome_observation();
    actor
        .register_operation_waiter(&submission, WaiterExpectation::RssiRead(10.into()))
        .unwrap();
    assert!(submission.try_claim());
    actor.reservations.reserve_rssi(peripheral, submission.id);
    submission.observe_completion(actor.reservations.attach_completion_observer(submission.id));
    submission.resolve(Ok(()));
    actor.reservations.resolve_dispatch(submission.id, &Ok(()));
    (actor, submission, result, receiver)
}

fn rssi_terminal(command_id: CommandId, success: bool) -> BleEvent {
    BleEvent::CommandCompleted {
        command_id,
        error: (!success).then(|| BleEventError {
            category: BleErrorCategory::Internal,
            message: "RSSI failed".into(),
            platform_details: None,
        }),
    }
}

fn rssi_value(command_id: CommandId) -> BleEvent {
    BleEvent::RssiRead {
        command_id,
        connection_id: 10.into(),
        rssi: -41,
    }
}

fn rssi_loss() -> BleEvent {
    BleEvent::ConnectionStateChanged {
        command_id: None,
        connection_id: 10.into(),
        result: ConnectionStateResult::Ok {
            state: ConnectionState::Disconnected,
        },
    }
}

fn prune_rssi(
    actor: &mut BlengActor,
    submission: &OperationSubmission,
    receiver: oneshot::Receiver<WaiterOutcome>,
) {
    drop(receiver);
    let _other = actor
        .waiters
        .register(99.into(), WaiterExpectation::Initialized)
        .unwrap();
    assert!(
        !actor.waiters.contains(submission.id),
        "exercise real capacity-admission pruning for RSSI"
    );
    assert!(
        actor
            .reservations
            .command_completion(submission.id)
            .is_some()
    );
}

fn expected_rssi_outcome(command_id: CommandId, success: bool) -> WaiterOutcome {
    match rssi_terminal(command_id, success) {
        BleEvent::CommandCompleted {
            error: Some(error), ..
        } => Err(WaiterError::Operation(error)),
        _ => Ok(WaiterSuccess::RssiRead(10.into(), -41)),
    }
}

#[test]
fn disconnect_observation_success_requires_terminal_and_matching_loss_with_live_or_pruned_caller() {
    for terminal_first in [false, true] {
        for pruned in [false, true] {
            let (mut actor, _events) = connected_fixture();
            let peripheral = actor
                .reservations
                .test_connections()
                .validate_disconnect(10.into())
                .unwrap();
            let (sender, result) = WaiterSender::new();
            let submission = OperationSubmission::new(2.into(), sender);
            // Explicit disconnect opts in internally, without a public observer API.
            submission.enable_outcome_observation();
            actor
                .register_operation_waiter(&submission, WaiterExpectation::Disconnected(10.into()))
                .unwrap();
            assert!(submission.try_claim());
            actor
                .reservations
                .test_connections()
                .reserve_disconnect(peripheral, submission.id);
            submission
                .observe_completion(actor.reservations.attach_completion_observer(submission.id));
            submission.resolve(Ok(()));
            actor.reservations.resolve_dispatch(submission.id, &Ok(()));
            let mut result = if pruned {
                drop(result);
                let _other = actor
                    .waiters
                    .register(99.into(), WaiterExpectation::Initialized)
                    .unwrap();
                assert!(!actor.waiters.contains(submission.id));
                assert!(
                    actor
                        .reservations
                        .command_completion(submission.id)
                        .is_some()
                );
                None
            } else {
                Some(result)
            };
            let counter = Arc::new(Counter::default());
            let sink: Arc<dyn OperationFailureObserver> = counter.clone();
            let observation = submission.state.outcome_observation.get().unwrap();
            let early = observation.register(Arc::downgrade(&sink)).unwrap();
            let terminal = BleEvent::CommandCompleted {
                command_id: submission.id,
                error: None,
            };
            let disconnected = BleEvent::ConnectionStateChanged {
                command_id: Some(submission.id),
                connection_id: 10.into(),
                result: ConnectionStateResult::Ok {
                    state: ConnectionState::Disconnected,
                },
            };
            let evidence = if terminal_first {
                [terminal.clone(), disconnected.clone()]
            } else {
                [disconnected.clone(), terminal.clone()]
            };
            actor.handle_ingress_event(evidence[0].clone());
            assert!(actor.lifecycle.is_open());
            assert!(
                !submission.state.is_completed(),
                "disconnect needs both terminal and domain evidence (terminal_first={terminal_first}, pruned={pruned})"
            );
            assert_eq!(counter.0.load(Ordering::SeqCst), 0);
            if let Some(result) = result.as_mut() {
                assert_eq!(result.try_recv(), Err(oneshot::error::TryRecvError::Empty));
            }
            actor.handle_ingress_event(evidence[1].clone());
            assert!(submission.state.is_completed());
            assert_eq!(
                counter.0.load(Ordering::SeqCst),
                0,
                "matching loss completes explicit disconnect successfully, not as passive failure"
            );
            drop(early);
            let _late = observation.register(Arc::downgrade(&sink)).unwrap();
            assert_eq!(counter.0.load(Ordering::SeqCst), 0);
            actor.handle_ingress_event(disconnected);
            actor.handle_ingress_event(rssi_loss());
            actor.handle_ingress_event(terminal);
            assert!(actor.lifecycle.is_open());
            assert!(submission.state.is_completed());
            assert_eq!(counter.0.load(Ordering::SeqCst), 0);
            if let Some(result) = result.as_mut() {
                assert_eq!(
                    result.try_recv(),
                    Ok(Ok(WaiterSuccess::Disconnected(10.into()))),
                    "observers and duplicate callbacks must preserve the sole typed success"
                );
                assert_eq!(result.try_recv(), Err(oneshot::error::TryRecvError::Closed));
            }
        }
    }
}

#[test]
fn rssi_observation_native_terminal_preserves_wait_result_and_first_outcome() {
    for success in [false, true] {
        for late in [false, true] {
            let (mut actor, submission, mut result, _events) = rssi_fixture();
            let counter = Arc::new(Counter::default());
            let sink: Arc<dyn OperationFailureObserver> = counter.clone();
            let observation = submission.state.outcome_observation.get().unwrap();
            let mut token = if late {
                None
            } else {
                observation.register(Arc::downgrade(&sink))
            };
            actor.handle_ingress_event(rssi_value(submission.id));
            assert_eq!(result.try_recv(), Err(oneshot::error::TryRecvError::Empty));
            assert!(!submission.state.is_completed());
            assert_eq!(counter.0.load(Ordering::SeqCst), 0);
            actor.handle_ingress_event(rssi_terminal(submission.id, success));
            assert!(submission.state.is_completed());
            if late {
                token = observation.register(Arc::downgrade(&sink));
            }
            assert!(token.is_some());
            let failures = usize::from(!success);
            assert_eq!(counter.0.load(Ordering::SeqCst), failures);
            assert_eq!(
                result.try_recv(),
                Ok(expected_rssi_outcome(submission.id, success)),
                "failure observation must not consume the caller's typed result"
            );
            actor.handle_ingress_event(rssi_terminal(submission.id, !success));
            actor.handle_ingress_event(rssi_loss());
            assert_eq!(counter.0.load(Ordering::SeqCst), failures);
            assert_eq!(result.try_recv(), Err(oneshot::error::TryRecvError::Closed));
            drop(token);
            let _replay = observation.register(Arc::downgrade(&sink)).unwrap();
            assert_eq!(counter.0.load(Ordering::SeqCst), 2 * failures);
            assert!(actor.lifecycle.is_open());
        }
    }
}

fn assert_rssi_passive_loss_observation(pruned: bool) {
    for late in [false, true] {
        let (mut actor, submission, result, _events) = rssi_fixture();
        let mut result = if pruned {
            prune_rssi(&mut actor, &submission, result);
            None
        } else {
            Some(result)
        };
        let counter = Arc::new(Counter::default());
        let sink: Arc<dyn OperationFailureObserver> = counter.clone();
        let observation = submission.state.outcome_observation.get().unwrap();
        let mut token = if late {
            None
        } else {
            observation.register(Arc::downgrade(&sink))
        };
        // A value without a terminal must still fail on passive connection loss.
        actor.handle_ingress_event(rssi_value(submission.id));
        assert!(!submission.state.is_completed());
        assert_eq!(counter.0.load(Ordering::SeqCst), 0);
        actor.handle_ingress_event(rssi_loss());
        assert!(actor.lifecycle.is_open());
        assert_eq!(
            actor.reservations.validate_rssi(10.into()),
            Err(WaiterError::ConnectionNotActive)
        );
        if let Some(result) = result.as_mut() {
            assert_eq!(result.try_recv(), Ok(Err(WaiterError::Disconnected)));
        }
        if late {
            token = observation.register(Arc::downgrade(&sink));
        }
        assert!(token.is_some());
        assert_eq!(
            counter.0.load(Ordering::SeqCst),
            1,
            "unfinished RSSI must notify failure on passive loss (pruned={pruned}, late={late})"
        );
        actor.handle_ingress_event(rssi_loss());
        actor.handle_ingress_event(rssi_terminal(submission.id, true));
        actor.handle_ingress_event(rssi_terminal(submission.id, false));
        assert_eq!(counter.0.load(Ordering::SeqCst), 1);
        drop(token);
        let _replay = observation.register(Arc::downgrade(&sink)).unwrap();
        assert_eq!(counter.0.load(Ordering::SeqCst), 2);
    }
}

#[test]
fn rssi_observation_passive_loss_notifies_without_consuming_wait_result() {
    assert_rssi_passive_loss_observation(false);
}

#[test]
fn rssi_observation_passive_loss_notifies_after_caller_receiver_prune() {
    assert_rssi_passive_loss_observation(true);
}

#[test]
fn rssi_observation_passive_loss_preserves_deferred_native_outcome_until_control_rejection() {
    for success in [false, true] {
        for pruned in [false, true] {
            let (mut actor, submission, result, _events) = rssi_fixture();
            let mut result = if pruned {
                prune_rssi(&mut actor, &submission, result);
                None
            } else {
                Some(result)
            };
            let receipt = match actor.reservations.request_cancellation(submission.id) {
                Some(CancellationDecision::Accepted(receipt)) => receipt,
                _ => panic!("RSSI cancellation must be admitted"),
            };
            assert!(
                actor
                    .reservations
                    .try_claim_cancellation_dispatch(submission.id)
            );
            actor.handle_ingress_event(rssi_value(submission.id));
            actor.handle_ingress_event(rssi_terminal(submission.id, success));
            assert!(submission.state.is_completed());
            assert!(matches!(receipt.has_changed(), Ok(false)));
            let counter = Arc::new(Counter::default());
            let sink: Arc<dyn OperationFailureObserver> = counter.clone();
            let observation = submission.state.outcome_observation.get().unwrap();
            let token = observation.register(Arc::downgrade(&sink)).unwrap();
            assert_eq!(counter.0.load(Ordering::SeqCst), 0);
            actor.handle_ingress_event(rssi_loss());
            assert!(actor.lifecycle.is_open());
            assert!(matches!(receipt.has_changed(), Ok(false)));
            if let Some(result) = result.as_mut() {
                assert_eq!(result.try_recv(), Err(oneshot::error::TryRecvError::Empty));
            }
            assert_eq!(
                counter.0.load(Ordering::SeqCst),
                0,
                "loss cannot finalize an already completed RSSI while control is pending"
            );
            drop(token);
            let token = observation.register(Arc::downgrade(&sink)).unwrap();
            assert_eq!(counter.0.load(Ordering::SeqCst), 0);
            let rejection = Err(WaiterError::OperationAlreadyCompleted(submission.id));
            actor.handle_cancellation_result(submission.id, rejection.clone());
            assert_eq!(*receipt.borrow(), Some(rejection.clone()));
            let failures = usize::from(!success);
            assert_eq!(counter.0.load(Ordering::SeqCst), failures);
            if let Some(result) = result.as_mut() {
                assert_eq!(
                    result.try_recv(),
                    Ok(expected_rssi_outcome(submission.id, success)),
                    "control rejection must preserve the native result, not Disconnected"
                );
                assert_eq!(result.try_recv(), Err(oneshot::error::TryRecvError::Closed));
            }
            actor.handle_cancellation_result(submission.id, rejection);
            actor.handle_ingress_event(rssi_loss());
            actor.handle_ingress_event(rssi_terminal(submission.id, !success));
            assert_eq!(counter.0.load(Ordering::SeqCst), failures);
            drop(token);
            let _replay = observation.register(Arc::downgrade(&sink)).unwrap();
            assert_eq!(counter.0.load(Ordering::SeqCst), 2 * failures);
            assert!(actor.lifecycle.is_open());
        }
    }
}

#[test]
fn scan_start_dispatch_rejection_notifies_without_retaining_observation_in_directory() {
    for late in [false, true] {
        let (mut actor, submission, mut result, _events) = pending_fixture();
        let counter = Arc::new(Counter::default());
        let sink: Arc<dyn OperationFailureObserver> = counter.clone();
        let observation = submission.state.outcome_observation.get().unwrap();
        let weak_observation = Arc::downgrade(observation);
        let mut token = if late {
            None
        } else {
            observation.register(Arc::downgrade(&sink))
        };
        let rejection = blehub::CommandDispatchError::InvalidState {
            detail: "scan dispatch rejected without a BLE callback".into(),
        };
        let prepared = kameo::actor::PreparedActor::<BlengActor>::new(kameo::mailbox::bounded(1));
        actor.handle_dispatch_result(
            1.into(),
            Err(rejection.clone()),
            prepared.actor_ref().clone(),
        );
        assert_eq!(
            result.try_recv().unwrap(),
            Err(WaiterError::DispatchRejected(rejection))
        );
        if late {
            token = observation.register(Arc::downgrade(&sink));
        }
        assert!(token.is_some());
        assert_eq!(counter.0.load(Ordering::SeqCst), 1);
        assert!(actor.reservations.command_completion(1.into()).is_none());
        assert_eq!(actor.reservations.require_scan_available(), Ok(()));
        drop(submission);
        assert!(
            weak_observation.upgrade().is_none(),
            "the live actor must not retain rejected scan observation state"
        );
        // The registration retains only its listener, not the observation or Hub.
        assert!(token.is_some());
    }
}

#[test]
fn scan_start_failure_after_waiter_prune_reaches_early_and_late_observers() {
    for late in [false, true] {
        let (mut actor, submission, result, _events) = fixture();
        let counter = Arc::new(Counter::default());
        let sink: Arc<dyn OperationFailureObserver> = counter.clone();
        let observation = submission.state.outcome_observation.get().unwrap();
        let mut token = if late {
            None
        } else {
            observation.register(Arc::downgrade(&sink))
        };
        prune(&mut actor, result);
        actor.handle_ingress_event(terminal(false));
        if late {
            token = observation.register(Arc::downgrade(&sink));
        }
        assert!(token.is_some());
        assert_eq!(counter.0.load(Ordering::SeqCst), 1);
        actor.handle_ingress_event(terminal(false));
        assert_eq!(counter.0.load(Ordering::SeqCst), 1);
    }
}

#[test]
fn scan_start_failure_control_decides_pruned_native_result_before_observer_replay() {
    for native_success in [false, true] {
        for cancel_accepted in [false, true] {
            let (mut actor, submission, result, _events) = fixture();
            prune(&mut actor, result);
            assert!(matches!(
                actor.reservations.request_cancellation(1.into()),
                Some(CancellationDecision::Accepted(_))
            ));
            actor.handle_ingress_event(BleEvent::ScanStarted {
                command_id: 1.into(),
                scan_id: 10.into(),
            });
            actor.handle_ingress_event(terminal(native_success));
            let counter = Arc::new(Counter::default());
            let sink: Arc<dyn OperationFailureObserver> = counter.clone();
            let observation = submission.state.outcome_observation.get().unwrap();
            let token = observation.register(Arc::downgrade(&sink)).unwrap();
            assert_eq!(
                counter.0.load(Ordering::SeqCst),
                0,
                "native result is not final while control is pending"
            );
            actor.handle_cancellation_result(
                1.into(),
                if cancel_accepted {
                    Ok(())
                } else {
                    Err(WaiterError::OperationNotCancellable(1.into()))
                },
            );
            let failed = usize::from(cancel_accepted || !native_success);
            assert_eq!(counter.0.load(Ordering::SeqCst), failed);
            drop(token);
            let _late = observation.register(Arc::downgrade(&sink)).unwrap();
            assert_eq!(counter.0.load(Ordering::SeqCst), 2 * failed);
        }
    }
}

#[test]
fn scan_start_failure_success_is_not_overwritten_by_stop_failure_or_duplicate_terminal() {
    let (mut actor, submission, mut result, _events) = fixture();
    actor.handle_ingress_event(BleEvent::ScanStarted {
        command_id: 1.into(),
        scan_id: 10.into(),
    });
    actor.handle_ingress_event(terminal(true));
    assert!(result.try_recv().unwrap().is_ok());
    let counter = Arc::new(Counter::default());
    let sink: Arc<dyn OperationFailureObserver> = counter.clone();
    let _token = submission
        .state
        .outcome_observation
        .get()
        .unwrap()
        .register(Arc::downgrade(&sink))
        .unwrap();
    actor
        .reservations
        .register_command(2.into(), CommandRoute::ScanStop);
    actor.reservations.reserve_scan_stop(2.into());
    actor.reservations.attach_completion_observer(2.into());
    actor.reservations.resolve_dispatch(2.into(), &Ok(()));
    actor.handle_ingress_event(BleEvent::CommandCompleted {
        command_id: 2.into(),
        error: Some(BleEventError {
            category: BleErrorCategory::Internal,
            message: "stop failed".into(),
            platform_details: None,
        }),
    });
    actor.handle_ingress_event(terminal(false));
    assert_eq!(counter.0.load(Ordering::SeqCst), 0);
}

#[test]
fn scan_start_failure_control_before_native_result_preserves_acceptance_and_rejection() {
    for accepted in [false, true] {
        let (mut actor, submission, result, _events) = fixture();
        prune(&mut actor, result);
        assert!(matches!(
            actor.reservations.request_cancellation(1.into()),
            Some(CancellationDecision::Accepted(_))
        ));
        actor.handle_cancellation_result(
            1.into(),
            if accepted {
                Ok(())
            } else {
                Err(WaiterError::OperationNotCancellable(1.into()))
            },
        );
        let counter = Arc::new(Counter::default());
        let sink: Arc<dyn OperationFailureObserver> = counter.clone();
        let _token = submission
            .state
            .outcome_observation
            .get()
            .unwrap()
            .register(Arc::downgrade(&sink))
            .unwrap();
        assert_eq!(counter.0.load(Ordering::SeqCst), usize::from(accepted));
        actor.handle_ingress_event(BleEvent::ScanStarted {
            command_id: 1.into(),
            scan_id: 10.into(),
        });
        actor.handle_ingress_event(terminal(true));
        assert_eq!(counter.0.load(Ordering::SeqCst), usize::from(accepted));
    }
}

struct ScanBeforeDispatch {
    submission: OperationSubmission<ScanSessionId>,
    fault: bool,
    entered: oneshot::Sender<()>,
    release: oneshot::Receiver<()>,
}

impl crate::runtime::OperationSpec for ScanBeforeDispatch {
    type Resource = ScanSessionId;
    type Output = ScanSessionId;
    type Correlation = ();

    fn correlation(&self) {}

    fn decode_success(
        _: (),
        _: ScanSessionId,
        _: crate::waiter::WaiterSuccess,
    ) -> Option<ScanSessionId> {
        panic!("lifecycle rejection must not decode a successful scan")
    }
}

impl kameo::prelude::Message<ScanBeforeDispatch> for BlengActor {
    type Reply = ();

    async fn handle(
        &mut self,
        message: ScanBeforeDispatch,
        ctx: &mut kameo::prelude::Context<Self, ()>,
    ) {
        self.require_open().unwrap();
        let id = self
            .register_operation_waiter(
                &message.submission,
                WaiterExpectation::ScanStarted(10.into()),
            )
            .unwrap();
        message.entered.send(()).unwrap();
        message.release.await.unwrap();
        // Reproduce loss after admission/registration but before hub acquisition.
        // launch_dispatch obtains the hub BEFORE its submission.try_claim CAS.
        if message.fault {
            self.lifecycle.fault();
        } else {
            self.lifecycle.request_close();
        }
        let error = self
            .launch_dispatch(
                ctx,
                &message.submission,
                id,
                |_| panic!("closed dispatch must not create a scan reservation"),
                |_| panic!("closed dispatch must not call the native backend"),
            )
            .expect_err("hub_for_dispatch must reject the retired lifecycle");
        assert_eq!(
            error,
            if message.fault {
                WaiterError::TransportFault
            } else {
                WaiterError::Closed
            }
        );
        assert!(!self.waiters.contains(id));
        assert!(self.reservations.command_completion(id).is_none());
        message.submission.resolve(Err(error));
    }
}

#[test]
fn scan_start_lifecycle_rejection_at_hub_acquisition_does_not_notify() {
    use crate::runtime::RuntimeOwner;
    use blehub_testkit::FakeBackend;
    use std::time::Duration;

    let caller = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .unwrap();
    caller.block_on(async {
        for fault in [false, true] {
            let owner = RuntimeOwner::new().unwrap();
            let (backend, handle) = FakeBackend::scripted([]);
            owner.assemble_backend(Arc::new(backend)).await.unwrap();
            let (entered, entered_rx) = oneshot::channel();
            let (release_tx, release) = oneshot::channel();
            let mut entry = None;
            let operation = owner
                .submit_operation(true, |submission| {
                    submission.enable_outcome_observation();
                    entry = Some(submission.clone());
                    ScanBeforeDispatch {
                        submission,
                        fault,
                        entered,
                        release,
                    }
                })
                .unwrap();
            let submission = entry.unwrap();
            let counter = Arc::new(Counter::default());
            let sink: Arc<dyn OperationFailureObserver> = counter.clone();
            let observation = submission.state.outcome_observation.get().unwrap();
            let early = observation.register(Arc::downgrade(&sink)).unwrap();
            tokio::time::timeout(Duration::from_secs(2), entered_rx)
                .await
                .unwrap()
                .unwrap();
            release_tx.send(()).unwrap();
            let (_, _, result) = operation.into_parts();
            assert_eq!(
                result.wait(Duration::from_secs(2)).await,
                Err(if fault {
                    WaiterError::TransportFault
                } else {
                    WaiterError::Closed
                })
            );
            owner.close().await.unwrap();
            assert!(handle.commands().is_empty());
            assert_eq!(handle.dispose_count(), 1);
            assert!(submission.state.is_locally_settled());
            let early_count = counter.0.load(Ordering::SeqCst);
            drop(early);
            let _late = observation.register(Arc::downgrade(&sink)).unwrap();
            assert_eq!((early_count, counter.0.load(Ordering::SeqCst)), (0, 0));
        }
    });
}

#[test]
fn scan_start_failure_is_not_inferred_from_reservation_drop() {
    let (actor, submission, result, _events) = fixture();
    let counter = Arc::new(Counter::default());
    let sink: Arc<dyn OperationFailureObserver> = counter.clone();
    let _token = submission
        .state
        .outcome_observation
        .get()
        .unwrap()
        .register(Arc::downgrade(&sink))
        .unwrap();
    drop(result);
    drop(actor);
    assert!(submission.state.is_completed());
    assert_eq!(counter.0.load(Ordering::SeqCst), 0);
}
