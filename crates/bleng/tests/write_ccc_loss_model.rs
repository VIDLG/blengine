//! Canonical-evidence model, not a platform loss or same-Hub RF verdict.
use std::{
    collections::VecDeque,
    future::Future,
    sync::{Arc, Mutex},
    time::Duration,
};

use blehub::*;
use blehub_testkit::{EventScenario, FakeBackend, FakeBackendHandle, RecordedCommand};
use bleng::{
    Bleng, BlengError, BlengNotReadyReason, DisableNotificationsOperation,
    EnableNotificationsOperation, WriteOperation,
};
use tokio::sync::mpsc;

const DEADLINE: u64 = 5000;

#[derive(Clone, Copy, Debug)]
enum Case {
    ResponseWritePassiveLoss,
    NoResponseLocalSuccessAdapterOff,
    EnablePassiveLoss,
    DisablePermissionLoss,
}

impl Case {
    fn head_id(self) -> u64 {
        if matches!(self, Self::DisablePermissionLoss) {
            5
        } else {
            4
        }
    }

    fn head(self) -> RecordedCommand {
        match self {
            Self::ResponseWritePassiveLoss => write(self.head_id(), 1, 2, WriteType::WithResponse),
            Self::NoResponseLocalSuccessAdapterOff => {
                write(self.head_id(), 1, 2, WriteType::WithoutResponse)
            }
            Self::EnablePassiveLoss => notify(
                self.head_id(),
                1,
                2,
                3,
                NotificationAction::Enable {
                    mode: NotificationModeRequest::PlatformDefault,
                },
            ),
            Self::DisablePermissionLoss => {
                notify(self.head_id(), 1, 2, 3, NotificationAction::Disable)
            }
        }
    }

    fn sibling(self) -> RecordedCommand {
        match self {
            Self::ResponseWritePassiveLoss | Self::NoResponseLocalSuccessAdapterOff => notify(
                self.head_id() + 1,
                1,
                2,
                3,
                NotificationAction::Enable {
                    mode: NotificationModeRequest::PlatformDefault,
                },
            ),
            Self::EnablePassiveLoss | Self::DisablePermissionLoss => {
                write(self.head_id() + 1, 1, 2, WriteType::WithResponse)
            }
        }
    }

    fn error(self) -> Option<BleEventError> {
        let category = match self {
            Self::NoResponseLocalSuccessAdapterOff => BleErrorCategory::AdapterUnavailable,
            Self::DisablePermissionLoss => BleErrorCategory::Permission,
            _ => return None,
        };
        Some(BleEventError {
            category,
            message: "modeled canonical native loss".into(),
            platform_details: None,
        })
    }
}

fn snapshot() -> AdapterSnapshot {
    AdapterSnapshot {
        state: AdapterState::PoweredOn,
        scan_authorization: AuthorizationState::Authorized,
        connection_authorization: AuthorizationState::Authorized,
        name: None,
    }
}

fn terminal(command: u64, error: Option<BleEventError>) -> BleEvent {
    BleEvent::CommandCompleted {
        command_id: command.into(),
        error,
    }
}

fn connected(command: u64, connection: u64) -> BleEvent {
    BleEvent::ConnectionStateChanged {
        command_id: Some(command.into()),
        connection_id: connection.into(),
        result: ConnectionStateResult::Ok {
            state: ConnectionState::Connected,
        },
    }
}

fn database(connection: u64, session: u64) -> GattDatabase {
    GattDatabase {
        connection_id: connection.into(),
        gatt_session_id: session.into(),
        services: vec![GattService {
            id: 20.into(),
            uuid: BluetoothUuid::canonicalize("180d").unwrap(),
            primary: Some(true),
            characteristics: [30, 31]
                .into_iter()
                .map(|id| GattCharacteristic {
                    id: id.into(),
                    uuid: BluetoothUuid::canonicalize(if id == 30 { "2a37" } else { "2a38" })
                        .unwrap(),
                    properties: CharacteristicProperties {
                        readable: true,
                        writable_with_response: true,
                        writable_without_response: true,
                        notifiable: true,
                        indicatable: false,
                    },
                    descriptors: vec![],
                })
                .collect(),
        }],
    }
}

fn connect(command: u64, connection: u64) -> RecordedCommand {
    RecordedCommand::Connect(ConnectCommand {
        command_id: command.into(),
        connection_id: connection.into(),
        peripheral_id: 10.into(),
    })
}

fn discovery(command: u64, connection: u64, session: u64) -> RecordedCommand {
    RecordedCommand::DiscoverGatt(DiscoverGattCommand {
        command_id: command.into(),
        connection_id: connection.into(),
        gatt_session_id: session.into(),
    })
}

fn write(command: u64, connection: u64, session: u64, write_type: WriteType) -> RecordedCommand {
    RecordedCommand::WriteCharacteristic(WriteCharacteristicCommand {
        command_id: command.into(),
        connection_id: connection.into(),
        gatt_session_id: session.into(),
        characteristic_id: 30.into(),
        value: vec![command as u8, 0xff],
        write_type,
    })
}

fn notify(
    command: u64,
    connection: u64,
    session: u64,
    subscription: u64,
    action: NotificationAction,
) -> RecordedCommand {
    RecordedCommand::SetNotifications(SetNotificationsCommand {
        command_id: command.into(),
        connection_id: connection.into(),
        gatt_session_id: session.into(),
        subscription_id: subscription.into(),
        characteristic_id: 31.into(),
        action,
    })
}

fn domain(command: &RecordedCommand) -> BleEvent {
    match command {
        RecordedCommand::WriteCharacteristic(c) => BleEvent::CharacteristicWritten {
            command_id: c.command_id,
            connection_id: c.connection_id,
            gatt_session_id: c.gatt_session_id,
            characteristic_id: c.characteristic_id,
        },
        RecordedCommand::SetNotifications(c) => BleEvent::NotificationStateChanged {
            command_id: c.command_id,
            connection_id: c.connection_id,
            gatt_session_id: c.gatt_session_id,
            subscription_id: c.subscription_id,
            characteristic_id: c.characteristic_id,
            state: match c.action {
                NotificationAction::Enable { .. } => NotificationState::Enabled {
                    actual_mode: Some(NotificationMode::Notification),
                },
                NotificationAction::Disable => NotificationState::Disabled,
            },
        },
        _ => panic!("not a write/CCC command"),
    }
}

// This lane is deliberately test-owned. FakeBackend still checks the exact
// public dispatch transcript; BLeng does not own or prove native FIFO scheduling.
struct LaneBackend {
    inner: FakeBackend,
    lane: Arc<Mutex<VecDeque<RecordedCommand>>>,
    accepted: mpsc::UnboundedSender<RecordedCommand>,
}

impl LaneBackend {
    fn accepted(&self, command: RecordedCommand) {
        self.lane.lock().unwrap().push_back(command.clone());
        self.accepted.send(command).unwrap();
    }
}

impl BleBackend for LaneBackend {
    delegate::delegate! {
        to self.inner {
            fn capabilities(&self) -> Result<BackendCapabilities, BleHubError>;
            fn bind_event_ingress(&self, ingress: Arc<BackendEventIngressHandle>) -> Result<(), BleHubError>;
            fn initialize(&self, command: InitializeCommand) -> Result<(), CommandDispatchError>;
            fn start_scan(&self, command: StartScanCommand) -> Result<(), CommandDispatchError>;
            fn stop_scan(&self, command: StopScanCommand) -> Result<(), CommandDispatchError>;
            fn connect(&self, command: ConnectCommand) -> Result<(), CommandDispatchError>;
            fn disconnect(&self, command: DisconnectCommand) -> Result<(), CommandDispatchError>;
            fn discover_gatt(&self, command: DiscoverGattCommand) -> Result<(), CommandDispatchError>;
            fn read_characteristic(&self, command: ReadCharacteristicCommand) -> Result<(), CommandDispatchError>;
            fn read_rssi(&self, command: ReadRssiCommand) -> Result<(), CommandDispatchError>;
            fn cancel_operation(&self, request: CancelOperationRequest) -> Result<(), CommandDispatchError>;
            fn dispose(&self) -> Result<(), BleHubError>;
        }
    }

    fn write_characteristic(
        &self,
        command: WriteCharacteristicCommand,
    ) -> Result<(), CommandDispatchError> {
        self.inner.write_characteristic(command.clone())?;
        self.accepted(RecordedCommand::WriteCharacteristic(command));
        Ok(())
    }

    fn set_notifications(
        &self,
        command: SetNotificationsCommand,
    ) -> Result<(), CommandDispatchError> {
        self.inner.set_notifications(command)?;
        self.accepted(RecordedCommand::SetNotifications(command));
        Ok(())
    }
}

enum Pending {
    Write(Arc<WriteOperation>),
    Enable(Arc<EnableNotificationsOperation>),
    Disable(Arc<DisableNotificationsOperation>),
}

impl Pending {
    async fn wait(self) -> Result<(), BlengError> {
        match self {
            Self::Write(op) => op.wait(DEADLINE).await,
            Self::Enable(op) => op.wait(DEADLINE).await.map(|_| ()),
            Self::Disable(op) => op.wait(DEADLINE).await,
        }
    }
}

fn begin(bleng: &Bleng, command: &RecordedCommand) -> Pending {
    match command {
        RecordedCommand::WriteCharacteristic(c) => bleng
            .begin_write_characteristic(
                c.connection_id,
                c.gatt_session_id,
                c.characteristic_id,
                c.value.clone(),
                c.write_type,
            )
            .map(Pending::Write),
        RecordedCommand::SetNotifications(c) => match c.action {
            NotificationAction::Enable { mode } => bleng
                .begin_enable_notifications(
                    c.connection_id,
                    c.gatt_session_id,
                    c.characteristic_id,
                    mode,
                )
                .map(Pending::Enable),
            NotificationAction::Disable => bleng
                .begin_disable_notifications(c.connection_id, c.gatt_session_id, c.subscription_id)
                .map(Pending::Disable),
        },
        _ => panic!("not a write/CCC command"),
    }
    .unwrap()
}

async fn bounded<T>(future: impl Future<Output = T>) -> T {
    tokio::time::timeout(Duration::from_millis(DEADLINE), future)
        .await
        .expect("model progress deadline")
}

fn emit(handle: &FakeBackendHandle, event: BleEvent) {
    assert_eq!(handle.emit(event), EventIngressStatus::Delivered);
}

async fn observe(bleng: &Bleng, event: BleEvent) {
    bounded(async {
        loop {
            if bleng.next_event(DEADLINE).await.unwrap() == Some(event.clone()) {
                break;
            }
        }
    })
    .await;
}

fn assert_loss(result: Result<(), BlengError>, error: Option<&BleEventError>) {
    match (result, error) {
        (Err(BlengError::Disconnected), None) => {}
        (Err(BlengError::Operation { error: actual }), Some(expected)) => {
            assert_eq!(&actual, expected)
        }
        (result, expected) => panic!("wrong typed loss result: {result:?}; expected {expected:?}"),
    }
}

async fn run(case: Case) {
    let head_id = case.head_id();
    // Three rejected submissions below still own unique command identities.
    let fresh_connect = head_id + 5;
    let fresh_discover = fresh_connect + 1;
    // Two stale-GATT/subscription admissions also consume command identities.
    let fresh_write_id = fresh_connect + 4;
    let fresh_enable_id = fresh_connect + 5;
    let fresh_read_id = fresh_connect + 6;
    let setup = notify(
        4,
        1,
        2,
        3,
        NotificationAction::Enable {
            mode: NotificationModeRequest::PlatformDefault,
        },
    );
    let head = case.head();
    let sibling = case.sibling();
    let fresh_write = write(fresh_write_id, 4, 5, WriteType::WithResponse);
    let fresh_enable = notify(
        fresh_enable_id,
        4,
        5,
        6,
        NotificationAction::Enable {
            mode: NotificationModeRequest::PlatformDefault,
        },
    );
    let mut scenarios = vec![
        EventScenario::emit_before_return(
            RecordedCommand::Initialize(InitializeCommand {
                command_id: 1.into(),
            }),
            vec![
                BleEvent::AdapterSnapshotChanged {
                    command_id: Some(1.into()),
                    snapshot: snapshot(),
                },
                terminal(1, None),
            ],
        ),
        EventScenario::emit_before_return(connect(2, 1), vec![connected(2, 1), terminal(2, None)]),
        EventScenario::emit_before_return(
            discovery(3, 1, 2),
            vec![
                BleEvent::GattDiscovered {
                    command_id: 3.into(),
                    database: database(1, 2),
                },
                terminal(3, None),
            ],
        ),
    ];
    if matches!(case, Case::DisablePermissionLoss) {
        scenarios.push(EventScenario::accept(setup.clone()));
    }
    scenarios.extend([
        EventScenario::accept(head.clone()),
        EventScenario::accept(sibling.clone()),
        EventScenario::emit_before_return(
            connect(fresh_connect, 4),
            vec![terminal(fresh_connect, None), connected(fresh_connect, 4)],
        ),
        EventScenario::emit_before_return(
            discovery(fresh_discover, 4, 5),
            vec![
                terminal(fresh_discover, None),
                BleEvent::GattDiscovered {
                    command_id: fresh_discover.into(),
                    database: database(4, 5),
                },
            ],
        ),
        EventScenario::accept(fresh_write.clone()),
        EventScenario::accept(fresh_enable.clone()),
        EventScenario::accept(RecordedCommand::ReadCharacteristic(
            ReadCharacteristicCommand {
                command_id: fresh_read_id.into(),
                connection_id: 4.into(),
                gatt_session_id: 5.into(),
                characteristic_id: 30.into(),
            },
        )),
    ]);
    let (inner, handle) = FakeBackend::scripted(scenarios);
    let lane = Arc::new(Mutex::new(VecDeque::new()));
    let (accepted, mut accepted_rx) = mpsc::unbounded_channel();
    let bleng = Bleng::new().unwrap();
    bleng
        .assemble_backend(Arc::new(LaneBackend {
            inner,
            lane: Arc::clone(&lane),
            accepted,
        }))
        .await
        .unwrap();
    assert_eq!(bleng.initialize(DEADLINE).await.unwrap(), snapshot());
    let connection = bleng.connect(10.into(), DEADLINE).await.unwrap();
    assert_eq!(connection, 1.into());
    assert_eq!(
        bleng.discover_gatt(connection, DEADLINE).await.unwrap(),
        database(1, 2)
    );
    if matches!(case, Case::DisablePermissionLoss) {
        let enabling = bleng
            .begin_enable_notifications(
                connection,
                2.into(),
                31.into(),
                NotificationModeRequest::PlatformDefault,
            )
            .unwrap();
        assert_eq!(bounded(accepted_rx.recv()).await.unwrap(), setup);
        emit(&handle, domain(&setup));
        emit(&handle, terminal(4, None));
        assert_eq!(enabling.wait(DEADLINE).await.unwrap(), 3.into());
        assert_eq!(lane.lock().unwrap().pop_front(), Some(setup));
    }
    let head_op = begin(&bleng, &head);
    assert_eq!(bounded(accepted_rx.recv()).await.unwrap(), head);
    let head_op = if matches!(case, Case::NoResponseLocalSuccessAdapterOff) {
        emit(&handle, domain(&head));
        emit(&handle, terminal(head_id, None));
        head_op.wait().await.unwrap();
        // Local success is not remote delivery or native lane retirement.
        assert_eq!(lane.lock().unwrap().front(), Some(&head));
        None
    } else {
        Some(head_op)
    };
    let sibling_op = begin(&bleng, &sibling);
    assert_eq!(bounded(accepted_rx.recv()).await.unwrap(), sibling);
    assert_eq!(
        *lane.lock().unwrap(),
        VecDeque::from([head.clone(), sibling.clone()])
    );
    assert!(
        !handle.terminal_attempted(head_id.into())
            || matches!(case, Case::NoResponseLocalSuccessAdapterOff)
    );
    assert!(!handle.terminal_attempted((head_id + 1).into()));

    let error = case.error();
    let mut unavailable = snapshot();
    match case {
        Case::NoResponseLocalSuccessAdapterOff => unavailable.state = AdapterState::PoweredOff,
        Case::DisablePermissionLoss => {
            unavailable.scan_authorization = AuthorizationState::Denied;
            unavailable.connection_authorization = AuthorizationState::Denied;
        }
        _ => {}
    }
    emit(
        &handle,
        BleEvent::AdapterSnapshotChanged {
            command_id: None,
            snapshot: unavailable,
        },
    );
    // Native outcome order is explicit. No adapter control API or snapshot-only
    // inference manufactures a write/CCC failure in BLeng.
    lane.lock().unwrap().clear();
    if let Some(error) = &error {
        if head_op.is_some() {
            emit(&handle, terminal(head_id, Some(error.clone())));
        }
        emit(&handle, terminal(head_id + 1, Some(error.clone())));
    }
    emit(
        &handle,
        BleEvent::ConnectionStateChanged {
            command_id: None,
            connection_id: connection,
            result: match &error {
                Some(error) => ConnectionStateResult::Err {
                    error: error.clone(),
                },
                None => ConnectionStateResult::Ok {
                    state: ConnectionState::Disconnected,
                },
            },
        },
    );
    if let Some(op) = head_op {
        assert_loss(op.wait().await, error.as_ref());
    }
    assert_loss(sibling_op.wait().await, error.as_ref());

    let restored = BleEvent::AdapterSnapshotChanged {
        command_id: None,
        snapshot: snapshot(),
    };
    emit(&handle, restored.clone());
    observe(&bleng, restored).await;
    let before_reconnect = handle.commands();
    assert_eq!(before_reconnect.len(), (head_id + 1) as usize);
    assert!(matches!(
        bleng
            .begin_write_characteristic(
                connection,
                2.into(),
                30.into(),
                vec![1],
                WriteType::WithResponse
            )
            .unwrap()
            .wait(DEADLINE)
            .await,
        Err(BlengError::NotReady {
            reason: BlengNotReadyReason::ConnectionNotActive
        })
    ));
    assert!(matches!(
        bleng
            .begin_enable_notifications(
                connection,
                2.into(),
                31.into(),
                NotificationModeRequest::PlatformDefault
            )
            .unwrap()
            .wait(DEADLINE)
            .await,
        Err(BlengError::NotReady {
            reason: BlengNotReadyReason::ConnectionNotActive
        })
    ));
    assert!(matches!(
        bleng
            .begin_disable_notifications(connection, 2.into(), 3.into())
            .unwrap()
            .wait(DEADLINE)
            .await,
        Err(BlengError::NotReady {
            reason: BlengNotReadyReason::ConnectionNotActive
        })
    ));
    assert_eq!(
        handle.commands(),
        before_reconnect,
        "restore must not reconnect or replay old commands"
    );
    assert!(lane.lock().unwrap().is_empty());
    assert_eq!(handle.dispose_count(), 0);

    let fresh_connection = bleng.connect(10.into(), DEADLINE).await.unwrap();
    assert_eq!(fresh_connection, 4.into());
    assert_ne!(fresh_connection, connection);
    assert_eq!(
        bleng
            .discover_gatt(fresh_connection, DEADLINE)
            .await
            .unwrap(),
        database(4, 5)
    );
    assert!(matches!(
        bleng
            .begin_write_characteristic(
                fresh_connection,
                2.into(),
                30.into(),
                vec![1],
                WriteType::WithResponse
            )
            .unwrap()
            .wait(DEADLINE)
            .await,
        Err(BlengError::NotReady {
            reason: BlengNotReadyReason::GattNotReady
        })
    ));
    assert!(matches!(
        bleng
            .begin_disable_notifications(fresh_connection, 5.into(), 3.into())
            .unwrap()
            .wait(DEADLINE)
            .await,
        Err(BlengError::NotReady {
            reason: BlengNotReadyReason::SubscriptionNotActive
        })
    ));
    let new_write = begin(&bleng, &fresh_write);
    assert_eq!(bounded(accepted_rx.recv()).await.unwrap(), fresh_write);
    // Old domains and terminals arrive while a new operation is live. Poison
    // terminals would fail it if correlation accidentally reused the old IDs.
    for command in [&head, &sibling] {
        emit(&handle, domain(command));
    }
    let stale_error = BleEventError {
        category: BleErrorCategory::Gatt,
        message: "stale terminal must not poison the fresh operation".into(),
        platform_details: None,
    };
    emit(&handle, terminal(head_id, Some(stale_error.clone())));
    emit(&handle, terminal(head_id + 1, Some(stale_error.clone())));
    emit(
        &handle,
        BleEvent::ConnectionStateChanged {
            command_id: None,
            connection_id: connection,
            result: ConnectionStateResult::Ok {
                state: ConnectionState::Disconnected,
            },
        },
    );
    emit(&handle, terminal(fresh_write_id, None));
    emit(&handle, domain(&fresh_write));
    new_write.wait().await.unwrap();
    assert_eq!(lane.lock().unwrap().pop_front(), Some(fresh_write));
    let enabling = bleng
        .begin_enable_notifications(
            fresh_connection,
            5.into(),
            31.into(),
            NotificationModeRequest::PlatformDefault,
        )
        .unwrap();
    assert_eq!(bounded(accepted_rx.recv()).await.unwrap(), fresh_enable);
    emit(&handle, domain(&head));
    emit(&handle, domain(&sibling));
    emit(&handle, domain(&fresh_enable));
    emit(&handle, terminal(fresh_enable_id, None));
    assert_eq!(enabling.wait(DEADLINE).await.unwrap(), 6.into());
    assert_eq!(lane.lock().unwrap().pop_front(), Some(fresh_enable));

    let reading = bleng
        .begin_read_characteristic(fresh_connection, 5.into(), 30.into())
        .unwrap();
    // Even a current command ID with an old connection/session cannot supply
    // the new generation's data. The final typed payload distinguishes it.
    let read_value = |connection: u64, session: u64, value| BleEvent::CharacteristicRead {
        command_id: fresh_read_id.into(),
        connection_id: connection.into(),
        gatt_session_id: session.into(),
        characteristic_id: 30.into(),
        value,
    };
    emit(&handle, read_value(1, 2, vec![0xde, 0xad]));
    emit(&handle, terminal(head_id, Some(stale_error)));
    emit(&handle, terminal(fresh_read_id, None));
    emit(&handle, read_value(4, 5, vec![0x26, 0x10]));
    assert_eq!(reading.wait(DEADLINE).await.unwrap(), vec![0x26, 0x10]);
    assert_eq!(
        handle
            .commands()
            .iter()
            .filter(|c| matches!(c, RecordedCommand::Initialize(_)))
            .count(),
        1
    );
    assert_eq!(
        handle
            .commands()
            .iter()
            .filter(|c| matches!(c, RecordedCommand::Connect(_)))
            .count(),
        2
    );
    assert_eq!(handle.commands().len(), (fresh_read_id - 5) as usize);
    assert_eq!(handle.dispose_count(), 0);
    bleng.shutdown().await.unwrap();
    assert_eq!(handle.dispose_count(), 1);
}

#[test]
fn initialized_once_write_ccc_loss_restore_requires_explicit_fresh_connection() {
    let caller = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .unwrap();
    for case in [
        Case::ResponseWritePassiveLoss,
        Case::NoResponseLocalSuccessAdapterOff,
        Case::EnablePassiveLoss,
        Case::DisablePermissionLoss,
    ] {
        eprintln!("canonical write/CCC loss model: {case:?}");
        caller.block_on(bounded(run(case)));
    }
}
