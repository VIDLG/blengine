use super::{
    AdapterSnapshot, AdvertisementObservation, BleEventError, CommandId, ConnectionId,
    GattCharacteristicId, GattDatabase, GattSessionId, NotificationMode, ScanSessionId,
    SubscriptionId,
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
            | Self::ConnectionStateChanged { command_id, .. } => *command_id,
            Self::ScanStarted { command_id, .. }
            | Self::GattDiscovered { command_id, .. }
            | Self::CharacteristicRead { command_id, .. }
            | Self::CharacteristicWritten { command_id, .. }
            | Self::NotificationStateChanged { command_id, .. }
            | Self::RssiRead { command_id, .. }
            | Self::CommandCompleted { command_id, .. } => Some(*command_id),
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
            _ => Ok(()),
        }
    }
}
