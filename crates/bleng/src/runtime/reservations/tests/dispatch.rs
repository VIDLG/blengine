use super::super::dispatch::{
    CancellationDecision, CancellationReceiver, CommandProgress, DispatchAction, ScopedPublication,
};
use crate::waiter::WaiterError;
use blehub::{BleErrorCategory, BleEventError};

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
                assert!(progress.accepted_resolution().events_to_replay.is_empty());
            }
            Step::Claim => {
                let expected =
                    acknowledged && active.is_some_and(|attempt| !attempts[attempt].claimed);
                assert_eq!(
                    progress.take_cancellation_dispatch(),
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
            progress.cancellation_requested(),
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
            progress.cancellation_accepted(),
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
    progress.accepted_resolution();
    let mut old = match progress.request_cancellation() {
        CancellationDecision::Accepted(receipt) => receipt,
        _ => panic!("first request must be admitted"),
    };
    assert!(progress.take_cancellation_dispatch());
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
    assert!(progress.take_cancellation_dispatch());
    assert!(!progress.take_cancellation_dispatch());
    for result in [Ok(()), rejection.clone(), Ok(())] {
        progress.resolve_cancellation(result.clone());
        assert_eq!(*current.borrow_and_update(), Some(result.clone()));
        assert_eq!(*retry.borrow(), Some(result.clone()));
        assert_eq!(progress.cancellation_accepted(), result.is_ok());
        assert_eq!(*old.borrow(), Some(rejection.clone()));
        assert!(!progress.take_cancellation_dispatch());
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
    assert!(!progress.take_cancellation_dispatch());
    progress.resolve_cancellation(Ok(()));
    assert!(progress.cancellation_requested());
    assert!(!progress.is_awaiting_cancellation_result());
    assert_eq!(*receipt.borrow(), Some(Ok(())));
    let retry = match progress.request_cancellation() {
        CancellationDecision::AlreadyRequested(receipt) => receipt,
        _ => panic!("local resolution retains the shared receipt until retirement"),
    };
    let resolution = progress.rejected_resolution(DispatchAction::None, |_| {
        panic!("no callbacks were buffered")
    });
    assert!(resolution.events_to_replay.is_empty());
    assert!(!progress.take_cancellation_dispatch());
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
        progress.accepted_resolution();
        let receipt = match progress.request_cancellation() {
            CancellationDecision::Accepted(receipt) => receipt,
            _ => panic!("cancellation must precede terminal"),
        };
        assert!(progress.take_cancellation_dispatch());
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
    progress.accepted_resolution();
    progress.record_terminal(&Ok(()));

    assert!(matches!(
        progress.request_cancellation(),
        CancellationDecision::AlreadyCompleted
    ));
}

#[test]
fn typed_boundary_duplicate_dispatch_ack_does_not_drain_pending_public_evidence() {
    let mut progress = CommandProgress::new(1.into());
    assert!(progress.accepted_resolution().events_to_replay.is_empty());
    assert!(matches!(
        progress.request_cancellation(),
        CancellationDecision::Accepted(_)
    ));
    assert!(progress.take_cancellation_dispatch());
    let event = blehub::BleEvent::CommandCompleted {
        command_id: 1.into(),
        error: None,
    };
    let mut publication = ScopedPublication::default();
    assert!(!publication.allow(&event, true, false).unwrap());
    assert!(
        progress.accepted_resolution().events_to_replay.is_empty(),
        "a duplicate acknowledgement must not reprocess public-only evidence"
    );
    progress.resolve_cancellation(Err(WaiterError::OperationNotCancellable(1.into())));
    assert_eq!(publication.resolve(1.into(), false, false), vec![event]);
    assert!(publication.resolve(1.into(), false, false).is_empty());
}
