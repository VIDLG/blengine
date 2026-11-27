import XCTest

/// One UI scene driven in-process by XCTest, instead of over WebDriver.
///
/// The host writes a step plan (`BLEHUB_SCENE_PLAN`, JSON) and the test performs it with
/// `XCUIElement` directly: no WebDriverAgent, no Appium server, no HTTP hop, no element-id
/// round trips. It is skipped unless a plan is present, so an ordinary test build runs nothing.
///
/// The plan is deliberately small and declarative, because the *scenario* stays host-owned:
/// fixture, physical lease, markers and the verdict never move into the test process.
///
/// ```json
/// {"launch": true, "steps": [
///   {"op": "wait", "id": "formal-host-title", "exists": true},
///   {"op": "tap",  "id": "start-session"},
///   {"op": "wait", "id": "run-status", "status": {"state": "started"}},
///   {"op": "wait", "id": "run-status", "status": {"scan": "running", "failure": "none"}},
///   {"op": "tapUntil", "id": "next-scan-event", "until": {"id": "scan-latest", "exists": true}},
///   {"op": "read", "id": "run-status", "as": "status"},
///   {"op": "tap",  "id": "close-session"},
///   {"op": "wait", "id": "run-status", "status": {"state": "closed"}}
/// ]}
/// ```
///
/// **There is no scroll, reveal or coordinate step, and that is a property of the design.**
/// A scene only reads or acts on the App's pinned machine surface: the `run-status`
/// projection plus the elements deliberately rendered outside every scroll container. The
/// Android accessibility tree omits off-viewport `ScrollView` children, so a gesture-based
/// scene would behave differently per platform and only as accurately as input injection.
/// If a plan needs an element the tree does not have, the fix is to pin that fact in the App
/// (`src/App.tsx`), not to teach this driver to swipe. See `src/ids.ts`.
///
/// Conditions are a conjunction of assertions on ONE element:
///
/// | key | meaning |
/// |---|---|
/// | `exists` | `true` requires presence, `false` requires absence |
/// | `label` | exact label |
/// | `labelContains` | case-insensitive substring |
/// | `status` | `run-status` `key=value` pairs that must all hold |
/// | `enabled` | `isEnabled` must equal the given bool |
/// | `value` | a text field's `value` must equal the given string |
///
/// A condition with no `exists` (but a `label`/`labelContains`/`status`/`enabled`) implies
/// presence. `status` is how a scene asserts a BLE fact through the App's own one-node
/// projection, so a poll is one element read whose facts cannot disagree.
///
/// `still` is the one **temporal** step over the App's own surface: the condition must keep
/// holding for `for` seconds, sampled every poll interval. "Nothing may change" is not a wait —
/// a wait for a state that is already true returns instantly — so a quiet window needs its own
/// step.
///
/// `background` is the platform's temporal step: it leaves the App and holds it away for its
/// declared `for` window, failing if the App comes back early. Both windows are declared by the
/// plan, so the driver never waits for something the plan did not ask for.
///
/// `waitAny` is a bounded **disjunction**: it succeeds on the first of 2...8 conditions that
/// holds and reports which one (`observed["waitAny"]` = its index). A platform may
/// legitimately answer one question two ways — a refused CCC disable can keep delivering or
/// stop locally — and which answers are acceptable is the host's decision, not the driver's.
///
/// `tapUntil` is a bounded pull loop: tap, check `until`, repeat at most `maxTaps` times
/// (default 8, hard limit 64). It exists because the App drains scan/notification events
/// only when asked to (`Pull next`), so a scene has to ask — a fixed number of times is
/// either flaky or slow, and an unbounded loop can burn the whole run budget. A step may also
/// declare `advance`, a status counter that must be strictly greater than when the step began:
/// that is how a read whose expected value equals the value already on screen still taps
/// instead of settling on the earlier command's answer.
///
/// `answerAlert` is the one step that looks outside the App, because a freshly installed
/// App's system prompts sit above it in SpringBoard and swallow taps. It answers only an
/// alert whose label matches the plan's declared `match` strings, using only a declared
/// `buttons` label, and refuses anything else.
///
/// `launch` is not a foregrounding mode. `true` starts a fresh App process, which is what
/// the standalone driver entry does. A host-owned scene installs, launches and pins one pid
/// itself, so it passes `"launch": false` and its plan foregrounds the App with the
/// `activate` step instead — after any system prompt has been answered, since `activate`
/// fails when an alert holds the foreground.
final class BlengSceneDriverUITests: XCTestCase {
  private static let planVariable = "BLEHUB_SCENE_PLAN"
  /// The receipt is an attachment, never stdout: UI-test process output does not reach the
  /// `xcodebuild` log, so the host reads it back with `xcresulttool export attachments`.
  private static let receiptName = "ios-scene-driver-receipt"
  private static let defaultTimeout: TimeInterval = 30
  private static let pollInterval: TimeInterval = 0.05
  /// How long the App's own window may take to become hittable after `activate`.
  private static let foregroundSettleSeconds: TimeInterval = 10
  /// How many Home+activate repairs one `activate` may use. Two is enough to leave a presented
  /// App Switcher and land on the App; more would be an unbounded Home loop.
  private static let maxActivateRepairs = 2
  /// How long one repair may take to land before deciding whether another is needed.
  private static let activateRepairSettleSeconds: TimeInterval = 3
  /// Background leaving is a platform transition, so it is polled at the same cadence as any
  /// other wait; holding the App away is not, and samples once a second.
  private static let backgroundGrace: TimeInterval = 15
  private static let dwellPollInterval: TimeInterval = 1
  private static let maxDwell: TimeInterval = 60
  private static let maxTapsLimit = 64

  override func setUpWithError() throws {
    continueAfterFailure = false
  }

  @MainActor
  func testScenePlan() throws {
    guard
      let raw = ProcessInfo.processInfo.environment[Self.planVariable],
      let data = raw.data(using: .utf8),
      let plan = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
      let steps = plan["steps"] as? [[String: Any]]
    else {
      throw XCTSkip("No scene plan: this driver is host-driven only")
    }

    let app = XCUIApplication()
    // `launch` starts a *new* App process, for the standalone driver entry. A host-owned
    // scene installs, launches and pins one pid itself and passes `"launch": false`; its
    // plan then owns foregrounding through the `activate` step, because activation has to
    // happen *after* a system prompt is answered, not before.
    if plan["launch"] as? Bool ?? true {
      app.launch()
    }

    var records: [[String: Any]] = []
    var values: [String: String] = [:]
    var failure: String?

    for (index, step) in steps.enumerated() {
      let started = Date()
      var observed: [String: String] = [:]
      do {
        try perform(step, in: app, observed: &observed, values: &values)
      } catch {
        records.append([
          "index": index,
          "step": step,
          "seconds": Date().timeIntervalSince(started),
          "error": String(describing: error),
          "observed": observed,
        ])
        failure = "scene step \(index) \(step) failed: \(error)"
        break
      }
      records.append([
        "index": index,
        "step": step,
        "seconds": Date().timeIntervalSince(started),
        "observed": observed,
      ])
    }

    let payload: [String: Any] = [
      "scope": "ios-scene-driver",
      "status": failure == nil ? "passed" : "failed",
      "primary_error": failure ?? "",
      "steps": records,
      "values": values,
    ]
    attach(payload)
    // The trees are evidence, not only a failure aid: they are the only record of what the
    // screen showed after the last step, and `stdout` never leaves the test process.
    // SpringBoard's tree is included because an overlay that swallows a tap lives there,
    // not in the App's own tree.
    attachHierarchy("app", app)
    attachHierarchy("springboard", XCUIApplication(bundleIdentifier: "com.apple.springboard"))
    if let failure {
      XCTFail(failure)
    }
  }

  /// Publish one bounded hierarchy dump so a failure names what the screen actually showed.
  private func attachHierarchy(_ name: String, _ application: XCUIApplication) {
    let text = String(application.debugDescription.prefix(6000))
    let attachment = XCTAttachment(string: text)
    attachment.name = "ios-scene-driver-\(name)-hierarchy"
    attachment.lifetime = .keepAlways
    add(attachment)
  }

  /// Publish the receipt as an `XCTAttachment` so the host can read it from the result bundle.
  private func attach(_ payload: [String: Any]) {
    guard
      let data = try? JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys])
    else {
      return
    }
    let attachment = XCTAttachment(data: data, uniformTypeIdentifier: "public.json")
    attachment.name = Self.receiptName
    attachment.lifetime = .keepAlways
    add(attachment)
  }

  // MARK: - Steps

  @MainActor
  private func perform(
    _ step: [String: Any],
    in app: XCUIApplication,
    observed: inout [String: String],
    values: inout [String: String]
  ) throws {
    let op = step["op"] as? String ?? ""
    let timeout = step["timeout"] as? TimeInterval ?? Self.defaultTimeout
    switch op {
    case "tap":
      let id = try requireID(step)
      let element = try waitFor(step, id: id, in: app, timeout: timeout, observed: &observed)
        .element
      observed["tap"] = Self.describe(try tap(element, id: id, app: app, timeout: timeout))
    case "tapWhen":
      let id = try requireID(step)
      guard let when = step["when"] as? [String: Any] else {
        throw DriverError.invalid("tapWhen requires a `when` object")
      }
      _ = try waitFor(when, id: try requireID(when), in: app, timeout: timeout,
                      observed: &observed)
      let element = try waitFor(step, id: id, in: app, timeout: timeout, observed: &observed)
        .element
      observed["tap"] = Self.describe(try tap(element, id: id, app: app, timeout: timeout))
    case "tapUntil":
      let id = try requireID(step)
      guard let until = step["until"] as? [String: Any] else {
        throw DriverError.invalid("tapUntil requires an `until` condition")
      }
      let untilID = try requireID(until)
      let maxTaps = step["maxTaps"] as? Int ?? 8
      guard (1...Self.maxTapsLimit).contains(maxTaps) else {
        throw DriverError.invalid("maxTaps must be 1...\(Self.maxTapsLimit)")
      }
      // `advance`: the step settles only once the named status counter is strictly greater
      // than it was when the step began. A control whose effect can equal the value already on
      // screen (a re-read that reports the same bytes) would otherwise be satisfied by the
      // previous command's value without ever being tapped.
      let advance = step["advance"] as? String ?? ""
      let floor = advance.isEmpty ? 0 : Self.statusCount(label(app, id: untilID), advance)
      var settled = floor
      let deadline = Date().addingTimeInterval(timeout)
      var attempt = 0
      while true {
        if let found = try satisfied(until, id: untilID, in: app, observed: &observed),
          advance.isEmpty || Self.statusCount(found.label, advance) > floor
        {
          observed["taps"] = "\(attempt)"
          return
        }
        if attempt >= maxTaps {
          throw DriverError.timeout(
            "\(untilID) still not satisfied after \(maxTaps) taps of \(id)")
        }
        let remaining = deadline.timeIntervalSinceNow
        guard remaining > 0 else {
          throw DriverError.timeout("\(untilID) not reached within \(timeout)s of tapping \(id)")
        }
        let element = try waitFor(step, id: id, in: app, timeout: remaining, observed: &observed)
          .element
        observed["tap"] = Self.describe(
          try tap(element, id: id, app: app, timeout: remaining))
        attempt += 1
        if !advance.isEmpty {
          // Wait for *this* tap's own operation to settle before deciding to tap again;
          // otherwise a fast poll loop records the same command several times.
          settled = try awaitStatusAbove(
            advance, id: untilID, above: settled, in: app, deadline: deadline,
            observed: &observed)
        }
      }
    case "wait":
      let id = try requireID(step)
      _ = try waitFor(step, id: id, in: app, timeout: timeout, observed: &observed)
    case "waitAny":
      // A bounded disjunction over the same conditions `wait` accepts. It exists because one
      // question can have two legitimate answers (see the type documentation); the step
      // reports which condition held and the host decides whether that answer is acceptable.
      guard let any = step["any"] as? [[String: Any]], (2...8).contains(any.count) else {
        throw DriverError.invalid("waitAny requires 2...8 conditions")
      }
      let ids = try any.map { try requireID($0) }
      let deadline = Date().addingTimeInterval(timeout)
      while true {
        for (index, condition) in any.enumerated() {
          if try satisfied(condition, id: ids[index], in: app, observed: &observed) != nil {
            observed["waitAny"] = "\(index)"
            return
          }
        }
        guard Date() < deadline else {
          let seen = ids.map { "\($0)=\(observed[$0] ?? "absent")" }.joined(separator: "; ")
          throw DriverError.timeout("none of \(ids) held within \(timeout)s (saw \(seen))")
        }
        usleep(useconds_t(Self.pollInterval * 1_000_000))
      }
    case "still":
      let id = try requireID(step)
      guard let window = step["for"] as? TimeInterval, window > 0, window <= 30 else {
        throw DriverError.invalid("still requires `for` in (0, 30] seconds")
      }
      let deadline = Date().addingTimeInterval(timeout)
      let quietUntil = Date().addingTimeInterval(window)
      while true {
        guard try satisfied(step, id: id, in: app, observed: &observed) != nil else {
          throw DriverError.invalid(
            "\(id) did not hold for \(window)s: \(describe(step)) "
              + "(saw \(observed[id] ?? "absent"))")
        }
        guard Date() < quietUntil else {
          observed["still"] = "\(window)s"
          return
        }
        guard Date() < deadline else {
          throw DriverError.timeout("\(id) could not be sampled for \(window)s")
        }
        usleep(useconds_t(Self.pollInterval * 1_000_000))
      }
    case "activate":
      // Foreground the App this UITest target already owns; never a relaunch. It is a step
      // rather than a mode because a covering system prompt must be answered first.
      //
      // Foreground is a *process* state, not a screen state, and that is the trap: the App
      // can be `.runningForeground` while its window is still animating in from the Home
      // screen or the App Switcher, and every tap dispatched then reads "present but not
      // hittable" (measured on `disconnect`, 2026-11-26, with the switcher's card in
      // SpringBoard's tree). Pressing Home unconditionally used to *create* that transition:
      // a two-phase scene never backgrounds the App, so Home alone moved it off screen and
      // `activate()` raced the way back.
      //
      // So Home is a repair, not a ritual: an App that is already foreground with a hittable
      // window is left exactly as it is, and Home is pressed only when it is not.
      //
      // One repair is not always enough. A presented App Switcher keeps the App
      // `.runningForeground` while its card covers the window, and a synthesized Home that lands
      // while the switcher is still animating can leave it up: measured on `recovery-ten`
      // `announce:9` (2026-11-26) as `state=4, switcher=card-present` after the single repair, so
      // the scene failed although the App was alive and the ninth announcement had passed. The
      // repair therefore repeats while the window still takes no hits — bounded by the same
      // settle budget, one verified attempt at a time, and counted in the receipt so a run says
      // how many presses it took instead of just whether it worked.
      if isFrontmost(app) {
        observed["activate"] = "already-frontmost"
        return
      }
      let settle = Date().addingTimeInterval(Self.foregroundSettleSeconds)
      var repairs = 0
      while !isFrontmost(app) {
        guard Date() < settle else {
          throw DriverError.timeout(
            "the App's window never became hittable after activate "
              + "(state=\(app.state.rawValue), switcher=\(Self.switcherNote()), "
              + "repairs=\(repairs))")
        }
        guard repairs < Self.maxActivateRepairs else {
          usleep(useconds_t(Self.pollInterval * 1_000_000))
          continue
        }
        XCUIDevice.shared.press(.home)
        usleep(300_000)
        app.activate()
        repairs += 1
        observed["activate"] = repairs == 1 ? "repaired" : "repaired x\(repairs)"
        let repaired = Date().addingTimeInterval(Self.activateRepairSettleSeconds)
        while !isFrontmost(app), Date() < repaired {
          usleep(useconds_t(Self.pollInterval * 1_000_000))
        }
      }
    case "background":
      // The inverse of `activate`: leaving the App is proven, because the host's next action
      // (a value emitted while the App is away) only means something if it really was away.
      //
      // `for` is the window the App must stay away, not a timeout: one instant of being away
      // proves nothing (an App that was never suspended cannot lose a subscription), so the
      // step samples the same fact for the declared window and fails if the App comes back
      // early. The window is the plan's, never this driver's, and the observed value records
      // it so a receipt says how long the App was actually away.
      guard let window = step["for"] as? TimeInterval, window > 0, window <= Self.maxDwell
      else {
        throw DriverError.invalid("background requires `for` in (0, \(Self.maxDwell)] seconds")
      }
      XCUIDevice.shared.press(.home)
      let until = Date().addingTimeInterval(Self.backgroundGrace)
      let secondPress = Date().addingTimeInterval(3)
      var pressed = 1
      while app.state == .runningForeground {
        guard Date() < until else {
          observed["background"] = "still-foreground state=\(app.state.rawValue)"
          throw DriverError.timeout(
            "the App is still foreground \(Self.backgroundGrace)s after backgrounding "
              + "(state=\(app.state.rawValue))")
        }
        if pressed == 1, Date() >= secondPress {
          // A synthesized Home that was swallowed leaves the App frontmost; pressing again
          // is what a person would do, and one retry keeps a lost press from failing a scene.
          pressed = 2
          XCUIDevice.shared.press(.home)
        }
        usleep(useconds_t(Self.pollInterval * 1_000_000))
      }
      let awaySince = Date()
      let holdUntil = awaySince.addingTimeInterval(window)
      while true {
        if app.state == .runningForeground {
          observed["background"] =
            "resumed after \(String(format: "%.1f", Date().timeIntervalSince(awaySince)))s "
            + "of \(window)s state=\(app.state.rawValue)"
          throw DriverError.invalid(
            "the App returned to the foreground inside the \(window)s background window")
        }
        guard Date() < holdUntil else { break }
        // The hold is sampled once a second: this asks the device for a state, so it is the
        // cheapest interval that still catches a return rather than a busy poll.
        usleep(useconds_t(Self.dwellPollInterval * 1_000_000))
      }
      observed["background"] =
        "not-foreground for=\(String(format: "%.1f", Date().timeIntervalSince(awaySince)))s "
        + "state=\(app.state.rawValue)"
    case "setText":
      try setText(step, in: app, timeout: timeout, observed: &observed)
    case "answerAlert":
      try answerAlert(step, timeout: timeout, observed: &observed)
    case "read":
      let id = try requireID(step)
      guard let name = step["as"] as? String, !name.isEmpty else {
        throw DriverError.invalid("read requires `as`")
      }
      let element = try waitFor(step, id: id, in: app, timeout: timeout, observed: &observed)
      values[name] = element.element.label
    default:
      throw DriverError.invalid("unknown scene step op \(op)")
    }
  }

  /// Whether the App is on screen right now: foreground *and* its own window takes hits.
  ///
  /// `isHittable` on the window is what separates "the process is frontmost" from "the screen
  /// shows this App"; it is false mid-transition and under a foreign window such as the App
  /// Switcher's.
  @MainActor
  private func isFrontmost(_ app: XCUIApplication) -> Bool {
    guard app.state == .runningForeground else { return false }
    let window = app.windows.firstMatch
    return window.exists && window.isHittable
  }

  /// The App's card in the App Switcher, when SpringBoard has one presented.
  ///
  /// The switcher window exists whether or not it is presented, so this reports only the card
  /// — and only as a diagnostic note, never as a decision.
  @MainActor
  private static func switcherNote() -> String {
    let springboard = XCUIApplication(bundleIdentifier: "com.apple.springboard")
    let card = springboard.descendants(matching: .any)
      .matching(NSPredicate(format: "identifier BEGINSWITH 'card:'")).firstMatch
    return card.exists ? "card-present" : "card-absent"
  }

  /// Tap one element once it can actually receive the tap.
  ///
  /// `isHittable` is false while the element is off screen, has no hit point of its own, or is
  /// covered — including, briefly, while the App is still coming forward after `activate` and
  /// while a layout pass re-settles. Presence alone therefore proves nothing about a tap, and
  /// failing on one non-hittable sample turned a one-frame transition into a scene failure
  /// (`disconnect` at 2026-11-26: `pull-notification is present but not hittable`). The wait is
  /// bounded by the step's own budget, and a control that never becomes hittable fails with its
  /// frame and the App's state, so the next run says why instead of just that.
  ///
  /// Returns the two halves separately. XCTest waits for the App to report idle before it
  /// dispatches an event, so an App that never idles pays that budget inside `press` on every
  /// touch — measured on the iPhone 14 Plus, 2026-11-26: ten presses inside one scene cost about
  /// 121 s each while the same scene standalone finished in 26 s. Recording `hittable` and
  /// `press` apart is what lets the next run say which half took the seconds instead of leaving
  /// it to guesswork. Nothing here retries: the measurement is a fact, not a policy change.
  private func tap(
    _ element: XCUIElement, id: String, app: XCUIApplication, timeout: TimeInterval
  ) throws -> (hittable: TimeInterval, press: TimeInterval) {
    let started = Date()
    let deadline = Date().addingTimeInterval(timeout)
    while !element.isHittable {
      guard Date() < deadline else {
        throw DriverError.timeout(
          "\(id) is present but never became hittable within \(timeout)s "
            + "(frame=\(element.frame), enabled=\(element.isEnabled), "
            + "app=\(app.state.rawValue))")
      }
      usleep(useconds_t(Self.pollInterval * 1_000_000))
    }
    let hittable = Date().timeIntervalSince(started)
    let pressing = Date()
    element.tap()
    return (hittable, Date().timeIntervalSince(pressing))
  }

  /// The receipt-facing form of a tap's own timing.
  private static func describe(_ timing: (hittable: TimeInterval, press: TimeInterval)) -> String {
    String(format: "hittable=%.2f press=%.2f", timing.hittable, timing.press)
  }

  /// Replace one text field's value and prove the App kept it.
  ///
  /// A controlled input can drop characters while its own clear/re-render runs, and a
  /// scene must not proceed on a value the App never took: the declared text is verified
  /// exactly, and a field that still disagrees fails the step with what it shows. Fields
  /// are pinned machine surface, so this never needs a scroll or a coordinate.
  @MainActor
  private func setText(
    _ step: [String: Any], in app: XCUIApplication, timeout: TimeInterval,
    observed: inout [String: String]
  ) throws {
    let id = try requireID(step)
    guard let text = step["text"] as? String else {
      throw DriverError.invalid("setText requires `text`")
    }
    let current = try waitFor(step, id: id, in: app, timeout: timeout, observed: &observed)
      .element
    // Each part is timed into the step's `observed`, so a device run says where a text field
    // actually costs its seconds instead of leaving it to guesswork.
    let focused = Date()
    let focusTap = try tap(current, id: id, app: app, timeout: timeout)
    let focusSeconds = Date().timeIntervalSince(focused)
    observed["focusTap"] = Self.describe(focusTap)
    // Clear by deleting until the field really reads empty. A controlled React input
    // re-renders while it is being edited, and a fixed-length burst of deletes races that
    // re-render: measured on the recovery scene, the deletion was partly undone and the new
    // value was appended to the old one (36 characters became 48).
    let clearing = Date().addingTimeInterval(timeout)
    let clearStart = Date()
    var previous = ""
    while true {
      let existing = (current.value as? String) ?? ""
      observed[id] = "value=\(existing)"
      if existing.isEmpty { break }
      guard Date() < clearing else {
        throw DriverError.timeout("\(id) never cleared: still \(existing.debugDescription)")
      }
      // Deleting the whole remainder at once is the point; a re-render that restores some
      // characters only means this loop runs again.
      if existing == previous {
        current.typeText(
          String(repeating: XCUIKeyboardKey.delete.rawValue, count: existing.count))
      }
      previous = existing
      usleep(useconds_t(Self.pollInterval * 1_000_000))
    }
    let clearSeconds = Date().timeIntervalSince(clearStart)
    let typeStart = Date()
    if !text.isEmpty {
      // A device with an external keyboard (Continuity/hardware) never presents the software
      // keyboard, and `typeText` then has no keyboard to drive: measured on the iPhone 14 Plus,
      // 2026-11-26, every process logged `hasHardwareKeyboard=1` and a controlled React input
      // stayed empty until the 30 s verify budget expired, without `typeText` throwing. Recording
      // presence is what turns that silent state into a named one.
      observed["keyboardBeforeTyping"] =
        app.keyboards.firstMatch.exists ? "present" : "absent (external keyboard?)"
      current.typeText(text)
    }
    let typeSeconds = Date().timeIntervalSince(typeStart)
    let deadline = Date().addingTimeInterval(timeout)
    let verifyStart = Date()
    var shown = ""
    repeat {
      shown = (current.value as? String) ?? ""
      observed[id] = "value=\(shown)"
      if shown == text {
        observed["timing"] = String(
          format: "focus=%.2f clear=%.2f type=%.2f verify=%.2f",
          focusSeconds, clearSeconds, typeSeconds,
          Date().timeIntervalSince(verifyStart))
        Self.dismissKeyboard(in: app, observed: &observed)
        return
      }
      usleep(useconds_t(Self.pollInterval * 1_000_000))
    } while Date() < deadline
    throw DriverError.timeout(
      "\(id) kept \(shown.debugDescription), not \(text.debugDescription) "
        + "(keyboard=\(observed["keyboardBeforeTyping"] ?? "unknown"))")
  }

  /// Retire the software keyboard a text entry raises.
  ///
  /// A focused input's keyboard covers the bottom of the screen — measured on the iPhone 14
  /// Plus at y 578...926, while `pull-notification` sits at y 622: present, enabled and
  /// unreachable, which failed `disconnect` on 2026-11-26 with "present but not hittable". The
  /// value is committed on every keystroke, so the keyboard has no further job, and leaving it
  /// up silently narrows the pinned surface every later step can reach.
  ///
  /// `formal-host-title` is the App's own dismiss surface (`Keyboard.dismiss()`): it exists on
  /// every page, no plan taps it, and it is the only App identifier this driver knows by name.
  /// A keyboard that stays up is recorded rather than ignored, so a receipt says which run
  /// could not retire it.
  @MainActor
  private static func dismissKeyboard(
    in app: XCUIApplication, observed: inout [String: String]
  ) {
    let keyboard = app.keyboards.firstMatch
    guard keyboard.exists else { return }
    let title = app.descendants(matching: .any).matching(identifier: "formal-host-title")
      .firstMatch
    guard title.exists, title.isHittable else {
      observed["keyboard"] = "present, no dismiss surface"
      return
    }
    title.tap()
    observed["keyboard"] = keyboard.exists ? "still present" : "dismissed"
  }

  /// Answer *identified* system alerts, or refuse.
  ///
  /// A freshly installed App asserts system prompts that sit above the App and swallow
  /// taps (local network at launch, Bluetooth consent and "find Bluetooth devices" on the
  /// first `CBCentralManager`): a step then reports a timeout although the tap was
  /// injected. They live in SpringBoard, not in the App's tree, so this is the one step
  /// that looks outside it.
  ///
  /// It never answers an alert it was not told to answer. Each rule declares the substrings
  /// its title may contain and the button labels it may press; both are matched against
  /// what the device actually shows, so localisation stays data. An identified alert with
  /// none of the declared buttons fails the step. An *unidentified* alert is left alone:
  /// it either belongs to another app or must not be answered by this lane, and
  /// `required: true` is how a plan says "this prompt is expected, so its absence — or a
  /// stranger in its place — is a failure".
  ///
  /// Up to three consecutive prompts are answered within one step, because iOS asks more
  /// than once while the first scan starts. `required: false` (the default) makes absence
  /// an ordinary state; `grace` is how long such a step watches for one, because a prompt
  /// appears shortly *after* the action that causes it and a defensive step should not pay
  /// the full timeout for a state it does not expect.
  @MainActor
  private func answerAlert(
    _ step: [String: Any], timeout: TimeInterval, observed: inout [String: String]
  ) throws {
    guard let raw = step["rules"] as? [[String: Any]], !raw.isEmpty else {
      throw DriverError.invalid("answerAlert requires a non-empty `rules` list")
    }
    let rules = try raw.map { rule -> (match: [String], buttons: [String]) in
      guard let match = rule["match"] as? [String], !match.isEmpty else {
        throw DriverError.invalid("an alert rule requires a non-empty `match` list")
      }
      guard let buttons = rule["buttons"] as? [String], !buttons.isEmpty else {
        throw DriverError.invalid("an alert rule requires a non-empty `buttons` list")
      }
      return (match, buttons)
    }
    let required = step["required"] as? Bool ?? false
    // How long to watch for a prompt this step does not require. A defensive step must not
    // spend its `timeout` waiting for a state the plan does not expect: that cost a full
    // 4 s per step and about a quarter of every scene's wall clock, and it was paid even when
    // no prompt existed. `grace` is the declared window instead, and 0 means one look. A
    // `required` prompt still uses the whole `timeout`, because its absence is a failure
    // rather than an ordinary state.
    let grace: TimeInterval = step["grace"] as? TimeInterval ?? 0
    guard grace >= 0, grace.isFinite else {
      throw DriverError.invalid("answerAlert `grace` must be a finite non-negative number")
    }
    let watch = required ? timeout : min(timeout, grace)
    let springboard = XCUIApplication(bundleIdentifier: "com.apple.springboard")
    let deadline = Date().addingTimeInterval(watch)
    var answered = 0
    repeat {
      let alerts = springboard.alerts
      let count = alerts.count
      // Never resolve `.element`: a count read and an element read are two snapshots, and
      // an alert that disappears between them turns into a hard snapshot failure that
      // aborts the whole test. `firstMatch` + `exists` cannot raise that way.
      let alert = alerts.firstMatch
      if count > 1 {
        throw DriverError.invalid("expected one system alert, saw \(count)")
      }
      if count == 1, alert.exists {
        // Read everything needed before the tap: after it the alert can be gone, and a
        // read against a vanished element is a hard failure, not a throw.
        let label = alert.label
        let identifier = alert.identifier
        // A system alert's controls can appear twice in the tree (the `kTCCService*`
        // identifier is shared by a wrapper and its child), and an ambiguous query makes
        // `tap()` a hard XCTest failure. `firstMatch` is the same primitive the rest of
        // this driver uses for an identifier that may resolve to more than one node.
        guard
          let rule = rules.first(where: { candidate in
            candidate.match.contains { label.localizedCaseInsensitiveContains($0) }
          })
        else {
          // An alert this plan did not declare is never answered. When the step is
          // optional the alert may belong to another app and not cover ours at all, so it
          // is recorded and left alone; the next step that a covering alert would break
          // then fails with the hierarchy attached. `required` makes it fatal instead.
          observed["alert"] = "left-alone id=\(identifier) label=\(label)"
          if required {
            throw DriverError.invalid("refusing an unidentified system alert: \(label)")
          }
          return
        }
        guard
          let tapped = rule.buttons.first(where: {
            alert.buttons[$0].firstMatch.exists
          })
        else {
          let seen = alert.buttons.allElementsBoundByIndex.prefix(8).map(\.label)
          observed["alert"] = "unanswerable label=\(label)"
          throw DriverError.invalid(
            "system alert has none of the declared buttons \(rule.buttons); saw \(seen)")
        }
        alert.buttons[tapped].firstMatch.tap()
        answered += 1
        observed["alert"] =
          "answered[\(answered)] id=\(identifier) label=\(label) button=\(tapped)"
        if answered >= 3 { return }
        // Let the next prompt of the same burst render before looking again.
        usleep(200_000)
        continue
      }
      if answered > 0 { return }
      usleep(useconds_t(Self.pollInterval * 1_000_000))
    } while Date() < deadline
    // Only a step that answered nothing may say nothing was there: an answered prompt is a
    // fact the receipt keeps, even when the window closed before the loop noticed.
    if answered == 0 {
      observed["alert"] = "absent"
      if required {
        throw DriverError.timeout("no system alert appeared within \(timeout)s")
      }
    }
  }

  @MainActor
  private func waitFor(
    _ condition: [String: Any],
    id: String,
    in app: XCUIApplication,
    timeout: TimeInterval,
    observed: inout [String: String]
  ) throws -> (element: XCUIElement, label: String) {
    guard !id.isEmpty else { throw DriverError.invalid("condition requires `id`") }
    let deadline = Date().addingTimeInterval(timeout)
    repeat {
      if let matched = try satisfied(condition, id: id, in: app, observed: &observed) {
        return matched
      }
      usleep(useconds_t(Self.pollInterval * 1_000_000))
    } while Date() < deadline
    throw DriverError.timeout(
      "\(id) did not reach \(describe(condition)) within \(timeout)s "
        + "(saw \(observed[id] ?? "absent"))\(Self.presentedAlertNote())")
  }

  /// Name a covering system alert, if there is one.
  ///
  /// A tap on a covered screen simply does nothing, so the timeout alone reads as "the App
  /// ignored me". The alert lives in SpringBoard, so this is the only way the step can say
  /// why — and an alert this lane did not declare is a human's decision, not a retry.
  @MainActor
  private static func presentedAlertNote() -> String {
    let springboard = XCUIApplication(bundleIdentifier: "com.apple.springboard")
    let alert = springboard.alerts.firstMatch
    guard alert.exists else { return "" }
    return "; a system alert is covering the App: \(alert.label)"
  }

  /// One numeric status field from a `run-status` label, or `0` when it is absent/unparsable.
  static func statusCount(_ label: String, _ field: String) -> Int {
    guard let status = decodeStatus(label), let value = Int(status[field] ?? "") else { return 0 }
    return value
  }

  /// Poll one status counter until it is strictly greater than `above`.
  ///
  /// A counter that never moves is a step whose operation never settled, so it fails here
  /// with a named budget instead of tapping again in the hope of a different answer.
  @MainActor
  private func awaitStatusAbove(
    _ field: String, id: String, above: Int, in app: XCUIApplication,
    deadline: Date, observed: inout [String: String]
  ) throws -> Int {
    while true {
      let current = label(app, id: id)
      observed[id] = current.isEmpty ? "absent" : "exists label=\(current)"
      let count = Self.statusCount(current, field)
      if count > above { return count }
      guard deadline.timeIntervalSinceNow > 0 else {
        throw DriverError.timeout("\(id) \(field) did not advance above \(above)")
      }
      Thread.sleep(forTimeInterval: 0.1)
    }
  }

  /// The current label of one element, or `""` when it is not on screen.
  @MainActor
  private func label(_ app: XCUIApplication, id: String) -> String {
    let element = app.descendants(matching: .any).matching(identifier: id).firstMatch
    return element.exists ? element.label : ""
  }

  /// One observation of one element, or `nil` when the condition does not hold yet.
  @MainActor
  private func satisfied(
    _ condition: [String: Any],
    id: String,
    in app: XCUIApplication,
    observed: inout [String: String]
  ) throws -> (element: XCUIElement, label: String)? {
    let element = app.descendants(matching: .any).matching(identifier: id).firstMatch
    let exists = element.exists
    let label = exists ? element.label : ""
    observed[id] = exists ? "exists label=\(label)" : "absent"
    guard try matches(condition, element: element, exists: exists, label: label) else {
      return nil
    }
    return (element, label)
  }

  private func matches(
    _ condition: [String: Any], element: XCUIElement, exists: Bool, label: String
  ) throws -> Bool {
    let assertsValue =
      condition["label"] != nil || condition["labelContains"] != nil
      || condition["status"] != nil || condition["enabled"] != nil
      || condition["value"] != nil
    let requiresPresent: Bool
    if let expected = condition["exists"] as? Bool {
      if !expected && assertsValue {
        throw DriverError.invalid("an absent condition cannot also assert a value")
      }
      requiresPresent = expected
    } else {
      requiresPresent = true
    }
    guard requiresPresent else { return !exists }
    guard exists else { return false }
    if let expected = condition["label"] as? String, label != expected { return false }
    if let contains = condition["labelContains"] as? String,
      !label.localizedCaseInsensitiveContains(contains)
    {
      return false
    }
    if let expected = condition["enabled"] as? Bool, element.isEnabled != expected {
      return false
    }
    if let expected = condition["value"] as? String,
      ((element.value as? String) ?? "") != expected
    {
      return false
    }
    if let expected = condition["status"] as? [String: String] {
      guard let observed = Self.decodeStatus(label) else { return false }
      for (key, value) in expected where observed[key] != value { return false }
    }
    return true
  }

  /// Decode `key=value;key=value` (the App's `run-status` encoding) or return `nil`.
  ///
  /// The host re-decodes the same text strictly, so a malformed frame fails there with the
  /// field named; here a frame that does not decode simply does not satisfy a condition.
  static func decodeStatus(_ label: String) -> [String: String]? {
    var fields: [String: String] = [:]
    for part in label.split(separator: ";", omittingEmptySubsequences: false) {
      let trimmed = part.trimmingCharacters(in: .whitespaces)
      guard let separator = trimmed.firstIndex(of: "=") else { return nil }
      let key = String(trimmed[trimmed.startIndex..<separator])
      let value = String(trimmed[trimmed.index(after: separator)...])
      guard !key.isEmpty, !value.isEmpty, fields[key] == nil else { return nil }
      fields[key] = value
    }
    return fields.isEmpty ? nil : fields
  }

  private func describe(_ condition: [String: Any]) -> String {
    var parts: [String] = []
    for key in ["exists", "label", "labelContains", "enabled", "value", "status"] {
      guard let value = condition[key] else { continue }
      if let status = value as? [String: String] {
        parts.append("status=" + status.sorted { $0.key < $1.key }
          .map { "\($0.key):\($0.value)" }.joined(separator: ","))
      } else {
        parts.append("\(key)=\(value)")
      }
    }
    return parts.isEmpty ? "exists" : parts.joined(separator: " ")
  }

  private func requireID(_ step: [String: Any]) throws -> String {
    guard let id = step["id"] as? String, !id.isEmpty else {
      throw DriverError.invalid("step requires `id`: \(step)")
    }
    return id
  }

  private enum DriverError: Error, CustomStringConvertible {
    case invalid(String)
    case timeout(String)

    var description: String {
      switch self {
      case let .invalid(message): return "invalid plan: \(message)"
      case let .timeout(message): return "timeout: \(message)"
      }
    }
  }
}
