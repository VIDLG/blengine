#[cfg(not(target_os = "macos"))]
use std::sync::Arc;

#[cfg(target_os = "macos")]
mod macos_assembly;

#[cfg(not(target_os = "macos"))]
use blehub::{
    AdapterSnapshot, AdapterState, AuthorizationState, BleEvent, CommandId, ConnectionId,
    ConnectionState, ConnectionStateResult, GattCharacteristicId, GattSessionId, InitializeCommand,
    NotificationMode, NotificationState, PeripheralId, ReadCharacteristicCommand, ScanSessionId,
    ScanStopReason, ScanStopResult, SubscriptionId,
};
#[cfg(target_os = "macos")]
use blehub_tauri_plugin_spike::init_with_assembly;
#[cfg(not(target_os = "macos"))]
use blehub_tauri_plugin_spike::{CallbackDriver, init};
#[cfg(not(target_os = "macos"))]
use blehub_testkit::{
    EventScenario, FakeBackend, FakeBackendHandle, RecordedCommand,
    advertisement_observation_fixture, duplicate_gatt_database_fixture,
};

#[cfg(not(target_os = "macos"))]
struct FakeDriver(FakeBackendHandle);

#[cfg(target_os = "macos")]
#[tauri::command]
fn macos_assembly_probe() -> Result<u32, String> {
    macos_assembly::probe()
}

#[cfg(not(target_os = "macos"))]
impl CallbackDriver for FakeDriver {
    fn advance_all(&self) -> usize {
        self.0.advance_all().len()
    }
}

#[cfg(not(target_os = "macos"))]
fn id(value: u64) -> CommandId {
    CommandId { value }
}

#[cfg(not(target_os = "macos"))]
fn success(command_id: CommandId) -> BleEvent {
    BleEvent::CommandCompleted {
        command_id,
        error: None,
    }
}

#[cfg(not(target_os = "macos"))]
fn main() {
    let connection_id = ConnectionId { value: 301 };
    let gatt_session_id = GattSessionId { value: 401 };
    let characteristic_id = GattCharacteristicId { value: 601 };
    let subscription_id = SubscriptionId { value: 801 };
    let database = duplicate_gatt_database_fixture();
    let advertisement = advertisement_observation_fixture();

    let scenarios = [
        EventScenario::complete(
            RecordedCommand::Initialize(InitializeCommand { command_id: id(1) }),
            vec![
                BleEvent::AdapterSnapshotChanged {
                    command_id: Some(id(1)),
                    snapshot: AdapterSnapshot {
                        state: AdapterState::PoweredOn,
                        scan_authorization: AuthorizationState::Authorized,
                        connection_authorization: AuthorizationState::Authorized,
                        name: Some("tauri-spike".to_owned()),
                    },
                },
                success(id(1)),
            ],
        ),
        EventScenario::complete(
            RecordedCommand::StartScan(blehub::StartScanCommand {
                command_id: id(2),
                scan_session_id: ScanSessionId { value: 101 },
                options: blehub::ScanOptions::default(),
            }),
            vec![
                BleEvent::ScanStarted {
                    command_id: id(2),
                    scan_id: ScanSessionId { value: 101 },
                },
                BleEvent::AdvertisementObserved { advertisement },
                success(id(2)),
            ],
        ),
        EventScenario::complete(
            RecordedCommand::StopScan(blehub::StopScanCommand {
                command_id: id(3),
                scan_session_id: ScanSessionId { value: 101 },
            }),
            vec![
                BleEvent::ScanStopped {
                    scan_id: ScanSessionId { value: 101 },
                    result: ScanStopResult::Ok {
                        reason: ScanStopReason::Requested { command_id: id(3) },
                    },
                },
                success(id(3)),
            ],
        ),
        EventScenario::complete(
            RecordedCommand::Connect(blehub::ConnectCommand {
                command_id: id(4),
                connection_id,
                peripheral_id: PeripheralId { value: 201 },
            }),
            vec![
                BleEvent::ConnectionStateChanged {
                    command_id: Some(id(4)),
                    connection_id,
                    result: ConnectionStateResult::Ok {
                        state: ConnectionState::Connected,
                    },
                },
                success(id(4)),
            ],
        ),
        EventScenario::complete(
            RecordedCommand::DiscoverGatt(blehub::DiscoverGattCommand {
                command_id: id(5),
                connection_id,
                gatt_session_id,
            }),
            vec![
                BleEvent::GattDiscovered {
                    command_id: id(5),
                    database,
                },
                success(id(5)),
            ],
        ),
        EventScenario::complete(
            RecordedCommand::ReadCharacteristic(ReadCharacteristicCommand {
                command_id: id(6),
                connection_id,
                gatt_session_id,
                characteristic_id,
            }),
            vec![
                BleEvent::CharacteristicRead {
                    command_id: id(6),
                    connection_id,
                    gatt_session_id,
                    characteristic_id,
                    value: vec![0x00],
                },
                success(id(6)),
            ],
        ),
        EventScenario::complete(
            RecordedCommand::WriteCharacteristic(blehub::WriteCharacteristicCommand {
                command_id: id(7),
                connection_id,
                gatt_session_id,
                characteristic_id,
                value: vec![0x10, 0x20, 0x30],
                write_type: blehub::WriteType::WithResponse,
            }),
            vec![
                BleEvent::CharacteristicWritten {
                    command_id: id(7),
                    connection_id,
                    gatt_session_id,
                    characteristic_id,
                },
                success(id(7)),
            ],
        ),
        EventScenario::complete(
            RecordedCommand::WriteCharacteristic(blehub::WriteCharacteristicCommand {
                command_id: id(8),
                connection_id,
                gatt_session_id,
                characteristic_id,
                value: vec![0xab, 0xcd],
                write_type: blehub::WriteType::WithoutResponse,
            }),
            vec![
                BleEvent::CharacteristicWritten {
                    command_id: id(8),
                    connection_id,
                    gatt_session_id,
                    characteristic_id,
                },
                success(id(8)),
            ],
        ),
        EventScenario::complete(
            RecordedCommand::SetNotifications(blehub::SetNotificationsCommand {
                command_id: id(9),
                connection_id,
                gatt_session_id,
                subscription_id,
                characteristic_id,
                action: blehub::NotificationAction::Enable {
                    mode: blehub::NotificationModeRequest::PlatformDefault,
                },
            }),
            vec![
                BleEvent::NotificationStateChanged {
                    command_id: id(9),
                    connection_id,
                    gatt_session_id,
                    subscription_id,
                    characteristic_id,
                    state: NotificationState::Enabled {
                        actual_mode: Some(NotificationMode::Notification),
                    },
                },
                BleEvent::NotificationReceived {
                    connection_id,
                    gatt_session_id,
                    subscription_id,
                    characteristic_id,
                    value: vec![0xde, 0xad, 0xbe, 0xef],
                },
                success(id(9)),
            ],
        ),
        EventScenario::complete(
            RecordedCommand::ReadRssi(blehub::ReadRssiCommand {
                command_id: id(10),
                connection_id,
            }),
            vec![
                BleEvent::RssiRead {
                    command_id: id(10),
                    connection_id,
                    rssi: -48,
                },
                success(id(10)),
            ],
        ),
        EventScenario::complete(
            RecordedCommand::SetNotifications(blehub::SetNotificationsCommand {
                command_id: id(11),
                connection_id,
                gatt_session_id,
                subscription_id,
                characteristic_id,
                action: blehub::NotificationAction::Disable,
            }),
            vec![
                BleEvent::NotificationStateChanged {
                    command_id: id(11),
                    connection_id,
                    gatt_session_id,
                    subscription_id,
                    characteristic_id,
                    state: NotificationState::Disabled,
                },
                success(id(11)),
            ],
        ),
        EventScenario::complete(
            RecordedCommand::Disconnect(blehub::DisconnectCommand {
                command_id: id(12),
                connection_id,
            }),
            vec![
                BleEvent::ConnectionStateChanged {
                    command_id: Some(id(12)),
                    connection_id,
                    result: ConnectionStateResult::Ok {
                        state: ConnectionState::Disconnected,
                    },
                },
                success(id(12)),
            ],
        ),
    ];
    let (backend, driver) = FakeBackend::scripted(scenarios);

    let builder = tauri::Builder::default();
    #[cfg(feature = "wdio")]
    let builder = builder.plugin(tauri_plugin_wdio_webdriver::init());

    builder
        .plugin(init(Arc::new(backend), Some(Arc::new(FakeDriver(driver)))))
        .invoke_handler(tauri::generate_handler![macos_assembly_probe])
        .run(tauri::generate_context!())
        .expect("error while running Tauri spike");
}

#[cfg(target_os = "macos")]
fn main() {
    let builder = tauri::Builder::default();
    #[cfg(feature = "wdio")]
    let builder = builder.plugin(tauri_plugin_wdio_webdriver::init());

    builder
        .plugin(init_with_assembly(macos_assembly::assemble))
        .invoke_handler(tauri::generate_handler![macos_assembly_probe])
        .run(tauri::generate_context!())
        .expect("error while running Tauri macOS host");
}
