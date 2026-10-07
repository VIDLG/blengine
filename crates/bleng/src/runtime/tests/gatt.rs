use super::support::{
    Arc, BleErrorCategory, BleEvent, BleEventError, BleHub, CommandDispatchError, CommandId,
    ConnectionId, ConnectionState, ConnectionStateResult, DiscoverGattCommand, Duration,
    EventIngressStatus, EventScenario, FakeBackend, GattDatabase, GattSessionId,
    NotificationAction, NotificationMode, NotificationModeRequest, NotificationState, Notify,
    PeripheralId, RecordedCommand, RuntimeOwner, SetNotificationsCommand, WaiterError,
    WaiterExpectation, command_completed, connect_scenario, gatt_scenario, init_command, mpsc,
    notification_command, notification_discovery, notification_enabled_scenario,
    notification_state, oneshot, read_command, read_database, read_discovery, read_value,
};
use crate::runtime::{
    RuntimePhase,
    actor::{
        BeginInitializeWithBlockedReservation, CancelledConnectionLeaf, CancelledRediscovery,
        HoldActor,
    },
};

#[test]
fn cancelling_begin_after_launch_handoff_before_reply_faults_and_disposes_once() {
    let owner = RuntimeOwner::new().unwrap();
    let (backend, handle) = FakeBackend::scripted([EventScenario::accept(init_command(1))]);
    owner
        .attach_hub(BleHub::new(Arc::new(backend), owner.event_sink()).unwrap())
        .unwrap();
    let host = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    host.block_on(async {
        let deadline = Duration::from_secs(2);
        let (entered_tx, entered_rx) = oneshot::channel();
        let (release_tx, release_rx) = mpsc::sync_channel(1);
        let mut pending = Box::pin(owner.run_operation(
            deadline,
            |handoff| BeginInitializeWithBlockedReservation {
                handoff,
                entered_tx,
                release_rx,
            },
        ));
        tokio::time::timeout(deadline, async {
            tokio::select! {
                entered = entered_rx => entered.expect("reservation never reached the gate"),
                result = &mut pending => panic!("begin replied before reservation release: {result:?}"),
            }
        })
        .await
        .unwrap();
        drop(pending);
        assert_eq!(owner.lifecycle.phase(), RuntimePhase::Faulted);
        release_tx.send(()).unwrap();
        // Cleanup races native dispatch; only fault/shutdown ownership is asserted here.
        tokio::time::timeout(deadline, owner.close())
            .await
            .unwrap()
            .unwrap();
    });
    assert_eq!(handle.dispose_count(), 1);
}

#[test]
fn rejected_or_failed_rediscovery_preserves_committed_database_for_reads() {
    for rejected in [true, false] {
        let owner = RuntimeOwner::new_assuming_initialized().unwrap();
        let rejection = CommandDispatchError::InvalidState {
            detail: "rediscovery rejected".to_owned(),
        };
        let error = BleEventError {
            category: BleErrorCategory::Gatt,
            message: "rediscovery failed after drafting a database".to_owned(),
            platform_details: None,
        };
        let command = RecordedCommand::DiscoverGatt(DiscoverGattCommand {
            command_id: 3.into(),
            connection_id: 1.into(),
            gatt_session_id: 3.into(),
        });
        let mut draft = read_database();
        draft.gatt_session_id = 3.into();
        draft.services[0].characteristics[0].id = 31.into();
        let rediscovery = if rejected {
            EventScenario::reject(command, rejection.clone())
        } else {
            EventScenario::emit_before_return(
                command,
                vec![
                    BleEvent::GattDiscovered {
                        command_id: 3.into(),
                        database: draft,
                    },
                    command_completed(3, Some(error.clone())),
                ],
            )
        };
        let (backend, handle) = FakeBackend::scripted([
            connect_scenario(1, 1, 10),
            read_discovery(),
            rediscovery,
            EventScenario::emit_before_return(
                read_command(4),
                vec![read_value(4, &[0x12, 0x34]), command_completed(4, None)],
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
            let deadline = Duration::from_secs(2);
            let connection = owner.connect(10.into(), deadline).await.unwrap();
            let database = owner.discover_gatt(connection, deadline).await.unwrap();
            assert_eq!(database, read_database());
            assert_eq!(
                owner.discover_gatt(connection, deadline).await,
                Err(if rejected {
                    WaiterError::DispatchRejected(rejection)
                } else {
                    WaiterError::Operation(error)
                })
            );
            assert!(!owner.faulted_for_test());
            assert_eq!(
                owner
                    .read_characteristic(connection, 3.into(), 31.into(), deadline)
                    .await,
                Err(WaiterError::GattNotReady),
                "unsuccessful rediscovery must not publish its draft session"
            );
            assert_eq!(
                owner
                    .read_characteristic(connection, database.gatt_session_id, 30.into(), deadline,)
                    .await,
                Ok(vec![0x12, 0x34]),
                "the committed session must remain usable after rediscovery fails"
            );
            owner.close().await.unwrap();
        });
        assert_eq!(handle.commands().len(), 4);
        assert_eq!(handle.dispose_count(), 1);
    }
}

#[test]
fn rediscovery_keeps_first_success_terminal_until_database_arrives() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let (backend, handle) = FakeBackend::scripted([
        connect_scenario(1, 1, 10),
        read_discovery(),
        EventScenario::accept(RecordedCommand::DiscoverGatt(DiscoverGattCommand {
            command_id: 3.into(),
            connection_id: 1.into(),
            gatt_session_id: 3.into(),
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
        let deadline = Duration::from_secs(2);
        let connection = owner.connect(10.into(), deadline).await.unwrap();
        assert_eq!(
            owner.discover_gatt(connection, deadline).await,
            Ok(read_database())
        );
        let pending = owner.discover_gatt(connection, deadline);
        tokio::pin!(pending);
        assert!(
            tokio::time::timeout(Duration::from_millis(20), &mut pending)
                .await
                .is_err()
        );
        for terminal in [
            command_completed(3, None),
            command_completed(
                3,
                Some(BleEventError {
                    category: BleErrorCategory::Gatt,
                    message: "conflicting duplicate terminal".to_owned(),
                    platform_details: None,
                }),
            ),
        ] {
            assert_eq!(handle.emit(terminal.clone()), EventIngressStatus::Delivered);
            tokio::time::timeout(deadline, async {
                while owner.next_event(deadline).await.unwrap() != Some(terminal.clone()) {}
            })
            .await
            .unwrap();
            assert!(
                tokio::time::timeout(Duration::from_millis(20), &mut pending)
                    .await
                    .is_err(),
                "neither terminal may complete discovery without a database"
            );
            assert_eq!(
                owner.discover_gatt(connection, deadline).await,
                Err(WaiterError::GattBusy),
                "the duplicate must not retire the pending discovery"
            );
        }
        let mut database = read_database();
        database.gatt_session_id = 3.into();
        assert_eq!(
            handle.emit(BleEvent::GattDiscovered {
                command_id: 3.into(),
                database: database.clone(),
            }),
            EventIngressStatus::Delivered
        );
        assert_eq!(pending.await, Ok(database));
        assert!(!owner.faulted_for_test());
        owner.close().await.unwrap();
    });
    assert_eq!(handle.commands().len(), 3);
    assert_eq!(handle.dispose_count(), 1);
}

#[test]
fn pending_rediscovery_blocks_enable_until_commit_after_subscription_retirement() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let (backend, handle) = FakeBackend::scripted([
        connect_scenario(1, 1, 10),
        notification_discovery(),
        notification_enabled_scenario(3, 3),
        EventScenario::emit_before_return(
            notification_command(4, 3, NotificationAction::Disable),
            vec![
                notification_state(4, 3, NotificationState::Disabled),
                command_completed(4, None),
            ],
        ),
        EventScenario::accept(RecordedCommand::DiscoverGatt(DiscoverGattCommand {
            command_id: 5.into(),
            connection_id: 1.into(),
            gatt_session_id: 4.into(),
        })),
        EventScenario::emit_before_return(
            RecordedCommand::SetNotifications(SetNotificationsCommand {
                command_id: 6.into(),
                connection_id: 1.into(),
                gatt_session_id: 4.into(),
                subscription_id: 5.into(),
                characteristic_id: 30.into(),
                action: NotificationAction::Enable {
                    mode: NotificationModeRequest::PlatformDefault,
                },
            }),
            vec![
                BleEvent::NotificationStateChanged {
                    command_id: 6.into(),
                    connection_id: 1.into(),
                    gatt_session_id: 4.into(),
                    subscription_id: 5.into(),
                    characteristic_id: 30.into(),
                    state: NotificationState::Enabled {
                        actual_mode: Some(NotificationMode::Notification),
                    },
                },
                command_completed(6, None),
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
        let deadline = Duration::from_secs(2);
        let connection = owner.connect(10.into(), deadline).await.unwrap();
        let database = owner.discover_gatt(connection, deadline).await.unwrap();
        let session = database.gatt_session_id;
        let characteristic = database.services[0].characteristics[0].id;
        let (subscription, _) = owner
            .enable_notifications(
                connection,
                session,
                characteristic,
                NotificationModeRequest::PlatformDefault,
                deadline,
            )
            .await
            .unwrap();
        owner
            .disable_notifications(connection, session, subscription, deadline)
            .await
            .unwrap();
        assert_eq!(
            owner
                .disable_notifications(connection, session, subscription, deadline)
                .await,
            Err(WaiterError::SubscriptionNotActive)
        );

        let pending = owner.discover_gatt(connection, deadline);
        tokio::pin!(pending);
        assert!(
            tokio::time::timeout(Duration::from_millis(20), &mut pending)
                .await
                .is_err()
        );
        assert_eq!(handle.commands().len(), 5, "rediscovery must be admitted");
        let mut draft = database;
        draft.gatt_session_id = 4.into();
        for database_arrived in [false, true] {
            if database_arrived {
                assert_eq!(
                    handle.emit(BleEvent::GattDiscovered {
                        command_id: 5.into(),
                        database: draft.clone(),
                    }),
                    EventIngressStatus::Delivered
                );
                assert!(
                    tokio::time::timeout(Duration::from_millis(20), &mut pending)
                        .await
                        .is_err(),
                    "a database draft alone must not retire discovery"
                );
            }
            assert_eq!(
                owner
                    .enable_notifications(
                        connection,
                        session,
                        characteristic,
                        NotificationModeRequest::PlatformDefault,
                        deadline,
                    )
                    .await,
                Err(WaiterError::GattBusy)
            );
            assert_eq!(
                owner
                    .enable_notifications(
                        connection,
                        draft.gatt_session_id,
                        characteristic,
                        NotificationModeRequest::PlatformDefault,
                        deadline,
                    )
                    .await,
                Err(WaiterError::GattNotReady),
                "the draft session must not be published before completion"
            );
            assert_eq!(
                handle.commands().len(),
                5,
                "blocked enable must not dispatch"
            );
        }
        assert_eq!(
            handle.emit(command_completed(5, None)),
            EventIngressStatus::Delivered
        );
        assert_eq!(pending.await, Ok(draft.clone()));
        assert_eq!(
            owner
                .enable_notifications(
                    connection,
                    session,
                    characteristic,
                    NotificationModeRequest::PlatformDefault,
                    deadline,
                )
                .await,
            Err(WaiterError::GattNotReady),
            "successful rediscovery must invalidate the old GATT session"
        );
        assert_eq!(
            owner
                .enable_notifications(
                    connection,
                    draft.gatt_session_id,
                    characteristic,
                    NotificationModeRequest::PlatformDefault,
                    deadline,
                )
                .await,
            Ok((5.into(), Some(NotificationMode::Notification)))
        );
        for invalid_session in [session, draft.gatt_session_id] {
            assert_eq!(
                owner
                    .disable_notifications(connection, invalid_session, subscription, deadline)
                    .await,
                Err(WaiterError::SubscriptionNotActive),
                "enabling a new subscription must not revive the retired route"
            );
        }
        assert!(!owner.faulted_for_test());
        owner.close().await.unwrap();
    });
    assert_eq!(handle.commands().len(), 6);
    assert_eq!(handle.dispose_count(), 1);
}

#[test]
fn prelaunch_rediscovery_cancellation_preserves_committed_database_for_reads() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let (backend, handle) = FakeBackend::scripted([
        connect_scenario(1, 1, 10),
        read_discovery(),
        EventScenario::emit_before_return(
            read_command(4),
            vec![read_value(4, &[0x56, 0x78]), command_completed(4, None)],
        ),
        gatt_scenario(5, 1, 4),
    ]);
    owner
        .attach_hub(BleHub::new(Arc::new(backend), owner.event_sink()).unwrap())
        .unwrap();
    let host = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    host.block_on(async {
        let deadline = Duration::from_secs(2);
        let connection = owner.connect(10.into(), deadline).await.unwrap();
        let database = owner.discover_gatt(connection, deadline).await.unwrap();
        assert_eq!(database, read_database());
        let entered = Arc::new(Notify::new());
        let release = Arc::new(Notify::new());
        let (checked_tx, checked_rx) = oneshot::channel();
        let mut pending = Box::pin(
            owner.run_operation(deadline, |handoff| CancelledRediscovery {
                handoff,
                gate: HoldActor {
                    entered: Arc::clone(&entered),
                    release: Arc::clone(&release),
                },
                database: database.clone(),
                checked_tx,
            }),
        );
        tokio::time::timeout(deadline, async {
            tokio::select! {
                _ = entered.notified() => {}
                result = &mut pending => panic!("rediscovery passed the actor gate: {result:?}"),
            }
        })
        .await
        .unwrap();
        // Drop the real begin guard while the actor owns the message but cannot launch it.
        drop(pending);
        release.notify_one();
        let (command_id, discarded_session) = tokio::time::timeout(deadline, checked_rx)
            .await
            .unwrap()
            .unwrap();
        assert_eq!((command_id, discarded_session), (3.into(), 3.into()));
        assert_eq!(handle.commands().len(), 2, "cancellation must not dispatch");
        assert_eq!(
            owner
                .read_characteristic(connection, discarded_session, 30.into(), deadline)
                .await,
            Err(WaiterError::GattNotReady)
        );
        assert_eq!(
            owner
                .read_characteristic(connection, database.gatt_session_id, 30.into(), deadline)
                .await,
            Ok(vec![0x56, 0x78]),
            "cancelled rediscovery must leave the committed session readable"
        );
        assert_eq!(
            owner.discover_gatt_session(connection, deadline).await,
            Ok(4.into()),
            "cancelled rediscovery must not reuse its discarded resource ID"
        );
        assert!(!owner.faulted_for_test());
        owner.close().await.unwrap();
    });
    assert_eq!(handle.commands().len(), 4);
    assert_eq!(handle.dispose_count(), 1);
}

#[test]
fn connection_leaf_prelaunch_cancellations_preserve_siblings_database_and_active_subscription() {
    for leaf in 0..6 {
        let owner = RuntimeOwner::new_assuming_initialized().unwrap();
        let (backend, handle) = FakeBackend::scripted([
            connect_scenario(1, 1, 10),
            notification_discovery(),
            notification_enabled_scenario(3, 3),
        ]);
        owner
            .attach_hub(BleHub::new(Arc::new(backend), owner.event_sink()).unwrap())
            .unwrap();
        let host = tokio::runtime::Builder::new_multi_thread()
            .enable_time()
            .build()
            .unwrap();
        host.block_on(async {
            let deadline = Duration::from_secs(2);
            let connection = owner.connect(10.into(), deadline).await.unwrap();
            let database = owner.discover_gatt(connection, deadline).await.unwrap();
            let session = database.gatt_session_id;
            let characteristic = database.services[0].characteristics[0].id;
            let (active_subscription, _) = owner
                .enable_notifications(
                    connection,
                    session,
                    characteristic,
                    NotificationModeRequest::PlatformDefault,
                    deadline,
                )
                .await
                .unwrap();
            let expectation = [
                WaiterExpectation::RssiRead(connection),
                WaiterExpectation::CharacteristicRead(connection, session, characteristic),
                WaiterExpectation::CharacteristicWritten(connection, session, characteristic),
                WaiterExpectation::Disconnected(connection),
                WaiterExpectation::NotificationsEnabled(connection, session, 100.into(), characteristic),
                WaiterExpectation::NotificationsDisabled(connection, session, active_subscription, characteristic),
            ][leaf];
            let entered = Arc::new(Notify::new());
            let release = Arc::new(Notify::new());
            let (checked_tx, checked_rx) = oneshot::channel();
            let mut pending = Box::pin(owner.run_operation(
                deadline,
                |handoff| CancelledConnectionLeaf {
                        handoff,
                        gate: HoldActor {
                            entered: Arc::clone(&entered),
                            release: Arc::clone(&release),
                        },
                        peripheral_id: 10.into(),
                        database,
                        active_subscription,
                        expectation,
                        checked_tx,
                },
            ));
            tokio::time::timeout(deadline, async {
                tokio::select! {
                    _ = entered.notified() => {}
                    result = &mut pending => panic!("{expectation:?} passed the actor gate: {result:?}"),
                }
            })
            .await
            .unwrap();
            drop(pending);
            release.notify_one();
            tokio::time::timeout(deadline, checked_rx)
                .await
                .unwrap()
                .unwrap();
            assert_eq!(handle.commands().len(), 3, "{expectation:?} must not dispatch");
            assert!(!owner.faulted_for_test());
            owner.close().await.unwrap();
        });
        assert_eq!(handle.commands().len(), 3);
        assert_eq!(handle.dispose_count(), 1);
    }
}

#[test]
fn discover_gatt_binds_session_to_connection_and_disconnect_retires_it() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let (backend, handle) = FakeBackend::scripted([
        connect_scenario(1, 1, 10),
        gatt_scenario(2, 1, 2),
        gatt_scenario(3, 1, 3),
        EventScenario::accept(RecordedCommand::DiscoverGatt(DiscoverGattCommand {
            command_id: CommandId { value: 4 },
            connection_id: ConnectionId { value: 1 },
            gatt_session_id: GattSessionId { value: 4 },
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
            Ok(GattSessionId { value: 2 })
        );
        assert_eq!(
            owner
                .discover_gatt_session(connection, Duration::from_secs(2))
                .await,
            Ok(GattSessionId { value: 3 })
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
                    command_id: CommandId { value: 4 },
                    database: GattDatabase {
                        connection_id: connection,
                        gatt_session_id: GattSessionId { value: 4 },
                        services: Vec::new(),
                    },
                }),
                EventIngressStatus::Delivered
            );
            assert_eq!(
                handle.emit(BleEvent::CommandCompleted {
                    command_id: CommandId { value: 4 },
                    error: None,
                }),
                EventIngressStatus::Delivered
            );
            assert_eq!(pending.as_mut().await, Ok(GattSessionId { value: 4 }));
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
        connect_scenario(1, 1, 10),
        EventScenario::emit_before_return(
            RecordedCommand::DiscoverGatt(DiscoverGattCommand {
                command_id: CommandId { value: 2 },
                connection_id: ConnectionId { value: 1 },
                gatt_session_id: GattSessionId { value: 2 },
            }),
            vec![BleEvent::ConnectionStateChanged {
                command_id: None,
                connection_id: ConnectionId { value: 1 },
                result: ConnectionStateResult::Ok {
                    state: ConnectionState::Disconnected,
                },
            }],
        ),
        connect_scenario(3, 3, 10),
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
            Ok(ConnectionId { value: 3 })
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
        connect_scenario(1, 1, 10),
        EventScenario::emit_before_return(
            RecordedCommand::DiscoverGatt(DiscoverGattCommand {
                command_id: CommandId { value: 2 },
                connection_id: ConnectionId { value: 1 },
                gatt_session_id: GattSessionId { value: 2 },
            }),
            vec![BleEvent::CommandCompleted {
                command_id: CommandId { value: 2 },
                error: Some(error.clone()),
            }],
        ),
        gatt_scenario(3, 1, 3),
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
            Ok(GattSessionId { value: 3 })
        );
        owner.close().await.unwrap();
    });
    assert_eq!(handle.commands().len(), 3);
    assert_eq!(handle.dispose_count(), 1);
}
