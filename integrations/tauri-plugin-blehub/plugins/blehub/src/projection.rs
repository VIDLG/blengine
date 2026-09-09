//! Plugin-owned JSON contract. Core DTOs deliberately do not depend on Serde.

use blehub::{
    AdapterState, AuthorizationState, BackendCapabilities, BleErrorCategory, BleEvent,
    BleEventError, ConnectionState, ConnectionStateResult, NotificationMode, NotificationState,
    Platform, PlatformErrorDetails, ScanStopReason, ScanStopResult,
};
use serde::{Deserialize, Deserializer, Serialize, Serializer};

/// JSON IDs are decimal strings, including values above JavaScript's safe integer range.
/// Input accepts ASCII digits only (leading zeroes are allowed); output is canonical decimal.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct WireId(pub u64);

impl Serialize for WireId {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.collect_str(&self.0)
    }
}

impl<'de> Deserialize<'de> for WireId {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let value = String::deserialize(deserializer)?;
        if value.is_empty() || !value.bytes().all(|byte| byte.is_ascii_digit()) {
            return Err(serde::de::Error::custom(
                "ID must be a non-empty ASCII decimal string",
            ));
        }
        value
            .parse::<u64>()
            .map(Self)
            .map_err(|_| serde::de::Error::custom("ID exceeds u64::MAX"))
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ProjectedPlatform {
    Windows,
    Android,
    Apple,
    Linux,
    Test,
}

impl From<Platform> for ProjectedPlatform {
    fn from(value: Platform) -> Self {
        match value {
            Platform::Windows => Self::Windows,
            Platform::Android => Self::Android,
            Platform::Apple => Self::Apple,
            Platform::Linux => Self::Linux,
            Platform::Test => Self::Test,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct ProjectedCapabilities {
    pub platform: ProjectedPlatform,
    pub contract_version: u32,
    pub supports_duplicate_scan_results: bool,
    pub supports_connectable_flag: bool,
    pub supports_read_rssi: bool,
    pub supports_required_notification: bool,
    pub supports_required_indication: bool,
    pub reports_actual_notification_mode: bool,
}

impl From<BackendCapabilities> for ProjectedCapabilities {
    fn from(value: BackendCapabilities) -> Self {
        let BackendCapabilities {
            platform,
            contract_version,
            supports_duplicate_scan_results,
            supports_connectable_flag,
            supports_read_rssi,
            supports_required_notification,
            supports_required_indication,
            reports_actual_notification_mode,
        } = value;
        Self {
            platform: platform.into(),
            contract_version,
            supports_duplicate_scan_results,
            supports_connectable_flag,
            supports_read_rssi,
            supports_required_notification,
            supports_required_indication,
            reports_actual_notification_mode,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ProjectedErrorCategory {
    Permission,
    AdapterUnavailable,
    InvalidState,
    NotFound,
    Timeout,
    Cancelled,
    Connection,
    Gatt,
    Protocol,
    NotSupported,
    Backend,
    Internal,
}

impl From<BleErrorCategory> for ProjectedErrorCategory {
    fn from(value: BleErrorCategory) -> Self {
        match value {
            BleErrorCategory::Permission => Self::Permission,
            BleErrorCategory::AdapterUnavailable => Self::AdapterUnavailable,
            BleErrorCategory::InvalidState => Self::InvalidState,
            BleErrorCategory::NotFound => Self::NotFound,
            BleErrorCategory::Timeout => Self::Timeout,
            BleErrorCategory::Cancelled => Self::Cancelled,
            BleErrorCategory::Connection => Self::Connection,
            BleErrorCategory::Gatt => Self::Gatt,
            BleErrorCategory::Protocol => Self::Protocol,
            BleErrorCategory::NotSupported => Self::NotSupported,
            BleErrorCategory::Backend => Self::Backend,
            BleErrorCategory::Internal => Self::Internal,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct ProjectedPlatformErrorDetails {
    pub platform: ProjectedPlatform,
    pub code: Option<String>,
    pub name: Option<String>,
    pub message: Option<String>,
}

impl From<&PlatformErrorDetails> for ProjectedPlatformErrorDetails {
    fn from(value: &PlatformErrorDetails) -> Self {
        let PlatformErrorDetails {
            platform,
            code,
            name,
            message,
        } = value;
        Self {
            platform: (*platform).into(),
            code: code.clone(),
            name: name.clone(),
            message: message.clone(),
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct ProjectedError {
    pub category: ProjectedErrorCategory,
    pub message: String,
    pub platform_details: Option<ProjectedPlatformErrorDetails>,
}

impl From<&BleEventError> for ProjectedError {
    fn from(value: &BleEventError) -> Self {
        let BleEventError {
            category,
            message,
            platform_details,
        } = value;
        Self {
            category: (*category).into(),
            message: message.clone(),
            platform_details: platform_details.as_ref().map(Into::into),
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ProjectedAdapterState {
    Unknown,
    Unavailable,
    PoweredOff,
    PoweredOn,
}

impl From<AdapterState> for ProjectedAdapterState {
    fn from(value: AdapterState) -> Self {
        match value {
            AdapterState::Unknown => Self::Unknown,
            AdapterState::Unavailable => Self::Unavailable,
            AdapterState::PoweredOff => Self::PoweredOff,
            AdapterState::PoweredOn => Self::PoweredOn,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ProjectedAuthorizationState {
    NotRequired,
    NotDetermined,
    Denied,
    Restricted,
    Authorized,
}

impl From<AuthorizationState> for ProjectedAuthorizationState {
    fn from(value: AuthorizationState) -> Self {
        match value {
            AuthorizationState::NotRequired => Self::NotRequired,
            AuthorizationState::NotDetermined => Self::NotDetermined,
            AuthorizationState::Denied => Self::Denied,
            AuthorizationState::Restricted => Self::Restricted,
            AuthorizationState::Authorized => Self::Authorized,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ProjectedConnectionState {
    Connecting,
    Connected,
    Disconnecting,
    Disconnected,
}

impl From<ConnectionState> for ProjectedConnectionState {
    fn from(value: ConnectionState) -> Self {
        match value {
            ConnectionState::Connecting => Self::Connecting,
            ConnectionState::Connected => Self::Connected,
            ConnectionState::Disconnecting => Self::Disconnecting,
            ConnectionState::Disconnected => Self::Disconnected,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ProjectedNotificationState {
    Enabled,
    Disabled,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ProjectedNotificationMode {
    Notification,
    Indication,
}

impl From<NotificationMode> for ProjectedNotificationMode {
    fn from(value: NotificationMode) -> Self {
        match value {
            NotificationMode::Notification => Self::Notification,
            NotificationMode::Indication => Self::Indication,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ProjectedScanStopReason {
    Requested,
    SystemSuspended,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct ProjectedManufacturerData {
    pub company_id: u16,
    pub payload: Vec<u8>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct ProjectedServiceData {
    pub service_uuid: String,
    pub payload: Vec<u8>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct ProjectedGattDescriptor {
    pub id: WireId,
    pub uuid: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct ProjectedGattCharacteristic {
    pub id: WireId,
    pub uuid: String,
    pub readable: bool,
    pub writable_with_response: bool,
    pub writable_without_response: bool,
    pub notifiable: bool,
    pub indicatable: bool,
    pub descriptors: Vec<ProjectedGattDescriptor>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct ProjectedGattService {
    pub id: WireId,
    pub uuid: String,
    pub primary: Option<bool>,
    pub characteristics: Vec<ProjectedGattCharacteristic>,
}

impl From<&blehub::GattService> for ProjectedGattService {
    fn from(value: &blehub::GattService) -> Self {
        let blehub::GattService {
            id,
            uuid,
            primary,
            characteristics,
        } = value;
        Self {
            id: WireId(id.value),
            uuid: uuid.value.clone(),
            primary: *primary,
            characteristics: characteristics
                .iter()
                .map(|value| {
                    let blehub::GattCharacteristic {
                        id,
                        uuid,
                        properties,
                        descriptors,
                    } = value;
                    let blehub::CharacteristicProperties {
                        readable,
                        writable_with_response,
                        writable_without_response,
                        notifiable,
                        indicatable,
                    } = properties;
                    ProjectedGattCharacteristic {
                        id: WireId(id.value),
                        uuid: uuid.value.clone(),
                        readable: *readable,
                        writable_with_response: *writable_with_response,
                        writable_without_response: *writable_without_response,
                        notifiable: *notifiable,
                        indicatable: *indicatable,
                        descriptors: descriptors
                            .iter()
                            .map(|value| {
                                let blehub::GattDescriptor { id, uuid } = value;
                                ProjectedGattDescriptor {
                                    id: WireId(id.value),
                                    uuid: uuid.value.clone(),
                                }
                            })
                            .collect(),
                    }
                })
                .collect(),
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ProjectedEventKind {
    AdapterSnapshotChanged,
    CommandCompleted,
    ScanStarted,
    AdvertisementObserved,
    ScanStopped,
    ConnectionStateChanged,
    GattDiscovered,
    CharacteristicRead,
    CharacteristicWritten,
    NotificationStateChanged,
    NotificationReceived,
    RssiRead,
    BackendError,
}

/// The envelope's `kind` selects the data shape; no extra JSON variant tag is emitted.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
#[serde(untagged)]
pub enum ProjectedEventData {
    AdapterSnapshot {
        state: ProjectedAdapterState,
        scan_authorization: ProjectedAuthorizationState,
        connection_authorization: ProjectedAuthorizationState,
        name: Option<String>,
    },
    ScanStarted {
        scan_session_id: WireId,
    },
    Advertisement {
        peripheral_id: WireId,
        scan_session_id: WireId,
        local_name: Option<String>,
        advertised_services: Vec<String>,
        manufacturer_data: Vec<ProjectedManufacturerData>,
        service_data: Vec<ProjectedServiceData>,
        rssi: Option<i16>,
        connectable: Option<bool>,
    },
    ScanStopped {
        scan_session_id: WireId,
        reason: Option<ProjectedScanStopReason>,
        request_command_id: Option<WireId>,
    },
    Connection {
        connection_id: WireId,
        state: Option<ProjectedConnectionState>,
    },
    Gatt {
        connection_id: WireId,
        gatt_session_id: WireId,
        services: Vec<ProjectedGattService>,
    },
    CharacteristicRead {
        connection_id: WireId,
        gatt_session_id: WireId,
        characteristic_id: WireId,
        value: Vec<u8>,
    },
    CharacteristicWritten {
        connection_id: WireId,
        gatt_session_id: WireId,
        characteristic_id: WireId,
    },
    NotificationState {
        connection_id: WireId,
        gatt_session_id: WireId,
        subscription_id: WireId,
        characteristic_id: WireId,
        state: ProjectedNotificationState,
        actual_mode: Option<ProjectedNotificationMode>,
    },
    NotificationValue {
        connection_id: WireId,
        gatt_session_id: WireId,
        subscription_id: WireId,
        characteristic_id: WireId,
        value: Vec<u8>,
    },
    Rssi {
        connection_id: WireId,
        rssi: i16,
    },
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct ProjectedEvent {
    pub kind: ProjectedEventKind,
    pub command_id: Option<WireId>,
    /// Display-only text, never a source of machine-readable state or error identity.
    pub detail: Option<String>,
    pub data: Option<ProjectedEventData>,
    pub error: Option<ProjectedError>,
}

fn projected(
    kind: ProjectedEventKind,
    command_id: Option<blehub::CommandId>,
    data: Option<ProjectedEventData>,
    error: Option<&BleEventError>,
) -> ProjectedEvent {
    ProjectedEvent {
        kind,
        command_id: command_id.map(|id| WireId(id.value)),
        detail: error.map(|error| error.message.clone()),
        data,
        error: error.map(Into::into),
    }
}

pub fn project_event(event: &BleEvent) -> ProjectedEvent {
    use ProjectedEventData as Data;
    use ProjectedEventKind as Kind;

    match event {
        BleEvent::AdapterSnapshotChanged {
            command_id,
            snapshot,
        } => {
            let blehub::AdapterSnapshot {
                state,
                scan_authorization,
                connection_authorization,
                name,
            } = snapshot;
            projected(
                Kind::AdapterSnapshotChanged,
                *command_id,
                Some(Data::AdapterSnapshot {
                    state: (*state).into(),
                    scan_authorization: (*scan_authorization).into(),
                    connection_authorization: (*connection_authorization).into(),
                    name: name.clone(),
                }),
                None,
            )
        }
        BleEvent::CommandCompleted { command_id, error } => projected(
            Kind::CommandCompleted,
            Some(*command_id),
            None,
            error.as_ref(),
        ),
        BleEvent::ScanStarted {
            command_id,
            scan_id,
        } => projected(
            Kind::ScanStarted,
            Some(*command_id),
            Some(Data::ScanStarted {
                scan_session_id: WireId(scan_id.value),
            }),
            None,
        ),
        BleEvent::AdvertisementObserved { advertisement } => {
            let blehub::AdvertisementObservation {
                scan_session_id,
                peripheral_id,
                local_name,
                advertised_services,
                manufacturer_data,
                service_data,
                rssi,
                connectable,
            } = advertisement;
            projected(
                Kind::AdvertisementObserved,
                None,
                Some(Data::Advertisement {
                    peripheral_id: WireId(peripheral_id.value),
                    scan_session_id: WireId(scan_session_id.value),
                    local_name: local_name.clone(),
                    advertised_services: advertised_services
                        .iter()
                        .map(|uuid| uuid.value.clone())
                        .collect(),
                    manufacturer_data: manufacturer_data
                        .iter()
                        .map(|value| {
                            let blehub::ManufacturerData {
                                company_id,
                                payload,
                            } = value;
                            ProjectedManufacturerData {
                                company_id: *company_id,
                                payload: payload.clone(),
                            }
                        })
                        .collect(),
                    service_data: service_data
                        .iter()
                        .map(|value| {
                            let blehub::ServiceData {
                                service_uuid,
                                payload,
                            } = value;
                            ProjectedServiceData {
                                service_uuid: service_uuid.value.clone(),
                                payload: payload.clone(),
                            }
                        })
                        .collect(),
                    rssi: *rssi,
                    connectable: *connectable,
                }),
                None,
            )
        }
        BleEvent::ScanStopped { scan_id, result } => {
            let (reason, request_command_id, error) = match result {
                ScanStopResult::Ok {
                    reason: ScanStopReason::Requested { command_id },
                } => (
                    Some(ProjectedScanStopReason::Requested),
                    Some(*command_id),
                    None,
                ),
                ScanStopResult::Ok {
                    reason: ScanStopReason::SystemSuspended,
                } => (Some(ProjectedScanStopReason::SystemSuspended), None, None),
                ScanStopResult::Err { error } => (None, None, Some(error)),
            };
            projected(
                Kind::ScanStopped,
                request_command_id,
                Some(Data::ScanStopped {
                    scan_session_id: WireId(scan_id.value),
                    reason,
                    request_command_id: request_command_id.map(|id| WireId(id.value)),
                }),
                error,
            )
        }
        BleEvent::ConnectionStateChanged {
            command_id,
            connection_id,
            result,
        } => {
            let (state, error) = match result {
                ConnectionStateResult::Ok { state } => (Some((*state).into()), None),
                // A failed transition supplies no state; do not invent "disconnected".
                ConnectionStateResult::Err { error } => (None, Some(error)),
            };
            projected(
                Kind::ConnectionStateChanged,
                *command_id,
                Some(Data::Connection {
                    connection_id: WireId(connection_id.value),
                    state,
                }),
                error,
            )
        }
        BleEvent::GattDiscovered {
            command_id,
            database,
        } => {
            let blehub::GattDatabase {
                connection_id,
                gatt_session_id,
                services,
            } = database;
            projected(
                Kind::GattDiscovered,
                Some(*command_id),
                Some(Data::Gatt {
                    connection_id: WireId(connection_id.value),
                    gatt_session_id: WireId(gatt_session_id.value),
                    services: services.iter().map(Into::into).collect(),
                }),
                None,
            )
        }
        BleEvent::CharacteristicRead {
            command_id,
            connection_id,
            gatt_session_id,
            characteristic_id,
            value,
        } => projected(
            Kind::CharacteristicRead,
            Some(*command_id),
            Some(Data::CharacteristicRead {
                connection_id: WireId(connection_id.value),
                gatt_session_id: WireId(gatt_session_id.value),
                characteristic_id: WireId(characteristic_id.value),
                value: value.clone(),
            }),
            None,
        ),
        BleEvent::CharacteristicWritten {
            command_id,
            connection_id,
            gatt_session_id,
            characteristic_id,
        } => projected(
            Kind::CharacteristicWritten,
            Some(*command_id),
            Some(Data::CharacteristicWritten {
                connection_id: WireId(connection_id.value),
                gatt_session_id: WireId(gatt_session_id.value),
                characteristic_id: WireId(characteristic_id.value),
            }),
            None,
        ),
        BleEvent::NotificationStateChanged {
            command_id,
            connection_id,
            gatt_session_id,
            subscription_id,
            characteristic_id,
            state,
        } => {
            let (state, actual_mode) = match state {
                NotificationState::Enabled { actual_mode } => (
                    ProjectedNotificationState::Enabled,
                    actual_mode.map(Into::into),
                ),
                NotificationState::Disabled => (ProjectedNotificationState::Disabled, None),
            };
            projected(
                Kind::NotificationStateChanged,
                Some(*command_id),
                Some(Data::NotificationState {
                    connection_id: WireId(connection_id.value),
                    gatt_session_id: WireId(gatt_session_id.value),
                    subscription_id: WireId(subscription_id.value),
                    characteristic_id: WireId(characteristic_id.value),
                    state,
                    actual_mode,
                }),
                None,
            )
        }
        BleEvent::NotificationReceived {
            connection_id,
            gatt_session_id,
            subscription_id,
            characteristic_id,
            value,
        } => projected(
            Kind::NotificationReceived,
            None,
            Some(Data::NotificationValue {
                connection_id: WireId(connection_id.value),
                gatt_session_id: WireId(gatt_session_id.value),
                subscription_id: WireId(subscription_id.value),
                characteristic_id: WireId(characteristic_id.value),
                value: value.clone(),
            }),
            None,
        ),
        BleEvent::RssiRead {
            command_id,
            connection_id,
            rssi,
        } => projected(
            Kind::RssiRead,
            Some(*command_id),
            Some(Data::Rssi {
                connection_id: WireId(connection_id.value),
                rssi: *rssi,
            }),
            None,
        ),
        BleEvent::BackendError { error } => projected(Kind::BackendError, None, None, Some(error)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use blehub::*;
    use serde_json::{Value, json, to_value};

    const MAX: u64 = u64::MAX;
    const MAX_TEXT: &str = "18446744073709551615";

    fn json_event(event: BleEvent) -> Value {
        to_value(project_event(&event)).unwrap()
    }

    fn uuid(value: &str) -> BluetoothUuid {
        BluetoothUuid {
            value: value.to_owned(),
        }
    }

    fn error() -> BleEventError {
        BleEventError {
            category: BleErrorCategory::Gatt,
            message: "operation failed".into(),
            platform_details: Some(PlatformErrorDetails {
                platform: Platform::Apple,
                code: Some("18446744073709551615".into()),
                name: Some("CBATTErrorDomain".into()),
                message: Some("native detail".into()),
            }),
        }
    }

    #[test]
    fn wire_ids_are_lossless_strings_and_reject_non_decimal_or_overflow() {
        for value in [
            0,
            1,
            9_007_199_254_740_991,
            9_007_199_254_740_992,
            9_007_199_254_740_993,
            MAX,
        ] {
            let encoded = to_value(WireId(value)).unwrap();
            assert_eq!(encoded, json!(value.to_string()));
            assert_eq!(serde_json::from_value::<WireId>(encoded).unwrap().0, value);
        }
        assert_eq!(
            serde_json::from_value::<WireId>(json!("0001")).unwrap(),
            WireId(1)
        );
        for value in [
            json!(""),
            json!("+1"),
            json!("-1"),
            json!(" 1"),
            json!("1 "),
            json!("1\n"),
            json!("1.0"),
            json!("1e3"),
            json!("0x10"),
            json!("1_000"),
            json!("１２"),
            json!("18446744073709551616"),
            json!(0),
            json!(MAX),
            json!(1.0),
            Value::Null,
            json!(true),
            json!([]),
            json!({}),
        ] {
            assert!(
                serde_json::from_value::<WireId>(value.clone()).is_err(),
                "{value}"
            );
        }
    }

    #[test]
    fn adapter_and_capabilities_have_complete_stable_fields() {
        assert_eq!(
            json_event(BleEvent::AdapterSnapshotChanged {
                command_id: Some(CommandId { value: MAX }),
                snapshot: AdapterSnapshot {
                    state: AdapterState::PoweredOn,
                    scan_authorization: AuthorizationState::Authorized,
                    connection_authorization: AuthorizationState::NotDetermined,
                    name: Some("Local adapter".into()),
                },
            }),
            json!({
                "kind": "adapter_snapshot_changed", "command_id": MAX_TEXT,
                "detail": null, "error": null,
                "data": { "state": "powered_on", "scan_authorization": "authorized",
                    "connection_authorization": "not_determined", "name": "Local adapter" }
            })
        );
        for (platform, expected) in [
            (Platform::Windows, "windows"),
            (Platform::Android, "android"),
            (Platform::Apple, "apple"),
            (Platform::Linux, "linux"),
            (Platform::Test, "test"),
        ] {
            for flag in [false, true] {
                let capabilities = BackendCapabilities {
                    platform,
                    contract_version: BLEHUB_CONTRACT_VERSION,
                    supports_duplicate_scan_results: flag,
                    supports_connectable_flag: !flag,
                    supports_read_rssi: flag,
                    supports_required_notification: !flag,
                    supports_required_indication: flag,
                    reports_actual_notification_mode: !flag,
                };
                assert_eq!(
                    to_value(ProjectedCapabilities::from(capabilities)).unwrap(),
                    json!({
                        "platform": expected, "contract_version": BLEHUB_CONTRACT_VERSION,
                        "supports_duplicate_scan_results": flag, "supports_connectable_flag": !flag,
                        "supports_read_rssi": flag, "supports_required_notification": !flag,
                        "supports_required_indication": flag, "reports_actual_notification_mode": !flag
                    })
                );
            }
        }
    }

    #[test]
    fn advertisement_retains_all_entries_bytes_and_unknown_fields() {
        let value = json_event(BleEvent::AdvertisementObserved {
            advertisement: AdvertisementObservation {
                scan_session_id: ScanSessionId { value: MAX },
                peripheral_id: PeripheralId { value: MAX - 1 },
                local_name: None,
                advertised_services: vec![uuid("180d"), uuid("180d")],
                manufacturer_data: vec![
                    ManufacturerData {
                        company_id: 65535,
                        payload: vec![0, 128, 255],
                    },
                    ManufacturerData {
                        company_id: 65535,
                        payload: vec![],
                    },
                ],
                service_data: vec![
                    ServiceData {
                        service_uuid: uuid("180d"),
                        payload: vec![255, 0],
                    },
                    ServiceData {
                        service_uuid: uuid("180d"),
                        payload: vec![],
                    },
                ],
                rssi: Some(-127),
                connectable: None,
            },
        });
        assert_eq!(
            value,
            json!({
                "kind": "advertisement_observed", "command_id": null, "detail": null, "error": null,
                "data": {
                    "scan_session_id": MAX_TEXT, "peripheral_id": "18446744073709551614",
                    "local_name": null, "advertised_services": ["180d", "180d"],
                    "manufacturer_data": [
                        { "company_id": 65535, "payload": [0, 128, 255] },
                        { "company_id": 65535, "payload": [] }
                    ],
                    "service_data": [
                        { "service_uuid": "180d", "payload": [255, 0] },
                        { "service_uuid": "180d", "payload": [] }
                    ],
                    "rssi": -127, "connectable": null
                }
            })
        );
    }

    #[test]
    fn gatt_preserves_duplicate_uuid_instances_properties_and_descriptors() {
        let service = GattService {
            id: GattServiceId { value: MAX },
            uuid: uuid("180d"),
            primary: None,
            characteristics: vec![GattCharacteristic {
                id: GattCharacteristicId { value: MAX - 1 },
                uuid: uuid("2a37"),
                properties: CharacteristicProperties {
                    readable: true,
                    writable_with_response: false,
                    writable_without_response: true,
                    notifiable: false,
                    indicatable: true,
                },
                descriptors: vec![
                    GattDescriptor {
                        id: GattDescriptorId { value: MAX - 2 },
                        uuid: uuid("2902"),
                    },
                    GattDescriptor {
                        id: GattDescriptorId { value: MAX - 3 },
                        uuid: uuid("2902"),
                    },
                ],
            }],
        };
        let mut second = service.clone();
        second.id.value = 7;
        second.characteristics.clear();
        let value = json_event(BleEvent::GattDiscovered {
            command_id: CommandId { value: MAX },
            database: GattDatabase {
                connection_id: ConnectionId { value: MAX },
                gatt_session_id: GattSessionId { value: MAX },
                services: vec![service, second],
            },
        });
        assert_eq!(
            value,
            json!({
                "kind": "gatt_discovered", "command_id": MAX_TEXT, "detail": null, "error": null,
                "data": { "connection_id": MAX_TEXT, "gatt_session_id": MAX_TEXT, "services": [
                    { "id": MAX_TEXT, "uuid": "180d", "primary": null, "characteristics": [
                        { "id": "18446744073709551614", "uuid": "2a37", "readable": true,
                            "writable_with_response": false, "writable_without_response": true,
                            "notifiable": false, "indicatable": true, "descriptors": [
                                { "id": "18446744073709551613", "uuid": "2902" },
                                { "id": "18446744073709551612", "uuid": "2902" }
                            ] }
                    ] },
                    { "id": "7", "uuid": "180d", "primary": null, "characteristics": [] }
                ] }
            })
        );
    }

    #[test]
    fn scan_stop_and_all_error_events_keep_structured_failure_and_correlation() {
        let requested = json_event(BleEvent::ScanStopped {
            scan_id: ScanSessionId { value: MAX },
            result: ScanStopResult::Ok {
                reason: ScanStopReason::Requested {
                    command_id: CommandId { value: MAX },
                },
            },
        });
        assert_eq!(
            requested,
            json!({
                "kind": "scan_stopped", "command_id": MAX_TEXT, "detail": null, "error": null,
                "data": { "scan_session_id": MAX_TEXT, "reason": "requested", "request_command_id": MAX_TEXT }
            })
        );
        let suspended = json_event(BleEvent::ScanStopped {
            scan_id: ScanSessionId { value: MAX },
            result: ScanStopResult::Ok {
                reason: ScanStopReason::SystemSuspended,
            },
        });
        assert_eq!(
            suspended["data"],
            json!({
                "scan_session_id": MAX_TEXT, "reason": "system_suspended", "request_command_id": null
            })
        );
        assert!(suspended["command_id"].is_null());
        assert!(suspended["error"].is_null());
        let expected_error = json!({
            "category": "gatt", "message": "operation failed",
            "platform_details": { "platform": "apple", "code": MAX_TEXT,
                "name": "CBATTErrorDomain", "message": "native detail" }
        });
        for (event, kind, command_id, data) in [
            (
                BleEvent::ScanStopped {
                    scan_id: ScanSessionId { value: MAX },
                    result: ScanStopResult::Err { error: error() },
                },
                "scan_stopped",
                Value::Null,
                json!({
                    "scan_session_id": MAX_TEXT, "reason": null, "request_command_id": null
                }),
            ),
            (
                BleEvent::ConnectionStateChanged {
                    command_id: Some(CommandId { value: MAX }),
                    connection_id: ConnectionId { value: MAX },
                    result: ConnectionStateResult::Err { error: error() },
                },
                "connection_state_changed",
                json!(MAX_TEXT),
                json!({ "connection_id": MAX_TEXT, "state": null }),
            ),
            (
                BleEvent::CommandCompleted {
                    command_id: CommandId { value: MAX },
                    error: Some(error()),
                },
                "command_completed",
                json!(MAX_TEXT),
                Value::Null,
            ),
            (
                BleEvent::BackendError { error: error() },
                "backend_error",
                Value::Null,
                Value::Null,
            ),
        ] {
            assert_eq!(
                json_event(event),
                json!({
                    "kind": kind, "command_id": command_id, "data": data,
                    "detail": "operation failed", "error": expected_error
                })
            );
        }
        for (category, expected) in [
            (BleErrorCategory::Permission, "permission"),
            (BleErrorCategory::AdapterUnavailable, "adapter_unavailable"),
            (BleErrorCategory::InvalidState, "invalid_state"),
            (BleErrorCategory::NotFound, "not_found"),
            (BleErrorCategory::Timeout, "timeout"),
            (BleErrorCategory::Cancelled, "cancelled"),
            (BleErrorCategory::Connection, "connection"),
            (BleErrorCategory::Gatt, "gatt"),
            (BleErrorCategory::Protocol, "protocol"),
            (BleErrorCategory::NotSupported, "not_supported"),
            (BleErrorCategory::Backend, "backend"),
            (BleErrorCategory::Internal, "internal"),
        ] {
            let value = json_event(BleEvent::BackendError {
                error: BleEventError {
                    category,
                    message: "failure".into(),
                    platform_details: None,
                },
            });
            assert_eq!(
                value["error"],
                json!({ "category": expected, "message": "failure", "platform_details": null })
            );
        }
    }

    #[test]
    fn connection_and_notification_states_are_stable_and_modes_are_separate() {
        for (state, expected) in [
            (ConnectionState::Connecting, "connecting"),
            (ConnectionState::Connected, "connected"),
            (ConnectionState::Disconnecting, "disconnecting"),
            (ConnectionState::Disconnected, "disconnected"),
        ] {
            let value = json_event(BleEvent::ConnectionStateChanged {
                command_id: None,
                connection_id: ConnectionId { value: MAX },
                result: ConnectionStateResult::Ok { state },
            });
            assert_eq!(
                value["data"],
                json!({ "connection_id": MAX_TEXT, "state": expected })
            );
            assert!(value["command_id"].is_null());
            assert!(value["error"].is_null());
        }
        for (state, expected, mode) in [
            (
                NotificationState::Enabled {
                    actual_mode: Some(NotificationMode::Notification),
                },
                "enabled",
                json!("notification"),
            ),
            (
                NotificationState::Enabled {
                    actual_mode: Some(NotificationMode::Indication),
                },
                "enabled",
                json!("indication"),
            ),
            (
                NotificationState::Enabled { actual_mode: None },
                "enabled",
                Value::Null,
            ),
            (NotificationState::Disabled, "disabled", Value::Null),
        ] {
            assert_eq!(
                json_event(BleEvent::NotificationStateChanged {
                    command_id: CommandId { value: MAX },
                    connection_id: ConnectionId { value: MAX },
                    gatt_session_id: GattSessionId { value: MAX },
                    subscription_id: SubscriptionId { value: MAX },
                    characteristic_id: GattCharacteristicId { value: MAX },
                    state,
                }),
                json!({
                    "kind": "notification_state_changed", "command_id": MAX_TEXT, "detail": null, "error": null,
                    "data": { "connection_id": MAX_TEXT, "gatt_session_id": MAX_TEXT,
                        "subscription_id": MAX_TEXT, "characteristic_id": MAX_TEXT, "state": expected, "actual_mode": mode }
                })
            );
        }
    }

    #[test]
    fn remaining_event_shapes_preserve_ids_values_and_terminal_nulls() {
        let command_id = CommandId { value: MAX };
        let connection_id = ConnectionId { value: MAX };
        let gatt_session_id = GattSessionId { value: MAX };
        let characteristic_id = GattCharacteristicId { value: MAX };
        for (event, kind, id, data) in [
            (
                BleEvent::CommandCompleted {
                    command_id,
                    error: None,
                },
                "command_completed",
                json!(MAX_TEXT),
                Value::Null,
            ),
            (
                BleEvent::ScanStarted {
                    command_id,
                    scan_id: ScanSessionId { value: MAX },
                },
                "scan_started",
                json!(MAX_TEXT),
                json!({ "scan_session_id": MAX_TEXT }),
            ),
            (
                BleEvent::CharacteristicRead {
                    command_id,
                    connection_id,
                    gatt_session_id,
                    characteristic_id,
                    value: vec![0, 255],
                },
                "characteristic_read",
                json!(MAX_TEXT),
                json!({ "connection_id": MAX_TEXT,
                    "gatt_session_id": MAX_TEXT, "characteristic_id": MAX_TEXT, "value": [0, 255] }),
            ),
            (
                BleEvent::CharacteristicWritten {
                    command_id,
                    connection_id,
                    gatt_session_id,
                    characteristic_id,
                },
                "characteristic_written",
                json!(MAX_TEXT),
                json!({ "connection_id": MAX_TEXT,
                    "gatt_session_id": MAX_TEXT, "characteristic_id": MAX_TEXT }),
            ),
            (
                BleEvent::NotificationReceived {
                    connection_id,
                    gatt_session_id,
                    subscription_id: SubscriptionId { value: MAX },
                    characteristic_id,
                    value: vec![255, 0],
                },
                "notification_received",
                Value::Null,
                json!({ "connection_id": MAX_TEXT, "gatt_session_id": MAX_TEXT, "subscription_id": MAX_TEXT,
                    "characteristic_id": MAX_TEXT, "value": [255, 0] }),
            ),
            (
                BleEvent::RssiRead {
                    command_id,
                    connection_id,
                    rssi: -42,
                },
                "rssi_read",
                json!(MAX_TEXT),
                json!({ "connection_id": MAX_TEXT, "rssi": -42 }),
            ),
        ] {
            assert_eq!(
                json_event(event),
                json!({
                    "kind": kind, "command_id": id, "detail": null, "data": data, "error": null
                })
            );
        }
    }
}
