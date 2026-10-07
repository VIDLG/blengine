import Dispatch
import XCTest
@testable import BleHubNitroHandoff

final class BoundedEventHandoffTests: XCTestCase {
  func testCloseDiscardsAcceptedEventBeforeDeliveryAndRejectsLateSubmission() {
    let queue = DispatchQueue(label: "dev.blehub.test.handoff.close")
    var delivered: [Int] = []
    let handoff = BoundedEventHandoff<Int>(stateQueue: queue, capacity: 1) {
      delivered.append($0)
    }

    queue.suspend()
    XCTAssertTrue(handoff.submit(1))
    handoff.close()
    handoff.close()
    XCTAssertFalse(handoff.submit(2))
    queue.resume()
    queue.sync {}

    XCTAssertTrue(delivered.isEmpty)
    XCTAssertFalse(handoff.submit(3))
  }

  func testCapacityOverflowRejectsPermanentlyWithoutDroppingAcceptedEvent() {
    let queue = DispatchQueue(label: "dev.blehub.test.handoff.capacity")
    var delivered: [Int] = []
    let handoff = BoundedEventHandoff<Int>(stateQueue: queue, capacity: 1) {
      delivered.append($0)
    }

    queue.suspend()
    XCTAssertTrue(handoff.submit(1))
    XCTAssertFalse(handoff.submit(2))
    queue.resume()
    queue.sync {}

    XCTAssertEqual(delivered, [1])
    XCTAssertFalse(handoff.submit(3))
  }
}
