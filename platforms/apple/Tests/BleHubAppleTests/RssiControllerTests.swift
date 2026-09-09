import BleHubUniFfi
import Foundation
import XCTest
@testable import BleHubApple

final class RssiControllerTests: XCTestCase {
    func testSuccessAndCallbackFailuresCompleteExactlyOnce() throws {
        let successController = AppleRssiController()
        let successConnection = connectionCorrelation(generation: 1, connectionId: 10)
        let successCommand = rssiCommand(commandId: 1, connectionId: 10)
        try successController.begin(command: successCommand, connection: successConnection)

        guard case .read(let completedCommand, let rssi) = successController.didReadRssi(
            connection: successConnection,
            peripheral: successConnection.peripheral,
            rssi: NSNumber(value: -61),
            error: nil
        ) else {
            return XCTFail("expected RSSI success completion")
        }
        XCTAssertEqual(completedCommand, successCommand)
        XCTAssertEqual(rssi, -61)
        XCTAssertNil(
            successController.didReadRssi(
                connection: successConnection,
                peripheral: successConnection.peripheral,
                rssi: NSNumber(value: -60),
                error: nil
            )
        )

        let callbackError = eventError(category: .connection, message: "native RSSI failure")
        let errorController = AppleRssiController()
        let errorConnection = connectionCorrelation(generation: 2, connectionId: 20)
        let errorCommand = rssiCommand(commandId: 2, connectionId: 20)
        try errorController.begin(command: errorCommand, connection: errorConnection)
        assertFailure(
            errorController.didReadRssi(
                connection: errorConnection,
                peripheral: errorConnection.peripheral,
                rssi: NSNumber(value: -50),
                error: callbackError
            ),
            command: errorCommand,
            category: .connection,
            message: "native RSSI failure"
        )
        XCTAssertNil(
            errorController.didReadRssi(
                connection: errorConnection,
                peripheral: errorConnection.peripheral,
                rssi: NSNumber(value: -50),
                error: callbackError
            )
        )

        let missingController = AppleRssiController()
        let missingConnection = connectionCorrelation(generation: 3, connectionId: 30)
        let missingCommand = rssiCommand(commandId: 3, connectionId: 30)
        try missingController.begin(command: missingCommand, connection: missingConnection)
        assertFailure(
            missingController.didReadRssi(
                connection: missingConnection,
                peripheral: missingConnection.peripheral,
                rssi: nil,
                error: nil
            ),
            command: missingCommand,
            category: .connection,
            message: "CoreBluetooth RSSI read completed without a value"
        )

        let rangeController = AppleRssiController()
        let rangeConnection = connectionCorrelation(generation: 4, connectionId: 40)
        let rangeCommand = rssiCommand(commandId: 4, connectionId: 40)
        try rangeController.begin(command: rangeCommand, connection: rangeConnection)
        assertFailure(
            rangeController.didReadRssi(
                connection: rangeConnection,
                peripheral: rangeConnection.peripheral,
                rssi: NSNumber(value: 32_768),
                error: nil
            ),
            command: rangeCommand,
            category: .connection,
            message: "CoreBluetooth RSSI read returned a value that is not representable as Int16"
        )
    }

    func testValidatesConnectionAndRejectsDuplicatePendingRead() throws {
        let controller = AppleRssiController()
        let connection = connectionCorrelation(generation: 5, connectionId: 50)
        let command = rssiCommand(commandId: 5, connectionId: 50)

        assertInvalidState {
            try controller.begin(
                command: rssiCommand(commandId: 6, connectionId: 51),
                connection: connection
            )
        }
        try controller.begin(command: command, connection: connection)
        assertInvalidState {
            try controller.begin(
                command: rssiCommand(commandId: 7, connectionId: 50),
                connection: connection
            )
        }
        XCTAssertEqual(controller.pendingCount, 1)
        XCTAssertTrue(controller.hasPending(connection: connection))

        _ = controller.didReadRssi(
            connection: connection,
            peripheral: connection.peripheral,
            rssi: NSNumber(value: -48),
            error: nil
        )
        XCTAssertNoThrow(try controller.begin(command: command, connection: connection))
    }

    func testRejectsStaleGenerationAndReplacementPeripheralCallbacks() throws {
        let controller = AppleRssiController()
        let identifier = UUID()
        let peripheral = FakeRssiPeripheral(identifier: identifier)
        let replacement = FakeRssiPeripheral(identifier: identifier)
        let connection = connectionCorrelation(
            generation: 6,
            connectionId: 60,
            peripheral: peripheral
        )
        let command = rssiCommand(commandId: 8, connectionId: 60)
        try controller.begin(command: command, connection: connection)

        let staleGeneration = connectionCorrelation(
            generation: 7,
            connectionId: 60,
            peripheral: peripheral
        )
        XCTAssertNil(
            controller.didReadRssi(
                connection: staleGeneration,
                peripheral: peripheral,
                rssi: NSNumber(value: -47),
                error: nil
            )
        )

        let replacementConnection = connectionCorrelation(
            generation: 6,
            connectionId: 60,
            peripheral: replacement
        )
        XCTAssertNil(
            controller.didReadRssi(
                connection: replacementConnection,
                peripheral: replacement,
                rssi: NSNumber(value: -46),
                error: nil
            )
        )
        XCTAssertTrue(controller.hasPending(connection: connection))

        guard case .read(let completedCommand, let rssi) = controller.didReadRssi(
            connection: connection,
            peripheral: peripheral,
            rssi: NSNumber(value: -45),
            error: nil
        ) else {
            return XCTFail("expected exact callback route to complete")
        }
        XCTAssertEqual(completedCommand, command)
        XCTAssertEqual(rssi, -45)
    }

    func testRetirementQuarantinesLateCallbackBeforeLifecycleReuse() throws {
        let controller = AppleRssiController()
        let peripheral = FakeRssiPeripheral(identifier: UUID())
        let oldConnection = connectionCorrelation(
            generation: 8,
            connectionId: 70,
            peripheral: peripheral
        )
        let oldCommand = rssiCommand(commandId: 9, connectionId: 70)
        try controller.begin(command: oldCommand, connection: oldConnection)

        assertFailure(
            controller.retire(
                connection: oldConnection,
                error: eventError(category: .connection, message: "disconnected")
            ),
            command: oldCommand,
            category: .connection,
            message: "disconnected"
        )
        XCTAssertNil(
            controller.retire(
                connection: oldConnection,
                error: eventError(category: .connection, message: "duplicate retirement")
            )
        )

        let newConnection = connectionCorrelation(
            generation: 9,
            connectionId: 70,
            peripheral: peripheral
        )
        let newCommand = rssiCommand(commandId: 10, connectionId: 70)
        assertInvalidState {
            try controller.begin(command: newCommand, connection: newConnection)
        }

        XCTAssertTrue(controller.consumeRetiredCallback(peripheral: peripheral))
        XCTAssertFalse(controller.consumeRetiredCallback(peripheral: peripheral))
        try controller.begin(command: newCommand, connection: newConnection)

        guard case .read(let completedCommand, let rssi) = controller.didReadRssi(
            connection: newConnection,
            peripheral: peripheral,
            rssi: NSNumber(value: -44),
            error: nil
        ) else {
            return XCTFail("expected reused lifecycle to complete after the late callback barrier")
        }
        XCTAssertEqual(completedCommand, newCommand)
        XCTAssertEqual(rssi, -44)
    }

    func testDrainSilentlyClearsPendingReadsAndRetiredBarriers() throws {
        let controller = AppleRssiController()
        let retiredPeripheral = FakeRssiPeripheral(identifier: UUID())
        let retiredConnection = connectionCorrelation(
            generation: 10,
            connectionId: 80,
            peripheral: retiredPeripheral
        )
        let retiredCommand = rssiCommand(commandId: 11, connectionId: 80)
        try controller.begin(command: retiredCommand, connection: retiredConnection)
        _ = controller.retire(
            connection: retiredConnection,
            error: eventError(category: .cancelled, message: "closed")
        )

        let pendingConnection = connectionCorrelation(generation: 11, connectionId: 81)
        let pendingCommand = rssiCommand(commandId: 12, connectionId: 81)
        try controller.begin(command: pendingCommand, connection: pendingConnection)

        controller.drain()

        XCTAssertEqual(controller.pendingCount, 0)
        XCTAssertFalse(controller.consumeRetiredCallback(peripheral: retiredPeripheral))
        XCTAssertNil(
            controller.didReadRssi(
                connection: pendingConnection,
                peripheral: pendingConnection.peripheral,
                rssi: NSNumber(value: -43),
                error: nil
            )
        )
        XCTAssertNoThrow(
            try controller.begin(command: retiredCommand, connection: retiredConnection)
        )
    }

    private func connectionCorrelation(
        generation: UInt64,
        connectionId: UInt64,
        peripheral: FakeRssiPeripheral = FakeRssiPeripheral(identifier: UUID())
    ) -> AppleConnectionCorrelation {
        AppleConnectionCorrelation(
            generation: generation,
            connectCommandId: CommandId(value: generation),
            connectionId: ConnectionId(value: connectionId),
            peripheralId: PeripheralId(value: connectionId + 1_000),
            peripheral: peripheral
        )
    }

    private func rssiCommand(commandId: UInt64, connectionId: UInt64) -> ReadRssiCommand {
        ReadRssiCommand(
            commandId: CommandId(value: commandId),
            connectionId: ConnectionId(value: connectionId)
        )
    }

    private func eventError(
        category: BleErrorCategory,
        message: String
    ) -> BleEventError {
        BleEventError(category: category, message: message, platformDetails: nil)
    }

    private func assertInvalidState(
        _ operation: () throws -> Void,
        file: StaticString = #filePath,
        line: UInt = #line
    ) {
        XCTAssertThrowsError(try operation(), file: file, line: line) {
            guard case CommandDispatchError.InvalidState = $0 else {
                return XCTFail("unexpected error: \($0)", file: file, line: line)
            }
        }
    }

    private func assertFailure(
        _ completion: AppleRssiCompletion?,
        command: ReadRssiCommand,
        category: BleErrorCategory,
        message: String,
        file: StaticString = #filePath,
        line: UInt = #line
    ) {
        guard case .failed(let observedCommand, let error) = completion else {
            return XCTFail("expected RSSI failure completion", file: file, line: line)
        }
        XCTAssertEqual(observedCommand, command, file: file, line: line)
        XCTAssertEqual(error.category, category, file: file, line: line)
        XCTAssertEqual(error.message, message, file: file, line: line)
    }
}

private final class FakeRssiPeripheral: ApplePeripheralObject {
    let identifier: UUID

    init(identifier: UUID) {
        self.identifier = identifier
    }
}
