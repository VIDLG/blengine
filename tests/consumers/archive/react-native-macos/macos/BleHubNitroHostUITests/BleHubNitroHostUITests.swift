import XCTest

final class BleHubNitroHostUITests: XCTestCase {
    override func setUpWithError() throws {
        continueAfterFailure = false
    }

    @MainActor
    func testContractLabCompletesAndCloses() {
        let app = XCUIApplication()
        app.launch()
        defer { app.terminate() }

        let title = app.descendants(matching: .any)["macos-contract-lab-title"]
        XCTAssertTrue(
            title.waitForExistence(timeout: 30),
            "The signed Release host did not expose the contract lab"
        )
        XCTAssertEqual(title.label, "Contract lab")

        let events = app.descendants(matching: .any)["macos-runtime-events"]
        XCTAssertTrue(events.waitForExistence(timeout: 30))
        let completedAndClosed = NSPredicate(
            format: "label CONTAINS %@ AND label CONTAINS %@",
            "completion:18446744073709551615:ok",
            "close:resolved"
        )
        let expectation = XCTNSPredicateExpectation(predicate: completedAndClosed, object: events)
        XCTAssertEqual(
            XCTWaiter.wait(for: [expectation], timeout: 30),
            .completed,
            "The production BleHub completion and close were not accessible"
        )
    }
}
