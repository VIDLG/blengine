import BleHubTauriMacOSAssembly
import BleHubApple
import BleHubUniFfi
import XCTest

final class AppleBackendFactoryTests: XCTestCase {
    func testConstructsTheProductionAppleBackend() throws {
        let backend = try AppleBackendFactory.make()
        let capabilities = try backend.capabilities()

        XCTAssertEqual(capabilities.platform, .apple)
        XCTAssertEqual(capabilities.contractVersion, AppleBleBackend.contractVersion)
        try backend.dispose()
    }

    func testAttachRejectsAnUnknownTicket() throws {
        let backend = try AppleBackendFactory.make()
        XCTAssertThrowsError(
            try attachBackend(ticket: UInt64.max, backend: backend)
        )
        try backend.dispose()
    }
}
