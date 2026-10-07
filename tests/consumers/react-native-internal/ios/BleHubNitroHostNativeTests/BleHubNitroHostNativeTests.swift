import NitroModules
import XCTest
@testable import BleHubReactNative

final class BleHubNitroHostNativeTests: XCTestCase {
  func testClosePromiseRemainsPendingUntilDisposeReturnsAndSettlesOnce() throws {
    let disposeGate = DispatchSemaphore(value: 0)
    let probe = HybridBleHubProbe.forDisposeWaitTest(disposeGate)
    let settled = DispatchSemaphore(value: 0)
    let lock = NSLock()
    var settleCount = 0
    var rejection: Error?

    let first = try probe.close()
    let second = try probe.close()
    XCTAssertTrue(first === second)

    first.then { _ in
      lock.lock()
      settleCount += 1
      lock.unlock()
      settled.signal()
    }.catch { error in
      lock.lock()
      rejection = error
      settleCount += 1
      lock.unlock()
      settled.signal()
    }

    XCTAssertEqual(
      settled.wait(timeout: .now() + 0.1),
      .timedOut,
      "close Promise settled before the native dispose operation returned"
    )

    disposeGate.signal()
    XCTAssertEqual(settled.wait(timeout: .now() + 5), .success)

    lock.lock()
    let count = settleCount
    let failure = rejection
    lock.unlock()
    XCTAssertNil(failure)
    XCTAssertEqual(count, 1)
    XCTAssertTrue(try probe.close() === first)
  }
}
