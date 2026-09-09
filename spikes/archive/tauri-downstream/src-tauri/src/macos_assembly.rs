#[cfg(target_os = "macos")]
mod native {
    use swift_rs::{Int32, UInt64, swift};

    swift!(fn blehub_tauri_macos_assembly_probe() -> Int32);
    swift!(fn blehub_tauri_macos_assemble(ticket: UInt64) -> Int32);

    pub(super) fn probe() -> Result<u32, String> {
        let result = unsafe { blehub_tauri_macos_assembly_probe() };
        if result < 0 {
            Err("macOS Swift assembly could not construct AppleBleBackend".to_owned())
        } else {
            Ok(result as u32)
        }
    }

    pub(super) fn assemble(ticket: u64) -> Result<(), String> {
        let result = unsafe { blehub_tauri_macos_assemble(ticket) };
        if result < 0 {
            Err("macOS Swift assembly could not attach AppleBleBackend".to_owned())
        } else {
            Ok(())
        }
    }
}

#[cfg(target_os = "macos")]
pub fn probe() -> Result<u32, String> {
    native::probe()
}

#[cfg(target_os = "macos")]
pub fn assemble(ticket: u64) -> Result<(), String> {
    native::assemble(ticket)
}

#[cfg(not(target_os = "macos"))]
pub fn probe() -> Result<u32, String> {
    Err("macOS Swift assembly is only available on macOS".to_owned())
}
