use blehub::{
    BleErrorCategory, BleEvent, BleEventError, CommandId, ConnectionId, GattCharacteristicId,
    GattSessionId, NotificationMode, NotificationModeRequest, NotificationState, SubscriptionId,
};

use crate::waiter::{WaiterError, WaiterSuccess};

use super::super::{GattAccess, subscriptions::SubscriptionReservations};
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
fn dispatch_gate_separates_command_ownership_from_connection_loss() {
    use blehub::{ConnectionState, ConnectionStateResult};

    let mut route = enabling();
    for command_id in [None, Some(99.into())] {
        for connection_id in [CONNECTION, 99.into()] {
            let event = BleEvent::ConnectionStateChanged {
                command_id,
                connection_id,
                result: ConnectionStateResult::Ok {
                    state: ConnectionState::Disconnected,
                },
            };
            assert_eq!(
                route.should_buffer_event(&event, CONNECTION, SUBSCRIPTION),
                connection_id == CONNECTION,
                "connection loss is not owned by its optional command ID"
            );
        }
        let error = BleEvent::ConnectionStateChanged {
            command_id,
            connection_id: CONNECTION,
            result: ConnectionStateResult::Err {
                error: BleEventError {
                    category: BleErrorCategory::NotSupported,
                    message: "fixture connection error".into(),
                    platform_details: None,
                },
            },
        };
        assert_eq!(
            route.should_buffer_event(&error, CONNECTION, SUBSCRIPTION),
            command_id.is_none(),
            "only passive errors establish connection loss"
        );
    }
    let own_terminal = BleEvent::CommandCompleted {
        command_id: 5.into(),
        error: None,
    };
    let foreign_terminal = BleEvent::CommandCompleted {
        command_id: 99.into(),
        error: None,
    };
    assert!(route.should_buffer_event(&own_terminal, CONNECTION, SUBSCRIPTION));
    assert!(!route.should_buffer_event(&foreign_terminal, CONNECTION, SUBSCRIPTION));

    route.progress_mut().unwrap().resolve_accepted_dispatch();
    assert!(!route.should_buffer_event(&own_terminal, CONNECTION, SUBSCRIPTION));
    record_domain(
        &mut route,
        5.into(),
        NotificationState::Enabled { actual_mode: None },
    );
    route.progress_mut().unwrap().record_terminal(&Ok(()));
    route.retire_transition().unwrap();
    assert!(route.is_active());
    assert!(!route.should_buffer_event(&own_terminal, CONNECTION, SUBSCRIPTION));
}

#[test]
fn event_identity_is_checked_before_phase_mode_policy() {
    use super::ReconciliationFault;

    let mut route = SubscriptionReservation::enabling(
        5.into(),
        SESSION,
        CHARACTERISTIC,
        NotificationModeRequest::RequireNotification,
    );
    let progress = route.progress_mut().unwrap();
    progress.resolve_accepted_dispatch();
    progress.record_terminal(&Ok(()));
    let wrong_mode = BleEvent::NotificationStateChanged {
        command_id: 5.into(),
        connection_id: CONNECTION,
        gatt_session_id: SESSION,
        subscription_id: SUBSCRIPTION,
        characteristic_id: CHARACTERISTIC,
        state: NotificationState::Enabled {
            actual_mode: Some(NotificationMode::Indication),
        },
    };
    for mismatch in 0..5 {
        let mut event = wrong_mode.clone();
        let BleEvent::NotificationStateChanged {
            command_id,
            connection_id,
            gatt_session_id,
            subscription_id,
            characteristic_id,
            ..
        } = &mut event
        else {
            unreachable!();
        };
        match mismatch {
            0 => *command_id = 99.into(),
            1 => *connection_id = 99.into(),
            2 => *gatt_session_id = 99.into(),
            3 => *subscription_id = 99.into(),
            4 => *characteristic_id = 99.into(),
            _ => unreachable!(),
        }
        route
            .record_event(&event, CONNECTION, SUBSCRIPTION)
            .unwrap();
        assert_eq!(
            route.waiter_result(5.into(), CONNECTION, SUBSCRIPTION),
            None
        );
    }
    assert!(matches!(
        route.record_event(&wrong_mode, CONNECTION, SUBSCRIPTION),
        Err(ReconciliationFault::UnexpectedNotificationMode)
    ));
    record_domain(
        &mut route,
        5.into(),
        NotificationState::Enabled {
            actual_mode: Some(NotificationMode::Notification),
        },
    );
    assert!(matches!(
        route.waiter_result(5.into(), CONNECTION, SUBSCRIPTION),
        Some(Ok(WaiterSuccess::NotificationsEnabled {
            actual_mode: Some(NotificationMode::Notification),
            ..
        }))
    ));
    assert!(matches!(
        route.record_event(&wrong_mode, CONNECTION, SUBSCRIPTION),
        Err(ReconciliationFault::UnexpectedNotificationMode)
    ));
    assert!(matches!(
        route.waiter_result(5.into(), CONNECTION, SUBSCRIPTION),
        Some(Ok(WaiterSuccess::NotificationsEnabled {
            actual_mode: Some(NotificationMode::Notification),
            ..
        }))
    ));
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
    assert!(route.retire_transition().is_none());
    assert!(!route.is_active());
    record_domain(&mut route, 5.into(), enabled);
    assert_eq!(
        route.waiter_result(5.into(), CONNECTION, SUBSCRIPTION),
        None
    );
    assert!(route.retire_transition().is_none());
    assert!(!route.is_active());
    route.progress_mut().unwrap().resolve_accepted_dispatch();
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
    let retirement = route.retire_transition().unwrap();
    assert_eq!(retirement.command_id, 5.into());
    assert!(matches!(
        retirement.disposition,
        SubscriptionDisposition::Keep
    ));
    assert!(route.is_active());
    assert!(!route.is_transitioning());
    assert!(route.retire_transition().is_none());
    assert_eq!(
        route.waiter_result(5.into(), CONNECTION, SUBSCRIPTION),
        None
    );

    route.begin_disable(6.into());
    assert!(route.is_active());
    record_domain(&mut route, 6.into(), enabled);
    route.progress_mut().unwrap().resolve_accepted_dispatch();
    assert_eq!(
        route.waiter_result(6.into(), CONNECTION, SUBSCRIPTION),
        None
    );
    route.progress_mut().unwrap().record_terminal(&Ok(()));
    assert_eq!(
        route.waiter_result(6.into(), CONNECTION, SUBSCRIPTION),
        None
    );
    assert!(route.retire_transition().is_none());
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
    let retirement = route.retire_transition().unwrap();
    assert_eq!(retirement.command_id, 6.into());
    assert!(matches!(
        retirement.disposition,
        SubscriptionDisposition::Remove
    ));
    let route = match retirement.disposition {
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
            progress.resolve_accepted_dispatch();
            progress.record_terminal(&Ok(()));
            assert!(matches!(
                route.retire_transition().unwrap().disposition,
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
        assert!(route.retire_transition().is_none());
        assert!(route.is_transitioning());
        route.progress_mut().unwrap().resolve_accepted_dispatch();
        assert_eq!(
            route.waiter_result(7.into(), CONNECTION, SUBSCRIPTION),
            None
        );
        assert_eq!(
            route.waiter_result(command_id, CONNECTION, SUBSCRIPTION),
            Some(Err(WaiterError::Operation(error)))
        );
        let retirement = route.retire_transition().unwrap();
        assert_eq!(retirement.command_id, command_id);
        let disposition = retirement.disposition;
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
fn retirement_waits_for_cancellation_and_reports_the_completed_command() {
    use crate::runtime::reservations::CancellationDecision;

    for succeeded in [false, true] {
        let mut route = enabling();
        let progress = route.progress_mut().unwrap();
        progress.resolve_accepted_dispatch();
        let completion = progress.completion_observer();
        let receipt = match progress.request_cancellation() {
            CancellationDecision::Accepted(receipt) => receipt,
            _ => panic!("fixture cancellation must be admitted"),
        };
        assert!(progress.try_claim_cancellation_dispatch());
        if succeeded {
            record_domain(
                &mut route,
                5.into(),
                NotificationState::Enabled { actual_mode: None },
            );
        }
        let terminal = if succeeded {
            Ok(())
        } else {
            Err(BleEventError {
                category: BleErrorCategory::NotSupported,
                message: "fixture enable failed".into(),
                platform_details: None,
            })
        };
        route.progress_mut().unwrap().record_terminal(&terminal);
        assert_eq!(
            route
                .waiter_result(5.into(), CONNECTION, SUBSCRIPTION)
                .unwrap()
                .is_ok(),
            succeeded
        );
        assert!(route.retire_transition().is_none());
        assert!(!completion.is_completed());
        assert!(matches!(receipt.has_changed(), Ok(false)));

        let cancellation = Err(WaiterError::OperationNotCancellable(5.into()));
        route
            .progress_mut()
            .unwrap()
            .resolve_cancellation(cancellation.clone());
        let retirement = route.retire_transition().unwrap();
        assert_eq!(retirement.command_id, 5.into());
        let mut retained = match retirement.disposition {
            SubscriptionDisposition::Keep => Some(route),
            SubscriptionDisposition::Remove => {
                drop(route);
                None
            }
        };
        assert_eq!(retained.is_some(), succeeded);
        if let Some(route) = &mut retained {
            assert!(route.is_active());
            assert!(route.retire_transition().is_none());
        }
        assert!(completion.is_completed());
        assert_eq!(*receipt.borrow(), Some(cancellation));
    }
}

#[test]
fn begin_disable_rejects_overwriting_an_unfinished_transition() {
    for disabling in [false, true] {
        let mut route = enabling();
        if disabling {
            record_domain(
                &mut route,
                5.into(),
                NotificationState::Enabled { actual_mode: None },
            );
            let progress = route.progress_mut().unwrap();
            progress.resolve_accepted_dispatch();
            progress.record_terminal(&Ok(()));
            route.retire_transition().expect("fixture enable completed");
            route.begin_disable(6.into());
        }
        let progress = route.progress().unwrap();
        let command_id = progress.command_id();
        let completion = progress.completion_observer();
        let rejected = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            route.begin_disable(7.into());
        }));
        assert!(
            rejected.is_err(),
            "disabling={disabling}: transition was overwritten"
        );
        assert_eq!(route.progress().unwrap().command_id(), command_id);
        assert!(!completion.is_completed());
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
    progress.resolve_accepted_dispatch();
    progress.record_terminal(&Ok(()));
    assert!(matches!(
        route.retire_transition().unwrap().disposition,
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

fn enabling_collection() -> SubscriptionReservations {
    let mut subscriptions = SubscriptionReservations::default();
    subscriptions.reserve_enable(
        5.into(),
        SUBSCRIPTION,
        SESSION,
        CHARACTERISTIC,
        NotificationModeRequest::PlatformDefault,
    );
    subscriptions
}

fn state_event(command_id: CommandId, state: NotificationState) -> BleEvent {
    BleEvent::NotificationStateChanged {
        command_id,
        connection_id: CONNECTION,
        gatt_session_id: SESSION,
        subscription_id: SUBSCRIPTION,
        characteristic_id: CHARACTERISTIC,
        state,
    }
}

fn activate_collection(subscriptions: &mut SubscriptionReservations) {
    subscriptions
        .resolve_dispatch(5.into(), &Ok(()), CONNECTION)
        .unwrap();
    subscriptions
        .record_event(
            &state_event(5.into(), NotificationState::Enabled { actual_mode: None }),
            CONNECTION,
        )
        .unwrap();
    subscriptions
        .progress_for_cmd_mut(5.into())
        .unwrap()
        .record_terminal(&Ok(()));
    assert_eq!(subscriptions.retire_completed(), vec![5.into()]);
}

#[test]
fn collection_access_checks_local_conflicts_and_defers_duplicate_enable() {
    #[derive(Clone, Copy, Debug)]
    enum Phase {
        Absent,
        Enabling,
        Active,
        Disabling,
    }

    for phase in [
        Phase::Absent,
        Phase::Enabling,
        Phase::Active,
        Phase::Disabling,
    ] {
        let mut subscriptions = if matches!(phase, Phase::Absent) {
            SubscriptionReservations::default()
        } else {
            enabling_collection()
        };
        if matches!(phase, Phase::Active | Phase::Disabling) {
            activate_collection(&mut subscriptions);
        }
        if matches!(phase, Phase::Disabling) {
            subscriptions.reserve_disable(6.into(), SUBSCRIPTION);
        }
        for access in [
            GattAccess::Read,
            GattAccess::Write,
            GattAccess::EnableNotifications,
        ] {
            let expected = match (phase, access) {
                (Phase::Enabling | Phase::Disabling, _) | (Phase::Active, GattAccess::Read) => {
                    Err(WaiterError::OperationBusy)
                }
                _ => Ok(()),
            };
            assert_eq!(
                subscriptions.validate_characteristic_access(CHARACTERISTIC, access),
                expected,
                "phase={phase:?}, access={access:?}"
            );
            assert_eq!(
                subscriptions.validate_characteristic_access(99.into(), access),
                Ok(()),
                "another target must not inherit this route's conflict"
            );
        }
    }
}

#[test]
fn collection_preserves_route_identity_and_sibling_progress_across_retirement() {
    let mut subscriptions = enabling_collection();
    let sibling_subscription = 40.into();
    let sibling_characteristic = 30.into();
    subscriptions.reserve_enable(
        50.into(),
        sibling_subscription,
        SESSION,
        sibling_characteristic,
        NotificationModeRequest::PlatformDefault,
    );
    assert_eq!(subscriptions.transition_count(), 2);
    assert_eq!(
        subscriptions.validate_disable(SESSION, SUBSCRIPTION),
        Err(WaiterError::SubscriptionNotActive)
    );
    activate_collection(&mut subscriptions);
    assert_eq!(subscriptions.transition_count(), 1);
    assert!(subscriptions.has_subscription_for(CHARACTERISTIC));
    assert_eq!(
        subscriptions.validate_characteristic_access(CHARACTERISTIC, GattAccess::Write),
        Ok(())
    );
    assert_eq!(
        subscriptions.validate_characteristic_access(sibling_characteristic, GattAccess::Write),
        Err(WaiterError::OperationBusy)
    );
    assert_eq!(
        subscriptions.validate_disable(SESSION, SUBSCRIPTION),
        Ok(CHARACTERISTIC)
    );
    assert_eq!(
        subscriptions.validate_disable(99.into(), SUBSCRIPTION),
        Err(WaiterError::SubscriptionNotActive)
    );
    assert_eq!(
        subscriptions.validate_disable(SESSION, 99.into()),
        Err(WaiterError::SubscriptionNotActive)
    );
    assert!(subscriptions.progress_for_cmd(5.into()).is_none());
    assert!(subscriptions.waiter_result(5.into(), CONNECTION).is_none());
    assert_eq!(
        subscriptions
            .progresses()
            .map(|p| p.command_id())
            .collect::<Vec<_>>(),
        vec![50.into()]
    );

    subscriptions.reserve_disable(6.into(), SUBSCRIPTION);
    assert_eq!(
        subscriptions.validate_disable(99.into(), SUBSCRIPTION),
        Err(WaiterError::SubscriptionNotActive),
        "identity must precede transition busy"
    );
    assert_eq!(
        subscriptions.validate_disable(SESSION, SUBSCRIPTION),
        Err(WaiterError::OperationBusy)
    );
    subscriptions
        .resolve_dispatch(6.into(), &Ok(()), CONNECTION)
        .unwrap();
    subscriptions
        .record_event(
            &state_event(6.into(), NotificationState::Disabled),
            CONNECTION,
        )
        .unwrap();
    subscriptions
        .progress_for_cmd_mut(6.into())
        .unwrap()
        .record_terminal(&Ok(()));
    assert_eq!(
        subscriptions.waiter_result(6.into(), CONNECTION),
        Some(Ok(WaiterSuccess::NotificationsDisabled(
            CONNECTION,
            SESSION,
            SUBSCRIPTION,
            CHARACTERISTIC,
        )))
    );
    assert_eq!(subscriptions.retire_completed(), vec![6.into()]);
    assert!(!subscriptions.has_subscription_for(CHARACTERISTIC));
    assert!(subscriptions.has_subscription_for(sibling_characteristic));
    assert!(subscriptions.progress_for_cmd(50.into()).is_some());
    assert!(subscriptions.retire_completed().is_empty());
}

#[test]
fn collection_notification_lookup_keeps_identity_checks_and_connection_loss_scope() {
    use blehub::{ConnectionState, ConnectionStateResult};

    let mut subscriptions = enabling_collection();
    activate_collection(&mut subscriptions);
    let pending_subscription = SubscriptionId::from(40);
    let pending_characteristic = GattCharacteristicId::from(30);
    subscriptions.reserve_enable(
        50.into(),
        pending_subscription,
        SESSION,
        pending_characteristic,
        NotificationModeRequest::PlatformDefault,
    );
    for (connection_id, gatt_session_id, subscription_id, characteristic_id) in [
        (
            99.into(),
            SESSION,
            pending_subscription,
            pending_characteristic,
        ),
        (
            CONNECTION,
            99.into(),
            pending_subscription,
            pending_characteristic,
        ),
        (CONNECTION, SESSION, 99.into(), pending_characteristic),
        (CONNECTION, SESSION, pending_subscription, 99.into()),
        (CONNECTION, SESSION, SUBSCRIPTION, CHARACTERISTIC),
    ] {
        for event in [
            BleEvent::NotificationStateChanged {
                command_id: 50.into(),
                connection_id,
                gatt_session_id,
                subscription_id,
                characteristic_id,
                state: NotificationState::Enabled { actual_mode: None },
            },
            BleEvent::NotificationReceived {
                connection_id,
                gatt_session_id,
                subscription_id,
                characteristic_id,
                value: vec![1],
            },
        ] {
            assert!(
                subscriptions
                    .pending_progress_for_event_mut(&event, CONNECTION)
                    .is_none(),
                "foreign identity or stable sibling must not enter the pending transition: {event:?}"
            );
        }
    }
    let state = BleEvent::NotificationStateChanged {
        command_id: 50.into(),
        connection_id: CONNECTION,
        gatt_session_id: SESSION,
        subscription_id: pending_subscription,
        characteristic_id: pending_characteristic,
        state: NotificationState::Enabled { actual_mode: None },
    };
    let mut foreign_command = state.clone();
    if let BleEvent::NotificationStateChanged { command_id, .. } = &mut foreign_command {
        *command_id = 99.into();
    }
    assert!(
        subscriptions
            .pending_progress_for_event_mut(&foreign_command, CONNECTION)
            .is_none()
    );

    let mut events = vec![
        state,
        BleEvent::NotificationReceived {
            connection_id: CONNECTION,
            gatt_session_id: SESSION,
            subscription_id: pending_subscription,
            characteristic_id: pending_characteristic,
            value: vec![2],
        },
    ];
    // Loss must still find the pending transition even without its command ID.
    for command_id in [None, Some(5.into()), Some(99.into())] {
        events.push(BleEvent::ConnectionStateChanged {
            command_id,
            connection_id: CONNECTION,
            result: ConnectionStateResult::Ok {
                state: ConnectionState::Disconnected,
            },
        });
    }
    events.push(BleEvent::CommandCompleted {
        command_id: 50.into(),
        error: None,
    });
    for event in &events {
        let progress = subscriptions
            .pending_progress_for_event_mut(event, CONNECTION)
            .unwrap();
        assert_eq!(progress.command_id(), 50.into());
        progress
            .buffer_dispatch_pending_event(event.clone())
            .unwrap();
    }
    let resolution = subscriptions
        .resolve_dispatch(50.into(), &Ok(()), CONNECTION)
        .unwrap();
    assert_eq!(resolution.events_to_replay, events);
    assert!(subscriptions.waiter_result(50.into(), CONNECTION).is_none());
    assert!(subscriptions.has_subscription_for(CHARACTERISTIC));
    assert!(subscriptions.progress_for_cmd(5.into()).is_none());
    assert!(
        subscriptions
            .resolve_dispatch(50.into(), &Ok(()), CONNECTION)
            .unwrap()
            .events_to_replay
            .is_empty()
    );
}

#[test]
fn collection_acceptance_transfers_early_events_once_without_recording_them() {
    let mut subscriptions = enabling_collection();
    let events = vec![
        state_event(5.into(), NotificationState::Enabled { actual_mode: None }),
        BleEvent::NotificationReceived {
            connection_id: CONNECTION,
            gatt_session_id: SESSION,
            subscription_id: SUBSCRIPTION,
            characteristic_id: CHARACTERISTIC,
            value: vec![1, 2],
        },
        BleEvent::CommandCompleted {
            command_id: 5.into(),
            error: None,
        },
    ];
    assert!(
        subscriptions
            .pending_progress_for_event_mut(
                &BleEvent::CommandCompleted {
                    command_id: 99.into(),
                    error: None
                },
                CONNECTION,
            )
            .is_none()
    );
    for event in &events {
        subscriptions
            .pending_progress_for_event_mut(event, CONNECTION)
            .unwrap()
            .buffer_dispatch_pending_event(event.clone())
            .unwrap();
    }
    let resolution = subscriptions
        .resolve_dispatch(5.into(), &Ok(()), CONNECTION)
        .unwrap();
    assert_eq!(resolution.events_to_replay, events);
    assert!(matches!(resolution.action, super::DispatchAction::NoAction));
    assert!(subscriptions.waiter_result(5.into(), CONNECTION).is_none());
    assert!(subscriptions.retire_completed().is_empty());
    assert!(
        subscriptions
            .pending_progress_for_event_mut(&events[0], CONNECTION)
            .is_none()
    );
    assert!(
        subscriptions
            .resolve_dispatch(5.into(), &Ok(()), CONNECTION)
            .unwrap()
            .events_to_replay
            .is_empty()
    );
    for event in resolution.events_to_replay {
        subscriptions.record_event(&event, CONNECTION).unwrap();
        if let BleEvent::CommandCompleted { command_id, .. } = event {
            subscriptions
                .progress_for_cmd_mut(command_id)
                .unwrap()
                .record_terminal(&Ok(()));
        }
    }
    assert!(matches!(
        subscriptions.waiter_result(5.into(), CONNECTION),
        Some(Ok(WaiterSuccess::NotificationsEnabled {
            connection_id: CONNECTION,
            gatt_session_id: SESSION,
            subscription_id: SUBSCRIPTION,
            characteristic_id: CHARACTERISTIC,
            actual_mode: None,
        }))
    ));
    assert_eq!(subscriptions.retire_completed(), vec![5.into()]);
    assert!(!subscriptions.is_empty());
}

#[test]
fn collection_rejection_drains_loss_and_conflicts_before_applying_route_disposition() {
    use super::{DispatchAction, ReconciliationFault};
    use blehub::{CommandDispatchError, ConnectionState, ConnectionStateResult};

    for disabling in [false, true] {
        let mut subscriptions = enabling_collection();
        let command_id = if disabling {
            activate_collection(&mut subscriptions);
            subscriptions.reserve_disable(6.into(), SUBSCRIPTION);
            6.into()
        } else {
            5.into()
        };
        let completion = subscriptions
            .progress_for_cmd(command_id)
            .unwrap()
            .completion_observer();
        let loss = |command_id, connection_id| BleEvent::ConnectionStateChanged {
            command_id,
            connection_id,
            result: ConnectionStateResult::Ok {
                state: ConnectionState::Disconnected,
            },
        };
        let passive = loss(None, CONNECTION);
        let commanded = loss(Some(99.into()), CONNECTION);
        let foreign = loss(None, 99.into());
        let data = BleEvent::NotificationReceived {
            connection_id: CONNECTION,
            gatt_session_id: SESSION,
            subscription_id: SUBSCRIPTION,
            characteristic_id: CHARACTERISTIC,
            value: vec![1],
        };
        for event in [passive.clone(), data, commanded.clone(), foreign] {
            subscriptions
                .progress_for_cmd_mut(command_id)
                .unwrap()
                .buffer_dispatch_pending_event(event)
                .unwrap();
        }
        let rejection = Err(CommandDispatchError::Closed);
        assert!(
            subscriptions
                .resolve_dispatch(99.into(), &rejection, CONNECTION)
                .is_none()
        );
        assert!(!completion.is_completed());
        let resolution = subscriptions
            .resolve_dispatch(command_id, &rejection, CONNECTION)
            .unwrap();
        assert_eq!(resolution.events_to_replay, vec![passive, commanded]);
        assert!(matches!(
            resolution.action,
            DispatchAction::FaultRuntime(ReconciliationFault::DispatchEvidenceConflict)
        ));
        assert!(completion.is_completed());
        assert!(subscriptions.progress_for_cmd(command_id).is_none());
        assert!(
            subscriptions
                .waiter_result(command_id, CONNECTION)
                .is_none()
        );
        assert_eq!(subscriptions.transition_count(), 0);
        assert_eq!(subscriptions.is_empty(), !disabling);
        if disabling {
            assert_eq!(
                subscriptions.validate_disable(SESSION, SUBSCRIPTION),
                Ok(CHARACTERISTIC)
            );
        }
        assert!(subscriptions.retire_completed().is_empty());

        // Commanded loss alone is permitted, rather than the ordinary I/O passive-only policy.
        let mut subscriptions = enabling_collection();
        let commanded = loss(Some(99.into()), CONNECTION);
        subscriptions
            .pending_progress_for_event_mut(&commanded, CONNECTION)
            .unwrap()
            .buffer_dispatch_pending_event(commanded.clone())
            .unwrap();
        let resolution = subscriptions
            .resolve_dispatch(5.into(), &rejection, CONNECTION)
            .unwrap();
        assert_eq!(resolution.events_to_replay, vec![commanded]);
        assert!(matches!(resolution.action, DispatchAction::NoAction));
        assert!(subscriptions.is_empty());
    }
}

#[test]
fn collection_failed_disable_retains_local_route_only_after_cancel_receipt_resolves() {
    use crate::runtime::reservations::CancellationDecision;

    let mut subscriptions = enabling_collection();
    activate_collection(&mut subscriptions);
    subscriptions.reserve_disable(6.into(), SUBSCRIPTION);
    subscriptions
        .resolve_dispatch(6.into(), &Ok(()), CONNECTION)
        .unwrap();
    let progress = subscriptions.progress_for_cmd_mut(6.into()).unwrap();
    let completion = progress.completion_observer();
    let receipt = match progress.request_cancellation() {
        CancellationDecision::Accepted(receipt) => receipt,
        _ => panic!("fixture cancellation must be admitted"),
    };
    assert!(progress.try_claim_cancellation_dispatch());
    let error = BleEventError {
        category: BleErrorCategory::NotSupported,
        message: "disable failed without proving native rollback".into(),
        platform_details: None,
    };
    progress.record_terminal(&Err(error.clone()));
    assert_eq!(
        subscriptions.waiter_result(6.into(), CONNECTION),
        Some(Err(WaiterError::Operation(error)))
    );
    assert!(subscriptions.retire_completed().is_empty());
    assert_eq!(subscriptions.transition_count(), 1);
    assert!(!completion.is_completed());
    assert!(matches!(receipt.has_changed(), Ok(false)));
    let cancellation = Err(WaiterError::OperationNotCancellable(6.into()));
    subscriptions
        .progress_for_cmd_mut(6.into())
        .unwrap()
        .resolve_cancellation(cancellation.clone());
    assert_eq!(subscriptions.retire_completed(), vec![6.into()]);
    assert!(completion.is_completed());
    assert_eq!(*receipt.borrow(), Some(cancellation));
    assert_eq!(subscriptions.transition_count(), 0);
    assert!(!subscriptions.is_empty());
    assert_eq!(
        subscriptions.validate_disable(SESSION, SUBSCRIPTION),
        Ok(CHARACTERISTIC)
    );
    assert!(subscriptions.retire_completed().is_empty());
}
