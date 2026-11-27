//! The companion-presence lane as a shape: ordered delivery, one pending pull at a time,
//! fail-closed overflow, and an end that is a retained fact rather than a synthesized failure.
//!
//! Everything here is pure: no runtime, no timer, no radio, no association. The lane only stores
//! what a consumer has not pulled, so these tests pin the storage rules and nothing about BLE.

use std::{
    future::Future,
    pin::{Pin, pin},
    task::{Context, Poll, Waker},
};

use super::{
    CompanionPresence, CompanionPresenceDelivery, CompanionPresenceDeliveryError,
    MAX_PENDING_PRESENCE,
};

fn presence(present: bool, key: &str) -> CompanionPresence {
    CompanionPresence {
        present,
        key: key.to_owned(),
        local_name: None,
    }
}

fn poll_once<F: Future>(future: Pin<&mut F>) -> Poll<F::Output> {
    future.poll(&mut Context::from_waker(Waker::noop()))
}

fn next_ready(
    delivery: &CompanionPresenceDelivery,
) -> Result<CompanionPresence, CompanionPresenceDeliveryError> {
    let mut next = pin!(delivery.next_event());
    match poll_once(next.as_mut()) {
        Poll::Ready(result) => result,
        Poll::Pending => panic!("expected an immediately available presence transition or error"),
    }
}

#[test]
fn transitions_are_delivered_in_the_order_they_were_offered() {
    let delivery = CompanionPresenceDelivery::new();
    delivery.offer(presence(true, "1"));
    delivery.offer(presence(false, "1"));

    assert_eq!(next_ready(&delivery), Ok(presence(true, "1")));
    assert_eq!(next_ready(&delivery), Ok(presence(false, "1")));
}

#[test]
fn a_second_pending_pull_is_a_contract_violation_not_a_queue() {
    let delivery = CompanionPresenceDelivery::new();
    {
        let mut first = pin!(delivery.next_event());
        assert_eq!(poll_once(first.as_mut()), Poll::Pending);
        {
            let mut second = pin!(delivery.next_event());
            assert_eq!(
                poll_once(second.as_mut()),
                Poll::Ready(Err(CompanionPresenceDeliveryError::PullBusy))
            );
        }
        assert_eq!(poll_once(first.as_mut()), Poll::Pending);
    }
    {
        let mut replacement = pin!(delivery.next_event());
        assert_eq!(poll_once(replacement.as_mut()), Poll::Pending);
    }
    delivery.offer(presence(true, "2"));
    assert_eq!(next_ready(&delivery), Ok(presence(true, "2")));
}

#[test]
fn overflow_ends_observation_instead_of_dropping_a_transition_silently() {
    let delivery = CompanionPresenceDelivery::new();
    for _ in 0..MAX_PENDING_PRESENCE {
        delivery.offer(presence(true, "1"));
    }
    delivery.offer(presence(false, "1"));

    assert_eq!(
        next_ready(&delivery),
        Err(CompanionPresenceDeliveryError::Overflow)
    );
    assert_eq!(
        next_ready(&delivery),
        Err(CompanionPresenceDeliveryError::Overflow)
    );
}

#[test]
fn retirement_ends_observation_and_the_end_is_retained() {
    let delivery = CompanionPresenceDelivery::new();
    delivery.offer(presence(true, "1"));
    delivery.close();

    assert_eq!(
        next_ready(&delivery),
        Err(CompanionPresenceDeliveryError::OwnerRetired)
    );
    assert_eq!(
        next_ready(&delivery),
        Err(CompanionPresenceDeliveryError::OwnerRetired)
    );
}

#[test]
fn the_first_failure_wins_over_a_later_close_or_detach() {
    let delivery = CompanionPresenceDelivery::new();
    for _ in 0..=MAX_PENDING_PRESENCE {
        delivery.offer(presence(true, "1"));
    }
    assert_eq!(
        next_ready(&delivery),
        Err(CompanionPresenceDeliveryError::Overflow)
    );
    delivery.close();
    delivery.detach();
    assert_eq!(
        next_ready(&delivery),
        Err(CompanionPresenceDeliveryError::Overflow)
    );
}

#[test]
fn detach_ends_observation_and_stops_storing_new_transitions() {
    let delivery = CompanionPresenceDelivery::new();
    delivery.offer(presence(true, "1"));
    delivery.detach();
    delivery.offer(presence(false, "1"));

    assert_eq!(
        next_ready(&delivery),
        Err(CompanionPresenceDeliveryError::Detached)
    );
    assert_eq!(
        next_ready(&delivery),
        Err(CompanionPresenceDeliveryError::Detached)
    );
}

#[test]
fn a_poisoned_lane_reports_unavailable_rather_than_hanging() {
    let delivery = CompanionPresenceDelivery::new();
    delivery.offer(presence(true, "1"));
    // Poison the lock from a panicking holder, then observe the fail-closed path.
    let _ = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        let _guard = delivery.state.lock().unwrap();
        panic!("poison the presence lane");
    }));
    assert_eq!(
        next_ready(&delivery),
        Err(CompanionPresenceDeliveryError::Unavailable)
    );
}
