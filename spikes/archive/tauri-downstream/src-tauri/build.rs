fn main() {
    tauri_build::try_build(
        tauri_build::Attributes::new().plugin(
            "blehub",
            tauri_build::InlinedPlugin::new()
                .commands(&[
                    "backend_mode",
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
                    "advance_fake_callbacks",
                    "run_basic_probe",
                    "close",
                ])
                .default_permission(tauri_build::DefaultPermissionRule::AllowAllCommands),
        ),
    )
    .expect("failed to generate BleHub plugin permissions");

    #[cfg(target_os = "macos")]
    swift_rs::SwiftLinker::new("13.0")
        .with_package(
            "BleHubTauriMacOSAssembly",
            "../plugins/blehub/assembly/macos",
        )
        .link();
}
