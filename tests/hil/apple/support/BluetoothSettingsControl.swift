import XCTest

/// One audited Settings navigation/control path shared by the BLeng UI tests.
///
/// iOS Settings exposes several switches on its root page, including Airplane
/// Mode. Never use `firstMatch`: only the Bluetooth page switch with identifier
/// `BLUETOOTH` is accepted.
final class BluetoothSettingsControl {
    enum Error: Swift.Error {
        case failed(String)
    }

    let application: XCUIApplication

    init(application: XCUIApplication = XCUIApplication(bundleIdentifier: "com.apple.Preferences")) {
        self.application = application
    }

    @discardableResult
    func open() throws -> XCUIElement {
        application.activate()
        try require(
            application.wait(for: .runningForeground, timeout: 10),
            "Settings did not reach runningForeground; no blind taps permitted"
        )
        try require(
            application.navigationBars.firstMatch.waitForExistence(timeout: 15),
            "Settings did not expose its navigation hierarchy; no blind taps permitted"
        )
        try returnToSettingsRoot()

        let radio = application.switches["BLUETOOTH"]
        if radio.exists && radio.isHittable {
            return radio
        }

        let entries = application.descendants(matching: .any)
            .matching(identifier: "com.apple.settings.bluetooth")
        let sidebar = application.collectionViews["com.apple.settings.sidebar.collectionView"]
        let deadline = Date().addingTimeInterval(20)
        var entry: XCUIElement?
        var scrollAttempts = 0
        while Date() < deadline {
            if entries.count == 1 {
                let candidate = entries.element(boundBy: 0)
                if candidate.isHittable {
                    entry = candidate
                    break
                }
            }
            if scrollAttempts < 6 && sidebar.exists && sidebar.isHittable {
                sidebar.swipeDown()
                scrollAttempts += 1
            }
            // Settings can publish a unique row outside the visible viewport;
            // use its stable collection view for bounded semantic scrolling.
            RunLoop.current.run(until: Date().addingTimeInterval(0.5))
        }
        try require(
            entries.count == 1,
            "Bluetooth Settings row identifier was not unique or not found"
        )
        try require(entry?.isHittable == true,
                    "Bluetooth Settings row did not become hittable")
        entry?.tap()

        try require(
            radio.waitForExistence(timeout: 10)
                && radio.isHittable
                && radio.identifier == "BLUETOOTH",
            "Bluetooth Settings page did not expose its named switch"
        )
        return radio
    }

    private func returnToSettingsRoot() throws {
        for _ in 0..<8 {
            let back = application.buttons["BackButton"]
            guard back.exists else { return }
            try require(back.isHittable, "Settings back navigation is not hittable")
            back.tap()
            RunLoop.current.run(until: Date().addingTimeInterval(0.5))
        }
        throw Error.failed("Settings did not return to its root page")
    }

    /// Reactivate the already-open Bluetooth page without navigating through
    /// Settings root. The system-loss provider has a bounded post-authorization
    /// retirement window, so repeating root navigation here can consume it.
    @discardableResult
    func activateCurrentPage() throws -> XCUIElement {
        application.activate()
        try require(
            application.wait(for: .runningForeground, timeout: 10),
            "Settings did not return to the current Bluetooth page"
        )
        let radio = application.switches["BLUETOOTH"]
        try require(
            radio.waitForExistence(timeout: 5)
                && radio.isHittable
                && radio.identifier == "BLUETOOTH",
            "current Bluetooth Settings page is not available; refusing root navigation"
        )
        return radio
    }

    @discardableResult
    func set(enabled: Bool) throws -> String {
        let radio = application.switches["BLUETOOTH"]
        let toggle = radio.children(matching: .switch).firstMatch
        let expected = enabled ? "1" : "0"
        try require(
            radio.waitForExistence(timeout: 10)
                && radio.isHittable
                && radio.identifier == "BLUETOOTH"
                && toggle.waitForExistence(timeout: 10)
                && toggle.isHittable
                && ["0", "1"].contains(toggle.value as? String ?? ""),
            "Bluetooth switch is not safely controllable"
        )
        if toggle.value as? String == expected {
            return expected
        }
        toggle.tap()
        // iOS may complete a global Bluetooth transition well after the Settings
        // tap has returned; keep one bounded readback window without retrying it.
        let deadline = Date().addingTimeInterval(90)
        while Date() < deadline {
            if toggle.value as? String == expected {
                return expected
            }
            RunLoop.current.run(until: Date().addingTimeInterval(0.1))
        }
        throw Error.failed(
            "Bluetooth Settings did not read back \(enabled ? "ON" : "OFF"); "
                + "observed=\(toggle.value as? String ?? "unknown")"
        )
    }

    private func require(_ condition: Bool, _ message: String) throws {
        guard condition else { throw Error.failed(message) }
    }
}
