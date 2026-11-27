use super::BleHubError;

/// Backend compatibility value: the canonical contract revision a backend was built against.
///
/// It changes only for an **incompatible** contract revision — a new required foreign-backend method,
/// a changed command/event/error shape, or a removed public item — never for an additive change a
/// previously built backend can still serve. Assembly compares it before binding, so a stale backend
/// fails with a typed contract error instead of surfacing as a missing callback at first use.
///
/// Revision 7 added the descriptor I/O, MTU, connection-priority, bonding, GATT-cache-refresh,
/// adapter-control, device-inventory and companion association/presence surface, and named the MTU
/// answer event `MtuRead`.
pub const BLEHUB_CONTRACT_VERSION: u32 = 7;

#[uniffi::export]
pub fn blehub_contract_version() -> u32 {
    BLEHUB_CONTRACT_VERSION
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, uniffi::Enum)]
pub enum Platform {
    Windows,
    Android,
    Apple,
    Linux,
    Test,
}

/// Static backend metadata, queried once during assembly.
///
/// It answers two kinds of question, and neither of them is "is this command accepted":
///
/// - **identity and compatibility**: `platform` is the single platform identity of an assembled
///   [`crate::BleHub`], and `contract_version` is checked before anything is bound;
/// - **observation quality**: the flags declare what the backend can *report*, so a host can decide
///   whether an observation is meaningful before relying on it (Android cannot report
///   `connectable` before API 26).
///
/// A command the backend cannot serve is refused at dispatch with
/// [`crate::CommandDispatchError::NotSupported`] — the one refusal every command shares. A flag
/// never pre-empts a dispatch, and none of them may be re-read as "command supported".
#[derive(Clone, Debug, PartialEq, Eq, uniffi::Record)]
pub struct BackendCapabilities {
    pub platform: Platform,
    pub contract_version: u32,
    pub supports_duplicate_scan_results: bool,
    pub supports_connectable_flag: bool,
    pub supports_read_rssi: bool,
    pub supports_required_notification: bool,
    pub supports_required_indication: bool,
    pub reports_actual_notification_mode: bool,
}

impl BackendCapabilities {
    pub fn new(platform: Platform) -> Self {
        Self {
            platform,
            contract_version: BLEHUB_CONTRACT_VERSION,
            supports_duplicate_scan_results: false,
            supports_connectable_flag: false,
            supports_read_rssi: false,
            supports_required_notification: false,
            supports_required_indication: false,
            reports_actual_notification_mode: false,
        }
    }

    pub(crate) fn validated(self) -> Result<Self, BleHubError> {
        if self.contract_version != BLEHUB_CONTRACT_VERSION {
            return Err(BleHubError::FfiContract {
                detail: format!(
                    "unsupported contract version {}; expected {}",
                    self.contract_version, BLEHUB_CONTRACT_VERSION
                ),
            });
        }
        Ok(self)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, uniffi::Enum)]
pub enum AdapterState {
    Unknown,
    Unavailable,
    PoweredOff,
    PoweredOn,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, uniffi::Enum)]
pub enum AuthorizationState {
    NotRequired,
    NotDetermined,
    Denied,
    Restricted,
    Authorized,
}

#[derive(Clone, Debug, PartialEq, Eq, uniffi::Record)]
pub struct AdapterSnapshot {
    pub state: AdapterState,
    pub scan_authorization: AuthorizationState,
    pub connection_authorization: AuthorizationState,
    pub name: Option<String>,
}
