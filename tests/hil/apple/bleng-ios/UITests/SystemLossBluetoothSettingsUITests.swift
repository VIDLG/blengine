import Foundation
import XCTest

final class SystemLossBluetoothSettingsUITests: XCTestCase {
    private var coordination: URL!
    private var runId: String!
    private var actions: [[String: Any]] = []
    private var started = Date()
    private var mutationAttempted = false
    private var restored = false
    private var probe: XCUIApplication!

    override func setUpWithError() throws {
        try super.setUpWithError()
        continueAfterFailure = false
        let preflight = ProcessInfo.processInfo.environment["BLEHUB_SYSTEM_LOSS_UI_PREFLIGHT"] == "1"
        guard preflight || ProcessInfo.processInfo.environment["BLEHUB_SYSTEM_LOSS_UI_RUN"] == "1" else {
            throw XCTSkip("system-loss UI control is explicit opt-in")
        }
        guard preflight || ProcessInfo.processInfo.environment["BLEHUB_SYSTEM_LOSS_UI_COORDINATION"] != nil else {
            throw XCTSkip("system-loss UI coordination inputs are missing")
        }
        guard let path = ProcessInfo.processInfo.environment["BLEHUB_SYSTEM_LOSS_UI_COORDINATION"],
              let run = ProcessInfo.processInfo.environment["BLEHUB_SYSTEM_LOSS_UI_RUN_ID"],
              !path.isEmpty, UUID(uuidString: run) != nil else {
            if preflight { return }
            throw XCTSkip("system-loss UI coordination inputs are missing")
        }
        guard let appBundleId = ProcessInfo.processInfo.environment["BLEHUB_SYSTEM_LOSS_UI_APP_BUNDLE_ID"],
              !appBundleId.isEmpty else {
            if preflight { return }
            throw XCTSkip("system-loss UI App bundle identity is missing")
        }
        let documents = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        coordination = path == "__TEST_RUNNER_DOCUMENTS__"
            ? documents.appendingPathComponent("bleng-rf-\(run)", isDirectory: true)
            : URL(fileURLWithPath: path, isDirectory: true)
        runId = run
        if path == "__TEST_RUNNER_DOCUMENTS__" {
            try FileManager.default.createDirectory(
                at: coordination, withIntermediateDirectories: true
            )
        }
        try require(coordination.hasDirectoryPath && FileManager.default.fileExists(atPath: coordination.path),
                     "private system-loss coordination directory is unavailable")
        started = Date()
        probe = XCUIApplication(bundleIdentifier: appBundleId)
    }

    func testSystemLossSettingsAutomationPreflight() throws {
        try require(
            ProcessInfo.processInfo.environment["BLEHUB_SYSTEM_LOSS_UI_PREFLIGHT"] == "1",
            "UI automation preflight was not explicitly enabled"
        )
        let settings = BluetoothSettingsControl()
        let radio = try settings.open()
        let toggle = radio.children(matching: .switch).firstMatch
        try require(
            toggle.waitForExistence(timeout: 10)
                && toggle.isHittable
                && toggle.value as? String == "1",
            "Bluetooth Settings did not read back ON during UI automation preflight"
        )
    }

    func testSystemLossBluetoothOffAndAuthorizedOn() throws {
        var primary: Error?
        do {
            // Establish the read-only initial ON observation before launching the
            // Central. This keeps Settings navigation outside the provider's
            // short ARM-originated OFF budget.
            let settings = BluetoothSettingsControl()
            try settings.open()
            _ = try settings.set(enabled: true)
            record("initial-on", value: "1")
            try publish()

            try launchProbe()
            let pending = probe.staticTexts["BLeng: system-loss pending"]
            try require(
                pending.waitForExistence(timeout: 300),
                "BLeng HIL App did not reach pending witness before OFF authorization"
            )
            try waitForHostMarker("settings-ready", action: "settings-ready")
            _ = try settings.activateCurrentPage()
            try publishSettingsReady()
            try waitForHostMarker("off-authorized", action: "off-authorized")
            _ = try settings.set(enabled: false)
            record("off", value: "0")
            mutationAttempted = true
            try publish()
            try waitForHostRestoreAuthorization()
            _ = try settings.activateCurrentPage()
            _ = try settings.set(enabled: true)
            record("restored-on", value: "1")
            restored = true
            try publish()

            // Keep the same production App in the foreground after ON so it
            // can consume the host restore command and perform same-owner
            // reconnect/discovery. Host evidence, not this label, remains the
            // recovery verdict.
            probe.activate()
            let finished = probe.staticTexts["BLeng: system-loss finished; gate pending"]
            try require(
                finished.waitForExistence(timeout: 300),
                "BLeng HIL App did not remain alive through same-owner recovery"
            )
        } catch {
            primary = error
            try? publish()
            throw error
        }
        if primary == nil && !restored {
            throw NSError(domain: "BleHubSystemLossUI", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "Bluetooth was not restored"])
        }
    }

    private func launchProbe() throws {
        func environment(_ name: String) throws -> String {
            guard let value = ProcessInfo.processInfo.environment[name], !value.isEmpty else {
                throw NSError(domain: "BleHubSystemLossUI", code: 4,
                              userInfo: [NSLocalizedDescriptionKey: "missing \(name)"])
            }
            return value
        }
        let scenario = try environment("BLEHUB_SYSTEM_LOSS_CASE")
        let service = try environment("BLEHUB_SYSTEM_LOSS_SERVICE_UUID")
        let prefix = try environment("BLEHUB_SYSTEM_LOSS_NAME_PREFIX")
        let before = try environment("BLEHUB_SYSTEM_LOSS_WRITE_BEFORE")
        let requested = try environment("BLEHUB_SYSTEM_LOSS_WRITE_REQUESTED")
        probe.launchArguments = [
            "--run-id", runId,
            "--rf-system-loss-active", scenario,
            "--rf-system-loss-service-uuid", service,
            "--rf-system-loss-name-prefix", prefix,
            "--rf-system-loss-write-before", before,
            "--rf-system-loss-write-requested", requested,
        ]
        probe.launch()
        try require(probe.waitForExistence(timeout: 30), "BLeng HIL App did not launch")
    }



    private func record(_ action: String, value: String) {
        actions.append([
            "action": action,
            "elapsed_seconds": Date().timeIntervalSince(started),
            "switch_value": value,
        ])
    }

    private func waitForHostRestoreAuthorization() throws {
        try waitForHostMarker("restore-on-authorized", action: "restore-on-authorized")
    }

    private func publishSettingsReady() throws {
        let document: [String: Any] = [
            "schema_version": 1,
            "run_id": runId as Any,
            "action": "settings-ready",
        ]
        let data = try JSONSerialization.data(withJSONObject: document,
                                               options: [.sortedKeys, .withoutEscapingSlashes])
        let temporary = coordination.appendingPathComponent(".settings-ready.tmp")
        let destination = coordination.appendingPathComponent("settings-ready")
        try data.write(to: temporary, options: .atomic)
        _ = try? FileManager.default.removeItem(at: destination)
        try FileManager.default.moveItem(at: temporary, to: destination)
    }

    private func waitForHostMarker(_ name: String, action: String) throws {
        let marker = coordination.appendingPathComponent(name)
        let deadline = Date().addingTimeInterval(300)
        while Date() < deadline {
            if FileManager.default.fileExists(atPath: marker.path) {
                let raw = try Data(contentsOf: marker)
                let object = try JSONSerialization.jsonObject(with: raw) as? [String: Any]
                try require(object?["schema_version"] as? Int == 1
                    && object?["run_id"] as? String == runId
                    && object?["action"] as? String == action,
                    "stale/foreign \(action) authorization")
                return
            }
            RunLoop.current.run(until: Date().addingTimeInterval(0.2))
        }
        throw NSError(domain: "BleHubSystemLossUI", code: 2,
                      userInfo: [NSLocalizedDescriptionKey: "host \(action) authorization timed out"])
    }

    private func publish() throws {
        let document: [String: Any] = [
            "schema_version": 1,
            "scope": "ios-bleng-system-loss-ui",
            "run_id": runId as Any,
            "status": restored ? "restored" : (mutationAttempted ? "off-readback" : "initial-on"),
            "mutation_attempted": mutationAttempted,
            "restored": restored,
            "actions": actions,
        ]
        let data = try JSONSerialization.data(withJSONObject: document,
                                               options: [.sortedKeys, .withoutEscapingSlashes])
        let temporary = coordination.appendingPathComponent(".system-loss-ui.json.tmp")
        let destination = coordination.appendingPathComponent("system-loss-ui.json")
        try data.write(to: temporary, options: .atomic)
        _ = try? FileManager.default.removeItem(at: destination)
        try FileManager.default.moveItem(at: temporary, to: destination)
    }

    private func require(_ condition: Bool, _ message: String) throws {
        guard condition else {
            throw NSError(domain: "BleHubSystemLossUI", code: 3,
                          userInfo: [NSLocalizedDescriptionKey: message])
        }
    }
}
