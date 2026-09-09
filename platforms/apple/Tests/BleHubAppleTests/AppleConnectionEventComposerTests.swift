import BleHubUniFfi
import Foundation
import XCTest
@testable import BleHubApple

final class AppleConnectionEventComposerTests: XCTestCase {
    func testAdapterLossDuringAcceptedDisconnectKeepsSuccessfulDisconnectSemantics() {
        let route = connectionRoute(
            phase: .disconnecting(commandId: CommandId(value: 2), pendingConnect: false)
        )
        let events = AppleConnectionEventComposer.unavailable(
            route: route,
            error: eventError()
        )

        XCTAssertEqual(events.count, 2)
        assertConnectionState(
            events[0],
            commandId: CommandId(value: 2),
            connectionId: route.correlation.connectionId,
            state: .disconnected
        )
        assertSuccessfulTerminal(events[1], commandId: CommandId(value: 2))
    }

    func testAdapterLossDuringPendingConnectThenDisconnectCompletesBothCommandsOnce() {
        let route = connectionRoute(
            phase: .disconnecting(commandId: CommandId(value: 2), pendingConnect: true)
        )
        let events = AppleConnectionEventComposer.unavailable(
            route: route,
            error: eventError()
        )

        XCTAssertEqual(events.count, 4)
        assertConnectionFailure(
            events[0],
            commandId: route.correlation.connectCommandId,
            connectionId: route.correlation.connectionId
        )
        assertFailedTerminal(events[1], commandId: route.correlation.connectCommandId)
        assertConnectionState(
            events[2],
            commandId: CommandId(value: 2),
            connectionId: route.correlation.connectionId,
            state: .disconnected
        )
        assertSuccessfulTerminal(events[3], commandId: CommandId(value: 2))
    }

    func testUnavailableConnectedRouteIsReportedAsPassiveFailure() {
        let route = connectionRoute(phase: .connected)
        let events = AppleConnectionEventComposer.unavailable(
            route: route,
            error: eventError()
        )

        XCTAssertEqual(events.count, 1)
        assertConnectionFailure(
            events[0],
            commandId: nil,
            connectionId: route.correlation.connectionId
        )
    }

    private func connectionRoute(phase: AppleConnectionPhase) -> AppleConnectionRoute {
        AppleConnectionRoute(
            correlation: AppleConnectionCorrelation(
                generation: 1,
                connectCommandId: CommandId(value: 1),
                connectionId: ConnectionId(value: 10),
                peripheralId: PeripheralId(value: 20),
                peripheral: FakeConnectionEventPeripheral(identifier: UUID())
            ),
            phase: phase
        )
    }

    private func eventError() -> BleEventError {
        BleEventError(
            category: .adapterUnavailable,
            message: "adapter unavailable",
            platformDetails: nil
        )
    }

    private func assertConnectionState(
        _ event: BleEvent,
        commandId: CommandId?,
        connectionId: ConnectionId,
        state: ConnectionState,
        file: StaticString = #filePath,
        line: UInt = #line
    ) {
        guard case .connectionStateChanged(
            let observedCommandId,
            let observedConnectionId,
            let result
        ) = event,
            case .ok(let observedState) = result
        else {
            return XCTFail("expected successful connection-state event", file: file, line: line)
        }
        XCTAssertEqual(observedCommandId, commandId, file: file, line: line)
        XCTAssertEqual(observedConnectionId, connectionId, file: file, line: line)
        XCTAssertEqual(observedState, state, file: file, line: line)
    }

    private func assertConnectionFailure(
        _ event: BleEvent,
        commandId: CommandId?,
        connectionId: ConnectionId,
        file: StaticString = #filePath,
        line: UInt = #line
    ) {
        guard case .connectionStateChanged(
            let observedCommandId,
            let observedConnectionId,
            let result
        ) = event,
            case .err(let error) = result
        else {
            return XCTFail("expected failed connection-state event", file: file, line: line)
        }
        XCTAssertEqual(observedCommandId, commandId, file: file, line: line)
        XCTAssertEqual(observedConnectionId, connectionId, file: file, line: line)
        XCTAssertEqual(error.category, .adapterUnavailable, file: file, line: line)
    }

    private func assertSuccessfulTerminal(
        _ event: BleEvent,
        commandId: CommandId,
        file: StaticString = #filePath,
        line: UInt = #line
    ) {
        guard case .commandCompleted(let observedCommandId, let error) = event else {
            return XCTFail("expected command terminal", file: file, line: line)
        }
        XCTAssertEqual(observedCommandId, commandId, file: file, line: line)
        XCTAssertNil(error, file: file, line: line)
    }

    private func assertFailedTerminal(
        _ event: BleEvent,
        commandId: CommandId,
        file: StaticString = #filePath,
        line: UInt = #line
    ) {
        guard case .commandCompleted(let observedCommandId, let error) = event else {
            return XCTFail("expected command terminal", file: file, line: line)
        }
        XCTAssertEqual(observedCommandId, commandId, file: file, line: line)
        XCTAssertEqual(error?.category, .adapterUnavailable, file: file, line: line)
    }
}

private final class FakeConnectionEventPeripheral: ApplePeripheralObject {
    let identifier: UUID

    init(identifier: UUID) {
        self.identifier = identifier
    }
}
