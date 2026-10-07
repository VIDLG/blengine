use crate::waiter::{
    WaiterCloseReason, WaiterError, WaiterExpectation, WaiterRegistry, WaiterSuccess,
};
use blehub::{AdapterSnapshot, AdapterState, AuthorizationState, CommandId, ConnectionId};
use tokio::sync::oneshot::error::TryRecvError;

fn id(value: u64) -> CommandId {
    CommandId { value }
}

fn connection(value: u64) -> ConnectionId {
    ConnectionId { value }
}

#[test]
fn registry_delivers_one_explicit_result_and_ignores_late_results() {
    let mut registry = WaiterRegistry::new();
    let mut result_rx = registry
        .register(id(1), WaiterExpectation::Initialized)
        .unwrap();
    let success = Ok(WaiterSuccess::Initialized(AdapterSnapshot {
        state: AdapterState::PoweredOn,
        scan_authorization: AuthorizationState::Authorized,
        connection_authorization: AuthorizationState::Authorized,
        name: Some("adapter".to_owned()),
    }));

    registry.finish(id(1), success.clone());
    registry.finish(id(1), Err(WaiterError::TransportFault));

    assert_eq!(result_rx.try_recv(), Ok(success));
    assert_eq!(result_rx.try_recv(), Err(TryRecvError::Closed));
}

#[test]
fn registry_rejects_non_monotonic_command_ids_without_advancing_watermark() {
    let mut registry = WaiterRegistry::new();
    let _result_rx = registry
        .register(id(2), WaiterExpectation::Initialized)
        .unwrap();

    assert!(matches!(
        registry.register(id(1), WaiterExpectation::Initialized),
        Err(WaiterError::InvalidCommandId)
    ));
    let _result_rx = registry
        .register(id(3), WaiterExpectation::Initialized)
        .unwrap();
}

#[test]
fn registry_rejects_wrapped_and_reused_command_ids_after_maximum() {
    let mut registry = WaiterRegistry::new();
    let mut penultimate = registry
        .register(
            id(u64::MAX - 1),
            WaiterExpectation::Connected(connection(1)),
        )
        .unwrap();
    let mut last = registry
        .register(id(u64::MAX), WaiterExpectation::Connected(connection(2)))
        .unwrap();
    registry.cancel(id(u64::MAX - 1));
    registry.timeout(id(u64::MAX));
    assert_eq!(penultimate.try_recv(), Ok(Err(WaiterError::Cancelled)));
    assert_eq!(last.try_recv(), Ok(Err(WaiterError::Timeout)));

    for reused in [0, 1, u64::MAX - 1, u64::MAX] {
        assert!(matches!(
            registry.register(id(reused), WaiterExpectation::Connected(connection(3))),
            Err(WaiterError::InvalidCommandId)
        ));
    }
}

#[test]
fn registry_retires_only_connection_bound_waiters_after_passive_loss() {
    let mut registry = WaiterRegistry::new();
    let mut connected_rx = registry
        .register(id(1), WaiterExpectation::Connected(connection(10)))
        .unwrap();
    let mut explicit_disconnect_rx = registry
        .register(id(2), WaiterExpectation::Disconnected(connection(10)))
        .unwrap();
    let mut unrelated_rx = registry
        .register(id(3), WaiterExpectation::Initialized)
        .unwrap();

    registry.disconnect_commands([id(1), id(2), id(3)]);

    assert_eq!(connected_rx.try_recv(), Ok(Err(WaiterError::Disconnected)));
    assert_eq!(explicit_disconnect_rx.try_recv(), Err(TryRecvError::Empty));
    assert_eq!(unrelated_rx.try_recv(), Err(TryRecvError::Empty));

    registry.finish(id(2), Ok(WaiterSuccess::Disconnected(connection(10))));
    assert_eq!(
        explicit_disconnect_rx.try_recv(),
        Ok(Ok(WaiterSuccess::Disconnected(connection(10))))
    );
}

#[test]
fn cancellation_and_timeout_only_retire_the_waiter() {
    let mut registry = WaiterRegistry::new();
    let mut cancelled_rx = registry
        .register(id(1), WaiterExpectation::Initialized)
        .unwrap();
    let mut timed_out_rx = registry
        .register(id(2), WaiterExpectation::Initialized)
        .unwrap();

    registry.cancel(id(1));
    registry.timeout(id(2));

    assert_eq!(cancelled_rx.try_recv(), Ok(Err(WaiterError::Cancelled)));
    assert_eq!(timed_out_rx.try_recv(), Ok(Err(WaiterError::Timeout)));
    for retired in [id(1), id(2)] {
        assert!(matches!(
            registry.register(retired, WaiterExpectation::Initialized),
            Err(WaiterError::InvalidCommandId)
        ));
    }
    let mut next_rx = registry
        .register(id(3), WaiterExpectation::Initialized)
        .unwrap();
    registry.finish(id(1), Err(WaiterError::TransportFault));
    registry.finish(id(2), Err(WaiterError::TransportFault));
    assert_eq!(next_rx.try_recv(), Err(TryRecvError::Empty));
}

#[test]
fn close_fails_all_waiters_and_rejects_future_registration() {
    let mut registry = WaiterRegistry::new();
    let mut first_rx = registry
        .register(id(1), WaiterExpectation::Initialized)
        .unwrap();
    let mut second_rx = registry
        .register(id(2), WaiterExpectation::Initialized)
        .unwrap();

    registry.close(WaiterCloseReason::TransportFault);
    registry.close(WaiterCloseReason::Closed);

    assert_eq!(first_rx.try_recv(), Ok(Err(WaiterError::TransportFault)));
    assert_eq!(second_rx.try_recv(), Ok(Err(WaiterError::TransportFault)));
    assert!(matches!(
        registry.register(id(3), WaiterExpectation::Initialized),
        Err(WaiterError::TransportFault)
    ));
}

#[test]
fn dropped_receivers_are_reclaimed_without_reusing_ids() {
    let mut registry = WaiterRegistry::new();
    let result_rx = registry
        .register(id(1), WaiterExpectation::Initialized)
        .unwrap();
    drop(result_rx);

    let _result_rxs: Vec<_> = (2..=65)
        .map(|value| registry.register(id(value), WaiterExpectation::Initialized))
        .collect::<Result<_, _>>()
        .unwrap();
    assert!(matches!(
        registry.register(id(66), WaiterExpectation::Initialized),
        Err(WaiterError::CapacityExceeded)
    ));
}
