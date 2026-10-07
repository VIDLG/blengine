use super::*;
use blehub::{
    AdapterSnapshot, AdapterState, AuthorizationState, CommandDispatchError, ConnectionId,
};
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
    let mut receiver = registry
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

    assert_eq!(receiver.try_recv(), Ok(success));
    assert_eq!(receiver.try_recv(), Err(TryRecvError::Closed));
}

#[test]
fn registry_rejects_non_monotonic_command_ids_without_advancing_watermark() {
    let mut registry = WaiterRegistry::new();
    let _receiver = registry
        .register(id(2), WaiterExpectation::Initialized)
        .unwrap();

    assert!(matches!(
        registry.register(id(1), WaiterExpectation::Initialized),
        Err(WaiterError::InvalidCommandId)
    ));
    let _receiver = registry
        .register(id(3), WaiterExpectation::Initialized)
        .unwrap();
}

#[test]
fn registry_retires_only_connection_bound_waiters_after_passive_loss() {
    let mut registry = WaiterRegistry::new();
    let mut connected = registry
        .register(id(1), WaiterExpectation::Connected(connection(10)))
        .unwrap();
    let mut explicit_disconnect = registry
        .register(id(2), WaiterExpectation::Disconnected(connection(10)))
        .unwrap();
    let mut unrelated = registry
        .register(id(3), WaiterExpectation::Initialized)
        .unwrap();

    registry.disconnect_commands([id(1), id(2), id(3)]);

    assert_eq!(connected.try_recv(), Ok(Err(WaiterError::Disconnected)));
    assert_eq!(explicit_disconnect.try_recv(), Err(TryRecvError::Empty));
    assert_eq!(unrelated.try_recv(), Err(TryRecvError::Empty));

    registry.finish(id(2), Ok(WaiterSuccess::Disconnected(connection(10))));
    assert_eq!(
        explicit_disconnect.try_recv(),
        Ok(Ok(WaiterSuccess::Disconnected(connection(10))))
    );
}

#[test]
fn cancellation_and_timeout_only_retire_the_waiter() {
    let mut registry = WaiterRegistry::new();
    let mut cancelled = registry
        .register(id(1), WaiterExpectation::Initialized)
        .unwrap();
    let mut timed_out = registry
        .register(id(2), WaiterExpectation::Initialized)
        .unwrap();

    registry.cancel(id(1));
    registry.timeout(id(2));

    assert_eq!(cancelled.try_recv(), Ok(Err(WaiterError::Cancelled)));
    assert_eq!(timed_out.try_recv(), Ok(Err(WaiterError::Timeout)));
}

#[test]
fn close_fails_all_waiters_and_rejects_future_registration() {
    let mut registry = WaiterRegistry::new();
    let mut first = registry
        .register(id(1), WaiterExpectation::Initialized)
        .unwrap();
    let mut second = registry
        .register(id(2), WaiterExpectation::Initialized)
        .unwrap();

    registry.close(CloseReason::TransportFault);
    registry.close(CloseReason::Closed);

    assert_eq!(first.try_recv(), Ok(Err(WaiterError::TransportFault)));
    assert_eq!(second.try_recv(), Ok(Err(WaiterError::TransportFault)));
    assert!(matches!(
        registry.register(id(3), WaiterExpectation::Initialized),
        Err(WaiterError::TransportFault)
    ));
}

#[test]
fn dropped_receivers_are_reclaimed_without_reusing_ids() {
    let mut registry = WaiterRegistry::new();
    let receiver = registry
        .register(id(1), WaiterExpectation::Initialized)
        .unwrap();
    drop(receiver);

    let _receivers: Vec<_> = (2..=65)
        .map(|value| registry.register(id(value), WaiterExpectation::Initialized))
        .collect::<Result<_, _>>()
        .unwrap();
    assert!(matches!(
        registry.register(id(66), WaiterExpectation::Initialized),
        Err(WaiterError::CapacityExceeded)
    ));
}

#[test]
fn dispatch_rejection_is_a_normal_explicit_result() {
    let mut registry = WaiterRegistry::new();
    let mut receiver = registry
        .register(id(1), WaiterExpectation::Initialized)
        .unwrap();

    registry.finish(
        id(1),
        Err(WaiterError::DispatchRejected(CommandDispatchError::Closed)),
    );

    assert_eq!(
        receiver.try_recv(),
        Ok(Err(WaiterError::DispatchRejected(
            CommandDispatchError::Closed
        )))
    );
}
