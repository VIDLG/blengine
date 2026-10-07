use super::support::*;

#[test]
fn discover_gatt_binds_session_to_connection_and_disconnect_retires_it() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let (backend, handle) = FakeBackend::scripted([
        connect_scenario(1, 2, 10),
        gatt_scenario(3, 2, 4),
        gatt_scenario(5, 2, 6),
        EventScenario::accept(RecordedCommand::DiscoverGatt(DiscoverGattCommand {
            command_id: CommandId { value: 7 },
            connection_id: ConnectionId { value: 2 },
            gatt_session_id: GattSessionId { value: 8 },
        })),
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
                .discover_gatt_session(connection, Duration::from_secs(2))
                .await,
            Ok(GattSessionId { value: 4 })
        );
        assert_eq!(
            owner
                .discover_gatt_session(connection, Duration::from_secs(2))
                .await,
            Ok(GattSessionId { value: 6 })
        );
        {
            let pending = owner.discover_gatt_session(connection, Duration::from_secs(2));
            tokio::pin!(pending);
            tokio::select! {
                result = &mut pending => panic!("discovery unexpectedly completed: {result:?}"),
                _ = tokio::time::sleep(Duration::from_millis(20)) => {}
            }
            assert_eq!(
                owner
                    .discover_gatt_session(connection, Duration::from_millis(20))
                    .await,
                Err(WaiterError::GattBusy)
            );
            assert_eq!(
                handle.emit(BleEvent::GattDiscovered {
                    command_id: CommandId { value: 7 },
                    database: GattDatabase {
                        connection_id: connection,
                        gatt_session_id: GattSessionId { value: 8 },
                        services: Vec::new(),
                    },
                }),
                EventIngressStatus::Delivered
            );
            assert_eq!(
                handle.emit(BleEvent::CommandCompleted {
                    command_id: CommandId { value: 7 },
                    error: None,
                }),
                EventIngressStatus::Delivered
            );
            assert_eq!(pending.as_mut().await, Ok(GattSessionId { value: 8 }));
        }
        let disconnected = BleEvent::ConnectionStateChanged {
            command_id: None,
            connection_id: connection,
            result: ConnectionStateResult::Ok {
                state: ConnectionState::Disconnected,
            },
        };
        assert_eq!(handle.emit(disconnected), EventIngressStatus::Delivered);
        for _ in 0..9 {
            let _ = owner.next_event(Duration::from_secs(2)).await;
        }
        assert_eq!(
            owner
                .discover_gatt_session(connection, Duration::from_millis(20))
                .await,
            Err(WaiterError::ConnectionNotActive)
        );
        owner.close().await.unwrap();
    });
    assert_eq!(handle.commands().len(), 4);
    assert_eq!(handle.dispose_count(), 1);
}

#[test]
fn disconnect_before_gatt_dispatch_return_completes_discovery_as_disconnected() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let (backend, handle) = FakeBackend::scripted([
        connect_scenario(1, 2, 10),
        EventScenario::emit_before_return(
            RecordedCommand::DiscoverGatt(DiscoverGattCommand {
                command_id: CommandId { value: 3 },
                connection_id: ConnectionId { value: 2 },
                gatt_session_id: GattSessionId { value: 4 },
            }),
            vec![BleEvent::ConnectionStateChanged {
                command_id: None,
                connection_id: ConnectionId { value: 2 },
                result: ConnectionStateResult::Ok {
                    state: ConnectionState::Disconnected,
                },
            }],
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
            owner
                .discover_gatt_session(connection, Duration::from_secs(2))
                .await,
            Err(WaiterError::Disconnected)
        );
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
fn failed_gatt_discovery_releases_only_the_pending_session() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let error = BleEventError {
        category: BleErrorCategory::Gatt,
        message: "discovery failed".to_owned(),
        platform_details: None,
    };
    let (backend, handle) = FakeBackend::scripted([
        connect_scenario(1, 2, 10),
        EventScenario::emit_before_return(
            RecordedCommand::DiscoverGatt(DiscoverGattCommand {
                command_id: CommandId { value: 3 },
                connection_id: ConnectionId { value: 2 },
                gatt_session_id: GattSessionId { value: 4 },
            }),
            vec![BleEvent::CommandCompleted {
                command_id: CommandId { value: 3 },
                error: Some(error.clone()),
            }],
        ),
        gatt_scenario(5, 2, 6),
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
                .discover_gatt_session(connection, Duration::from_secs(2))
                .await,
            Err(WaiterError::Operation(error))
        );
        assert_eq!(
            owner
                .discover_gatt_session(connection, Duration::from_secs(2))
                .await,
            Ok(GattSessionId { value: 6 })
        );
        owner.close().await.unwrap();
    });
    assert_eq!(handle.commands().len(), 3);
    assert_eq!(handle.dispose_count(), 1);
}
