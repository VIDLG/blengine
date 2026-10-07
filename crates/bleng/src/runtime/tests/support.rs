//! Owner-local fixtures and shared scenario data. These helpers are compiled
//! only inside owner tests; they do not add production methods or field access.

use crate::runtime::{
    RuntimePhase,
    actor::{AssumeInitialized, RegisterWaiter},
    messages::DispatchResolved,
    owner::WaiterTicket,
};
use kameo::error::SendError;

pub(super) use crate::runtime::actor::HoldActor;

pub(super) use crate::runtime::{EventReadError, RuntimeOwner};
pub(super) use crate::{
    cleanup::AssemblyAdmissionError,
    waiter::{WaiterError, WaiterExpectation, WaiterSuccess},
};
pub(super) use blehub::{
    AdapterSnapshot, AdapterState, AdvertisementObservation, AuthorizationState,
    BackendCapabilities, BackendEventIngressHandle, BleBackend, BleErrorCategory, BleEvent,
    BleEventError, BleEventSink, BleHub, BleHubError, BluetoothUuid, CharacteristicProperties,
    CommandDispatchError, CommandId, ConnectCommand, ConnectionId, ConnectionState,
    ConnectionStateResult, DisconnectCommand, DiscoverGattCommand, EventIngressStatus,
    GattCharacteristic, GattCharacteristicId, GattDatabase, GattService, GattServiceId,
    GattSessionId, InitializeCommand, NotificationAction, NotificationMode,
    NotificationModeRequest, NotificationState, PeripheralId, ReadCharacteristicCommand,
    ReadRssiCommand, ScanOptions, ScanSessionId, ScanStopReason, ScanStopResult,
    SetNotificationsCommand, StartScanCommand, StopScanCommand, SubscriptionId,
    WriteCharacteristicCommand, WriteType,
};
pub(super) use blehub_testkit::{EventScenario, FakeBackend, RecordedCommand};
pub(super) use std::{
    sync::{Arc, Mutex, mpsc},
    time::Duration,
};
pub(super) use tokio::sync::{Notify, oneshot};

impl RuntimeOwner {
    pub(super) fn new_assuming_initialized() -> std::io::Result<Self> {
        let owner = Self::new()?;
        owner
            .actor
            .tell(AssumeInitialized)
            .try_send()
            .expect("new actor has mailbox capacity");
        Ok(owner)
    }

    pub(super) async fn register_waiter(
        &self,
        id: CommandId,
        expectation: WaiterExpectation,
    ) -> Result<WaiterTicket, WaiterError> {
        match self.lifecycle.phase() {
            RuntimePhase::Closed => return Err(WaiterError::Closed),
            RuntimePhase::Faulted => return Err(WaiterError::TransportFault),
            _ => {}
        }
        let result_rx = match self.actor.ask(RegisterWaiter { id, expectation }).await {
            Ok(result_rx) => result_rx,
            Err(SendError::HandlerError(error)) => return Err(error),
            Err(_) => {
                self.lifecycle.fault();
                return Err(match self.lifecycle.phase() {
                    RuntimePhase::Closed => WaiterError::Closed,
                    _ => WaiterError::TransportFault,
                });
            }
        };
        Ok(self.ticket(id, result_rx))
    }

    pub(super) fn resolve_dispatch(&self, id: CommandId, result: Result<(), CommandDispatchError>) {
        if self
            .actor
            .tell(DispatchResolved { id, result })
            .try_send()
            .is_err()
            && self.lifecycle.phase() == RuntimePhase::Open
        {
            self.lifecycle.fault();
        }
    }

    pub(super) async fn discover_gatt_session(
        &self,
        connection: ConnectionId,
        deadline: Duration,
    ) -> Result<GattSessionId, WaiterError> {
        self.discover_gatt(connection, deadline)
            .await
            .map(|database| database.gatt_session_id)
    }

    pub(super) fn is_closed(&self) -> bool {
        self.lifecycle.phase() == RuntimePhase::Closed
    }

    pub(super) fn attach_hub(&self, hub: Arc<BleHub>) -> Result<(), AssemblyAdmissionError> {
        if self.lifecycle.phase() != RuntimePhase::Open {
            self.lifecycle.start_cleanup();
        }
        self.lifecycle.attach_hub(hub)
    }

    pub(super) fn event_sink(&self) -> Arc<dyn BleEventSink> {
        self.backend_port()
    }

    pub(super) fn faulted_for_test(&self) -> bool {
        self.lifecycle.phase() == RuntimePhase::Faulted
    }

    pub(super) async fn wait_for_actor_stop_for_test(&self) {
        self.actor.wait_for_shutdown().await;
    }
}

pub(super) enum BlockedDispatch {
    Rssi,
    NotificationEnable(GattCharacteristicId),
}

pub(super) struct BlockingDispatchBackend {
    pub(super) inner: FakeBackend,
    pub(super) blocked: BlockedDispatch,
    pub(super) entered: Arc<Notify>,
    pub(super) release_rx: Arc<Mutex<mpsc::Receiver<()>>>,
    pub(super) finished: Arc<Notify>,
}

impl BlockingDispatchBackend {
    fn wait(&self) {
        self.entered.notify_one();
        self.release_rx
            .lock()
            .unwrap()
            .recv()
            .expect("blocking dispatch release sender dropped");
        self.finished.notify_one();
    }
}

impl BleBackend for BlockingDispatchBackend {
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
            fn write_characteristic(&self, command: WriteCharacteristicCommand) -> Result<(), CommandDispatchError>;
            fn dispose(&self) -> Result<(), BleHubError>;
        }
    }

    fn set_notifications(
        &self,
        command: SetNotificationsCommand,
    ) -> Result<(), CommandDispatchError> {
        let should_block = match (&self.blocked, &command.action) {
            (
                BlockedDispatch::NotificationEnable(characteristic),
                NotificationAction::Enable { .. },
            ) => *characteristic == command.characteristic_id,
            _ => false,
        };
        if should_block {
            self.wait();
        }
        self.inner.set_notifications(command)
    }

    fn read_rssi(&self, command: ReadRssiCommand) -> Result<(), CommandDispatchError> {
        if matches!(self.blocked, BlockedDispatch::Rssi) {
            self.wait();
        }
        self.inner.read_rssi(command)
    }
}

pub(super) fn event() -> BleEvent {
    event_with_message("fixture")
}

pub(super) fn event_with_message(message: &str) -> BleEvent {
    BleEvent::BackendError {
        error: BleEventError {
            category: BleErrorCategory::Internal,
            message: message.to_owned(),
            platform_details: None,
        },
    }
}

pub(super) fn init_command(id: u64) -> RecordedCommand {
    RecordedCommand::Initialize(InitializeCommand {
        command_id: id.into(),
    })
}

pub(super) fn init_snapshot() -> AdapterSnapshot {
    AdapterSnapshot {
        state: AdapterState::PoweredOn,
        scan_authorization: AuthorizationState::Authorized,
        connection_authorization: AuthorizationState::Authorized,
        name: Some("fixture".to_owned()),
    }
}

pub(super) fn init_snapshot_event(command: Option<u64>) -> BleEvent {
    BleEvent::AdapterSnapshotChanged {
        command_id: command.map(Into::into),
        snapshot: init_snapshot(),
    }
}

pub(super) fn connected_event(command: u64, connection: u64) -> BleEvent {
    BleEvent::ConnectionStateChanged {
        command_id: Some(command.into()),
        connection_id: connection.into(),
        result: ConnectionStateResult::Ok {
            state: ConnectionState::Connected,
        },
    }
}

pub(super) fn connect_scenario(command: u64, connection: u64, peripheral: u64) -> EventScenario {
    EventScenario::emit_before_return(
        RecordedCommand::Connect(ConnectCommand {
            command_id: command.into(),
            connection_id: connection.into(),
            peripheral_id: peripheral.into(),
        }),
        vec![
            connected_event(command, connection),
            BleEvent::CommandCompleted {
                command_id: command.into(),
                error: None,
            },
        ],
    )
}

pub(super) fn disconnect_command(command: u64, connection: u64) -> RecordedCommand {
    RecordedCommand::Disconnect(DisconnectCommand {
        command_id: command.into(),
        connection_id: connection.into(),
    })
}

pub(super) fn disconnected_event(command: Option<u64>, connection: u64) -> BleEvent {
    BleEvent::ConnectionStateChanged {
        command_id: command.map(Into::into),
        connection_id: connection.into(),
        result: ConnectionStateResult::Ok {
            state: ConnectionState::Disconnected,
        },
    }
}

pub(super) fn passive_disconnect_error_event(connection: u64, error: BleEventError) -> BleEvent {
    BleEvent::ConnectionStateChanged {
        command_id: None,
        connection_id: connection.into(),
        result: ConnectionStateResult::Err { error },
    }
}

pub(super) fn command_completed(command: u64, error: Option<BleEventError>) -> BleEvent {
    BleEvent::CommandCompleted {
        command_id: command.into(),
        error,
    }
}

pub(super) fn rssi_command(command: u64, connection: u64) -> RecordedCommand {
    RecordedCommand::ReadRssi(ReadRssiCommand {
        command_id: command.into(),
        connection_id: connection.into(),
    })
}

pub(super) fn rssi_value(command: u64, connection: u64, rssi: i16) -> BleEvent {
    BleEvent::RssiRead {
        command_id: command.into(),
        connection_id: connection.into(),
        rssi,
    }
}

pub(super) fn rssi_terminal(command: u64) -> BleEvent {
    BleEvent::CommandCompleted {
        command_id: command.into(),
        error: None,
    }
}

pub(super) fn gatt_scenario(command: u64, connection: u64, session: u64) -> EventScenario {
    EventScenario::emit_before_return(
        RecordedCommand::DiscoverGatt(DiscoverGattCommand {
            command_id: command.into(),
            connection_id: connection.into(),
            gatt_session_id: session.into(),
        }),
        vec![
            BleEvent::GattDiscovered {
                command_id: command.into(),
                database: GattDatabase {
                    connection_id: connection.into(),
                    gatt_session_id: session.into(),
                    services: Vec::new(),
                },
            },
            BleEvent::CommandCompleted {
                command_id: command.into(),
                error: None,
            },
        ],
    )
}

pub(super) fn read_database() -> GattDatabase {
    GattDatabase {
        connection_id: ConnectionId { value: 1 },
        gatt_session_id: GattSessionId { value: 2 },
        services: vec![GattService {
            id: GattServiceId { value: 1 },
            uuid: BluetoothUuid {
                value: "0000180d-0000-1000-8000-00805f9b34fb".to_owned(),
            },
            primary: Some(true),
            characteristics: vec![GattCharacteristic {
                id: GattCharacteristicId { value: 30 },
                uuid: BluetoothUuid {
                    value: "00002a37-0000-1000-8000-00805f9b34fb".to_owned(),
                },
                properties: CharacteristicProperties {
                    readable: true,
                    writable_with_response: false,
                    writable_without_response: false,
                    notifiable: false,
                    indicatable: false,
                },
                descriptors: Vec::new(),
            }],
        }],
    }
}

pub(super) fn read_discovery() -> EventScenario {
    EventScenario::emit_before_return(
        RecordedCommand::DiscoverGatt(DiscoverGattCommand {
            command_id: CommandId { value: 2 },
            connection_id: ConnectionId { value: 1 },
            gatt_session_id: GattSessionId { value: 2 },
        }),
        vec![
            BleEvent::GattDiscovered {
                command_id: CommandId { value: 2 },
                database: read_database(),
            },
            BleEvent::CommandCompleted {
                command_id: CommandId { value: 2 },
                error: None,
            },
        ],
    )
}

pub(super) fn notification_discovery() -> EventScenario {
    let mut database = read_database();
    let properties = &mut database.services[0].characteristics[0].properties;
    properties.notifiable = true;
    properties.indicatable = true;
    EventScenario::emit_before_return(
        RecordedCommand::DiscoverGatt(DiscoverGattCommand {
            command_id: CommandId { value: 2 },
            connection_id: ConnectionId { value: 1 },
            gatt_session_id: GattSessionId { value: 2 },
        }),
        vec![
            BleEvent::GattDiscovered {
                command_id: CommandId { value: 2 },
                database,
            },
            BleEvent::CommandCompleted {
                command_id: CommandId { value: 2 },
                error: None,
            },
        ],
    )
}

pub(super) fn read_command(command: u64) -> RecordedCommand {
    RecordedCommand::ReadCharacteristic(ReadCharacteristicCommand {
        command_id: command.into(),
        connection_id: ConnectionId { value: 1 },
        gatt_session_id: GattSessionId { value: 2 },
        characteristic_id: GattCharacteristicId { value: 30 },
    })
}

pub(super) fn read_value(command: u64, value: &[u8]) -> BleEvent {
    BleEvent::CharacteristicRead {
        command_id: command.into(),
        connection_id: ConnectionId { value: 1 },
        gatt_session_id: GattSessionId { value: 2 },
        characteristic_id: GattCharacteristicId { value: 30 },
        value: value.to_vec(),
    }
}

pub(super) fn read_terminal(command: u64, error: Option<BleEventError>) -> BleEvent {
    BleEvent::CommandCompleted {
        command_id: command.into(),
        error,
    }
}

pub(super) fn write_command(command: u64, value: &[u8], write_type: WriteType) -> RecordedCommand {
    RecordedCommand::WriteCharacteristic(WriteCharacteristicCommand {
        command_id: command.into(),
        connection_id: ConnectionId { value: 1 },
        gatt_session_id: GattSessionId { value: 2 },
        characteristic_id: GattCharacteristicId { value: 30 },
        value: value.to_vec(),
        write_type,
    })
}

pub(super) fn written(command: u64) -> BleEvent {
    BleEvent::CharacteristicWritten {
        command_id: command.into(),
        connection_id: ConnectionId { value: 1 },
        gatt_session_id: GattSessionId { value: 2 },
        characteristic_id: GattCharacteristicId { value: 30 },
    }
}

pub(super) fn notification_command(
    command: u64,
    subscription: u64,
    action: NotificationAction,
) -> RecordedCommand {
    RecordedCommand::SetNotifications(SetNotificationsCommand {
        command_id: command.into(),
        connection_id: ConnectionId { value: 1 },
        gatt_session_id: GattSessionId { value: 2 },
        subscription_id: subscription.into(),
        characteristic_id: GattCharacteristicId { value: 30 },
        action,
    })
}

pub(super) fn notification_state(
    command: u64,
    subscription: u64,
    state: NotificationState,
) -> BleEvent {
    BleEvent::NotificationStateChanged {
        command_id: command.into(),
        connection_id: ConnectionId { value: 1 },
        gatt_session_id: GattSessionId { value: 2 },
        subscription_id: subscription.into(),
        characteristic_id: GattCharacteristicId { value: 30 },
        state,
    }
}

pub(super) fn notification_value(subscription: u64) -> BleEvent {
    BleEvent::NotificationReceived {
        connection_id: ConnectionId { value: 1 },
        gatt_session_id: GattSessionId { value: 2 },
        subscription_id: subscription.into(),
        characteristic_id: GattCharacteristicId { value: 30 },
        value: vec![0xde, 0xad],
    }
}

pub(super) fn notification_enabled_scenario(command: u64, subscription: u64) -> EventScenario {
    EventScenario::emit_before_return(
        notification_command(
            command,
            subscription,
            NotificationAction::Enable {
                mode: NotificationModeRequest::PlatformDefault,
            },
        ),
        vec![
            notification_state(
                command,
                subscription,
                NotificationState::Enabled {
                    actual_mode: Some(NotificationMode::Notification),
                },
            ),
            read_terminal(command, None),
        ],
    )
}

pub(super) fn started_scan() -> EventScenario {
    EventScenario::emit_before_return(
        RecordedCommand::StartScan(StartScanCommand {
            command_id: CommandId { value: 1 },
            scan_session_id: ScanSessionId { value: 1 },
            options: ScanOptions::default(),
        }),
        vec![
            BleEvent::ScanStarted {
                command_id: CommandId { value: 1 },
                scan_id: ScanSessionId { value: 1 },
            },
            BleEvent::CommandCompleted {
                command_id: CommandId { value: 1 },
                error: None,
            },
        ],
    )
}

pub(super) fn requested_stop(command: u64) -> BleEvent {
    BleEvent::ScanStopped {
        scan_id: ScanSessionId { value: 1 },
        result: ScanStopResult::Ok {
            reason: ScanStopReason::Requested {
                command_id: command.into(),
            },
        },
    }
}

pub(super) fn stop_command(command: u64) -> RecordedCommand {
    RecordedCommand::StopScan(StopScanCommand {
        command_id: command.into(),
        scan_session_id: ScanSessionId { value: 1 },
    })
}
