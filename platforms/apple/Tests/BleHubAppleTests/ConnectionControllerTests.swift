import BleHubUniFfi
import Foundation
import XCTest
@testable import BleHubApple

final class ConnectionControllerTests: XCTestCase {
    func testConnectionExclusivityAndGenerationAcrossLifecycles() throws {
        let controller = AppleConnectionController()
        let peripheral = FakePeripheral(identifier: UUID())
        let first = connectCommand(commandId: 1, connectionId: 10, peripheralId: 100)

        let firstCorrelation = try controller.begin(
            command: first,
            peripheral: peripheral
        )
        XCTAssertEqual(firstCorrelation.generation, 1)
        XCTAssertEqual(controller.activeCount, 1)
        XCTAssertThrowsError(
            try controller.begin(
                command: connectCommand(commandId: 2, connectionId: 11, peripheralId: 100),
                peripheral: peripheral
            )
        ) { error in
            guard case CommandDispatchError.InvalidState = error else {
                return XCTFail("unexpected error: \(error)")
            }
        }
        XCTAssertThrowsError(
            try controller.begin(
                command: connectCommand(commandId: 3, connectionId: 10, peripheralId: 101),
                peripheral: FakePeripheral(identifier: UUID())
            )
        ) { error in
            guard case CommandDispatchError.InvalidRequest = error else {
                return XCTFail("unexpected error: \(error)")
            }
        }

        XCTAssertEqual(
            controller.didConnect(peripheral: peripheral),
            .connected(firstCorrelation)
        )
        let ended = try XCTUnwrap(
            controller.didDisconnect(peripheral: peripheral)
        )
        XCTAssertEqual(ended.phase, .connected)
        XCTAssertEqual(controller.activeCount, 0)

        let secondCorrelation = try controller.begin(
            command: connectCommand(commandId: 4, connectionId: 12, peripheralId: 100),
            peripheral: peripheral
        )
        XCTAssertEqual(secondCorrelation.generation, 2)
    }

    func testConnectFailureRemovesCorrelationBeforeReuse() throws {
        let controller = AppleConnectionController()
        let peripheral = FakePeripheral(identifier: UUID())
        let command = connectCommand(commandId: 5, connectionId: 20, peripheralId: 200)
        let correlation = try controller.begin(command: command, peripheral: peripheral)

        let failed = try XCTUnwrap(
            controller.didFailToConnect(peripheral: peripheral)
        )
        XCTAssertEqual(failed.correlation, correlation)
        XCTAssertEqual(failed.phase, .connecting)
        XCTAssertEqual(controller.activeCount, 0)
        XCTAssertNil(controller.didFailToConnect(peripheral: peripheral))
        XCTAssertNoThrow(
            try controller.begin(command: command, peripheral: peripheral)
        )
    }

    func testDisconnectingPendingConnectIgnoresLateConnectAndWaitsForDisconnect() throws {
        let controller = AppleConnectionController()
        let peripheral = FakePeripheral(identifier: UUID())
        let connect = connectCommand(commandId: 6, connectionId: 30, peripheralId: 300)
        let correlation = try controller.begin(command: connect, peripheral: peripheral)
        let disconnect = DisconnectCommand(
            commandId: CommandId(value: 7),
            connectionId: connect.connectionId
        )

        XCTAssertEqual(try controller.beginDisconnect(command: disconnect), correlation)
        XCTAssertEqual(
            controller.didConnect(peripheral: peripheral),
            .cancelAgain(correlation)
        )
        XCTAssertEqual(controller.activeCount, 1)

        let ended = try XCTUnwrap(
            controller.didDisconnect(peripheral: peripheral)
        )
        XCTAssertEqual(
            ended.phase,
            .disconnecting(commandId: disconnect.commandId, pendingConnect: true)
        )
        XCTAssertEqual(controller.activeCount, 0)
        XCTAssertNil(controller.didConnect(peripheral: peripheral))
    }

    func testLogicalAndRepeatedDisconnectBoundaries() throws {
        let controller = AppleConnectionController()
        let peripheral = FakePeripheral(identifier: UUID())
        let connect = connectCommand(commandId: 8, connectionId: 40, peripheralId: 400)
        _ = try controller.begin(command: connect, peripheral: peripheral)
        _ = controller.didConnect(peripheral: peripheral)
        let disconnect = DisconnectCommand(
            commandId: CommandId(value: 9),
            connectionId: connect.connectionId
        )

        _ = try controller.beginDisconnect(command: disconnect)
        XCTAssertThrowsError(try controller.beginDisconnect(command: disconnect))
        let ended = try XCTUnwrap(
            controller.didDisconnect(peripheral: peripheral)
        )
        XCTAssertEqual(
            ended.phase,
            .disconnecting(commandId: disconnect.commandId, pendingConnect: false)
        )
        XCTAssertThrowsError(try controller.beginDisconnect(command: disconnect))
    }

    func testNativeObjectIdentityRejectsCallbacksFromReplacementWithSameIdentifier() throws {
        let controller = AppleConnectionController()
        let identifier = UUID()
        let peripheral = FakePeripheral(identifier: identifier)
        let replacement = FakePeripheral(identifier: identifier)
        let command = connectCommand(commandId: 10, connectionId: 50, peripheralId: 500)
        let correlation = try controller.begin(command: command, peripheral: peripheral)

        XCTAssertNil(controller.didConnect(peripheral: replacement))
        XCTAssertEqual(controller.activeCount, 1)
        XCTAssertEqual(controller.didConnect(peripheral: peripheral), .connected(correlation))
        XCTAssertNil(controller.connectedCorrelation(for: replacement))
        XCTAssertEqual(controller.connectedCorrelation(for: peripheral), correlation)
        XCTAssertNil(controller.didDisconnect(peripheral: replacement))
        XCTAssertEqual(controller.activeCount, 1)
        XCTAssertNotNil(controller.didDisconnect(peripheral: peripheral))
        XCTAssertEqual(controller.activeCount, 0)
    }

    func testFailClosedDisconnectRejectsCommandsButRetainsPassiveCallbackRoute() throws {
        let controller = AppleConnectionController()
        let peripheral = FakePeripheral(identifier: UUID())
        let command = connectCommand(commandId: 11, connectionId: 60, peripheralId: 600)
        let correlation = try controller.begin(command: command, peripheral: peripheral)
        _ = controller.didConnect(peripheral: peripheral)

        XCTAssertTrue(controller.beginFailClosedDisconnect(connection: correlation))
        XCTAssertNil(controller.connectedCorrelation(for: command.connectionId))
        XCTAssertNil(controller.connectedCorrelation(for: peripheral))
        XCTAssertFalse(controller.beginFailClosedDisconnect(connection: correlation))

        let ended = try XCTUnwrap(controller.didDisconnect(peripheral: peripheral))
        XCTAssertEqual(ended.phase, .cancellingAfterBackendFailure)
        XCTAssertEqual(ended.correlation, correlation)
        XCTAssertEqual(controller.activeCount, 0)
    }

    func testDrainInvalidatesEveryRouteBeforeLateCallbacks() throws {
        let controller = AppleConnectionController()
        let firstPeripheral = FakePeripheral(identifier: UUID())
        let secondPeripheral = FakePeripheral(identifier: UUID())
        _ = try controller.begin(
            command: connectCommand(commandId: 12, connectionId: 61, peripheralId: 601),
            peripheral: firstPeripheral
        )
        _ = try controller.begin(
            command: connectCommand(commandId: 13, connectionId: 62, peripheralId: 602),
            peripheral: secondPeripheral
        )

        XCTAssertEqual(controller.drain().count, 2)
        XCTAssertEqual(controller.activeCount, 0)
        XCTAssertNil(controller.didConnect(peripheral: firstPeripheral))
        XCTAssertNil(controller.didDisconnect(peripheral: secondPeripheral))
    }

    private func connectCommand(
        commandId: UInt64,
        connectionId: UInt64,
        peripheralId: UInt64
    ) -> ConnectCommand {
        ConnectCommand(
            commandId: CommandId(value: commandId),
            connectionId: ConnectionId(value: connectionId),
            peripheralId: PeripheralId(value: peripheralId), autoConnect: false
        )
    }
}

private final class FakePeripheral: ApplePeripheralObject {
    let identifier: UUID

    init(identifier: UUID) {
        self.identifier = identifier
    }
}
