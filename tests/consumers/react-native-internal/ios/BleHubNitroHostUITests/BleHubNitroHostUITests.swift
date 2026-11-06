import XCTest

final class BleHubNitroHostUITests: XCTestCase {
    private var authorizationBoots = Set<String>()
    override func setUpWithError() throws {
        continueAfterFailure = false
    }

    @MainActor
    func testContractLabCompletesAndRecreatesProbe() {
        let app = XCUIApplication()
        app.launchArguments = ["--blehub-nitro-basic"]
        app.launch()
        defer { app.terminate() }

        XCTAssertTrue(
            app.staticTexts["contract-lab-title"].waitForExistence(timeout: 30),
            "The signed React Native host did not expose its contract-lab UI"
        )
        assertContractPassed(run: 1, in: app)

        let recreate = app.buttons["recreate-run-button"]
        XCTAssertTrue(recreate.waitForExistence(timeout: 10))
        XCTAssertEqual(recreate.label, "Recreate and run")
        recreate.tap()

        assertContractPassed(run: 2, in: app)
        XCTAssertTrue(app.buttons["close-probe-button"].exists)

        app.buttons["rf-workbench-home"].tap()
        let session = app.staticTexts["rf-workbench-session-mode"]
        XCTAssertTrue(session.waitForExistence(timeout: 10))
        XCTAssertEqual(session.label, "ABI transport smoke")
        XCTAssertEqual(app.staticTexts["rf-workbench-summary"].label, "App checks · Passed")

        app.buttons["rf-workbench-evidence"].tap()
        XCTAssertTrue(app.staticTexts["Host gate · Verify on the host"].waitForExistence(timeout: 10))
        XCTAssertTrue(app.staticTexts["Mode: basic"].exists)

        app.buttons["rf-workbench-current"].tap()
        assertContractPassed(run: 2, in: app)
    }

    private enum AuthorizationCheckError: Error {
        case failed(String)
    }

    @MainActor
    func testBluetoothAuthorizationDenyAndRestore() throws {
        try XCTSkipUnless(ProcessInfo.processInfo.environment["BLEHUB_IOS_PERMISSION_GATE"] == "1",
                          "App-specific Bluetooth authorization reset requires the explicit host permission recipe")
        authorizationBoots.removeAll()
        let app = XCUIApplication()
        let runId = UUID().uuidString.lowercased()
        if ProcessInfo.processInfo.environment["BLEHUB_IOS_PERMISSION_RESTORE_ONLY"] == "1" {
            try restoreBluetoothAuthorization(app, runId: runId, inspectBeforeReset: true)
            let attachment = XCTAttachment(string: "scope=authorization-restoration-only; full-gate-not-run; run_id=\(runId)")
            attachment.name = "permission-restoration-only"
            attachment.lifetime = .keepAlways
            add(attachment)
            return
        }
        var primary: Error?
        var cleanup: Error?
        var changed = false
        var restored = false
        do {
            let initial = try launchAuthorizationPhase("inspect", runId: runId, app: app)
            try requireAuthorization(initial["authorization_at_launch"] as? String == "authorized",
                                     "Initial authorization must already be allowed; no permission was reset")
            _ = try launchAuthorizationPhase("authorized", runId: runId, app: app)
            try awaitAuthorizationPhase(app)
            app.terminate()
            changed = true
            app.resetAuthorizationStatus(for: .bluetooth)
            let reset = try launchAuthorizationPhase("inspect", runId: runId, app: app)
            try requireAuthorization(reset["authorization_at_launch"] as? String == "not_determined",
                                     "Authorization reset did not produce NotDetermined")
            let denied = try launchAuthorizationPhase("denied", runId: runId, app: app, consent: false)
            try requireAuthorization(denied["authorization_at_launch"] as? String == "denied",
                                     "Fresh denied process did not observe Denied")
            try awaitAuthorizationPhase(app)
            let deniedReadback = try launchAuthorizationPhase("inspect", runId: runId, app: app)
            try requireAuthorization(deniedReadback["authorization_at_launch"] as? String == "denied",
                                     "Native authorization did not become Denied")
            try restoreBluetoothAuthorization(app, runId: runId)
            restored = true
        } catch {
            primary = error
        }
        if changed && !restored {
            do {
                try restoreBluetoothAuthorization(app, runId: runId, inspectBeforeReset: true)
                restored = true
            } catch { cleanup = error }
        }
        let record: [String: Any] = ["run_id": runId, "scope": "ios-authorization-only-non-rf",
            "status": primary == nil && cleanup == nil && restored ? "passed" : "failed",
            "restored": restored, "primary_error": primary.map(String.init(describing:)) ?? "",
            "cleanup_error": cleanup.map(String.init(describing:)) ?? ""]
        let data = try JSONSerialization.data(withJSONObject: record, options: [.sortedKeys])
        let attachment = XCTAttachment(data: data, uniformTypeIdentifier: "public.json")
        attachment.name = "ios-permission-host-verdict"
        attachment.lifetime = .keepAlways
        add(attachment)
        if let primary { XCTFail("Primary authorization failure: \(primary); restoration failure: \(String(describing: cleanup))") }
        else if let cleanup { XCTFail("Authorization restoration failed: \(cleanup)") }
        XCTAssertTrue(restored)
        // Leave the fresh authorized result visible; do not terminate the successful App.
    }

    // Control feasibility only. No live Probe remains while Settings changes the radio.
    @MainActor
    func testBluetoothSettingsOffAndRestore() throws {
        try XCTSkipUnless(ProcessInfo.processInfo.environment["BLEHUB_IOS_RADIO_CONTROL_GATE"] == "1",
                          "Global radio mutation requires the explicit Settings-control recipe")
        authorizationBoots.removeAll()
        let app = XCUIApplication()
        let settings = XCUIApplication(bundleIdentifier: "com.apple.Preferences")
        if ProcessInfo.processInfo.environment["BLEHUB_IOS_RADIO_CONTROL_RESTORE_ONLY"] == "1" {
            settings.activate()
            let foreground = settings.wait(for: .runningForeground, timeout: 10)
            try requireAuthorization(foreground, "Settings did not reach runningForeground; no blind taps permitted")
            let radio = try openBluetoothSettings(settings)
            try setBluetoothSwitch(settings, enabled: true)
            attachBluetoothSettings(settings, action: "restore-only-on")
            let note = XCTAttachment(string: "scope=settings-bluetooth-switch-restoration-only; switch_value=1; one-on-only")
            note.name = "ios-radio-restoration-only"
            note.lifetime = .keepAlways
            add(note)
            return
        }
        if ProcessInfo.processInfo.environment["BLEHUB_IOS_RADIO_CONTROL_INSPECT_ONLY"] == "1" {
            defer {
                if app.state == .runningBackground || app.state == .runningBackgroundSuspended { app.activate() }
            }
            settings.activate()
            let foreground = settings.wait(for: .runningForeground, timeout: 10)
            let visible = foreground && settings.navigationBars.firstMatch.waitForExistence(timeout: 15)
            attachBluetoothSettings(settings, action: "inspection-only-no-mutation")
            try requireAuthorization(foreground, "Settings did not reach runningForeground; no mutation attempted")
            try requireAuthorization(visible, "Foreground Settings did not expose its navigation hierarchy; no mutation attempted")
            let radio = try openBluetoothSettings(settings)
            attachBluetoothSettings(settings, action: "toggle-inspection-only-no-mutation")
            let note = XCTAttachment(string: "scope=settings-bluetooth-switch-inspection-only; switch_value=\(radio.value as? String ?? "unknown"); radio-control-gate-not-run")
            note.name = "ios-radio-inspection-only"
            note.lifetime = .keepAlways
            add(note)
            return
        }
        let runId = UUID().uuidString.lowercased()
        let started = ProcessInfo.processInfo.systemUptime
        var observations = [[String: Any]]()
        var primary: Error?
        var cleanup: Error?
        var changed = false
        var restored = false
        var sameBoot = false
        func observe(_ action: String) {
            observations.append(["action": action,
                "elapsed_seconds": ProcessInfo.processInfo.systemUptime - started])
        }
        do {
            let initial = try launchAuthorizationPhase("inspect", runId: runId, app: app)
            try requireAuthorization(initial["authorization_at_launch"] as? String == "authorized",
                                     "Initial App authorization must be Authorized; no permission reset is permitted")
            let baseline = try launchAuthorizationPhase("authorized", runId: runId, app: app)
            try awaitAuthorizationPhase(app)
            observe("authorized-powered-on-probe-closed")
            let radio = try openBluetoothSettings(settings)
            try requireAuthorization(radio.value as? String == "1", "Initial Settings Bluetooth switch must be ON")
            attachBluetoothSettings(settings, action: "initial-on")
            observe("settings-initial-on")
            // Set before tapping: an ambiguous/failed tap still requires an ON restoration attempt.
            changed = true
            try setBluetoothSwitch(settings, enabled: false)
            attachBluetoothSettings(settings, action: "off-readback")
            observe("settings-off")
            try setBluetoothSwitch(settings, enabled: true)
            attachBluetoothSettings(settings, action: "on-readback")
            observe("settings-restored-on")
            restored = true
            try requireAuthorization(app.state == .runningBackground || app.state == .runningBackgroundSuspended,
                                     "The audited App stopped while Settings was foreground; no relaunch is allowed")
            app.activate()
            let identity = app.staticTexts["permission-native-identity"]
            try requireAuthorization(identity.waitForExistence(timeout: 10), "Original App identity did not return")
            guard let data = identity.label.data(using: .utf8),
                  let returned = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                throw AuthorizationCheckError.failed("Invalid returned App identity")
            }
            sameBoot = NSDictionary(dictionary: baseline).isEqual(to: returned)
            try requireAuthorization(sameBoot, "App native boot/PID changed during Settings control")
            try awaitAuthorizationPhase(app)
            observe("original-closed-probe-result-visible")
        } catch {
            primary = error
            attachBluetoothSettings(settings, action: "primary-failure")
        }
        if changed && !restored {
            do {
                let radio = try openBluetoothSettings(settings)
                try setBluetoothSwitch(settings, enabled: true)
                attachBluetoothSettings(settings, action: "cleanup-on-readback")
                observe("cleanup-restored-on")
                restored = true
            } catch { cleanup = error }
        }
        let record: [String: Any] = ["run_id": runId, "scope": "ios-settings-radio-control-feasibility-non-rf",
            "status": primary == nil && cleanup == nil && restored && sameBoot ? "passed" : "failed",
            "restored": restored, "mutation_attempted": changed, "same_native_boot": sameBoot, "observations": observations,
            "powered_off_backend_proven": false, "rf_recovery_proven": false,
            "primary_error": primary.map(String.init(describing:)) ?? "",
            "cleanup_error": cleanup.map(String.init(describing:)) ?? ""]
        let data = try JSONSerialization.data(withJSONObject: record, options: [.sortedKeys])
        let attachment = XCTAttachment(data: data, uniformTypeIdentifier: "public.json")
        attachment.name = "ios-radio-control-host-verdict"
        attachment.lifetime = .keepAlways
        add(attachment)
        if !changed && (app.state == .runningBackground || app.state == .runningBackgroundSuspended) { app.activate() }
        if let primary { XCTFail("Primary Settings-control failure: \(primary); restoration failure: \(String(describing: cleanup))") }
        else if let cleanup { XCTFail("Bluetooth ON restoration failed: \(cleanup)") }
        XCTAssertTrue(restored && sameBoot)
    }

    // Transport-only preflight. It never opens Settings, CoreBluetooth, a Probe or a fixture.
    @MainActor
    func testAdapterReadyTransportWithoutRadio() throws {
        let environment = ProcessInfo.processInfo.environment
        try XCTSkipUnless(environment["BLEHUB_IOS_ADAPTER_HANDSHAKE_GATE"] == "1",
                          "Adapter handshake transport requires the explicit non-RF host recipe")
        let runId = environment["BLEHUB_IOS_ADAPTER_HANDSHAKE_RUN_ID"] ?? ""
        let nonce = UUID().uuidString.lowercased()
        try requireAuthorization(adapterCanonicalUUID(runId),
                                 "BLEHUB_IOS_ADAPTER_HANDSHAKE_RUN_ID must be a canonical UUID")
        let payload: [String: Any] = [
            "schema_version": 1,
            "run_id": runId,
            "nonce": nonce,
            "source": "ios-ui-handshake",
            "test_process_id": ProcessInfo.processInfo.processIdentifier
        ]
        let data = try JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys, .withoutEscapingSlashes])
        guard let json = String(data: data, encoding: .utf8) else {
            throw AuthorizationCheckError.failed("Handshake marker JSON encoding failed")
        }
        print("BLEHUB_IOS_ADAPTER_HANDSHAKE_READY " + json)
        let attachment = XCTAttachment(string: "scope=ios-adapter-handshake; radio-control-gate-not-run; run_id=\(runId)")
        attachment.name = "ios-adapter-handshake"
        attachment.lifetime = .keepAlways
        add(attachment)
    }

    // One uninterrupted XCTest control window, not a persistent result-page or host RF verdict.
    @MainActor
    func testBluetoothStartupOffAndFreshProbeRecovery() throws {
        let environment = ProcessInfo.processInfo.environment
        try XCTSkipUnless(environment["BLEHUB_IOS_ADAPTER_STARTUP_GATE"] == "1",
                          "Startup-off radio mutation requires explicit host authorization")
        let runId = environment["BLEHUB_IOS_ADAPTER_RUN_ID"] ?? ""
        let service = environment["BLEHUB_IOS_ADAPTER_SERVICE_UUID"] ?? ""
        let localName = environment["BLEHUB_IOS_ADAPTER_LOCAL_NAME"] ?? ""
        let receipt = environment["BLEHUB_IOS_ADAPTER_FIXTURE_RECEIPT_SHA256"] ?? ""
        let started = ProcessInfo.processInfo.systemUptime
        var observations = [[String: Any]]()
        var primary: Error?
        var cleanup: Error?
        var changed = false
        var restored = false
        var sameSession = false
        var complete = false
        let app = XCUIApplication()
        let settings = XCUIApplication(bundleIdentifier: "com.apple.Preferences")
        func observe(_ action: String, identity: [String: Any]? = nil, switchValue: String? = nil) {
            observations.append(["action": action,
                "elapsed_seconds": ProcessInfo.processInfo.systemUptime - started,
                "identity": identity.map { $0 as Any } ?? NSNull(),
                "switch_value": switchValue.map { $0 as Any } ?? NSNull()])
        }
        do {
            // Validate every input before even the authorization-inspection launch.
            try requireAuthorization(adapterCanonicalUUID(runId), "BLEHUB_IOS_ADAPTER_RUN_ID must be a canonical UUID")
            try requireAuthorization(adapterCanonicalUUID(service), "BLEHUB_IOS_ADAPTER_SERVICE_UUID must be a canonical UUID")
            try requireAuthorization(!localName.isEmpty && localName.utf8.count <= 128 &&
                localName == localName.trimmingCharacters(in: .whitespacesAndNewlines) &&
                !localName.hasPrefix("--") &&
                !localName.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) }),
                "BLEHUB_IOS_ADAPTER_LOCAL_NAME must be bounded, trimmed, control-free and accepted by the launch parser")
            try requireAuthorization(receipt.utf8.count == 64 &&
                receipt.range(of: "^[0-9a-f]{64}\\z", options: .regularExpression) != nil,
                "BLEHUB_IOS_ADAPTER_FIXTURE_RECEIPT_SHA256 must be lowercase 64-hex")
            authorizationBoots.removeAll()
            let inspection = try launchAuthorizationPhase("inspect", runId: runId, app: app)
            try requireAuthorization(inspection["authorization_at_launch"] as? String == "authorized",
                                     "Initial authorization must already be Authorized; permission changes are forbidden")
            observe("authorization-verified")
            // This inspection process is not the audited adapter session.
            app.terminate()
            try requireAuthorization(app.wait(for: .notRunning, timeout: 10), "Inspection App did not terminate")
            let initialRadio = try openBluetoothSettings(settings)
            try requireAuthorization(initialRadio.value as? String == "1", "Initial Settings Bluetooth switch must be ON")
            attachBluetoothSettings(settings, action: "adapter-initial-on")
            observe("settings-initial-on", switchValue: "1")
            // Even an ambiguous OFF tap requires one bounded ON cleanup attempt. Never retry OFF.
            changed = true
            try setBluetoothSwitch(settings, enabled: false)
            attachBluetoothSettings(settings, action: "adapter-off-readback")
            observe("settings-off", switchValue: "0")

            app.launchArguments = ["--blehub-nitro-rf-adapter-service", service,
                "--blehub-nitro-rf-adapter-run-id", runId,
                "--blehub-nitro-rf-adapter-mode", "startup-off",
                "--blehub-nitro-rf-adapter-local-name", localName]
            app.launch() // The only launch of the audited adapter session.
            try requireAuthorization(waitForLabel("Off: Passed", on: app.staticTexts["rf-adapter-transition-off"], timeout: 30),
                                     "Startup-off Probe did not pass and close")
            try requireAuthorization(waitForLabel("Signal: Awaiting on URL (30s)",
                on: app.staticTexts["rf-adapter-transition-signal"], timeout: 5), "App is not awaiting the host window")
            let baseline = try adapterStartupIdentity(app, runId: runId)
            guard let native = baseline["native"] as? [String: Any] else {
                throw AuthorizationCheckError.failed("Missing validated native adapter identity")
            }
            try requireAuthorization(native["boot_id"] as? String != inspection["boot_id"] as? String,
                                     "Adapter session reused the inspection boot")
            observe("off-probe-closed", identity: baseline)

            let onRadio = try openBluetoothSettings(settings)
            try setBluetoothSwitch(settings, enabled: true)
            attachBluetoothSettings(settings, action: "adapter-on-readback")
            observe("settings-on", switchValue: "1")
            let returned = try returnToAdapterSession(app, runId: runId, baseline: baseline)
            observe("original-session-returned", identity: returned)
            try requireAuthorization(app.staticTexts["rf-adapter-transition-signal"].label == "Signal: Awaiting on URL (30s)",
                                     "Host window was already signalled or expired before the request")
            let request: [String: Any] = ["schema_version": 1, "run_id": runId,
                "fixture_receipt_sha256": receipt, "identity": returned]
            let requestData = try JSONSerialization.data(withJSONObject: request, options: [.sortedKeys, .withoutEscapingSlashes])
            guard let requestJSON = String(data: requestData, encoding: .utf8) else {
                throw AuthorizationCheckError.failed("Host window JSON encoding failed")
            }
            observe("host-window-requested", identity: returned)
            // Only the host guard/CoreDevice may deliver ready. XCTest never opens a URL.
            print("BLEHUB_IOS_ADAPTER_WINDOW_READY " + requestJSON)
            let confirm = app.buttons["rf-adapter-transition-confirm-on"]
            let hostReady = XCTNSPredicateExpectation(predicate: NSPredicate(format: "label == %@",
                "Signal: Host ready · confirm after Settings ON readback"), object: app.staticTexts["rf-adapter-transition-signal"])
            let enabled = XCTNSPredicateExpectation(predicate: NSPredicate(format: "exists == true AND enabled == true AND hittable == true"), object: confirm)
            try requireAuthorization(XCTWaiter.wait(for: [hostReady, enabled], timeout: 15) == .completed,
                                     "Host ready signal and enabled confirmation did not arrive in the bounded window")
            let readyIdentity = try adapterStartupIdentity(app, runId: runId, baseline: baseline)
            observe("host-window-ready", identity: readyIdentity)
            confirm.tap() // Exactly one request; the existing App invokes Linking.openURL itself.
            let requestedIdentity = try adapterStartupIdentity(app, runId: runId, baseline: baseline)
            observe("on-requested", identity: requestedIdentity)
            try awaitAdapterStartupPassed(app)
            let passedIdentity = try adapterStartupIdentity(app, runId: runId, baseline: baseline)
            observe("app-runtime-passed", identity: passedIdentity)

            // An independent final readback, never a successful-path toggle or an intermediate ON claim.
            let finalRadio = try openBluetoothSettings(settings)
            try requireAuthorization(finalRadio.value as? String == "1", "Final Settings Bluetooth switch is not ON")
            restored = true
            attachBluetoothSettings(settings, action: "adapter-final-on")
            observe("settings-final-on", switchValue: "1")
            let finalIdentity = try returnToAdapterSession(app, runId: runId, baseline: baseline)
            try awaitAdapterStartupPassed(app)
            let finalResultIdentity = try adapterStartupIdentity(app, runId: runId, baseline: finalIdentity)
            observe("original-result-returned", identity: finalResultIdentity)
            sameSession = true
            complete = true
        } catch {
            // XCTest snapshot failures are not Swift Errors; keep diagnostics from
            // aborting ON cleanup or publication of the primary failure.
            primary = error
        }
        if changed && primary != nil {
            // Best effort only: system/XCTest failures can prevent this bounded cleanup from executing.
            restored = false
            do {
                let radio = try openBluetoothSettings(settings)
                try setBluetoothSwitch(settings, enabled: true)
                try requireAuthorization(radio.value as? String == "1", "Cleanup Bluetooth ON readback failed")
                restored = true
                observe("cleanup-restored-on", switchValue: "1")
            } catch {
                cleanup = error
            }
        }
        let record: [String: Any] = ["schema_version": 1, "scope": "ios-adapter-startup-control-window",
            "run_id": runId, "fixture_receipt_sha256": receipt,
            "status": primary == nil && cleanup == nil && complete && restored && sameSession ? "passed" : "failed",
            "mutation_attempted": changed, "restored": restored, "same_session": sameSession,
            "observations": observations, "primary_error": primary.map(String.init(describing:)) ?? "",
            "cleanup_error": cleanup.map(String.init(describing:)) ?? ""]
        let data = try JSONSerialization.data(withJSONObject: record, options: [.sortedKeys])
        let attachment = XCTAttachment(data: data, uniformTypeIdentifier: "public.json")
        attachment.name = "ios-adapter-startup-control"
        attachment.lifetime = .keepAlways
        add(attachment)
        if let primary { XCTFail("Primary adapter startup failure: \(primary); cleanup failure: \(String(describing: cleanup))") }
        else if let cleanup { XCTFail("Adapter startup cleanup failed: \(cleanup)") }
        else if !complete || !restored || !sameSession { XCTFail("Adapter startup control window did not complete") }
        // No relaunch/termination here. Test completion may still cause the original App to exit.
    }

    private func adapterCanonicalUUID(_ value: String) -> Bool {
        value.utf8.count == 36 && UUID(uuidString: value)?.uuidString.lowercased() == value
    }

    @MainActor
    private func adapterStartupIdentity(_ app: XCUIApplication, runId: String,
                                        baseline: [String: Any]? = nil) throws -> [String: Any] {
        try requireAuthorization(app.state == .runningForeground, "Audited adapter App is not live in foreground")
        let element = app.staticTexts["rf-adapter-transition-identity"]
        try requireAuthorization(element.waitForExistence(timeout: 10), "Adapter session identity did not appear")
        let label = element.label
        guard label.utf8.count <= 8192, let data = label.data(using: .utf8),
              let identity = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              Set(identity.keys) == Set(["native", "run_id", "nonce"]),
              identity["run_id"] as? String == runId,
              let nonce = identity["nonce"] as? String, nonce.utf8.count <= 128,
              nonce.range(of: "^[0-9a-z]+-[0-9a-z]+-[0-9a-z]+\\z", options: .regularExpression) != nil,
              let native = identity["native"] as? [String: Any],
              Set(native.keys) == Set(["run_id", "adapter_mode", "pid", "boot_id", "bundle_id", "executable", "source"]),
              native["run_id"] as? String == runId, native["adapter_mode"] as? String == "startup-off",
              native["source"] as? String == "launch",
              native["bundle_id"] as? String == "org.reactjs.native.example.BleHubNitroHost",
              let pid = native["pid"] as? NSNumber, String(cString: pid.objCType) != "c",
              pid.doubleValue > 0, pid.doubleValue <= Double(Int32.max), pid.doubleValue.rounded() == pid.doubleValue,
              let boot = native["boot_id"] as? String, adapterCanonicalUUID(boot),
              let executable = native["executable"] as? String,
              executable.range(of: "^file:///[^\\s?#\\\\]+\\z", options: .regularExpression) != nil,
              !executable.hasSuffix("/"), let decoded = executable.removingPercentEncoding,
              !decoded.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) }),
              let url = URL(string: executable), url.isFileURL, url.host == nil || url.host == "",
              !url.path.isEmpty, url.query == nil, url.fragment == nil else {
            throw AuthorizationCheckError.failed("Adapter UI identity has invalid schema/native/run/PID/boot/nonce")
        }
        if let baseline {
            try requireAuthorization(NSDictionary(dictionary: baseline).isEqual(to: identity),
                                     "Audited adapter native identity or JS nonce changed; no relaunch is allowed")
        }
        try requireAuthorization(app.state == .runningForeground, "Audited adapter App exited during identity readback")
        return identity
    }

    @MainActor
    private func returnToAdapterSession(_ app: XCUIApplication, runId: String,
                                        baseline: [String: Any]) throws -> [String: Any] {
        // activate() can launch a missing process: only a live background session may be activated.
        try requireAuthorization(app.state == .runningBackground || app.state == .runningBackgroundSuspended,
                                 "Audited adapter App stopped or is not backgrounded; refusing activate/relaunch")
        app.activate()
        try requireAuthorization(app.wait(for: .runningForeground, timeout: 10), "Original adapter session did not reach foreground")
        return try adapterStartupIdentity(app, runId: runId, baseline: baseline)
    }

    @MainActor
    private func awaitAdapterStartupPassed(_ app: XCUIApplication) throws {
        let result = app.staticTexts["rf-adapter-transition-result"]
        let on = app.staticTexts["rf-adapter-transition-on"]
        let expectations = [
            XCTNSPredicateExpectation(predicate: NSPredicate(format: "label == %@", "Passed"), object: result),
            XCTNSPredicateExpectation(predicate: NSPredicate(format: "label == %@", "On: Passed"), object: on)]
        let passed = XCTWaiter.wait(for: expectations, timeout: 90) == .completed
        let failure = app.staticTexts["rf-adapter-transition-failure"]
        try requireAuthorization(passed && result.label == "Passed" && on.label == "On: Passed",
                                 "Adapter runtime did not pass: \(failure.exists ? failure.label : "failure UI unavailable")")
    }

    @MainActor
    private func openBluetoothSettings(_ settings: XCUIApplication) throws -> XCUIElement {
        return try BluetoothSettingsControl(application: settings).open()
    }

    @MainActor
    private func setBluetoothSwitch(_ settings: XCUIApplication, enabled: Bool) throws {
        _ = try BluetoothSettingsControl(application: settings).set(enabled: enabled)
    }

    @MainActor
    private func attachBluetoothSettings(_ settings: XCUIApplication, action: String) {
        let screenshot = XCTAttachment(screenshot: settings.screenshot())
        screenshot.name = "bluetooth-settings-\(action)"
        screenshot.lifetime = .keepAlways
        add(screenshot)
        let tree = XCTAttachment(string: settings.debugDescription)
        tree.name = "bluetooth-settings-accessibility-\(action)"
        tree.lifetime = .keepAlways
        add(tree)
        let screen = XCTAttachment(screenshot: XCUIScreen.main.screenshot())
        screen.name = "bluetooth-settings-screen-\(action)"
        screen.lifetime = .keepAlways
        add(screen)
        let springboard = XCUIApplication(bundleIdentifier: "com.apple.springboard")
        let context = XCTAttachment(string: "settings_state=\(settings.state.rawValue); target_state=\(XCUIApplication().state.rawValue); springboard_state=\(springboard.state.rawValue)\n" + springboard.debugDescription)
        context.name = "bluetooth-settings-context-\(action)"
        context.lifetime = .keepAlways
        add(context)
    }

    @MainActor
    private func requireAuthorization(_ valid: Bool, _ message: String) throws {
        if !valid { throw AuthorizationCheckError.failed(message) }
    }

    @MainActor
    private func launchAuthorizationPhase(_ phase: String, runId: String, app: XCUIApplication, consent: Bool? = nil) throws -> [String: Any] {
        app.terminate()
        // A non-RF sentinel scan filter: denied must reject before scanning; authorized only initializes.
        app.launchArguments = ["--blehub-nitro-permission-phase", phase,
            "--blehub-nitro-permission-run-id", runId,
            "--blehub-nitro-permission-service", "00000000-0000-4000-8000-000000000001"]
        app.launch()
        if let consent {
            try answerBluetoothConsent(allow: consent)
            let decision = XCTAttachment(string: "run_id=\(runId); phase=\(phase); decision=\(consent ? "allow" : "deny"); consent-staging-process-close-unverified")
            decision.name = "permission-consent-decision"
            decision.lifetime = .keepAlways
            add(decision)
            // The consent-staging process has no asserted close/zero result. Audit
            // the resulting OS authorization with a fresh process, not its old UI.
            app.terminate()
            app.launch()
        }
        let identity = app.staticTexts["permission-native-identity"]
        try requireAuthorization(identity.waitForExistence(timeout: 30), "Native permission identity did not appear")
        guard let data = identity.label.data(using: .utf8),
              let record = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw AuthorizationCheckError.failed("Invalid native authorization JSON")
        }
        try requireAuthorization(record["run_id"] as? String == runId && record["phase"] as? String == phase &&
                                 record["platform"] as? String == "ios" && record["source"] as? String == "launch" &&
                                 (record["pid"] as? Int ?? 0) > 0 && record["bundle_id"] as? String == "org.reactjs.native.example.BleHubNitroHost",
                                 "Native authorization identity mismatch")
        guard let boot = record["boot_id"] as? String, UUID(uuidString: boot) != nil,
              authorizationBoots.insert(boot).inserted else {
            throw AuthorizationCheckError.failed("Phase reused a native boot identity")
        }
        let attachment = XCTAttachment(data: data, uniformTypeIdentifier: "public.json")
        attachment.name = "permission-native-\(phase)"
        attachment.lifetime = .keepAlways
        add(attachment)
        return record
    }

    @MainActor
    private func answerBluetoothConsent(allow: Bool) throws {
        let springboard = XCUIApplication(bundleIdentifier: "com.apple.springboard")
        let alert = springboard.alerts.firstMatch
        try requireAuthorization(alert.waitForExistence(timeout: 15), "Bluetooth consent prompt did not appear")
        let snapshot = XCTAttachment(string: alert.debugDescription)
        snapshot.name = "bluetooth-consent-accessibility"
        snapshot.lifetime = .keepAlways
        add(snapshot)
        let text = alert.label
        try requireAuthorization(text.contains("BleHubNitroHost") &&
                                 (text.localizedCaseInsensitiveContains("Bluetooth") || text.contains("蓝牙")),
                                 "Refusing an unrelated system alert: \(text)")
        let labels = allow ? ["Allow", "OK", "允许", "好"] : ["Don\u{27}t Allow", "Don’t Allow", "不允许"]
        let buttons = alert.buttons.matching(NSPredicate(format: "label IN %@", labels))
        try requireAuthorization(buttons.count == 1 && buttons.firstMatch.isHittable, "Missing/ambiguous Bluetooth consent choice")
        let screenshot = XCTAttachment(screenshot: springboard.screenshot())
        screenshot.name = allow ? "bluetooth-consent-allow" : "bluetooth-consent-deny"
        screenshot.lifetime = .keepAlways
        add(screenshot)
        buttons.firstMatch.tap()
    }

    @MainActor
    private func awaitAuthorizationPhase(_ app: XCUIApplication) throws {
        let result = app.staticTexts["rf-permission-transition-result"]
        let passed = waitForLabel("Current phase App checks passed", on: result, timeout: 20)
        let failure = app.staticTexts["rf-permission-transition-failure"]
        try requireAuthorization(passed, "Permission phase failed: \(failure.exists ? failure.label : "result UI unavailable")")
        let screenshot = XCTAttachment(screenshot: app.screenshot())
        screenshot.name = "permission-phase-complete"
        screenshot.lifetime = .keepAlways
        add(screenshot)
    }

    @MainActor
    private func restoreBluetoothAuthorization(_ app: XCUIApplication, runId: String, inspectBeforeReset: Bool = false) throws {
        if inspectBeforeReset {
            let current = try launchAuthorizationPhase("inspect", runId: runId, app: app)
            if current["authorization_at_launch"] as? String == "authorized" {
                _ = try launchAuthorizationPhase("authorized", runId: runId, app: app)
                try awaitAuthorizationPhase(app)
                return
            }
        }
        app.terminate()
        app.resetAuthorizationStatus(for: .bluetooth)
        let reset = try launchAuthorizationPhase("inspect", runId: runId, app: app)
        try requireAuthorization(reset["authorization_at_launch"] as? String == "not_determined",
                                 "Restoration reset did not produce NotDetermined")
        let identity = try launchAuthorizationPhase("authorized", runId: runId, app: app, consent: true)
        try requireAuthorization(identity["authorization_at_launch"] as? String == "authorized",
                                 "Fresh restored process did not observe Authorized")
        try awaitAuthorizationPhase(app)
    }

    @MainActor
    private func assertContractPassed(
        run: Int,
        in app: XCUIApplication,
        file: StaticString = #filePath,
        line: UInt = #line
    ) {
        let runCount = app.staticTexts["run-count"]
        XCTAssertTrue(
            runCount.waitForExistence(timeout: 10),
            "The contract run count was not visible",
            file: file,
            line: line
        )
        XCTAssertTrue(
            waitForLabel("Run \(run)", on: runCount, timeout: 10),
            "Contract run \(run) did not start",
            file: file,
            line: line
        )

        let coverage = app.staticTexts["contract-coverage"]
        XCTAssertTrue(
            coverage.waitForExistence(timeout: 10),
            "Contract coverage was not visible",
            file: file,
            line: line
        )
        XCTAssertTrue(
            waitForLabel("5/5", on: coverage, timeout: 20),
            "Contract run \(run) did not complete all five stages",
            file: file,
            line: line
        )

        let status = app.staticTexts["runtime-status"]
        XCTAssertTrue(
            waitForLabel("contract passed", on: status, timeout: 10),
            "Contract run \(run) did not reach the passed state",
            file: file,
            line: line
        )
        XCTAssertEqual(
            app.buttons["recreate-run-button"].label,
            "Recreate and run",
            file: file,
            line: line
        )
    }

    @MainActor
    private func waitForLabel(
        _ expected: String,
        on element: XCUIElement,
        timeout: TimeInterval
    ) -> Bool {
        let predicate = NSPredicate(format: "label == %@", expected)
        let expectation = XCTNSPredicateExpectation(predicate: predicate, object: element)
        return XCTWaiter.wait(for: [expectation], timeout: timeout) == .completed
    }
}
