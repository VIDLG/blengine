use blehub::{
    BleErrorCategory, BleEvent, BleEventError, CommandId, ConnectionId, GattCharacteristicId,
    GattSessionId, NotificationModeRequest, NotificationState, SubscriptionId,
};

use crate::waiter::{WaiterError, WaiterSuccess};

use super::{SubscriptionDisposition, SubscriptionReservation};

const CONNECTION: ConnectionId = ConnectionId { value: 1 };
const SESSION: GattSessionId = GattSessionId { value: 2 };
const CHARACTERISTIC: GattCharacteristicId = GattCharacteristicId { value: 3 };
const SUBSCRIPTION: SubscriptionId = SubscriptionId { value: 4 };

fn enabling() -> SubscriptionReservation {
    SubscriptionReservation::enabling(
        5.into(),
        SESSION,
        CHARACTERISTIC,
        NotificationModeRequest::PlatformDefault,
    )
}

fn record_domain(
    route: &mut SubscriptionReservation,
    command_id: CommandId,
    state: NotificationState,
) {
    route
        .record_event(
            &BleEvent::NotificationStateChanged {
                command_id,
                connection_id: CONNECTION,
                gatt_session_id: SESSION,
                subscription_id: SUBSCRIPTION,
                characteristic_id: CHARACTERISTIC,
                state,
            },
            CONNECTION,
            SUBSCRIPTION,
        )
        .unwrap();
}

#[test]
fn successful_transitions_require_ack_terminal_and_matching_domain() {
    let mut route = enabling();
    let enabled = NotificationState::Enabled { actual_mode: None };
    record_domain(&mut route, 5.into(), NotificationState::Disabled);
    route.progress_mut().unwrap().record_terminal(&Ok(()));
    assert_eq!(
        route.waiter_result(5.into(), CONNECTION, SUBSCRIPTION),
        None
    );
    assert!(matches!(
        route.retire_transition(),
        SubscriptionDisposition::Keep
    ));
    assert!(!route.is_active());
    record_domain(&mut route, 5.into(), enabled);
    assert_eq!(
        route.waiter_result(5.into(), CONNECTION, SUBSCRIPTION),
        None
    );
    assert!(matches!(
        route.retire_transition(),
        SubscriptionDisposition::Keep
    ));
    assert!(!route.is_active());
    route.progress_mut().unwrap().accepted_resolution();
    assert!(!route.progress().unwrap().is_awaiting_dispatch_result());
    assert_eq!(
        route.waiter_result(6.into(), CONNECTION, SUBSCRIPTION),
        None
    );
    assert_eq!(
        route.waiter_result(5.into(), CONNECTION, SUBSCRIPTION),
        Some(Ok(WaiterSuccess::NotificationsEnabled {
            connection_id: CONNECTION,
            gatt_session_id: SESSION,
            subscription_id: SUBSCRIPTION,
            characteristic_id: CHARACTERISTIC,
            actual_mode: None,
        }))
    );
    assert!(matches!(
        route.retire_transition(),
        SubscriptionDisposition::Keep
    ));
    assert!(route.is_active());
    assert!(!route.is_transitioning());
    assert_eq!(
        route.waiter_result(5.into(), CONNECTION, SUBSCRIPTION),
        None
    );

    route.begin_disable(6.into());
    assert!(route.is_active());
    record_domain(&mut route, 6.into(), enabled);
    route.progress_mut().unwrap().accepted_resolution();
    assert_eq!(
        route.waiter_result(6.into(), CONNECTION, SUBSCRIPTION),
        None
    );
    route.progress_mut().unwrap().record_terminal(&Ok(()));
    assert_eq!(
        route.waiter_result(6.into(), CONNECTION, SUBSCRIPTION),
        None
    );
    assert!(matches!(
        route.retire_transition(),
        SubscriptionDisposition::Keep
    ));
    assert!(route.is_transitioning());
    record_domain(&mut route, 6.into(), NotificationState::Disabled);
    assert_eq!(
        route.waiter_result(5.into(), CONNECTION, SUBSCRIPTION),
        None
    );
    assert_eq!(
        route.waiter_result(6.into(), CONNECTION, SUBSCRIPTION),
        Some(Ok(WaiterSuccess::NotificationsDisabled(
            CONNECTION,
            SESSION,
            SUBSCRIPTION,
            CHARACTERISTIC,
        )))
    );
    let disposition = route.retire_transition();
    assert!(matches!(disposition, SubscriptionDisposition::Remove));
    let route = match disposition {
        SubscriptionDisposition::Keep => Some(route),
        SubscriptionDisposition::Remove => None,
    };
    assert_eq!(
        route
            .as_ref()
            .and_then(|route| route.waiter_result(6.into(), CONNECTION, SUBSCRIPTION)),
        None
    );
}

#[test]
fn accepted_failure_waits_for_ack_then_removes_enable_or_restores_disable() {
    for disabling in [false, true] {
        let error = BleEventError {
            category: BleErrorCategory::NotSupported,
            message: "notification transition failed".into(),
            platform_details: None,
        };
        let mut route = enabling();
        if disabling {
            record_domain(
                &mut route,
                5.into(),
                NotificationState::Enabled { actual_mode: None },
            );
            let progress = route.progress_mut().unwrap();
            progress.accepted_resolution();
            progress.record_terminal(&Ok(()));
            assert!(matches!(
                route.retire_transition(),
                SubscriptionDisposition::Keep
            ));
            route.begin_disable(6.into());
        }
        let command_id = if disabling { 6.into() } else { 5.into() };
        route
            .progress_mut()
            .unwrap()
            .record_terminal(&Err(error.clone()));
        assert_eq!(
            route.waiter_result(command_id, CONNECTION, SUBSCRIPTION),
            None
        );
        assert!(matches!(
            route.retire_transition(),
            SubscriptionDisposition::Keep
        ));
        assert!(route.is_transitioning());
        route.progress_mut().unwrap().accepted_resolution();
        assert_eq!(
            route.waiter_result(7.into(), CONNECTION, SUBSCRIPTION),
            None
        );
        assert_eq!(
            route.waiter_result(command_id, CONNECTION, SUBSCRIPTION),
            Some(Err(WaiterError::Operation(error)))
        );
        let disposition = route.retire_transition();
        if disabling {
            assert!(matches!(disposition, SubscriptionDisposition::Keep));
            assert!(route.is_active());
            assert!(!route.is_transitioning());
            assert_eq!(
                route.waiter_result(command_id, CONNECTION, SUBSCRIPTION),
                None
            );
        } else {
            assert!(matches!(disposition, SubscriptionDisposition::Remove));
            assert!(!route.is_active());
        }
    }
}

#[test]
fn dispatch_rejection_removes_provisional_route_but_preserves_active_route() {
    let mut route = enabling();
    assert!(matches!(
        route.reject_transition(),
        SubscriptionDisposition::Remove
    ));
    let mut route = enabling();
    record_domain(
        &mut route,
        5.into(),
        NotificationState::Enabled { actual_mode: None },
    );
    let progress = route.progress_mut().unwrap();
    progress.accepted_resolution();
    progress.record_terminal(&Ok(()));
    assert!(matches!(
        route.retire_transition(),
        SubscriptionDisposition::Keep
    ));
    route.begin_disable(6.into());
    assert!(matches!(
        route.reject_transition(),
        SubscriptionDisposition::Keep
    ));
    assert!(route.is_active());
    assert!(!route.is_transitioning());
    assert_eq!(
        route.waiter_result(6.into(), CONNECTION, SUBSCRIPTION),
        None
    );
}
