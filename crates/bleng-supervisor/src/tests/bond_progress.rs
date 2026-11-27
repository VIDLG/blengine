//! The bond-progress lane as a pure state machine: which event may advance it, what a second
//! consumer sees, and how it ends.
//!
//! The point of this lane is to keep `Bonding` an observation: the terminal receipt stays on
//! `BondOperation::wait`, while progress is a bounded, retained end that can be read repeatedly.

use std::future::Future;
use std::pin::Pin;
use std::task::{Context, Poll, Waker};

use blehub::{
    BleErrorCategory, BleEvent, BleEventError, BondState, ConnectionState, ConnectionStateResult,
};

use super::{BondProgressDelivery, BondProgressEnd, BondProgressStep};
use crate::recovery::{OwnerBackendIdentity, OwnerGeneration, RecoveryAttemptId, RecoveryContext};

const COMMAND: u64 = 3;
const CONNECTION: u64 = 2;

fn context() -> RecoveryContext {
    RecoveryContext::new(
        OwnerBackendIdentity::new("bond-owner", "bond-backend").unwrap(),
        OwnerGeneration::new(1).unwrap(),
        RecoveryAttemptId::new(1).unwrap(),
    )
}

fn lane() -> BondProgressDelivery {
    BondProgressDelivery::new(
        context(),
        COMMAND.into(),
        CONNECTION.into(),
        BondState::Bonded,
    )
}

fn bond(command: Option<u64>, connection: u64, state: BondState) -> BleEvent {
    BleEvent::BondStateChanged {
        command_id: command.map(Into::into),
        connection_id: connection.into(),
        state,
    }
}

fn failure() -> BleEventError {
    BleEventError {
        category: BleErrorCategory::Gatt,
        message: "pairing failed".to_owned(),
        platform_details: None,
    }
}

fn next(lane: &BondProgressDelivery) -> BondProgressStep {
    let future = lane.next_state();
    let mut future = std::pin::pin!(future);
    match future
        .as_mut()
        .poll(&mut Context::from_waker(Waker::noop()))
    {
        Poll::Ready(Ok(step)) => step,
        other => panic!("expected one ready progress step, got {other:?}"),
    }
}

fn poll_once<F: Future>(future: Pin<&mut F>) -> Poll<F::Output> {
    future.poll(&mut Context::from_waker(Waker::noop()))
}

#[test]
fn bonding_then_terminal_is_delivered_in_order_and_ends() {
    let lane = lane();
    lane.offer(&bond(Some(COMMAND), CONNECTION, BondState::Bonding));
    assert_eq!(
        next(&lane),
        BondProgressStep::State {
            state: BondState::Bonding,
            ended: false,
        }
    );

    lane.offer(&bond(Some(COMMAND), CONNECTION, BondState::Bonded));
    assert_eq!(
        next(&lane),
        BondProgressStep::State {
            state: BondState::Bonded,
            ended: true,
        }
    );
    // The end is retained, so a late or second reader still learns the lane finished.
    assert_eq!(
        next(&lane),
        BondProgressStep::End(BondProgressEnd::Terminal)
    );
}

#[test]
fn another_command_or_connection_cannot_advance_this_lane() {
    let lane = lane();
    lane.offer(&bond(Some(COMMAND + 1), CONNECTION, BondState::Bonding));
    lane.offer(&bond(Some(COMMAND), CONNECTION + 1, BondState::Bonding));
    lane.offer(&bond(None, CONNECTION, BondState::Bonding));

    // Nothing above matched, so the lane is still empty. Its own state arrives first.
    lane.offer(&bond(Some(COMMAND), CONNECTION, BondState::Bonding));
    assert_eq!(
        next(&lane),
        BondProgressStep::State {
            state: BondState::Bonding,
            ended: false,
        }
    );
}

#[test]
fn overflow_ends_the_lane_instead_of_dropping_progress() {
    let lane = lane();
    for _ in 0..=super::MAX_BOND_STATES {
        lane.offer(&bond(Some(COMMAND), CONNECTION, BondState::Bonding));
    }
    // Overflow clears the buffered states and retains the end; no partial stream is delivered.
    assert_eq!(
        next(&lane),
        BondProgressStep::End(BondProgressEnd::Overflow)
    );
}

#[test]
fn a_terminal_failure_is_retained_instead_of_becoming_progress() {
    let lane = lane();
    lane.offer(&BleEvent::CommandCompleted {
        command_id: COMMAND.into(),
        error: Some(failure()),
    });

    assert_eq!(next(&lane), BondProgressStep::Failed(failure()));
    assert_eq!(next(&lane), BondProgressStep::Failed(failure()));
}

#[test]
fn connection_loss_ends_a_lane_that_never_reached_its_terminal() {
    let lane = lane();
    lane.offer(&bond(Some(COMMAND), CONNECTION, BondState::Bonding));
    lane.offer(&BleEvent::ConnectionStateChanged {
        command_id: None,
        connection_id: CONNECTION.into(),
        result: ConnectionStateResult::Ok {
            state: ConnectionState::Disconnected,
        },
    });

    assert_eq!(
        next(&lane),
        BondProgressStep::State {
            state: BondState::Bonding,
            ended: false,
        }
    );
    assert_eq!(
        next(&lane),
        BondProgressStep::End(BondProgressEnd::ConnectionLost)
    );
}

#[test]
fn owner_retirement_ends_the_lane() {
    let lane = lane();
    lane.close();
    assert_eq!(
        next(&lane),
        BondProgressStep::End(BondProgressEnd::OwnerRetired)
    );
}

#[test]
fn a_second_concurrent_pull_is_refused() {
    let lane = lane();
    let first = lane.next_state();
    let mut first = std::pin::pin!(first);
    // The first pull is pending (no event yet) and holds the lane's single pull slot.
    assert!(poll_once(first.as_mut()).is_pending());

    let second = lane.next_state();
    let mut second = std::pin::pin!(second);
    assert!(matches!(
        poll_once(second.as_mut()),
        Poll::Ready(Err(super::BondProgressError::Pending))
    ));
}
