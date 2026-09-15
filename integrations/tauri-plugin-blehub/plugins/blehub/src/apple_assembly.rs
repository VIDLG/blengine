use swift_rs::{Int32, UInt64, swift};

swift!(fn blehub_tauri_apple_assemble(ticket: UInt64) -> Int32);
swift!(fn blehub_tauri_apple_open_settings() -> Int32);

pub(super) fn open_settings() -> Result<(), String> {
    let result = unsafe { blehub_tauri_apple_open_settings() };
    if result < 0 {
        Err("Apple system settings could not be opened".to_owned())
    } else {
        Ok(())
    }
}

pub(super) fn assemble(ticket: u64) -> Result<(), String> {
    let result = unsafe { blehub_tauri_apple_assemble(ticket) };
    if result < 0 {
        Err("Apple Swift assembly could not attach AppleBleBackend".to_owned())
    } else {
        Ok(())
    }
}
