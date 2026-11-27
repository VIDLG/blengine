import XCTest

final class BleHubBlengHostUITests: XCTestCase {
  override func setUpWithError() throws {
    continueAfterFailure = false
  }

  @MainActor
  func testFormalLifecycleCanStartAndClose() {
    let app = XCUIApplication()
    app.launch()
    defer { app.terminate() }

    let state = app.staticTexts["session-state"]
    XCTAssertTrue(state.waitForExistence(timeout: 30))
    XCTAssertEqual(state.label, "idle")

    app.buttons["start-session"].tap()
    XCTAssertTrue(waitForLabel("started", on: state, timeout: 30))

    app.buttons["close-session"].tap()
    XCTAssertTrue(waitForLabel("closed", on: state, timeout: 30))
  }

  @MainActor
  func testFormalWorkbenchNavigationPreservesTheSameRun() {
    let app = XCUIApplication()
    app.launch()
    defer { app.terminate() }

    let state = app.staticTexts["session-state"]
    XCTAssertTrue(state.waitForExistence(timeout: 30))
    XCTAssertEqual(app.staticTexts["workbench-page"].label, "Tests")

    app.buttons["workbench-evidence"].tap()
    XCTAssertTrue(waitForLabel("Evidence", on: app.staticTexts["workbench-page"], timeout: 5))
    XCTAssertTrue(app.staticTexts["lifecycle-evidence"].label.contains("No run has been requested."))

    app.buttons["workbench-tests"].tap()
    XCTAssertTrue(waitForLabel("Tests", on: app.staticTexts["workbench-page"], timeout: 5))
    app.buttons["start-session"].tap()
    XCTAssertTrue(waitForLabel("started", on: state, timeout: 30))

    app.buttons["workbench-evidence"].tap()
    XCTAssertTrue(waitForLabel("Evidence", on: app.staticTexts["workbench-page"], timeout: 5))
    XCTAssertTrue(app.staticTexts["lifecycle-evidence"].label.contains("start: succeeded"))

    app.buttons["workbench-current"].tap()
    XCTAssertTrue(waitForLabel("Current run", on: app.staticTexts["workbench-page"], timeout: 5))
    XCTAssertTrue(app.staticTexts["adapter-state"].waitForExistence(timeout: 5))
    XCTAssertEqual(state.label, "started")

    app.buttons["close-session"].tap()
    XCTAssertTrue(waitForLabel("closed", on: state, timeout: 30))
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
