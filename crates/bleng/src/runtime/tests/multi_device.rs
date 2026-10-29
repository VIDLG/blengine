use super::support::{
    AdvertisementObservation, Arc, BleEvent, BleHub, CommandId, ConnectionId, DiscoverGattCommand,
    Duration, EventIngressStatus, EventScenario, FakeBackend, GattCharacteristicId, GattSessionId,
    NotificationAction, NotificationMode, NotificationModeRequest, NotificationState, PeripheralId,
    ReadCharacteristicCommand, RecordedCommand, RuntimeOwner, ScanOptions, ScanSessionId,
    ScanStopReason, ScanStopResult, SetNotificationsCommand, StartScanCommand, StopScanCommand,
    SubscriptionId, WaiterError, WriteCharacteristicCommand, WriteType, command_completed,
    connect_scenario, disconnected_event, init_command, init_snapshot, init_snapshot_event,
    read_database,
};

#[test]
fn one_hub_two_peripherals_keep_gatt_operations_and_notification_routes_independent() {
    let owner = RuntimeOwner::new().unwrap();
    let mut database_a = read_database();
    database_a.connection_id = ConnectionId { value: 2 };
    database_a.gatt_session_id = GattSessionId { value: 4 };
    database_a.services[0].characteristics[0]
        .properties
        .writable_with_response = true;
    let mut database_b = database_a.clone();
    database_b.connection_id = ConnectionId { value: 3 };
    database_b.gatt_session_id = GattSessionId { value: 5 };
    database_b.services[0].characteristics[0].id = GattCharacteristicId { value: 31 };
    database_b.services[0].characteristics[0]
        .properties
        .notifiable = true;
    let a = GattCharacteristicId { value: 30 };
    let b = GattCharacteristicId { value: 31 };
    let (backend, handle) = FakeBackend::scripted([
        EventScenario::emit_before_return(
            init_command(1),
            vec![init_snapshot_event(Some(1)), command_completed(1, None)],
        ),
        EventScenario::emit_before_return(
            RecordedCommand::StartScan(StartScanCommand {
                command_id: CommandId { value: 2 },
                scan_session_id: ScanSessionId { value: 1 },
                options: ScanOptions::default(),
            }),
            vec![
                BleEvent::ScanStarted {
                    command_id: CommandId { value: 2 },
                    scan_id: ScanSessionId { value: 1 },
                },
                command_completed(2, None),
            ],
        ),
        EventScenario::emit_before_return(
            RecordedCommand::StopScan(StopScanCommand {
                command_id: CommandId { value: 4 },
                scan_session_id: ScanSessionId { value: 1 },
            }),
            vec![
                BleEvent::ScanStopped {
                    scan_id: ScanSessionId { value: 1 },
                    result: ScanStopResult::Ok {
                        reason: ScanStopReason::Requested {
                            command_id: CommandId { value: 4 },
                        },
                    },
                },
                command_completed(4, None),
            ],
        ),
        connect_scenario(5, 2, 10),
        connect_scenario(6, 3, 20),
        EventScenario::emit_before_return(
            RecordedCommand::DiscoverGatt(DiscoverGattCommand {
                command_id: CommandId { value: 8 },
                connection_id: ConnectionId { value: 2 },
                gatt_session_id: GattSessionId { value: 4 },
            }),
            vec![
                BleEvent::GattDiscovered {
                    command_id: CommandId { value: 8 },
                    database: database_a,
                },
                command_completed(8, None),
            ],
        ),
        EventScenario::emit_before_return(
            RecordedCommand::DiscoverGatt(DiscoverGattCommand {
                command_id: CommandId { value: 9 },
                connection_id: ConnectionId { value: 3 },
                gatt_session_id: GattSessionId { value: 5 },
            }),
            vec![
                BleEvent::GattDiscovered {
                    command_id: CommandId { value: 9 },
                    database: database_b,
                },
                command_completed(9, None),
            ],
        ),
        EventScenario::emit_before_return(
            RecordedCommand::ReadCharacteristic(ReadCharacteristicCommand {
                command_id: CommandId { value: 10 },
                connection_id: ConnectionId { value: 2 },
                gatt_session_id: GattSessionId { value: 4 },
                characteristic_id: a,
            }),
            vec![
                BleEvent::CharacteristicRead {
                    command_id: CommandId { value: 10 },
                    connection_id: ConnectionId { value: 2 },
                    gatt_session_id: GattSessionId { value: 4 },
                    characteristic_id: a,
                    value: vec![0xa0],
                },
                command_completed(10, None),
            ],
        ),
        EventScenario::emit_before_return(
            RecordedCommand::SetNotifications(SetNotificationsCommand {
                command_id: CommandId { value: 11 },
                connection_id: ConnectionId { value: 3 },
                gatt_session_id: GattSessionId { value: 5 },
                subscription_id: SubscriptionId { value: 6 },
                characteristic_id: b,
                action: NotificationAction::Enable {
                    mode: NotificationModeRequest::PlatformDefault,
                },
            }),
            vec![
                BleEvent::NotificationStateChanged {
                    command_id: CommandId { value: 11 },
                    connection_id: ConnectionId { value: 3 },
                    gatt_session_id: GattSessionId { value: 5 },
                    subscription_id: SubscriptionId { value: 6 },
                    characteristic_id: b,
                    state: NotificationState::Enabled {
                        actual_mode: Some(NotificationMode::Notification),
                    },
                },
                command_completed(11, None),
            ],
        ),
        EventScenario::emit_before_return(
            RecordedCommand::WriteCharacteristic(WriteCharacteristicCommand {
                command_id: CommandId { value: 12 },
                connection_id: ConnectionId { value: 2 },
                gatt_session_id: GattSessionId { value: 4 },
                characteristic_id: a,
                value: vec![0xa1],
                write_type: WriteType::WithResponse,
            }),
            vec![
                BleEvent::CharacteristicWritten {
                    command_id: CommandId { value: 12 },
                    connection_id: ConnectionId { value: 2 },
                    gatt_session_id: GattSessionId { value: 4 },
                    characteristic_id: a,
                },
                command_completed(12, None),
            ],
        ),
        EventScenario::accept(RecordedCommand::WriteCharacteristic(
            WriteCharacteristicCommand {
                command_id: CommandId { value: 13 },
                connection_id: ConnectionId { value: 3 },
                gatt_session_id: GattSessionId { value: 5 },
                characteristic_id: b,
                value: vec![0xb1],
                write_type: WriteType::WithResponse,
            },
        )),
        EventScenario::accept(RecordedCommand::ReadCharacteristic(
            ReadCharacteristicCommand {
                command_id: CommandId { value: 14 },
                connection_id: ConnectionId { value: 2 },
                gatt_session_id: GattSessionId { value: 4 },
                characteristic_id: a,
            },
        )),
        connect_scenario(16, 7, 10),
        EventScenario::emit_before_return(
            RecordedCommand::SetNotifications(SetNotificationsCommand {
                command_id: CommandId { value: 18 },
                connection_id: ConnectionId { value: 3 },
                gatt_session_id: GattSessionId { value: 5 },
                subscription_id: SubscriptionId { value: 6 },
                characteristic_id: b,
                action: NotificationAction::Disable,
            }),
            vec![
                BleEvent::NotificationStateChanged {
                    command_id: CommandId { value: 18 },
                    connection_id: ConnectionId { value: 3 },
                    gatt_session_id: GattSessionId { value: 5 },
                    subscription_id: SubscriptionId { value: 6 },
                    characteristic_id: b,
                    state: NotificationState::Disabled,
                },
                command_completed(18, None),
            ],
        ),
        EventScenario::emit_before_return(
            RecordedCommand::ReadCharacteristic(ReadCharacteristicCommand {
                command_id: CommandId { value: 19 },
                connection_id: ConnectionId { value: 3 },
                gatt_session_id: GattSessionId { value: 5 },
                characteristic_id: b,
            }),
            vec![
                BleEvent::CharacteristicRead {
                    command_id: CommandId { value: 19 },
                    connection_id: ConnectionId { value: 3 },
                    gatt_session_id: GattSessionId { value: 5 },
                    characteristic_id: b,
                    value: vec![0xb3],
                },
                command_completed(19, None),
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
        assert_eq!(owner.initialize(deadline).await, Ok(init_snapshot()));
        let scan = owner
            .start_scan(ScanOptions::default(), deadline)
            .await
            .unwrap();
        assert_eq!(scan, ScanSessionId { value: 1 });
        let advertisement = |peripheral, name: &str| BleEvent::AdvertisementObserved {
            advertisement: AdvertisementObservation {
                scan_session_id: scan,
                peripheral_id: PeripheralId { value: peripheral },
                local_name: Some(name.to_owned()),
                advertised_services: Vec::new(),
                manufacturer_data: Vec::new(),
                service_data: Vec::new(),
                rssi: Some(-40),
                connectable: Some(true),
            },
        };
        assert_eq!(
            owner.start_scan(ScanOptions::default(), deadline).await,
            Err(WaiterError::ScanBusy)
        );
        let advertised_a = advertisement(10, "DK-A");
        let advertised_b = advertisement(20, "DK-B");
        assert_eq!(
            handle.emit(advertised_a.clone()),
            EventIngressStatus::Delivered
        );
        assert_eq!(
            handle.emit(advertised_b.clone()),
            EventIngressStatus::Delivered
        );
        let mut observed = Vec::new();
        for _ in 0..6 {
            observed.push(owner.next_event(deadline).await.unwrap().unwrap());
        }
        assert!(observed.contains(&advertised_a));
        assert!(observed.contains(&advertised_b));
        assert_eq!(owner.stop_scan(scan, deadline).await, Ok(()));
        let connection_a = owner
            .connect(PeripheralId { value: 10 }, deadline)
            .await
            .unwrap();
        let connection_b = owner
            .connect(PeripheralId { value: 20 }, deadline)
            .await
            .unwrap();
        assert_eq!(
            (connection_a, connection_b),
            (ConnectionId { value: 2 }, ConnectionId { value: 3 })
        );
        assert_eq!(
            owner.connect(PeripheralId { value: 10 }, deadline).await,
            Err(WaiterError::PeripheralBusy)
        );
        let session_a = owner
            .discover_gatt_session(connection_a, deadline)
            .await
            .unwrap();
        let session_b = owner
            .discover_gatt_session(connection_b, deadline)
            .await
            .unwrap();
        assert_eq!(
            (session_a, session_b),
            (GattSessionId { value: 4 }, GattSessionId { value: 5 })
        );
        assert_eq!(
            owner
                .read_characteristic(connection_a, session_a, a, deadline)
                .await,
            Ok(vec![0xa0])
        );
        let (subscription_b, mode) = owner
            .enable_notifications(
                connection_b,
                session_b,
                b,
                NotificationModeRequest::PlatformDefault,
                deadline,
            )
            .await
            .unwrap();
        assert_eq!(
            (subscription_b, mode),
            (
                SubscriptionId { value: 6 },
                Some(NotificationMode::Notification)
            )
        );
        assert_eq!(
            owner
                .write_characteristic(
                    connection_a,
                    session_a,
                    a,
                    vec![0xa1],
                    WriteType::WithResponse,
                    deadline
                )
                .await,
            Ok(())
        );
        {
            let write_b = owner.write_characteristic(
                connection_b,
                session_b,
                b,
                vec![0xb1],
                WriteType::WithResponse,
                deadline,
            );
            tokio::pin!(write_b);
            tokio::select! {
                result = &mut write_b => panic!("B write completed before callbacks: {result:?}"),
                _ = tokio::time::sleep(Duration::from_millis(20)) => {}
            }
            let read_a = owner.read_characteristic(connection_a, session_a, a, deadline);
            tokio::pin!(read_a);
            tokio::select! {
                result = &mut read_a => panic!("A read completed before callbacks: {result:?}"),
                _ = tokio::time::sleep(Duration::from_millis(20)) => {}
            }
            assert_eq!(handle.commands().len(), 12);
            assert_eq!(
                handle.emit(disconnected_event(None, 2)),
                EventIngressStatus::Delivered
            );
            assert_eq!(read_a.await, Err(WaiterError::Disconnected));
            assert!(
                tokio::time::timeout(Duration::from_millis(20), &mut write_b)
                    .await
                    .is_err()
            );
            assert_eq!(
                handle.emit(BleEvent::CharacteristicWritten {
                    command_id: CommandId { value: 13 },
                    connection_id: connection_b,
                    gatt_session_id: session_b,
                    characteristic_id: b,
                }),
                EventIngressStatus::Delivered
            );
            assert_eq!(
                handle.emit(command_completed(13, None)),
                EventIngressStatus::Delivered
            );
            assert_eq!(write_b.await, Ok(()));
        }
        assert_eq!(
            owner
                .read_characteristic(connection_a, session_a, a, deadline)
                .await,
            Err(WaiterError::ConnectionNotActive)
        );
        let value_b = BleEvent::NotificationReceived {
            connection_id: connection_b,
            gatt_session_id: session_b,
            subscription_id: subscription_b,
            characteristic_id: b,
            value: vec![0xb2],
        };
        assert_eq!(handle.emit(value_b.clone()), EventIngressStatus::Delivered);
        tokio::time::timeout(deadline, async {
            loop {
                if owner.next_event(Duration::from_secs(2)).await.unwrap() == Some(value_b.clone())
                {
                    break;
                }
            }
        })
        .await
        .unwrap();
        assert_eq!(
            owner.connect(PeripheralId { value: 10 }, deadline).await,
            Ok(ConnectionId { value: 7 })
        );
        assert_eq!(
            owner
                .read_characteristic(connection_b, session_b, b, deadline)
                .await,
            Err(WaiterError::OperationBusy),
            "A's disconnect must not retire B's active subscription"
        );
        assert_eq!(
            owner
                .disable_notifications(connection_b, session_b, subscription_b, deadline)
                .await,
            Ok(())
        );
        assert_eq!(
            owner
                .read_characteristic(connection_b, session_b, b, deadline)
                .await,
            Ok(vec![0xb3])
        );
        assert_eq!(
            owner
                .read_characteristic(connection_a, session_a, a, deadline)
                .await,
            Err(WaiterError::ConnectionNotActive)
        );
        assert_eq!(owner.close().await, Ok(()));
        assert_eq!(owner.close().await, Ok(()));
    });
    assert_eq!(handle.commands().len(), 15);
    assert_eq!(handle.dispose_count(), 1);
}
