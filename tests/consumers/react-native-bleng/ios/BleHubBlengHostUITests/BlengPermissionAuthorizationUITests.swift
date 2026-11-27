import XCTest

/// One Bluetooth-consent phase on the formal workbench, in a genuinely fresh process pair.
///
/// The host owns the state change that makes a phase possible: it **uninstalls** the App first, so
/// the first `CBCentralManager` of the new installation raises the real system consent alert. The
/// test answers that alert the way a user would (interruption monitor), then relaunches and reads
/// the App's own initialization snapshot: it must report the authorization the system recorded and
/// must never claim a powered-on adapter it does not have.
///
/// Two phases, two host entries: `allow` proves a fresh grant is reported as `authorized`, `deny`
/// proves a fresh denial is reported as `denied`. The runner drives `deny` then `allow`, so the
/// device ends in the authorized state it started from.
///
/// Nothing here touches radio, a fixture, a Probe or RF: no power toggle, no scan, no link.
final class BlengPermissionAuthorizationUITests: XCTestCase {
  private enum GateError: Error, CustomStringConvertible {
    case failed(String)
    var description: String {
      switch self {
      case let .failed(message): return message
      }
    }
  }

  private static let displayName = "BleHubBlengHost"
  private static let phaseVariable = "BLEHUB_IOS_PERMISSION_PHASE"
  private static let phases = ["allow", "deny"]

  override func setUpWithError() throws {
    continueAfterFailure = false
  }

  @MainActor
  func testBluetoothConsentPhase() throws {
    let phase = ProcessInfo.processInfo.environment[Self.phaseVariable] ?? ""
    try XCTSkipUnless(
      Self.phases.contains(phase),
      "Fresh-install Bluetooth consent requires the explicit host permission recipe"
    )
    let allow = phase == "allow"
    let app = XCUIApplication()
    let runId = UUID().uuidString.lowercased()
    var primary: Error?
    do {
      // First process of a fresh installation: the OS must ask.
      try launchAndStartAnsweringConsent(app, allow: allow)
      let answered = try launchStartAndReadAdapter(app)
      if allow {
        try require(
          answered.authorization == "authorized",
          "A fresh allowed process must report authorized, not \(answered.authorization)"
        )
        try require(
          answered.state == "poweredOn",
          "A fresh allowed process must report a powered-on adapter, not \(answered.state)"
        )
      } else {
        try require(
          answered.authorization == "denied",
          "A fresh denied process must report denied, not \(answered.authorization)"
        )
        try require(
          answered.state != "poweredOn",
          "A runtime without authorization must never claim a powered-on adapter (\(answered.state))"
        )
      }
    } catch {
      primary = error
    }
    record(runId: runId, phase: phase, primary: primary)
    if let primary {
      XCTFail("Bluetooth consent phase \(phase) failed: \(primary)")
    }
    // Leave the resulting state visible for the host and the next phase.
  }

  // MARK: - Scene steps

  private struct AdapterReport {
    let authorization: String
    let state: String
  }

  /// Launch, start the session, and read the initialization snapshot from its pinned card.
  @MainActor
  private func launchStartAndReadAdapter(_ app: XCUIApplication) throws -> AdapterReport {
    app.terminate()
    app.launch()
    try startSession(app)
    let authorization = app.staticTexts["scan-authorization"]
    try require(
      authorization.waitForExistence(timeout: 30),
      "The initialization snapshot did not appear after Start session"
    )
    let state = app.staticTexts["adapter-state"]
    try require(state.exists, "The snapshot did not report an adapter state")
    return AdapterReport(authorization: authorization.label, state: state.label)
  }

  /// Launch, answer any environmental alert, start, and answer the Bluetooth consent alert.
  @MainActor
  private func launchAndStartAnsweringConsent(_ app: XCUIApplication, allow: Bool) throws {
    app.launch()
    // A fresh debug installation asserts the local-network prompt at launch. It is not the
    // permission under test; answer it as a user would so the workbench can be driven.
    let environmental = try answerEnvironmentalAlert(app)
    try startSession(app)
    let consent = try answerBluetoothConsent(app, allow: allow)
    let snapshot = XCTAttachment(
      string: "phase=\(allow ? "allow" : "deny"); local-network=\(environmental); "
        + "bluetooth=\(consent)"
    )
    snapshot.name = "bluetooth-consent-result"
    snapshot.lifetime = .keepAlways
    add(snapshot)
  }

  /// Answer the launch-time local-network prompt (Allow) when a fresh install shows it.
  @MainActor
  private func answerEnvironmentalAlert(_ app: XCUIApplication) throws -> String {
    let springboard = XCUIApplication(bundleIdentifier: "com.apple.springboard")
    let deadline = Date().addingTimeInterval(20)
    while Date() < deadline {
      let alert = springboard.alerts.firstMatch
      if alert.exists {
        let label = alert.label
        if label.localizedCaseInsensitiveContains("Local Network") || label.contains("本地网络") {
          attachHierarchy("local-network", springboard)
          if !tapAlertButton(alert, springboard: springboard, labels: ["Allow", "OK", "允许", "好"])
          {
            tapAlertSide(alert, springboard: springboard, trailing: true)
          }
          Thread.sleep(forTimeInterval: 1)
          return "allowed"
        }
        return "unexpected:\(label)"
      }
      Thread.sleep(forTimeInterval: 0.5)
    }
    return "absent"
  }

  /// Answer the Bluetooth consent alert with the user's decision.
  @MainActor
  private func answerBluetoothConsent(_ app: XCUIApplication, allow: Bool) throws -> String {
    let springboard = XCUIApplication(bundleIdentifier: "com.apple.springboard")
    let labels = allow
      ? ["Allow", "OK", "允许", "好"]
      : ["Don't Allow", "Don’t Allow", "不允许"]
    let deadline = Date().addingTimeInterval(30)
    while Date() < deadline {
      let alert = springboard.alerts.firstMatch
      if alert.exists {
        let label = alert.label
        let isBluetooth = label.localizedCaseInsensitiveContains("Bluetooth")
          || label.contains("蓝牙")
        // Never answer an alert this lane did not identify.
        try require(isBluetooth, "Refusing an unrelated system alert: \(label)")
        attachHierarchy("bluetooth-consent", springboard)
        let springboardApp = XCUIApplication(bundleIdentifier: "com.apple.springboard")
        let screenshot = XCTAttachment(screenshot: springboardApp.screenshot())
        screenshot.name = allow ? "bluetooth-consent-allow" : "bluetooth-consent-deny"
        screenshot.lifetime = .keepAlways
        add(screenshot)
        if !tapAlertButton(alert, springboard: springboard, labels: labels) {
          tapAlertSide(alert, springboard: springboard, trailing: allow)
        }
        Thread.sleep(forTimeInterval: 1)
        return "answered:\(label)"
      }
      // Interaction keeps the alert-discovery loop honest on OS versions that only expose it
      // to an active query; the probe element is inert.
      let probe = app.staticTexts["session-state"]
      if probe.exists { probe.tap() } else { app.tap() }
      Thread.sleep(forTimeInterval: 0.5)
    }
    let authorization = app.staticTexts["scan-authorization"]
    let reported = authorization.exists ? authorization.label : "absent"
    attachHierarchy("app-at-consent-timeout", app)
    throw GateError.failed(
      "the Bluetooth consent prompt was never answered (scan-authorization=\(reported))"
    )
  }

  /// Tap a button by label when this OS exposes the alert's controls.
  @MainActor
  private func tapAlertButton(
    _ alert: XCUIElement,
    springboard: XCUIApplication,
    labels: [String]
  ) -> Bool {
    for label in labels {
      let button = alert.buttons[label]
      if button.exists && button.isHittable {
        button.tap()
        return true
      }
    }
    for label in labels {
      let button = springboard.buttons[label]
      if button.exists && button.isHittable {
        button.tap()
        return true
      }
    }
    return false
  }

  /// Coordinate fallback for OS versions that do not expose an alert's buttons at all.
  /// The alert is a centred card with one bottom button row; `trailing` selects Allow.
  @MainActor
  private func tapAlertSide(
    _ alert: XCUIElement,
    springboard: XCUIApplication,
    trailing: Bool
  ) {
    let frame = alert.frame
    let point = CGVector(
      dx: frame.minX + frame.width * (trailing ? 0.75 : 0.25),
      dy: frame.maxY - 30
    )
    springboard.coordinate(withNormalizedOffset: CGVector(dx: 0, dy: 0))
      .withOffset(point)
      .tap()
  }

  @MainActor
  private func startSession(_ app: XCUIApplication) throws {
    let current = app.buttons["workbench-current"]
    try require(current.waitForExistence(timeout: 30), "The workbench navigation did not appear")
    current.tap()
    let start = app.buttons["start-session"]
    try require(
      start.waitForExistence(timeout: 10),
      "The pinned Start session control did not appear"
    )
    try require(start.isEnabled, "Start session was not enabled on a fresh run")
    start.tap()
  }

  // MARK: - Evidence

  private func require(_ valid: Bool, _ message: String) throws {
    if !valid { throw GateError.failed(message) }
  }

  private func attachHierarchy(_ name: String, _ application: XCUIApplication) {
    let text = String(application.debugDescription.prefix(6000))
    let attachment = XCTAttachment(string: text)
    attachment.name = "\(name)-hierarchy"
    attachment.lifetime = .keepAlways
    add(attachment)
  }

  @MainActor
  private func record(runId: String, phase: String, primary: Error?) {
    let payload: [String: Any] = [
      "run_id": runId,
      "phase": phase,
      "scope": "ios-authorization-only-non-rf",
      "status": primary == nil ? "passed" : "failed",
      "primary_error": primary.map(String.init(describing:)) ?? "",
    ]
    guard let data = try? JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys])
    else { return }
    let attachment = XCTAttachment(data: data, uniformTypeIdentifier: "public.json")
    attachment.name = "ios-permission-host-verdict"
    attachment.lifetime = .keepAlways
    add(attachment)
  }
}
