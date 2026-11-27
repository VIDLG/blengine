use super::{
    AdvertisementObservation, BluetoothUuid, CommandDispatchError, CommandId, ConnectionId,
    GattCharacteristicId, GattDescriptorId, GattSessionId, PeripheralId, ScanSessionId,
    SubscriptionId,
};

/// Bluetooth local names are limited to 248 UTF-8 bytes, so a longer name filter (or the association
/// label shown in a platform chooser) cannot match anything.
pub const MAX_LOCAL_NAME_BYTES: usize = 248;

/// ATT limits an attribute value to 512 octets; a platform's per-write limit may be lower.
pub const MAX_WRITE_VALUE_BYTES: usize = 512;

/// Search-mode hint for Android's LE scanner. Apple rejects a scan that sets it.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, uniffi::Enum)]
pub enum AndroidScanMode {
    /// Lowest power; scan windows are sparse.
    LowPower,
    /// Balance of latency and power.
    Balanced,
    /// Highest duty cycle; lowest latency and highest power.
    LowLatency,
    /// Opportunistic scans that do not affect other scanners.
    Opportunistic,
}

/// Preferred PHY for Android's LE scanner. Requires API 26; Apple rejects it.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, uniffi::Enum)]
pub enum AndroidScanPhy {
    Le1m,
    Le2m,
    LeCoded,
}

/// Android-only native scan tuning.
///
/// These are scanner hints, not part of the engine's fixed match predicate: they change how the
/// platform scans, never what counts as a match. Apple has no equivalent and rejects any scan that
/// sets this record, instead of silently scanning with different behaviour.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, uniffi::Record)]
pub struct AndroidScanOptions {
    pub scan_mode: Option<AndroidScanMode>,
    pub phy: Option<AndroidScanPhy>,
    /// Scanner batch delay in milliseconds; `0` reports every result immediately.
    pub report_delay_ms: Option<u32>,
    /// Scan only legacy (non-extended) advertisements.
    pub legacy: Option<bool>,
}

/// Upper bound Android accepts for `ScanSettings.Builder.setReportDelay`.
pub const MAX_SCAN_REPORT_DELAY_MS: u32 = 5000;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, uniffi::Enum)]
pub enum CommandKind {
    Initialize,
    SetAdapterEnabled,
    QueryDevices,
    AssociateCompanionDevice,
    StartScan,
    StopScan,
    Connect,
    Disconnect,
    DiscoverGatt,
    ReadCharacteristic,
    WriteCharacteristic,
    ReadDescriptor,
    WriteDescriptor,
    SetNotifications,
    ReadRssi,
    Mtu,
    SetConnectionPriority,
    CreateBond,
    RemoveBond,
    RefreshGattCache,
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
    /// Require an exact local-name match. Applied on both platforms by the scan predicate.
    pub exact_name: Option<String>,
    pub company_id: Option<u16>,
    /// Require an advertisement RSSI at or above this value. Absent RSSI never matches.
    pub min_rssi: Option<i16>,
    /// Require a manufacturer-data payload match (prefix, or masked equality when `mask` is set).
    pub manufacturer_data: Option<ManufacturerDataFilter>,
    /// Android-only scanner tuning; `None` keeps the engine's fixed defaults. Apple rejects it.
    pub android: Option<AndroidScanOptions>,
    pub allow_duplicates: bool,
}

/// A caller-supplied advertisement filter is bounded so no filter can be unbounded. The number
/// mirrors the wire guard on a write value; it is an input bound, not a claim about what an
/// advertisement carries.
const MAX_MANUFACTURER_DATA_FILTER_BYTES: usize = MAX_WRITE_VALUE_BYTES;

/// One manufacturer-data predicate: the payload must start with `data`, or match it byte-for-byte
/// under `mask` when a mask is supplied. An entry with a different `company_id` never matches.
///
/// The filter's own size is bounded by the same wire guard as a write value, so a caller cannot
/// submit an unbounded mask; `mask` must be exactly as long as `data`.
#[derive(Clone, Debug, Default, PartialEq, Eq, uniffi::Record)]
pub struct ManufacturerDataFilter {
    pub company_id: u16,
    pub data: Vec<u8>,
    pub mask: Option<Vec<u8>>,
}

impl ManufacturerDataFilter {
    fn validate(&self) -> Result<(), CommandDispatchError> {
        if self.data.len() > MAX_MANUFACTURER_DATA_FILTER_BYTES {
            return Err(CommandDispatchError::InvalidRequest {
                detail: format!(
                    "manufacturer_data.data exceeds {MAX_MANUFACTURER_DATA_FILTER_BYTES} bytes"
                ),
            });
        }
        if let Some(mask) = &self.mask
            && mask.len() != self.data.len()
        {
            return Err(CommandDispatchError::InvalidRequest {
                detail: "manufacturer_data.mask must match data length".to_owned(),
            });
        }
        Ok(())
    }

    /// One predicate evaluation. A filter whose mask length disagrees with its data can only come
    /// from a caller that skipped validation, so it matches nothing instead of comparing fewer
    /// bytes than the caller asked for.
    fn matches_payload(&self, payload: &[u8]) -> bool {
        if payload.len() < self.data.len() {
            return false;
        }
        let Some(mask) = &self.mask else {
            return payload.starts_with(&self.data);
        };
        mask.len() == self.data.len()
            && self
                .data
                .iter()
                .zip(mask)
                .enumerate()
                .all(|(index, (expected, mask))| (payload[index] & mask) == (expected & mask))
    }
}

/// Canonicalizes one command's UUID list in place, naming the offending index.
///
/// Every command that carries service UUIDs shares this rule, so a caller cannot get a different
/// accepted spelling depending on which command it submitted.
fn canonicalize_service_uuids(uuids: &mut [BluetoothUuid]) -> Result<(), CommandDispatchError> {
    for (index, service_uuid) in uuids.iter_mut().enumerate() {
        let Some(canonical) = BluetoothUuid::canonicalize(&service_uuid.value) else {
            return Err(CommandDispatchError::InvalidRequest {
                detail: format!("service_uuids[{index}] is not a valid Bluetooth UUID"),
            });
        };
        *service_uuid = canonical;
    }
    Ok(())
}

/// One caller-supplied name filter: non-empty and within the Bluetooth local-name limit.
fn validate_name_filter(field: &str, name: &str) -> Result<(), CommandDispatchError> {
    if name.is_empty() {
        return Err(CommandDispatchError::InvalidRequest {
            detail: format!("{field} must not be empty"),
        });
    }
    if name.len() > MAX_LOCAL_NAME_BYTES {
        return Err(CommandDispatchError::InvalidRequest {
            detail: format!("{field} exceeds {MAX_LOCAL_NAME_BYTES} UTF-8 bytes"),
        });
    }
    Ok(())
}

impl ScanOptions {
    /// The canonical scan-filter contract, and the rule every caller passes through.
    ///
    /// It is public so a foreign boundary can refuse an invalid filter **synchronously** — with the
    /// same message the engine would produce — instead of re-implementing the rules or letting a
    /// caller mistake surface only as a later dispatch rejection.
    pub fn validated(mut self) -> Result<Self, CommandDispatchError> {
        self.validate_and_canonicalize()?;
        Ok(self)
    }

    fn validate_and_canonicalize(&mut self) -> Result<(), CommandDispatchError> {
        canonicalize_service_uuids(&mut self.service_uuids)?;
        if let Some(name_prefix) = &self.name_prefix {
            validate_name_filter("name_prefix", name_prefix)?;
        }
        if let Some(exact_name) = &self.exact_name {
            validate_name_filter("exact_name", exact_name)?;
        }
        if let Some(filter) = &self.manufacturer_data {
            filter.validate()?;
        }
        if let Some(android) = &self.android
            && let Some(report_delay_ms) = android.report_delay_ms
            && report_delay_ms > MAX_SCAN_REPORT_DELAY_MS
        {
            return Err(CommandDispatchError::InvalidRequest {
                detail: format!(
                    "android.report_delay_ms must be at most {MAX_SCAN_REPORT_DELAY_MS}"
                ),
            });
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
        if let Some(exact) = &self.exact_name
            && observation.local_name.as_deref() != Some(exact.as_str())
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
        if let Some(min_rssi) = self.min_rssi
            && observation.rssi.is_none_or(|rssi| rssi < min_rssi)
        {
            return false;
        }
        if let Some(filter) = &self.manufacturer_data
            && !observation.manufacturer_data.iter().any(|entry| {
                entry.company_id == filter.company_id && filter.matches_payload(&entry.payload)
            })
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

/// One bounded adapter power request.
///
/// Success only means the platform accepted the request, not that the radio changed: the adapter's
/// actual state arrives through the adapter observation lane. Platforms that cannot change the
/// radio reject this command with `CommandDispatchError::NotSupported` instead of pretending.
#[derive(Clone, Copy, Debug, PartialEq, Eq, uniffi::Record)]
pub struct SetAdapterEnabledCommand {
    pub command_id: CommandId,
    pub enabled: bool,
}

/// Bounded length for a host-supplied Android companion-device profile identifier.
pub const MAX_COMPANION_PROFILE_BYTES: usize = 128;

/// One companion-device association request.
///
/// Android's `CompanionDeviceManager` shows a system chooser and records the association; the
/// platform, not this engine, decides which peer the user picked. A receipt therefore means the
/// platform reported an association, not that a device is present, connected or bonded: read
/// `DeviceQueryKind::Companion` afterwards for the association list.
///
/// `device_profile` is the host's own decision. When set, the host must hold the matching
/// `REQUEST_COMPANION_PROFILE_*` permission; this library declares no companion permission and no
/// default profile. Apple has no equivalent association model and rejects the request with
/// `CommandDispatchError::NotSupported`.
#[derive(Clone, Debug, PartialEq, Eq, uniffi::Record)]
pub struct AssociateCompanionDeviceCommand {
    pub command_id: CommandId,
    /// At least one service UUID; the platform chooser filters candidates on it.
    pub service_uuids: Vec<BluetoothUuid>,
    /// Optional label the platform chooser shows for this request.
    pub display_name: Option<String>,
    /// Optional platform companion-device profile, for example
    /// `android.app.role.COMPANION_DEVICE_WATCH`.
    pub device_profile: Option<String>,
    /// Whether the platform should deliver presence to the app's companion-device service.
    ///
    /// The host opts in because it grants the App a wake-up privilege; the library ships the
    /// service but never asks for presence on its own.
    pub notify_on_device_nearby: bool,
}

impl AssociateCompanionDeviceCommand {
    pub(crate) fn validated(mut self) -> Result<Self, CommandDispatchError> {
        if self.service_uuids.is_empty() {
            return Err(CommandDispatchError::InvalidRequest {
                detail: "a companion association requires at least one service UUID".to_owned(),
            });
        }
        canonicalize_service_uuids(&mut self.service_uuids)?;
        if let Some(display_name) = &self.display_name {
            validate_name_filter("display_name", display_name)?;
        }
        if let Some(profile) = &self.device_profile
            && (profile.is_empty() || profile.len() > MAX_COMPANION_PROFILE_BYTES)
        {
            return Err(CommandDispatchError::InvalidRequest {
                detail: format!(
                    "device_profile must be 1 to {MAX_COMPANION_PROFILE_BYTES} UTF-8 bytes"
                ),
            });
        }
        Ok(self)
    }
}

/// Which system device inventory to read.
///
/// A query is read-only and never creates a connectable capability: the returned keys are for
/// display and correlation, not native addressing.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, uniffi::Enum)]
pub enum DeviceQueryKind {
    Connected,
    Bonded,
    /// Companion-device associations this app registered with the platform (Android API 26+).
    ///
    /// It is a system association list, not a link: an associated device may be absent, and the
    /// record reports neither `connected` nor `bonded`. Apple has no equivalent association model,
    /// so it refuses this kind with `not_supported`.
    Companion,
}

/// One bounded system-inventory query.
///
/// Apple's connected query requires at least one service UUID and has no bonded or companion
/// inventory; Android accepts all kinds and ignores `service_uuids`.
#[derive(Clone, Debug, PartialEq, Eq, uniffi::Record)]
pub struct QueryDevicesCommand {
    pub command_id: CommandId,
    pub kind: DeviceQueryKind,
    pub service_uuids: Vec<BluetoothUuid>,
}

impl QueryDevicesCommand {
    pub(crate) fn validated(mut self) -> Result<Self, CommandDispatchError> {
        canonicalize_service_uuids(&mut self.service_uuids)?;
        Ok(self)
    }
}

/// One read-only device in a system inventory.
#[derive(Clone, Debug, PartialEq, Eq, uniffi::Record)]
pub struct DeviceRecord {
    /// Opaque and owner-scoped. Comparable with `peripheral.key()` when the same peer was scanned,
    /// but it cannot address native state on its own.
    pub key: String,
    pub local_name: Option<String>,
    pub connected: bool,
    pub bonded: bool,
}

#[derive(Clone, Debug, PartialEq, Eq, uniffi::Record)]
pub struct StartScanCommand {
    pub command_id: CommandId,
    pub scan_session_id: ScanSessionId,
    pub options: ScanOptions,
}

impl StartScanCommand {
    pub(crate) fn validated(mut self) -> Result<Self, CommandDispatchError> {
        self.options = self.options.validated()?;
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
    /// Ask the platform to establish the link whenever the peer becomes available, instead of a
    /// one-shot connect attempt. Where the platform cannot express this, the backend rejects the
    /// command with `CommandDispatchError::NotSupported` rather than silently connecting once.
    pub auto_connect: bool,
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
    pub(crate) fn validated(self) -> Result<Self, CommandDispatchError> {
        if self.value.len() > MAX_WRITE_VALUE_BYTES {
            return Err(CommandDispatchError::InvalidRequest {
                detail: format!(
                    "value contains {} bytes; maximum is {MAX_WRITE_VALUE_BYTES}",
                    self.value.len()
                ),
            });
        }
        Ok(self)
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

/// Reads one discovered descriptor. Descriptors are addressed through the characteristic that owns
/// them so a stale topology cannot address a replacement attribute by bare ID.
#[derive(Clone, Copy, Debug, PartialEq, Eq, uniffi::Record)]
pub struct ReadDescriptorCommand {
    pub command_id: CommandId,
    pub connection_id: ConnectionId,
    pub gatt_session_id: GattSessionId,
    pub characteristic_id: GattCharacteristicId,
    pub descriptor_id: GattDescriptorId,
}

/// Writes one discovered descriptor. `WriteType` is absent: descriptor writes are always ATT
/// requests with a response, so there is no with/without-response choice to make.
#[derive(Clone, Debug, PartialEq, Eq, uniffi::Record)]
pub struct WriteDescriptorCommand {
    pub command_id: CommandId,
    pub connection_id: ConnectionId,
    pub gatt_session_id: GattSessionId,
    pub characteristic_id: GattCharacteristicId,
    pub descriptor_id: GattDescriptorId,
    pub value: Vec<u8>,
}

impl WriteDescriptorCommand {
    pub(crate) fn validated(self) -> Result<Self, CommandDispatchError> {
        if self.value.len() > MAX_WRITE_VALUE_BYTES {
            return Err(CommandDispatchError::InvalidRequest {
                detail: format!(
                    "value contains {} bytes; maximum is {MAX_WRITE_VALUE_BYTES}",
                    self.value.len()
                ),
            });
        }
        Ok(self)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, uniffi::Record)]
pub struct ReadRssiCommand {
    pub command_id: CommandId,
    pub connection_id: ConnectionId,
}

/// Minimum and maximum MTU values accepted by the canonical contract.
pub const MIN_MTU: u16 = 23;
pub const MAX_MTU: u16 = 517;

/// One bounded MTU query or request. `request: None` asks for the current values; `Some(mtu)` asks
/// the platform to negotiate that MTU where the platform supports it.
#[derive(Clone, Copy, Debug, PartialEq, Eq, uniffi::Record)]
pub struct MtuCommand {
    pub command_id: CommandId,
    pub connection_id: ConnectionId,
    pub request: Option<u16>,
}

impl MtuCommand {
    pub(crate) fn validated(self) -> Result<Self, CommandDispatchError> {
        if let Some(mtu) = self.request
            && !(MIN_MTU..=MAX_MTU).contains(&mtu)
        {
            return Err(CommandDispatchError::InvalidRequest {
                detail: format!("mtu must be between {MIN_MTU} and {MAX_MTU}"),
            });
        }
        Ok(self)
    }
}

/// Link-level MTU and payload limits reported by the platform.
///
/// `mtu` is absent when the platform does not expose the negotiated value (CoreBluetooth does not).
/// Payload limits are absent when the platform cannot report them.
#[derive(Clone, Copy, Debug, PartialEq, Eq, uniffi::Record)]
pub struct MtuInfo {
    pub mtu: Option<u16>,
    pub maximum_write_with_response: Option<u16>,
    pub maximum_write_without_response: Option<u16>,
}

/// Requested link scheduling preference. This is the caller's request, not a measured link mode.
///
/// Platforms that do not expose connection priority reject the command with
/// `CommandDispatchError::NotSupported` instead of silently ignoring it.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, uniffi::Enum)]
pub enum ConnectionPriority {
    /// Throughput between the two peers; the platform's default scheduling.
    Balanced,
    /// Favour throughput over latency and power.
    High,
    /// Favour power over throughput.
    LowPower,
}

/// One bounded connection-priority request.
///
/// A successful terminal only means the platform accepted the request. It is not proof that the
/// controller applied it: platforms do not report the effective scheduling back to the app.
#[derive(Clone, Copy, Debug, PartialEq, Eq, uniffi::Record)]
pub struct SetConnectionPriorityCommand {
    pub command_id: CommandId,
    pub connection_id: ConnectionId,
    pub priority: ConnectionPriority,
}

/// The platform-reported bond (pairing) state for one peer.
///
/// `Bonding` is progress, not a terminal; an app must keep waiting until `Bonded` or the operation
/// reports a failure. There is no cross-platform promise that a bond survives reinstall, and none
/// that a bonded peer reconnects without user interaction.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, uniffi::Enum)]
pub enum BondState {
    None,
    Bonding,
    Bonded,
}

/// Longest numeric PIN the platform accepts for programmatic pairing. A PIN is a caller-supplied
/// secret, so the only thing this contract can check is its shape.
const MAX_BOND_PIN_DIGITS: usize = 16;

/// One bounded bond (pairing) request on an existing connection.
///
/// Platforms that cannot initiate bonding reject this command with
/// `CommandDispatchError::NotSupported` instead of silently pretending the peer is bonded.
///
/// `pin` is only the platform's **numeric PIN pairing** input: 1 to 16 ASCII digits. It is never
/// logged, and `None` leaves the system pairing UI in charge. Platforms without a programmatic PIN
/// (iOS) still reject the whole command; a passkey/consent prompt is never auto-answered by a
/// library.
#[derive(Clone, Debug, PartialEq, Eq, uniffi::Record)]
pub struct CreateBondCommand {
    pub command_id: CommandId,
    pub connection_id: ConnectionId,
    pub pin: Option<String>,
}

impl CreateBondCommand {
    /// Rejects a PIN shape no platform can consume, before it reaches any backend.
    pub(crate) fn validated(self) -> Result<Self, CommandDispatchError> {
        if let Some(pin) = &self.pin
            && (pin.is_empty()
                || pin.len() > MAX_BOND_PIN_DIGITS
                || !pin.bytes().all(|digit| digit.is_ascii_digit()))
        {
            return Err(CommandDispatchError::InvalidRequest {
                detail: format!("pin must be 1 to {MAX_BOND_PIN_DIGITS} ASCII digits"),
            });
        }
        Ok(self)
    }
}

/// One bounded bond-removal request on an existing connection.
///
/// Success is the platform-reported `None` bond state. Platforms that cannot remove a bond reject
/// this command with `CommandDispatchError::NotSupported` instead of silently claiming success.
#[derive(Clone, Copy, Debug, PartialEq, Eq, uniffi::Record)]
pub struct RemoveBondCommand {
    pub command_id: CommandId,
    pub connection_id: ConnectionId,
}

/// One bounded request to invalidate the platform's cached GATT topology for a connection.
///
/// Success only means the platform accepted the refresh. The engine also drops its previously
/// discovered topology for this connection, so old GATT-session capabilities fail and the caller
/// must rediscover. Platforms without a cache control reject this command.
#[derive(Clone, Copy, Debug, PartialEq, Eq, uniffi::Record)]
pub struct RefreshGattCacheCommand {
    pub command_id: CommandId,
    pub connection_id: ConnectionId,
}

#[cfg(test)]
#[path = "../tests/commands_tests.rs"]
mod tests;
