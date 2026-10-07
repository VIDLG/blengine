use super::support::*;

#[test]
fn write_both_modes_require_exact_domain_and_terminal_and_keep_payload() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let (backend, handle) = FakeBackend::scripted([
        connect_scenario(1, 2, 10),
        read_discovery(),
        EventScenario::emit_before_return(
            write_command(5, &[0, 255, 128], WriteType::WithResponse),
            vec![read_terminal(5, None), written(5)],
        ),
        EventScenario::accept(write_command(6, &[0xab], WriteType::WithoutResponse)),
    ]);
    owner
        .attach_hub(BleHub::new(Arc::new(backend), owner.event_sink()).unwrap())
        .unwrap();
    let host = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    host.block_on(async {
        let connection = ConnectionId { value: 2 };
        let session = GattSessionId { value: 4 };
        let characteristic = GattCharacteristicId { value: 30 };
        assert_eq!(owner.write_characteristic(connection, session, characteristic, vec![1], WriteType::WithResponse, Duration::from_secs(1)).await, Err(WaiterError::ConnectionNotActive));
        assert_eq!(owner.connect(PeripheralId { value: 10 }, Duration::from_secs(2)).await, Ok(connection));
        assert_eq!(owner.write_characteristic(connection, session, characteristic, vec![1], WriteType::WithResponse, Duration::from_secs(1)).await, Err(WaiterError::GattNotReady));
        assert_eq!(owner.discover_gatt_session(connection, Duration::from_secs(2)).await, Ok(session));
        assert_eq!(owner.write_characteristic(connection, GattSessionId { value: 99 }, characteristic, vec![1], WriteType::WithResponse, Duration::from_secs(1)).await, Err(WaiterError::GattNotReady));
        assert_eq!(owner.write_characteristic(connection, session, GattCharacteristicId { value: 31 }, vec![1], WriteType::WithResponse, Duration::from_secs(1)).await, Err(WaiterError::GattNotReady));
        assert_eq!(owner.write_characteristic(connection, session, characteristic, vec![0, 255, 128], WriteType::WithResponse, Duration::from_secs(2)).await, Ok(()));
        {
            let pending = owner.write_characteristic(connection, session, characteristic, vec![0xab], WriteType::WithoutResponse, Duration::from_secs(2));
            tokio::pin!(pending);
            tokio::select! {
                result = &mut pending => panic!("write completed without domain/terminal: {result:?}"),
                _ = tokio::time::sleep(Duration::from_millis(20)) => {}
            }
            assert_eq!(owner.read_characteristic(connection, session, characteristic, Duration::from_secs(1)).await, Err(WaiterError::OperationBusy));
            assert_eq!(owner.write_characteristic(connection, session, characteristic, vec![1], WriteType::WithoutResponse, Duration::from_secs(1)).await, Err(WaiterError::OperationBusy));
            assert_eq!(handle.emit(read_terminal(6, None)), EventIngressStatus::Delivered);
            assert!(tokio::time::timeout(Duration::from_millis(20), &mut pending).await.is_err(), "terminal alone does not complete without-response write");
            assert_eq!(handle.emit(written(6)), EventIngressStatus::Delivered);
            assert_eq!(pending.await, Ok(()));
        }
        owner.close().await.unwrap();
    });
    assert_eq!(handle.commands().len(), 4);
}

#[test]
fn write_timeout_keeps_native_slot_until_late_completion_then_retries() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let error = BleEventError {
        category: BleErrorCategory::Gatt,
        message: "write failed".to_owned(),
        platform_details: None,
    };
    let (backend, handle) = FakeBackend::scripted([
        connect_scenario(1, 2, 10),
        read_discovery(),
        EventScenario::accept(write_command(5, &[1], WriteType::WithResponse)),
        EventScenario::emit_before_return(
            write_command(6, &[2], WriteType::WithoutResponse),
            vec![read_terminal(6, Some(error.clone()))],
        ),
        EventScenario::emit_before_return(
            write_command(7, &[3], WriteType::WithResponse),
            vec![written(7), read_terminal(7, None)],
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
        let characteristic = GattCharacteristicId { value: 30 };
        assert_eq!(
            owner
                .write_characteristic(
                    connection,
                    session,
                    characteristic,
                    vec![1],
                    WriteType::WithResponse,
                    Duration::from_millis(20)
                )
                .await,
            Err(WaiterError::Timeout)
        );
        assert_eq!(
            owner
                .write_characteristic(
                    connection,
                    session,
                    characteristic,
                    vec![2],
                    WriteType::WithoutResponse,
                    Duration::from_secs(1)
                )
                .await,
            Err(WaiterError::OperationBusy)
        );
        assert_eq!(
            handle.emit(read_terminal(5, None)),
            EventIngressStatus::Delivered
        );
        assert_eq!(
            owner
                .write_characteristic(
                    connection,
                    session,
                    characteristic,
                    vec![2],
                    WriteType::WithoutResponse,
                    Duration::from_secs(1)
                )
                .await,
            Err(WaiterError::OperationBusy)
        );
        assert_eq!(handle.emit(written(5)), EventIngressStatus::Delivered);
        for _ in 0..6 {
            let _ = owner.next_event(Duration::from_secs(2)).await;
        }
        assert_eq!(
            owner
                .write_characteristic(
                    connection,
                    session,
                    characteristic,
                    vec![2],
                    WriteType::WithoutResponse,
                    Duration::from_secs(2)
                )
                .await,
            Err(WaiterError::Operation(error))
        );
        assert_eq!(
            owner
                .write_characteristic(
                    connection,
                    session,
                    characteristic,
                    vec![3],
                    WriteType::WithResponse,
                    Duration::from_secs(2)
                )
                .await,
            Ok(())
        );
        owner.close().await.unwrap();
    });
    assert_eq!(handle.commands().len(), 5);
}

#[test]
fn write_predispatch_disconnect_and_rejected_callback_fail_closed() {
    let host = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let (backend, handle) = FakeBackend::scripted([
        connect_scenario(1, 2, 10),
        read_discovery(),
        EventScenario::emit_before_return(
            write_command(5, &[1], WriteType::WithResponse),
            vec![BleEvent::ConnectionStateChanged {
                command_id: None,
                connection_id: ConnectionId { value: 2 },
                result: ConnectionStateResult::Ok {
                    state: ConnectionState::Disconnected,
                },
            }],
        ),
        connect_scenario(6, 7, 10),
    ]);
    owner
        .attach_hub(BleHub::new(Arc::new(backend), owner.event_sink()).unwrap())
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
                .write_characteristic(
                    connection,
                    session,
                    GattCharacteristicId { value: 30 },
                    vec![1],
                    WriteType::WithResponse,
                    Duration::from_secs(2)
                )
                .await,
            Err(WaiterError::Disconnected)
        );
        assert_eq!(
            owner
                .connect(PeripheralId { value: 10 }, Duration::from_secs(2))
                .await,
            Ok(ConnectionId { value: 7 })
        );
        owner.close().await.unwrap();
    });
    assert_eq!(handle.commands().len(), 4);

    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let (backend, handle) = FakeBackend::scripted([
        connect_scenario(1, 2, 10),
        read_discovery(),
        EventScenario::emit_before_reject(
            write_command(5, &[1], WriteType::WithoutResponse),
            vec![written(5)],
            CommandDispatchError::InvalidState {
                detail: "write rejected".to_owned(),
            },
        ),
    ]);
    owner
        .attach_hub(BleHub::new(Arc::new(backend), owner.event_sink()).unwrap())
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
                .write_characteristic(
                    connection,
                    session,
                    GattCharacteristicId { value: 30 },
                    vec![1],
                    WriteType::WithoutResponse,
                    Duration::from_secs(2)
                )
                .await,
            Err(WaiterError::TransportFault)
        );
        assert!(owner.faulted_for_test());
        assert_eq!(
            owner.next_event(Duration::from_secs(2)).await,
            Err(EventReadError::Fault)
        );
        owner.close().await.unwrap();
    });
    assert_eq!(handle.commands().len(), 3);
}

#[test]
fn read_requires_committed_gatt_identity_and_preserves_binary_value() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let value = [0, 255, 128, 0x41];
    let (backend, handle) = FakeBackend::scripted([
        connect_scenario(1, 2, 10),
        read_discovery(),
        EventScenario::emit_before_return(
            read_command(5),
            vec![read_terminal(5, None), read_value(5, &value)],
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
        let connection = ConnectionId { value: 2 };
        let session = GattSessionId { value: 4 };
        let characteristic = GattCharacteristicId { value: 30 };
        assert_eq!(
            owner
                .read_characteristic(connection, session, characteristic, Duration::from_secs(1))
                .await,
            Err(WaiterError::ConnectionNotActive)
        );
        assert_eq!(
            owner
                .connect(PeripheralId { value: 10 }, Duration::from_secs(2))
                .await,
            Ok(connection)
        );
        assert_eq!(
            owner
                .read_characteristic(connection, session, characteristic, Duration::from_secs(1))
                .await,
            Err(WaiterError::GattNotReady)
        );
        assert_eq!(
            owner
                .discover_gatt_session(connection, Duration::from_secs(2))
                .await,
            Ok(session)
        );
        assert_eq!(
            owner
                .read_characteristic(
                    connection,
                    GattSessionId { value: 99 },
                    characteristic,
                    Duration::from_secs(1)
                )
                .await,
            Err(WaiterError::GattNotReady)
        );
        assert_eq!(
            owner
                .read_characteristic(
                    connection,
                    session,
                    GattCharacteristicId { value: 31 },
                    Duration::from_secs(1)
                )
                .await,
            Err(WaiterError::GattNotReady)
        );
        assert_eq!(
            owner
                .read_characteristic(connection, session, characteristic, Duration::from_secs(2))
                .await,
            Ok(value.to_vec())
        );
        owner.close().await.unwrap();
    });
    assert_eq!(handle.commands().len(), 3);
    assert_eq!(handle.dispose_count(), 1);
}

#[test]
fn read_timeout_retains_slot_until_late_domain_and_terminal() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let (backend, handle) = FakeBackend::scripted([
        connect_scenario(1, 2, 10),
        read_discovery(),
        EventScenario::accept(read_command(5)),
        EventScenario::emit_before_return(
            read_command(6),
            vec![read_value(6, &[0x80]), read_terminal(6, None)],
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
        let characteristic = GattCharacteristicId { value: 30 };
        assert_eq!(
            owner
                .read_characteristic(
                    connection,
                    session,
                    characteristic,
                    Duration::from_millis(20)
                )
                .await,
            Err(WaiterError::Timeout)
        );
        assert_eq!(
            owner
                .read_characteristic(connection, session, characteristic, Duration::from_secs(1))
                .await,
            Err(WaiterError::OperationBusy)
        );
        assert_eq!(
            handle.emit(read_terminal(5, None)),
            EventIngressStatus::Delivered
        );
        assert_eq!(
            owner
                .read_characteristic(connection, session, characteristic, Duration::from_secs(1))
                .await,
            Err(WaiterError::OperationBusy)
        );
        assert_eq!(
            handle.emit(read_value(5, &[0, 255])),
            EventIngressStatus::Delivered
        );
        // Drain the delivered event to establish the actor has retired the native reservation.
        for _ in 0..6 {
            let _ = owner.next_event(Duration::from_secs(2)).await;
        }
        assert_eq!(
            owner
                .read_characteristic(connection, session, characteristic, Duration::from_secs(2))
                .await,
            Ok(vec![0x80])
        );
        owner.close().await.unwrap();
    });
    assert_eq!(handle.commands().len(), 4);
}

#[test]
fn read_failure_releases_slot_and_accepted_read_is_busy_until_complete() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let error = BleEventError {
        category: BleErrorCategory::Gatt,
        message: "read failed".to_owned(),
        platform_details: None,
    };
    let (backend, handle) = FakeBackend::scripted([
        connect_scenario(1, 2, 10),
        read_discovery(),
        EventScenario::emit_before_return(
            read_command(5),
            vec![read_terminal(5, Some(error.clone()))],
        ),
        EventScenario::accept(read_command(6)),
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
        let characteristic = GattCharacteristicId { value: 30 };
        assert_eq!(
            owner
                .read_characteristic(connection, session, characteristic, Duration::from_secs(2))
                .await,
            Err(WaiterError::Operation(error))
        );
        {
            let pending = owner.read_characteristic(
                connection,
                session,
                characteristic,
                Duration::from_secs(2),
            );
            tokio::pin!(pending);
            tokio::select! {
                result = &mut pending => panic!("read unexpectedly completed: {result:?}"),
                _ = tokio::time::sleep(Duration::from_millis(20)) => {}
            }
            assert_eq!(
                owner
                    .read_characteristic(
                        connection,
                        session,
                        characteristic,
                        Duration::from_secs(1)
                    )
                    .await,
                Err(WaiterError::OperationBusy)
            );
            assert_eq!(
                handle.emit(read_value(6, &[0x10, 0x20])),
                EventIngressStatus::Delivered
            );
            assert_eq!(
                handle.emit(read_terminal(6, None)),
                EventIngressStatus::Delivered
            );
            assert_eq!(pending.await, Ok(vec![0x10, 0x20]));
        }
        owner.close().await.unwrap();
    });
    assert_eq!(handle.commands().len(), 4);
}

#[test]
fn read_predispatch_disconnect_releases_connection_and_callback_rejection_faults() {
    let host = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    for disconnected in [
        BleEvent::ConnectionStateChanged {
            command_id: None,
            connection_id: ConnectionId { value: 2 },
            result: ConnectionStateResult::Ok {
                state: ConnectionState::Disconnected,
            },
        },
        BleEvent::ConnectionStateChanged {
            command_id: None,
            connection_id: ConnectionId { value: 2 },
            result: ConnectionStateResult::Err {
                error: BleEventError {
                    category: BleErrorCategory::Connection,
                    message: "link lost".to_owned(),
                    platform_details: None,
                },
            },
        },
    ] {
        let owner = RuntimeOwner::new_assuming_initialized().unwrap();
        let (backend, handle) = FakeBackend::scripted([
            connect_scenario(1, 2, 10),
            read_discovery(),
            EventScenario::emit_before_return(read_command(5), vec![disconnected]),
            connect_scenario(6, 7, 10),
        ]);
        owner
            .attach_hub(BleHub::new(Arc::new(backend), owner.event_sink()).unwrap())
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
                        Duration::from_secs(2)
                    )
                    .await,
                Err(WaiterError::Disconnected)
            );
            assert_eq!(
                owner
                    .connect(PeripheralId { value: 10 }, Duration::from_secs(2))
                    .await,
                Ok(ConnectionId { value: 7 })
            );
            owner.close().await.unwrap();
        });
        assert_eq!(handle.commands().len(), 4);
    }

    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let (backend, handle) = FakeBackend::scripted([
        connect_scenario(1, 2, 10),
        read_discovery(),
        EventScenario::emit_before_reject(
            read_command(5),
            vec![read_value(5, &[1])],
            CommandDispatchError::InvalidState {
                detail: "rejected read".to_owned(),
            },
        ),
    ]);
    owner
        .attach_hub(BleHub::new(Arc::new(backend), owner.event_sink()).unwrap())
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
                    Duration::from_secs(2)
                )
                .await,
            Err(WaiterError::TransportFault)
        );
        assert!(owner.faulted_for_test());
        assert_eq!(
            owner.next_event(Duration::from_secs(2)).await,
            Err(EventReadError::Fault)
        );
        owner.close().await.unwrap();
    });
    assert_eq!(handle.commands().len(), 3);
}
