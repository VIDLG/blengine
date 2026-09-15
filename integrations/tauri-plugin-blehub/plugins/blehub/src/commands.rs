//! Tauri dispatch boundary. Successful dispatch is acceptance, not BLE completion.

use blehub::{
    BluetoothUuid, CommandDispatchError, CommandId, ConnectCommand, ConnectionId,
    DisconnectCommand, DiscoverGattCommand, GattCharacteristicId, GattSessionId, InitializeCommand,
    NotificationAction, NotificationModeRequest, PeripheralId, ReadCharacteristicCommand,
    ReadRssiCommand, ScanOptions, ScanSessionId, SetNotificationsCommand, StartScanCommand,
    StopScanCommand, SubscriptionId, WriteCharacteristicCommand, WriteType,
};
use serde::{Deserialize, Serialize};
use tauri::State;

use super::projection::{ProjectedCapabilities, ProjectedPlatformErrorDetails, WireId};

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum CommandErrorCategory {
    Closed,
    InvalidRequest,
    InvalidState,
    BackendUnavailable,
    FfiContract,
    CloseFailed,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct CommandError {
    pub category: CommandErrorCategory,
    pub message: String,
    pub detail: Option<String>,
    pub platform_details: Option<Box<ProjectedPlatformErrorDetails>>,
}

impl From<CommandDispatchError> for CommandError {
    fn from(error: CommandDispatchError) -> Self {
        let message = error.to_string();
        let (category, detail, platform_details) = match error {
            CommandDispatchError::Closed => (CommandErrorCategory::Closed, None, None),
            CommandDispatchError::InvalidRequest { detail } => {
                (CommandErrorCategory::InvalidRequest, Some(detail), None)
            }
            CommandDispatchError::InvalidState { detail } => {
                (CommandErrorCategory::InvalidState, Some(detail), None)
            }
            CommandDispatchError::BackendUnavailable {
                detail,
                platform_details,
            } => (
                CommandErrorCategory::BackendUnavailable,
                Some(detail),
                platform_details
                    .as_ref()
                    .map(|details| Box::new(details.into())),
            ),
            CommandDispatchError::FfiContract { detail } => {
                (CommandErrorCategory::FfiContract, Some(detail), None)
            }
        };
        Self {
            category,
            message,
            detail,
            platform_details,
        }
    }
}

impl CommandError {
    fn invalid_request(detail: String) -> Self {
        CommandDispatchError::InvalidRequest { detail }.into()
    }

    fn close_failed(detail: String) -> Self {
        // The parent's close API returns text, not a core error. Do not infer
        // a native category or platform details by parsing that text.
        Self {
            category: CommandErrorCategory::CloseFailed,
            message: detail.clone(),
            detail: Some(detail),
            platform_details: None,
        }
    }
}

#[derive(Clone, Debug, Deserialize)]
pub struct StartScanRequest {
    pub command_id: WireId,
    pub scan_session_id: WireId,
    #[serde(default)]
    pub service_uuids: Vec<String>,
    pub name_prefix: Option<String>,
    pub company_id: Option<u16>,
    #[serde(default)]
    pub allow_duplicates: bool,
}

#[derive(Clone, Copy, Debug, Deserialize)]
pub struct StopScanRequest {
    pub command_id: WireId,
    pub scan_session_id: WireId,
}

#[derive(Clone, Copy, Debug, Deserialize)]
pub struct ConnectRequest {
    pub command_id: WireId,
    pub connection_id: WireId,
    pub peripheral_id: WireId,
}

#[derive(Clone, Copy, Debug, Deserialize)]
pub struct DisconnectRequest {
    pub command_id: WireId,
    pub connection_id: WireId,
}

#[derive(Clone, Copy, Debug, Deserialize)]
pub struct DiscoverGattRequest {
    pub command_id: WireId,
    pub connection_id: WireId,
    pub gatt_session_id: WireId,
}

#[derive(Clone, Copy, Debug, Deserialize)]
pub struct ReadCharacteristicRequest {
    pub command_id: WireId,
    pub connection_id: WireId,
    pub gatt_session_id: WireId,
    pub characteristic_id: WireId,
}

#[derive(Clone, Debug, Deserialize)]
pub struct WriteCharacteristicRequest {
    pub command_id: WireId,
    pub connection_id: WireId,
    pub gatt_session_id: WireId,
    pub characteristic_id: WireId,
    pub value: Vec<u8>,
    pub write_type: String,
}

#[derive(Clone, Debug, Deserialize)]
pub struct SetNotificationsRequest {
    pub command_id: WireId,
    pub connection_id: WireId,
    pub gatt_session_id: WireId,
    pub subscription_id: WireId,
    pub characteristic_id: WireId,
    pub action: String,
    pub mode: Option<String>,
}

#[derive(Clone, Copy, Debug, Deserialize)]
pub struct ReadRssiRequest {
    pub command_id: WireId,
    pub connection_id: WireId,
}

// Validate these strings inside the command so unsupported choices return a
// structured CommandError rather than Tauri's argument-deserialization text.
fn write_type(value: &str) -> Result<WriteType, CommandError> {
    match value {
        "with_response" => Ok(WriteType::WithResponse),
        "without_response" => Ok(WriteType::WithoutResponse),
        other => Err(CommandError::invalid_request(format!(
            "unsupported write_type: {other}"
        ))),
    }
}

fn notification_action(
    action: &str,
    mode: Option<&str>,
) -> Result<NotificationAction, CommandError> {
    match action {
        // As in the old request contract, mode is irrelevant when disabling.
        "disable" => Ok(NotificationAction::Disable),
        "enable" => {
            let mode = match mode.unwrap_or("platform_default") {
                "platform_default" => NotificationModeRequest::PlatformDefault,
                "require_notification" => NotificationModeRequest::RequireNotification,
                "require_indication" => NotificationModeRequest::RequireIndication,
                other => {
                    return Err(CommandError::invalid_request(format!(
                        "unsupported notification mode: {other}"
                    )));
                }
            };
            Ok(NotificationAction::Enable { mode })
        }
        other => Err(CommandError::invalid_request(format!(
            "unsupported notification action: {other}"
        ))),
    }
}

#[tauri::command]
pub fn open_settings() -> Result<(), String> {
    #[cfg(all(any(target_os = "macos", target_os = "ios"), feature = "native"))]
    {
        return super::apple_assembly::open_settings();
    }
    #[cfg(not(all(feature = "native", any(target_os = "macos", target_os = "ios"))))]
    {
        Err("opening platform settings is unavailable on this target".to_owned())
    }
}

#[tauri::command]
pub fn capabilities(state: State<'_, super::PluginState>) -> ProjectedCapabilities {
    state.hub.capabilities().into()
}

/// Tauri exposes this top-level argument as `commandId`; nested request fields
/// on the other commands retain their existing snake_case names.
#[tauri::command(rename_all = "camelCase")]
pub fn initialize(
    state: State<'_, super::PluginState>,
    command_id: WireId,
) -> Result<(), CommandError> {
    state
        .hub
        .initialize(InitializeCommand {
            command_id: CommandId {
                value: command_id.0,
            },
        })
        .map_err(Into::into)
}

#[tauri::command]
pub fn start_scan(
    state: State<'_, super::PluginState>,
    request: StartScanRequest,
) -> Result<(), CommandError> {
    state
        .hub
        .start_scan(StartScanCommand {
            command_id: CommandId {
                value: request.command_id.0,
            },
            scan_session_id: ScanSessionId {
                value: request.scan_session_id.0,
            },
            options: ScanOptions {
                service_uuids: request
                    .service_uuids
                    .into_iter()
                    .map(|value| BluetoothUuid { value })
                    .collect(),
                name_prefix: request.name_prefix,
                company_id: request.company_id,
                allow_duplicates: request.allow_duplicates,
            },
        })
        .map_err(Into::into)
}

#[tauri::command]
pub fn stop_scan(
    state: State<'_, super::PluginState>,
    request: StopScanRequest,
) -> Result<(), CommandError> {
    state
        .hub
        .stop_scan(StopScanCommand {
            command_id: CommandId {
                value: request.command_id.0,
            },
            scan_session_id: ScanSessionId {
                value: request.scan_session_id.0,
            },
        })
        .map_err(Into::into)
}

#[tauri::command]
pub fn connect(
    state: State<'_, super::PluginState>,
    request: ConnectRequest,
) -> Result<(), CommandError> {
    state
        .hub
        .connect(ConnectCommand {
            command_id: CommandId {
                value: request.command_id.0,
            },
            connection_id: ConnectionId {
                value: request.connection_id.0,
            },
            peripheral_id: PeripheralId {
                value: request.peripheral_id.0,
            },
        })
        .map_err(Into::into)
}

#[tauri::command]
pub fn disconnect(
    state: State<'_, super::PluginState>,
    request: DisconnectRequest,
) -> Result<(), CommandError> {
    state
        .hub
        .disconnect(DisconnectCommand {
            command_id: CommandId {
                value: request.command_id.0,
            },
            connection_id: ConnectionId {
                value: request.connection_id.0,
            },
        })
        .map_err(Into::into)
}

#[tauri::command]
pub fn discover_gatt(
    state: State<'_, super::PluginState>,
    request: DiscoverGattRequest,
) -> Result<(), CommandError> {
    state
        .hub
        .discover_gatt(DiscoverGattCommand {
            command_id: CommandId {
                value: request.command_id.0,
            },
            connection_id: ConnectionId {
                value: request.connection_id.0,
            },
            gatt_session_id: GattSessionId {
                value: request.gatt_session_id.0,
            },
        })
        .map_err(Into::into)
}

#[tauri::command]
pub fn read_characteristic(
    state: State<'_, super::PluginState>,
    request: ReadCharacteristicRequest,
) -> Result<(), CommandError> {
    state
        .hub
        .read_characteristic(ReadCharacteristicCommand {
            command_id: CommandId {
                value: request.command_id.0,
            },
            connection_id: ConnectionId {
                value: request.connection_id.0,
            },
            gatt_session_id: GattSessionId {
                value: request.gatt_session_id.0,
            },
            characteristic_id: GattCharacteristicId {
                value: request.characteristic_id.0,
            },
        })
        .map_err(Into::into)
}

#[tauri::command]
pub fn write_characteristic(
    state: State<'_, super::PluginState>,
    request: WriteCharacteristicRequest,
) -> Result<(), CommandError> {
    let write_type = write_type(&request.write_type)?;
    state
        .hub
        .write_characteristic(WriteCharacteristicCommand {
            command_id: CommandId {
                value: request.command_id.0,
            },
            connection_id: ConnectionId {
                value: request.connection_id.0,
            },
            gatt_session_id: GattSessionId {
                value: request.gatt_session_id.0,
            },
            characteristic_id: GattCharacteristicId {
                value: request.characteristic_id.0,
            },
            value: request.value,
            write_type,
        })
        .map_err(Into::into)
}

#[tauri::command]
pub fn set_notifications(
    state: State<'_, super::PluginState>,
    request: SetNotificationsRequest,
) -> Result<(), CommandError> {
    let action = notification_action(&request.action, request.mode.as_deref())?;
    state
        .hub
        .set_notifications(SetNotificationsCommand {
            command_id: CommandId {
                value: request.command_id.0,
            },
            connection_id: ConnectionId {
                value: request.connection_id.0,
            },
            gatt_session_id: GattSessionId {
                value: request.gatt_session_id.0,
            },
            subscription_id: SubscriptionId {
                value: request.subscription_id.0,
            },
            characteristic_id: GattCharacteristicId {
                value: request.characteristic_id.0,
            },
            action,
        })
        .map_err(Into::into)
}

#[tauri::command]
pub fn read_rssi(
    state: State<'_, super::PluginState>,
    request: ReadRssiRequest,
) -> Result<(), CommandError> {
    state
        .hub
        .read_rssi(ReadRssiCommand {
            command_id: CommandId {
                value: request.command_id.0,
            },
            connection_id: ConnectionId {
                value: request.connection_id.0,
            },
        })
        .map_err(Into::into)
}

#[tauri::command]
pub async fn close(state: State<'_, super::PluginState>) -> Result<(), CommandError> {
    state.close().await.map_err(CommandError::close_failed)
}

#[cfg(test)]
mod tests {
    use super::*;
    use blehub::{Platform, PlatformErrorDetails};
    use serde::de::DeserializeOwned;
    use serde_json::{Value, json, to_value};

    #[test]
    fn dispatch_errors_keep_core_category_detail_and_native_fields() {
        for (error, category, detail, platform_details) in [
            (
                CommandDispatchError::Closed,
                "closed",
                Value::Null,
                Value::Null,
            ),
            (
                CommandDispatchError::InvalidRequest {
                    detail: "bad UUID".into(),
                },
                "invalid_request",
                json!("bad UUID"),
                Value::Null,
            ),
            (
                CommandDispatchError::InvalidState {
                    detail: "scan active".into(),
                },
                "invalid_state",
                json!("scan active"),
                Value::Null,
            ),
            (
                CommandDispatchError::BackendUnavailable {
                    detail: "adapter missing".into(),
                    platform_details: Some(PlatformErrorDetails {
                        platform: Platform::Windows,
                        code: Some("0x80070005".into()),
                        name: Some("E_ACCESSDENIED".into()),
                        message: Some("native message".into()),
                    }),
                },
                "backend_unavailable",
                json!("adapter missing"),
                json!({
                    "platform": "windows", "code": "0x80070005",
                    "name": "E_ACCESSDENIED", "message": "native message"
                }),
            ),
            (
                CommandDispatchError::BackendUnavailable {
                    detail: "no native details".into(),
                    platform_details: None,
                },
                "backend_unavailable",
                json!("no native details"),
                Value::Null,
            ),
            (
                CommandDispatchError::FfiContract {
                    detail: "foreign exception".into(),
                },
                "ffi_contract",
                json!("foreign exception"),
                Value::Null,
            ),
        ] {
            let message = error.to_string();
            assert_eq!(
                to_value(CommandError::from(error)).unwrap(),
                json!({
                    "category": category, "message": message, "detail": detail, "platform_details": platform_details
                })
            );
        }
        assert_eq!(
            to_value(CommandError::close_failed("cleanup text".into())).unwrap(),
            json!({
                "category": "close_failed", "message": "cleanup text", "detail": "cleanup text", "platform_details": null
            })
        );
    }

    #[test]
    fn command_choices_preserve_contract_modes_and_return_typed_rejections() {
        assert_eq!(
            write_type("with_response").unwrap(),
            WriteType::WithResponse
        );
        assert_eq!(
            write_type("without_response").unwrap(),
            WriteType::WithoutResponse
        );
        for (mode, expected) in [
            (None, NotificationModeRequest::PlatformDefault),
            (
                Some("platform_default"),
                NotificationModeRequest::PlatformDefault,
            ),
            (
                Some("require_notification"),
                NotificationModeRequest::RequireNotification,
            ),
            (
                Some("require_indication"),
                NotificationModeRequest::RequireIndication,
            ),
        ] {
            assert_eq!(
                notification_action("enable", mode).unwrap(),
                NotificationAction::Enable { mode: expected }
            );
        }
        assert_eq!(
            notification_action("disable", Some("ignored")).unwrap(),
            NotificationAction::Disable
        );
        for error in [
            write_type("WithResponse").unwrap_err(),
            notification_action("enable", Some("indication")).unwrap_err(),
            notification_action("toggle", None).unwrap_err(),
        ] {
            let value = to_value(error).unwrap();
            assert_eq!(value["category"], "invalid_request");
            assert!(
                value["detail"]
                    .as_str()
                    .unwrap()
                    .starts_with("unsupported ")
            );
            assert!(value["platform_details"].is_null());
        }
    }

    fn assert_request_ids<T: DeserializeOwned>(valid: Value, fields: &[&str]) {
        assert!(serde_json::from_value::<T>(valid.clone()).is_ok());
        for field in fields {
            for invalid in [
                json!(0),
                json!(u64::MAX),
                json!("18446744073709551616"),
                json!("-1"),
                json!("+1"),
            ] {
                let mut request = valid.clone();
                request[*field] = invalid;
                assert!(serde_json::from_value::<T>(request).is_err(), "{field}");
            }
            let mut request = valid.clone();
            request[*field] = json!("0");
            assert!(serde_json::from_value::<T>(request).is_ok(), "{field}");
        }
    }

    #[test]
    fn every_request_id_is_a_decimal_string_including_u64_boundaries() {
        let max = "18446744073709551615";
        assert_request_ids::<StartScanRequest>(
            json!({ "command_id": max, "scan_session_id": max }),
            &["command_id", "scan_session_id"],
        );
        assert_request_ids::<StopScanRequest>(
            json!({ "command_id": max, "scan_session_id": max }),
            &["command_id", "scan_session_id"],
        );
        assert_request_ids::<ConnectRequest>(
            json!({ "command_id": max, "connection_id": max, "peripheral_id": max }),
            &["command_id", "connection_id", "peripheral_id"],
        );
        assert_request_ids::<DisconnectRequest>(
            json!({ "command_id": max, "connection_id": max }),
            &["command_id", "connection_id"],
        );
        assert_request_ids::<DiscoverGattRequest>(
            json!({ "command_id": max, "connection_id": max, "gatt_session_id": max }),
            &["command_id", "connection_id", "gatt_session_id"],
        );
        assert_request_ids::<ReadCharacteristicRequest>(
            json!({ "command_id": max, "connection_id": max,
            "gatt_session_id": max, "characteristic_id": max }),
            &[
                "command_id",
                "connection_id",
                "gatt_session_id",
                "characteristic_id",
            ],
        );
        assert_request_ids::<WriteCharacteristicRequest>(
            json!({ "command_id": max, "connection_id": max,
            "gatt_session_id": max, "characteristic_id": max, "value": [0, 255], "write_type": "with_response" }),
            &[
                "command_id",
                "connection_id",
                "gatt_session_id",
                "characteristic_id",
            ],
        );
        assert_request_ids::<SetNotificationsRequest>(
            json!({ "command_id": max, "connection_id": max,
            "gatt_session_id": max, "characteristic_id": max, "subscription_id": max, "action": "enable" }),
            &[
                "command_id",
                "connection_id",
                "gatt_session_id",
                "characteristic_id",
                "subscription_id",
            ],
        );
        assert_request_ids::<ReadRssiRequest>(
            json!({ "command_id": max, "connection_id": max }),
            &["command_id", "connection_id"],
        );
    }
}
