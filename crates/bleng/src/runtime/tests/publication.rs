//! Actor-level ordering regressions: publication may lag evidence, never teardown.
use super::BlengActor;
use crate::{
    runtime::{
        RuntimeLifecycle, RuntimePhase,
        reservations::{CancellationDecision, CancellationReceiver},
        shared::IdGenerator,
    },
    waiter::{WaiterError, WaiterExpectation, WaiterOutcome, WaiterSuccess},
};
use blehub::{
    BleEvent, CommandId, ConnectionId, ConnectionState, ConnectionStateResult, PeripheralId,
    ScanStopReason, ScanStopResult,
};
use std::sync::Arc;
use tokio::sync::{mpsc, oneshot};

type ResultReceiver = oneshot::Receiver<WaiterOutcome>;

fn fixture() -> (BlengActor, mpsc::Receiver<BleEvent>, Arc<RuntimeLifecycle>) {
    let lifecycle = Arc::new(RuntimeLifecycle::new());
    let (tx, rx) = mpsc::channel(128);
    (
        BlengActor::with_ids(tx, Arc::clone(&lifecycle), Arc::new(IdGenerator::default())),
        rx,
        lifecycle,
    )
}

fn terminal(command_id: CommandId) -> BleEvent {
    BleEvent::CommandCompleted {
        command_id,
        error: None,
    }
}

fn connected(command_id: CommandId, connection_id: ConnectionId) -> BleEvent {
    BleEvent::ConnectionStateChanged {
        command_id: Some(command_id),
        connection_id,
        result: ConnectionStateResult::Ok {
            state: ConnectionState::Connected,
        },
    }
}

fn lost(connection_id: ConnectionId) -> BleEvent {
    BleEvent::ConnectionStateChanged {
        command_id: None,
        connection_id,
        result: ConnectionStateResult::Ok {
            state: ConnectionState::Disconnected,
        },
    }
}

fn stopped() -> BleEvent {
    BleEvent::ScanStopped {
        scan_id: 1.into(),
        result: ScanStopResult::Ok {
            reason: ScanStopReason::SystemSuspended,
        },
    }
}

fn notification(connection_id: ConnectionId) -> BleEvent {
    BleEvent::NotificationReceived {
        connection_id,
        gatt_session_id: 30.into(),
        subscription_id: 50.into(),
        characteristic_id: 40.into(),
        value: vec![9],
    }
}

fn read(command_id: CommandId, characteristic: u64) -> BleEvent {
    BleEvent::CharacteristicRead {
        command_id,
        connection_id: 10.into(),
        gatt_session_id: 30.into(),
        characteristic_id: characteristic.into(),
        value: vec![1],
    }
}

fn drain(rx: &mut mpsc::Receiver<BleEvent>) -> Vec<BleEvent> {
    let mut events = Vec::new();
    while let Ok(event) = rx.try_recv() {
        events.push(event);
    }
    events
}

fn reserve_connect(
    actor: &mut BlengActor,
    peripheral: u64,
    connection: u64,
) -> (CommandId, ResultReceiver) {
    let (id, result) = actor
        .register_waiter(WaiterExpectation::Connected(connection.into()))
        .unwrap();
    actor.reservations.test_connections().reserve_connection(
        peripheral.into(),
        id,
        connection.into(),
    );
    actor
        .reservations
        .test_connections()
        .resolve_dispatch(id, &Ok(()))
        .unwrap();
    (id, result)
}

fn ready_connection(actor: &mut BlengActor) {
    let (id, mut result) = reserve_connect(actor, 20, 10);
    actor.handle_ingress_event(connected(id, 10.into()));
    actor.handle_ingress_event(terminal(id));
    assert_eq!(
        result.try_recv(),
        Ok(Ok(WaiterSuccess::Connected(10.into())))
    );
}

fn reserve_read(actor: &mut BlengActor, characteristic: u64) -> (CommandId, ResultReceiver) {
    let (id, result) = actor
        .register_waiter(WaiterExpectation::CharacteristicRead(
            10.into(),
            30.into(),
            characteristic.into(),
        ))
        .unwrap();
    actor.reservations.test_connections().reserve_read(
        20.into(),
        id,
        30.into(),
        characteristic.into(),
    );
    actor
        .reservations
        .test_connections()
        .resolve_dispatch(id, &Ok(()))
        .unwrap();
    (id, result)
}

fn cancel(actor: &mut BlengActor, command_id: CommandId) -> CancellationReceiver {
    let receipt = match actor
        .reservations
        .test_connections()
        .request_cancellation(command_id)
    {
        Some(CancellationDecision::Accepted(receipt)) => receipt,
        _ => panic!("cancellation must be admitted"),
    };
    assert!(
        actor
            .reservations
            .test_connections()
            .take_cancellation_dispatch(command_id)
    );
    receipt
}

fn control(command_id: CommandId, accepted: bool) -> Result<(), WaiterError> {
    if accepted {
        Ok(())
    } else {
        Err(WaiterError::OperationAlreadyCompleted(command_id))
    }
}

fn notification_state(command_id: CommandId, disabling: bool) -> BleEvent {
    BleEvent::NotificationStateChanged {
        command_id,
        connection_id: 10.into(),
        gatt_session_id: 30.into(),
        subscription_id: 50.into(),
        characteristic_id: 40.into(),
        state: if disabling {
            blehub::NotificationState::Disabled
        } else {
            blehub::NotificationState::Enabled {
                actual_mode: Some(blehub::NotificationMode::Indication),
            }
        },
    }
}

fn resolve_notification_dispatch(
    actor: &mut BlengActor,
    command_id: CommandId,
    result: Result<(), blehub::CommandDispatchError>,
) {
    let prepared = kameo::actor::PreparedActor::<BlengActor>::new(kameo::mailbox::bounded(1));
    actor.handle_dispatch_result(command_id, result, prepared.actor_ref().clone());
}

fn reserve_notification_transition(
    actor: &mut BlengActor,
    disabling: bool,
) -> (CommandId, ResultReceiver) {
    if disabling {
        let (enable, mut result) = reserve_notification_transition(actor, false);
        actor.handle_ingress_event(notification_state(enable, false));
        actor.handle_ingress_event(terminal(enable));
        resolve_notification_dispatch(actor, enable, Ok(()));
        assert_eq!(result.try_recv(), Ok(Ok(notification_success(false))));
    }
    let expectation = if disabling {
        WaiterExpectation::NotificationsDisabled(10.into(), 30.into(), 50.into(), 40.into())
    } else {
        WaiterExpectation::NotificationsEnabled(10.into(), 30.into(), 50.into(), 40.into())
    };
    let (id, result) = actor.register_waiter(expectation).unwrap();
    if disabling {
        actor
            .reservations
            .test_connections()
            .reserve_notification_disable(20.into(), id, 50.into());
    } else {
        actor
            .reservations
            .test_connections()
            .reserve_notification_enable(
                20.into(),
                id,
                50.into(),
                30.into(),
                40.into(),
                blehub::NotificationModeRequest::PlatformDefault,
            );
    }
    (id, result)
}

fn notification_success(disabling: bool) -> WaiterSuccess {
    if disabling {
        WaiterSuccess::NotificationsDisabled(10.into(), 30.into(), 50.into(), 40.into())
    } else {
        WaiterSuccess::NotificationsEnabled {
            connection_id: 10.into(),
            gatt_session_id: 30.into(),
            subscription_id: 50.into(),
            characteristic_id: 40.into(),
            actual_mode: Some(blehub::NotificationMode::Indication),
        }
    }
}

#[test]
fn notification_order_preserves_input_across_dispatch_ack_without_making_value_completion() {
    for disabling in [false, true] {
        for order in 0..4 {
            for before_ack in (0..=3).rev() {
                let (mut actor, mut rx, lifecycle) = fixture();
                ready_connection(&mut actor);
                let (id, mut result) = reserve_notification_transition(&mut actor, disabling);
                drain(&mut rx);
                let state = notification_state(id, disabling);
                let value = notification(10.into());
                // Preserve actual input order, including genuinely early native
                // values; do not manufacture a universal state-before-value rule.
                let events = match order {
                    0 => vec![state, terminal(id), value],
                    1 => vec![value, state, terminal(id)],
                    2 => vec![terminal(id), state, value],
                    _ => vec![state, value, terminal(id)],
                };
                for event in &events[..before_ack] {
                    actor.handle_ingress_event(event.clone());
                }
                assert_eq!(result.try_recv(), Err(oneshot::error::TryRecvError::Empty));
                resolve_notification_dispatch(&mut actor, id, Ok(()));
                let has_completion_evidence = |prefix: &[BleEvent]| {
                    prefix
                        .iter()
                        .any(|event| matches!(event, BleEvent::NotificationStateChanged { .. }))
                        && prefix
                            .iter()
                            .any(|event| matches!(event, BleEvent::CommandCompleted { .. }))
                };
                if !has_completion_evidence(&events[..before_ack]) {
                    assert_eq!(result.try_recv(), Err(oneshot::error::TryRecvError::Empty));
                }
                for (index, event) in events.iter().enumerate().skip(before_ack) {
                    actor.handle_ingress_event(event.clone());
                    if !has_completion_evidence(&events[..=index]) {
                        assert_eq!(
                            result.try_recv(),
                            Err(oneshot::error::TryRecvError::Empty),
                            "value must never complete the transition waiter"
                        );
                    }
                }
                assert_eq!(
                    drain(&mut rx),
                    events,
                    "disabling={disabling}, order={order}, before_ack={before_ack}: value must not overtake earlier transition evidence"
                );
                assert_eq!(result.try_recv(), Ok(Ok(notification_success(disabling))));
                assert_eq!(result.try_recv(), Err(oneshot::error::TryRecvError::Closed));
                assert!(lifecycle.is_open());
            }
        }
    }
}

#[test]
fn notification_order_rejected_dispatch_with_same_route_callbacks_is_fail_closed() {
    for disabling in [false, true] {
        for value_only in [true, false] {
            let (mut actor, mut rx, lifecycle) = fixture();
            ready_connection(&mut actor);
            let (id, mut result) = reserve_notification_transition(&mut actor, disabling);
            drain(&mut rx);
            if !value_only {
                actor.handle_ingress_event(notification_state(id, disabling));
                actor.handle_ingress_event(terminal(id));
            }
            actor.handle_ingress_event(notification(10.into()));
            let error = blehub::CommandDispatchError::InvalidState {
                detail: "notification dispatch rejected after callback".into(),
            };
            resolve_notification_dispatch(&mut actor, id, Err(error.clone()));
            assert_eq!(
                lifecycle.phase(),
                RuntimePhase::Faulted,
                "disabling={disabling}, value_only={value_only}: matching data cannot escape rejection reconciliation"
            );
            assert!(
                drain(&mut rx).is_empty(),
                "no callback from rejected transition may have escaped before the fault"
            );
            assert_eq!(
                result.try_recv(),
                Ok(Err(WaiterError::DispatchRejected(error)))
            );
        }
    }
}

#[test]
fn notification_order_exact_route_gating_does_not_block_another_connection_or_subscription() {
    let (mut actor, mut rx, lifecycle) = fixture();
    ready_connection(&mut actor);
    let (id, mut result) = reserve_notification_transition(&mut actor, false);
    drain(&mut rx);
    let state = notification_state(id, false);
    let value = notification(10.into());
    actor.handle_ingress_event(state.clone());
    actor.handle_ingress_event(terminal(id));
    actor.handle_ingress_event(value.clone());
    let (other, mut other_result) = reserve_connect(&mut actor, 21, 11);
    let mut unrelated = vec![connected(other, 11.into()), terminal(other)];
    // Each route component is required independently, including the connection
    // on an event that otherwise names the pending subscription exactly.
    for component in 0..4 {
        let mut wrong_route = value.clone();
        if let BleEvent::NotificationReceived {
            connection_id,
            gatt_session_id,
            subscription_id,
            characteristic_id,
            ..
        } = &mut wrong_route
        {
            match component {
                0 => *connection_id = 11.into(),
                1 => *gatt_session_id = 31.into(),
                2 => *subscription_id = 51.into(),
                _ => *characteristic_id = 41.into(),
            }
        }
        unrelated.push(wrong_route);
    }
    for event in &unrelated {
        actor.handle_ingress_event(event.clone());
    }
    assert_eq!(
        drain(&mut rx),
        unrelated,
        "only the matching subscription waits for its dispatch result"
    );
    assert_eq!(
        other_result.try_recv(),
        Ok(Ok(WaiterSuccess::Connected(11.into())))
    );
    resolve_notification_dispatch(&mut actor, id, Ok(()));
    assert_eq!(drain(&mut rx), vec![state, terminal(id), value]);
    assert_eq!(result.try_recv(), Ok(Ok(notification_success(false))));
    assert!(lifecycle.is_open());
}

#[test]
fn notification_order_state_terminal_and_values_share_the_existing_64_event_buffer() {
    for disabling in [false, true] {
        let (mut actor, mut rx, lifecycle) = fixture();
        ready_connection(&mut actor);
        let (id, mut result) = reserve_notification_transition(&mut actor, disabling);
        drain(&mut rx);
        actor.handle_ingress_event(notification_state(id, disabling));
        actor.handle_ingress_event(terminal(id));
        for _ in 0..62 {
            actor.handle_ingress_event(notification(10.into()));
        }
        assert!(
            lifecycle.is_open(),
            "state + terminal + 62 values fit the single transition buffer"
        );
        actor.handle_ingress_event(notification(10.into()));
        assert_eq!(
            lifecycle.phase(),
            RuntimePhase::Faulted,
            "the 65th related callback must fail closed, not spill into another queue"
        );
        assert!(drain(&mut rx).is_empty());
        drop(actor);
        assert_eq!(result.try_recv(), Ok(Err(WaiterError::TransportFault)));
    }
}

#[test]
fn disable_cancellation_receipt_retains_transition_and_applies_original_outcome_once() {
    // Accepted control has a Cancelled terminal; rejected control preserves the
    // original typed failure or complete success in either domain/terminal order.
    for (accepted, failed, terminal_first) in [
        (true, true, true),
        (false, true, true),
        (false, false, true),
        (false, false, false),
    ] {
        let (mut actor, mut rx, lifecycle) = fixture();
        ready_connection(&mut actor);
        let (id, mut result) = reserve_notification_transition(&mut actor, true);
        resolve_notification_dispatch(&mut actor, id, Ok(()));
        drain(&mut rx);
        let completion = actor
            .reservations
            .test_connections()
            .completion_observer(id)
            .unwrap();
        let receipt = cancel(&mut actor, id);
        let error = blehub::BleEventError {
            category: if accepted {
                blehub::BleErrorCategory::Cancelled
            } else {
                blehub::BleErrorCategory::NotSupported
            },
            message: "disable ended while its cancellation receipt was pending".into(),
            platform_details: None,
        };
        let completion_event = BleEvent::CommandCompleted {
            command_id: id,
            error: failed.then(|| error.clone()),
        };
        let mut before = notification(10.into());
        let mut after = notification(10.into());
        if let BleEvent::NotificationReceived { value, .. } = &mut before {
            *value = vec![0, 0xff];
        }
        if let BleEvent::NotificationReceived { value, .. } = &mut after {
            *value = vec![1, 0xfe];
        }
        let mut events = vec![before];
        if failed {
            events.push(completion_event);
        } else if terminal_first {
            events.extend([completion_event, notification_state(id, true)]);
        } else {
            events.extend([notification_state(id, true), completion_event]);
        }
        events.push(after);
        for (index, event) in events.iter().enumerate() {
            actor.handle_ingress_event(event.clone());
            assert!(
                drain(&mut rx).is_empty(),
                "pending control must retain FIFO"
            );
            assert_eq!(result.try_recv(), Err(oneshot::error::TryRecvError::Empty));
            assert!(matches!(receipt.has_changed(), Ok(false)));
            assert_eq!(
                actor
                    .reservations
                    .test_connections()
                    .validate_notification_disable(10.into(), 30.into(), 50.into()),
                Err(WaiterError::OperationBusy),
                "terminal must not release the disable transition before its receipt"
            );
            if index == 0 {
                assert!(
                    !completion.is_completed(),
                    "value is not completion evidence"
                );
            }
        }
        assert!(completion.is_completed());
        let retry_receipt = match actor
            .reservations
            .test_connections()
            .request_cancellation(id)
        {
            Some(CancellationDecision::AlreadyRequested(receipt)) => receipt,
            _ => panic!("terminal must not retire the pending receipt owner"),
        };
        assert!(
            !actor
                .reservations
                .test_connections()
                .take_cancellation_dispatch(id)
        );
        let control_result = control(id, accepted);
        actor.handle_cancellation_result(id, control_result.clone());
        assert_eq!(*receipt.borrow(), Some(control_result.clone()));
        assert_eq!(*retry_receipt.borrow(), Some(control_result.clone()));
        assert_eq!(
            drain(&mut rx),
            events,
            "release must preserve exact IDs, bytes and FIFO"
        );
        assert_eq!(
            result.try_recv(),
            Ok(if accepted {
                Err(WaiterError::Cancelled)
            } else if failed {
                Err(WaiterError::Operation(error))
            } else {
                Ok(notification_success(true))
            })
        );
        assert_eq!(result.try_recv(), Err(oneshot::error::TryRecvError::Closed));
        assert!(
            actor
                .reservations
                .test_connections()
                .completion_observer(id)
                .is_none()
        );
        actor.handle_cancellation_result(id, control_result.clone());
        assert!(
            drain(&mut rx).is_empty(),
            "duplicate receipt must not republish"
        );
        assert_eq!(result.try_recv(), Err(oneshot::error::TryRecvError::Closed));

        if failed {
            // Failed/cancelled disable restores the same active identity, not a
            // new enable. Only a fresh explicit disable may retire that route.
            assert_eq!(
                actor
                    .reservations
                    .test_connections()
                    .validate_notification_disable(10.into(), 30.into(), 50.into()),
                Ok((20.into(), 40.into()))
            );
            assert_eq!(
                actor
                    .reservations
                    .test_connections()
                    .validate_gatt_discovery(10.into()),
                Err(WaiterError::GattBusy)
            );
            let observed = notification(10.into());
            actor.handle_ingress_event(observed.clone());
            assert_eq!(drain(&mut rx), vec![observed]);
            assert_eq!(result.try_recv(), Err(oneshot::error::TryRecvError::Closed));
            let (retry, mut retry_result) = actor
                .register_waiter(WaiterExpectation::NotificationsDisabled(
                    10.into(),
                    30.into(),
                    50.into(),
                    40.into(),
                ))
                .unwrap();
            assert_ne!(retry, id);
            actor
                .reservations
                .test_connections()
                .reserve_notification_disable(20.into(), retry, 50.into());
            resolve_notification_dispatch(&mut actor, retry, Ok(()));
            actor.handle_cancellation_result(id, control_result);
            assert_eq!(
                retry_result.try_recv(),
                Err(oneshot::error::TryRecvError::Empty)
            );
            assert!(
                drain(&mut rx).is_empty(),
                "old receipt cannot settle or publish for retry"
            );
            let retry_events = [notification_state(retry, true), terminal(retry)];
            for event in &retry_events {
                actor.handle_ingress_event(event.clone());
            }
            assert_eq!(drain(&mut rx), retry_events);
            assert_eq!(retry_result.try_recv(), Ok(Ok(notification_success(true))));
            assert_eq!(
                retry_result.try_recv(),
                Err(oneshot::error::TryRecvError::Closed)
            );
        }
        assert_eq!(
            actor
                .reservations
                .test_connections()
                .validate_notification_disable(10.into(), 30.into(), 50.into()),
            Err(WaiterError::SubscriptionNotActive)
        );
        assert_eq!(
            actor
                .reservations
                .test_connections()
                .validate_gatt_discovery(10.into()),
            Ok(20.into())
        );
        assert_eq!(
            actor
                .reservations
                .test_connections()
                .require_peripheral_available(20.into()),
            Err(WaiterError::PeripheralBusy)
        );
        assert!(
            drain(&mut rx).is_empty(),
            "no value may be manufactured by control or retirement"
        );
        assert!(lifecycle.is_open());
    }
}

#[test]
fn publication_order_connection_teardown_cannot_overtake_pending_connect_evidence() {
    for accepted in [false, true] {
        for terminal_first in [false, true] {
            let (mut actor, mut rx, lifecycle) = fixture();
            let (id, mut result) = reserve_connect(&mut actor, 20, 10);
            let receipt = cancel(&mut actor, id);
            let domain = connected(id, 10.into());
            let mut events = if terminal_first {
                vec![terminal(id), domain]
            } else {
                vec![domain, terminal(id)]
            };
            events.push(lost(10.into()));
            for event in &events {
                actor.handle_ingress_event(event.clone());
            }
            actor.handle_cancellation_result(id, control(id, accepted));
            let expected: Vec<_> = events
                .into_iter()
                .filter(|event| {
                    !(accepted
                        && event.command_id() == Some(id)
                        && !matches!(event, BleEvent::CommandCompleted { .. }))
                })
                .collect();
            assert_eq!(
                drain(&mut rx),
                expected,
                "accepted={accepted}, terminal_first={terminal_first}: old connect success must never follow teardown"
            );
            assert_eq!(*receipt.borrow(), Some(control(id, accepted)));
            assert_eq!(
                result.try_recv(),
                Ok(if accepted {
                    Err(WaiterError::Cancelled)
                } else {
                    Ok(WaiterSuccess::Connected(10.into()))
                })
            );
            assert_eq!(
                actor
                    .reservations
                    .test_connections()
                    .require_peripheral_available(PeripheralId::from(20)),
                Ok(())
            );
            actor.handle_cancellation_result(id, control(id, accepted));
            assert!(
                drain(&mut rx).is_empty(),
                "a repeated receipt must not republish evidence"
            );
            assert!(lifecycle.is_open());
        }
    }
}

#[test]
fn publication_order_scan_stop_cannot_overtake_pending_start_evidence() {
    for accepted in [false, true] {
        for terminal_first in [false, true] {
            let (mut actor, mut rx, lifecycle) = fixture();
            let (id, mut result) = actor
                .register_waiter(WaiterExpectation::ScanStarted(1.into()))
                .unwrap();
            actor
                .reservations
                .test_scan()
                .reserve_scan_start(id, 1.into());
            actor
                .reservations
                .test_scan()
                .resolve_dispatch(id, &Ok(()))
                .unwrap();
            let receipt = match actor.reservations.test_scan().request_cancellation(id) {
                Some(CancellationDecision::Accepted(receipt)) => receipt,
                _ => panic!("scan cancellation must be admitted"),
            };
            assert!(
                actor
                    .reservations
                    .test_scan()
                    .take_cancellation_dispatch(id)
            );
            let domain = BleEvent::ScanStarted {
                command_id: id,
                scan_id: 1.into(),
            };
            let mut events = if terminal_first {
                vec![terminal(id), domain]
            } else {
                vec![domain, terminal(id)]
            };
            events.push(stopped());
            for event in &events {
                actor.handle_ingress_event(event.clone());
            }
            actor.handle_cancellation_result(id, control(id, accepted));
            let expected: Vec<_> = events
                .into_iter()
                .filter(|event| {
                    !(accepted
                        && event.command_id() == Some(id)
                        && !matches!(event, BleEvent::CommandCompleted { .. }))
                })
                .collect();
            assert_eq!(
                drain(&mut rx),
                expected,
                "accepted={accepted}, terminal_first={terminal_first}: old start success must never follow teardown"
            );
            assert_eq!(*receipt.borrow(), Some(control(id, accepted)));
            assert_eq!(
                result.try_recv(),
                Ok(if accepted {
                    Err(WaiterError::Cancelled)
                } else {
                    Ok(WaiterSuccess::ScanStarted(1.into()))
                })
            );
            assert_eq!(
                actor.reservations.test_scan().require_scan_available(),
                Ok(())
            );
            actor.handle_cancellation_result(id, control(id, accepted));
            assert!(drain(&mut rx).is_empty());
            assert!(lifecycle.is_open());
        }
    }
}

#[test]
fn publication_order_multiple_controls_share_one_connection_order_without_blocking_other_scopes() {
    for accept_a in [false, true] {
        for accept_b in [false, true] {
            for reverse_receipts in [false, true] {
                let (mut actor, mut rx, lifecycle) = fixture();
                ready_connection(&mut actor);
                assert_eq!(drain(&mut rx).len(), 2);
                let (a, result_a) = reserve_read(&mut actor, 40);
                let (b, result_b) = reserve_read(&mut actor, 41);
                let receipt_a = cancel(&mut actor, a);
                let receipt_b = cancel(&mut actor, b);
                let events = vec![
                    read(b, 41),
                    terminal(a),
                    notification(10.into()),
                    read(a, 40),
                    terminal(b),
                    lost(10.into()),
                ];
                for event in &events {
                    actor.handle_ingress_event(event.clone());
                }
                let (other, mut other_result) = reserve_connect(&mut actor, 21, 11);
                let mut other_events = vec![connected(other, 11.into()), terminal(other)];
                for event in &other_events {
                    actor.handle_ingress_event(event.clone());
                }
                let (scan, mut scan_result) = actor
                    .register_waiter(WaiterExpectation::ScanStarted(1.into()))
                    .unwrap();
                actor
                    .reservations
                    .test_scan()
                    .reserve_scan_start(scan, 1.into());
                actor
                    .reservations
                    .test_scan()
                    .resolve_dispatch(scan, &Ok(()))
                    .unwrap();
                let scan_events = [
                    BleEvent::ScanStarted {
                        command_id: scan,
                        scan_id: 1.into(),
                    },
                    terminal(scan),
                ];
                for event in &scan_events {
                    actor.handle_ingress_event(event.clone());
                }
                other_events.extend(scan_events);
                assert_eq!(
                    scan_result.try_recv(),
                    Ok(Ok(WaiterSuccess::ScanStarted(1.into())))
                );
                assert_eq!(
                    drain(&mut rx),
                    other_events,
                    "only the pending connection is delayed"
                );
                assert_eq!(
                    other_result.try_recv(),
                    Ok(Ok(WaiterSuccess::Connected(11.into())))
                );
                let controls = if reverse_receipts {
                    [(b, accept_b), (a, accept_a)]
                } else {
                    [(a, accept_a), (b, accept_b)]
                };
                actor.handle_cancellation_result(
                    controls[0].0,
                    control(controls[0].0, controls[0].1),
                );
                assert!(
                    drain(&mut rx).is_empty(),
                    "one receipt must not release a scope with another pending control"
                );
                // Events arriving after one receipt still follow the same scope order.
                let after_first = notification(10.into());
                actor.handle_ingress_event(after_first.clone());
                assert!(drain(&mut rx).is_empty());
                actor.handle_cancellation_result(
                    controls[1].0,
                    control(controls[1].0, controls[1].1),
                );
                let mut expected: Vec<_> = events
                    .into_iter()
                    .filter(|event| {
                        let accepted_target = (accept_a && event.command_id() == Some(a))
                            || (accept_b && event.command_id() == Some(b));
                        !accepted_target || matches!(event, BleEvent::CommandCompleted { .. })
                    })
                    .collect();
                expected.push(after_first);
                assert_eq!(
                    drain(&mut rx),
                    expected,
                    "accept_a={accept_a}, accept_b={accept_b}, reverse={reverse_receipts}"
                );
                assert_eq!(*receipt_a.borrow(), Some(control(a, accept_a)));
                assert_eq!(*receipt_b.borrow(), Some(control(b, accept_b)));
                for (mut result, accepted, characteristic) in
                    [(result_a, accept_a, 40), (result_b, accept_b, 41)]
                {
                    assert_eq!(
                        result.try_recv(),
                        Ok(if accepted {
                            Err(WaiterError::Cancelled)
                        } else {
                            Ok(WaiterSuccess::CharacteristicRead {
                                connection_id: 10.into(),
                                gatt_session_id: 30.into(),
                                characteristic_id: characteristic.into(),
                                value: vec![1],
                            })
                        })
                    );
                    assert_eq!(result.try_recv(), Err(oneshot::error::TryRecvError::Closed));
                }
                actor.handle_cancellation_result(a, control(a, accept_a));
                actor.handle_cancellation_result(b, control(b, accept_b));
                assert!(drain(&mut rx).is_empty());
                assert!(lifecycle.is_open());
            }
        }
    }
}

#[test]
fn publication_order_dispatch_rejection_finishes_pending_control_without_stranding_scope() {
    for other_pending in [false, true] {
        let (mut actor, mut rx, lifecycle) = fixture();
        ready_connection(&mut actor);
        drain(&mut rx);
        let (a, mut result_a) = actor
            .register_waiter(WaiterExpectation::CharacteristicRead(
                10.into(),
                30.into(),
                40.into(),
            ))
            .unwrap();
        actor
            .reservations
            .test_connections()
            .reserve_read(20.into(), a, 30.into(), 40.into());
        let receipt_a = match actor
            .reservations
            .test_connections()
            .request_cancellation(a)
        {
            Some(CancellationDecision::Accepted(receipt)) => receipt,
            _ => panic!("pre-dispatch cancellation must be admitted"),
        };
        assert!(
            !actor
                .reservations
                .test_connections()
                .take_cancellation_dispatch(a)
        );
        let sibling = other_pending.then(|| {
            let (b, result_b) = reserve_read(&mut actor, 41);
            let receipt_b = cancel(&mut actor, b);
            (b, result_b, receipt_b)
        });
        let event = notification(10.into());
        actor.handle_ingress_event(event.clone());
        assert!(drain(&mut rx).is_empty());
        // Rejection takes no native-control dispatch path; the ActorRef is unused.
        let prepared = kameo::actor::PreparedActor::<BlengActor>::new(kameo::mailbox::bounded(1));
        actor.handle_dispatch_result(
            a,
            Err(blehub::CommandDispatchError::InvalidState {
                detail: "test dispatch rejection".into(),
            }),
            prepared.actor_ref().clone(),
        );
        assert_eq!(*receipt_a.borrow(), Some(Ok(())));
        assert_eq!(result_a.try_recv(), Ok(Err(WaiterError::Cancelled)));
        if let Some((b, _result_b, receipt_b)) = sibling {
            assert!(
                drain(&mut rx).is_empty(),
                "a sibling control still owns the scope hold"
            );
            actor.handle_cancellation_result(b, control(b, false));
            assert_eq!(*receipt_b.borrow(), Some(control(b, false)));
        }
        assert_eq!(
            drain(&mut rx),
            vec![event],
            "the scope releases exactly once after its last control"
        );
        assert!(lifecycle.is_open());
    }
}

#[test]
fn completed_loss_connect_success_and_partial_evidence_after_rejected_control() {
    connect_result_after_rejected_control(&[0, 1, 2, 3]);
}

#[test]
fn completed_loss_connect_typed_failure_after_rejected_control() {
    connect_result_after_rejected_control(&[4]);
}

fn connect_result_after_rejected_control(stages: &[u8]) {
    for &stage in stages {
        let (mut actor, mut rx, lifecycle) = fixture();
        let (id, mut result) = reserve_connect(&mut actor, 20, 10);
        let completion = actor
            .reservations
            .test_connections()
            .completion_observer(id)
            .unwrap();
        let receipt = cancel(&mut actor, id);
        let error = blehub::BleEventError {
            category: blehub::BleErrorCategory::Connection,
            message: "original connect failure, not later loss".into(),
            platform_details: None,
        };
        let (mut events, expected) = match stage {
            0 => (vec![], None),
            1 => (vec![connected(id, 10.into())], None),
            2 => (vec![terminal(id)], None),
            3 => (
                vec![connected(id, 10.into()), terminal(id)],
                Some(Ok(WaiterSuccess::Connected(10.into()))),
            ),
            _ => (
                vec![
                    BleEvent::ConnectionStateChanged {
                        command_id: Some(id),
                        connection_id: 10.into(),
                        result: ConnectionStateResult::Err {
                            error: error.clone(),
                        },
                    },
                    BleEvent::CommandCompleted {
                        command_id: id,
                        error: Some(error.clone()),
                    },
                ],
                Some(Err(WaiterError::Operation(error))),
            ),
        };
        for event in &events {
            actor.handle_ingress_event(event.clone());
        }
        assert_eq!(completion.is_completed(), expected.is_some());
        assert_eq!(result.try_recv(), Err(oneshot::error::TryRecvError::Empty));
        let loss = lost(10.into());
        actor.handle_ingress_event(loss.clone());
        events.push(loss);
        assert_eq!(
            actor
                .reservations
                .test_connections()
                .validate_rssi(10.into()),
            Err(WaiterError::ConnectionNotActive)
        );
        if expected.is_some() {
            assert_eq!(
                result.try_recv(),
                Err(oneshot::error::TryRecvError::Empty),
                "stage={stage}: completed result must survive loss while its receipt is pending"
            );
        } else {
            assert_eq!(
                result.try_recv(),
                Ok(Err(WaiterError::Disconnected)),
                "stage={stage}: partial evidence must not defer loss until the receipt"
            );
        }
        // Late completion cannot revive loss-first or replace the original failure.
        for event in [connected(id, 10.into()), terminal(id)] {
            actor.handle_ingress_event(event.clone());
            events.push(event);
        }
        assert!(drain(&mut rx).is_empty());
        assert!(receipt.borrow().is_none());
        actor.handle_cancellation_result(id, control(id, false));
        assert_eq!(*receipt.borrow(), Some(control(id, false)));
        if let Some(expected) = expected {
            assert_eq!(result.try_recv(), Ok(expected), "stage={stage}");
        }
        assert_eq!(drain(&mut rx), events);
        assert_eq!(
            actor
                .reservations
                .test_connections()
                .require_peripheral_available(20.into()),
            Ok(())
        );
        actor.handle_cancellation_result(id, control(id, false));
        assert!(drain(&mut rx).is_empty());
        assert_eq!(result.try_recv(), Err(oneshot::error::TryRecvError::Closed));
        assert!(lifecycle.is_open());
    }
}

#[test]
fn completed_loss_read_success_and_partial_evidence_fail_unfinished_sibling_immediately() {
    read_result_after_rejected_control(&[0, 1, 2, 3]);
}

#[test]
fn completed_loss_read_typed_failure_fails_unfinished_sibling_immediately() {
    read_result_after_rejected_control(&[4]);
}

fn read_result_after_rejected_control(stages: &[u8]) {
    for &stage in stages {
        let (mut actor, mut rx, lifecycle) = fixture();
        ready_connection(&mut actor);
        drain(&mut rx);
        let (id, mut result) = reserve_read(&mut actor, 40);
        let (_, mut sibling_result) = reserve_read(&mut actor, 41);
        let completion = actor
            .reservations
            .test_connections()
            .completion_observer(id)
            .unwrap();
        let receipt = cancel(&mut actor, id);
        let error = blehub::BleEventError {
            category: blehub::BleErrorCategory::Permission,
            message: "original read failure, not later loss".into(),
            platform_details: None,
        };
        let domain = BleEvent::CharacteristicRead {
            command_id: id,
            connection_id: 10.into(),
            gatt_session_id: 30.into(),
            characteristic_id: 40.into(),
            value: vec![0, 255, 128],
        };
        let (mut events, expected) = match stage {
            0 => (vec![], None),
            1 => (vec![domain.clone()], None),
            2 => (vec![terminal(id)], None),
            3 => (
                vec![domain.clone(), terminal(id)],
                Some(Ok(WaiterSuccess::CharacteristicRead {
                    connection_id: 10.into(),
                    gatt_session_id: 30.into(),
                    characteristic_id: 40.into(),
                    value: vec![0, 255, 128],
                })),
            ),
            _ => (
                vec![BleEvent::CommandCompleted {
                    command_id: id,
                    error: Some(error.clone()),
                }],
                Some(Err(WaiterError::Operation(error))),
            ),
        };
        for event in &events {
            actor.handle_ingress_event(event.clone());
        }
        assert_eq!(completion.is_completed(), expected.is_some());
        assert_eq!(result.try_recv(), Err(oneshot::error::TryRecvError::Empty));
        let loss = lost(10.into());
        actor.handle_ingress_event(loss.clone());
        events.push(loss);
        assert_eq!(
            sibling_result.try_recv(),
            Ok(Err(WaiterError::Disconnected))
        );
        assert_eq!(
            actor
                .reservations
                .test_connections()
                .validate_rssi(10.into()),
            Err(WaiterError::ConnectionNotActive)
        );
        if expected.is_some() {
            assert_eq!(
                result.try_recv(),
                Err(oneshot::error::TryRecvError::Empty),
                "stage={stage}: completed result must survive loss while its receipt is pending"
            );
        } else {
            assert_eq!(
                result.try_recv(),
                Ok(Err(WaiterError::Disconnected)),
                "stage={stage}: partial evidence must not defer loss until the receipt"
            );
        }
        for event in [domain, terminal(id)] {
            actor.handle_ingress_event(event.clone());
            events.push(event);
        }
        assert!(drain(&mut rx).is_empty());
        assert!(receipt.borrow().is_none());
        let rejection = Err(WaiterError::OperationNotCancellable(id));
        actor.handle_cancellation_result(id, rejection.clone());
        assert_eq!(*receipt.borrow(), Some(rejection.clone()));
        if let Some(expected) = expected {
            assert_eq!(result.try_recv(), Ok(expected), "stage={stage}");
        }
        assert_eq!(drain(&mut rx), events);
        assert_eq!(
            actor
                .reservations
                .test_connections()
                .require_peripheral_available(20.into()),
            Ok(())
        );
        actor.handle_cancellation_result(id, rejection);
        assert!(drain(&mut rx).is_empty());
        assert_eq!(result.try_recv(), Err(oneshot::error::TryRecvError::Closed));
        assert_eq!(
            sibling_result.try_recv(),
            Err(oneshot::error::TryRecvError::Closed)
        );
        assert!(lifecycle.is_open());
    }
}

#[test]
fn cancel_loss_cancelled_terminal_before_loss_preserves_target_without_delaying_siblings() {
    for terminal_first in [true, false] {
        let (mut actor, mut rx, lifecycle) = fixture();
        ready_connection(&mut actor);
        drain(&mut rx);
        let (target, mut target_result) = reserve_read(&mut actor, 40);
        let (sibling, mut sibling_result) = reserve_read(&mut actor, 41);
        let receipt = cancel(&mut actor, target);
        let cancelled = BleEvent::CommandCompleted {
            command_id: target,
            error: Some(blehub::BleEventError {
                category: blehub::BleErrorCategory::Cancelled,
                message: "native cancellation won before its control receipt".into(),
                platform_details: None,
            }),
        };
        let loss = lost(10.into());
        let value = notification(10.into());
        let events = if terminal_first {
            vec![cancelled, value, loss]
        } else {
            vec![loss, value, cancelled]
        };
        for event in &events {
            actor.handle_ingress_event(event.clone());
            if matches!(event, BleEvent::ConnectionStateChanged { .. }) {
                assert_eq!(
                    sibling_result.try_recv(),
                    Ok(Err(WaiterError::Disconnected)),
                    "loss must settle an unfinished sibling before the next callback or receipt"
                );
                assert_eq!(
                    actor
                        .reservations
                        .test_connections()
                        .validate_rssi(10.into()),
                    Err(WaiterError::ConnectionNotActive),
                    "pending cancellation must not postpone recording connection loss"
                );
                if terminal_first {
                    assert_eq!(
                        target_result.try_recv(),
                        Err(oneshot::error::TryRecvError::Empty),
                        "loss must not overwrite an already observed cancellation terminal"
                    );
                } else {
                    assert_eq!(
                        target_result.try_recv(),
                        Ok(Err(WaiterError::Disconnected)),
                        "loss first must settle the cancelling target before its terminal or receipt"
                    );
                }
            }
        }
        assert!(receipt.borrow().is_none());
        assert!(drain(&mut rx).is_empty());
        actor.handle_cancellation_result(target, Ok(()));
        assert_eq!(*receipt.borrow(), Some(Ok(())));
        if terminal_first {
            assert_eq!(target_result.try_recv(), Ok(Err(WaiterError::Cancelled)));
        }
        assert_eq!(
            drain(&mut rx),
            events,
            "public evidence retains its FIFO order"
        );
        assert_eq!(
            actor
                .reservations
                .test_connections()
                .require_peripheral_available(20.into()),
            Ok(())
        );
        actor.handle_cancellation_result(target, Ok(()));
        assert!(drain(&mut rx).is_empty());
        assert_eq!(
            target_result.try_recv(),
            Err(oneshot::error::TryRecvError::Closed)
        );
        assert_eq!(
            sibling_result.try_recv(),
            Err(oneshot::error::TryRecvError::Closed)
        );
        assert!(
            actor
                .reservations
                .test_connections()
                .request_cancellation(sibling)
                .is_none()
        );
        assert!(lifecycle.is_open());
    }
}

#[test]
fn cancel_loss_completed_disconnect_is_completed_while_sibling_control_retains_root() {
    for terminal_first in [false, true] {
        let (mut actor, mut rx, lifecycle) = fixture();
        ready_connection(&mut actor);
        drain(&mut rx);
        let (read, mut read_result) = reserve_read(&mut actor, 40);
        let receipt = cancel(&mut actor, read);
        assert_eq!(
            actor
                .reservations
                .test_connections()
                .validate_disconnect(10.into()),
            Ok(20.into())
        );
        let (disconnect, mut disconnect_result) = actor
            .register_waiter(WaiterExpectation::Disconnected(10.into()))
            .unwrap();
        actor
            .reservations
            .test_connections()
            .reserve_disconnect(20.into(), disconnect);
        actor
            .reservations
            .test_connections()
            .resolve_dispatch(disconnect, &Ok(()))
            .unwrap();
        let prepared = kameo::actor::PreparedActor::<BlengActor>::new(kameo::mailbox::bounded(1));
        let request_cancel = |actor: &mut BlengActor| {
            actor.request_operation_cancellation(disconnect, prepared.actor_ref().clone())
        };
        assert!(matches!(
            request_cancel(&mut actor),
            Err(WaiterError::OperationNotCancellable(id)) if id == disconnect
        ));
        let completion = actor
            .reservations
            .test_connections()
            .completion_observer(disconnect)
            .unwrap();
        let domain = BleEvent::ConnectionStateChanged {
            command_id: Some(disconnect),
            connection_id: 10.into(),
            result: ConnectionStateResult::Ok {
                state: ConnectionState::Disconnected,
            },
        };
        let events = if terminal_first {
            vec![terminal(disconnect), domain]
        } else {
            vec![domain, terminal(disconnect)]
        };
        actor.handle_ingress_event(events[0].clone());
        assert_eq!(
            disconnect_result.try_recv(),
            Err(oneshot::error::TryRecvError::Empty)
        );
        assert!(matches!(
            request_cancel(&mut actor),
            Err(WaiterError::OperationNotCancellable(id)) if id == disconnect
        ));
        actor.handle_ingress_event(events[1].clone());
        assert_eq!(
            disconnect_result.try_recv(),
            Ok(Ok(WaiterSuccess::Disconnected(10.into())))
        );
        assert_eq!(read_result.try_recv(), Ok(Err(WaiterError::Disconnected)));
        assert!(completion.is_completed());
        assert!(receipt.borrow().is_none());
        assert_eq!(
            actor
                .reservations
                .test_connections()
                .require_peripheral_available(20.into()),
            Err(WaiterError::PeripheralBusy),
            "the unresolved sibling receipt still owns the retired connection root"
        );
        assert_eq!(
            request_cancel(&mut actor).err(),
            Some(WaiterError::OperationAlreadyCompleted(disconnect))
        );
        assert!(drain(&mut rx).is_empty());
        actor.handle_cancellation_result(read, control(read, false));
        assert_eq!(*receipt.borrow(), Some(control(read, false)));
        assert_eq!(drain(&mut rx), events);
        assert_eq!(
            actor
                .reservations
                .test_connections()
                .require_peripheral_available(20.into()),
            Ok(())
        );
        actor.handle_cancellation_result(read, control(read, false));
        assert!(drain(&mut rx).is_empty());
        assert_eq!(
            disconnect_result.try_recv(),
            Err(oneshot::error::TryRecvError::Closed)
        );
        assert_eq!(
            read_result.try_recv(),
            Err(oneshot::error::TryRecvError::Closed)
        );
        assert!(lifecycle.is_open());
    }
}

#[test]
fn publication_order_connection_has_one_aggregate_64_event_bound() {
    for uncorrelated in [false, true] {
        let (mut actor, mut rx, lifecycle) = fixture();
        ready_connection(&mut actor);
        drain(&mut rx);
        let (a, mut result_a) = reserve_read(&mut actor, 40);
        let (b, mut result_b) = reserve_read(&mut actor, 41);
        let _receipt_a = cancel(&mut actor, a);
        let _receipt_b = cancel(&mut actor, b);
        for index in 0..64 {
            actor.handle_ingress_event(if uncorrelated {
                notification(10.into())
            } else if index % 2 == 0 {
                read(a, 40)
            } else {
                read(b, 41)
            });
        }
        assert!(lifecycle.is_open());
        actor.handle_ingress_event(if uncorrelated {
            notification(10.into())
        } else {
            read(a, 40)
        });
        assert_eq!(
            lifecycle.phase(),
            RuntimePhase::Faulted,
            "the cap is per connection, not 64 per pending command"
        );
        assert!(drain(&mut rx).is_empty());
        drop(actor);
        assert_eq!(result_a.try_recv(), Ok(Err(WaiterError::TransportFault)));
        assert_eq!(result_b.try_recv(), Ok(Err(WaiterError::TransportFault)));
    }
}

#[test]
fn publication_order_scan_has_a_64_event_bound_including_passive_stop() {
    let (mut actor, mut rx, lifecycle) = fixture();
    let (id, mut result) = actor
        .register_waiter(WaiterExpectation::ScanStarted(1.into()))
        .unwrap();
    actor
        .reservations
        .test_scan()
        .reserve_scan_start(id, 1.into());
    actor
        .reservations
        .test_scan()
        .resolve_dispatch(id, &Ok(()))
        .unwrap();
    assert!(matches!(
        actor.reservations.test_scan().request_cancellation(id),
        Some(CancellationDecision::Accepted(_))
    ));
    assert!(
        actor
            .reservations
            .test_scan()
            .take_cancellation_dispatch(id)
    );
    for _ in 0..64 {
        actor.handle_ingress_event(stopped());
    }
    assert!(lifecycle.is_open());
    actor.handle_ingress_event(stopped());
    assert_eq!(lifecycle.phase(), RuntimePhase::Faulted);
    assert!(drain(&mut rx).is_empty());
    drop(actor);
    assert_eq!(result.try_recv(), Ok(Err(WaiterError::TransportFault)));
}
