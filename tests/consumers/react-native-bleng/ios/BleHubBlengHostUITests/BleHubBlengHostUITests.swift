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
