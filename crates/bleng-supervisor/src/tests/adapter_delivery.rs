//! The adapter lane as a shape: ordered delivery, one pending pull at a time, fail-closed overflow,
//! and an end that is a retained fact rather than a synthesized failure.
//!
//! Everything here is pure: no runtime, no timer, no radio. The lane only stores what a consumer has
//! not pulled, so these tests pin the storage rules and nothing about BLE.

use std::{
    future::Future,
    pin::{Pin, pin},
    task::{Context, Poll, Waker},
};

use super::{AdapterDelivery, AdapterDeliveryError, MAX_PENDING_SNAPSHOTS};
use blehub::{AdapterSnapshot, AdapterState, AuthorizationState};

fn snapshot(state: AdapterState) -> AdapterSnapshot {
    AdapterSnapshot {
        state,
        scan_authorization: AuthorizationState::Authorized,
        connection_authorization: AuthorizationState::Authorized,
        name: None,
    }
}

fn poll_once<F: Future>(future: Pin<&mut F>) -> Poll<F::Output> {
    future.poll(&mut Context::from_waker(Waker::noop()))
}

fn next_ready(delivery: &AdapterDelivery) -> Result<AdapterSnapshot, AdapterDeliveryError> {
    let mut next = pin!(delivery.next_event());
    match poll_once(next.as_mut()) {
        Poll::Ready(result) => result,
        Poll::Pending => panic!("expected an immediately available adapter snapshot or error"),
    }
}

#[test]
fn snapshots_are_delivered_in_the_order_they_were_offered() {
    let delivery = AdapterDelivery::new();
    delivery.offer(&snapshot(AdapterState::PoweredOff));
    delivery.offer(&snapshot(AdapterState::PoweredOn));

    assert_eq!(
        next_ready(&delivery),
        Ok(snapshot(AdapterState::PoweredOff))
    );
    assert_eq!(next_ready(&delivery), Ok(snapshot(AdapterState::PoweredOn)));
}

#[test]
fn a_second_pending_pull_is_a_contract_violation_not_a_queue() {
    let delivery = AdapterDelivery::new();
    {
        let mut first = pin!(delivery.next_event());
        assert_eq!(poll_once(first.as_mut()), Poll::Pending);
        {
            let mut second = pin!(delivery.next_event());
            assert_eq!(
                poll_once(second.as_mut()),
                Poll::Ready(Err(AdapterDeliveryError::PullBusy))
            );
        }
        // Dropping the second pull does not release the first one's slot.
        assert_eq!(poll_once(first.as_mut()), Poll::Pending);
    }
    // Dropping the first pull releases the slot: a cancelled pull is not a wedged stream.
    {
        let mut replacement = pin!(delivery.next_event());
        assert_eq!(poll_once(replacement.as_mut()), Poll::Pending);
    }
    delivery.offer(&snapshot(AdapterState::PoweredOn));
    assert_eq!(next_ready(&delivery), Ok(snapshot(AdapterState::PoweredOn)));
}

#[test]
fn overflow_ends_observation_instead_of_dropping_a_snapshot_silently() {
    let delivery = AdapterDelivery::new();
    for _ in 0..MAX_PENDING_SNAPSHOTS {
        delivery.offer(&snapshot(AdapterState::PoweredOn));
    }
    delivery.offer(&snapshot(AdapterState::PoweredOff));

    // The lane ends rather than growing without bound. Undelivered snapshots go with it, because a
    // consumer that fell this far behind asked for a state it already missed — and the loss is
    // reported, never hidden.
    assert_eq!(next_ready(&delivery), Err(AdapterDeliveryError::Overflow));
    // The end of observation is retained: polling again reports it again.
    assert_eq!(next_ready(&delivery), Err(AdapterDeliveryError::Overflow));
}

#[test]
fn retirement_ends_observation_and_the_end_is_retained() {
    let delivery = AdapterDelivery::new();
    delivery.offer(&snapshot(AdapterState::PoweredOff));
    delivery.close();

    // Retirement discards what was not read: an unread snapshot describes the generation that just
    // retired, and the replacement owner reports its own state through `start()`.
    assert_eq!(
        next_ready(&delivery),
        Err(AdapterDeliveryError::OwnerRetired)
    );
    // ...and the end of observation stays readable.
    assert_eq!(
        next_ready(&delivery),
        Err(AdapterDeliveryError::OwnerRetired)
    );
}

#[test]
fn the_first_failure_wins_over_a_later_close_or_detach() {
    let delivery = AdapterDelivery::new();
    delivery.offer(&snapshot(AdapterState::PoweredOn));
    for _ in 0..MAX_PENDING_SNAPSHOTS {
        delivery.offer(&snapshot(AdapterState::PoweredOn));
    }
    assert_eq!(next_ready(&delivery), Err(AdapterDeliveryError::Overflow));
    delivery.close();
    delivery.detach();
    // A later close must never make an earlier delivery failure look like a clean retirement.
    assert_eq!(next_ready(&delivery), Err(AdapterDeliveryError::Overflow));
}

#[test]
fn detach_ends_observation_and_stops_storing_new_snapshots() {
    let delivery = AdapterDelivery::new();
    delivery.offer(&snapshot(AdapterState::PoweredOff));
    delivery.detach();
    delivery.offer(&snapshot(AdapterState::PoweredOn));

    assert_eq!(next_ready(&delivery), Err(AdapterDeliveryError::Detached));
    assert_eq!(next_ready(&delivery), Err(AdapterDeliveryError::Detached));
}
