//! The coexisting-lane set as a pure shape: every event reaches every lane, a lane nobody holds is
//! pruned instead of accumulating, and a poisoned set fails the lanes rather than dropping events.
//!
//! The defect this shape exists to prevent is behavioural, not structural, so it is pinned in two
//! places: here (the set reaches every lane) and in `tests/ffi_scan/notification_view.rs` (a second
//! subscription does not take over the first one's values).

use std::sync::atomic::{AtomicUsize, Ordering};

use super::{LaneSet, OwnerLane};
use blehub::BleEvent;

fn poll_once<F: std::future::Future>(future: std::pin::Pin<&mut F>) -> std::task::Poll<F::Output> {
    future.poll(&mut std::task::Context::from_waker(std::task::Waker::noop()))
}

#[derive(Default)]
struct CountingLane {
    offered: AtomicUsize,
    failed: AtomicUsize,
    closed: AtomicUsize,
}

impl OwnerLane for CountingLane {
    fn offer(&self, _event: &BleEvent) {
        self.offered.fetch_add(1, Ordering::SeqCst);
    }

    fn fail_unavailable(&self) {
        self.failed.fetch_add(1, Ordering::SeqCst);
    }

    fn close(&self) {
        self.closed.fetch_add(1, Ordering::SeqCst);
    }
}

fn event() -> BleEvent {
    BleEvent::CommandCompleted {
        command_id: 1.into(),
        error: None,
    }
}

fn offered(lane: &CountingLane) -> usize {
    lane.offered.load(Ordering::SeqCst)
}

#[test]
fn one_event_reaches_every_live_lane_and_never_only_the_newest() {
    let mut lanes = LaneSet::default();
    let first = std::sync::Arc::new(CountingLane::default());
    let second = std::sync::Arc::new(CountingLane::default());
    lanes.install(&first);
    lanes.install(&second);

    lanes.fan_out(&event(), false);

    // The earlier lane is not starved by the later one: this is the whole contract.
    assert_eq!(offered(&first), 1);
    assert_eq!(offered(&second), 1);
}

#[test]
fn a_lane_nobody_holds_is_pruned_instead_of_becoming_a_tombstone() {
    let mut lanes = LaneSet::default();
    let kept = std::sync::Arc::new(CountingLane::default());
    {
        let dropped = std::sync::Arc::new(CountingLane::default());
        lanes.install(&dropped);
        lanes.install(&kept);
    }
    lanes.fan_out(&event(), false);

    assert_eq!(offered(&kept), 1);
    assert_eq!(
        lanes.live.len(),
        1,
        "a released lane must not stay registered"
    );
}

#[test]
fn a_poisoned_set_fails_every_lane_and_offers_nothing() {
    let mut lanes = LaneSet::default();
    let first = std::sync::Arc::new(CountingLane::default());
    let second = std::sync::Arc::new(CountingLane::default());
    lanes.install(&first);
    lanes.install(&second);

    lanes.fan_out(&event(), true);

    // Bookkeeping loss is a terminal for the consumer, never a silent gap.
    assert_eq!(offered(&first), 0);
    assert_eq!(offered(&second), 0);
    assert_eq!(first.failed.load(Ordering::SeqCst), 1);
    assert_eq!(second.failed.load(Ordering::SeqCst), 1);
    assert!(lanes.live.is_empty());
}

#[test]
fn closing_the_owner_ends_every_lane_once() {
    let mut lanes = LaneSet::default();
    let first = std::sync::Arc::new(CountingLane::default());
    let second = std::sync::Arc::new(CountingLane::default());
    lanes.install(&first);
    lanes.install(&second);

    lanes.close_all();
    lanes.close_all();

    assert_eq!(first.closed.load(Ordering::SeqCst), 1);
    assert_eq!(second.closed.load(Ordering::SeqCst), 1);
}

/// The adapter lane is a coexisting reader of the owner's one stream: two observers each see every
/// snapshot, an advertisement is nobody's adapter event, and retirement ends both.
#[test]
fn adapter_lanes_each_receive_every_snapshot_and_ignore_other_events() {
    use crate::adapter_delivery::AdapterDelivery;

    let mut lanes: LaneSet<AdapterDelivery> = LaneSet::default();
    let first = std::sync::Arc::new(AdapterDelivery::new());
    let second = std::sync::Arc::new(AdapterDelivery::new());
    lanes.install(&first);
    lanes.install(&second);

    let snapshot = blehub::AdapterSnapshot {
        state: blehub::AdapterState::PoweredOff,
        scan_authorization: blehub::AuthorizationState::Authorized,
        connection_authorization: blehub::AuthorizationState::Authorized,
        name: None,
    };
    let changed = BleEvent::AdapterSnapshotChanged {
        command_id: None,
        snapshot,
    };
    lanes.fan_out(&changed, false);
    // A non-adapter event must not become an adapter observation in any lane.
    lanes.fan_out(&event(), false);

    for lane in [&first, &second] {
        assert!(matches!(
            next_ready(lane),
            Ok(snapshot) if snapshot.state == blehub::AdapterState::PoweredOff
        ));
        // The only other offered event was somebody else's, so this pull is still pending.
        let mut pending = std::pin::pin!(lane.next_event());
        assert_eq!(poll_once(pending.as_mut()), std::task::Poll::Pending);
    }

    lanes.close_all();
    for lane in [&first, &second] {
        assert_eq!(
            next_ready(lane),
            Err(crate::AdapterDeliveryError::OwnerRetired)
        );
    }
}

fn next_ready(
    delivery: &crate::adapter_delivery::AdapterDelivery,
) -> Result<blehub::AdapterSnapshot, crate::AdapterDeliveryError> {
    let mut next = std::pin::pin!(delivery.next_event());
    match poll_once(next.as_mut()) {
        std::task::Poll::Ready(result) => result,
        std::task::Poll::Pending => panic!("expected an available adapter snapshot or error"),
    }
}

/// Companion presence is another coexisting reader: two observers each see every transition, an
/// uncorrelated non-presence event is nobody's transition, and retirement ends both.
#[test]
fn presence_lanes_each_receive_every_transition_and_ignore_other_events() {
    use crate::companion_presence::CompanionPresenceDelivery;

    let mut lanes: LaneSet<CompanionPresenceDelivery> = LaneSet::default();
    let first = std::sync::Arc::new(CompanionPresenceDelivery::new());
    let second = std::sync::Arc::new(CompanionPresenceDelivery::new());
    lanes.install(&first);
    lanes.install(&second);

    let changed = BleEvent::CompanionPresenceChanged {
        present: true,
        key: "7".to_owned(),
        local_name: Some("peer".to_owned()),
    };
    lanes.fan_out(&changed, false);
    // Somebody else's event must not become a presence transition in any lane.
    lanes.fan_out(&event(), false);

    for lane in [&first, &second] {
        let presence = next_presence_ready(lane).expect("presence transition");
        assert!(presence.present);
        assert_eq!(presence.key, "7");
        assert_eq!(presence.local_name.as_deref(), Some("peer"));
        let mut pending = std::pin::pin!(lane.next_event());
        assert_eq!(poll_once(pending.as_mut()), std::task::Poll::Pending);
    }

    lanes.close_all();
    for lane in [&first, &second] {
        assert_eq!(
            next_presence_ready(lane),
            Err(crate::CompanionPresenceDeliveryError::OwnerRetired)
        );
    }
}

fn next_presence_ready(
    delivery: &crate::companion_presence::CompanionPresenceDelivery,
) -> Result<crate::CompanionPresence, crate::CompanionPresenceDeliveryError> {
    let mut next = std::pin::pin!(delivery.next_event());
    match poll_once(next.as_mut()) {
        std::task::Poll::Ready(result) => result,
        std::task::Poll::Pending => panic!("expected an available presence transition or error"),
    }
}

/// Registration and revocation are one decision: an owner that has retired never hands out a lane
/// that could observe a generation it no longer owns.
#[test]
fn adapter_observation_registration_and_revocation_share_one_decision() {
    use crate::{
        AdapterDeliveryError, AdapterSubmissionError, OwnerBackendIdentity, OwnerGeneration,
        RecoveryAttemptId,
    };

    fn context() -> super::RecoveryContext {
        super::RecoveryContext::new(
            OwnerBackendIdentity::new("adapter-owner", "adapter-backend").unwrap(),
            OwnerGeneration::new(1).unwrap(),
            RecoveryAttemptId::new(1).unwrap(),
        )
    }

    let lease = super::ObservationProjectionLease::new(context());
    let projection = lease.projection();
    let lane = projection.observe_adapter().unwrap();

    // A live owner hands out lanes...
    lanes_see_everything(&projection, &lane);

    // ...and retirement ends the lane it already handed out, then refuses new ones.
    drop(lease);
    assert_eq!(next_ready(&lane), Err(AdapterDeliveryError::OwnerRetired));
    assert!(matches!(
        projection.observe_adapter(),
        Err(AdapterSubmissionError::OwnerRetired)
    ));
}

fn lanes_see_everything(
    projection: &super::SessionObservationProjection,
    lane: &crate::adapter_delivery::AdapterDelivery,
) {
    let snapshot = blehub::AdapterSnapshot {
        state: blehub::AdapterState::PoweredOn,
        scan_authorization: blehub::AuthorizationState::Authorized,
        connection_authorization: blehub::AuthorizationState::Authorized,
        name: Some("adapter".to_owned()),
    };
    projection.project(BleEvent::AdapterSnapshotChanged {
        command_id: None,
        snapshot: snapshot.clone(),
    });
    assert_eq!(next_ready(lane), Ok(snapshot));
}
