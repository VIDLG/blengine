use super::super::{
    CancellationDecision, CancellationReceiver, CommandProgress, DispatchAction,
    PublicationDecision, PublicationGate, PublicationRelease, ScopedPublication,
};
use crate::waiter::WaiterError;
use blehub::{BleErrorCategory, BleEventError};

#[test]
fn initialization_failure_does_not_skip_other_roots_retirement() {
    use super::super::ReservationCoordinator;
    use crate::runtime::command::CommandRoute;

    let mut reservations = ReservationCoordinator::default();
    // Install roots directly to isolate retirement from normal admission rules.
    reservations.reserve_initialize(1.into());
    reservations.register_command(1.into(), CommandRoute::Initialization);
    reservations.reserve_scan_start(2.into(), 20.into());
    reservations.register_command(2.into(), CommandRoute::ScanStart);
    reservations.reserve_connection(30.into(), 3.into(), 40.into());
    reservations.register_command(
        3.into(),
        CommandRoute::Connection {
            connection_id: 40.into(),
        },
    );
    let scan_completion = reservations.attach_completion_observer(2.into());
    let connection_completion = reservations.attach_completion_observer(3.into());
    let error = BleEventError {
        category: BleErrorCategory::Internal,
        message: "terminal failure".into(),
        platform_details: None,
    };
    for command_id in [1.into(), 2.into(), 3.into()] {
        reservations.resolve_dispatch(command_id, &Ok(()));
        reservations.record_terminal(command_id, Some(error.clone()));
    }

    assert_eq!(
        reservations.advance_and_retire(),
        Err(WaiterError::Operation(error))
    );
    for command_id in [2.into(), 3.into()] {
        assert!(reservations.commands.route(command_id).is_none());
        assert!(reservations.command_completion(command_id).is_none());
    }
    assert!(scan_completion.is_completed());
    assert!(connection_completion.is_completed());
    assert_eq!(reservations.require_scan_available(), Ok(()));
    assert_eq!(reservations.require_peripheral_available(30.into()), Ok(()));
    assert_eq!(
        reservations.commands.route(1.into()),
        Some(CommandRoute::Initialization)
    );
}

#[test]
fn scan_start_dispatch_rejection_removes_only_the_released_command_entry() {
    use super::super::{ReconciliationFault, ReservationCoordinator};
    use crate::runtime::command::CommandRoute;
    use blehub::{BleEvent, CommandDispatchError, CommandId};

    for with_callback in [false, true] {
        let mut reservations = ReservationCoordinator::default();
        reservations.reserve_initialize(1.into());
        reservations.register_command(1.into(), CommandRoute::Initialization);
        let initialization = reservations.attach_completion_observer(1.into());
        let rejection = Err(CommandDispatchError::InvalidState {
            detail: "scan start rejected".into(),
        });
        // A route without a matching reservation must not be treated as a consumed result.
        reservations.register_command(99.into(), CommandRoute::ScanStart);
        reservations.resolve_dispatch(99.into(), &rejection);
        assert_eq!(
            reservations.commands.route(99.into()),
            Some(CommandRoute::ScanStart)
        );

        for value in 2..10_u64 {
            let command_id = CommandId::from(value);
            let session_id = (value + 100).into();
            reservations.reserve_scan_start(command_id, session_id);
            reservations.register_command(command_id, CommandRoute::ScanStart);
            let completion = reservations.attach_completion_observer(command_id);
            if with_callback {
                assert!(
                    reservations
                        .gate_event(BleEvent::ScanStarted {
                            command_id,
                            scan_id: session_id,
                        })
                        .unwrap()
                        .is_none()
                );
            }

            let resolution = reservations.resolve_dispatch(command_id, &rejection);
            if with_callback {
                assert!(matches!(
                    resolution.action,
                    DispatchAction::FaultRuntime(ReconciliationFault::DispatchEvidenceConflict)
                ));
            } else {
                assert!(matches!(resolution.action, DispatchAction::NoAction));
            }
            assert!(resolution.events_to_replay.is_empty());
            assert!(completion.is_completed());
            assert!(reservations.commands.route(command_id).is_none());
            assert!(reservations.command_completion(command_id).is_none());
            assert_eq!(reservations.require_scan_available(), Ok(()));
            reservations.advance_and_retire().unwrap();

            let duplicate = reservations.resolve_dispatch(command_id, &rejection);
            assert!(matches!(duplicate.action, DispatchAction::NoAction));
            assert!(duplicate.events_to_replay.is_empty());
            assert_eq!(
                reservations.commands.route(1.into()),
                Some(CommandRoute::Initialization)
            );
            assert!(reservations.command_completion(1.into()).is_some());
            assert!(!initialization.is_completed());
        }
    }
}

#[test]
fn scan_stop_dispatch_rejection_releases_command_but_preserves_session_and_replay() {
    use super::super::ReservationCoordinator;
    use crate::runtime::command::CommandRoute;
    use blehub::{BleEvent, CommandDispatchError, CommandId, ScanStopReason, ScanStopResult};

    for passive_end in [false, true] {
        let mut reservations = ReservationCoordinator::default();
        reservations.reserve_scan_start(1.into(), 10.into());
        reservations.register_command(1.into(), CommandRoute::ScanStart);
        let start = reservations.attach_completion_observer(1.into());
        reservations.resolve_dispatch(1.into(), &Ok(()));
        reservations
            .record_event_evidence(&BleEvent::ScanStarted {
                command_id: 1.into(),
                scan_id: 10.into(),
            })
            .unwrap();
        reservations.record_terminal(1.into(), None);
        let rejection = Err(CommandDispatchError::InvalidState {
            detail: "scan stop rejected".into(),
        });
        let stopped = BleEvent::ScanStopped {
            scan_id: 10.into(),
            result: ScanStopResult::Ok {
                reason: ScanStopReason::SystemSuspended,
            },
        };

        for value in 2..10_u64 {
            let command_id = CommandId::from(value);
            reservations.reserve_scan_stop(command_id);
            reservations.register_command(command_id, CommandRoute::ScanStop);
            let stop = reservations.attach_completion_observer(command_id);
            if passive_end {
                assert!(reservations.gate_event(stopped.clone()).unwrap().is_none());
            }
            let resolution = reservations.resolve_dispatch(command_id, &rejection);
            assert!(matches!(resolution.action, DispatchAction::NoAction));
            assert!(stop.is_completed());
            assert!(reservations.commands.route(command_id).is_none());
            assert!(reservations.command_completion(command_id).is_none());
            assert_eq!(
                reservations.commands.route(1.into()),
                Some(CommandRoute::ScanStart)
            );
            assert!(reservations.command_completion(1.into()).is_some());
            assert!(!start.is_completed());
            assert_eq!(
                reservations.require_scan_available(),
                Err(WaiterError::ScanBusy)
            );
            assert_eq!(reservations.validate_scan_stop(10.into()), Ok(()));

            if passive_end {
                assert_eq!(resolution.events_to_replay, vec![stopped.clone()]);
                for event in resolution.events_to_replay {
                    reservations.record_event_evidence(&event).unwrap();
                }
                reservations.advance_and_retire().unwrap();
                assert!(start.is_completed());
                assert!(reservations.commands.route(1.into()).is_none());
                assert!(reservations.command_completion(1.into()).is_none());
                assert_eq!(reservations.require_scan_available(), Ok(()));
                break;
            }
            assert!(resolution.events_to_replay.is_empty());
            reservations.advance_and_retire().unwrap();
            let duplicate = reservations.resolve_dispatch(command_id, &rejection);
            assert!(matches!(duplicate.action, DispatchAction::NoAction));
            assert!(duplicate.events_to_replay.is_empty());
        }
    }
}

#[derive(Clone, Copy, Debug)]
enum Step {
    Request,
    Acknowledge,
    Claim,
    ResolveAccepted,
    ResolveRejected,
    Clear,
    TerminalSuccess,
    TerminalFailure,
}
const STEPS: [Step; 8] = [
    Step::Request,
    Step::Acknowledge,
    Step::Claim,
    Step::ResolveAccepted,
    Step::ResolveRejected,
    Step::Clear,
    Step::TerminalSuccess,
    Step::TerminalFailure,
];

fn check_cancel_trace(trace: &[Step]) {
    struct Attempt {
        claimed: bool,
        result: Option<Result<(), WaiterError>>,
    }
    let failure = Err(BleEventError {
        category: BleErrorCategory::Cancelled,
        message: "terminal before the cancellation receipt".into(),
        platform_details: None,
    });
    let mut progress = CommandProgress::new(1.into());
    let mut acknowledged = false;
    let mut terminal = None;
    let mut active: Option<usize> = None;
    let mut attempts: Vec<Attempt> = Vec::new();
    let mut receipts: Vec<(CancellationReceiver, usize)> = Vec::new();
    for (index, step) in trace.iter().enumerate() {
        match step {
            Step::Request => match (active, terminal.is_some(), progress.request_cancellation()) {
                (Some(attempt), _, CancellationDecision::AlreadyRequested(receipt)) => {
                    receipts.push((receipt, attempt));
                }
                (None, true, CancellationDecision::AlreadyCompleted) => {}
                (None, false, CancellationDecision::Accepted(receipt)) => {
                    active = Some(attempts.len());
                    receipts.push((receipt, attempts.len()));
                    attempts.push(Attempt {
                        claimed: false,
                        result: None,
                    });
                }
                _ => panic!("request classification differs at {index}: {trace:?}"),
            },
            Step::Acknowledge => {
                acknowledged = true;
                assert!(
                    progress
                        .resolve_accepted_dispatch()
                        .events_to_replay
                        .is_empty()
                );
            }
            Step::Claim => {
                let expected =
                    acknowledged && active.is_some_and(|attempt| !attempts[attempt].claimed);
                assert_eq!(
                    progress.try_claim_cancellation_dispatch(),
                    expected,
                    "claim at {index}: {trace:?}"
                );
                if expected {
                    attempts[active.unwrap()].claimed = true;
                }
            }
            Step::ResolveAccepted | Step::ResolveRejected => {
                let result = if matches!(step, Step::ResolveAccepted) {
                    Ok(())
                } else {
                    Err(WaiterError::OperationNotCancellable(1.into()))
                };
                progress.resolve_cancellation(result.clone());
                if let Some(attempt) = active {
                    // Preserve existing send_replace semantics, including a
                    // repeated resolution. This is not a first-wins receipt.
                    attempts[attempt].result = Some(result);
                }
            }
            Step::Clear => {
                active = None;
                progress.clear_cancellation();
            }
            Step::TerminalSuccess | Step::TerminalFailure => {
                let result = if matches!(step, Step::TerminalSuccess) {
                    Ok(())
                } else {
                    failure.clone()
                };
                progress.record_terminal(&result);
                terminal.get_or_insert(result);
            }
        }
        let pending = active.is_some_and(|attempt| attempts[attempt].result.is_none());
        assert_eq!(
            progress.is_cancellation_requested(),
            active.is_some(),
            "requested at {index}: {trace:?}"
        );
        assert_eq!(
            progress.is_awaiting_dispatch_result(),
            !acknowledged,
            "dispatch at {index}: {trace:?}"
        );
        assert_eq!(
            progress.is_awaiting_cancellation_result(),
            pending,
            "receipt at {index}: {trace:?}"
        );
        assert_eq!(
            progress.is_cancellation_accepted(),
            active.is_some_and(|attempt| attempts[attempt].result == Some(Ok(()))),
            "accepted at {index}: {trace:?}"
        );
        let expected_terminal = if acknowledged {
            terminal
                .clone()
                .map(|result| result.map_err(WaiterError::Operation))
        } else {
            None
        };
        assert_eq!(
            progress.terminal_result(),
            expected_terminal,
            "terminal at {index}: {trace:?}"
        );
        for domain_ready in [false, true] {
            let complete = acknowledged
                && !pending
                && match &terminal {
                    Some(Ok(())) => domain_ready,
                    Some(Err(_)) => true,
                    None => false,
                };
            assert_eq!(
                progress.is_complete(domain_ready),
                complete,
                "retirement at {index}: {trace:?}"
            );
        }
        for (receipt, attempt) in &receipts {
            assert_eq!(
                *receipt.borrow(),
                attempts[*attempt].result,
                "receipt generation at {index}: {trace:?}"
            );
            assert_eq!(
                receipt.has_changed().is_err(),
                active != Some(*attempt),
                "receipt ownership at {index}: {trace:?}"
            );
        }
    }
}

#[test]
fn cancel_state_bounded_traces_preserve_receipts_claims_and_retirement() {
    for mut encoded in 0..STEPS.len().pow(5) {
        let trace: Vec<_> = (0..5)
            .map(|_| {
                let step = STEPS[encoded % STEPS.len()];
                encoded /= STEPS.len();
                step
            })
            .collect();
        check_cancel_trace(&trace);
    }
}

proptest::proptest! {
    #![proptest_config(proptest::test_runner::Config {
        cases: 256,
        max_shrink_iters: 2_048,
        max_shrink_time: 1_000,
        ..proptest::test_runner::Config::default()
    })]
    #[test]
    fn cancel_state_generated_traces_preserve_receipts_claims_and_retirement(
        trace in proptest::collection::vec(proptest::sample::select(STEPS.to_vec()), 0..=96),
    ) {
        check_cancel_trace(&trace);
    }
}

#[test]
fn cancel_state_clear_keeps_old_receipt_and_repeated_resolution_stays_last_write() {
    let mut progress = CommandProgress::new(1.into());
    progress.resolve_accepted_dispatch();
    let mut old = match progress.request_cancellation() {
        CancellationDecision::Accepted(receipt) => receipt,
        _ => panic!("first request must be admitted"),
    };
    assert!(progress.try_claim_cancellation_dispatch());
    let rejection = Err(WaiterError::OperationNotCancellable(1.into()));
    progress.resolve_cancellation(rejection.clone());
    assert_eq!(*old.borrow_and_update(), Some(rejection.clone()));
    progress.clear_cancellation();
    assert!(old.has_changed().is_err());
    let mut current = match progress.request_cancellation() {
        CancellationDecision::Accepted(receipt) => receipt,
        _ => panic!("clear after rejection must allow a new request"),
    };
    let retry = match progress.request_cancellation() {
        CancellationDecision::AlreadyRequested(receipt) => receipt,
        _ => panic!("retry must share the active request"),
    };
    assert!(progress.try_claim_cancellation_dispatch());
    assert!(!progress.try_claim_cancellation_dispatch());
    for result in [Ok(()), rejection.clone(), Ok(())] {
        progress.resolve_cancellation(result.clone());
        assert_eq!(*current.borrow_and_update(), Some(result.clone()));
        assert_eq!(*retry.borrow(), Some(result.clone()));
        assert_eq!(progress.is_cancellation_accepted(), result.is_ok());
        assert_eq!(*old.borrow(), Some(rejection.clone()));
        assert!(!progress.try_claim_cancellation_dispatch());
    }
}

#[test]
fn cancel_state_local_resolution_before_rejection_does_not_dispatch_native_control() {
    let mut progress = CommandProgress::new(1.into());
    let completion = progress.completion_observer();
    let receipt = match progress.request_cancellation() {
        CancellationDecision::Accepted(receipt) => receipt,
        _ => panic!("pre-ack request must be admitted"),
    };
    assert!(!progress.try_claim_cancellation_dispatch());
    progress.resolve_cancellation(Ok(()));
    assert!(progress.is_cancellation_requested());
    assert!(!progress.is_awaiting_cancellation_result());
    assert_eq!(*receipt.borrow(), Some(Ok(())));
    let retry = match progress.request_cancellation() {
        CancellationDecision::AlreadyRequested(receipt) => receipt,
        _ => panic!("local resolution retains the shared receipt until retirement"),
    };
    let resolution = progress.resolve_rejected_dispatch(DispatchAction::NoAction, |_| {
        panic!("no callbacks were buffered")
    });
    assert!(resolution.events_to_replay.is_empty());
    assert!(!progress.try_claim_cancellation_dispatch());
    assert!(!progress.is_complete(true));
    assert!(!completion.is_completed());
    drop(progress);
    assert!(completion.is_completed());
    assert_eq!(*receipt.borrow(), Some(Ok(())));
    assert_eq!(*retry.borrow(), Some(Ok(())));
    assert!(receipt.has_changed().is_err());
}

#[test]
fn cancellation_receipt_gates_retirement_without_erasing_terminal_evidence() {
    use blehub::{BleErrorCategory, BleEventError};

    for terminal in [
        Ok(()),
        Err(BleEventError {
            category: BleErrorCategory::Cancelled,
            message: "native cancellation terminal".to_owned(),
            platform_details: None,
        }),
    ] {
        let mut progress = CommandProgress::new(1.into());
        progress.resolve_accepted_dispatch();
        let receipt = match progress.request_cancellation() {
            CancellationDecision::Accepted(receipt) => receipt,
            _ => panic!("cancellation must precede terminal"),
        };
        assert!(progress.try_claim_cancellation_dispatch());
        progress.record_terminal(&terminal);
        assert_eq!(
            progress.terminal_result(),
            Some(terminal.clone().map_err(WaiterError::Operation))
        );
        assert!(!progress.is_complete(true));
        assert!(matches!(receipt.has_changed(), Ok(false)));

        progress.resolve_cancellation(Ok(()));
        assert_eq!(*receipt.borrow(), Some(Ok(())));
        assert!(progress.is_complete(true));
        assert_eq!(
            progress.terminal_result(),
            Some(terminal.map_err(WaiterError::Operation))
        );
    }
}

#[test]
fn terminal_evidence_wins_over_a_first_cancellation_request() {
    let mut progress = CommandProgress::new(1.into());
    progress.resolve_accepted_dispatch();
    progress.record_terminal(&Ok(()));

    assert!(matches!(
        progress.request_cancellation(),
        CancellationDecision::AlreadyCompleted
    ));
}

#[test]
fn typed_boundary_duplicate_dispatch_ack_does_not_drain_pending_public_evidence() {
    let mut progress = CommandProgress::new(1.into());
    assert!(
        progress
            .resolve_accepted_dispatch()
            .events_to_replay
            .is_empty()
    );
    assert!(matches!(
        progress.request_cancellation(),
        CancellationDecision::Accepted(_)
    ));
    assert!(progress.try_claim_cancellation_dispatch());
    let event = blehub::BleEvent::CommandCompleted {
        command_id: 1.into(),
        error: None,
    };
    let mut publication = ScopedPublication::default();
    assert!(matches!(
        publication.decide(
            &event,
            PublicationGate {
                cancellation_accepted_for_event: false,
                cancellation_pending: true,
            },
        ),
        Ok(PublicationDecision::Defer)
    ));
    assert!(
        progress
            .resolve_accepted_dispatch()
            .events_to_replay
            .is_empty(),
        "a duplicate acknowledgement must not reprocess public-only evidence"
    );
    progress.resolve_cancellation(Err(WaiterError::OperationNotCancellable(1.into())));
    assert_eq!(
        publication.resolve(
            1.into(),
            PublicationRelease {
                cancellation_accepted: false,
                another_cancellation_pending: false,
            },
        ),
        vec![event]
    );
    assert!(
        publication
            .resolve(
                1.into(),
                PublicationRelease {
                    cancellation_accepted: false,
                    another_cancellation_pending: false,
                },
            )
            .is_empty()
    );
}

#[test]
fn accepted_cancelled_terminal_publishes_when_scope_has_no_other_pending_control() {
    let event = blehub::BleEvent::CommandCompleted {
        command_id: 1.into(),
        error: None,
    };
    let mut publication = ScopedPublication::default();

    assert!(matches!(
        publication.decide(
            &event,
            PublicationGate {
                cancellation_accepted_for_event: true,
                cancellation_pending: false,
            },
        ),
        Ok(PublicationDecision::Publish)
    ));
    assert!(matches!(
        publication.decide(
            &event,
            PublicationGate {
                cancellation_accepted_for_event: true,
                cancellation_pending: true,
            },
        ),
        Ok(PublicationDecision::Defer)
    ));
}
