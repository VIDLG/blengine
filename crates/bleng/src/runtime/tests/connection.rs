use super::support::*;

#[test]
fn disconnect_requires_exact_domain_and_terminal_then_allows_reconnect() {
    for terminal_first in [false, true] {
        let owner = RuntimeOwner::new_assuming_initialized().unwrap();
        let mut events = vec![
            disconnected_event(Some(99), 2),
            disconnected_event(None, 99),
            command_completed(99, None),
        ];
        let matching_domain = disconnected_event(Some(3), 2);
        let matching_terminal = command_completed(3, None);
        if terminal_first {
            events.extend([matching_terminal, matching_domain]);
        } else {
            events.extend([matching_domain, matching_terminal]);
        }
        let (backend, handle) = FakeBackend::scripted([
            connect_scenario(1, 2, 10),
            EventScenario::emit_before_return(disconnect_command(3, 2), events),
            connect_scenario(4, 5, 10),
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
            assert_eq!(connection, ConnectionId { value: 2 });
            assert_eq!(
                owner.disconnect(connection, Duration::from_secs(2)).await,
                Ok(())
            );
            assert_eq!(
                owner
                    .connect(PeripheralId { value: 10 }, Duration::from_secs(2))
                    .await,
                Ok(ConnectionId { value: 5 })
            );
            owner.close().await.unwrap();
        });
        assert_eq!(handle.commands().len(), 3);
        assert_eq!(handle.dispose_count(), 1);
    }
}

#[test]
fn disconnect_terminal_failure_preserves_active_connection_for_retry() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let error = BleEventError {
        category: BleErrorCategory::Connection,
        message: "disconnect failed".to_owned(),
        platform_details: None,
    };
    let (backend, handle) = FakeBackend::scripted([
        connect_scenario(1, 2, 10),
        EventScenario::emit_before_return(
            disconnect_command(3, 2),
            vec![command_completed(3, Some(error.clone()))],
        ),
        EventScenario::emit_before_return(
            disconnect_command(4, 2),
            vec![disconnected_event(Some(4), 2), command_completed(4, None)],
        ),
        connect_scenario(5, 6, 10),
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
            owner.disconnect(connection, Duration::from_secs(2)).await,
            Err(WaiterError::Operation(error))
        );
        assert_eq!(
            owner.disconnect(connection, Duration::from_secs(2)).await,
            Ok(())
        );
        assert_eq!(
            owner
                .connect(PeripheralId { value: 10 }, Duration::from_secs(2))
                .await,
            Ok(ConnectionId { value: 6 })
        );
        owner.close().await.unwrap();
    });
    assert_eq!(handle.commands().len(), 4);
    assert_eq!(handle.dispose_count(), 1);
}

#[test]
fn disconnect_pre_dispatch_callbacks_and_rejection_fail_closed() {
    let rejection = CommandDispatchError::InvalidState {
        detail: "disconnect rejected after callback".to_owned(),
    };
    for terminal_first in [false, true] {
        let owner = RuntimeOwner::new_assuming_initialized().unwrap();
        let domain = disconnected_event(Some(3), 2);
        let terminal = command_completed(3, None);
        let events = if terminal_first {
            vec![terminal, domain]
        } else {
            vec![domain, terminal]
        };
        let (backend, handle) = FakeBackend::scripted([
            connect_scenario(1, 2, 10),
            EventScenario::emit_before_reject(disconnect_command(3, 2), events, rejection.clone()),
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
                owner.disconnect(connection, Duration::from_secs(2)).await,
                Err(WaiterError::TransportFault)
            );
            assert!(owner.faulted_for_test());
            owner.close().await.unwrap();
        });
        assert_eq!(handle.commands().len(), 2);
        assert_eq!(handle.dispose_count(), 1);
    }
}

#[test]
fn disconnect_rejection_without_callbacks_preserves_active_connection_for_retry() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let rejection = CommandDispatchError::InvalidState {
        detail: "disconnect rejected without callbacks".to_owned(),
    };
    let (backend, handle) = FakeBackend::scripted([
        connect_scenario(1, 2, 10),
        EventScenario::reject(disconnect_command(3, 2), rejection.clone()),
        EventScenario::emit_before_return(
            disconnect_command(4, 2),
            vec![disconnected_event(Some(4), 2), command_completed(4, None)],
        ),
        connect_scenario(5, 6, 10),
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
            owner.disconnect(connection, Duration::from_secs(2)).await,
            Err(WaiterError::DispatchRejected(rejection))
        );
        assert_eq!(
            owner.disconnect(connection, Duration::from_secs(2)).await,
            Ok(())
        );
        assert_eq!(
            owner
                .connect(PeripheralId { value: 10 }, Duration::from_secs(2))
                .await,
            Ok(ConnectionId { value: 6 })
        );
        owner.close().await.unwrap();
    });
    assert_eq!(handle.commands().len(), 4);
    assert_eq!(handle.dispose_count(), 1);
}

#[test]
fn passive_disconnect_before_rejected_disconnect_retires_connection_for_reconnect() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let rejection = CommandDispatchError::InvalidState {
        detail: "disconnect rejected after passive loss".to_owned(),
    };
    let (backend, handle) = FakeBackend::scripted([
        connect_scenario(1, 2, 10),
        EventScenario::emit_before_reject(
            disconnect_command(3, 2),
            vec![disconnected_event(None, 2)],
            rejection.clone(),
        ),
        connect_scenario(4, 5, 10),
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
            owner.disconnect(connection, Duration::from_secs(2)).await,
            Err(WaiterError::DispatchRejected(rejection))
        );
        assert_eq!(
            owner
                .connect(PeripheralId { value: 10 }, Duration::from_secs(2))
                .await,
            Ok(ConnectionId { value: 5 })
        );
        owner.close().await.unwrap();
    });
    assert_eq!(handle.commands().len(), 3);
    assert_eq!(handle.dispose_count(), 1);
}

#[test]
fn disconnecting_pending_connect_retires_connect_waiter_and_allows_reconnect() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let (backend, handle) = FakeBackend::scripted([
        EventScenario::accept(RecordedCommand::Connect(ConnectCommand {
            command_id: CommandId { value: 1 },
            connection_id: ConnectionId { value: 2 },
            peripheral_id: PeripheralId { value: 10 },
        })),
        EventScenario::accept(disconnect_command(3, 2)),
        connect_scenario(4, 5, 10),
    ]);
    owner
        .attach_hub(BleHub::new(Arc::new(backend), owner.event_sink()).unwrap())
        .unwrap();
    let host = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    host.block_on(async {
        {
            let connect = owner.connect(PeripheralId { value: 10 }, Duration::from_secs(2));
            tokio::pin!(connect);
            tokio::select! {
                result = &mut connect => panic!("connect unexpectedly completed: {result:?}"),
                _ = tokio::time::sleep(Duration::from_millis(20)) => {}
            }
            {
                let disconnect = owner.disconnect(ConnectionId { value: 2 }, Duration::from_secs(2));
                tokio::pin!(disconnect);
                tokio::select! {
                    result = &mut disconnect => panic!("disconnect unexpectedly completed: {result:?}"),
                    _ = tokio::time::sleep(Duration::from_millis(20)) => {}
                }
                assert_eq!(
                    handle.emit(disconnected_event(Some(3), 2)),
                    EventIngressStatus::Delivered
                );
                assert_eq!(
                    handle.emit(command_completed(3, None)),
                    EventIngressStatus::Delivered
                );
                assert_eq!(disconnect.as_mut().await, Ok(()));
            }
            // This retires the waiter only; the test makes no native cancellation claim.
            assert_eq!(connect.as_mut().await, Err(WaiterError::Disconnected));
        }
        assert_eq!(
            owner
                .connect(PeripheralId { value: 10 }, Duration::from_secs(2))
                .await,
            Ok(ConnectionId { value: 5 })
        );
        owner.close().await.unwrap();
    });
    assert_eq!(handle.commands().len(), 3);
    assert_eq!(handle.dispose_count(), 1);
}

#[test]
fn passive_disconnect_before_rejected_read_gatt_rssi_or_write_preserves_rejection() {
    let host = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    for kind in ["gatt", "read", "rssi", "write"] {
        let rejection = CommandDispatchError::InvalidState {
            detail: format!("{kind} rejected after passive disconnect"),
        };
        let (scenarios, expected_connection, expected_commands) = match kind {
            "gatt" => (
                vec![
                    connect_scenario(1, 2, 10),
                    EventScenario::emit_before_reject(
                        RecordedCommand::DiscoverGatt(DiscoverGattCommand {
                            command_id: CommandId { value: 3 },
                            connection_id: ConnectionId { value: 2 },
                            gatt_session_id: GattSessionId { value: 4 },
                        }),
                        vec![disconnected_event(None, 2)],
                        rejection.clone(),
                    ),
                    connect_scenario(5, 6, 10),
                ],
                ConnectionId { value: 6 },
                3,
            ),
            "read" => (
                vec![
                    connect_scenario(1, 2, 10),
                    read_discovery(),
                    EventScenario::emit_before_reject(
                        read_command(5),
                        vec![disconnected_event(None, 2)],
                        rejection.clone(),
                    ),
                    connect_scenario(6, 7, 10),
                ],
                ConnectionId { value: 7 },
                4,
            ),
            "rssi" => (
                vec![
                    connect_scenario(1, 2, 10),
                    EventScenario::emit_before_reject(
                        rssi_command(3, 2),
                        vec![disconnected_event(None, 2)],
                        rejection.clone(),
                    ),
                    connect_scenario(4, 5, 10),
                ],
                ConnectionId { value: 5 },
                3,
            ),
            "write" => (
                vec![
                    connect_scenario(1, 2, 10),
                    read_discovery(),
                    EventScenario::emit_before_reject(
                        write_command(5, &[1], WriteType::WithResponse),
                        vec![disconnected_event(None, 2)],
                        rejection.clone(),
                    ),
                    connect_scenario(6, 7, 10),
                ],
                ConnectionId { value: 7 },
                4,
            ),
            _ => unreachable!(),
        };
        let owner = RuntimeOwner::new_assuming_initialized().unwrap();
        let (backend, handle) = FakeBackend::scripted(scenarios);
        owner
            .attach_hub(BleHub::new(Arc::new(backend), owner.event_sink()).unwrap())
            .unwrap();
        host.block_on(async {
            let connection = owner
                .connect(PeripheralId { value: 10 }, Duration::from_secs(2))
                .await
                .unwrap();
            let result = match kind {
                "gatt" => owner
                    .discover_gatt_session(connection, Duration::from_secs(2))
                    .await
                    .map(|_| ()),
                "read" => {
                    let session = owner
                        .discover_gatt_session(connection, Duration::from_secs(2))
                        .await
                        .unwrap();
                    owner
                        .read_characteristic(
                            connection,
                            session,
                            GattCharacteristicId { value: 30 },
                            Duration::from_secs(2),
                        )
                        .await
                        .map(|_| ())
                }
                "rssi" => owner
                    .read_rssi(connection, Duration::from_secs(2))
                    .await
                    .map(|_| ()),
                "write" => {
                    let session = owner
                        .discover_gatt_session(connection, Duration::from_secs(2))
                        .await
                        .unwrap();
                    owner
                        .write_characteristic(
                            connection,
                            session,
                            GattCharacteristicId { value: 30 },
                            vec![1],
                            WriteType::WithResponse,
                            Duration::from_secs(2),
                        )
                        .await
                }
                _ => unreachable!(),
            };
            assert_eq!(
                result,
                Err(WaiterError::DispatchRejected(rejection.clone()))
            );
            assert!(!owner.faulted_for_test());
            assert_eq!(
                owner
                    .connect(PeripheralId { value: 10 }, Duration::from_secs(2))
                    .await,
                Ok(expected_connection)
            );
            owner.close().await.unwrap();
        });
        assert_eq!(handle.commands().len(), expected_commands);
        assert_eq!(handle.dispose_count(), 1);
    }
}

#[test]
fn disconnect_rejects_while_sibling_rssi_dispatch_is_pending() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let (backend, handle) = FakeBackend::scripted([
        connect_scenario(1, 2, 10),
        EventScenario::accept(rssi_command(3, 2)),
    ]);
    let entered = Arc::new(Notify::new());
    let finished = Arc::new(Notify::new());
    let (release_sender, release_receiver) = mpsc::channel();
    let backend = BlockingDispatchBackend {
        inner: backend,
        blocked: BlockedDispatch::Rssi,
        entered: Arc::clone(&entered),
        release: Arc::new(Mutex::new(release_receiver)),
        finished: Arc::clone(&finished),
    };
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
                _ = entered.notified() => {}
            }
            assert_eq!(
                owner.disconnect(connection, Duration::from_secs(2)).await,
                Err(WaiterError::OperationBusy)
            );
            assert!(
                !handle
                    .commands()
                    .iter()
                    .any(|command| matches!(command, RecordedCommand::Disconnect(_)))
            );
            release_sender.send(()).unwrap();
            tokio::time::timeout(Duration::from_secs(2), finished.notified())
                .await
                .unwrap();
            assert_eq!(handle.commands().len(), 2);
        }
        owner.close().await.unwrap();
    });
    assert_eq!(handle.dispose_count(), 1);
}

#[test]
fn read_characteristic_rejects_while_sibling_rssi_dispatch_is_pending() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let (backend, handle) = FakeBackend::scripted([
        connect_scenario(1, 2, 10),
        read_discovery(),
        EventScenario::accept(rssi_command(5, 2)),
    ]);
    let entered = Arc::new(Notify::new());
    let finished = Arc::new(Notify::new());
    let (release_sender, release_receiver) = mpsc::channel();
    let backend = BlockingDispatchBackend {
        inner: backend,
        blocked: BlockedDispatch::Rssi,
        entered: Arc::clone(&entered),
        release: Arc::new(Mutex::new(release_receiver)),
        finished: Arc::clone(&finished),
    };
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
        let session = owner
            .discover_gatt_session(connection, Duration::from_secs(2))
            .await
            .unwrap();
        {
            let pending = owner.read_rssi(connection, Duration::from_secs(2));
            tokio::pin!(pending);
            tokio::select! {
                result = &mut pending => panic!("RSSI unexpectedly completed: {result:?}"),
                _ = entered.notified() => {}
            }
            assert_eq!(
                owner
                    .read_characteristic(
                        connection,
                        session,
                        GattCharacteristicId { value: 30 },
                        Duration::from_secs(2),
                    )
                    .await,
                Err(WaiterError::OperationBusy)
            );
            assert!(
                !handle
                    .commands()
                    .iter()
                    .any(|command| matches!(command, RecordedCommand::ReadCharacteristic(_)))
            );
            release_sender.send(()).unwrap();
            tokio::time::timeout(Duration::from_secs(2), finished.notified())
                .await
                .unwrap();
            assert_eq!(handle.commands().len(), 3);
        }
        owner.close().await.unwrap();
    });
    assert_eq!(handle.dispose_count(), 1);
}

#[test]
fn wrong_command_passive_disconnect_before_rejected_read_keeps_connection_active() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let rejection = CommandDispatchError::InvalidState {
        detail: "read rejected after wrong disconnect callback".to_owned(),
    };
    let (backend, handle) = FakeBackend::scripted([
        connect_scenario(1, 2, 10),
        read_discovery(),
        EventScenario::emit_before_reject(
            read_command(5),
            vec![disconnected_event(Some(99), 2)],
            rejection.clone(),
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
        let session = owner
            .discover_gatt_session(connection, Duration::from_secs(2))
            .await
            .unwrap();
        assert_eq!(
            owner
                .read_characteristic(
                    connection,
                    session,
                    GattCharacteristicId { value: 30 },
                    Duration::from_secs(2),
                )
                .await,
            Err(WaiterError::DispatchRejected(rejection))
        );
        assert!(!owner.faulted_for_test());
        assert_eq!(
            owner
                .connect(PeripheralId { value: 10 }, Duration::from_millis(20))
                .await,
            Err(WaiterError::PeripheralBusy)
        );
        owner.close().await.unwrap();
    });
    assert_eq!(handle.commands().len(), 3);
    assert_eq!(handle.dispose_count(), 1);
}

#[test]
fn disconnect_timeout_holds_connection_until_late_domain_and_terminal() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let (backend, handle) = FakeBackend::scripted([
        connect_scenario(1, 2, 10),
        EventScenario::accept(disconnect_command(3, 2)),
        connect_scenario(4, 5, 10),
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
            owner
                .disconnect(connection, Duration::from_millis(20))
                .await,
            Err(WaiterError::Timeout)
        );
        assert_eq!(
            owner
                .connect(PeripheralId { value: 10 }, Duration::from_millis(20))
                .await,
            Err(WaiterError::PeripheralBusy)
        );
        assert_eq!(
            handle.emit(command_completed(3, None)),
            EventIngressStatus::Delivered
        );
        assert_eq!(
            owner
                .connect(PeripheralId { value: 10 }, Duration::from_millis(20))
                .await,
            Err(WaiterError::PeripheralBusy)
        );
        assert_eq!(
            handle.emit(disconnected_event(Some(3), 2)),
            EventIngressStatus::Delivered
        );
        for _ in 0..2 {
            let _ = owner.next_event(Duration::from_secs(2)).await;
        }
        assert_eq!(
            owner
                .connect(PeripheralId { value: 10 }, Duration::from_secs(2))
                .await,
            Ok(ConnectionId { value: 5 })
        );
        owner.close().await.unwrap();
    });
    assert_eq!(handle.commands().len(), 3);
    assert_eq!(handle.dispose_count(), 1);
}

#[test]
fn passive_disconnect_racing_accepted_disconnect_waits_for_terminal_and_retires_other_waiters() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let (backend, handle) = FakeBackend::scripted([
        connect_scenario(1, 2, 10),
        EventScenario::accept(rssi_command(3, 2)),
        EventScenario::accept(disconnect_command(4, 2)),
        connect_scenario(5, 6, 10),
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
            let disconnect = owner.disconnect(connection, Duration::from_secs(2));
            tokio::pin!(disconnect);
            {
                let read = owner.read_rssi(connection, Duration::from_secs(2));
            tokio::pin!(read);
            tokio::select! {
                result = &mut read => panic!("RSSI unexpectedly completed: {result:?}"),
                _ = tokio::time::sleep(Duration::from_millis(20)) => {}
            }
            tokio::select! {
                result = &mut disconnect => panic!("disconnect unexpectedly completed: {result:?}"),
                _ = tokio::time::sleep(Duration::from_millis(20)) => {}
            }
            assert_eq!(handle.emit(disconnected_event(None, 2)), EventIngressStatus::Delivered);
                assert_eq!(read.as_mut().await, Err(WaiterError::Disconnected));
            }
            tokio::select! {
            result = &mut disconnect => panic!("disconnect completed without its terminal: {result:?}"),
            _ = tokio::time::sleep(Duration::from_millis(20)) => {}
        }
        assert_eq!(handle.emit(command_completed(4, None)), EventIngressStatus::Delivered);
            assert_eq!(disconnect.as_mut().await, Ok(()));
        }
        assert_eq!(
            owner
                .connect(PeripheralId { value: 10 }, Duration::from_secs(2))
                .await,
            Ok(ConnectionId { value: 6 })
        );
        owner.close().await.unwrap();
    });
    assert_eq!(handle.commands().len(), 4);
    assert_eq!(handle.dispose_count(), 1);
}

#[test]
fn disconnect_wrong_command_or_connection_cannot_finish_waiter() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let (backend, handle) = FakeBackend::scripted([
        connect_scenario(1, 2, 10),
        EventScenario::accept(disconnect_command(3, 2)),
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
            let disconnect = owner.disconnect(connection, Duration::from_secs(2));
            tokio::pin!(disconnect);
            tokio::select! {
            result = &mut disconnect => panic!("disconnect unexpectedly completed: {result:?}"),
            _ = tokio::time::sleep(Duration::from_millis(20)) => {}
        }
        assert_eq!(
            handle.emit(command_completed(99, None)),
            EventIngressStatus::Delivered
        );
        assert_eq!(
            handle.emit(disconnected_event(Some(99), 2)),
            EventIngressStatus::Delivered
        );
        assert_eq!(
            handle.emit(disconnected_event(None, 99)),
            EventIngressStatus::Delivered
        );
        tokio::select! {
            result = &mut disconnect => panic!("wrong callbacks completed disconnect: {result:?}"),
            _ = tokio::time::sleep(Duration::from_millis(20)) => {}
        }
        assert_eq!(
            handle.emit(disconnected_event(Some(3), 2)),
            EventIngressStatus::Delivered
        );
        assert_eq!(
            handle.emit(command_completed(3, None)),
            EventIngressStatus::Delivered
        );
            assert_eq!(disconnect.as_mut().await, Ok(()));
        }
        owner.close().await.unwrap();
    });
    assert_eq!(handle.commands().len(), 2);
    assert_eq!(handle.dispose_count(), 1);
}

#[test]
fn connect_keeps_peripheral_lifecycles_independent_and_retires_passive_disconnect() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let (backend, handle) = FakeBackend::scripted([
        connect_scenario(1, 2, 10),
        connect_scenario(3, 4, 20),
        connect_scenario(5, 6, 10),
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
            Ok(ConnectionId { value: 2 })
        );
        assert_eq!(
            owner
                .connect(PeripheralId { value: 10 }, Duration::from_millis(20))
                .await,
            Err(WaiterError::PeripheralBusy)
        );
        assert_eq!(
            owner
                .connect(PeripheralId { value: 20 }, Duration::from_secs(2))
                .await,
            Ok(ConnectionId { value: 4 })
        );
        let disconnected = BleEvent::ConnectionStateChanged {
            command_id: None,
            connection_id: ConnectionId { value: 2 },
            result: ConnectionStateResult::Ok {
                state: ConnectionState::Disconnected,
            },
        };
        assert_eq!(
            handle.emit(disconnected.clone()),
            EventIngressStatus::Delivered
        );
        for _ in 0..5 {
            let _ = owner.next_event(Duration::from_secs(2)).await;
        }
        assert_eq!(
            owner
                .connect(PeripheralId { value: 10 }, Duration::from_secs(2))
                .await,
            Ok(ConnectionId { value: 6 })
        );
        owner.close().await.unwrap();
    });
    assert_eq!(handle.commands().len(), 3);
    assert_eq!(handle.dispose_count(), 1);
}

#[test]
fn connect_failure_releases_peripheral_and_timeout_keeps_native_reservation() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let error = BleEventError {
        category: BleErrorCategory::Connection,
        message: "connection failed".to_owned(),
        platform_details: None,
    };
    let (backend, handle) = FakeBackend::scripted([
        EventScenario::emit_before_return(
            RecordedCommand::Connect(ConnectCommand {
                command_id: CommandId { value: 1 },
                connection_id: ConnectionId { value: 2 },
                peripheral_id: PeripheralId { value: 10 },
            }),
            vec![
                BleEvent::ConnectionStateChanged {
                    command_id: Some(CommandId { value: 1 }),
                    connection_id: ConnectionId { value: 2 },
                    result: ConnectionStateResult::Err {
                        error: error.clone(),
                    },
                },
                BleEvent::CommandCompleted {
                    command_id: CommandId { value: 1 },
                    error: Some(error.clone()),
                },
            ],
        ),
        connect_scenario(3, 4, 10),
        EventScenario::accept(RecordedCommand::Connect(ConnectCommand {
            command_id: CommandId { value: 5 },
            connection_id: ConnectionId { value: 6 },
            peripheral_id: PeripheralId { value: 20 },
        })),
        connect_scenario(7, 8, 20),
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
            Err(WaiterError::Operation(error))
        );
        assert_eq!(
            owner
                .connect(PeripheralId { value: 10 }, Duration::from_secs(2))
                .await,
            Ok(ConnectionId { value: 4 })
        );
        assert_eq!(
            owner
                .connect(PeripheralId { value: 20 }, Duration::from_millis(20))
                .await,
            Err(WaiterError::Timeout)
        );
        assert_eq!(
            owner
                .connect(PeripheralId { value: 20 }, Duration::from_millis(20))
                .await,
            Err(WaiterError::PeripheralBusy)
        );
        let disconnected = BleEvent::ConnectionStateChanged {
            command_id: None,
            connection_id: ConnectionId { value: 6 },
            result: ConnectionStateResult::Ok {
                state: ConnectionState::Disconnected,
            },
        };
        assert_eq!(handle.emit(disconnected), EventIngressStatus::Delivered);
        for _ in 0..5 {
            let _ = owner.next_event(Duration::from_secs(2)).await;
        }
        assert_eq!(
            owner
                .connect(PeripheralId { value: 20 }, Duration::from_secs(2))
                .await,
            Ok(ConnectionId { value: 8 })
        );
        owner.close().await.unwrap();
    });
    assert_eq!(handle.commands().len(), 4);
    assert_eq!(handle.dispose_count(), 1);
}

#[test]
fn connect_dispatch_rejection_with_callback_fails_closed() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let rejection = CommandDispatchError::InvalidState {
        detail: "connect rejected".to_owned(),
    };
    let (backend, handle) = FakeBackend::scripted([
        EventScenario::emit_before_reject(
            RecordedCommand::Connect(ConnectCommand {
                command_id: CommandId { value: 1 },
                connection_id: ConnectionId { value: 2 },
                peripheral_id: PeripheralId { value: 10 },
            }),
            vec![connected_event(1, 2)],
            rejection.clone(),
        ),
        connect_scenario(3, 4, 10),
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
            Err(WaiterError::TransportFault)
        );
        assert!(owner.faulted_for_test());
        owner.close().await.unwrap();
    });
    assert_eq!(handle.commands().len(), 1);
    assert_eq!(handle.dispose_count(), 1);
}

#[test]
fn cancelling_begin_connect_before_actor_handles_it_never_dispatches() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let (backend, handle) = FakeBackend::scripted([]);
    owner
        .attach_hub(BleHub::new(Arc::new(backend), owner.event_sink()).unwrap())
        .unwrap();
    let host = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    host.block_on(async {
        let entered = Arc::new(Notify::new());
        let release = Arc::new(Notify::new());
        owner
            .actor
            .tell(HoldActor {
                entered: Arc::clone(&entered),
                release: Arc::clone(&release),
            })
            .try_send()
            .unwrap();
        entered.notified().await;
        assert!(
            tokio::time::timeout(
                Duration::from_millis(20),
                owner.connect(PeripheralId { value: 10 }, Duration::from_secs(1))
            )
            .await
            .is_err()
        );
        release.notify_one();
        tokio::time::timeout(Duration::from_secs(2), owner.close())
            .await
            .unwrap()
            .unwrap();
    });
    assert_eq!(handle.commands().len(), 0);
    assert_eq!(handle.dispose_count(), 1);
}

#[test]
fn explicit_disconnect_completes_after_passive_error_and_allows_reconnect() {
    for terminal_first in [false, true] {
        let owner = RuntimeOwner::new_assuming_initialized().unwrap();
        let passive_error = BleEventError {
            category: BleErrorCategory::Connection,
            message: "peer disappeared during explicit disconnect".to_owned(),
            platform_details: None,
        };
        let domain = passive_disconnect_error_event(2, passive_error);
        let terminal = command_completed(3, None);
        let events = if terminal_first {
            vec![terminal, domain]
        } else {
            vec![domain, terminal]
        };
        let (backend, handle) = FakeBackend::scripted([
            connect_scenario(1, 2, 10),
            EventScenario::emit_before_return(disconnect_command(3, 2), events),
            connect_scenario(4, 5, 10),
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
                owner.disconnect(connection, Duration::from_secs(2)).await,
                Ok(())
            );
            assert_eq!(
                owner
                    .connect(PeripheralId { value: 10 }, Duration::from_secs(2))
                    .await,
                Ok(ConnectionId { value: 5 })
            );
            owner.close().await.unwrap();
        });
        assert_eq!(handle.commands().len(), 3);
        assert_eq!(handle.dispose_count(), 1);
    }
}
