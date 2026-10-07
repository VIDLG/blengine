import XCTest

final class BleHubNitroHostUITests: XCTestCase {
    override func setUpWithError() throws {
        continueAfterFailure = false
    }

    @MainActor
    func testContractLabCompletesAndRecreatesProbe() {
        let app = XCUIApplication()
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
