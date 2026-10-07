//! Typed identities use the full `u64` wire range, including zero. They are not
//! collection indices or native ATT handles; narrowing them would shorten the
//! non-reusing allocator's lifetime, and `usize` would make the wire width
//! platform-dependent.
//!
//! Rust callers use `From<u64>` and `Into<u64>` at allocator/projection boundaries.
//! These conversions preserve numbers only, not identity scope or lifecycle validity.

use derive_more::{From, Into};

const BLUETOOTH_BASE_UUID_SUFFIX: &str = "-0000-1000-8000-00805f9b34fb";

/// FFI-safe textual Bluetooth UUID.
///
/// Command inputs may use supported short forms. BleHub canonicalizes them to
/// lowercase hyphenated 128-bit form before dispatching to a backend.
#[derive(Clone, Debug, PartialEq, Eq, Hash, uniffi::Record)]
pub struct BluetoothUuid {
    pub value: String,
}

impl BluetoothUuid {
    /// Converts 16-bit, 32-bit, simple 128-bit, or hyphenated 128-bit Bluetooth
    /// UUID text to lowercase hyphenated 128-bit form.
    pub fn canonicalize(value: &str) -> Option<Self> {
        let expanded = match value.len() {
            4 if value.bytes().all(|byte| byte.is_ascii_hexdigit()) => {
                format!("0000{value}{BLUETOOTH_BASE_UUID_SUFFIX}")
            }
            8 if value.bytes().all(|byte| byte.is_ascii_hexdigit()) => {
                format!("{value}{BLUETOOTH_BASE_UUID_SUFFIX}")
            }
            32 | 36 => value.to_owned(),
            _ => return None,
        };
        uuid::Uuid::parse_str(&expanded).ok().map(|value| Self {
            value: value.hyphenated().to_string(),
        })
    }

    pub(crate) fn is_canonical(&self) -> bool {
        Self::canonicalize(&self.value).as_ref() == Some(self)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, From, Into, uniffi::Record)]
pub struct CommandId {
    pub value: u64,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, From, Into, uniffi::Record)]
pub struct ScanSessionId {
    pub value: u64,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, From, Into, uniffi::Record)]
pub struct PeripheralId {
    pub value: u64,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, From, Into, uniffi::Record)]
pub struct ConnectionId {
    pub value: u64,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, From, Into, uniffi::Record)]
pub struct GattSessionId {
    pub value: u64,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, From, Into, uniffi::Record)]
pub struct GattServiceId {
    pub value: u64,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, From, Into, uniffi::Record)]
pub struct GattCharacteristicId {
    pub value: u64,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, From, Into, uniffi::Record)]
pub struct GattDescriptorId {
    pub value: u64,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, From, Into, uniffi::Record)]
pub struct SubscriptionId {
    pub value: u64,
}

#[cfg(test)]
#[path = "../tests/ids_tests.rs"]
mod tests;
