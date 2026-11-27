use std::collections::HashSet;

use super::{
    AdapterSnapshot, AdvertisementObservation, BleEventError, BondState, CommandId, ConnectionId,
    DeviceRecord, GattCharacteristicId, GattDatabase, GattDescriptorId, GattSessionId, MtuInfo,
    NotificationMode, ScanSessionId, SubscriptionId,
};

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, uniffi::Enum)]
pub enum ScanStopReason {
    Requested { command_id: CommandId },
    SystemSuspended,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, uniffi::Enum)]
pub enum ConnectionState {
    Connecting,
    Connected,
    /// Optional backend progress; callers must not require it cross-platform.
    Disconnecting,
    /// The logical connection lifecycle has ended successfully.
    Disconnected,
}

#[derive(Clone, Debug, PartialEq, Eq, uniffi::Enum)]
pub enum ScanStopResult {
    Ok { reason: ScanStopReason },
    Err { error: BleEventError },
}

#[derive(Clone, Debug, PartialEq, Eq, uniffi::Enum)]
pub enum ConnectionStateResult {
    Ok { state: ConnectionState },
    Err { error: BleEventError },
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, uniffi::Enum)]
pub enum NotificationState {
    Enabled {
        actual_mode: Option<NotificationMode>,
    },
    Disabled,
}

#[derive(Clone, Debug, PartialEq, Eq, uniffi::Enum)]
pub enum BleEvent {
    AdapterSnapshotChanged {
        command_id: Option<CommandId>,
        snapshot: AdapterSnapshot,
    },
    ScanStarted {
        command_id: CommandId,
        scan_id: ScanSessionId,
    },
    AdvertisementObserved {
        advertisement: AdvertisementObservation,
    },
    ScanStopped {
        scan_id: ScanSessionId,
        result: ScanStopResult,
    },
    ConnectionStateChanged {
        command_id: Option<CommandId>,
        connection_id: ConnectionId,
        result: ConnectionStateResult,
    },
    GattDiscovered {
        command_id: CommandId,
        database: GattDatabase,
    },
    CharacteristicRead {
        command_id: CommandId,
        connection_id: ConnectionId,
        gatt_session_id: GattSessionId,
        characteristic_id: GattCharacteristicId,
        value: Vec<u8>,
    },
    CharacteristicWritten {
        command_id: CommandId,
        connection_id: ConnectionId,
        gatt_session_id: GattSessionId,
        characteristic_id: GattCharacteristicId,
    },
    /// One descriptor value. An empty payload is a real value, not a missing one; absence is
    /// reported on the query's outcome, never inside this event.
    DescriptorRead {
        command_id: CommandId,
        connection_id: ConnectionId,
        gatt_session_id: GattSessionId,
        characteristic_id: GattCharacteristicId,
        descriptor_id: GattDescriptorId,
        value: Vec<u8>,
    },
    /// One descriptor write the stack accepted. A write receipt is not a read-back.
    DescriptorWritten {
        command_id: CommandId,
        connection_id: ConnectionId,
        gatt_session_id: GattSessionId,
        characteristic_id: GattCharacteristicId,
        descriptor_id: GattDescriptorId,
    },
    NotificationStateChanged {
        command_id: CommandId,
        connection_id: ConnectionId,
        gatt_session_id: GattSessionId,
        subscription_id: SubscriptionId,
        characteristic_id: GattCharacteristicId,
        state: NotificationState,
    },
    NotificationReceived {
        connection_id: ConnectionId,
        gatt_session_id: GattSessionId,
        subscription_id: SubscriptionId,
        characteristic_id: GattCharacteristicId,
        value: Vec<u8>,
    },
    RssiRead {
        command_id: CommandId,
        connection_id: ConnectionId,
        rssi: i16,
    },
    /// The link's own MTU/payload report for a query or an accepted negotiation request.
    ///
    /// It is the platform's snapshot, never an echo of the requested value; any field may be absent
    /// when the platform does not expose it, and absent means unknown rather than `0`.
    MtuRead {
        command_id: CommandId,
        connection_id: ConnectionId,
        info: MtuInfo,
    },
    /// One read-only system inventory. `devices` are not connectable capabilities.
    DevicesListed {
        command_id: CommandId,
        devices: Vec<DeviceRecord>,
    },
    /// One platform-reported companion-device presence change (Android only).
    ///
    /// Presence is an association event, not a link: an associated device may appear without being
    /// connectable, and it never carries a bond claim. `key` reuses the owner-scoped inventory key
    /// the backend already allocated for that address, and `local_name` is the platform's
    /// association label when it has one. This event is never correlated with a command.
    CompanionPresenceChanged {
        present: bool,
        key: String,
        local_name: Option<String>,
    },
    /// A platform-reported bond (pairing) state change.
    ///
    /// `command_id` is present only while it correlates with the accepted `CreateBond`; a
    /// peer-initiated or unsolicited change carries `None` and reaches the connection lane. The
    /// state is the platform's report, not a promise that encryption was negotiated.
    BondStateChanged {
        command_id: Option<CommandId>,
        connection_id: ConnectionId,
        state: BondState,
    },
    CommandCompleted {
        command_id: CommandId,
        error: Option<BleEventError>,
    },
    BackendError {
        error: BleEventError,
    },
}

impl BleEvent {
    /// Returns the command ID carried by this event, when the canonical event
    /// has one. This is only a structural projection: consumers may still
    /// apply their own routing policy when an event can affect multiple
    /// waiters, such as connection-state or scan-stop events.
    pub fn command_id(&self) -> Option<CommandId> {
        match self {
            Self::AdapterSnapshotChanged { command_id, .. }
            | Self::ConnectionStateChanged { command_id, .. }
            | Self::BondStateChanged { command_id, .. } => *command_id,
            Self::ScanStarted { command_id, .. }
            | Self::GattDiscovered { command_id, .. }
            | Self::CharacteristicRead { command_id, .. }
            | Self::CharacteristicWritten { command_id, .. }
            | Self::DescriptorRead { command_id, .. }
            | Self::DescriptorWritten { command_id, .. }
            | Self::NotificationStateChanged { command_id, .. }
            | Self::RssiRead { command_id, .. }
            | Self::MtuRead { command_id, .. }
            | Self::DevicesListed { command_id, .. }
            | Self::CommandCompleted { command_id, .. } => Some(*command_id),
            Self::CompanionPresenceChanged { .. } => None,
            Self::ScanStopped { result, .. } => match result {
                ScanStopResult::Ok {
                    reason: ScanStopReason::Requested { command_id },
                } => Some(*command_id),
                ScanStopResult::Ok {
                    reason: ScanStopReason::SystemSuspended,
                }
                | ScanStopResult::Err { .. } => None,
            },
            Self::AdvertisementObserved { .. }
            | Self::NotificationReceived { .. }
            | Self::BackendError { .. } => None,
        }
    }

    pub(crate) fn validate(&self) -> Result<(), String> {
        match self {
            Self::AdvertisementObserved { advertisement } => advertisement.validate(),
            Self::GattDiscovered { database, .. } => database.validate(),
            Self::DevicesListed { devices, .. } => validate_device_inventory(devices),
            Self::CompanionPresenceChanged { key, .. } => validate_scoped_key("presence key", key),
            _ => Ok(()),
        }
    }
}

/// One inventory answer may not repeat a key or carry an empty one.
///
/// The App uses these keys as map keys and may adopt one, so a malformed key is a backend contract
/// violation rather than data to display.
fn validate_device_inventory(devices: &[DeviceRecord]) -> Result<(), String> {
    let mut keys = HashSet::new();
    for (index, device) in devices.iter().enumerate() {
        if device.key.is_empty() {
            return Err(format!("devices[{index}].key must not be empty"));
        }
        if !keys.insert(device.key.as_str()) {
            return Err(format!(
                "devices[{index}].key duplicates another device key"
            ));
        }
    }
    Ok(())
}

/// An owner-scoped key is the only handle a caller gets, so an empty one addresses nothing.
fn validate_scoped_key(field: &str, key: &str) -> Result<(), String> {
    if key.is_empty() {
        return Err(format!("{field} must not be empty"));
    }
    Ok(())
}
