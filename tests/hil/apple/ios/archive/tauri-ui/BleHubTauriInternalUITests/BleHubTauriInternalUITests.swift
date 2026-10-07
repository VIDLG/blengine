import XCTest

final class BleHubTauriInternalUITests: XCTestCase {
    private let bundleIdentifier = "dev.blehub.tauri.blehubinternal"

    func testTauriApplicationLaunches() {
        let app = XCUIApplication(bundleIdentifier: bundleIdentifier)
        app.launch()

        XCTAssertTrue(
            app.staticTexts["Central contract lab"].waitForExistence(timeout: 20),
            "The signed Tauri iOS app did not expose its root UI"
        )
    }

    func testTauriNrfRfFlow() throws {
        try XCTSkipUnless(
            ProcessInfo.processInfo.environment["BLEHUB_IOS_TAURI_RF"] == "1",
            "Requires the explicitly enabled iOS nRF RF harness"
        )
        continueAfterFailure = false
        let app = XCUIApplication(bundleIdentifier: bundleIdentifier)
        defer { app.terminate() }
        app.launch()
        let initialize = app.buttons["Initialize CoreBluetooth"]
        XCTAssertTrue(initialize.waitForExistence(timeout: 30))
        initialize.tap()

        let flow = app.buttons["Run CoreBluetooth flow"]
        XCTAssertTrue(
            flow.waitForExistence(timeout: 45),
            "CoreBluetooth initialization did not expose the flow button. "
                + "Check the iPhone Bluetooth permission prompt.\n\(app.debugDescription)"
        )
        flow.tap()

        XCTAssertTrue(
            app.staticTexts["CoreBluetooth flow passed"].waitForExistence(timeout: 180),
            "The signed iOS Tauri nRF RF flow did not complete"
        )
        XCTAssertTrue(
            app.staticTexts["12/12"].waitForExistence(timeout: 10),
            "The iOS Tauri RF flow did not complete all 12 contract steps"
        )
    }

    func testBluetoothPermissionGuidanceIsActionableWhenDisplayed() {
        let app = XCUIApplication(bundleIdentifier: bundleIdentifier)
        app.launch()

        guard app.staticTexts["Bluetooth permission is turned off"].waitForExistence(timeout: 5) else {
            // Permission may already be authorized or not determined on the device.
            return
        }

        XCTAssertTrue(app.buttons["Open Bluetooth settings"].exists)
        XCTAssertTrue(
            app.staticTexts["Settings → Privacy & Security → Bluetooth → BleHub Tauri Internal"].exists
        )
    }
}
