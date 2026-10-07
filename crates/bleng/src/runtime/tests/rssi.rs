use super::support::{
    Arc, BleEvent, BleHub, CommandDispatchError, ConnectionId, ConnectionState,
    ConnectionStateResult, Duration, EventIngressStatus, EventScenario, FakeBackend, PeripheralId,
    RuntimeOwner, WaiterError, connect_scenario, rssi_command, rssi_terminal, rssi_value,
};

#[test]
fn read_rssi_requires_exact_domain_and_terminal_in_any_order() {
    for terminal_first in [false, true] {
        let owner = RuntimeOwner::new_assuming_initialized().unwrap();
        let command = 2;
        let connection = 1;
        let domain = rssi_value(command, connection, -47);
        let terminal = rssi_terminal(command);
        let mut events = vec![
            rssi_value(command, 99, -90),
            rssi_value(99, connection, -91),
            rssi_terminal(99),
        ];
        if terminal_first {
            events.extend([terminal, domain]);
        } else {
            events.extend([domain, terminal]);
        }
        let (backend, handle) = FakeBackend::scripted([
            connect_scenario(1, connection, 10),
            EventScenario::emit_before_return(rssi_command(command, connection), events),
        ]);
        owner
            .attach_hub(BleHub::new(Arc::new(backend), owner.event_sink()).unwrap())
            .unwrap();
        let host = tokio::runtime::Builder::new_multi_thread()
            .enable_time()
            .build()
            .unwrap();
        host.block_on(async {
            assert_eq!(
                owner
                    .connect(PeripheralId { value: 10 }, Duration::from_secs(2))
                    .await,
                Ok(ConnectionId { value: connection })
            );
            assert_eq!(
                owner
                    .read_rssi(ConnectionId { value: connection }, Duration::from_secs(2))
                    .await,
                Ok(-47)
            );
            owner.close().await.unwrap();
        });
        assert_eq!(handle.commands().len(), 2);
        assert_eq!(handle.dispose_count(), 1);
    }
}

#[test]
fn read_rssi_synchronous_callbacks_before_rejected_dispatch_fail_closed() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let error = CommandDispatchError::InvalidState {
        detail: "rejected RSSI read after callback".to_owned(),
    };
    let (backend, handle) = FakeBackend::scripted([
        connect_scenario(1, 1, 10),
        EventScenario::emit_before_reject(
            rssi_command(2, 1),
            vec![rssi_value(2, 1, -48), rssi_terminal(2)],
            error.clone(),
        ),
    ]);
    owner
        .attach_hub(BleHub::new(Arc::new(backend), owner.event_sink()).unwrap())
        .unwrap();
    let host = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    host.block_on(async {
        assert_eq!(
            owner
                .connect(PeripheralId { value: 10 }, Duration::from_secs(2))
                .await,
            Ok(ConnectionId { value: 1 })
        );
        for _ in 0..2 {
            assert!(
                owner
                    .next_event(Duration::from_secs(2))
                    .await
                    .unwrap()
                    .is_some()
            );
        }
        assert_eq!(
            tokio::time::timeout(
                Duration::from_secs(2),
                owner.read_rssi(ConnectionId { value: 1 }, Duration::from_secs(1))
            )
            .await
            .unwrap(),
            Err(WaiterError::TransportFault)
        );
        assert!(owner.faulted_for_test());
        owner.close().await.unwrap();
    });
    assert_eq!(handle.commands().len(), 2);
    assert_eq!(handle.dispose_count(), 1);
}

#[test]
fn read_rssi_timeout_keeps_slot_until_late_completion_then_retries() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let (backend, handle) = FakeBackend::scripted([
        connect_scenario(1, 1, 10),
        EventScenario::accept(rssi_command(2, 1)),
        EventScenario::emit_before_return(
            rssi_command(3, 1),
            vec![rssi_value(3, 1, -52), rssi_terminal(3)],
        ),
    ]);
    owner
        .attach_hub(BleHub::new(Arc::new(backend), owner.event_sink()).unwrap())
        .unwrap();
    let host = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    host.block_on(async {
        let connection = owner
            .connect(PeripheralId { value: 10 }, Duration::from_secs(2))
            .await
            .unwrap();
        assert_eq!(
            owner.read_rssi(connection, Duration::from_millis(20)).await,
            Err(WaiterError::Timeout)
        );
        assert_eq!(
            owner.read_rssi(connection, Duration::from_secs(1)).await,
            Err(WaiterError::OperationBusy)
        );
        assert_eq!(
            handle.emit(rssi_value(2, 1, -53)),
            EventIngressStatus::Delivered
        );
        assert_eq!(handle.emit(rssi_terminal(2)), EventIngressStatus::Delivered);
        // Drain the connect and late RSSI events to establish that the late
        // native completion retired the timed-out reservation.
        for _ in 0..4 {
            let _ = owner.next_event(Duration::from_secs(2)).await;
        }
        assert_eq!(
            owner.read_rssi(connection, Duration::from_secs(2)).await,
            Ok(-52)
        );
        owner.close().await.unwrap();
    });
    assert_eq!(handle.commands().len(), 3);
    assert_eq!(handle.dispose_count(), 1);
}

#[test]
fn read_rssi_is_completed_as_disconnected_and_connection_can_be_reused() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let disconnected = BleEvent::ConnectionStateChanged {
        command_id: None,
        connection_id: ConnectionId { value: 1 },
        result: ConnectionStateResult::Ok {
            state: ConnectionState::Disconnected,
        },
    };
    let (backend, handle) = FakeBackend::scripted([
        connect_scenario(1, 1, 10),
        EventScenario::accept(rssi_command(2, 1)),
        connect_scenario(3, 2, 10),
    ]);
    owner
        .attach_hub(BleHub::new(Arc::new(backend), owner.event_sink()).unwrap())
        .unwrap();
    let host = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    host.block_on(async {
        let connection = owner
            .connect(PeripheralId { value: 10 }, Duration::from_secs(2))
            .await
            .unwrap();
        {
            let pending = owner.read_rssi(connection, Duration::from_secs(2));
            tokio::pin!(pending);
            tokio::select! {
                result = &mut pending => panic!("RSSI unexpectedly completed: {result:?}"),
                _ = tokio::time::sleep(Duration::from_millis(20)) => {}
            }
            assert_eq!(
                handle.emit(disconnected.clone()),
                EventIngressStatus::Delivered
            );
            assert_eq!(pending.await, Err(WaiterError::Disconnected));
        }
        assert_eq!(
            owner
                .connect(PeripheralId { value: 10 }, Duration::from_secs(2))
                .await,
            Ok(ConnectionId { value: 2 })
        );
        owner.close().await.unwrap();
    });
    assert_eq!(handle.commands().len(), 3);
    assert_eq!(handle.dispose_count(), 1);
}
