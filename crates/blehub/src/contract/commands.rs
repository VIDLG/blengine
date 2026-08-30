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
mod tests {
    use super::*;
    use crate::ManufacturerData;

    fn uuid(value: &str) -> BluetoothUuid {
        BluetoothUuid {
            value: value.to_owned(),
        }
    }

    fn observation() -> AdvertisementObservation {
        AdvertisementObservation {
            scan_session_id: ScanSessionId { value: 1 },
            peripheral_id: PeripheralId { value: 2 },
            local_name: Some("BleHub Fixture".to_owned()),
            advertised_services: vec![uuid("0000180d-0000-1000-8000-00805f9b34fb")],
            manufacturer_data: vec![ManufacturerData {
                company_id: 0x1234,
                payload: vec![1, 2],
            }],
            service_data: Vec::new(),
            rssi: Some(-42),
            connectable: None,
        }
    }

    fn assert_invalid<T>(result: Result<T, CommandDispatchError>, detail: &str) {
        assert_eq!(
            result.err(),
            Some(CommandDispatchError::InvalidRequest {
                detail: detail.to_owned(),
            })
        );
    }

    #[test]
    fn scan_predicate_uses_any_service_and_and_between_filter_groups() {
        let observation = observation();
        let matching = ScanOptions {
            service_uuids: vec![
                uuid("0000180f-0000-1000-8000-00805f9b34fb"),
                uuid("0000180d-0000-1000-8000-00805f9b34fb"),
            ],
            name_prefix: Some("BleHub".to_owned()),
            company_id: Some(0x1234),
            allow_duplicates: false,
        };
        assert!(matching.matches(&observation));

        for non_matching in [
            ScanOptions {
                service_uuids: vec![uuid("0000180f-0000-1000-8000-00805f9b34fb")],
                ..matching.clone()
            },
            ScanOptions {
                name_prefix: Some("blehub".to_owned()),
                ..matching.clone()
            },
            ScanOptions {
                company_id: Some(0xffff),
                ..matching.clone()
            },
        ] {
            assert!(!non_matching.matches(&observation));
        }
    }

    #[test]
    fn required_missing_observation_fields_do_not_match() {
        let observation = AdvertisementObservation {
            local_name: None,
            advertised_services: Vec::new(),
            manufacturer_data: Vec::new(),
            ..observation()
        };
        let options = ScanOptions {
            service_uuids: vec![uuid("0000180d-0000-1000-8000-00805f9b34fb")],
            name_prefix: Some("BleHub".to_owned()),
            company_id: Some(0x1234),
            allow_duplicates: true,
        };
        assert!(!options.matches(&observation));
        assert!(ScanOptions::default().matches(&observation));
    }

    #[test]
    fn scan_options_enforce_wire_guards() {
        let valid = StartScanCommand {
            command_id: CommandId { value: 1 },
            scan_session_id: ScanSessionId { value: 2 },
            options: ScanOptions {
                service_uuids: vec![BluetoothUuid {
                    value: "180D".to_owned(),
                }],
                name_prefix: Some("BleHub".to_owned()),
                company_id: Some(0x1234),
                allow_duplicates: true,
            },
        };
        let canonical = valid.clone().validated().unwrap();
        assert_eq!(
            canonical.options.service_uuids,
            vec![BluetoothUuid {
                value: "0000180d-0000-1000-8000-00805f9b34fb".to_owned(),
            }]
        );

        let mut command = valid.clone();
        command.options.service_uuids = vec![BluetoothUuid {
            value: String::new(),
        }];
        assert_invalid(
            command.validated(),
            "service_uuids[0] is not a valid Bluetooth UUID",
        );

        let mut command = valid.clone();
        command.options.name_prefix = Some(String::new());
        assert_invalid(command.validated(), "name_prefix must not be empty");

        let mut command = valid;
        command.options.name_prefix = Some("x".repeat(MAX_SCAN_NAME_PREFIX_BYTES + 1));
        assert_invalid(command.validated(), "name_prefix exceeds 248 UTF-8 bytes");
    }

    #[test]
    fn write_value_enforces_wire_guard() {
        let mut write = WriteCharacteristicCommand {
            command_id: CommandId { value: 1 },
            connection_id: ConnectionId { value: 2 },
            gatt_session_id: GattSessionId { value: 3 },
            characteristic_id: GattCharacteristicId { value: 4 },
            value: vec![0; MAX_WRITE_VALUE_BYTES],
            write_type: WriteType::WithResponse,
        };
        assert_eq!(write.validate(), Ok(()));
        write.value.push(0);
        assert_invalid(write.validate(), "value contains 513 bytes; maximum is 512");
    }
}
