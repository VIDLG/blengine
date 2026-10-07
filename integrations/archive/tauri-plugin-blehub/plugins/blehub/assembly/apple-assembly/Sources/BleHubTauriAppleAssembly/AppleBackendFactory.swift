import BleHubApple
import BleHubUniFfi
import SwiftRs
#if os(iOS)
import UIKit
#elseif os(macOS)
import AppKit
#endif

@_cdecl("blehub_tauri_apple_open_settings")
public func bleHubTauriAppleOpenSettings() -> Int32 {
    #if os(iOS)
    guard let url = URL(string: UIApplication.openSettingsURLString) else { return -1 }
    UIApplication.shared.open(url)
    return 0
    #elseif os(macOS)
    guard let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Bluetooth") else {
        return -1
    }
    return NSWorkspace.shared.open(url) ? 0 : -1
    #else
    return -1
    #endif
}

/// The Apple assembly owns construction of the platform backend only.
/// BleHub ownership remains in the Rust Tauri plugin.
public enum AppleBackendFactory {
    public static func make() throws -> AppleBleBackend {
        try AppleBleBackend(queueLabel: "dev.blehub.tauri.blehubinternal.apple")
    }
}


/// Requests one canonical Rust-owned BleHub assembly. The ticket is only an
/// assembly correlation value; it is not used for BLE operation routing.
@_cdecl("blehub_tauri_apple_assemble")
public func bleHubTauriAppleAssemble(_ ticket: UInt64) -> Int32 {
    do {
        let backend = try AppleBackendFactory.make()
        do {
            try attachBackend(ticket: ticket, backend: backend)
            return 0
        } catch {
            // The Rust constructor is rollback-safe. This extra call is
            // idempotent and covers rejection before ownership transfer.
            try? backend.dispose()
            return -1
        }
    } catch {
        return -1
    }
}
