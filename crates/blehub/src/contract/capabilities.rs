use super::BleHubError;

pub const BLEHUB_CONTRACT_VERSION: u32 = 5;

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
