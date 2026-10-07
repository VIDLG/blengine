use super::support::*;

#[test]
fn disable_active_subscription_rejects_during_other_characteristic_enable_dispatch() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let a = GattCharacteristicId { value: 31 };
    let mut database = read_database();
    let b_characteristic = &mut database.services[0].characteristics[0];
    b_characteristic.properties.notifiable = true;
    let mut a_characteristic = b_characteristic.clone();
    a_characteristic.id = a;
    a_characteristic.uuid = BluetoothUuid {
        value: "00002a38-0000-1000-8000-00805f9b34fb".to_owned(),
    };
    database.services[0].characteristics.push(a_characteristic);
    let enable_a = SetNotificationsCommand {
        command_id: CommandId { value: 7 },
        connection_id: ConnectionId { value: 2 },
        gatt_session_id: GattSessionId { value: 4 },
        subscription_id: SubscriptionId { value: 8 },
        characteristic_id: a,
        action: NotificationAction::Enable {
            mode: NotificationModeRequest::PlatformDefault,
        },
    };
    let (backend, handle) = FakeBackend::scripted([
        connect_scenario(1, 2, 10),
        EventScenario::emit_before_return(
            RecordedCommand::DiscoverGatt(DiscoverGattCommand {
                command_id: CommandId { value: 3 },
                connection_id: ConnectionId { value: 2 },
                gatt_session_id: GattSessionId { value: 4 },
            }),
            vec![
                BleEvent::GattDiscovered {
                    command_id: CommandId { value: 3 },
                    database,
                },
                read_terminal(3, None),
            ],
        ),
        notification_enabled_scenario(5, 6),
        EventScenario::accept(RecordedCommand::SetNotifications(enable_a)),
        EventScenario::emit_before_return(
            notification_command(9, 6, NotificationAction::Disable),
            vec![
                notification_state(9, 6, NotificationState::Disabled),
                read_terminal(9, None),
            ],
        ),
    ]);
    let entered = Arc::new(Notify::new());
    let finished = Arc::new(Notify::new());
    let (release_sender, release_receiver) = mpsc::channel();
    let backend = BlockingDispatchBackend {
        inner: backend,
        blocked: BlockedDispatch::NotificationEnable(a),
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
        let (b, _) = owner
            .enable_notifications(
                connection,
                session,
                GattCharacteristicId { value: 30 },
                NotificationModeRequest::PlatformDefault,
                Duration::from_secs(2),
            )
            .await
            .unwrap();
        assert_eq!(b, SubscriptionId { value: 6 });
        {
            let enabling_a = owner.enable_notifications(
                connection,
                session,
                a,
                NotificationModeRequest::PlatformDefault,
                Duration::from_secs(2),
            );
            tokio::pin!(enabling_a);
            tokio::select! {
                result = &mut enabling_a => panic!("enable A unexpectedly completed: {result:?}"),
                _ = entered.notified() => {}
            }
            assert_eq!(
                owner
                    .disable_notifications(connection, session, b, Duration::from_secs(2))
                    .await,
                Err(WaiterError::OperationBusy)
            );
            assert_eq!(handle.commands().len(), 3, "disable B must not dispatch");
            release_sender.send(()).unwrap();
            tokio::time::timeout(Duration::from_secs(2), finished.notified())
                .await
                .unwrap();
            let mut enabled_a = notification_state(
                7,
                8,
                NotificationState::Enabled {
                    actual_mode: Some(NotificationMode::Notification),
                },
            );
            if let BleEvent::NotificationStateChanged {
                characteristic_id, ..
            } = &mut enabled_a
            {
                *characteristic_id = a;
            }
            assert_eq!(handle.emit(enabled_a), EventIngressStatus::Delivered);
            assert_eq!(
                handle.emit(read_terminal(7, None)),
                EventIngressStatus::Delivered
            );
            assert_eq!(
                enabling_a.await,
                Ok((
                    SubscriptionId { value: 8 },
                    Some(NotificationMode::Notification)
                ))
            );
        }
        assert_eq!(
            owner
                .disable_notifications(connection, session, b, Duration::from_secs(2))
                .await,
            Ok(())
        );
        assert_eq!(handle.commands().len(), 5);
        owner.close().await.unwrap();
    });
    assert_eq!(handle.dispose_count(), 1);
}

#[test]
fn notification_enable_requires_state_and_terminal_and_value_does_not_complete_it() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let (backend, handle) = FakeBackend::scripted([
        connect_scenario(1, 2, 10),
        notification_discovery(),
        EventScenario::accept(notification_command(
            5,
            6,
            NotificationAction::Enable {
                mode: NotificationModeRequest::PlatformDefault,
            },
        )),
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
        {
            let pending = owner.enable_notifications(
                connection,
                session,
                GattCharacteristicId { value: 30 },
                NotificationModeRequest::PlatformDefault,
                Duration::from_secs(2),
            );
            tokio::pin!(pending);
            tokio::select! {
                result = &mut pending => panic!("enable completed before callbacks: {result:?}"),
                _ = tokio::time::sleep(Duration::from_millis(20)) => {}
            }
            assert_eq!(
                handle.emit(notification_value(6)),
                EventIngressStatus::Delivered
            );
            assert_eq!(
                handle.emit(read_terminal(5, None)),
                EventIngressStatus::Delivered
            );
            assert!(
                tokio::time::timeout(Duration::from_millis(20), &mut pending)
                    .await
                    .is_err(),
                "a value and terminal do not complete enable without notification state"
            );
            assert_eq!(
                handle.emit(notification_state(
                    5,
                    6,
                    NotificationState::Enabled {
                        actual_mode: Some(NotificationMode::Indication),
                    },
                )),
                EventIngressStatus::Delivered
            );
            assert_eq!(
                pending.await,
                Ok((
                    SubscriptionId { value: 6 },
                    Some(NotificationMode::Indication),
                ))
            );
        }
        owner.close().await.unwrap();
    });
    assert_eq!(handle.commands().len(), 3);
}

#[test]
fn notification_transition_blocks_read_and_write_and_read_or_write_blocks_enable() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let (backend, handle) = FakeBackend::scripted([
        connect_scenario(1, 2, 10),
        notification_discovery(),
        EventScenario::accept(notification_command(
            5,
            6,
            NotificationAction::Enable {
                mode: NotificationModeRequest::RequireNotification,
            },
        )),
        EventScenario::emit_before_return(
            notification_command(7, 6, NotificationAction::Disable),
            vec![
                notification_state(7, 6, NotificationState::Disabled),
                read_terminal(7, None),
            ],
        ),
        EventScenario::accept(read_command(8)),
        EventScenario::accept(write_command(9, &[2], WriteType::WithResponse)),
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
        {
            let enabling = owner.enable_notifications(
                connection,
                session,
                characteristic,
                NotificationModeRequest::RequireNotification,
                Duration::from_secs(2),
            );
            tokio::pin!(enabling);
            tokio::select! {
                result = &mut enabling => panic!("enable completed before callbacks: {result:?}"),
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
                owner
                    .write_characteristic(
                        connection,
                        session,
                        characteristic,
                        vec![1],
                        WriteType::WithResponse,
                        Duration::from_secs(1),
                    )
                    .await,
                Err(WaiterError::OperationBusy)
            );
            assert_eq!(
                handle.emit(notification_state(
                    5,
                    6,
                    NotificationState::Enabled {
                        actual_mode: Some(NotificationMode::Notification),
                    },
                )),
                EventIngressStatus::Delivered
            );
            assert_eq!(
                handle.emit(read_terminal(5, None)),
                EventIngressStatus::Delivered
            );
            assert_eq!(
                enabling.await,
                Ok((
                    SubscriptionId { value: 6 },
                    Some(NotificationMode::Notification),
                ))
            );
        }
        assert_eq!(
            owner
                .disable_notifications(
                    connection,
                    session,
                    SubscriptionId { value: 6 },
                    Duration::from_secs(2),
                )
                .await,
            Ok(())
        );

        {
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
            assert_eq!(
                owner
                    .enable_notifications(
                        connection,
                        session,
                        characteristic,
                        NotificationModeRequest::PlatformDefault,
                        Duration::from_secs(1),
                    )
                    .await,
                Err(WaiterError::OperationBusy)
            );
            assert_eq!(
                handle.emit(read_value(8, &[0x01])),
                EventIngressStatus::Delivered
            );
            assert_eq!(
                handle.emit(read_terminal(8, None)),
                EventIngressStatus::Delivered
            );
            assert_eq!(reading.await, Ok(vec![0x01]));
        }

        {
            let writing = owner.write_characteristic(
                connection,
                session,
                characteristic,
                vec![2],
                WriteType::WithResponse,
                Duration::from_secs(2),
            );
            tokio::pin!(writing);
            tokio::select! {
                result = &mut writing => panic!("write completed before callbacks: {result:?}"),
                _ = tokio::time::sleep(Duration::from_millis(20)) => {}
            }
            assert_eq!(
                owner
                    .enable_notifications(
                        connection,
                        session,
                        characteristic,
                        NotificationModeRequest::PlatformDefault,
                        Duration::from_secs(1),
                    )
                    .await,
                Err(WaiterError::OperationBusy)
            );
            assert_eq!(handle.emit(written(9)), EventIngressStatus::Delivered);
            assert_eq!(
                handle.emit(read_terminal(9, None)),
                EventIngressStatus::Delivered
            );
            assert_eq!(writing.await, Ok(()));
        }
        owner.close().await.unwrap();
    });
    assert_eq!(handle.commands().len(), 6);
}

#[test]
fn notification_disable_failure_releases_transition_for_retry() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let error = BleEventError {
        category: BleErrorCategory::Gatt,
        message: "disable failed".to_owned(),
        platform_details: None,
    };
    let (backend, handle) = FakeBackend::scripted([
        connect_scenario(1, 2, 10),
        notification_discovery(),
        EventScenario::emit_before_return(
            notification_command(
                5,
                6,
                NotificationAction::Enable {
                    mode: NotificationModeRequest::PlatformDefault,
                },
            ),
            vec![
                notification_state(
                    5,
                    6,
                    NotificationState::Enabled {
                        actual_mode: Some(NotificationMode::Notification),
                    },
                ),
                read_terminal(5, None),
            ],
        ),
        EventScenario::emit_before_return(
            notification_command(7, 6, NotificationAction::Disable),
            vec![read_terminal(7, Some(error.clone()))],
        ),
        EventScenario::emit_before_return(
            notification_command(8, 6, NotificationAction::Disable),
            vec![
                notification_state(8, 6, NotificationState::Disabled),
                read_terminal(8, None),
            ],
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
                .enable_notifications(
                    connection,
                    session,
                    GattCharacteristicId { value: 30 },
                    NotificationModeRequest::PlatformDefault,
                    Duration::from_secs(2),
                )
                .await,
            Ok((
                SubscriptionId { value: 6 },
                Some(NotificationMode::Notification),
            ))
        );
        assert_eq!(
            owner
                .disable_notifications(
                    connection,
                    session,
                    SubscriptionId { value: 6 },
                    Duration::from_secs(2),
                )
                .await,
            Err(WaiterError::Operation(error))
        );
        assert_eq!(
            owner
                .disable_notifications(
                    connection,
                    session,
                    SubscriptionId { value: 6 },
                    Duration::from_secs(2),
                )
                .await,
            Ok(())
        );
        owner.close().await.unwrap();
    });
    assert_eq!(handle.commands().len(), 5);
}

#[test]
fn notification_enable_timeout_keeps_native_slot_until_late_failure_then_retries() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let error = BleEventError {
        category: BleErrorCategory::Gatt,
        message: "late enable failed".to_owned(),
        platform_details: None,
    };
    let (backend, handle) = FakeBackend::scripted([
        connect_scenario(1, 2, 10),
        notification_discovery(),
        EventScenario::accept(notification_command(
            5,
            6,
            NotificationAction::Enable {
                mode: NotificationModeRequest::PlatformDefault,
            },
        )),
        EventScenario::emit_before_return(
            notification_command(
                7,
                8,
                NotificationAction::Enable {
                    mode: NotificationModeRequest::PlatformDefault,
                },
            ),
            vec![
                notification_state(
                    7,
                    8,
                    NotificationState::Enabled {
                        actual_mode: Some(NotificationMode::Notification),
                    },
                ),
                read_terminal(7, None),
            ],
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
                .enable_notifications(
                    connection,
                    session,
                    GattCharacteristicId { value: 30 },
                    NotificationModeRequest::PlatformDefault,
                    Duration::from_millis(20),
                )
                .await,
            Err(WaiterError::Timeout)
        );
        assert_eq!(
            owner
                .enable_notifications(
                    connection,
                    session,
                    GattCharacteristicId { value: 30 },
                    NotificationModeRequest::PlatformDefault,
                    Duration::from_secs(1),
                )
                .await,
            Err(WaiterError::OperationBusy)
        );
        assert_eq!(
            handle.emit(read_terminal(5, Some(error))),
            EventIngressStatus::Delivered
        );
        assert_eq!(
            owner
                .enable_notifications(
                    connection,
                    session,
                    GattCharacteristicId { value: 30 },
                    NotificationModeRequest::PlatformDefault,
                    Duration::from_secs(2),
                )
                .await,
            Ok((
                SubscriptionId { value: 8 },
                Some(NotificationMode::Notification),
            ))
        );
        owner.close().await.unwrap();
    });
    assert_eq!(handle.commands().len(), 4);
}

#[test]
fn notification_enable_is_completed_as_disconnected_and_connection_is_retired() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let (backend, handle) = FakeBackend::scripted([
        connect_scenario(1, 2, 10),
        notification_discovery(),
        EventScenario::accept(notification_command(
            5,
            6,
            NotificationAction::Enable {
                mode: NotificationModeRequest::PlatformDefault,
            },
        )),
        connect_scenario(7, 8, 10),
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
        {
            let enabling = owner.enable_notifications(
                connection,
                session,
                GattCharacteristicId { value: 30 },
                NotificationModeRequest::PlatformDefault,
                Duration::from_secs(2),
            );
            tokio::pin!(enabling);
            tokio::select! {
                result = &mut enabling => panic!("enable completed before disconnection: {result:?}"),
                _ = tokio::time::sleep(Duration::from_millis(20)) => {}
            }
            assert_eq!(
                handle.emit(BleEvent::ConnectionStateChanged {
                    command_id: None,
                    connection_id: connection,
                    result: ConnectionStateResult::Ok {
                        state: ConnectionState::Disconnected,
                    },
                }),
                EventIngressStatus::Delivered
            );
            assert_eq!(enabling.await, Err(WaiterError::Disconnected));
        }
        assert_eq!(
            owner
                .enable_notifications(
                    connection,
                    session,
                    GattCharacteristicId { value: 30 },
                    NotificationModeRequest::PlatformDefault,
                    Duration::from_secs(1),
                )
                .await,
            Err(WaiterError::ConnectionNotActive)
        );
        assert_eq!(
            owner
                .connect(PeripheralId { value: 10 }, Duration::from_secs(2))
                .await,
            Ok(ConnectionId { value: 8 })
        );
        owner.close().await.unwrap();
    });
    assert_eq!(handle.commands().len(), 4);
}

#[test]
fn notification_callbacks_before_rejected_enable_or_disable_fail_closed() {
    for disabling in [false, true] {
        let owner = RuntimeOwner::new_assuming_initialized().unwrap();
        let mut scenarios = vec![connect_scenario(1, 2, 10), notification_discovery()];
        if disabling {
            scenarios.push(notification_enabled_scenario(5, 6));
        }
        let command = if disabling { 7 } else { 5 };
        let action = if disabling {
            NotificationAction::Disable
        } else {
            NotificationAction::Enable {
                mode: NotificationModeRequest::PlatformDefault,
            }
        };
        let state = if disabling {
            NotificationState::Disabled
        } else {
            NotificationState::Enabled {
                actual_mode: Some(NotificationMode::Notification),
            }
        };
        scenarios.push(EventScenario::emit_before_reject(
            notification_command(command, 6, action),
            vec![notification_state(command, 6, state)],
            CommandDispatchError::InvalidState {
                detail: "notification transition rejected after callback".to_owned(),
            },
        ));
        let (backend, handle) = FakeBackend::scripted(scenarios);
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
            let result = if disabling {
                assert_eq!(
                    owner
                        .enable_notifications(
                            connection,
                            session,
                            GattCharacteristicId { value: 30 },
                            NotificationModeRequest::PlatformDefault,
                            Duration::from_secs(2),
                        )
                        .await,
                    Ok((
                        SubscriptionId { value: 6 },
                        Some(NotificationMode::Notification)
                    ))
                );
                owner
                    .disable_notifications(
                        connection,
                        session,
                        SubscriptionId { value: 6 },
                        Duration::from_secs(2),
                    )
                    .await
            } else {
                owner
                    .enable_notifications(
                        connection,
                        session,
                        GattCharacteristicId { value: 30 },
                        NotificationModeRequest::PlatformDefault,
                        Duration::from_secs(2),
                    )
                    .await
                    .map(|_| ())
            };
            assert_eq!(result, Err(WaiterError::TransportFault));
            assert!(owner.faulted_for_test());
            assert_eq!(
                owner.next_event(Duration::from_secs(2)).await,
                Err(EventReadError::Fault)
            );
            assert_eq!(
                owner
                    .discover_gatt_session(connection, Duration::from_secs(1))
                    .await,
                Err(WaiterError::TransportFault)
            );
            owner.close().await.unwrap();
        });
        assert_eq!(handle.commands().len(), if disabling { 4 } else { 3 });
        assert_eq!(handle.dispose_count(), 1);
    }
}

#[test]
fn notification_disconnect_before_rejected_enable_preserves_rejection_and_allows_reconnect() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let rejection = CommandDispatchError::InvalidState {
        detail: "enable rejected after passive disconnect".to_owned(),
    };
    let (backend, handle) = FakeBackend::scripted([
        connect_scenario(1, 2, 10),
        notification_discovery(),
        EventScenario::emit_before_reject(
            notification_command(
                5,
                6,
                NotificationAction::Enable {
                    mode: NotificationModeRequest::PlatformDefault,
                },
            ),
            vec![BleEvent::ConnectionStateChanged {
                command_id: None,
                connection_id: ConnectionId { value: 2 },
                result: ConnectionStateResult::Ok {
                    state: ConnectionState::Disconnected,
                },
            }],
            rejection.clone(),
        ),
        connect_scenario(7, 8, 10),
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
                .enable_notifications(
                    connection,
                    session,
                    GattCharacteristicId { value: 30 },
                    NotificationModeRequest::PlatformDefault,
                    Duration::from_secs(2),
                )
                .await,
            Err(WaiterError::DispatchRejected(rejection))
        );
        assert_eq!(
            owner
                .connect(PeripheralId { value: 10 }, Duration::from_secs(2))
                .await,
            Ok(ConnectionId { value: 8 })
        );
        owner.close().await.unwrap();
    });
    assert_eq!(handle.commands().len(), 4);
    assert_eq!(handle.dispose_count(), 1);
}

#[test]
fn notification_require_indication_mismatch_faults_instead_of_succeeding() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let (backend, handle) = FakeBackend::scripted([
        connect_scenario(1, 2, 10),
        notification_discovery(),
        EventScenario::emit_before_return(
            notification_command(
                5,
                6,
                NotificationAction::Enable {
                    mode: NotificationModeRequest::RequireIndication,
                },
            ),
            vec![notification_state(
                5,
                6,
                NotificationState::Enabled {
                    actual_mode: Some(NotificationMode::Notification),
                },
            )],
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
                .enable_notifications(
                    connection,
                    session,
                    GattCharacteristicId { value: 30 },
                    NotificationModeRequest::RequireIndication,
                    Duration::from_secs(2),
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
    assert_eq!(handle.dispose_count(), 1);
}

#[test]
fn stale_notification_after_disable_cannot_revive_route_or_complete_read() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let (backend, handle) = FakeBackend::scripted([
        connect_scenario(1, 2, 10),
        notification_discovery(),
        notification_enabled_scenario(5, 6),
        EventScenario::emit_before_return(
            notification_command(7, 6, NotificationAction::Disable),
            vec![
                notification_state(7, 6, NotificationState::Disabled),
                read_terminal(7, None),
            ],
        ),
        EventScenario::accept(read_command(8)),
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
        let (subscription, _) = owner
            .enable_notifications(
                connection,
                session,
                characteristic,
                NotificationModeRequest::PlatformDefault,
                Duration::from_secs(2),
            )
            .await
            .unwrap();
        assert_eq!(
            owner
                .disable_notifications(connection, session, subscription, Duration::from_secs(2))
                .await,
            Ok(())
        );
        {
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
            assert_eq!(
                handle.emit(notification_value(6)),
                EventIngressStatus::Delivered
            );
            assert!(
                tokio::time::timeout(Duration::from_millis(20), &mut reading)
                    .await
                    .is_err(),
                "stale notification must not complete a characteristic read"
            );
            assert_eq!(
                handle.emit(read_value(8, &[0x44, 0x55])),
                EventIngressStatus::Delivered
            );
            assert_eq!(
                handle.emit(read_terminal(8, None)),
                EventIngressStatus::Delivered
            );
            assert_eq!(reading.await, Ok(vec![0x44, 0x55]));
        }
        owner.close().await.unwrap();
    });
    assert_eq!(handle.commands().len(), 5);
    assert_eq!(handle.dispose_count(), 1);
}

#[test]
fn notification_rediscovery_invalidates_old_subscription_and_gatt_session() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let mut database = read_database();
    database.gatt_session_id = GattSessionId { value: 8 };
    let (backend, handle) = FakeBackend::scripted([
        connect_scenario(1, 2, 10),
        notification_discovery(),
        notification_enabled_scenario(5, 6),
        EventScenario::emit_before_return(
            RecordedCommand::DiscoverGatt(DiscoverGattCommand {
                command_id: CommandId { value: 7 },
                connection_id: ConnectionId { value: 2 },
                gatt_session_id: GattSessionId { value: 8 },
            }),
            vec![
                BleEvent::GattDiscovered {
                    command_id: CommandId { value: 7 },
                    database,
                },
                read_terminal(7, None),
            ],
        ),
        EventScenario::emit_before_return(
            RecordedCommand::ReadCharacteristic(ReadCharacteristicCommand {
                command_id: CommandId { value: 9 },
                connection_id: ConnectionId { value: 2 },
                gatt_session_id: GattSessionId { value: 8 },
                characteristic_id: GattCharacteristicId { value: 30 },
            }),
            vec![
                BleEvent::CharacteristicRead {
                    command_id: CommandId { value: 9 },
                    connection_id: ConnectionId { value: 2 },
                    gatt_session_id: GattSessionId { value: 8 },
                    characteristic_id: GattCharacteristicId { value: 30 },
                    value: vec![0x80, 0xff],
                },
                read_terminal(9, None),
            ],
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
        let (subscription, _) = owner
            .enable_notifications(
                connection,
                session,
                GattCharacteristicId { value: 30 },
                NotificationModeRequest::PlatformDefault,
                Duration::from_secs(2),
            )
            .await
            .unwrap();
        let new_session = owner
            .discover_gatt_session(connection, Duration::from_secs(2))
            .await
            .unwrap();
        assert_eq!(new_session, GattSessionId { value: 8 });
        for invalid_session in [session, new_session] {
            assert_eq!(
                owner
                    .disable_notifications(
                        connection,
                        invalid_session,
                        subscription,
                        Duration::from_secs(1)
                    )
                    .await,
                Err(WaiterError::SubscriptionNotActive)
            );
        }
        assert_eq!(
            owner
                .read_characteristic(
                    connection,
                    session,
                    GattCharacteristicId { value: 30 },
                    Duration::from_secs(1)
                )
                .await,
            Err(WaiterError::GattNotReady)
        );
        assert_eq!(
            handle.emit(notification_state(
                5,
                6,
                NotificationState::Enabled {
                    actual_mode: Some(NotificationMode::Notification)
                }
            )),
            EventIngressStatus::Delivered
        );
        assert_eq!(
            owner
                .read_characteristic(
                    connection,
                    new_session,
                    GattCharacteristicId { value: 30 },
                    Duration::from_secs(2)
                )
                .await,
            Ok(vec![0x80, 0xff])
        );
        owner.close().await.unwrap();
    });
    assert_eq!(handle.commands().len(), 5);
}

#[test]
fn notification_active_route_blocks_read_but_allows_both_write_modes() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let (backend, handle) = FakeBackend::scripted([
        connect_scenario(1, 2, 10),
        notification_discovery(),
        notification_enabled_scenario(5, 6),
        EventScenario::emit_before_return(
            write_command(7, &[0x10], WriteType::WithResponse),
            vec![written(7), read_terminal(7, None)],
        ),
        EventScenario::emit_before_return(
            write_command(8, &[0x20], WriteType::WithoutResponse),
            vec![read_terminal(8, None), written(8)],
        ),
        EventScenario::emit_before_return(
            notification_command(9, 6, NotificationAction::Disable),
            vec![
                notification_state(9, 6, NotificationState::Disabled),
                read_terminal(9, None),
            ],
        ),
        EventScenario::emit_before_return(
            read_command(10),
            vec![read_value(10, &[0x30]), read_terminal(10, None)],
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
        let (subscription, _) = owner
            .enable_notifications(
                connection,
                session,
                characteristic,
                NotificationModeRequest::PlatformDefault,
                Duration::from_secs(2),
            )
            .await
            .unwrap();
        for (write_type, value) in [
            (WriteType::WithResponse, 0x10),
            (WriteType::WithoutResponse, 0x20),
        ] {
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
                owner
                    .write_characteristic(
                        connection,
                        session,
                        characteristic,
                        vec![value],
                        write_type,
                        Duration::from_secs(2)
                    )
                    .await,
                Ok(())
            );
        }
        assert_eq!(
            owner
                .read_characteristic(connection, session, characteristic, Duration::from_secs(1))
                .await,
            Err(WaiterError::OperationBusy)
        );
        assert_eq!(
            owner
                .disable_notifications(connection, session, subscription, Duration::from_secs(2))
                .await,
            Ok(())
        );
        assert_eq!(
            owner
                .read_characteristic(connection, session, characteristic, Duration::from_secs(2))
                .await,
            Ok(vec![0x30])
        );
        owner.close().await.unwrap();
    });
    assert_eq!(handle.commands().len(), 7);
}

#[test]
fn notification_disable_timeout_retains_transition_until_late_state_and_terminal() {
    for state_first in [false, true] {
        let owner = RuntimeOwner::new_assuming_initialized().unwrap();
        let (backend, handle) = FakeBackend::scripted([
            connect_scenario(1, 2, 10),
            notification_discovery(),
            notification_enabled_scenario(5, 6),
            EventScenario::accept(notification_command(7, 6, NotificationAction::Disable)),
            notification_enabled_scenario(8, 9),
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
            let (subscription, _) = owner
                .enable_notifications(
                    connection,
                    session,
                    characteristic,
                    NotificationModeRequest::PlatformDefault,
                    Duration::from_secs(2),
                )
                .await
                .unwrap();
            assert_eq!(
                owner
                    .disable_notifications(
                        connection,
                        session,
                        subscription,
                        Duration::from_millis(100)
                    )
                    .await,
                Err(WaiterError::Timeout)
            );
            assert_eq!(
                owner
                    .disable_notifications(
                        connection,
                        session,
                        subscription,
                        Duration::from_secs(1)
                    )
                    .await,
                Err(WaiterError::OperationBusy)
            );
            let state = notification_state(7, 6, NotificationState::Disabled);
            let terminal = read_terminal(7, None);
            let (first, second) = if state_first {
                (state, terminal)
            } else {
                (terminal, state)
            };
            assert_eq!(handle.emit(first.clone()), EventIngressStatus::Delivered);
            tokio::time::timeout(Duration::from_secs(2), async {
                while owner.next_event(Duration::from_secs(2)).await.unwrap() != Some(first.clone())
                {
                }
            })
            .await
            .unwrap();
            assert_eq!(
                owner
                    .disable_notifications(
                        connection,
                        session,
                        subscription,
                        Duration::from_secs(1)
                    )
                    .await,
                Err(WaiterError::OperationBusy),
                "one late event cannot release the timed-out native transition"
            );
            assert_eq!(
                owner
                    .write_characteristic(
                        connection,
                        session,
                        characteristic,
                        vec![1],
                        WriteType::WithResponse,
                        Duration::from_secs(1)
                    )
                    .await,
                Err(WaiterError::OperationBusy)
            );
            assert_eq!(handle.emit(second.clone()), EventIngressStatus::Delivered);
            tokio::time::timeout(Duration::from_secs(2), async {
                while owner.next_event(Duration::from_secs(2)).await.unwrap()
                    != Some(second.clone())
                {}
            })
            .await
            .unwrap();
            assert_eq!(
                owner
                    .disable_notifications(
                        connection,
                        session,
                        subscription,
                        Duration::from_secs(1)
                    )
                    .await,
                Err(WaiterError::SubscriptionNotActive)
            );
            assert_eq!(
                owner
                    .enable_notifications(
                        connection,
                        session,
                        characteristic,
                        NotificationModeRequest::PlatformDefault,
                        Duration::from_secs(2)
                    )
                    .await,
                Ok((
                    SubscriptionId { value: 9 },
                    Some(NotificationMode::Notification)
                ))
            );
            owner.close().await.unwrap();
        });
        assert_eq!(handle.commands().len(), 5);
        assert_eq!(handle.dispose_count(), 1);
    }
}
