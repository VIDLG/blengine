import BleHubApple
import BleHubUniFfi
import SwiftRs

/// The macOS assembly owns construction of the platform backend only.
/// BleHub ownership remains in the Rust Tauri plugin.
public enum AppleBackendFactory {
    public static func make() throws -> AppleBleBackend {
        try AppleBleBackend(queueLabel: "dev.blehub.tauri.spike.apple")
    }
}

/// Narrow C-compatible launcher used only to prove the Tauri host can link
/// the plugin-owned Swift assembly. BLE operations stay on the UniFFI ABI.
@_cdecl("blehub_tauri_macos_assembly_probe")
public func bleHubTauriMacosAssemblyProbe() -> Int32 {
    do {
        let backend = try AppleBackendFactory.make()
        defer { try? backend.dispose() }
        return Int32(try backend.capabilities().contractVersion)
    } catch {
        return -1
    }
}

/// Requests one canonical Rust-owned BleHub assembly. The ticket is only an
/// assembly correlation value; it is not used for BLE operation routing.
@_cdecl("blehub_tauri_macos_assemble")
public func bleHubTauriMacosAssemble(_ ticket: UInt64) -> Int32 {
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
