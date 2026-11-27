use std::{fmt::Debug, future::Future, sync::Arc, time::Duration};

use blehub::*;
use blehub_testkit::{
    CancellationPolicy, EventScenario, FakeBackend, FakeBackendHandle, RecordedCommand,
};
use bleng::{
    Bleng, BlengBusyReason, BlengContractFailure, BlengError, BlengNotReadyReason, BlengResource,
    BondOperation, ConnectOperation, DisableNotificationsOperation, DisconnectOperation,
    DiscoverGattOperation, EnableNotificationsOperation, InitializeOperation, MtuOperation,
    NotificationSubscription, ReadOperation, ReadRssiOperation, SetConnectionPriorityOperation,
    StartScanOperation, StopScanOperation, WriteOperation,
};

fn caller() -> tokio::runtime::Runtime {
    tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .unwrap()
}

fn snapshot() -> AdapterSnapshot {
    AdapterSnapshot {
        state: AdapterState::PoweredOn,
        scan_authorization: AuthorizationState::Authorized,
        connection_authorization: AuthorizationState::Authorized,
        name: Some("typed consumer".into()),
    }
}

fn terminal(command: u64) -> BleEvent {
    BleEvent::CommandCompleted {
        command_id: command.into(),
        error: None,
    }
}

fn initialized() -> BleEvent {
    BleEvent::AdapterSnapshotChanged {
        command_id: Some(1.into()),
        snapshot: snapshot(),
    }
}

fn initialize_command() -> RecordedCommand {
    RecordedCommand::Initialize(InitializeCommand {
        command_id: 1.into(),
    })
}

fn database() -> GattDatabase {
    GattDatabase {
        connection_id: 2.into(),
        gatt_session_id: 3.into(),
        services: vec![GattService {
            id: 20.into(),
            uuid: BluetoothUuid::canonicalize("180d").unwrap(),
            primary: Some(true),
            characteristics: vec![GattCharacteristic {
                id: 30.into(),
                uuid: BluetoothUuid::canonicalize("2a37").unwrap(),
                properties: CharacteristicProperties {
                    readable: true,
                    writable_with_response: true,
                    writable_without_response: true,
                    notifiable: true,
                    indicatable: true,
                },
                descriptors: vec![],
            }],
        }],
    }
}

async fn accepted(handle: &FakeBackendHandle, count: usize) {
    tokio::time::timeout(Duration::from_secs(2), async {
        while handle.commands().len() < count {
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("native dispatch did not arrive");
}

fn emit(handle: &FakeBackendHandle, event: BleEvent) {
    assert_eq!(handle.emit(event), EventIngressStatus::Delivered);
}

// Every typed result must await both pieces of evidence, not merely submission or
// synchronous native acceptance. Alternate event order across the full flow.
async fn complete<T: Debug>(
    handle: &FakeBackendHandle,
    count: usize,
    command_id: CommandId,
    wait: impl Future<Output = Result<T, BlengError>>,
    first: BleEvent,
    second: BleEvent,
) -> T {
    accepted(handle, count).await;
    assert_eq!(first.command_id(), Some(command_id));
    assert_eq!(second.command_id(), Some(command_id));
    tokio::pin!(wait);
    assert!(
        tokio::time::timeout(Duration::from_millis(5), &mut wait)
            .await
            .is_err()
    );
    emit(handle, first);
    assert!(
        tokio::time::timeout(Duration::from_millis(5), &mut wait)
            .await
            .is_err(),
        "partial evidence must not complete an operation"
    );
    emit(handle, second);
    tokio::time::timeout(Duration::from_secs(2), wait)
        .await
        .unwrap()
        .unwrap()
}

#[test]
fn stateful_admission_errors_are_typed_completed_results_with_unique_identity() {
    caller().block_on(async {
        let (backend, handle) = FakeBackend::scripted([
            EventScenario::emit_before_return(initialize_command(), vec![initialized(), terminal(1)]),
            EventScenario::emit_before_return(RecordedCommand::StartScan(StartScanCommand {
                command_id: 2.into(), scan_session_id: 1.into(), options: ScanOptions::default(),
            }), vec![BleEvent::ScanStarted { command_id: 2.into(), scan_id: 1.into() }, terminal(2)]),
        ]);
        let bleng = Bleng::new().unwrap();
        bleng.assemble_backend(Arc::new(backend)).await.unwrap();
        bleng.initialize(2000).await.unwrap();
        bleng.start_scan(ScanOptions::default(), 2000).await.unwrap();
        let busy = bleng.begin_start_scan(ScanOptions::default()).unwrap();

        assert!(matches!(busy.wait(0).await, Err(BlengError::InvalidTimeout)));
        assert!(matches!(busy.wait(2000).await, Err(BlengError::Busy { reason: BlengBusyReason::Scan })));
        assert!(matches!(busy.cancel().await, Err(BlengError::OperationAlreadyCompleted { command_id }) if command_id == 3.into()));
        assert!(matches!(busy.wait(2000).await, Err(BlengError::OperationResultConsumed)));
        let stale = bleng.begin_read_characteristic(99.into(), 98.into(), 97.into()).unwrap();

        assert!(matches!(stale.wait(2000).await, Err(BlengError::NotReady { reason: BlengNotReadyReason::ConnectionNotActive })));
        assert!(matches!(stale.cancel().await, Err(BlengError::OperationAlreadyCompleted { command_id }) if command_id == 4.into()));
        assert_eq!(handle.commands().len(), 2);
        bleng.shutdown().await.unwrap();
        assert!(matches!(bleng.begin_initialize(), Err(BlengError::Closed)));
    });
}

#[test]
fn external_consumer_names_and_executes_all_eleven_typed_operations() {
    caller().block_on(async {
        let commands = vec![
            initialize_command(),
            RecordedCommand::StartScan(StartScanCommand { command_id: 2.into(), scan_session_id: 1.into(), options: ScanOptions::default() }),
            RecordedCommand::StopScan(StopScanCommand { command_id: 3.into(), scan_session_id: 1.into() }),
            RecordedCommand::Connect(ConnectCommand { auto_connect: false, command_id: 4.into(), connection_id: 2.into(), peripheral_id: 10.into() }),
            RecordedCommand::DiscoverGatt(DiscoverGattCommand { command_id: 5.into(), connection_id: 2.into(), gatt_session_id: 3.into() }),
            RecordedCommand::ReadCharacteristic(ReadCharacteristicCommand { command_id: 6.into(), connection_id: 2.into(), gatt_session_id: 3.into(), characteristic_id: 30.into() }),
            RecordedCommand::WriteCharacteristic(WriteCharacteristicCommand { command_id: 7.into(), connection_id: 2.into(), gatt_session_id: 3.into(), characteristic_id: 30.into(), value: vec![7, 8], write_type: WriteType::WithResponse }),
            RecordedCommand::SetNotifications(SetNotificationsCommand { command_id: 8.into(), connection_id: 2.into(), gatt_session_id: 3.into(), subscription_id: 4.into(), characteristic_id: 30.into(), action: NotificationAction::Enable { mode: NotificationModeRequest::PlatformDefault } }),
            RecordedCommand::SetNotifications(SetNotificationsCommand { command_id: 9.into(), connection_id: 2.into(), gatt_session_id: 3.into(), subscription_id: 4.into(), characteristic_id: 30.into(), action: NotificationAction::Disable }),
            RecordedCommand::ReadRssi(ReadRssiCommand { command_id: 10.into(), connection_id: 2.into() }),
            RecordedCommand::Disconnect(DisconnectCommand { command_id: 11.into(), connection_id: 2.into() }),
        ];
        // The backend would accept control, so NotCancellable must be BLeng's local policy.
        let (backend, handle) = FakeBackend::scripted_with_cancellation(commands.clone().into_iter().map(EventScenario::accept), CancellationPolicy::Accept);
        let bleng = Bleng::new().unwrap();
        assert!(matches!(bleng.begin_initialize(),
            Err(BlengError::NotReady { reason: BlengNotReadyReason::BackendNotAssembled })));
        bleng.assemble_backend(Arc::new(backend)).await.unwrap();
        let init: Arc<InitializeOperation> = bleng.begin_initialize().unwrap();
        accepted(&handle, 1).await;
        assert!(matches!(init.cancel().await, Err(BlengError::OperationNotCancellable { command_id }) if command_id == 1.into() && command_id == init.command_id()));
        let value: AdapterSnapshot = complete(&handle, 1, init.command_id(), init.wait(2000), terminal(1), initialized()).await;
        assert_eq!(value, snapshot());
        assert!(matches!(init.wait(2000).await, Err(BlengError::OperationResultConsumed)));
        assert!(matches!(init.cancel().await, Err(BlengError::OperationAlreadyCompleted { command_id }) if command_id == 1.into() && command_id == init.command_id()));

        let start: Arc<StartScanOperation> = bleng.begin_start_scan(ScanOptions::default()).unwrap();
        let scan: ScanSessionId = complete(&handle, 2, start.command_id(), start.wait(2000), BleEvent::ScanStarted { command_id: 2.into(), scan_id: 1.into() }, terminal(2)).await;
        assert_eq!(scan, 1.into());
        let stop: Arc<StopScanOperation> = bleng.begin_stop_scan(scan).unwrap();
        accepted(&handle, 3).await;
        assert!(matches!(stop.cancel().await, Err(BlengError::OperationNotCancellable { command_id }) if command_id == 3.into() && command_id == stop.command_id()));
        let (): () = complete(&handle, 3, stop.command_id(), stop.wait(2000), terminal(3), BleEvent::ScanStopped { scan_id: scan, result: ScanStopResult::Ok { reason: ScanStopReason::Requested { command_id: 3.into() } } }).await;
        assert!(matches!(stop.cancel().await, Err(BlengError::OperationAlreadyCompleted { command_id }) if command_id == 3.into() && command_id == stop.command_id()));

        let connect: Arc<ConnectOperation> = bleng.begin_connect(10.into()).unwrap();
        let connection: ConnectionId = complete(&handle, 4, connect.command_id(), connect.wait(2000), BleEvent::ConnectionStateChanged { command_id: Some(4.into()), connection_id: 2.into(), result: ConnectionStateResult::Ok { state: ConnectionState::Connected } }, terminal(4)).await;
        assert_eq!(connection, 2.into());
        let discover: Arc<DiscoverGattOperation> = bleng.begin_discover_gatt(connection).unwrap();
        let db: GattDatabase = complete(&handle, 5, discover.command_id(), discover.wait(2000), terminal(5), BleEvent::GattDiscovered { command_id: 5.into(), database: database() }).await;
        assert_eq!(db, database());
        let session = db.gatt_session_id;
        let read: Arc<ReadOperation> = bleng.begin_read_characteristic(connection, session, 30.into()).unwrap();
        let bytes: Vec<u8> = complete(&handle, 6, read.command_id(), read.wait(2000), BleEvent::CharacteristicRead { command_id: 6.into(), connection_id: connection, gatt_session_id: session, characteristic_id: 30.into(), value: vec![1, 2, 3] }, terminal(6)).await;
        assert_eq!(bytes, vec![1, 2, 3]);
        let write: Arc<WriteOperation> = bleng.begin_write_characteristic(connection, session, 30.into(), vec![7, 8], WriteType::WithResponse).unwrap();
        let (): () = complete(&handle, 7, write.command_id(), write.wait(2000), terminal(7), BleEvent::CharacteristicWritten { command_id: 7.into(), connection_id: connection, gatt_session_id: session, characteristic_id: 30.into() }).await;
        let enable: Arc<EnableNotificationsOperation> = bleng.begin_enable_notifications(connection, session, 30.into(), NotificationModeRequest::PlatformDefault).unwrap();
        let actual = BleEvent::NotificationStateChanged { command_id: 8.into(), connection_id: connection, gatt_session_id: session, subscription_id: 4.into(), characteristic_id: 30.into(), state: NotificationState::Enabled { actual_mode: Some(NotificationMode::Indication) } };
        let receipt: NotificationSubscription = complete(&handle, 8, enable.command_id(), enable.wait(2000), actual.clone(), terminal(8)).await;
        assert_eq!(receipt.subscription, 4.into());
        // The actual mode is part of the enable receipt, not a second event lookup.
        assert_eq!(receipt.actual_mode, Some(NotificationMode::Indication));
        loop {
            if bleng.next_event(2000).await.unwrap() == Some(actual.clone()) { break; }
        }
        let subscription = receipt.subscription;
        let disable: Arc<DisableNotificationsOperation> = bleng.begin_disable_notifications(connection, session, subscription).unwrap();
        let (): () = complete(&handle, 9, disable.command_id(), disable.wait(2000), terminal(9), BleEvent::NotificationStateChanged { command_id: 9.into(), connection_id: connection, gatt_session_id: session, subscription_id: subscription, characteristic_id: 30.into(), state: NotificationState::Disabled }).await;
        let rssi: Arc<ReadRssiOperation> = bleng.begin_read_rssi(connection).unwrap();
        let value: i16 = complete(&handle, 10, rssi.command_id(), rssi.wait(2000), BleEvent::RssiRead { command_id: 10.into(), connection_id: connection, rssi: -61 }, terminal(10)).await;
        assert_eq!(value, -61);
        let disconnect: Arc<DisconnectOperation> = bleng.begin_disconnect(connection).unwrap();
        accepted(&handle, 11).await;
        assert!(matches!(disconnect.cancel().await, Err(BlengError::OperationNotCancellable { command_id }) if command_id == 11.into() && command_id == disconnect.command_id()));
        let (): () = complete(&handle, 11, disconnect.command_id(), disconnect.wait(2000), terminal(11), BleEvent::ConnectionStateChanged { command_id: Some(11.into()), connection_id: connection, result: ConnectionStateResult::Ok { state: ConnectionState::Disconnected } }).await;
        assert!(matches!(disconnect.cancel().await, Err(BlengError::OperationAlreadyCompleted { command_id }) if command_id == 11.into() && command_id == disconnect.command_id()));
        assert_eq!(handle.commands(), commands);
        // Terminal-first is deliberately adversarial backend evidence. The
                // backend tracker flags it; BLeng must still correlate without inventing success.
                assert_eq!(handle.contract_violations(), [1, 3, 5, 7, 9, 11].map(|id|
                    blehub_testkit::BackendContractViolation::EventAfterTerminalAttempt { command_id: id.into() }
                ));
        bleng.shutdown().await.unwrap();
        // All results have been consumed; close must not invalidate correlation.
        assert_eq!([
            init.command_id(), start.command_id(), stop.command_id(),
            connect.command_id(), discover.command_id(), read.command_id(),
            write.command_id(), enable.command_id(), disable.command_id(),
            rssi.command_id(), disconnect.command_id(),
        ], std::array::from_fn::<_, 11, _>(|index| CommandId::from(index as u64 + 1)));
        assert_eq!(handle.commands(), commands);
        assert_eq!(handle.dispose_count(), 1);
    });
}

#[test]
fn convenience_and_typed_paths_match_all_eleven_commands_results_and_events() {
    caller().block_on(async {
        let commands = vec![
            initialize_command(),
            RecordedCommand::StartScan(StartScanCommand {
                command_id: 2.into(),
                scan_session_id: 1.into(),
                options: ScanOptions::default(),
            }),
            RecordedCommand::StopScan(StopScanCommand {
                command_id: 3.into(),
                scan_session_id: 1.into(),
            }),
            RecordedCommand::Connect(ConnectCommand {
                auto_connect: false,
                command_id: 4.into(),
                connection_id: 2.into(),
                peripheral_id: 10.into(),
            }),
            RecordedCommand::DiscoverGatt(DiscoverGattCommand {
                command_id: 5.into(),
                connection_id: 2.into(),
                gatt_session_id: 3.into(),
            }),
            RecordedCommand::ReadCharacteristic(ReadCharacteristicCommand {
                command_id: 6.into(),
                connection_id: 2.into(),
                gatt_session_id: 3.into(),
                characteristic_id: 30.into(),
            }),
            RecordedCommand::WriteCharacteristic(WriteCharacteristicCommand {
                command_id: 7.into(),
                connection_id: 2.into(),
                gatt_session_id: 3.into(),
                characteristic_id: 30.into(),
                value: vec![7, 8],
                write_type: WriteType::WithResponse,
            }),
            RecordedCommand::SetNotifications(SetNotificationsCommand {
                command_id: 8.into(),
                connection_id: 2.into(),
                gatt_session_id: 3.into(),
                subscription_id: 4.into(),
                characteristic_id: 30.into(),
                action: NotificationAction::Enable {
                    mode: NotificationModeRequest::PlatformDefault,
                },
            }),
            RecordedCommand::SetNotifications(SetNotificationsCommand {
                command_id: 9.into(),
                connection_id: 2.into(),
                gatt_session_id: 3.into(),
                subscription_id: 4.into(),
                characteristic_id: 30.into(),
                action: NotificationAction::Disable,
            }),
            RecordedCommand::ReadRssi(ReadRssiCommand {
                command_id: 10.into(),
                connection_id: 2.into(),
            }),
            RecordedCommand::Disconnect(DisconnectCommand {
                command_id: 11.into(),
                connection_id: 2.into(),
            }),
        ];
        let domain_events = vec![
            initialized(),
            BleEvent::ScanStarted {
                command_id: 2.into(),
                scan_id: 1.into(),
            },
            BleEvent::ScanStopped {
                scan_id: 1.into(),
                result: ScanStopResult::Ok {
                    reason: ScanStopReason::Requested {
                        command_id: 3.into(),
                    },
                },
            },
            BleEvent::ConnectionStateChanged {
                command_id: Some(4.into()),
                connection_id: 2.into(),
                result: ConnectionStateResult::Ok {
                    state: ConnectionState::Connected,
                },
            },
            BleEvent::GattDiscovered {
                command_id: 5.into(),
                database: database(),
            },
            BleEvent::CharacteristicRead {
                command_id: 6.into(),
                connection_id: 2.into(),
                gatt_session_id: 3.into(),
                characteristic_id: 30.into(),
                value: vec![1, 2, 3],
            },
            BleEvent::CharacteristicWritten {
                command_id: 7.into(),
                connection_id: 2.into(),
                gatt_session_id: 3.into(),
                characteristic_id: 30.into(),
            },
            BleEvent::NotificationStateChanged {
                command_id: 8.into(),
                connection_id: 2.into(),
                gatt_session_id: 3.into(),
                subscription_id: 4.into(),
                characteristic_id: 30.into(),
                state: NotificationState::Enabled {
                    actual_mode: Some(NotificationMode::Indication),
                },
            },
            BleEvent::NotificationStateChanged {
                command_id: 9.into(),
                connection_id: 2.into(),
                gatt_session_id: 3.into(),
                subscription_id: 4.into(),
                characteristic_id: 30.into(),
                state: NotificationState::Disabled,
            },
            BleEvent::RssiRead {
                command_id: 10.into(),
                connection_id: 2.into(),
                rssi: -61,
            },
            BleEvent::ConnectionStateChanged {
                command_id: Some(11.into()),
                connection_id: 2.into(),
                result: ConnectionStateResult::Ok {
                    state: ConnectionState::Disconnected,
                },
            },
        ];
        let events: Vec<_> = domain_events
            .into_iter()
            .enumerate()
            .flat_map(|(index, event)| [event, terminal(index as u64 + 1)])
            .collect();
        for typed in [false, true] {
            let (backend, handle) =
                FakeBackend::scripted(commands.iter().cloned().enumerate().map(
                    |(index, command)| {
                        EventScenario::emit_before_return(
                            command,
                            events[index * 2..index * 2 + 2].to_vec(),
                        )
                    },
                ));
            let bleng = Bleng::new().unwrap();
            bleng.assemble_backend(Arc::new(backend)).await.unwrap();
            let mut operation_ids = Vec::new();
            // These exercise the public APIs, including subscription-result projection.
            macro_rules! result {
                ($begin:ident, $run:ident $(, $argument:expr)*) => {
                    if typed {
                        let operation = bleng.$begin($($argument),*).unwrap();
                        operation_ids.push(operation.command_id());
                        let value = operation.wait(2000).await.unwrap();
                        assert_eq!(operation.command_id(), *operation_ids.last().unwrap());
                        value
                    } else {
                        bleng.$run($($argument,)* 2000).await.unwrap()
                    }
                };
            }
            assert_eq!(result!(begin_initialize, initialize), snapshot());
            assert_eq!(
                result!(begin_start_scan, start_scan, ScanOptions::default()),
                ScanSessionId::from(1)
            );
            result!(begin_stop_scan, stop_scan, 1.into());
            assert_eq!(
                result!(begin_connect, connect, 10.into()),
                ConnectionId::from(2)
            );
            assert_eq!(
                result!(begin_discover_gatt, discover_gatt, 2.into()),
                database()
            );
            assert_eq!(
                result!(
                    begin_read_characteristic,
                    read_characteristic,
                    2.into(),
                    3.into(),
                    30.into()
                ),
                vec![1, 2, 3]
            );
            result!(
                begin_write_characteristic,
                write_characteristic,
                2.into(),
                3.into(),
                30.into(),
                vec![7, 8],
                WriteType::WithResponse
            );
            assert_eq!(
                result!(
                    begin_enable_notifications,
                    enable_notifications,
                    2.into(),
                    3.into(),
                    30.into(),
                    NotificationModeRequest::PlatformDefault
                )
                .subscription,
                SubscriptionId::from(4)
            );
            result!(
                begin_disable_notifications,
                disable_notifications,
                2.into(),
                3.into(),
                4.into()
            );
            assert_eq!(result!(begin_read_rssi, read_rssi, 2.into()), -61);
            result!(begin_disconnect, disconnect, 2.into());
            assert_eq!(handle.commands(), commands);
            for (index, event) in events.iter().enumerate() {
                let actual = bleng.next_event(2000).await.unwrap().unwrap();
                assert_eq!(&actual, event);
                if typed {
                    assert_eq!(actual.command_id(), Some(operation_ids[index / 2]));
                }
            }
            assert!(handle.contract_violations().is_empty());
            bleng.shutdown().await.unwrap();
            assert_eq!(handle.dispose_count(), 1);
        }
    });
}

#[test]
fn typed_connect_cancel_receipt_does_not_consume_result_or_promise_support() {
    caller().block_on(async {
        for policy in [CancellationPolicy::Accept, CancellationPolicy::Reject] {
            let (backend, handle) = FakeBackend::scripted_with_cancellation([
                            EventScenario::emit_before_return(initialize_command(), vec![initialized(), terminal(1)]),
                            EventScenario::accept(RecordedCommand::Connect(ConnectCommand { auto_connect: false, command_id: 2.into(), connection_id: 1.into(), peripheral_id: 10.into() })),
                        ], policy);
            let bleng = Bleng::new().unwrap();
            bleng.assemble_backend(Arc::new(backend)).await.unwrap();
            bleng.initialize(2000).await.unwrap();
            let connect: Arc<ConnectOperation> = bleng.begin_connect(10.into()).unwrap();
            let id = connect.command_id();
            assert_eq!(id, 2.into());
            accepted(&handle, 2).await;
            let waiting = Arc::clone(&connect);
            let wait = tokio::spawn(async move { waiting.wait(2000).await });
            if policy == CancellationPolicy::Accept {
                connect.cancel().await.unwrap();
                assert!(matches!(wait.await.unwrap(), Err(BlengError::Cancelled)));
            } else {
                assert!(matches!(connect.cancel().await, Err(BlengError::OperationNotCancellable { command_id }) if command_id == 2.into() && command_id == connect.command_id()));
                emit(&handle, BleEvent::ConnectionStateChanged { command_id: Some(2.into()), connection_id: 1.into(), result: ConnectionStateResult::Ok { state: ConnectionState::Connected } });
                emit(&handle, terminal(2));
                assert_eq!(wait.await.unwrap().unwrap(), ConnectionId::from(1));
            }
            assert!(matches!(connect.wait(2000).await, Err(BlengError::OperationResultConsumed)));
            assert_eq!(connect.command_id(), id);
            bleng.shutdown().await.unwrap();
            assert_eq!(connect.command_id(), id);
            assert_eq!(handle.commands().len(), 2);
        }
    });
}

#[test]
fn typed_begin_deadline_is_not_the_later_wait_deadline_and_pending_close_is_closed() {
    caller().block_on(async {
        for close in [false, true] {
            let (backend, handle) =
                FakeBackend::scripted([EventScenario::accept(initialize_command())]);
            let bleng = Bleng::new().unwrap();
            bleng.assemble_backend(Arc::new(backend)).await.unwrap();
            let init = bleng.begin_initialize().unwrap();
            let id = init.command_id();
            assert_eq!(id, 1.into());
            accepted(&handle, 1).await;
            tokio::time::sleep(Duration::from_millis(120)).await;
            if close {
                bleng.shutdown().await.unwrap();
                assert!(matches!(init.wait(2000).await, Err(BlengError::Closed)));
                assert!(matches!(init.cancel().await, Err(BlengError::Closed)));
            } else {
                let value = complete(
                    &handle,
                    1,
                    init.command_id(),
                    init.wait(2000),
                    initialized(),
                    terminal(1),
                )
                .await;
                assert_eq!(value, snapshot());
                bleng.shutdown().await.unwrap();
            }
            assert!(matches!(
                init.wait(2000).await,
                Err(BlengError::OperationResultConsumed)
            ));
            assert_eq!(init.command_id(), id);
            assert_eq!(handle.commands(), vec![initialize_command()]);
        }
    });
}

#[test]
fn typed_dispatch_rejection_is_a_wait_result_not_begin_acceptance() {
    caller().block_on(async {
        let error = CommandDispatchError::InvalidState { detail: "consumer rejection".into() };
        let (backend, handle) = FakeBackend::scripted([EventScenario::reject(initialize_command(), error.clone())]);
        let bleng = Bleng::new().unwrap();
        bleng.assemble_backend(Arc::new(backend)).await.unwrap();
        let init = bleng.begin_initialize().unwrap();
        assert!(matches!(init.wait(2000).await, Err(BlengError::DispatchRejected { error: actual }) if actual == error));
        assert_eq!(handle.commands(), vec![initialize_command()]);
        bleng.shutdown().await.unwrap();
    });
}

fn connected() -> BleEvent {
    BleEvent::ConnectionStateChanged {
        command_id: Some(2.into()),
        connection_id: 1.into(),
        result: ConnectionStateResult::Ok {
            state: ConnectionState::Connected,
        },
    }
}

async fn rssi_fixture() -> (Arc<Bleng>, FakeBackendHandle) {
    let (backend, handle) = FakeBackend::scripted([
        EventScenario::emit_before_return(initialize_command(), vec![initialized(), terminal(1)]),
        EventScenario::emit_before_return(
            RecordedCommand::Connect(ConnectCommand {
                auto_connect: false,
                command_id: 2.into(),
                connection_id: 1.into(),
                peripheral_id: 10.into(),
            }),
            vec![connected(), terminal(2)],
        ),
        EventScenario::accept(RecordedCommand::ReadRssi(ReadRssiCommand {
            command_id: 3.into(),
            connection_id: 1.into(),
        })),
    ]);
    let bleng = Bleng::new().unwrap();
    bleng.assemble_backend(Arc::new(backend)).await.unwrap();
    bleng.initialize(2000).await.unwrap();
    bleng.connect(10.into(), 2000).await.unwrap();
    (bleng, handle)
}

#[test]
fn non_read_completed_success_and_error_survive_close_without_cancel_consuming_them() {
    caller().block_on(async {
        for failed in [false, true] {
            let (bleng, handle) = rssi_fixture().await;
            let rssi = bleng.begin_read_rssi(1.into()).unwrap();
            let id = rssi.command_id();
            assert_eq!(id, 3.into());
            accepted(&handle, 3).await;
            let error = BleEventError { category: BleErrorCategory::Internal, message: "RSSI failure".into(), platform_details: None };
            emit(&handle, BleEvent::RssiRead { command_id: 3.into(), connection_id: 1.into(), rssi: -48 });
            emit(&handle, BleEvent::CommandCompleted { command_id: 3.into(), error: failed.then(|| error.clone()) });
            // Public terminal publication also proves dispatch reconciliation ran.
            loop {
                if let Some(BleEvent::CommandCompleted { command_id, error: actual }) = bleng.next_event(2000).await.unwrap()
                                    && command_id == id
                                {
                                    assert_eq!(command_id, 3.into());
                                    assert_eq!(actual, failed.then(|| error.clone()));
                                    break;
                                }
            }
            // Actor-linearized cancel observes completion without taking the result.
            assert!(matches!(rssi.cancel().await, Err(BlengError::OperationAlreadyCompleted { command_id }) if command_id == 3.into() && command_id == rssi.command_id()));
            bleng.shutdown().await.unwrap();
            let result = rssi.wait(2000).await;
            if failed {
                assert!(matches!(result, Err(BlengError::Operation { error: actual }) if actual == error));
            } else {
                assert_eq!(result.unwrap(), -48);
            }
            assert!(matches!(rssi.wait(2000).await, Err(BlengError::OperationResultConsumed)));
            drop(bleng);
            assert_eq!(rssi.command_id(), id);
            assert_eq!(handle.commands().len(), 3);
        }
    });
}

struct DisposeGate {
    entered: std::sync::mpsc::SyncSender<()>,
    release: std::sync::Mutex<std::sync::mpsc::Receiver<()>>,
}

struct InitializeGate {
    entered: std::sync::mpsc::SyncSender<std::thread::ThreadId>,
    release: std::sync::Mutex<std::sync::mpsc::Receiver<()>>,
}

struct CountCancellation {
    inner: FakeBackend,
    count: Arc<std::sync::atomic::AtomicUsize>,
    dispose_gate: Option<DisposeGate>,
    initialize_gate: Option<InitializeGate>,
}

impl BleBackend for CountCancellation {
    delegate::delegate! {
        to self.inner {
            fn capabilities(&self) -> Result<BackendCapabilities, BleHubError>;
            fn bind_event_ingress(&self, ingress: Arc<BackendEventIngressHandle>) -> Result<(), BleHubError>;
            fn set_adapter_enabled(&self, command: blehub::SetAdapterEnabledCommand) -> Result<(), CommandDispatchError>;
            fn query_devices(&self, command: blehub::QueryDevicesCommand) -> Result<(), CommandDispatchError>;
            fn associate_companion_device(&self, command: blehub::AssociateCompanionDeviceCommand) -> Result<(), CommandDispatchError>;
            fn refresh_gatt_cache(&self, command: blehub::RefreshGattCacheCommand) -> Result<(), CommandDispatchError>;
            fn start_scan(&self, command: StartScanCommand) -> Result<(), CommandDispatchError>;
            fn stop_scan(&self, command: StopScanCommand) -> Result<(), CommandDispatchError>;
            fn connect(&self, command: ConnectCommand) -> Result<(), CommandDispatchError>;
            fn disconnect(&self, command: DisconnectCommand) -> Result<(), CommandDispatchError>;
            fn discover_gatt(&self, command: DiscoverGattCommand) -> Result<(), CommandDispatchError>;
            fn read_characteristic(&self, command: ReadCharacteristicCommand) -> Result<(), CommandDispatchError>;
            fn write_characteristic(&self, command: WriteCharacteristicCommand) -> Result<(), CommandDispatchError>;
            fn set_notifications(&self, command: SetNotificationsCommand) -> Result<(), CommandDispatchError>;
            fn read_rssi(&self, command: ReadRssiCommand) -> Result<(), CommandDispatchError>;
            fn mtu(&self, command: blehub::MtuCommand) -> Result<(), CommandDispatchError>;
            fn set_connection_priority(&self, command: blehub::SetConnectionPriorityCommand) -> Result<(), CommandDispatchError>;
            fn create_bond(&self, command: blehub::CreateBondCommand) -> Result<(), CommandDispatchError>;
            fn remove_bond(&self, command: blehub::RemoveBondCommand) -> Result<(), CommandDispatchError>;
            fn read_descriptor(&self, command: blehub::ReadDescriptorCommand) -> Result<(), CommandDispatchError>;
            fn write_descriptor(&self, command: blehub::WriteDescriptorCommand) -> Result<(), CommandDispatchError>;

        }
    }

    fn initialize(&self, command: InitializeCommand) -> Result<(), CommandDispatchError> {
        if let Some(gate) = &self.initialize_gate {
            gate.entered.send(std::thread::current().id()).unwrap();
            gate.release
                .lock()
                .unwrap()
                .recv_timeout(Duration::from_secs(5))
                .expect("release native initialize gate");
        }
        self.inner.initialize(command)
    }

    fn dispose(&self) -> Result<(), BleHubError> {
        if let Some(gate) = &self.dispose_gate {
            gate.entered.send(()).unwrap();
            gate.release
                .lock()
                .unwrap()
                .recv_timeout(Duration::from_secs(5))
                .expect("test must release native cleanup");
        }
        self.inner.dispose()
    }

    fn cancel_operation(
        &self,
        request: CancelOperationRequest,
    ) -> Result<(), CommandDispatchError> {
        self.count.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        self.inner.cancel_operation(request)
    }
}

#[test]
fn dropped_convenience_wait_never_requests_native_cancellation() {
    caller().block_on(async {
        use std::sync::atomic::{AtomicUsize, Ordering};
        let (backend, handle) = FakeBackend::scripted_with_cancellation(
            [
                EventScenario::emit_before_return(
                    initialize_command(),
                    vec![initialized(), terminal(1)],
                ),
                EventScenario::accept(RecordedCommand::Connect(ConnectCommand {
                    auto_connect: false,
                    command_id: 2.into(),
                    connection_id: 1.into(),
                    peripheral_id: 10.into(),
                })),
            ],
            CancellationPolicy::Accept,
        );
        let count = Arc::new(AtomicUsize::new(0));
        let bleng = Bleng::new().unwrap();
        bleng
            .assemble_backend(Arc::new(CountCancellation {
                inner: backend,
                count: Arc::clone(&count),
                dispose_gate: None,
                initialize_gate: None,
            }))
            .await
            .unwrap();
        bleng.initialize(2000).await.unwrap();
        let waiting = Arc::clone(&bleng);
        let wait = tokio::spawn(async move { waiting.connect(10.into(), 2000).await });
        accepted(&handle, 2).await;
        wait.abort();
        assert!(wait.await.unwrap_err().is_cancelled());
        // The reservation survives loss of caller delivery. A new operation is
        // not a substitute for explicit cancellation of the dispatched command.
        assert!(matches!(
            bleng.begin_connect(10.into()).unwrap().wait(2000).await,
            Err(BlengError::Busy { .. })
        ));
        assert_eq!(count.load(Ordering::SeqCst), 0);
        bleng.cancel_operation(2.into()).await.unwrap();
        assert_eq!(count.load(Ordering::SeqCst), 1);
        bleng.shutdown().await.unwrap();
    });
}

#[test]
fn non_read_drop_retires_delivery_only_and_explicit_cancel_still_reaches_backend() {
    caller().block_on(async {
        use std::sync::atomic::{AtomicUsize, Ordering};
        let (backend, handle) = FakeBackend::scripted_with_cancellation(
            [
                EventScenario::emit_before_return(
                    initialize_command(),
                    vec![initialized(), terminal(1)],
                ),
                EventScenario::accept(RecordedCommand::Connect(ConnectCommand {
                    auto_connect: false,
                    command_id: 2.into(),
                    connection_id: 1.into(),
                    peripheral_id: 10.into(),
                })),
            ],
            CancellationPolicy::Accept,
        );
        let count = Arc::new(AtomicUsize::new(0));
        let bleng = Bleng::new().unwrap();
        bleng
            .assemble_backend(Arc::new(CountCancellation {
                inner: backend,
                count: Arc::clone(&count),
                dispose_gate: None,
                initialize_gate: None,
            }))
            .await
            .unwrap();
        bleng.initialize(2000).await.unwrap();
        let connect = bleng.begin_connect(10.into()).unwrap();
        accepted(&handle, 2).await;
        let waiting = Arc::clone(&connect);
        let wait = tokio::spawn(async move { waiting.wait(2000).await });
        // Invalid timeouts must not claim delivery, including from another clone.
        assert!(matches!(
            connect.wait(0).await,
            Err(BlengError::InvalidTimeout)
        ));
        wait.abort();
        assert!(wait.await.unwrap_err().is_cancelled());
        drop(connect);
        assert_eq!(count.load(Ordering::SeqCst), 0);
        bleng.cancel_operation(2.into()).await.unwrap();
        assert_eq!(count.load(Ordering::SeqCst), 1);
        emit(&handle, connected());
        emit(&handle, terminal(2));
        bleng.shutdown().await.unwrap();
    });
}

#[test]
fn typed_boundary_facade_drop_returns_while_native_cleanup_is_blocked() {
    caller().block_on(async {
        let (backend, handle) =
            FakeBackend::scripted([EventScenario::accept(initialize_command())]);
        let (entered_tx, entered_rx) = std::sync::mpsc::sync_channel(1);
        let (release_tx, release_rx) = std::sync::mpsc::sync_channel(1);
        let release = scopeguard::guard(release_tx, |tx| {
            let _ = tx.send(());
        });
        let bleng = Bleng::new().unwrap();
        bleng
            .assemble_backend(Arc::new(CountCancellation {
                inner: backend,
                count: Arc::new(std::sync::atomic::AtomicUsize::new(0)),
                initialize_gate: None,
                dispose_gate: Some(DisposeGate {
                    entered: entered_tx,
                    release: std::sync::Mutex::new(release_rx),
                }),
            }))
            .await
            .unwrap();
        let init = bleng.begin_initialize().unwrap();
        accepted(&handle, 1).await;
        let (dropped_tx, dropped_rx) = std::sync::mpsc::sync_channel(1);
        let dropping = std::thread::spawn(move || {
            drop(bleng);
            dropped_tx.send(()).unwrap();
        });
        dropped_rx
            .recv_timeout(Duration::from_secs(2))
            .expect("facade Drop must not wait for native cleanup");
        entered_rx
            .recv_timeout(Duration::from_secs(2))
            .expect("existing cleanup worker must start even with a retained handle");
        assert_eq!(
            handle.dispose_count(),
            0,
            "the native cleanup gate is still closed"
        );
        assert!(matches!(init.wait(2000).await, Err(BlengError::Closed)));
        assert!(matches!(init.cancel().await, Err(BlengError::Closed)));
        drop(release);
        dropping.join().unwrap();
        tokio::time::timeout(Duration::from_secs(2), async {
            while handle.dispose_count() != 1 {
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap();
    });
}

// These reasons must be nameable downstream as well as inside the private API module.
fn _name_error_payloads(error: BlengError) -> Option<(BlengResource, BlengContractFailure)> {
    match error {
        BlengError::ResourceExhausted { resource } => {
            Some((resource, BlengContractFailure::InvalidCommandId))
        }
        _ => None,
    }
}

#[test]
fn synchronous_begin_without_caller_runtime_is_eager_and_never_calls_native_on_caller() {
    let (backend, handle) = FakeBackend::scripted([EventScenario::emit_before_return(
        initialize_command(),
        vec![initialized(), terminal(1)],
    )]);
    let count = Arc::new(std::sync::atomic::AtomicUsize::new(0));
    let (entered, entered_rx) = std::sync::mpsc::sync_channel(1);
    let (release_tx, release) = std::sync::mpsc::channel();
    let bleng = Bleng::new().unwrap();
    caller()
        .block_on(bleng.assemble_backend(Arc::new(CountCancellation {
            inner: backend,
            count: Arc::clone(&count),
            dispose_gate: None,
            initialize_gate: Some(InitializeGate {
                entered,
                release: std::sync::Mutex::new(release),
            }),
        })))
        .unwrap();
    // No caller executor is running, and native initialize is held until after begin returns.
    let operation: Arc<InitializeOperation> = bleng.begin_initialize().unwrap();
    let native_thread = entered_rx.recv_timeout(Duration::from_secs(2)).unwrap();
    assert_ne!(native_thread, std::thread::current().id());
    // A synchronous correlation read needs no caller executor or native progress.
    assert_eq!(operation.command_id(), 1.into());
    assert_eq!(operation.command_id(), 1.into());
    assert!(handle.commands().is_empty());
    assert_eq!(count.load(std::sync::atomic::Ordering::SeqCst), 0);
    drop(operation);
    release_tx.send(()).unwrap();
    caller().block_on(async {
        assert_eq!(bleng.next_event(2000).await.unwrap(), Some(initialized()));
        assert_eq!(bleng.next_event(2000).await.unwrap(), Some(terminal(1)));
        assert_eq!(count.load(std::sync::atomic::Ordering::SeqCst), 0);
        assert_eq!(handle.commands(), vec![initialize_command()]);
        bleng.shutdown().await.unwrap();
    });
}

#[test]
fn mtu_query_and_request_are_distinct_bounded_platform_snapshots() {
    caller().block_on(async {
        let commands = vec![
            initialize_command(),
            RecordedCommand::Connect(ConnectCommand {
                auto_connect: false,
                command_id: 2.into(),
                connection_id: 1.into(),
                peripheral_id: 10.into(),
            }),
            RecordedCommand::Mtu(MtuCommand {
                command_id: 3.into(),
                connection_id: 1.into(),
                request: None,
            }),
            RecordedCommand::Mtu(MtuCommand {
                command_id: 4.into(),
                connection_id: 1.into(),
                request: Some(247),
            }),
        ];
        let (backend, handle) =
            FakeBackend::scripted(commands.clone().into_iter().map(EventScenario::accept));
        let bleng = Bleng::new().unwrap();
        bleng.assemble_backend(Arc::new(backend)).await.unwrap();
        let init: Arc<InitializeOperation> = bleng.begin_initialize().unwrap();
        complete(
            &handle,
            1,
            init.command_id(),
            init.wait(2000),
            initialized(),
            terminal(1),
        )
        .await;

        let connect: Arc<ConnectOperation> = bleng.begin_connect(10.into()).unwrap();
        let connection: ConnectionId = complete(
            &handle,
            2,
            connect.command_id(),
            connect.wait(2000),
            BleEvent::ConnectionStateChanged {
                command_id: Some(2.into()),
                connection_id: 1.into(),
                result: ConnectionStateResult::Ok {
                    state: ConnectionState::Connected,
                },
            },
            terminal(2),
        )
        .await;

        // A query never negotiates: the platform may not even expose the link MTU.
        let query: Arc<MtuOperation> = bleng.begin_query_mtu(connection).unwrap();
        assert_eq!(query.command_id(), 3.into());
        let queried = MtuInfo {
            mtu: None,
            maximum_write_with_response: Some(182),
            maximum_write_without_response: Some(185),
        };
        let info: MtuInfo = complete(
            &handle,
            3,
            query.command_id(),
            query.wait(2000),
            BleEvent::MtuRead {
                command_id: 3.into(),
                connection_id: connection,
                info: queried,
            },
            terminal(3),
        )
        .await;
        assert_eq!(info, queried);
        assert_eq!(info.mtu, None);

        // A successful request is still not an echo: the receipt is the platform's own report.
        let request: Arc<MtuOperation> = bleng.begin_request_mtu(connection, 247).unwrap();
        let reported = MtuInfo {
            mtu: Some(185),
            maximum_write_with_response: Some(182),
            maximum_write_without_response: Some(185),
        };
        let info: MtuInfo = complete(
            &handle,
            4,
            request.command_id(),
            request.wait(2000),
            BleEvent::MtuRead {
                command_id: 4.into(),
                connection_id: connection,
                info: reported,
            },
            terminal(4),
        )
        .await;
        assert_eq!(info.mtu, Some(185));
        assert_eq!(handle.commands(), commands);
        bleng.shutdown().await.unwrap();
    });
}

#[test]
fn an_unsupported_mtu_request_is_a_typed_dispatch_rejection_not_a_timeout() {
    caller().block_on(async {
        let (backend, handle) = FakeBackend::scripted([
            EventScenario::emit_before_return(
                initialize_command(),
                vec![initialized(), terminal(1)],
            ),
            EventScenario::emit_before_return(
                RecordedCommand::Connect(ConnectCommand {
                    auto_connect: false,
                    command_id: 2.into(),
                    connection_id: 1.into(),
                    peripheral_id: 10.into(),
                }),
                vec![
                    BleEvent::ConnectionStateChanged {
                        command_id: Some(2.into()),
                        connection_id: 1.into(),
                        result: ConnectionStateResult::Ok {
                            state: ConnectionState::Connected,
                        },
                    },
                    terminal(2),
                ],
            ),
            EventScenario::reject(
                RecordedCommand::Mtu(MtuCommand {
                    command_id: 3.into(),
                    connection_id: 1.into(),
                    request: Some(247),
                }),
                CommandDispatchError::NotSupported {
                    detail: "this platform reports MTU without negotiating".to_owned(),
                },
            ),
        ]);
        let bleng = Bleng::new().unwrap();
        bleng.assemble_backend(Arc::new(backend)).await.unwrap();
        bleng.initialize(2000).await.unwrap();
        let connection = bleng.connect(10.into(), 2000).await.unwrap();

        let request: Arc<MtuOperation> = bleng.begin_request_mtu(connection, 247).unwrap();
        assert!(matches!(
            request.wait(2000).await,
            Err(BlengError::DispatchRejected {
                error: CommandDispatchError::NotSupported { .. }
            })
        ));
        // The rejection is terminal for that submission only; the runtime stays usable.
        assert_eq!(handle.commands().len(), 3);
        bleng.shutdown().await.unwrap();
    });
}

#[test]
fn auto_connect_is_a_connect_option_not_a_separate_command() {
    caller().block_on(async {
        let commands = vec![
            initialize_command(),
            RecordedCommand::Connect(ConnectCommand {
                auto_connect: true,
                command_id: 2.into(),
                connection_id: 1.into(),
                peripheral_id: 10.into(),
            }),
        ];
        let (backend, handle) =
            FakeBackend::scripted(commands.clone().into_iter().map(EventScenario::accept));
        let bleng = Bleng::new().unwrap();
        bleng.assemble_backend(Arc::new(backend)).await.unwrap();
        let init: Arc<InitializeOperation> = bleng.begin_initialize().unwrap();
        complete(
            &handle,
            1,
            init.command_id(),
            init.wait(2000),
            initialized(),
            terminal(1),
        )
        .await;
        let connect: Arc<ConnectOperation> =
            bleng.begin_connect_with_options(10.into(), true).unwrap();
        let _: ConnectionId = complete(
            &handle,
            2,
            connect.command_id(),
            connect.wait(2000),
            BleEvent::ConnectionStateChanged {
                command_id: Some(2.into()),
                connection_id: 1.into(),
                result: ConnectionStateResult::Ok {
                    state: ConnectionState::Connected,
                },
            },
            terminal(2),
        )
        .await;
        assert_eq!(handle.commands(), commands);
        bleng.shutdown().await.unwrap();
    });
}

#[test]
fn adapter_control_is_a_terminal_receipt_not_a_radio_state_claim() {
    caller().block_on(async {
        let commands = vec![
            initialize_command(),
            RecordedCommand::SetAdapterEnabled(SetAdapterEnabledCommand {
                command_id: 2.into(),
                enabled: true,
            }),
        ];
        let (backend, handle) =
            FakeBackend::scripted(commands.clone().into_iter().map(EventScenario::accept));
        let bleng = Bleng::new().unwrap();
        bleng.assemble_backend(Arc::new(backend)).await.unwrap();
        let init: Arc<InitializeOperation> = bleng.begin_initialize().unwrap();
        complete(
            &handle,
            1,
            init.command_id(),
            init.wait(2000),
            initialized(),
            terminal(1),
        )
        .await;

        let control = bleng.begin_set_adapter_enabled(true).unwrap();
        assert_eq!(control.command_id(), 2.into());
        accepted(&handle, 2).await;
        emit(&handle, terminal(2));
        control.wait(2000).await.unwrap();
        assert_eq!(handle.commands(), commands);
        bleng.shutdown().await.unwrap();
    });
}

#[test]
fn every_device_query_kind_reaches_the_backend_unchanged() {
    caller().block_on(async {
        for kind in [
            DeviceQueryKind::Connected,
            DeviceQueryKind::Bonded,
            DeviceQueryKind::Companion,
        ] {
            let commands = vec![
                initialize_command(),
                RecordedCommand::QueryDevices(QueryDevicesCommand {
                    command_id: 2.into(),
                    kind,
                    service_uuids: vec![],
                }),
            ];
            let (backend, handle) =
                FakeBackend::scripted(commands.clone().into_iter().map(EventScenario::accept));
            let bleng = Bleng::new().unwrap();
            bleng.assemble_backend(Arc::new(backend)).await.unwrap();
            let init: Arc<InitializeOperation> = bleng.begin_initialize().unwrap();
            complete(
                &handle,
                1,
                init.command_id(),
                init.wait(2000),
                initialized(),
                terminal(1),
            )
            .await;

            let query = bleng.begin_query_devices(kind, vec![]).unwrap();
            accepted(&handle, 2).await;
            emit(
                &handle,
                BleEvent::DevicesListed {
                    command_id: 2.into(),
                    devices: vec![],
                },
            );
            emit(&handle, terminal(2));
            let devices = query.wait(2000).await.unwrap();
            assert!(devices.is_empty());
            // The engine forwards the requested kind verbatim: platform meaning, including a
            // companion association list, is owned by the backend.
            assert_eq!(handle.commands(), commands);
            bleng.shutdown().await.unwrap();
        }
    });
}

#[test]
fn device_query_returns_the_platform_inventory_payload() {
    caller().block_on(async {
        let device = DeviceRecord {
            key: "7".into(),
            local_name: Some("Kit".into()),
            connected: true,
            bonded: false,
        };
        let commands = vec![
            initialize_command(),
            RecordedCommand::QueryDevices(QueryDevicesCommand {
                command_id: 2.into(),
                kind: DeviceQueryKind::Connected,
                service_uuids: vec![],
            }),
        ];
        let (backend, handle) =
            FakeBackend::scripted(commands.clone().into_iter().map(EventScenario::accept));
        let bleng = Bleng::new().unwrap();
        bleng.assemble_backend(Arc::new(backend)).await.unwrap();
        let init: Arc<InitializeOperation> = bleng.begin_initialize().unwrap();
        complete(
            &handle,
            1,
            init.command_id(),
            init.wait(2000),
            initialized(),
            terminal(1),
        )
        .await;

        let query = bleng
            .begin_query_devices(DeviceQueryKind::Connected, vec![])
            .unwrap();
        accepted(&handle, 2).await;
        emit(
            &handle,
            BleEvent::DevicesListed {
                command_id: 2.into(),
                devices: vec![device.clone()],
            },
        );
        emit(&handle, terminal(2));
        let devices = query.wait(2000).await.unwrap();
        assert_eq!(devices, vec![device]);
        assert_eq!(handle.commands(), commands);
        bleng.shutdown().await.unwrap();
    });
}

#[test]
fn gatt_cache_refresh_is_a_terminal_receipt_on_a_live_connection() {
    caller().block_on(async {
        let commands = vec![
            initialize_command(),
            RecordedCommand::Connect(ConnectCommand {
                auto_connect: false,
                command_id: 2.into(),
                connection_id: 1.into(),
                peripheral_id: 10.into(),
            }),
            RecordedCommand::RefreshGattCache(RefreshGattCacheCommand {
                command_id: 3.into(),
                connection_id: 1.into(),
            }),
        ];
        let (backend, handle) =
            FakeBackend::scripted(commands.clone().into_iter().map(EventScenario::accept));
        let bleng = Bleng::new().unwrap();
        bleng.assemble_backend(Arc::new(backend)).await.unwrap();
        let init: Arc<InitializeOperation> = bleng.begin_initialize().unwrap();
        complete(
            &handle,
            1,
            init.command_id(),
            init.wait(2000),
            initialized(),
            terminal(1),
        )
        .await;
        let connect: Arc<ConnectOperation> = bleng.begin_connect(10.into()).unwrap();
        let connection: ConnectionId = complete(
            &handle,
            2,
            connect.command_id(),
            connect.wait(2000),
            BleEvent::ConnectionStateChanged {
                command_id: Some(2.into()),
                connection_id: 1.into(),
                result: ConnectionStateResult::Ok {
                    state: ConnectionState::Connected,
                },
            },
            terminal(2),
        )
        .await;

        let refresh = bleng.begin_refresh_gatt_cache(connection).unwrap();
        assert_eq!(refresh.command_id(), 3.into());
        accepted(&handle, 3).await;
        emit(&handle, terminal(3));
        refresh.wait(2000).await.unwrap();
        assert_eq!(handle.commands(), commands);
        bleng.shutdown().await.unwrap();
    });
}

#[test]
fn connection_priority_is_a_terminal_receipt_not_a_measured_link_mode() {
    caller().block_on(async {
        let commands = vec![
            initialize_command(),
            RecordedCommand::Connect(ConnectCommand {
                auto_connect: false,
                command_id: 2.into(),
                connection_id: 1.into(),
                peripheral_id: 10.into(),
            }),
            RecordedCommand::SetConnectionPriority(SetConnectionPriorityCommand {
                command_id: 3.into(),
                connection_id: 1.into(),
                priority: ConnectionPriority::High,
            }),
        ];
        let (backend, handle) =
            FakeBackend::scripted(commands.clone().into_iter().map(EventScenario::accept));
        let bleng = Bleng::new().unwrap();
        bleng.assemble_backend(Arc::new(backend)).await.unwrap();
        let init: Arc<InitializeOperation> = bleng.begin_initialize().unwrap();
        complete(
            &handle,
            1,
            init.command_id(),
            init.wait(2000),
            initialized(),
            terminal(1),
        )
        .await;
        let connect: Arc<ConnectOperation> = bleng.begin_connect(10.into()).unwrap();
        let connection: ConnectionId = complete(
            &handle,
            2,
            connect.command_id(),
            connect.wait(2000),
            BleEvent::ConnectionStateChanged {
                command_id: Some(2.into()),
                connection_id: 1.into(),
                result: ConnectionStateResult::Ok {
                    state: ConnectionState::Connected,
                },
            },
            terminal(2),
        )
        .await;

        let priority: Arc<SetConnectionPriorityOperation> = bleng
            .begin_set_connection_priority(connection, ConnectionPriority::High)
            .unwrap();
        assert_eq!(priority.command_id(), 3.into());
        accepted(&handle, 3).await;
        let mut wait = Box::pin(priority.wait(2000));
        assert!(
            tokio::time::timeout(Duration::from_millis(5), &mut wait)
                .await
                .is_err(),
            "the receipt must wait for the platform terminal"
        );
        // The receipt carries no value: a terminal success is the whole result.
        emit(&handle, terminal(3));
        tokio::time::timeout(Duration::from_secs(2), wait)
            .await
            .unwrap()
            .unwrap();
        assert_eq!(handle.commands(), commands);
        bleng.shutdown().await.unwrap();
    });
}

#[test]
fn bonding_waits_for_the_platform_terminal_state() {
    caller().block_on(async {
        let commands = vec![
            initialize_command(),
            RecordedCommand::Connect(ConnectCommand {
                auto_connect: false,
                command_id: 2.into(),
                connection_id: 1.into(),
                peripheral_id: 10.into(),
            }),
            RecordedCommand::CreateBond(CreateBondCommand {
                command_id: 3.into(),
                connection_id: 1.into(),
                pin: None,
            }),
        ];
        let (backend, handle) =
            FakeBackend::scripted(commands.clone().into_iter().map(EventScenario::accept));
        let bleng = Bleng::new().unwrap();
        bleng.assemble_backend(Arc::new(backend)).await.unwrap();
        let init: Arc<InitializeOperation> = bleng.begin_initialize().unwrap();
        complete(
            &handle,
            1,
            init.command_id(),
            init.wait(2000),
            initialized(),
            terminal(1),
        )
        .await;
        let connect: Arc<ConnectOperation> = bleng.begin_connect(10.into()).unwrap();
        let connection: ConnectionId = complete(
            &handle,
            2,
            connect.command_id(),
            connect.wait(2000),
            BleEvent::ConnectionStateChanged {
                command_id: Some(2.into()),
                connection_id: 1.into(),
                result: ConnectionStateResult::Ok {
                    state: ConnectionState::Connected,
                },
            },
            terminal(2),
        )
        .await;

        let bond: Arc<BondOperation> = bleng.begin_create_bond(connection, None).unwrap();
        assert_eq!(bond.command_id(), 3.into());
        accepted(&handle, 3).await;
        let mut wait = Box::pin(bond.wait(2000));
        assert!(
            tokio::time::timeout(Duration::from_millis(5), &mut wait)
                .await
                .is_err(),
            "the receipt must wait for the platform terminal"
        );
        // `bonding` is progress, not a terminal: an interim report must not settle the waiter.
        emit(
            &handle,
            BleEvent::BondStateChanged {
                command_id: Some(3.into()),
                connection_id: 1.into(),
                state: BondState::Bonding,
            },
        );
        assert!(
            tokio::time::timeout(Duration::from_millis(5), &mut wait)
                .await
                .is_err(),
            "bonding is progress, not success evidence"
        );
        emit(
            &handle,
            BleEvent::BondStateChanged {
                command_id: Some(3.into()),
                connection_id: 1.into(),
                state: BondState::Bonded,
            },
        );
        emit(&handle, terminal(3));
        let state = tokio::time::timeout(Duration::from_secs(2), wait)
            .await
            .unwrap()
            .unwrap();
        assert_eq!(state, BondState::Bonded);
        assert_eq!(handle.commands(), commands);
        bleng.shutdown().await.unwrap();
    });
}

#[test]
fn an_unsupported_connection_priority_is_a_typed_dispatch_rejection() {
    caller().block_on(async {
        let (backend, handle) = FakeBackend::scripted([
            EventScenario::emit_before_return(
                initialize_command(),
                vec![initialized(), terminal(1)],
            ),
            EventScenario::emit_before_return(
                RecordedCommand::Connect(ConnectCommand {
                    auto_connect: false,
                    command_id: 2.into(),
                    connection_id: 1.into(),
                    peripheral_id: 10.into(),
                }),
                vec![
                    BleEvent::ConnectionStateChanged {
                        command_id: Some(2.into()),
                        connection_id: 1.into(),
                        result: ConnectionStateResult::Ok {
                            state: ConnectionState::Connected,
                        },
                    },
                    terminal(2),
                ],
            ),
            EventScenario::reject(
                RecordedCommand::SetConnectionPriority(SetConnectionPriorityCommand {
                    command_id: 3.into(),
                    connection_id: 1.into(),
                    priority: ConnectionPriority::LowPower,
                }),
                CommandDispatchError::NotSupported {
                    detail: "CoreBluetooth does not expose connection priority".to_owned(),
                },
            ),
        ]);
        let bleng = Bleng::new().unwrap();
        bleng.assemble_backend(Arc::new(backend)).await.unwrap();
        bleng.initialize(2000).await.unwrap();
        let connection = bleng.connect(10.into(), 2000).await.unwrap();

        let priority: Arc<SetConnectionPriorityOperation> = bleng
            .begin_set_connection_priority(connection, ConnectionPriority::LowPower)
            .unwrap();
        assert!(matches!(
            priority.wait(2000).await,
            Err(BlengError::DispatchRejected {
                error: CommandDispatchError::NotSupported { .. }
            })
        ));
        assert_eq!(handle.commands().len(), 3);
        bleng.shutdown().await.unwrap();
    });
}
