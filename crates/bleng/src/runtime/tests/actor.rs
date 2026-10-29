use super::BlengActor;
use crate::{
    runtime::{RuntimeLifecycle, shared::IdGenerator},
    waiter::{WaiterError, WaiterExpectation},
};
use blehub::{BleEvent, GattCharacteristicId, GattSessionId, PeripheralId};
use std::sync::Arc;

#[path = "publication.rs"]
mod publication;

#[test]
fn explicit_cancellation_retains_pre_dispatch_reservation_until_dispatch_resolves() {
    use tokio::sync::mpsc::channel;

    let lifecycle = Arc::new(RuntimeLifecycle::new());
    let (event_tx, _event_rx) = channel(1);
    let mut actor = BlengActor::with_ids(event_tx, lifecycle, Arc::new(IdGenerator::default()));
    let connection_id = 10.into();
    let peripheral_id = PeripheralId { value: 20 };
    let session_id = GattSessionId { value: 30 };
    let characteristic_id = GattCharacteristicId { value: 40 };

    actor
        .connections
        .reserve_connection(peripheral_id, 0.into(), connection_id);
    let (command_id, mut result_rx) = actor
        .register_waiter(WaiterExpectation::CharacteristicRead(
            connection_id,
            session_id,
            characteristic_id,
        ))
        .unwrap();
    actor
        .connections
        .reserve_read(peripheral_id, command_id, session_id, characteristic_id);

    let receiver = match actor.connections.request_cancellation(command_id) {
        Some(crate::runtime::reservations::CancellationDecision::Accepted(receiver)) => receiver,
        _ => panic!("unexpected cancellation decision"),
    };
    assert!(result_rx.try_recv().is_err());
    assert!(receiver.borrow().is_none());
    assert!(matches!(
        actor.connections.request_cancellation(command_id),
        Some(crate::runtime::reservations::CancellationDecision::AlreadyRequested(_))
    ));
    assert!(!actor.connections.take_cancellation_dispatch(command_id));
}

#[test]
fn cancellation_receipt_survives_cancelled_terminal_before_control_returns() {
    for terminal_first in [false, true] {
        cancellation_receipt_survives_early_terminal(Ok(()), terminal_first, true);
    }
}

#[test]
fn cancellation_rejection_preserves_read_completed_before_control_returns() {
    for terminal_first in [false, true] {
        cancellation_receipt_survives_early_terminal(
            Err(WaiterError::OperationAlreadyCompleted(1.into())),
            terminal_first,
            false,
        );
    }
}

#[test]
fn typed_boundary_pending_cancel_preserves_public_evidence_until_accept_or_reject() {
    for terminal_first in [false, true] {
        for accepted in [false, true] {
            cancellation_receipt_survives_early_terminal(
                if accepted {
                    Ok(())
                } else {
                    Err(WaiterError::OperationNotCancellable(1.into()))
                },
                terminal_first,
                false,
            );
        }
    }
}

fn cancellation_receipt_survives_early_terminal(
    control_result: Result<(), WaiterError>,
    terminal_first: bool,
    cancelled_terminal: bool,
) {
    use crate::{runtime::reservations::CancellationDecision, waiter::WaiterSuccess};
    use blehub::{BleErrorCategory, BleEventError, ConnectionState, ConnectionStateResult};
    use tokio::sync::{mpsc::channel, oneshot::error::TryRecvError};

    let lifecycle = Arc::new(RuntimeLifecycle::new());
    let (event_tx, mut event_rx) = channel(16);
    let mut actor = BlengActor::with_ids(
        event_tx,
        Arc::clone(&lifecycle),
        Arc::new(IdGenerator::default()),
    );
    let connection_id = 10.into();
    let peripheral_id = PeripheralId { value: 20 };
    let session_id = GattSessionId { value: 30 };
    let characteristic_id = GattCharacteristicId { value: 40 };
    actor
        .connections
        .reserve_connection(peripheral_id, 0.into(), connection_id);
    actor
        .connections
        .resolve_dispatch(0.into(), &Ok(()))
        .unwrap();
    actor.handle_ingress_event(BleEvent::ConnectionStateChanged {
        command_id: Some(0.into()),
        connection_id,
        result: ConnectionStateResult::Ok {
            state: ConnectionState::Connected,
        },
    });
    actor.handle_ingress_event(BleEvent::CommandCompleted {
        command_id: 0.into(),
        error: None,
    });

    let (command_id, mut result_rx) = actor
        .register_waiter(WaiterExpectation::CharacteristicRead(
            connection_id,
            session_id,
            characteristic_id,
        ))
        .unwrap();
    actor
        .connections
        .reserve_read(peripheral_id, command_id, session_id, characteristic_id);
    actor
        .connections
        .resolve_dispatch(command_id, &Ok(()))
        .unwrap();
    assert!(event_rx.try_recv().is_ok());
    assert!(event_rx.try_recv().is_ok());
    assert_eq!(
        event_rx.try_recv(),
        Err(tokio::sync::mpsc::error::TryRecvError::Empty)
    );
    let receipt = match actor.connections.request_cancellation(command_id) {
        Some(CancellationDecision::Accepted(receipt)) => receipt,
        _ => panic!("first cancellation must have its own receipt"),
    };
    assert!(actor.connections.take_cancellation_dispatch(command_id));
    let repeated_receipt = match actor.connections.request_cancellation(command_id) {
        Some(CancellationDecision::AlreadyRequested(receipt)) => receipt,
        _ => panic!("concurrent cancellation must share the receipt"),
    };
    assert!(!actor.connections.take_cancellation_dispatch(command_id));

    let domain = BleEvent::CharacteristicRead {
        command_id,
        connection_id,
        gatt_session_id: session_id,
        characteristic_id,
        value: vec![0xde, 0xad],
    };
    let terminal = BleEvent::CommandCompleted {
        command_id,
        error: cancelled_terminal.then(|| BleEventError {
            category: BleErrorCategory::Cancelled,
            message: "native cancellation won".to_owned(),
            platform_details: None,
        }),
    };
    if !terminal_first {
        actor.handle_ingress_event(domain.clone());
    }
    actor.handle_ingress_event(terminal.clone());
    if terminal_first {
        actor.handle_ingress_event(domain.clone());
    }
    actor.handle_ingress_event(terminal.clone());
    assert_eq!(result_rx.try_recv(), Err(TryRecvError::Empty));
    assert!(
        matches!(receipt.has_changed(), Ok(false)),
        "terminal must not close an unresolved cancellation receipt"
    );

    assert_eq!(
        event_rx.try_recv(),
        Err(tokio::sync::mpsc::error::TryRecvError::Empty),
        "pending control must not prematurely publish target evidence"
    );
    actor.handle_cancellation_result(command_id, control_result.clone());
    let expected_events = if control_result.is_ok() {
        vec![terminal.clone(), terminal.clone()]
    } else if terminal_first {
        vec![terminal.clone(), domain.clone(), terminal.clone()]
    } else {
        vec![domain.clone(), terminal.clone(), terminal.clone()]
    };
    for expected_event in expected_events {
        assert_eq!(
            event_rx.try_recv(),
            Ok(expected_event),
            "control={control_result:?}, terminal_first={terminal_first}"
        );
    }
    assert_eq!(
        event_rx.try_recv(),
        Err(tokio::sync::mpsc::error::TryRecvError::Empty),
        "control release must not duplicate events"
    );
    assert_eq!(*receipt.borrow(), Some(control_result.clone()));
    assert_eq!(*repeated_receipt.borrow(), Some(control_result.clone()));
    let expected = if control_result.is_ok() {
        Err(WaiterError::Cancelled)
    } else {
        Ok(WaiterSuccess::CharacteristicRead {
            connection_id,
            gatt_session_id: session_id,
            characteristic_id,
            value: vec![0xde, 0xad],
        })
    };
    assert_eq!(result_rx.try_recv(), Ok(expected));
    assert!(actor.connections.request_cancellation(command_id).is_none());
    assert_eq!(
        actor.connections.validate_gatt_discovery(connection_id),
        Ok(peripheral_id),
        "completed native reservation must retire after receipt settlement"
    );

    actor.handle_cancellation_result(command_id, control_result.clone());
    assert_eq!(
        event_rx.try_recv(),
        Err(tokio::sync::mpsc::error::TryRecvError::Empty),
        "duplicate control must not replay public evidence again"
    );
    actor.handle_ingress_event(domain);
    actor.handle_ingress_event(terminal);
    assert_eq!(result_rx.try_recv(), Err(TryRecvError::Closed));
    assert_eq!(*receipt.borrow(), Some(control_result));
    assert!(lifecycle.is_open());
}

#[test]
fn typed_boundary_pending_public_evidence_and_release_overflow_fail_closed() {
    use crate::runtime::reservations::CancellationDecision;
    use tokio::sync::mpsc::channel;
    for overflow_on_release in [false, true] {
        let lifecycle = Arc::new(RuntimeLifecycle::new());
        let (event_tx, mut event_rx) = channel(1);
        let mut actor = BlengActor::with_ids(
            event_tx,
            Arc::clone(&lifecycle),
            Arc::new(IdGenerator::default()),
        );
        let peripheral = PeripheralId { value: 20 };
        actor
            .connections
            .reserve_connection(peripheral, 0.into(), 10.into());
        let (command_id, mut result_rx) = actor
            .register_waiter(WaiterExpectation::CharacteristicRead(
                10.into(),
                30.into(),
                40.into(),
            ))
            .unwrap();
        actor
            .connections
            .reserve_read(peripheral, command_id, 30.into(), 40.into());
        actor
            .connections
            .resolve_dispatch(command_id, &Ok(()))
            .unwrap();
        let receipt = match actor.connections.request_cancellation(command_id) {
            Some(CancellationDecision::Accepted(receipt)) => receipt,
            _ => panic!("cancellation must be admitted"),
        };
        assert!(actor.connections.take_cancellation_dispatch(command_id));
        let domain = BleEvent::CharacteristicRead {
            command_id,
            connection_id: 10.into(),
            gatt_session_id: 30.into(),
            characteristic_id: 40.into(),
            value: vec![1],
        };
        if overflow_on_release {
            let unrelated = BleEvent::CommandCompleted {
                command_id: 99.into(),
                error: None,
            };
            actor.handle_ingress_event(unrelated.clone());
            actor.handle_ingress_event(domain);
            actor.handle_ingress_event(BleEvent::CommandCompleted {
                command_id,
                error: None,
            });
            assert!(lifecycle.is_open());
            actor.handle_cancellation_result(
                command_id,
                Err(WaiterError::OperationNotCancellable(command_id)),
            );
            assert_eq!(event_rx.try_recv(), Ok(unrelated));
        } else {
            for _ in 0..64 {
                actor.handle_ingress_event(domain.clone());
            }
            assert!(
                lifecycle.is_open(),
                "the connection publication scope admits 64 callbacks"
            );
            actor.handle_ingress_event(domain);
            assert!(receipt.borrow().is_none());
        }
        assert_eq!(lifecycle.phase(), crate::runtime::RuntimePhase::Faulted);
        assert_eq!(
            event_rx.try_recv(),
            Err(tokio::sync::mpsc::error::TryRecvError::Empty)
        );
        drop(actor);
        assert_eq!(
            result_rx.try_recv(),
            Ok(Err(WaiterError::TransportFault)),
            "overflow must never settle target success"
        );
    }
}

#[test]
fn event_publication_rejects_a_closed_or_faulted_gate_before_enqueueing() {
    use tokio::sync::mpsc::channel;

    for faulted in [false, true] {
        let lifecycle = Arc::new(RuntimeLifecycle::new());
        let (event_tx, mut event_rx) = channel(1);
        let actor = BlengActor::with_ids(
            event_tx,
            Arc::clone(&lifecycle),
            Arc::new(IdGenerator::default()),
        );
        if faulted {
            lifecycle.fault();
        } else {
            lifecycle.mark_closed();
        }
        assert_eq!(
            actor.publish_event(&BleEvent::CommandCompleted {
                command_id: 1.into(),
                error: None,
            }),
            Err(if faulted {
                WaiterError::TransportFault
            } else {
                WaiterError::Closed
            })
        );
        assert_eq!(
            event_rx.try_recv(),
            Err(tokio::sync::mpsc::error::TryRecvError::Empty)
        );
    }
}
