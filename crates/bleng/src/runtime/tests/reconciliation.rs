use blehub::{BleEvent, CommandDispatchError, NotificationModeRequest};

use crate::runtime::reservations::{
    ConnectionReservations, DispatchEffect, InitializationState, ReconcileFault,
};

use super::support::{
    WaiterError, WaiterSuccess, connected_event, disconnected_event, init_snapshot,
};

#[test]
fn pre_ack_buffer_is_bounded_and_replays_correlated_evidence_in_order() {
    let mut initialization = InitializationState::default();
    initialization.reserve_initialize(1.into());
    let events: Vec<_> = (0..64)
        .map(|index| {
            let mut snapshot = init_snapshot();
            snapshot.name = Some(index.to_string());
            BleEvent::AdapterSnapshotChanged {
                command_id: Some(1.into()),
                snapshot,
            }
        })
        .collect();
    for event in &events {
        assert_eq!(
            initialization
                .buffer_before_dispatch(event.clone())
                .unwrap(),
            None
        );
    }
    let foreign = BleEvent::AdapterSnapshotChanged {
        command_id: Some(99.into()),
        snapshot: init_snapshot(),
    };
    assert_eq!(
        initialization
            .buffer_before_dispatch(foreign.clone())
            .unwrap(),
        Some(foreign)
    );
    assert!(matches!(
        initialization.buffer_before_dispatch(events[0].clone()),
        Err(ReconcileFault::BufferOverflow)
    ));

    let resolution = initialization.resolve_dispatch(1.into(), &Ok(())).unwrap();
    assert!(matches!(resolution.effect, DispatchEffect::None));
    assert_eq!(resolution.replay, events);
    for event in &resolution.replay {
        initialization.record_event(event).unwrap();
    }
    assert_eq!(initialization.waiter_result(1.into()), None);
    assert!(initialization.record_terminal(1.into(), &Ok(())));
    let mut expected_snapshot = init_snapshot();
    expected_snapshot.name = Some("0".to_owned());
    assert_eq!(
        initialization.waiter_result(1.into()),
        Some(Ok(WaiterSuccess::Initialized(expected_snapshot))),
        "replay preserves the first correlated snapshot, not the latest duplicate"
    );
}

#[test]
fn rejection_replay_preserves_distinct_io_and_subscription_loss_policies() {
    for subscription in [false, true] {
        let mut connections = ConnectionReservations::default();
        connections.reserve_connection(1.into(), 1.into(), 10.into());
        connections.resolve_dispatch(1.into(), &Ok(())).unwrap();
        connections.record_event(&connected_event(1, 10)).unwrap();
        connections.record_terminal(1.into(), &Ok(()));
        if subscription {
            connections.reserve_notification_enable(
                1.into(),
                2.into(),
                40.into(),
                20.into(),
                30.into(),
                NotificationModeRequest::PlatformDefault,
            );
        } else {
            connections.reserve_read(1.into(), 2.into(), 20.into(), 30.into());
        }

        // A commanded loss belonging to the connect lifecycle is not foreign,
        // but only subscription rejection permits replaying this kind of loss.
        let loss = disconnected_event(Some(1), 10);
        assert!(!connections.is_foreign_disconnect(&loss));
        assert_eq!(
            connections.buffer_before_dispatch(loss.clone()).unwrap(),
            None
        );
        let rejection = Err(CommandDispatchError::InvalidState {
            detail: "rejected after native connection loss".to_owned(),
        });
        let resolution = connections.resolve_dispatch(2.into(), &rejection).unwrap();
        assert_eq!(
            connections.waiter_result(1.into()),
            Some(Ok(WaiterSuccess::Connected(10.into())))
        );
        assert_eq!(connections.validate_gatt_discovery(10.into()), Ok(1.into()));
        assert!(connections.resolve_dispatch(2.into(), &Ok(())).is_none());
        if subscription {
            assert!(matches!(resolution.effect, DispatchEffect::None));
            assert_eq!(resolution.replay, vec![loss.clone()]);
            assert_eq!(
                connections.validate_notification_disable(10.into(), 20.into(), 40.into()),
                Err(WaiterError::SubscriptionNotActive)
            );
            // The actor, not resolve_dispatch(), decides when to record replay.
            connections.record_event(&loss).unwrap();
            connections.retire_completed();
            assert_eq!(connections.require_peripheral_available(1.into()), Ok(()));
            assert_eq!(
                connections.validate_rssi(10.into()),
                Err(WaiterError::ConnectionNotActive)
            );
        } else {
            assert!(matches!(
                resolution.effect,
                DispatchEffect::Fault(ReconcileFault::ContradictoryDispatch)
            ));
            assert!(resolution.replay.is_empty());
            assert_eq!(connections.validate_gatt_discovery(10.into()), Ok(1.into()));
        }
    }
}
