const COMMANDS: &[&str] = &[
    "capabilities",
    "initialize",
    "start_scan",
    "stop_scan",
    "connect",
    "disconnect",
    "discover_gatt",
    "read_characteristic",
    "write_characteristic",
    "set_notifications",
    "read_rssi",
    "close",
];

fn main() {
    let target = std::env::var("CARGO_CFG_TARGET_OS").expect("Cargo target OS");
    let native = std::env::var_os("CARGO_FEATURE_NATIVE").is_some();
    let mut builder = tauri_plugin::Builder::new(COMMANDS);
    if target == "ios" && native {
        // The official Tauri mobile builder owns iOS Swift linking. It calls
        // link_apple_library(CARGO_PKG_NAME, path), which in turn invokes
        // SwiftLinker exactly once for this package.
        builder = builder.ios_path("assembly/apple-assembly");
        #[cfg(target_os = "macos")]
        tauri_plugin::mobile::update_info_plist(|plist| {
            plist.insert(
                "NSBluetoothAlwaysUsageDescription".into(),
                "BleHub uses Bluetooth to discover and communicate with nearby devices.".into(),
            );
        })
        .expect("failed to update the iOS Bluetooth usage description");
    }
    builder.build();

    // macOS has no Tauri mobile builder path, so the plugin build script is
    // the single linker owner there. iOS is intentionally excluded: ios_path
    // above owns its Swift build/link step.
    if target == "macos" && native {
        #[cfg(any(target_os = "macos", target_os = "ios"))]
        swift_rs::SwiftLinker::new("13.0")
            .with_ios("16.0")
            .with_package("tauri-plugin-blehub", "assembly/apple-assembly")
            .link();
    }
}
