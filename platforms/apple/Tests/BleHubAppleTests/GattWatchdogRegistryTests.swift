import BleHubUniFfi
import Dispatch
import XCTest
@testable import BleHubApple

final class GattWatchdogRegistryTests: XCTestCase {
    func testStaleCallbackCannotRetireReplacement() {
        let registry = AppleGattWatchdogRegistry()
        let connectionId = ConnectionId(value: 7)
        let first = DispatchWorkItem {}
        let second = DispatchWorkItem {}

        let firstArm = registry.arm(
            connectionId: connectionId,
            kind: .io,
            workItem: first
        )
        let secondArm = registry.arm(
            connectionId: connectionId,
            kind: .discovery,
            workItem: second
        )

        XCTAssertEqual(registry.diagnosticCount, 1, "replacement owns one watchdog, not two")
        XCTAssertNil(
            registry.takeIfCurrent(connectionId: connectionId, token: firstArm.token)
        )
        XCTAssertEqual(registry.diagnosticCount, 1)
        XCTAssertSameWorkItem(
            second,
            registry.takeIfCurrent(connectionId: connectionId, token: secondArm.token)
        )
        XCTAssertNil(
            registry.takeIfCurrent(connectionId: connectionId, token: secondArm.token)
        )
        XCTAssertSameWorkItem(first, secondArm.replaced)
        XCTAssertEqual(registry.diagnosticCount, 0)
    }

    func testCancelAllDetachesEveryConnectionWatchdog() {
        let registry = AppleGattWatchdogRegistry()
        let first = DispatchWorkItem {}
        let second = DispatchWorkItem {}

        _ = registry.arm(
            connectionId: ConnectionId(value: 1),
            kind: .io,
            workItem: first
        )
        _ = registry.arm(
            connectionId: ConnectionId(value: 2),
            kind: .discovery,
            workItem: second
        )

        XCTAssertEqual(registry.diagnosticCount, 2)
        let cancelled = registry.cancelAll()
        XCTAssertEqual(registry.diagnosticCount, 0)
        XCTAssertEqual(cancelled.count, 2)
        XCTAssertNil(registry.cancel(connectionId: ConnectionId(value: 1)))
        XCTAssertNil(registry.cancel(connectionId: ConnectionId(value: 2)))
    }

    private func XCTAssertSameWorkItem(
        _ expected: DispatchWorkItem?,
        _ actual: DispatchWorkItem?,
        file: StaticString = #filePath,
        line: UInt = #line
    ) {
        XCTAssertTrue(
            expected === actual,
            "work item identity did not match",
            file: file,
            line: line
        )
    }
}
