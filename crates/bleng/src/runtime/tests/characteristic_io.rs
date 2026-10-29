use super::support::{
    Arc, BleErrorCategory, BleEvent, BleEventError, BleHub, CommandDispatchError, ConnectionId,
    ConnectionState, ConnectionStateResult, Duration, EventIngressStatus, EventReadError,
    EventScenario, FakeBackend, GattCharacteristicId, GattSessionId, PeripheralId, RuntimeOwner,
    WaiterError, WriteType, connect_scenario, read_command, read_discovery, read_terminal,
    read_value, write_command, written,
};

#[test]
fn write_both_modes_require_exact_domain_and_terminal_and_keep_payload() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let (backend, handle) = FakeBackend::scripted([
        connect_scenario(2, 1, 10),
        read_discovery(4),
        EventScenario::emit_before_return(
            write_command(7, &[0, 255, 128], WriteType::WithResponse),
            vec![read_terminal(7, None), written(7)],
        ),
        EventScenario::accept(write_command(8, &[0xab], WriteType::WithoutResponse)),
        EventScenario::accept(read_command(9)),
        EventScenario::accept(write_command(10, &[1], WriteType::WithResponse)),
    ]);
    owner
        .attach_hub(BleHub::new(Arc::new(backend), owner.event_sink()).unwrap())
        .unwrap();
    let host = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    host.block_on(async {
        let connection = ConnectionId { value: 1 };
        let session = GattSessionId { value: 2 };
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
            let reading = owner.read_characteristic(
                connection,
                session,
                characteristic,
                Duration::from_secs(2),
            );
            tokio::pin!(reading);
            tokio::select! {
                result = &mut reading => panic!("read completed before callbacks: {result:?}"),
                _ = tokio::time::sleep(Duration::from_millis(20)) => {}
            }
            let writing = owner.write_characteristic(
                connection,
                session,
                characteristic,
                vec![1],
                WriteType::WithResponse,
                Duration::from_secs(2),
            );
            tokio::pin!(writing);
            tokio::select! {
                result = &mut writing => panic!("sibling write completed before callbacks: {result:?}"),
                _ = tokio::time::sleep(Duration::from_millis(20)) => {}
            }
            assert_eq!(handle.emit(read_terminal(8, None)), EventIngressStatus::Delivered);
            assert!(tokio::time::timeout(Duration::from_millis(20), &mut pending).await.is_err(), "terminal alone does not complete without-response write");
            assert_eq!(handle.emit(written(8)), EventIngressStatus::Delivered);
            assert_eq!(pending.await, Ok(()));
            assert_eq!(handle.emit(read_value(9, &[0x55])), EventIngressStatus::Delivered);
            assert_eq!(handle.emit(read_terminal(9, None)), EventIngressStatus::Delivered);
            assert_eq!(reading.await, Ok(vec![0x55]));
            assert_eq!(handle.emit(written(10)), EventIngressStatus::Delivered);
            assert_eq!(handle.emit(read_terminal(10, None)), EventIngressStatus::Delivered);
            assert_eq!(writing.await, Ok(()));
        }
        owner.close().await.unwrap();
    });
    assert_eq!(handle.commands().len(), 6);
}

#[test]
fn write_timeout_does_not_block_sibling_failure_or_later_retry() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let error = BleEventError {
        category: BleErrorCategory::Gatt,
        message: "write failed".to_owned(),
        platform_details: None,
    };
    let (backend, handle) = FakeBackend::scripted([
        connect_scenario(1, 1, 10),
        read_discovery(2),
        EventScenario::accept(write_command(3, &[1], WriteType::WithResponse)),
        EventScenario::emit_before_return(
            write_command(4, &[2], WriteType::WithoutResponse),
            vec![read_terminal(4, Some(error.clone()))],
        ),
        EventScenario::emit_before_return(
            write_command(5, &[3], WriteType::WithResponse),
            vec![written(5), read_terminal(5, None)],
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
            Err(WaiterError::Operation(error.clone()))
        );
        assert_eq!(
            handle.emit(read_terminal(3, None)),
            EventIngressStatus::Delivered
        );
        assert_eq!(handle.emit(written(3)), EventIngressStatus::Delivered);
        for _ in 0..6 {
            let _ = owner.next_event(Duration::from_secs(2)).await;
        }
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
        connect_scenario(1, 1, 10),
        read_discovery(2),
        EventScenario::emit_before_return(
            write_command(3, &[1], WriteType::WithResponse),
            vec![BleEvent::ConnectionStateChanged {
                command_id: None,
                connection_id: ConnectionId { value: 1 },
                result: ConnectionStateResult::Ok {
                    state: ConnectionState::Disconnected,
                },
            }],
        ),
        connect_scenario(4, 3, 10),
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
            Ok(ConnectionId { value: 3 })
        );
        owner.close().await.unwrap();
    });
    assert_eq!(handle.commands().len(), 4);

    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let (backend, handle) = FakeBackend::scripted([
        connect_scenario(1, 1, 10),
        read_discovery(2),
        EventScenario::emit_before_reject(
            write_command(3, &[1], WriteType::WithoutResponse),
            vec![written(3)],
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
        connect_scenario(2, 1, 10),
        read_discovery(4),
        EventScenario::emit_before_return(
            read_command(7),
            vec![read_terminal(7, None), read_value(7, &value)],
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
        let connection = ConnectionId { value: 1 };
        let session = GattSessionId { value: 2 };
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
        connect_scenario(1, 1, 10),
        read_discovery(2),
        EventScenario::accept(read_command(3)),
        EventScenario::accept(read_command(4)),
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
        let sibling =
            owner.read_characteristic(connection, session, characteristic, Duration::from_secs(2));
        tokio::pin!(sibling);
        tokio::select! {
            result = &mut sibling => panic!("sibling read completed before callbacks: {result:?}"),
            _ = tokio::time::sleep(Duration::from_millis(20)) => {}
        }
        assert_eq!(
            handle.emit(read_value(3, &[0, 255])),
            EventIngressStatus::Delivered
        );
        assert_eq!(
            handle.emit(read_terminal(3, None)),
            EventIngressStatus::Delivered
        );
        // The timed-out caller no longer receives the first result, but its reservation
        // remains isolated until native evidence retires it.
        assert_eq!(
            handle.emit(read_value(4, &[0x80])),
            EventIngressStatus::Delivered
        );
        assert_eq!(
            handle.emit(read_terminal(4, None)),
            EventIngressStatus::Delivered
        );
        assert_eq!(sibling.await, Ok(vec![0x80]));
        owner.close().await.unwrap();
    });
    assert_eq!(handle.commands().len(), 4);
}

#[test]
fn read_failure_releases_only_its_reservation_and_siblings_complete_independently() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let error = BleEventError {
        category: BleErrorCategory::Gatt,
        message: "read failed".to_owned(),
        platform_details: None,
    };
    let (backend, handle) = FakeBackend::scripted([
        connect_scenario(1, 1, 10),
        read_discovery(2),
        EventScenario::emit_before_return(
            read_command(3),
            vec![read_terminal(3, Some(error.clone()))],
        ),
        EventScenario::accept(read_command(4)),
        EventScenario::accept(read_command(5)),
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
            let sibling = owner.read_characteristic(
                connection,
                session,
                characteristic,
                Duration::from_secs(2),
            );
            tokio::pin!(sibling);
            tokio::select! {
                result = &mut sibling => panic!("sibling read completed before callbacks: {result:?}"),
                _ = tokio::time::sleep(Duration::from_millis(20)) => {}
            }
            assert_eq!(handle.emit(read_value(4, &[0x10, 0x20])), EventIngressStatus::Delivered);
            assert_eq!(handle.emit(read_terminal(4, None)), EventIngressStatus::Delivered);
            assert_eq!(pending.await, Ok(vec![0x10, 0x20]));
            assert_eq!(handle.emit(read_value(5, &[0x30])), EventIngressStatus::Delivered);
            assert_eq!(handle.emit(read_terminal(5, None)), EventIngressStatus::Delivered);
            assert_eq!(sibling.await, Ok(vec![0x30]));
        }
        owner.close().await.unwrap();
    });
    assert_eq!(handle.commands().len(), 5);
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
            connection_id: ConnectionId { value: 1 },
            result: ConnectionStateResult::Ok {
                state: ConnectionState::Disconnected,
            },
        },
        BleEvent::ConnectionStateChanged {
            command_id: None,
            connection_id: ConnectionId { value: 1 },
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
            connect_scenario(1, 1, 10),
            read_discovery(2),
            EventScenario::emit_before_return(read_command(3), vec![disconnected]),
            connect_scenario(4, 3, 10),
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
                Ok(ConnectionId { value: 3 })
            );
            owner.close().await.unwrap();
        });
        assert_eq!(handle.commands().len(), 4);
    }

    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let (backend, handle) = FakeBackend::scripted([
        connect_scenario(1, 1, 10),
        read_discovery(2),
        EventScenario::emit_before_reject(
            read_command(3),
            vec![read_value(3, &[1])],
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
