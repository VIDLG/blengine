use super::support::{
    Arc, BleErrorCategory, BleEvent, BleEventError, BleHub, CommandDispatchError, CommandId,
    ConnectionId, ConnectionState, ConnectionStateResult, DiscoverGattCommand, Duration,
    EventIngressStatus, EventScenario, FakeBackend, GattDatabase, GattSessionId, HoldActor,
    InitializeCommand, NotificationModeRequest, Notify, PeripheralId, RecordedCommand,
    RuntimeOwner, SubscriptionId, WaiterError, WaiterExpectation, WaiterSuccess, command_completed,
    connect_scenario, gatt_scenario, init_command, mpsc, notification_discovery,
    notification_enabled_scenario, oneshot, read_command, read_database, read_discovery,
    read_value,
};
use crate::runtime::{BlengActor, RuntimePhase, owner::BeginHandoff};
use kameo::prelude::{Context, Message};

struct BeginInitializeWithBlockedReservation {
    handoff: BeginHandoff,
    entered_tx: oneshot::Sender<()>,
    release_rx: mpsc::Receiver<()>,
}

impl Message<BeginInitializeWithBlockedReservation> for BlengActor {
    type Reply = Result<(), WaiterError>;

    async fn handle(
        &mut self,
        message: BeginInitializeWithBlockedReservation,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        self.require_open()?;
        if self.initialization.require_not_started().is_err() {
            return Err(WaiterError::OperationBusy);
        }
        let (command_id, _result_rx) = self.register_waiter(WaiterExpectation::Initialized)?;
        self.launch_dispatch(
            ctx,
            &message.handoff,
            command_id,
            move |actor| {
                actor.initialization.reserve_initialize(command_id);
                message
                    .entered_tx
                    .send(())
                    .expect("begin caller dropped before reservation");
                // Hold the synchronous handoff open after try_launch wins, before the begin reply.
                message
                    .release_rx
                    .recv_timeout(Duration::from_secs(5))
                    .expect("reservation gate was not released");
            },
            move |hub| hub.initialize(InitializeCommand { command_id }),
        )
    }
}

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
        let mut pending = Box::pin(owner.begin_operation(deadline, |actor, handoff| async move {
            actor.ask(BeginInitializeWithBlockedReservation {
                handoff,
                entered_tx,
                release_rx,
            }).await
        }));
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

struct CancelledRediscovery {
    handoff: BeginHandoff,
    gate: HoldActor,
    database: GattDatabase,
    checked_tx: oneshot::Sender<(CommandId, GattSessionId)>,
}

impl Message<CancelledRediscovery> for BlengActor {
    type Reply = Result<(), WaiterError>;

    async fn handle(
        &mut self,
        message: CancelledRediscovery,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        message.gate.entered.notify_one();
        message.gate.release.notified().await;
        let connection_id = message.database.connection_id;
        let key = self.connections.validate_gatt_discovery(connection_id)?;
        let session_id = self.allocate_resource_id()?.into();
        let (command_id, mut result_rx) =
            self.register_waiter(WaiterExpectation::GattDiscovered(connection_id, session_id))?;
        let mut reserved = false;
        assert_eq!(
            self.launch_dispatch(
                ctx,
                &message.handoff,
                command_id,
                |actor| {
                    reserved = true;
                    actor
                        .connections
                        .reserve_gatt_discovery(key, command_id, session_id);
                },
                move |hub| {
                    hub.discover_gatt(DiscoverGattCommand {
                        command_id,
                        connection_id,
                        gatt_session_id: session_id,
                    })
                },
            ),
            Err(WaiterError::Cancelled)
        );
        assert!(
            !reserved,
            "cancelled rediscovery must never install a draft"
        );
        assert_eq!(result_rx.try_recv(), Ok(Err(WaiterError::Cancelled)));
        assert_eq!(
            self.connections.validate_gatt_discovery(connection_id),
            Ok(key)
        );
        let characteristic_id = message.database.services[0].characteristics[0].id;
        assert_eq!(
            self.connections.validate_gatt_io(
                connection_id,
                message.database.gatt_session_id,
                characteristic_id,
                true,
            ),
            Ok(key),
            "the committed session remains readable without a pending discovery"
        );
        assert_eq!(
            self.connections
                .validate_gatt_io(connection_id, session_id, characteristic_id, true),
            Err(WaiterError::GattNotReady),
            "the cancelled draft session must not become usable"
        );
        assert!(
            self.connections
                .resolve_dispatch(command_id, &Ok(()))
                .is_none()
        );
        message.checked_tx.send((command_id, session_id)).unwrap();
        Ok(())
    }
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
        let mut pending = Box::pin(owner.begin_operation(deadline, |actor, handoff| {
            let message = CancelledRediscovery {
                handoff,
                gate: HoldActor {
                    entered: Arc::clone(&entered),
                    release: Arc::clone(&release),
                },
                database: database.clone(),
                checked_tx,
            };
            async move { actor.ask(message).await }
        }));
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

struct CancelledConnectionLeaf {
    handoff: BeginHandoff,
    gate: HoldActor,
    peripheral_id: PeripheralId,
    database: GattDatabase,
    active_subscription: SubscriptionId,
    expectation: WaiterExpectation,
    checked_tx: oneshot::Sender<()>,
}

impl Message<CancelledConnectionLeaf> for BlengActor {
    type Reply = Result<(), WaiterError>;

    async fn handle(
        &mut self,
        message: CancelledConnectionLeaf,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        let key = message.peripheral_id;
        let connection_id = message.database.connection_id;
        let session_id = message.database.gatt_session_id;
        let characteristic_id = message.database.services[0].characteristics[0].id;
        let provisional_subscription = 100.into();
        let expectation = message.expectation;
        let cancelling_rssi = matches!(expectation, WaiterExpectation::RssiRead(_));
        let sibling_expectation = if cancelling_rssi {
            WaiterExpectation::CharacteristicRead(connection_id, session_id, characteristic_id)
        } else {
            WaiterExpectation::RssiRead(connection_id)
        };
        let (sibling_id, mut sibling_rx) = self.register_waiter(sibling_expectation)?;
        // Retain an unrelated pending sibling without dispatching native work.
        if cancelling_rssi {
            self.connections
                .reserve_read(key, sibling_id, session_id, characteristic_id);
        } else {
            self.connections.reserve_rssi(key, sibling_id);
        }
        message.gate.entered.notify_one();
        message.gate.release.notified().await;
        let (command_id, mut result_rx) = self.register_waiter(expectation)?;
        let mut reserved = false;
        assert_eq!(
            self.launch_dispatch(
                ctx,
                &message.handoff,
                command_id,
                |actor| {
                    reserved = true;
                    match expectation {
                        WaiterExpectation::RssiRead(_) => {
                            actor.connections.reserve_rssi(key, command_id)
                        }
                        WaiterExpectation::CharacteristicRead(..) => actor
                            .connections
                            .reserve_read(key, command_id, session_id, characteristic_id),
                        WaiterExpectation::CharacteristicWritten(..) => actor
                            .connections
                            .reserve_write(key, command_id, session_id, characteristic_id),
                        WaiterExpectation::Disconnected(_) => {
                            actor.connections.reserve_disconnect(key, command_id)
                        }
                        WaiterExpectation::NotificationsEnabled(..) => {
                            actor.connections.reserve_notification_enable(
                                key,
                                command_id,
                                provisional_subscription,
                                session_id,
                                characteristic_id,
                                NotificationModeRequest::PlatformDefault,
                            )
                        }
                        WaiterExpectation::NotificationsDisabled(..) => {
                            actor.connections.reserve_notification_disable(
                                key,
                                command_id,
                                message.active_subscription,
                            )
                        }
                        _ => unreachable!(),
                    }
                },
                move |_| panic!("cancelled {expectation:?} must never dispatch"),
            ),
            Err(WaiterError::Cancelled)
        );
        assert!(!reserved, "{expectation:?} must skip its reserve closure");
        assert_eq!(
            result_rx.try_recv(),
            Ok(Err(WaiterError::Cancelled)),
            "{expectation:?} must cancel only its own waiter"
        );
        assert!(matches!(
            sibling_rx.try_recv(),
            Err(oneshot::error::TryRecvError::Empty)
        ));
        assert_eq!(
            self.connections.waiter_result(1.into()),
            Some(Ok(WaiterSuccess::Connected(connection_id)))
        );
        assert!(
            self.connections
                .resolve_dispatch(command_id, &Ok(()))
                .is_none()
        );
        // A real correlated callback must still belong to the sibling's unresolved handoff.
        let (callback, expected) = if cancelling_rssi {
            let value = vec![0x56, 0x78];
            (
                BleEvent::CharacteristicRead {
                    command_id: sibling_id,
                    connection_id,
                    gatt_session_id: session_id,
                    characteristic_id,
                    value: value.clone(),
                },
                WaiterSuccess::CharacteristicRead {
                    connection_id,
                    gatt_session_id: session_id,
                    characteristic_id,
                    value,
                },
            )
        } else {
            (
                BleEvent::RssiRead {
                    command_id: sibling_id,
                    connection_id,
                    rssi: -42,
                },
                WaiterSuccess::RssiRead(connection_id, -42),
            )
        };
        assert_eq!(
            self.connections
                .buffer_before_dispatch(callback.clone())
                .unwrap(),
            None
        );
        assert_eq!(self.connections.waiter_result(sibling_id), None);
        let resolution = self
            .connections
            .resolve_dispatch(sibling_id, &Ok(()))
            .unwrap();
        assert!(matches!(
            resolution.effect,
            crate::runtime::reservations::DispatchEffect::None
        ));
        assert_eq!(resolution.replay, vec![callback]);
        for event in &resolution.replay {
            self.connections.record_event(event).unwrap();
        }
        assert_eq!(self.connections.waiter_result(sibling_id), None);
        self.connections.record_terminal(sibling_id, &Ok(()));
        assert_eq!(
            self.connections.waiter_result(sibling_id),
            Some(Ok(expected.clone()))
        );
        self.settle_waiter(sibling_id);
        assert_eq!(sibling_rx.try_recv(), Ok(Ok(expected)));
        self.connections.retire_completed();
        assert_eq!(self.connections.waiter_result(sibling_id), None);
        assert_eq!(self.connections.validate_rssi(connection_id), Ok(key));
        assert_eq!(
            self.connections
                .validate_gatt_io(connection_id, session_id, characteristic_id, false),
            Ok(key),
            "the committed database remains usable without a draft or cancelled I/O slot"
        );
        assert_eq!(
            self.connections.validate_notification_disable(
                connection_id,
                session_id,
                provisional_subscription
            ),
            Err(WaiterError::SubscriptionNotActive)
        );
        assert_eq!(
            self.connections.validate_notification_disable(
                connection_id,
                session_id,
                message.active_subscription
            ),
            Ok((key, characteristic_id)),
            "the active route keeps its exact session/characteristic and no disable transition"
        );
        assert_eq!(
            self.connections.validate_notification_enable(
                connection_id,
                session_id,
                characteristic_id
            ),
            Err(WaiterError::OperationBusy),
            "the existing route still excludes a duplicate enable"
        );
        assert_eq!(
            self.connections
                .validate_gatt_io(connection_id, session_id, characteristic_id, true),
            Err(WaiterError::OperationBusy),
            "the active route still excludes reads of its characteristic"
        );
        assert_eq!(
            self.connections.validate_gatt_discovery(connection_id),
            Err(WaiterError::GattBusy)
        );
        message.checked_tx.send(()).unwrap();
        Ok(())
    }
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
            let mut pending = Box::pin(owner.begin_operation(deadline, |actor, handoff| {
                let message = CancelledConnectionLeaf {
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
                };
                async move { actor.ask(message).await }
            }));
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
