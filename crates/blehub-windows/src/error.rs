use blehub::{
    BleErrorCategory, BleEventError, BleHubError, CommandDispatchError, Platform,
    PlatformErrorDetails,
};
use windows::{Devices::Bluetooth::BluetoothError, core::Error};

fn hresult_details(error: &Error) -> PlatformErrorDetails {
    PlatformErrorDetails {
        platform: Platform::Windows,
        code: Some(format!("0x{:08X}", error.code().0 as u32)),
        name: None,
        message: Some(error.message()),
    }
}

pub(crate) fn dispatch_error(context: &str, error: Error) -> CommandDispatchError {
    CommandDispatchError::BackendUnavailable {
        detail: format!("{context}: {}", error.message()),
        platform_details: Some(hresult_details(&error)),
    }
}

pub(crate) fn simple_event_error(
    category: BleErrorCategory,
    message: impl Into<String>,
) -> BleEventError {
    BleEventError {
        category,
        message: message.into(),
        platform_details: None,
    }
}

fn classify_hresult(error: &Error, fallback: BleErrorCategory) -> BleErrorCategory {
    match error.code().0 as u32 {
        0x8007_0005 => BleErrorCategory::Permission,
        0x8000_000E | 0x8000_0013 => BleErrorCategory::InvalidState,
        0x8000_4001 | 0x8000_4002 | 0x8007_0032 => BleErrorCategory::NotSupported,
        _ => fallback,
    }
}

pub(crate) fn event_error(
    fallback_category: BleErrorCategory,
    context: &str,
    error: Error,
) -> BleEventError {
    BleEventError {
        category: classify_hresult(&error, fallback_category),
        message: format!("{context}: {}", error.message()),
        platform_details: Some(hresult_details(&error)),
    }
}

pub(crate) fn bluetooth_event_error(context: &str, error: BluetoothError) -> BleEventError {
    let category = match error {
        BluetoothError::RadioNotAvailable => BleErrorCategory::AdapterUnavailable,
        BluetoothError::ResourceInUse => BleErrorCategory::InvalidState,
        BluetoothError::DisabledByUser => BleErrorCategory::AdapterUnavailable,
        BluetoothError::DisabledByPolicy | BluetoothError::ConsentRequired => {
            BleErrorCategory::Permission
        }
        BluetoothError::NotSupported | BluetoothError::TransportNotSupported => {
            BleErrorCategory::NotSupported
        }
        _ => BleErrorCategory::Backend,
    };
    BleEventError {
        category,
        message: format!("{context}: Windows Bluetooth error {}", error.0),
        platform_details: Some(PlatformErrorDetails {
            platform: Platform::Windows,
            code: Some(error.0.to_string()),
            name: Some(format!("{error:?}")),
            message: None,
        }),
    }
}

pub(crate) fn cleanup_error(context: &str, error: Error) -> BleHubError {
    BleHubError::BackendCleanup {
        detail: format!("{context}: {}", error.message()),
        platform_details: Some(hresult_details(&error)),
    }
}
