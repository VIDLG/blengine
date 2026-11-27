//! The connection-end lane as a pure state machine: which event may bind it, which may end it, and
//! what a second consumer sees.
//!
//! The end-to-end shape (a real connect, a real platform event, a real revocation) is covered by
//! `tests/ffi_scan/connection_end.rs`; these cases pin the semantics that shape cannot reach
//! cheaply — attribution after the fact, a refused second pull, and a retained terminal.

use std::{
    future::Future,
    pin::{Pin, pin},
    task::{Context, Poll, Waker},
};

use blehub::{BleErrorCategory, BleEvent, BleEventError, ConnectionState, ConnectionStateResult};

use super::{ConnectionEnd, ConnectionEndBy, ConnectionLane, ConnectionLaneError};
use crate::recovery::{OwnerBackendIdentity, OwnerGeneration, RecoveryAttemptId, RecoveryContext};

const CONNECT: u64 = 3;
const CONNECTION: u64 = 2;

fn context() -> RecoveryContext {
    RecoveryContext::new(
        OwnerBackendIdentity::new("connection-owner", "connection-backend").unwrap(),
        OwnerGeneration::new(1).unwrap(),
        RecoveryAttemptId::new(1).unwrap(),
    )
}

fn lane() -> ConnectionLane {
    ConnectionLane::new(context(), CONNECT.into())
}

fn state(command: Option<u64>, connection: u64, state: ConnectionState) -> BleEvent {
    BleEvent::ConnectionStateChanged {
        command_id: command.map(Into::into),
        connection_id: connection.into(),
        result: ConnectionStateResult::Ok { state },
    }
}

fn error(command: Option<u64>, connection: u64) -> BleEvent {
    BleEvent::ConnectionStateChanged {
        command_id: command.map(Into::into),
        connection_id: connection.into(),
        result: ConnectionStateResult::Err {
            error: BleEventError {
                category: BleErrorCategory::Connection,
                message: "link lost".to_owned(),
                platform_details: None,
            },
        },
    }
}

fn connected(command: Option<u64>, connection: u64) -> BleEvent {
    state(command, connection, ConnectionState::Connected)
}

fn disconnected(command: Option<u64>, connection: u64) -> BleEvent {
    state(command, connection, ConnectionState::Disconnected)
}

fn poll_once<F: Future>(future: Pin<&mut F>) -> Poll<F::Output> {
    future.poll(&mut Context::from_waker(Waker::noop()))
}

fn next_ready(lane: &ConnectionLane) -> Result<ConnectionEnd, ConnectionLaneError> {
    let mut next = pin!(lane.next_end());
    match poll_once(next.as_mut()) {
        Poll::Ready(result) => result,
        Poll::Pending => panic!("expected an immediately available end or error"),
    }
}

fn is_pending(lane: &ConnectionLane) -> bool {
    let mut next = pin!(lane.next_end());
    matches!(poll_once(next.as_mut()), Poll::Pending)
}

#[test]
fn is_connected_tracks_the_bound_link_and_its_terminal() {
    let lane = lane();
    assert!(!lane.is_connected(), "a fresh lane has no link yet");
    lane.offer(&state(
        Some(CONNECT),
        CONNECTION,
        ConnectionState::Connecting,
    ));
    assert!(!lane.is_connected(), "a connect in progress is not a link");
    lane.offer(&connected(Some(CONNECT), CONNECTION));
    assert!(lane.is_connected());
    lane.offer(&disconnected(Some(CONNECT), CONNECTION));
    assert!(!lane.is_connected(), "the first terminal ends the lane");
}

#[test]
fn a_lane_binds_only_to_its_own_connect_completion() {
    let lane = lane();
    // Another command's connected state, another connection's, and an unsolicited one are all
    // ignored: this lane's link is exactly the connection its own connect command produced.
    lane.offer(&connected(Some(CONNECT + 1), CONNECTION));
    lane.offer(&connected(None, CONNECTION));
    assert!(is_pending(&lane), "an unbound lane cannot report an end");

    lane.offer(&connected(Some(CONNECT), CONNECTION));
    lane.offer(&disconnected(None, CONNECTION + 1));
    assert!(
        is_pending(&lane),
        "another connection's end is not this lane's end"
    );

    lane.offer(&disconnected(None, CONNECTION));
    assert_eq!(
        next_ready(&lane).expect("retained end").by,
        ConnectionEndBy::Unrequested
    );
}

#[test]
fn the_first_terminal_wins_over_a_later_operator_request() {
    let lane = lane();
    lane.offer(&connected(Some(CONNECT), CONNECTION));
    lane.offer(&error(None, CONNECTION));
    // A disconnect submitted after the link already ended cannot re-label that end.
    lane.expect_operator_disconnect();
    lane.offer(&disconnected(Some(CONNECT + 1), CONNECTION));

    let end = next_ready(&lane).expect("retained end");
    assert_eq!(end.by, ConnectionEndBy::Unrequested);
    assert_eq!(
        end.error.as_ref().expect("reported error").message,
        "link lost"
    );
    // Retained, not consumed.
    assert_eq!(next_ready(&lane).expect("retained end"), end);
}

#[test]
fn a_submitted_disconnect_attributes_the_terminal_to_the_operator() {
    let lane = lane();
    lane.offer(&connected(Some(CONNECT), CONNECTION));
    lane.expect_operator_disconnect();
    lane.offer(&disconnected(Some(CONNECT + 4), CONNECTION));
    let end = next_ready(&lane).expect("retained end");
    assert_eq!(end.by, ConnectionEndBy::Operator);
    assert!(end.error.is_none());
}

#[test]
fn a_second_pending_pull_is_refused_and_the_first_keeps_its_claim() {
    let lane = lane();
    lane.offer(&connected(Some(CONNECT), CONNECTION));
    let mut first = pin!(lane.next_end());
    assert!(matches!(poll_once(first.as_mut()), Poll::Pending));
    assert_eq!(next_ready(&lane), Err(ConnectionLaneError::Pending));

    lane.offer(&disconnected(None, CONNECTION));
    match poll_once(first.as_mut()) {
        Poll::Ready(Ok(end)) => assert_eq!(end.by, ConnectionEndBy::Unrequested),
        other => panic!("the first pull must still own the terminal: {other:?}"),
    }
}

#[test]
fn owner_retirement_is_an_observation_end_not_a_link_fact() {
    let lane = lane();
    lane.offer(&connected(Some(CONNECT), CONNECTION));
    lane.close();
    let end = next_ready(&lane).expect("retained end");
    assert_eq!(end.by, ConnectionEndBy::Owner);
    assert!(end.error.is_none());
    // A later platform event cannot replace it.
    lane.offer(&disconnected(None, CONNECTION));
    assert_eq!(
        next_ready(&lane).expect("retained end").by,
        ConnectionEndBy::Owner
    );
}

#[test]
fn unavailable_bookkeeping_is_a_terminal_failure() {
    let lane = lane();
    lane.fail_unavailable();
    assert_eq!(next_ready(&lane), Err(ConnectionLaneError::Unavailable));
    lane.close();
    assert_eq!(next_ready(&lane), Err(ConnectionLaneError::Unavailable));
}

#[test]
fn progress_states_never_end_a_linked_lane() {
    let lane = lane();
    lane.offer(&connected(Some(CONNECT), CONNECTION));
    lane.offer(&state(None, CONNECTION, ConnectionState::Connected));
    lane.offer(&state(None, CONNECTION, ConnectionState::Connecting));
    lane.offer(&state(None, CONNECTION, ConnectionState::Disconnecting));
    assert!(is_pending(&lane));
}
