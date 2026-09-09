use std::sync::{Arc, Mutex, mpsc};

use blehub::{
    AdapterSnapshot, BleBackend, BleEvent, BleEventSink, BleHub, CommandId, ConnectCommand,
    ConnectionId, DisconnectCommand, DiscoverGattCommand, EventSinkError, GattCharacteristicId,
    GattSessionId, InitializeCommand, NotificationAction, NotificationModeRequest, PeripheralId,
    ReadCharacteristicCommand, ReadRssiCommand, ScanOptions, ScanSessionId,
    SetNotificationsCommand, StartScanCommand, StopScanCommand, SubscriptionId,
    WriteCharacteristicCommand, WriteType,
};
use serde::{Deserialize, Serialize};
use tauri::{
    AppHandle, Emitter, Manager, Runtime, State,
    plugin::{Builder, TauriPlugin},
};

/// A host-owned driver used only by the spike to advance deterministic fake
/// backend callbacks. Real platform hosts provide a backend and omit it.
pub trait CallbackDriver: Send + Sync {
    fn advance_all(&self) -> usize;
}

struct ChannelSink {
    sender: Mutex<mpsc::SyncSender<BleEvent>>,
}

impl BleEventSink for ChannelSink {
    fn on_event(&self, event: BleEvent) -> Result<(), EventSinkError> {
        self.sender
            .lock()
            .map_err(|error| EventSinkError::Rejected {
                detail: format!("event channel mutex poisoned: {error}"),
            })?
            .try_send(event)
            .map_err(|error| EventSinkError::Rejected {
                detail: format!("event channel unavailable: {error}"),
            })
    }
}

#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize)]
pub struct ProjectedGattCharacteristic {
    pub id: u64,
    pub uuid: String,
    pub readable: bool,
    pub writable_with_response: bool,
    pub writable_without_response: bool,
    pub notifiable: bool,
    pub indicatable: bool,
}

#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize)]
pub struct ProjectedGattService {
    pub id: u64,
    pub uuid: String,
    pub primary: Option<bool>,
    pub characteristics: Vec<ProjectedGattCharacteristic>,
}

#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize)]
pub struct ProjectedEventData {
    pub peripheral_id: Option<u64>,
    pub scan_session_id: Option<u64>,
    pub connection_id: Option<u64>,
    pub gatt_session_id: Option<u64>,
    pub characteristic_id: Option<u64>,
    pub subscription_id: Option<u64>,
    pub local_name: Option<String>,
    pub advertised_services: Vec<String>,
    pub value: Option<Vec<u8>>,
    pub rssi: Option<i16>,
    pub connectable: Option<bool>,
    pub state: Option<String>,
    pub services: Vec<ProjectedGattService>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct ProjectedEvent {
    pub kind: &'static str,
    pub command_id: Option<u64>,
    pub detail: Option<String>,
    pub data: Option<ProjectedEventData>,
}

fn projected(
    kind: &'static str,
    command_id: Option<u64>,
    detail: Option<String>,
    data: Option<ProjectedEventData>,
) -> ProjectedEvent {
    ProjectedEvent {
        kind,
        command_id,
        detail,
        data,
    }
}

pub fn project_event(event: &BleEvent) -> ProjectedEvent {
    match event {
        BleEvent::AdapterSnapshotChanged {
            command_id,
            snapshot,
        } => projected(
            "adapter_snapshot_changed",
            command_id.map(|id| id.value),
            Some(format_adapter_snapshot(snapshot)),
            None,
        ),
        BleEvent::CommandCompleted { command_id, error } => projected(
            "command_completed",
            Some(command_id.value),
            error.as_ref().map(|error| error.message.clone()),
            None,
        ),
        BleEvent::ScanStarted {
            command_id,
            scan_id,
        } => projected(
            "scan_started",
            Some(command_id.value),
            Some(format!("scan_id={}", scan_id.value)),
            Some(ProjectedEventData {
                scan_session_id: Some(scan_id.value),
                ..Default::default()
            }),
        ),
        BleEvent::AdvertisementObserved { advertisement } => projected(
            "advertisement_observed",
            None,
            Some(format!(
                "peripheral_id={};name={:?};rssi={:?};services={}",
                advertisement.peripheral_id.value,
                advertisement.local_name,
                advertisement.rssi,
                advertisement.advertised_services.len()
            )),
            Some(ProjectedEventData {
                peripheral_id: Some(advertisement.peripheral_id.value),
                scan_session_id: Some(advertisement.scan_session_id.value),
                local_name: advertisement.local_name.clone(),
                advertised_services: advertisement
                    .advertised_services
                    .iter()
                    .map(|uuid| uuid.value.clone())
                    .collect(),
                rssi: advertisement.rssi,
                connectable: advertisement.connectable,
                ..Default::default()
            }),
        ),
        BleEvent::ScanStopped { scan_id, result } => projected(
            "scan_stopped",
            None,
            Some(format!("scan_id={};result={result:?}", scan_id.value)),
            Some(ProjectedEventData {
                scan_session_id: Some(scan_id.value),
                ..Default::default()
            }),
        ),
        BleEvent::ConnectionStateChanged {
            command_id,
            connection_id,
            result,
        } => projected(
            "connection_state_changed",
            command_id.map(|id| id.value),
            Some(format!(
                "connection_id={};result={result:?}",
                connection_id.value
            )),
            Some(ProjectedEventData {
                connection_id: Some(connection_id.value),
                state: Some(format!("{result:?}")),
                ..Default::default()
            }),
        ),
        BleEvent::GattDiscovered {
            command_id,
            database,
        } => projected(
            "gatt_discovered",
            Some(command_id.value),
            Some(format!(
                "connection_id={};gatt_session_id={};services={}",
                database.connection_id.value,
                database.gatt_session_id.value,
                database.services.len()
            )),
            Some(ProjectedEventData {
                connection_id: Some(database.connection_id.value),
                gatt_session_id: Some(database.gatt_session_id.value),
                services: database
                    .services
                    .iter()
                    .map(|service| ProjectedGattService {
                        id: service.id.value,
                        uuid: service.uuid.value.clone(),
                        primary: service.primary,
                        characteristics: service
                            .characteristics
                            .iter()
                            .map(|characteristic| ProjectedGattCharacteristic {
                                id: characteristic.id.value,
                                uuid: characteristic.uuid.value.clone(),
                                readable: characteristic.properties.readable,
                                writable_with_response: characteristic.properties.writable_with_response,
                                writable_without_response: characteristic.properties.writable_without_response,
                                notifiable: characteristic.properties.notifiable,
                                indicatable: characteristic.properties.indicatable,
                            })
                            .collect(),
                    })
                    .collect(),
                ..Default::default()
            }),
        ),
        BleEvent::CharacteristicRead {
            command_id,
            connection_id,
            gatt_session_id,
            characteristic_id,
            value,
        } => projected(
            "characteristic_read",
            Some(command_id.value),
            Some(format!(
                "connection_id={};gatt_session_id={};characteristic_id={};value={}",
                connection_id.value,
                gatt_session_id.value,
                characteristic_id.value,
                format_bytes(value)
            )),
            Some(ProjectedEventData {
                connection_id: Some(connection_id.value),
                gatt_session_id: Some(gatt_session_id.value),
                characteristic_id: Some(characteristic_id.value),
                value: Some(value.clone()),
                ..Default::default()
            }),
        ),
        BleEvent::CharacteristicWritten {
            command_id,
            connection_id,
            gatt_session_id,
            characteristic_id,
        } => projected(
            "characteristic_written",
            Some(command_id.value),
            Some(format!(
                "connection_id={};gatt_session_id={};characteristic_id={}",
                connection_id.value, gatt_session_id.value, characteristic_id.value
            )),
            Some(ProjectedEventData {
                connection_id: Some(connection_id.value),
                gatt_session_id: Some(gatt_session_id.value),
                characteristic_id: Some(characteristic_id.value),
                ..Default::default()
            }),
        ),
        BleEvent::NotificationStateChanged {
            command_id,
            connection_id,
            gatt_session_id,
            subscription_id,
            characteristic_id,
            state,
        } => projected(
            "notification_state_changed",
            Some(command_id.value),
            Some(format!(
                "connection_id={};gatt_session_id={};subscription_id={};characteristic_id={};state={state:?}",
                connection_id.value,
                gatt_session_id.value,
                subscription_id.value,
                characteristic_id.value
            )),
            Some(ProjectedEventData {
                connection_id: Some(connection_id.value),
                gatt_session_id: Some(gatt_session_id.value),
                subscription_id: Some(subscription_id.value),
                characteristic_id: Some(characteristic_id.value),
                state: Some(format!("{state:?}")),
                ..Default::default()
            }),
        ),
        BleEvent::NotificationReceived {
            connection_id,
            gatt_session_id,
            subscription_id,
            characteristic_id,
            value,
        } => projected(
            "notification_received",
            None,
            Some(format!(
                "connection_id={};gatt_session_id={};subscription_id={};characteristic_id={};value={}",
                connection_id.value,
                gatt_session_id.value,
                subscription_id.value,
                characteristic_id.value,
                format_bytes(value)
            )),
            Some(ProjectedEventData {
                connection_id: Some(connection_id.value),
                gatt_session_id: Some(gatt_session_id.value),
                subscription_id: Some(subscription_id.value),
                characteristic_id: Some(characteristic_id.value),
                value: Some(value.clone()),
                ..Default::default()
            }),
        ),
        BleEvent::RssiRead {
            command_id,
            connection_id,
            rssi,
        } => projected(
            "rssi_read",
            Some(command_id.value),
            Some(format!("connection_id={};rssi={rssi}", connection_id.value)),
            Some(ProjectedEventData {
                connection_id: Some(connection_id.value),
                rssi: Some(*rssi),
                ..Default::default()
            }),
        ),
        BleEvent::BackendError { error } => {
            projected("backend_error", None, Some(error.message.clone()), None)
        }
    }
}

fn format_bytes(value: &[u8]) -> String {
    value
        .iter()
        .map(|byte| format!("{byte:02X}"))
        .collect::<Vec<_>>()
        .join(" ")
}

fn format_adapter_snapshot(snapshot: &AdapterSnapshot) -> String {
    format!(
        "state={:?};scan_authorization={:?};connection_authorization={:?}",
        snapshot.state, snapshot.scan_authorization, snapshot.connection_authorization
    )
}

pub struct PluginState {
    hub: Arc<BleHub>,
    driver: Option<Arc<dyn CallbackDriver>>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct ProbeStep {
    pub name: &'static str,
    pub event_count: usize,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct BasicProbeResult {
    pub steps: Vec<ProbeStep>,
}

#[derive(Clone, Debug, Deserialize)]
pub struct StartScanRequest {
    pub command_id: u64,
    pub scan_session_id: u64,
    #[serde(default)]
    pub service_uuids: Vec<String>,
    pub name_prefix: Option<String>,
    pub company_id: Option<u16>,
    #[serde(default)]
    pub allow_duplicates: bool,
}

#[derive(Clone, Copy, Debug, Deserialize)]
pub struct StopScanRequest {
    pub command_id: u64,
    pub scan_session_id: u64,
}

#[derive(Clone, Copy, Debug, Deserialize)]
pub struct ConnectRequest {
    pub command_id: u64,
    pub connection_id: u64,
    pub peripheral_id: u64,
}

#[derive(Clone, Copy, Debug, Deserialize)]
pub struct DisconnectRequest {
    pub command_id: u64,
    pub connection_id: u64,
}

#[derive(Clone, Copy, Debug, Deserialize)]
pub struct DiscoverGattRequest {
    pub command_id: u64,
    pub connection_id: u64,
    pub gatt_session_id: u64,
}

#[derive(Clone, Copy, Debug, Deserialize)]
pub struct ReadCharacteristicRequest {
    pub command_id: u64,
    pub connection_id: u64,
    pub gatt_session_id: u64,
    pub characteristic_id: u64,
}

#[derive(Clone, Debug, Deserialize)]
pub struct WriteCharacteristicRequest {
    pub command_id: u64,
    pub connection_id: u64,
    pub gatt_session_id: u64,
    pub characteristic_id: u64,
    pub value: Vec<u8>,
    pub write_type: String,
}

#[derive(Clone, Debug, Deserialize)]
pub struct SetNotificationsRequest {
    pub command_id: u64,
    pub connection_id: u64,
    pub gatt_session_id: u64,
    pub subscription_id: u64,
    pub characteristic_id: u64,
    pub action: String,
    pub mode: Option<String>,
}

#[derive(Clone, Copy, Debug, Deserialize)]
pub struct ReadRssiRequest {
    pub command_id: u64,
    pub connection_id: u64,
}

fn command_error(error: blehub::CommandDispatchError) -> String {
    error.to_string()
}

fn write_type(value: &str) -> Result<WriteType, String> {
    match value {
        "with_response" => Ok(WriteType::WithResponse),
        "without_response" => Ok(WriteType::WithoutResponse),
        other => Err(format!("unsupported write_type: {other}")),
    }
}

fn notification_action(action: &str, mode: Option<&str>) -> Result<NotificationAction, String> {
    match action {
        "disable" => Ok(NotificationAction::Disable),
        "enable" => {
            let mode = match mode.unwrap_or("platform_default") {
                "platform_default" => NotificationModeRequest::PlatformDefault,
                "require_notification" => NotificationModeRequest::RequireNotification,
                "require_indication" => NotificationModeRequest::RequireIndication,
                other => return Err(format!("unsupported notification mode: {other}")),
            };
            Ok(NotificationAction::Enable { mode })
        }
        other => Err(format!("unsupported notification action: {other}")),
    }
}

fn dispatch_probe_step(
    state: &PluginState,
    name: &'static str,
    dispatch: Result<(), blehub::CommandDispatchError>,
    steps: &mut Vec<ProbeStep>,
) -> Result<(), String> {
    dispatch.map_err(|error| format!("{name}: {error}"))?;
    let event_count = state
        .driver
        .as_ref()
        .map_or(0, |driver| driver.advance_all());
    steps.push(ProbeStep { name, event_count });
    Ok(())
}

pub mod commands {
    use super::*;

    #[tauri::command]
    pub fn backend_mode() -> &'static str {
        if cfg!(target_os = "macos") {
            "apple-corebluetooth"
        } else {
            "deterministic-fake"
        }
    }

    #[tauri::command]
    pub fn initialize(state: State<'_, PluginState>, command_id: u64) -> Result<(), String> {
        state
            .hub
            .initialize(InitializeCommand {
                command_id: CommandId { value: command_id },
            })
            .map_err(|error| error.to_string())
    }

    #[tauri::command]
    pub fn start_scan(
        state: State<'_, PluginState>,
        request: StartScanRequest,
    ) -> Result<(), String> {
        state
            .hub
            .start_scan(StartScanCommand {
                command_id: CommandId { value: request.command_id },
                scan_session_id: ScanSessionId { value: request.scan_session_id },
                options: ScanOptions {
                    service_uuids: request
                        .service_uuids
                        .into_iter()
                        .map(|value| blehub::BluetoothUuid { value })
                        .collect(),
                    name_prefix: request.name_prefix,
                    company_id: request.company_id,
                    allow_duplicates: request.allow_duplicates,
                },
            })
            .map_err(command_error)
    }

    #[tauri::command]
    pub fn stop_scan(
        state: State<'_, PluginState>,
        request: StopScanRequest,
    ) -> Result<(), String> {
        state
            .hub
            .stop_scan(StopScanCommand {
                command_id: CommandId { value: request.command_id },
                scan_session_id: ScanSessionId { value: request.scan_session_id },
            })
            .map_err(command_error)
    }

    #[tauri::command]
    pub fn connect(state: State<'_, PluginState>, request: ConnectRequest) -> Result<(), String> {
        state
            .hub
            .connect(ConnectCommand {
                command_id: CommandId { value: request.command_id },
                connection_id: ConnectionId { value: request.connection_id },
                peripheral_id: PeripheralId { value: request.peripheral_id },
            })
            .map_err(command_error)
    }

    #[tauri::command]
    pub fn disconnect(
        state: State<'_, PluginState>,
        request: DisconnectRequest,
    ) -> Result<(), String> {
        state
            .hub
            .disconnect(DisconnectCommand {
                command_id: CommandId { value: request.command_id },
                connection_id: ConnectionId { value: request.connection_id },
            })
            .map_err(command_error)
    }

    #[tauri::command]
    pub fn discover_gatt(
        state: State<'_, PluginState>,
        request: DiscoverGattRequest,
    ) -> Result<(), String> {
        state
            .hub
            .discover_gatt(DiscoverGattCommand {
                command_id: CommandId { value: request.command_id },
                connection_id: ConnectionId { value: request.connection_id },
                gatt_session_id: GattSessionId { value: request.gatt_session_id },
            })
            .map_err(command_error)
    }

    #[tauri::command]
    pub fn read_characteristic(
        state: State<'_, PluginState>,
        request: ReadCharacteristicRequest,
    ) -> Result<(), String> {
        state
            .hub
            .read_characteristic(ReadCharacteristicCommand {
                command_id: CommandId { value: request.command_id },
                connection_id: ConnectionId { value: request.connection_id },
                gatt_session_id: GattSessionId { value: request.gatt_session_id },
                characteristic_id: GattCharacteristicId { value: request.characteristic_id },
            })
            .map_err(command_error)
    }

    #[tauri::command]
    pub fn write_characteristic(
        state: State<'_, PluginState>,
        request: WriteCharacteristicRequest,
    ) -> Result<(), String> {
        state
            .hub
            .write_characteristic(WriteCharacteristicCommand {
                command_id: CommandId { value: request.command_id },
                connection_id: ConnectionId { value: request.connection_id },
                gatt_session_id: GattSessionId { value: request.gatt_session_id },
                characteristic_id: GattCharacteristicId { value: request.characteristic_id },
                value: request.value,
                write_type: write_type(&request.write_type)?,
            })
            .map_err(command_error)
    }

    #[tauri::command]
    pub fn set_notifications(
        state: State<'_, PluginState>,
        request: SetNotificationsRequest,
    ) -> Result<(), String> {
        state
            .hub
            .set_notifications(SetNotificationsCommand {
                command_id: CommandId { value: request.command_id },
                connection_id: ConnectionId { value: request.connection_id },
                gatt_session_id: GattSessionId { value: request.gatt_session_id },
                subscription_id: SubscriptionId { value: request.subscription_id },
                characteristic_id: GattCharacteristicId { value: request.characteristic_id },
                action: notification_action(&request.action, request.mode.as_deref())?,
            })
            .map_err(command_error)
    }

    #[tauri::command]
    pub fn read_rssi(
        state: State<'_, PluginState>,
        request: ReadRssiRequest,
    ) -> Result<(), String> {
        state
            .hub
            .read_rssi(ReadRssiCommand {
                command_id: CommandId { value: request.command_id },
                connection_id: ConnectionId { value: request.connection_id },
            })
            .map_err(command_error)
    }

    #[tauri::command]
    pub fn advance_fake_callbacks(state: State<'_, PluginState>) -> usize {
        state
            .driver
            .as_ref()
            .map_or(0, |driver| driver.advance_all())
    }

    /// Spike-only convenience command. It exercises the complete basic
    /// contract through the real BleHub object; production integrations should
    /// expose their own caller-owned command model instead.
    #[tauri::command]
    pub fn run_basic_probe(state: State<'_, PluginState>) -> Result<BasicProbeResult, String> {
        let mut steps = Vec::new();
        let command = |value| CommandId { value };
        let connection_id = ConnectionId { value: 301 };
        let gatt_session_id = GattSessionId { value: 401 };
        let characteristic_id = GattCharacteristicId { value: 601 };
        let subscription_id = SubscriptionId { value: 801 };

        dispatch_probe_step(
            &state,
            "initialize",
            state.hub.initialize(InitializeCommand {
                command_id: command(1),
            }),
            &mut steps,
        )?;
        dispatch_probe_step(
            &state,
            "scan_start",
            state.hub.start_scan(StartScanCommand {
                command_id: command(2),
                scan_session_id: ScanSessionId { value: 101 },
                options: ScanOptions::default(),
            }),
            &mut steps,
        )?;
        dispatch_probe_step(
            &state,
            "scan_stop",
            state.hub.stop_scan(StopScanCommand {
                command_id: command(3),
                scan_session_id: ScanSessionId { value: 101 },
            }),
            &mut steps,
        )?;
        dispatch_probe_step(
            &state,
            "connect",
            state.hub.connect(ConnectCommand {
                command_id: command(4),
                connection_id,
                peripheral_id: PeripheralId { value: 201 },
            }),
            &mut steps,
        )?;
        dispatch_probe_step(
            &state,
            "gatt_discovery",
            state.hub.discover_gatt(DiscoverGattCommand {
                command_id: command(5),
                connection_id,
                gatt_session_id,
            }),
            &mut steps,
        )?;
        dispatch_probe_step(
            &state,
            "read",
            state.hub.read_characteristic(ReadCharacteristicCommand {
                command_id: command(6),
                connection_id,
                gatt_session_id,
                characteristic_id,
            }),
            &mut steps,
        )?;
        dispatch_probe_step(
            &state,
            "write_with_response",
            state.hub.write_characteristic(WriteCharacteristicCommand {
                command_id: command(7),
                connection_id,
                gatt_session_id,
                characteristic_id,
                value: vec![0x10, 0x20, 0x30],
                write_type: WriteType::WithResponse,
            }),
            &mut steps,
        )?;
        dispatch_probe_step(
            &state,
            "write_without_response",
            state.hub.write_characteristic(WriteCharacteristicCommand {
                command_id: command(8),
                connection_id,
                gatt_session_id,
                characteristic_id,
                value: vec![0xab, 0xcd],
                write_type: WriteType::WithoutResponse,
            }),
            &mut steps,
        )?;
        dispatch_probe_step(
            &state,
            "notifications_enable",
            state.hub.set_notifications(SetNotificationsCommand {
                command_id: command(9),
                connection_id,
                gatt_session_id,
                subscription_id,
                characteristic_id,
                action: NotificationAction::Enable {
                    mode: NotificationModeRequest::PlatformDefault,
                },
            }),
            &mut steps,
        )?;
        dispatch_probe_step(
            &state,
            "rssi",
            state.hub.read_rssi(ReadRssiCommand {
                command_id: command(10),
                connection_id,
            }),
            &mut steps,
        )?;
        dispatch_probe_step(
            &state,
            "notifications_disable",
            state.hub.set_notifications(SetNotificationsCommand {
                command_id: command(11),
                connection_id,
                gatt_session_id,
                subscription_id,
                characteristic_id,
                action: NotificationAction::Disable,
            }),
            &mut steps,
        )?;
        dispatch_probe_step(
            &state,
            "disconnect",
            state.hub.disconnect(DisconnectCommand {
                command_id: command(12),
                connection_id,
            }),
            &mut steps,
        )?;

        Ok(BasicProbeResult { steps })
    }

    #[tauri::command]
    pub fn close(state: State<'_, PluginState>) -> Result<(), String> {
        state.hub.dispose().map_err(|error| error.to_string())
    }
}

pub fn init<R: Runtime>(
    backend: Arc<dyn BleBackend>,
    driver: Option<Arc<dyn CallbackDriver>>,
) -> TauriPlugin<R> {
    Builder::new("blehub")
        .invoke_handler(tauri::generate_handler![
            commands::backend_mode,
            commands::initialize,
            commands::start_scan,
            commands::stop_scan,
            commands::connect,
            commands::disconnect,
            commands::discover_gatt,
            commands::read_characteristic,
            commands::write_characteristic,
            commands::set_notifications,
            commands::read_rssi,
            commands::advance_fake_callbacks,
            commands::run_basic_probe,
            commands::close
        ])
        .setup(move |app, _api| {
            let (sender, receiver) = mpsc::sync_channel(128);
            let sink = Arc::new(ChannelSink {
                sender: Mutex::new(sender),
            });
            let hub = BleHub::new(backend, sink)
                .map_err(|error| -> Box<dyn std::error::Error> { Box::new(error) })?;

            spawn_event_pump(app.app_handle().clone(), receiver);
            app.manage(PluginState { hub, driver });
            Ok(())
        })
        .build()
}

/// Creates a Tauri plugin whose backend is assembled by a native integration
/// launcher through the one-shot Stable UniFFI attach surface.
pub fn init_with_assembly<R, F>(assemble: F) -> TauriPlugin<R>
where
    R: Runtime,
    F: Fn(u64) -> Result<(), String> + Send + Sync + 'static,
{
    let assemble = Arc::new(assemble);
    Builder::new("blehub")
        .invoke_handler(tauri::generate_handler![
            commands::backend_mode,
            commands::initialize,
            commands::start_scan,
            commands::stop_scan,
            commands::connect,
            commands::disconnect,
            commands::discover_gatt,
            commands::read_characteristic,
            commands::write_characteristic,
            commands::set_notifications,
            commands::read_rssi,
            commands::advance_fake_callbacks,
            commands::run_basic_probe,
            commands::close
        ])
        .setup(move |app, _api| {
            let (sender, receiver) = mpsc::sync_channel(128);
            let sink: Arc<dyn BleEventSink> = Arc::new(ChannelSink {
                sender: Mutex::new(sender),
            });
            let ticket = blehub::register_backend_assembly(sink);
            if let Err(detail) = assemble(ticket) {
                blehub::cancel_backend_assembly(ticket);
                return Err(Box::new(std::io::Error::other(detail))
                    as Box<dyn std::error::Error>);
            }
            let hub = match blehub::take_assembled_hub(ticket) {
                Ok(hub) => hub,
                Err(error) => {
                    blehub::cancel_backend_assembly(ticket);
                    return Err(Box::new(error) as Box<dyn std::error::Error>);
                }
            };

            spawn_event_pump(app.app_handle().clone(), receiver);
            app.manage(PluginState { hub, driver: None });
            Ok(())
        })
        .build()
}

fn spawn_event_pump<R: Runtime>(app: AppHandle<R>, receiver: mpsc::Receiver<BleEvent>) {
    std::thread::Builder::new()
        .name("blehub-tauri-event-pump".to_owned())
        .spawn(move || {
            while let Ok(event) = receiver.recv() {
                let projected = project_event(&event);
                if app.emit("blehub://event", projected).is_err() {
                    break;
                }
            }
        })
        .expect("event pump thread must start");
}

#[cfg(test)]
mod tests {
    use super::*;
    use blehub::{AdapterState, AuthorizationState};

    #[test]
    fn projection_keeps_command_identity_and_platform_detail() {
        let event = BleEvent::AdapterSnapshotChanged {
            command_id: Some(CommandId { value: 7 }),
            snapshot: AdapterSnapshot {
                state: AdapterState::PoweredOn,
                scan_authorization: AuthorizationState::Authorized,
                connection_authorization: AuthorizationState::Authorized,
                name: Some("spike".to_owned()),
            },
        };
        let projected = project_event(&event);
        assert_eq!(projected.kind, "adapter_snapshot_changed");
        assert_eq!(projected.command_id, Some(7));
        assert!(projected.detail.unwrap().contains("PoweredOn"));
    }

    #[test]
    fn command_wire_values_map_to_canonical_contract_enums() {
        assert_eq!(write_type("with_response"), Ok(WriteType::WithResponse));
        assert_eq!(
            write_type("without_response"),
            Ok(WriteType::WithoutResponse)
        );
        assert!(write_type("unknown").is_err());

        assert_eq!(
            notification_action("enable", Some("require_indication")),
            Ok(NotificationAction::Enable {
                mode: NotificationModeRequest::RequireIndication,
            })
        );
        assert_eq!(
            notification_action("enable", None),
            Ok(NotificationAction::Enable {
                mode: NotificationModeRequest::PlatformDefault,
            })
        );
        assert_eq!(notification_action("disable", None), Ok(NotificationAction::Disable));
        assert!(notification_action("enable", Some("unknown")).is_err());
    }

    #[test]
    fn sink_handoff_is_bounded_and_non_blocking() {
        let (sender, receiver) = mpsc::sync_channel(1);
        let sink = ChannelSink {
            sender: Mutex::new(sender),
        };
        let event = BleEvent::CommandCompleted {
            command_id: CommandId { value: 1 },
            error: None,
        };
        sink.on_event(event.clone()).unwrap();
        assert!(sink.on_event(event).is_err());
        assert!(receiver.try_recv().is_ok());
    }
}
