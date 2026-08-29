use super::BleHubError;

pub const BLEHUB_CONTRACT_VERSION: u32 = 3;

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
}

impl BackendCapabilities {
    pub fn new(platform: Platform) -> Self {
        Self {
            platform,
            contract_version: BLEHUB_CONTRACT_VERSION,
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
