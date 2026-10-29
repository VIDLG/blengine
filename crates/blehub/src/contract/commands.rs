use super::{
    AdvertisementObservation, BluetoothUuid, CommandDispatchError, CommandId, ConnectionId,
    GattCharacteristicId, GattSessionId, PeripheralId, ScanSessionId, SubscriptionId,
};

/// Bluetooth names are limited to 248 UTF-8 bytes, so a longer prefix cannot match.
pub const MAX_SCAN_NAME_PREFIX_BYTES: usize = 248;

/// ATT limits an attribute value to 512 octets; a platform's per-write limit may be lower.
pub const MAX_WRITE_VALUE_BYTES: usize = 512;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, uniffi::Enum)]
pub enum CommandKind {
    Initialize,
    StartScan,
    StopScan,
    Connect,
    Disconnect,
    DiscoverGatt,
    ReadCharacteristic,
    WriteCharacteristic,
    SetNotifications,
    ReadRssi,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, uniffi::Enum)]
pub enum WriteType {
    WithResponse,
    WithoutResponse,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, uniffi::Enum)]
pub enum NotificationMode {
    Notification,
    Indication,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, uniffi::Enum)]
pub enum NotificationModeRequest {
    PlatformDefault,
    RequireNotification,
    RequireIndication,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, uniffi::Enum)]
pub enum NotificationAction {
    Enable { mode: NotificationModeRequest },
    Disable,
}

#[derive(Clone, Debug, Default, PartialEq, Eq, uniffi::Record)]
pub struct ScanOptions {
    pub service_uuids: Vec<BluetoothUuid>,
    pub name_prefix: Option<String>,
    pub company_id: Option<u16>,
    pub allow_duplicates: bool,
}

impl ScanOptions {
    fn validate_and_canonicalize(&mut self) -> Result<(), CommandDispatchError> {
        for (index, service_uuid) in self.service_uuids.iter_mut().enumerate() {
            let Some(canonical) = BluetoothUuid::canonicalize(&service_uuid.value) else {
                return Err(CommandDispatchError::InvalidRequest {
                    detail: format!("service_uuids[{index}] is not a valid Bluetooth UUID"),
                });
            };
            *service_uuid = canonical;
        }
        if let Some(name_prefix) = &self.name_prefix {
            if name_prefix.is_empty() {
                return Err(CommandDispatchError::InvalidRequest {
                    detail: "name_prefix must not be empty".to_owned(),
                });
            }
            if name_prefix.len() > MAX_SCAN_NAME_PREFIX_BYTES {
                return Err(CommandDispatchError::InvalidRequest {
                    detail: format!("name_prefix exceeds {MAX_SCAN_NAME_PREFIX_BYTES} UTF-8 bytes"),
                });
            }
        }
        Ok(())
    }

    /// Evaluates BleHub's fixed scan predicate against one mapped observation.
    /// `allow_duplicates` is a reporting hint and does not affect matching.
    pub fn matches(&self, observation: &AdvertisementObservation) -> bool {
        if !self.service_uuids.is_empty()
            && !self
                .service_uuids
                .iter()
                .any(|required| observation.advertised_services.contains(required))
        {
            return false;
        }
        if let Some(prefix) = &self.name_prefix
            && !observation
                .local_name
                .as_ref()
                .is_some_and(|name| name.starts_with(prefix))
        {
            return false;
        }
        if let Some(company_id) = self.company_id
            && !observation
                .manufacturer_data
                .iter()
                .any(|data| data.company_id == company_id)
        {
            return false;
        }
        true
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, uniffi::Record)]
pub struct CancelOperationRequest {
    /// Existing command identity; cancellation has no independent lifecycle.
    pub target_command_id: CommandId,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, uniffi::Record)]
pub struct InitializeCommand {
    pub command_id: CommandId,
}

#[derive(Clone, Debug, PartialEq, Eq, uniffi::Record)]
pub struct StartScanCommand {
    pub command_id: CommandId,
    pub scan_session_id: ScanSessionId,
    pub options: ScanOptions,
}

impl StartScanCommand {
    pub(crate) fn validated(mut self) -> Result<Self, CommandDispatchError> {
        self.options.validate_and_canonicalize()?;
        Ok(self)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, uniffi::Record)]
pub struct StopScanCommand {
    pub command_id: CommandId,
    pub scan_session_id: ScanSessionId,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, uniffi::Record)]
pub struct ConnectCommand {
    pub command_id: CommandId,
    pub connection_id: ConnectionId,
    pub peripheral_id: PeripheralId,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, uniffi::Record)]
pub struct DisconnectCommand {
    pub command_id: CommandId,
    pub connection_id: ConnectionId,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, uniffi::Record)]
pub struct DiscoverGattCommand {
    pub command_id: CommandId,
    pub connection_id: ConnectionId,
    pub gatt_session_id: GattSessionId,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, uniffi::Record)]
pub struct ReadCharacteristicCommand {
    pub command_id: CommandId,
    pub connection_id: ConnectionId,
    pub gatt_session_id: GattSessionId,
    pub characteristic_id: GattCharacteristicId,
}

#[derive(Clone, Debug, PartialEq, Eq, uniffi::Record)]
pub struct WriteCharacteristicCommand {
    pub command_id: CommandId,
    pub connection_id: ConnectionId,
    pub gatt_session_id: GattSessionId,
    pub characteristic_id: GattCharacteristicId,
    pub value: Vec<u8>,
    pub write_type: WriteType,
}

impl WriteCharacteristicCommand {
    pub(crate) fn validate(&self) -> Result<(), CommandDispatchError> {
        if self.value.len() > MAX_WRITE_VALUE_BYTES {
            return Err(CommandDispatchError::InvalidRequest {
                detail: format!(
                    "value contains {} bytes; maximum is {MAX_WRITE_VALUE_BYTES}",
                    self.value.len()
                ),
            });
        }
        Ok(())
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, uniffi::Record)]
pub struct SetNotificationsCommand {
    pub command_id: CommandId,
    pub connection_id: ConnectionId,
    pub gatt_session_id: GattSessionId,
    pub subscription_id: SubscriptionId,
    pub characteristic_id: GattCharacteristicId,
    pub action: NotificationAction,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, uniffi::Record)]
pub struct ReadRssiCommand {
    pub command_id: CommandId,
    pub connection_id: ConnectionId,
}

#[cfg(test)]
#[path = "../tests/commands_tests.rs"]
mod tests;
