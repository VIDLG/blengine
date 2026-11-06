import BleHubUniFfi
import CoreBluetooth
import Dispatch
import Foundation
import XCTest
@_spi(AppleDiagnostics) @testable import BleHubApple

final class GattIoControllerTests: XCTestCase {
    func testFifoSnapshotObservesResponseWriteThenDisableWithAcceptedSiblings() throws {
        let controller = AppleGattIoController()
        let connection = connectionCorrelation(generation: 1, connectionId: 10)
        let characteristic = FakeIoCharacteristic(
            properties: properties(writableWithResponse: true, notifiable: true)
        )
        let ioTarget = target(
            connection: connection, sessionId: 20, characteristicId: 30,
            characteristic: characteristic
        )
        let write = writeCommand(
            commandId: 1, connectionId: 10, sessionId: 20, characteristicId: 30,
            value: [1], writeType: .withResponse
        )
        let enable = notificationCommand(
            commandId: 2, connectionId: 10, sessionId: 20, subscriptionId: 40,
            characteristicId: 30, action: .enable(mode: .platformDefault)
        )
        XCTAssertNil(controller.diagnosticSnapshotGattIo(connectionId: connection.connectionId))
        try controller.enqueueWrite(command: write, connection: connection, target: ioTarget)
        _ = try nextAction(controller, connection: connection)
        try controller.enqueueSetNotifications(command: enable, connection: connection, target: ioTarget)
        let cancelled = rssiCommand(commandId: 5, connectionId: 10)
        let tail = rssiCommand(commandId: 6, connectionId: 10)
        try controller.enqueueRssi(command: cancelled, connection: connection)
        try controller.enqueueRssi(command: tail, connection: connection)
        let generation = try XCTUnwrap(controller.diagnosticNotificationRoute(
            connection: connection, peripheral: connection.peripheral, characteristic: characteristic
        )["subscriptionGeneration"] as? String)
        let pending = try XCTUnwrap(controller.diagnosticSnapshotGattIo(connectionId: connection.connectionId))
        XCTAssertEqual(pending.connectionId, connection.connectionId)
        XCTAssertEqual(pending.headKind, .command)
        XCTAssertEqual(pending.headCommandId, write.commandId)
        XCTAssertEqual(pending.headState, .awaitingNativeCallback)
        XCTAssertEqual(pending.queuedCommandIds, [enable.commandId, cancelled.commandId, tail.commandId])
        XCTAssertEqual(controller.diagnosticSnapshotGattIo(connectionId: connection.connectionId), pending)
        guard case .queued = controller.cancel(commandId: cancelled.commandId) else {
            return XCTFail("expected queued cancellation")
        }
        XCTAssertEqual(
            controller.diagnosticSnapshotGattIo(connectionId: connection.connectionId)?.queuedCommandIds,
            [enable.commandId, tail.commandId]
        )
        guard case .queued = controller.cancel(commandId: tail.commandId) else {
            return XCTFail("expected queued tail cancellation")
        }
        XCTAssertEqual(controller.diagnosticNotificationRoute(
            connection: connection, peripheral: connection.peripheral, characteristic: characteristic
        )["subscriptionGeneration"] as? String, generation)
        XCTAssertNil(controller.nextNotificationAction(connection: connection))
        guard case .active = controller.cancel(commandId: write.commandId) else {
            return XCTFail("snapshot must not release the active head")
        }
        assertWriteCompletion(controller.didWriteValue(
            connection: connection, peripheral: connection.peripheral,
            characteristic: characteristic, error: nil
        ), command: write)
        let unstarted = try XCTUnwrap(controller.diagnosticSnapshotGattIo(connectionId: connection.connectionId))
        XCTAssertEqual(unstarted.headCommandId, enable.commandId)
        XCTAssertEqual(unstarted.headState, .awaitingDrive)
        XCTAssertEqual(unstarted.queuedCommandIds, [], "consumed write must not leak from storage")
        _ = controller.nextNotificationAction(connection: connection)
        assertNotificationEnabled(controller.didUpdateNotificationState(
            connection: connection, peripheral: connection.peripheral,
            characteristic: characteristic, isNotifying: true, error: nil
        ), command: enable, actualMode: .notification)
        let idle = try XCTUnwrap(controller.diagnosticSnapshotGattIo(connectionId: connection.connectionId))
        XCTAssertNil(idle.headKind)
        XCTAssertNil(idle.headCommandId)
        XCTAssertEqual(idle.headState, .idle)
        XCTAssertEqual(idle.queuedCommandIds, [])

        let disable = notificationCommand(
            commandId: 3, connectionId: 10, sessionId: 20, subscriptionId: 40,
            characteristicId: 30, action: .disable
        )
        let sibling = writeCommand(
            commandId: 4, connectionId: 10, sessionId: 20, characteristicId: 30,
            value: [2], writeType: .withResponse
        )
        try controller.enqueueSetNotifications(command: disable, connection: connection, target: ioTarget)
        _ = controller.nextNotificationAction(connection: connection)
        try controller.enqueueWrite(command: sibling, connection: connection, target: ioTarget)
        let disabling = try XCTUnwrap(controller.diagnosticSnapshotGattIo(connectionId: connection.connectionId))
        XCTAssertEqual(disabling.headKind, .command)
        XCTAssertEqual(disabling.headCommandId, disable.commandId)
        XCTAssertEqual(disabling.headState, .awaitingNativeCallback)
        XCTAssertEqual(disabling.queuedCommandIds, [sibling.commandId])
        XCTAssertEqual(controller.diagnosticSnapshotGattIo(connectionId: connection.connectionId), disabling)
        XCTAssertNil(try nextAction(controller, connection: connection))
        assertNotificationDisabled(controller.didUpdateNotificationState(
            connection: connection, peripheral: connection.peripheral,
            characteristic: characteristic, isNotifying: false, error: nil
        ), command: disable)
        assertWriteWithResponseAction(
            try XCTUnwrap(nextAction(controller, connection: connection)),
            command: sibling, characteristic: characteristic
        )
        _ = controller.retireAll(
            connection: connection, error: eventError(category: .connection, message: "retired")
        )
        let barriers = controller.diagnosticCallbackRouteCount
        let attributes = controller.diagnosticAttributeIdentities
        XCTAssertNil(controller.diagnosticSnapshotGattIo(connectionId: connection.connectionId))
        XCTAssertNil(controller.diagnosticSnapshotGattIo(connectionId: connection.connectionId))
        XCTAssertEqual(controller.diagnosticCallbackRouteCount, barriers)
        XCTAssertEqual(controller.diagnosticAttributeIdentities, attributes)
        assertInvalidState {
            try controller.enqueueWrite(command: sibling, connection: connection, target: ioTarget)
        }
        XCTAssertTrue(controller.consumeRetiredWriteCallback(
            peripheral: connection.peripheral, characteristic: characteristic
        ))
    }

    func testRssiSharesPerConnectionFifoAndCompletesFromExactCallback() throws {
        let controller = AppleGattIoController()
        let connection = connectionCorrelation(generation: 1, connectionId: 10)
        let rssi = rssiCommand(commandId: 1, connectionId: 10)
        let read = readCommand(
            commandId: 2,
            connectionId: 10,
            sessionId: 20,
            characteristicId: 30
        )
        let characteristic = FakeIoCharacteristic(properties: properties(readable: true))
        let target = target(
            connection: connection,
            sessionId: 20,
            characteristicId: 30,
            characteristic: characteristic
        )

        try controller.enqueueRssi(command: rssi, connection: connection)
        try controller.enqueueRead(command: read, connection: connection, target: target)

        guard case .rssi(let active, _) = try nextAction(controller, connection: connection) else {
            return XCTFail("RSSI did not become the FIFO head")
        }
        XCTAssertEqual(active, rssi)
        guard case .queued = controller.cancel(commandId: read.commandId) else {
            return XCTFail("queued characteristic read was not cancellable")
        }

        guard case .rssi(let completed, let value) = controller.didReadRssi(
            connection: connection,
            peripheral: connection.peripheral,
            rssi: NSNumber(value: -61),
            error: nil
        ) else {
            return XCTFail("RSSI callback did not complete the active operation")
        }
        XCTAssertEqual(completed, rssi)
        XCTAssertEqual(value, -61)
        XCTAssertEqual(controller.pendingCount, 0)
    }

    func testRssiRetirementQuarantinesLateCallbackBeforeRouteReuse() throws {
        let controller = AppleGattIoController()
        let connection = connectionCorrelation(generation: 1, connectionId: 10)
        let command = rssiCommand(commandId: 1, connectionId: 10)
        try controller.enqueueRssi(command: command, connection: connection)
        guard case .rssi = try nextAction(controller, connection: connection) else {
            return XCTFail("RSSI did not become active")
        }

        let error = BleEventError(
            category: .connection,
            message: "connection retired",
            platformDetails: nil
        )
        XCTAssertEqual(controller.diagnosticCallbackRouteCount, 2, "lane plus native RSSI callback")
        _ = controller.retireAll(connection: connection, error: error)
        XCTAssertEqual(controller.pendingCount, 0)
        XCTAssertEqual(controller.diagnosticCallbackRouteCount, 1, "retired RSSI barrier remains visible")
        XCTAssertTrue(controller.diagnosticAttributeIdentities.isEmpty)
        XCTAssertThrowsError(try controller.enqueueRssi(
            command: rssiCommand(commandId: 2, connectionId: 10),
            connection: connection
        ))
        XCTAssertNil(controller.didReadRssi(
            connection: connection,
            peripheral: connection.peripheral,
            rssi: NSNumber(value: -61),
            error: nil
        ))
        XCTAssertEqual(controller.diagnosticCallbackRouteCount, 0)
        XCTAssertNoThrow(try controller.enqueueRssi(
            command: rssiCommand(commandId: 2, connectionId: 10),
            connection: connection
        ))
    }

    func testRetirementClassifiesActiveTimeoutAndQueuedAbortSeparately() throws {
        let controller = AppleGattIoController()
        let connection = connectionCorrelation(generation: 1, connectionId: 10)
        let active = rssiCommand(commandId: 1, connectionId: 10)
        let characteristic = FakeIoCharacteristic(properties: properties(readable: true))
        let queued = readCommand(
            commandId: 2,
            connectionId: 10,
            sessionId: 20,
            characteristicId: 30
        )
        let target = target(
            connection: connection,
            sessionId: 20,
            characteristicId: 30,
            characteristic: characteristic
        )
        try controller.enqueueRssi(command: active, connection: connection)
        try controller.enqueueRead(command: queued, connection: connection, target: target)
        guard case .rssi = try nextAction(controller, connection: connection) else {
            return XCTFail("RSSI did not become the active head")
        }

        let timeout = BleEventError(
            category: .timeout,
            message: "active operation timed out",
            platformDetails: nil
        )
        let aborted = BleEventError(
            category: .operationAborted,
            message: "queued operation was retired",
            platformDetails: nil
        )
        let retirement = controller.retireAll(
            connection: connection,
            error: aborted,
            activeOperationError: timeout,
            queuedOperationError: aborted
        )
        XCTAssertEqual(retirement.ioCompletions.count, 2)
        guard case .failed(command: .rssi(let retiredActive), error: let activeError) =
            retirement.ioCompletions[0],
              case .failed(command: .read(let retiredQueued), error: let queuedError) =
            retirement.ioCompletions[1]
        else {
            return XCTFail("retirement did not preserve active/queued operation order")
        }
        XCTAssertEqual(retiredActive, active)
        XCTAssertEqual(activeError.category, .timeout)
        XCTAssertEqual(retiredQueued, queued)
        XCTAssertEqual(queuedError.category, .operationAborted)
    }

    func testQueuedCancellationRemovesOnlyQueuedOperationAndActiveHeadIsNotCancellable() {
        let controller = AppleGattIoController()
        let connection = connectionCorrelation(generation: 1, connectionId: 10)
        let characteristic = FakeIoCharacteristic(properties: properties(readable: true))
        let first = readCommand(
            commandId: 1,
            connectionId: 10,
            sessionId: 20,
            characteristicId: 30
        )
        let second = readCommand(
            commandId: 2,
            connectionId: 10,
            sessionId: 20,
            characteristicId: 30
        )
        let target = target(
            connection: connection,
            sessionId: 20,
            characteristicId: 30,
            characteristic: characteristic
        )

        XCTAssertNoThrow(try controller.enqueueRead(
            command: first,
            connection: connection,
            target: target
        ))
        XCTAssertNoThrow(try controller.enqueueRead(
            command: second,
            connection: connection,
            target: target
        ))
        XCTAssertEqual(controller.pendingCount, 2)

        guard case .queued = controller.cancel(commandId: second.commandId) else {
            return XCTFail("queued operation was not removed")
        }
        XCTAssertEqual(controller.pendingCount, 1)
        let snapshot = controller.diagnosticSnapshotGattIo(connectionId: connection.connectionId)
        XCTAssertEqual(snapshot?.headCommandId, first.commandId)
        XCTAssertEqual(snapshot?.headState, .awaitingDrive)
        XCTAssertEqual(snapshot?.queuedCommandIds, [])
        XCTAssertEqual(controller.diagnosticSnapshotGattIo(connectionId: connection.connectionId), snapshot)

        guard case .read = try? controller.nextAction(
            connection: connection,
            canSendWriteWithoutResponse: true,
            maximumWriteWithResponse: 512,
            maximumWriteWithoutResponse: 512
        ) else {
            return XCTFail("remaining operation did not become the active head")
        }
        guard case .active = controller.cancel(commandId: first.commandId) else {
            return XCTFail("active native operation was incorrectly cancelled")
        }
        XCTAssertEqual(controller.pendingCount, 1)
    }

    func testRejectsStaleAndMismatchedCommandsAndTargets() {
        let controller = AppleGattIoController()
        let connection = connectionCorrelation(generation: 1, connectionId: 10)
        let characteristic = FakeIoCharacteristic(properties: properties(readable: true))
        let command = readCommand(
            commandId: 1,
            connectionId: 10,
            sessionId: 20,
            characteristicId: 30
        )

        assertInvalidState {
            try controller.enqueueRead(
                command: readCommand(
                    commandId: 2,
                    connectionId: 11,
                    sessionId: 20,
                    characteristicId: 30
                ),
                connection: connection,
                target: target(
                    connection: connection,
                    sessionId: 20,
                    characteristicId: 30,
                    characteristic: characteristic
                )
            )
        }
        assertInvalidState {
            try controller.enqueueRead(
                command: command,
                connection: connection,
                target: AppleGattCharacteristicTarget(
                    connectionGeneration: 2,
                    gattSessionId: command.gattSessionId,
                    characteristicId: command.characteristicId,
                    characteristic: characteristic
                )
            )
        }
        assertInvalidState {
            try controller.enqueueRead(
                command: command,
                connection: connection,
                target: target(
                    connection: connection,
                    sessionId: 21,
                    characteristicId: 30,
                    characteristic: characteristic
                )
            )
        }
        assertInvalidState {
            try controller.enqueueRead(
                command: command,
                connection: connection,
                target: target(
                    connection: connection,
                    sessionId: 20,
                    characteristicId: 31,
                    characteristic: characteristic
                )
            )
        }
        XCTAssertEqual(controller.pendingCount, 0)
    }

    func testValidatesCharacteristicPropertiesBeforeAcceptance() {
        let controller = AppleGattIoController()
        let connection = connectionCorrelation(generation: 2, connectionId: 40)
        let unsupported = FakeIoCharacteristic(properties: properties())

        assertInvalidRequest {
            try controller.enqueueRead(
                command: readCommand(
                    commandId: 10,
                    connectionId: 40,
                    sessionId: 41,
                    characteristicId: 42
                ),
                connection: connection,
                target: target(
                    connection: connection,
                    sessionId: 41,
                    characteristicId: 42,
                    characteristic: unsupported
                )
            )
        }
        assertInvalidRequest {
            try controller.enqueueWrite(
                command: writeCommand(
                    commandId: 11,
                    connectionId: 40,
                    sessionId: 41,
                    characteristicId: 42,
                    value: [1],
                    writeType: .withResponse
                ),
                connection: connection,
                target: target(
                    connection: connection,
                    sessionId: 41,
                    characteristicId: 42,
                    characteristic: unsupported
                )
            )
        }
        assertInvalidRequest {
            try controller.enqueueWrite(
                command: writeCommand(
                    commandId: 12,
                    connectionId: 40,
                    sessionId: 41,
                    characteristicId: 42,
                    value: [1],
                    writeType: .withoutResponse
                ),
                connection: connection,
                target: target(
                    connection: connection,
                    sessionId: 41,
                    characteristicId: 42,
                    characteristic: unsupported
                )
            )
        }
        XCTAssertEqual(controller.pendingCount, 0)
    }

    func testWireWriteLengthAccepts512AndRejects513BytesSynchronously() throws {
        let controller = AppleGattIoController()
        let connection = connectionCorrelation(generation: 3, connectionId: 50)
        let characteristic = FakeIoCharacteristic(
            properties: properties(writableWithResponse: true)
        )
        let accepted = writeCommand(
            commandId: 20,
            connectionId: 50,
            sessionId: 51,
            characteristicId: 52,
            value: [UInt8](repeating: 0, count: 512),
            writeType: .withResponse
        )

        XCTAssertNoThrow(
            try controller.enqueueWrite(
                command: accepted,
                connection: connection,
                target: target(
                    connection: connection,
                    sessionId: 51,
                    characteristicId: 52,
                    characteristic: characteristic
                )
            )
        )
        assertInvalidRequest {
            try controller.enqueueWrite(
                command: writeCommand(
                    commandId: 21,
                    connectionId: 50,
                    sessionId: 51,
                    characteristicId: 52,
                    value: [UInt8](repeating: 0, count: 513),
                    writeType: .withResponse
                ),
                connection: connection,
                target: target(
                    connection: connection,
                    sessionId: 51,
                    characteristicId: 52,
                    characteristic: characteristic
                )
            )
        }
        XCTAssertEqual(controller.pendingCount, 1)
    }

    func testDuplicateUuidInstancesRequireExactCharacteristicObjectIdentity() throws {
        let controller = AppleGattIoController()
        let connection = connectionCorrelation(generation: 4, connectionId: 60)
        let first = FakeIoCharacteristic(properties: properties(readable: true))
        let duplicate = FakeIoCharacteristic(
            uuid: first.appleGattUuid,
            properties: properties(readable: true)
        )
        let command = readCommand(
            commandId: 30,
            connectionId: 60,
            sessionId: 61,
            characteristicId: 62
        )
        try controller.enqueueRead(
            command: command,
            connection: connection,
            target: target(
                connection: connection,
                sessionId: 61,
                characteristicId: 62,
                characteristic: first
            )
        )

        let action = try XCTUnwrap(nextAction(controller, connection: connection))
        assertReadAction(action, command: command, characteristic: first)
        XCTAssertNil(
            controller.didReadValue(
                connection: connection,
                peripheral: connection.peripheral,
                characteristic: duplicate,
                value: [1],
                error: nil
            )
        )
        XCTAssertEqual(controller.pendingCount, 1)
        assertReadCompletion(
            controller.didReadValue(
                connection: connection,
                peripheral: connection.peripheral,
                characteristic: first,
                value: [2],
                error: nil
            ),
            command: command,
            value: [2]
        )
    }

    func testFifoIsPerConnectionAndConnectionsDriveIndependently() throws {
        let controller = AppleGattIoController()
        let firstConnection = connectionCorrelation(generation: 5, connectionId: 70)
        let secondConnection = connectionCorrelation(generation: 6, connectionId: 71)
        let firstCharacteristic = FakeIoCharacteristic(
            properties: properties(readable: true, writableWithoutResponse: true)
        )
        let secondCharacteristic = FakeIoCharacteristic(
            properties: properties(writableWithResponse: true)
        )
        let read = readCommand(
            commandId: 40,
            connectionId: 70,
            sessionId: 72,
            characteristicId: 73
        )
        let withoutResponse = writeCommand(
            commandId: 41,
            connectionId: 70,
            sessionId: 72,
            characteristicId: 73,
            value: [1],
            writeType: .withoutResponse
        )
        let withResponse = writeCommand(
            commandId: 42,
            connectionId: 71,
            sessionId: 74,
            characteristicId: 75,
            value: [2],
            writeType: .withResponse
        )
        try controller.enqueueRead(
            command: read,
            connection: firstConnection,
            target: target(
                connection: firstConnection,
                sessionId: 72,
                characteristicId: 73,
                characteristic: firstCharacteristic
            )
        )
        try controller.enqueueWrite(
            command: withoutResponse,
            connection: firstConnection,
            target: target(
                connection: firstConnection,
                sessionId: 72,
                characteristicId: 73,
                characteristic: firstCharacteristic
            )
        )
        try controller.enqueueWrite(
            command: withResponse,
            connection: secondConnection,
            target: target(
                connection: secondConnection,
                sessionId: 74,
                characteristicId: 75,
                characteristic: secondCharacteristic
            )
        )
        XCTAssertTrue(controller.hasPending(connection: firstConnection))
        XCTAssertTrue(controller.hasPending(connection: secondConnection))

        assertReadAction(
            try XCTUnwrap(nextAction(controller, connection: firstConnection)),
            command: read,
            characteristic: firstCharacteristic
        )
        XCTAssertNil(try nextAction(controller, connection: firstConnection))
        assertWriteWithResponseAction(
            try XCTUnwrap(nextAction(controller, connection: secondConnection)),
            command: withResponse,
            characteristic: secondCharacteristic
        )
        assertReadCompletion(
            controller.didReadValue(
                connection: firstConnection,
                peripheral: firstConnection.peripheral,
                characteristic: firstCharacteristic,
                value: [3],
                error: nil
            ),
            command: read,
            value: [3]
        )
        assertWriteWithoutResponseAction(
            try XCTUnwrap(nextAction(controller, connection: firstConnection)),
            command: withoutResponse,
            characteristic: firstCharacteristic
        )
        XCTAssertEqual(controller.pendingCount, 2)
    }

    func testPerConnectionQueueCapRejectsWithoutMutationAndPreservesFifo() throws {
        let controller = AppleGattIoController()
        let connection = connectionCorrelation(generation: 60, connectionId: 600)
        let writeCharacteristic = FakeIoCharacteristic(
            properties: properties(writableWithoutResponse: true)
        )
        let writeTarget = target(
            connection: connection,
            sessionId: 601,
            characteristicId: 602,
            characteristic: writeCharacteristic
        )

        for index in 0 ..< 1_024 {
            try controller.enqueueWrite(
                command: writeCommand(
                    commandId: 10_000 + UInt64(index),
                    connectionId: 600,
                    sessionId: 601,
                    characteristicId: 602,
                    value: [UInt8(index % 256)],
                    writeType: .withoutResponse
                ),
                connection: connection,
                target: writeTarget
            )
        }
        XCTAssertEqual(controller.pendingCount, 1_024)

        let rejectedWrite = writeCommand(
            commandId: 20_000,
            connectionId: 600,
            sessionId: 601,
            characteristicId: 602,
            value: [1],
            writeType: .withoutResponse
        )
        assertInvalidState {
            try controller.enqueueWrite(
                command: rejectedWrite,
                connection: connection,
                target: writeTarget
            )
        }

        let notificationCharacteristic = FakeIoCharacteristic(
            properties: properties(notifiable: true)
        )
        let notificationTarget = target(
            connection: connection,
            sessionId: 601,
            characteristicId: 603,
            characteristic: notificationCharacteristic
        )
        let rejectedEnable = notificationCommand(
            commandId: 20_001,
            connectionId: 600,
            sessionId: 601,
            subscriptionId: 604,
            characteristicId: 603,
            action: .enable(mode: .platformDefault)
        )
        assertInvalidState {
            try controller.enqueueSetNotifications(
                command: rejectedEnable,
                connection: connection,
                target: notificationTarget
            )
        }
        XCTAssertEqual(controller.pendingCount, 1_024)

        for index in 0 ..< 1_024 {
            let action = try XCTUnwrap(nextAction(controller, connection: connection))
            guard case .writeWithoutResponse(let command, _, let characteristic) = action else {
                return XCTFail("expected queued write without response at index \(index)")
            }
            XCTAssertEqual(command.commandId, CommandId(value: 10_000 + UInt64(index)))
            XCTAssertEqual(ObjectIdentifier(characteristic), ObjectIdentifier(writeCharacteristic))
            guard case .write(let completedCommand) = try XCTUnwrap(
                controller.didSubmitWriteWithoutResponse(
                    connection: connection,
                    peripheral: connection.peripheral,
                    characteristic: writeCharacteristic
                )
            ) else {
                return XCTFail("expected write completion at index \(index)")
            }
            XCTAssertEqual(completedCommand.commandId, command.commandId)
        }
        XCTAssertEqual(controller.pendingCount, 0)

        XCTAssertNoThrow(
            try controller.enqueueSetNotifications(
                command: rejectedEnable,
                connection: connection,
                target: notificationTarget
            )
        )
        XCTAssertEqual(controller.pendingCount, 1)
    }

    func testReadCallbacksMatchGenerationPeripheralKindAndCarrySuccessOrError() throws {
        let controller = AppleGattIoController()
        let peripheral = FakeIoPeripheral(identifier: UUID())
        let connection = connectionCorrelation(
            generation: 7,
            connectionId: 80,
            peripheral: peripheral
        )
        let staleConnection = connectionCorrelation(
            generation: 8,
            connectionId: 80,
            peripheral: peripheral
        )
        let characteristic = FakeIoCharacteristic(properties: properties(readable: true))
        let first = readCommand(
            commandId: 50,
            connectionId: 80,
            sessionId: 81,
            characteristicId: 82
        )
        try controller.enqueueRead(
            command: first,
            connection: connection,
            target: target(
                connection: connection,
                sessionId: 81,
                characteristicId: 82,
                characteristic: characteristic
            )
        )
        _ = try nextAction(controller, connection: connection)

        XCTAssertNil(
            controller.didWriteValue(
                connection: connection,
                peripheral: peripheral,
                characteristic: characteristic,
                error: nil
            )
        )
        XCTAssertNil(
            controller.didReadValue(
                connection: staleConnection,
                peripheral: peripheral,
                characteristic: characteristic,
                value: [4],
                error: nil
            )
        )
        XCTAssertNil(
            controller.didReadValue(
                connection: connection,
                peripheral: FakeIoPeripheral(identifier: peripheral.identifier),
                characteristic: characteristic,
                value: [4],
                error: nil
            )
        )
        assertReadCompletion(
            controller.didReadValue(
                connection: connection,
                peripheral: peripheral,
                characteristic: characteristic,
                value: [4],
                error: nil
            ),
            command: first,
            value: [4]
        )

        let callbackError = eventError(category: .gatt, message: "read failed")
        let second = readCommand(
            commandId: 51,
            connectionId: 80,
            sessionId: 81,
            characteristicId: 82
        )
        try controller.enqueueRead(
            command: second,
            connection: connection,
            target: target(
                connection: connection,
                sessionId: 81,
                characteristicId: 82,
                characteristic: characteristic
            )
        )
        _ = try nextAction(controller, connection: connection)
        assertFailure(
            controller.didReadValue(
                connection: connection,
                peripheral: peripheral,
                characteristic: characteristic,
                value: nil,
                error: callbackError
            ),
            commandId: second.commandId,
            category: .gatt,
            message: "read failed"
        )

        let third = readCommand(
            commandId: 52,
            connectionId: 80,
            sessionId: 81,
            characteristicId: 82
        )
        try controller.enqueueRead(
            command: third,
            connection: connection,
            target: target(
                connection: connection,
                sessionId: 81,
                characteristicId: 82,
                characteristic: characteristic
            )
        )
        _ = try nextAction(controller, connection: connection)
        assertFailure(
            controller.didReadValue(
                connection: connection,
                peripheral: peripheral,
                characteristic: characteristic,
                value: nil,
                error: nil
            ),
            commandId: third.commandId,
            category: .gatt,
            message: "CoreBluetooth characteristic read completed without a value"
        )
    }

    func testWriteWithResponseCompletesOnlyFromExactWriteCallback() throws {
        let controller = AppleGattIoController()
        let connection = connectionCorrelation(generation: 9, connectionId: 90)
        let characteristic = FakeIoCharacteristic(
            properties: properties(writableWithResponse: true)
        )
        let first = writeCommand(
            commandId: 60,
            connectionId: 90,
            sessionId: 91,
            characteristicId: 92,
            value: [1],
            writeType: .withResponse
        )
        try controller.enqueueWrite(
            command: first,
            connection: connection,
            target: target(
                connection: connection,
                sessionId: 91,
                characteristicId: 92,
                characteristic: characteristic
            )
        )
        _ = try nextAction(controller, connection: connection)
        XCTAssertNil(
            controller.didReadValue(
                connection: connection,
                peripheral: connection.peripheral,
                characteristic: characteristic,
                value: [1],
                error: nil
            )
        )
        assertWriteCompletion(
            controller.didWriteValue(
                connection: connection,
                peripheral: connection.peripheral,
                characteristic: characteristic,
                error: nil
            ),
            command: first
        )

        let second = writeCommand(
            commandId: 61,
            connectionId: 90,
            sessionId: 91,
            characteristicId: 92,
            value: [2],
            writeType: .withResponse
        )
        try controller.enqueueWrite(
            command: second,
            connection: connection,
            target: target(
                connection: connection,
                sessionId: 91,
                characteristicId: 92,
                characteristic: characteristic
            )
        )
        _ = try nextAction(controller, connection: connection)
        assertFailure(
            controller.didWriteValue(
                connection: connection,
                peripheral: connection.peripheral,
                characteristic: characteristic,
                error: eventError(category: .gatt, message: "write failed")
            ),
            commandId: second.commandId,
            category: .gatt,
            message: "write failed"
        )
    }

    func testWriteWithoutResponseWaitsForCapacityAndCompletesOnlyAfterSubmission() throws {
        let controller = AppleGattIoController()
        let connection = connectionCorrelation(generation: 10, connectionId: 100)
        let characteristic = FakeIoCharacteristic(
            properties: properties(writableWithoutResponse: true)
        )
        let duplicate = FakeIoCharacteristic(
            uuid: characteristic.appleGattUuid,
            properties: properties(writableWithoutResponse: true)
        )
        let command = writeCommand(
            commandId: 70,
            connectionId: 100,
            sessionId: 101,
            characteristicId: 102,
            value: [1, 2],
            writeType: .withoutResponse
        )
        try controller.enqueueWrite(
            command: command,
            connection: connection,
            target: target(
                connection: connection,
                sessionId: 101,
                characteristicId: 102,
                characteristic: characteristic
            )
        )

        XCTAssertEqual(
            controller.diagnosticSnapshotGattIo(connectionId: connection.connectionId)?.headState,
            .awaitingDrive
        )
        assertWriteCapacityWaitAction(
            try XCTUnwrap(nextAction(
                controller,
                connection: connection,
                canSendWriteWithoutResponse: false
            )),
            command: command,
            connection: connection
        )
        let waiting = try XCTUnwrap(controller.diagnosticSnapshotGattIo(connectionId: connection.connectionId))
        XCTAssertEqual(waiting.headCommandId, command.commandId)
        XCTAssertEqual(waiting.headState, .waitingForWriteCapacity)
        XCTAssertNotEqual(waiting.headState, .awaitingNativeCallback)
        XCTAssertEqual(controller.diagnosticSnapshotGattIo(connectionId: connection.connectionId), waiting)
        XCTAssertTrue(controller.isWaitingForWriteCapacity(connection: connection, commandId: command.commandId))
        XCTAssertTrue(
            controller.didBecomeReadyToSendWriteWithoutResponse(
                connection: connection,
                peripheral: connection.peripheral
            )
        )
        assertWriteWithoutResponseAction(
            try XCTUnwrap(nextAction(controller, connection: connection)),
            command: command,
            characteristic: characteristic
        )
        let submitting = try XCTUnwrap(controller.diagnosticSnapshotGattIo(connectionId: connection.connectionId))
        XCTAssertEqual(submitting.headCommandId, command.commandId)
        XCTAssertEqual(submitting.headState, .submittingWriteWithoutResponse)
        XCTAssertEqual(controller.diagnosticSnapshotGattIo(connectionId: connection.connectionId), submitting)
        XCTAssertNil(try nextAction(controller, connection: connection))
        XCTAssertNil(
            controller.didSubmitWriteWithoutResponse(
                connection: connection,
                peripheral: connection.peripheral,
                characteristic: duplicate
            )
        )
        XCTAssertEqual(controller.pendingCount, 1)
        assertWriteCompletion(
            controller.didSubmitWriteWithoutResponse(
                connection: connection,
                peripheral: connection.peripheral,
                characteristic: characteristic
            ),
            command: command
        )
        XCTAssertFalse(
            controller.didBecomeReadyToSendWriteWithoutResponse(
                connection: connection,
                peripheral: connection.peripheral
            )
        )
        XCTAssertEqual(controller.pendingCount, 0)
    }

    func testBothWriteModesProjectCorrelatedWrittenBeforeTerminalAndFailureWithoutDomain() {
        for writeType: WriteType in [.withResponse, .withoutResponse] {
            let command = writeCommand(
                commandId: 70,
                connectionId: 100,
                sessionId: 101,
                characteristicId: 102,
                value: [1, 2],
                writeType: writeType
            )
            XCTAssertEqual(AppleBackendEventProjection.gattIoEvents(.write(command: command)), [
                .characteristicWritten(
                    commandId: command.commandId,
                    connectionId: command.connectionId,
                    gattSessionId: command.gattSessionId,
                    characteristicId: command.characteristicId
                ),
                .commandCompleted(commandId: command.commandId, error: nil),
            ])
            let error = eventError(category: .gatt, message: "write failed")
            XCTAssertEqual(AppleBackendEventProjection.gattIoEvents(
                .failed(command: .write(command), error: error)
            ), [.commandCompleted(commandId: command.commandId, error: error)])
        }
    }

    func testUsesSeparatePlatformMaximumsAndReturnsTerminalFailures() throws {
        let controller = AppleGattIoController()
        let connection = connectionCorrelation(generation: 11, connectionId: 110)
        let characteristic = FakeIoCharacteristic(
            properties: properties(
                writableWithResponse: true,
                writableWithoutResponse: true
            )
        )
        let withResponse = writeCommand(
            commandId: 80,
            connectionId: 110,
            sessionId: 111,
            characteristicId: 112,
            value: [UInt8](repeating: 1, count: 10),
            writeType: .withResponse
        )
        let withoutResponse = writeCommand(
            commandId: 81,
            connectionId: 110,
            sessionId: 111,
            characteristicId: 112,
            value: [UInt8](repeating: 2, count: 8),
            writeType: .withoutResponse
        )
        let ioTarget = target(
            connection: connection,
            sessionId: 111,
            characteristicId: 112,
            characteristic: characteristic
        )
        try controller.enqueueWrite(
            command: withResponse,
            connection: connection,
            target: ioTarget
        )
        try controller.enqueueWrite(
            command: withoutResponse,
            connection: connection,
            target: ioTarget
        )

        assertTerminalFailure(
            try XCTUnwrap(
                nextAction(
                    controller,
                    connection: connection,
                    maximumWriteWithResponse: 9,
                    maximumWriteWithoutResponse: 8
                )
            ),
            commandId: withResponse.commandId,
            category: .gatt
        )
        assertWriteWithoutResponseAction(
            try XCTUnwrap(
                nextAction(
                    controller,
                    connection: connection,
                    maximumWriteWithResponse: 1,
                    maximumWriteWithoutResponse: 8
                )
            ),
            command: withoutResponse,
            characteristic: characteristic
        )
        _ = controller.didSubmitWriteWithoutResponse(
            connection: connection,
            peripheral: connection.peripheral,
            characteristic: characteristic
        )

        let secondWithoutResponse = writeCommand(
            commandId: 82,
            connectionId: 110,
            sessionId: 111,
            characteristicId: 112,
            value: [UInt8](repeating: 3, count: 8),
            writeType: .withoutResponse
        )
        try controller.enqueueWrite(
            command: secondWithoutResponse,
            connection: connection,
            target: ioTarget
        )
        assertTerminalFailure(
            try XCTUnwrap(
                nextAction(
                    controller,
                    connection: connection,
                    maximumWriteWithResponse: 512,
                    maximumWriteWithoutResponse: 7
                )
            ),
            commandId: secondWithoutResponse.commandId,
            category: .gatt
        )
        XCTAssertEqual(controller.pendingCount, 0)
    }

    func testNotificationModeSelectionUsesCoreBluetoothMatrix() {
        let notify = properties(notifiable: true)
        let indicate = properties(indicatable: true)
        let both = properties(notifiable: true, indicatable: true)
        let neither = properties()

        XCTAssertEqual(
            AppleGattIoController.selectNotificationMode(
                request: .platformDefault,
                properties: both
            ),
            .supported(.notification)
        )
        XCTAssertEqual(
            AppleGattIoController.selectNotificationMode(
                request: .platformDefault,
                properties: indicate
            ),
            .supported(.indication)
        )
        XCTAssertEqual(
            AppleGattIoController.selectNotificationMode(
                request: .requireNotification,
                properties: notify
            ),
            .supported(.notification)
        )
        XCTAssertEqual(
            AppleGattIoController.selectNotificationMode(
                request: .requireIndication,
                properties: indicate
            ),
            .supported(.indication)
        )
        XCTAssertEqual(
            AppleGattIoController.selectNotificationMode(
                request: .requireIndication,
                properties: both
            ),
            .strictUnsupported(.indication)
        )
        XCTAssertEqual(
            AppleGattIoController.selectNotificationMode(
                request: .requireNotification,
                properties: indicate
            ),
            .strictUnsupported(.notification)
        )
        XCTAssertEqual(
            AppleGattIoController.selectNotificationMode(
                request: .platformDefault,
                properties: neither
            ),
            .noSupportedMode
        )
    }

    func testNotificationValidationConflictsAndStrictUnsupportedTerminal() throws {
        let controller = AppleGattIoController()
        let connection = connectionCorrelation(generation: 20, connectionId: 200)
        let characteristic = FakeIoCharacteristic(
            properties: properties(
                readable: true,
                writableWithResponse: true,
                notifiable: true,
                indicatable: true
            )
        )
        let ioTarget = target(
            connection: connection,
            sessionId: 201,
            characteristicId: 202,
            characteristic: characteristic
        )
        let read = readCommand(
            commandId: 200,
            connectionId: 200,
            sessionId: 201,
            characteristicId: 202
        )
        try controller.enqueueRead(command: read, connection: connection, target: ioTarget)
        assertInvalidState {
            try controller.enqueueSetNotifications(
                command: notificationCommand(
                    commandId: 201,
                    connectionId: 200,
                    sessionId: 201,
                    subscriptionId: 203,
                    characteristicId: 202,
                    action: .enable(mode: .platformDefault)
                ),
                connection: connection,
                target: ioTarget
            )
        }
        _ = try nextAction(controller, connection: connection)
        _ = controller.didReadValue(
            connection: connection,
            peripheral: connection.peripheral,
            characteristic: characteristic,
            value: [1],
            error: nil
        )

        let unsupported = notificationCommand(
            commandId: 202,
            connectionId: 200,
            sessionId: 201,
            subscriptionId: 204,
            characteristicId: 202,
            action: .enable(mode: .requireIndication)
        )
        try controller.enqueueSetNotifications(
            command: unsupported,
            connection: connection,
            target: ioTarget
        )
        assertInvalidRequest {
            try controller.enqueueSetNotifications(
                command: notificationCommand(
                    commandId: 299,
                    connectionId: 200,
                    sessionId: 201,
                    subscriptionId: 204,
                    characteristicId: 207,
                    action: .enable(mode: .requireNotification)
                ),
                connection: connection,
                target: target(
                    connection: connection,
                    sessionId: 201,
                    characteristicId: 207,
                    characteristic: FakeIoCharacteristic(properties: properties(notifiable: true))
                )
            )
        }
        assertInvalidState {
            try controller.enqueueSetNotifications(
                command: notificationCommand(
                    commandId: 298,
                    connectionId: 200,
                    sessionId: 201,
                    subscriptionId: 208,
                    characteristicId: 202,
                    action: .enable(mode: .requireNotification)
                ),
                connection: connection,
                target: ioTarget
            )
        }
        assertNotificationTerminalFailure(
            try XCTUnwrap(controller.nextNotificationAction(connection: connection)),
            command: unsupported,
            category: .notSupported
        )

        let neither = FakeIoCharacteristic(properties: properties())
        assertInvalidRequest {
            try controller.enqueueSetNotifications(
                command: notificationCommand(
                    commandId: 203,
                    connectionId: 200,
                    sessionId: 201,
                    subscriptionId: 205,
                    characteristicId: 206,
                    action: .enable(mode: .platformDefault)
                ),
                connection: connection,
                target: target(
                    connection: connection,
                    sessionId: 201,
                    characteristicId: 206,
                    characteristic: neither
                )
            )
        }
    }

    func testNotificationRouteIsReservedBeforeNativeEnableAndCallbackIsExact() throws {
        let controller = AppleGattIoController()
        let peripheral = FakeIoPeripheral(identifier: UUID())
        let connection = connectionCorrelation(
            generation: 21,
            connectionId: 210,
            peripheral: peripheral
        )
        let staleConnection = connectionCorrelation(
            generation: 22,
            connectionId: 210,
            peripheral: peripheral
        )
        let characteristic = FakeIoCharacteristic(
            properties: properties(readable: true, notifiable: true)
        )
        let duplicate = FakeIoCharacteristic(
            uuid: characteristic.appleGattUuid,
            properties: characteristic.appleGattProperties
        )
        let command = notificationCommand(
            commandId: 210,
            connectionId: 210,
            sessionId: 211,
            subscriptionId: 212,
            characteristicId: 213,
            action: .enable(mode: .platformDefault)
        )
        let ioTarget = target(
            connection: connection,
            sessionId: 211,
            characteristicId: 213,
            characteristic: characteristic
        )
        try controller.enqueueSetNotifications(
            command: command,
            connection: connection,
            target: ioTarget
        )

        assertIgnoredValue(
            controller.didUpdateValue(
                connection: connection,
                peripheral: peripheral,
                characteristic: characteristic,
                value: [1, 2],
                error: nil
            )
        )
        assertInvalidState {
            try controller.enqueueRead(
                command: readCommand(
                    commandId: 211,
                    connectionId: 210,
                    sessionId: 211,
                    characteristicId: 213
                ),
                connection: connection,
                target: ioTarget
            )
        }
        assertSetNotifyValueAction(
            try XCTUnwrap(controller.nextNotificationAction(connection: connection)),
            command: command,
            characteristic: characteristic,
            enabled: true
        )
        assertNotificationValue(
            controller.didUpdateValue(
                connection: connection,
                peripheral: peripheral,
                characteristic: characteristic,
                value: [1, 2],
                error: nil
            ),
            command: command,
            value: [1, 2]
        )
        XCTAssertNil(
            controller.didUpdateNotificationState(
                connection: staleConnection,
                peripheral: peripheral,
                characteristic: characteristic,
                error: nil
            )
        )
        XCTAssertNil(
            controller.didUpdateNotificationState(
                connection: connection,
                peripheral: FakeIoPeripheral(identifier: peripheral.identifier),
                characteristic: characteristic,
                error: nil
            )
        )
        XCTAssertNil(
            controller.didUpdateNotificationState(
                connection: connection,
                peripheral: peripheral,
                characteristic: duplicate,
                error: nil
            )
        )
        assertNotificationEnabled(
            controller.didUpdateNotificationState(
                connection: connection,
                peripheral: peripheral,
                characteristic: characteristic,
                error: nil
            ),
            command: command,
            actualMode: .notification
        )
    }

    func testNotificationOperationsShareFifoAndValuesCannotCompleteReads() throws {
        let controller = AppleGattIoController()
        let connection = connectionCorrelation(generation: 23, connectionId: 220)
        let readCharacteristic = FakeIoCharacteristic(
            properties: properties(readable: true, writableWithoutResponse: true)
        )
        let notificationCharacteristic = FakeIoCharacteristic(
            properties: properties(notifiable: true)
        )
        let read = readCommand(
            commandId: 220,
            connectionId: 220,
            sessionId: 221,
            characteristicId: 222
        )
        let enable = notificationCommand(
            commandId: 221,
            connectionId: 220,
            sessionId: 221,
            subscriptionId: 223,
            characteristicId: 224,
            action: .enable(mode: .platformDefault)
        )
        let write = writeCommand(
            commandId: 222,
            connectionId: 220,
            sessionId: 221,
            characteristicId: 222,
            value: [3],
            writeType: .withoutResponse
        )
        try controller.enqueueRead(
            command: read,
            connection: connection,
            target: target(
                connection: connection,
                sessionId: 221,
                characteristicId: 222,
                characteristic: readCharacteristic
            )
        )
        try controller.enqueueSetNotifications(
            command: enable,
            connection: connection,
            target: target(
                connection: connection,
                sessionId: 221,
                characteristicId: 224,
                characteristic: notificationCharacteristic
            )
        )
        try controller.enqueueWrite(
            command: write,
            connection: connection,
            target: target(
                connection: connection,
                sessionId: 221,
                characteristicId: 222,
                characteristic: readCharacteristic
            )
        )

        XCTAssertNil(controller.nextNotificationAction(connection: connection))
        assertReadAction(
            try XCTUnwrap(nextAction(controller, connection: connection)),
            command: read,
            characteristic: readCharacteristic
        )
        assertIgnoredValue(
            controller.didUpdateValue(
                connection: connection,
                peripheral: connection.peripheral,
                characteristic: notificationCharacteristic,
                value: [4],
                error: nil
            )
        )
        XCTAssertEqual(controller.pendingCount, 3)
        guard case .read(let completion) = controller.didUpdateValue(
            connection: connection,
            peripheral: connection.peripheral,
            characteristic: readCharacteristic,
            value: [5],
            error: nil
        ) else {
            return XCTFail("expected read disposition")
        }
        assertReadCompletion(completion, command: read, value: [5])
        assertSetNotifyValueAction(
            try XCTUnwrap(controller.nextNotificationAction(connection: connection)),
            command: enable,
            characteristic: notificationCharacteristic,
            enabled: true
        )
        assertNotificationValue(
            controller.didUpdateValue(
                connection: connection,
                peripheral: connection.peripheral,
                characteristic: notificationCharacteristic,
                value: [4],
                error: nil
            ),
            command: enable,
            value: [4]
        )
        _ = controller.didUpdateNotificationState(
            connection: connection,
            peripheral: connection.peripheral,
            characteristic: notificationCharacteristic,
            error: nil
        )
        assertWriteWithoutResponseAction(
            try XCTUnwrap(nextAction(controller, connection: connection)),
            command: write,
            characteristic: readCharacteristic
        )
    }

    func testDisableErrorRestoresActiveRouteAndQueuedDisableAcceptsValues() throws {
        let controller = AppleGattIoController()
        let connection = connectionCorrelation(generation: 24, connectionId: 230)
        let characteristic = FakeIoCharacteristic(
            properties: properties(writableWithResponse: true, notifiable: true)
        )
        let ioTarget = target(
            connection: connection,
            sessionId: 231,
            characteristicId: 232,
            characteristic: characteristic
        )
        let enable = notificationCommand(
            commandId: 230,
            connectionId: 230,
            sessionId: 231,
            subscriptionId: 233,
            characteristicId: 232,
            action: .enable(mode: .platformDefault)
        )
        try controller.enqueueSetNotifications(command: enable, connection: connection, target: ioTarget)
        _ = controller.nextNotificationAction(connection: connection)
        _ = controller.didUpdateNotificationState(
            connection: connection,
            peripheral: connection.peripheral,
            characteristic: characteristic,
            error: nil
        )

        let duplicate = FakeIoCharacteristic(
            uuid: characteristic.appleGattUuid,
            properties: characteristic.appleGattProperties
        )
        assertInvalidRequest {
            try controller.enqueueSetNotifications(
                command: notificationCommand(
                    commandId: 299,
                    connectionId: 230,
                    sessionId: 231,
                    subscriptionId: 233,
                    characteristicId: 232,
                    action: .disable
                ),
                connection: connection,
                target: target(
                    connection: connection,
                    sessionId: 231,
                    characteristicId: 232,
                    characteristic: duplicate
                )
            )
        }

        let write = writeCommand(
            commandId: 231,
            connectionId: 230,
            sessionId: 231,
            characteristicId: 232,
            value: [1],
            writeType: .withResponse
        )
        let disable = notificationCommand(
            commandId: 232,
            connectionId: 230,
            sessionId: 231,
            subscriptionId: 233,
            characteristicId: 232,
            action: .disable
        )
        try controller.enqueueWrite(command: write, connection: connection, target: ioTarget)
        try controller.enqueueSetNotifications(command: disable, connection: connection, target: ioTarget)
        assertNotificationValue(
            controller.didUpdateValue(
                connection: connection,
                peripheral: connection.peripheral,
                characteristic: characteristic,
                value: [2],
                error: nil
            ),
            command: enable,
            value: [2]
        )
        XCTAssertNil(controller.nextNotificationAction(connection: connection))
        _ = try nextAction(controller, connection: connection)
        _ = controller.didWriteValue(
            connection: connection,
            peripheral: connection.peripheral,
            characteristic: characteristic,
            error: nil
        )
        assertSetNotifyValueAction(
            try XCTUnwrap(controller.nextNotificationAction(connection: connection)),
            command: disable,
            characteristic: characteristic,
            enabled: false
        )
        let beforeFailure = controller.diagnosticNotificationRoute(
            connection: connection, peripheral: connection.peripheral, characteristic: characteristic
        )
        XCTAssertEqual(beforeFailure["phase"] as? String, "disabling")
        XCTAssertEqual(beforeFailure["activeCommandId"] as? String, "232")
        XCTAssertEqual(beforeFailure["routeMatches"] as? Bool, true)
        let disableError = eventError(category: .gatt, message: "disable failed")
        assertNotificationFailure(
            controller.didUpdateNotificationState(
                connection: connection,
                peripheral: connection.peripheral,
                characteristic: characteristic,
                isNotifying: false,
                error: disableError
            ),
            command: disable,
            category: .gatt,
            message: "disable failed"
        )
        let route = controller.diagnosticNotificationRoute(
            connection: connection, peripheral: connection.peripheral, characteristic: characteristic
        )
        XCTAssertEqual(route["phase"] as? String, "active")
        XCTAssertEqual(route["connectionId"] as? String, "230")
        XCTAssertEqual(route["connectionGeneration"] as? String, "24")
        XCTAssertEqual(route["gattSessionId"] as? String, "231")
        XCTAssertEqual(route["subscriptionId"] as? String, "233")
        XCTAssertEqual(route["characteristicId"] as? String, "232")
        XCTAssertEqual(route["subscriptionGeneration"] as? String, beforeFailure["subscriptionGeneration"] as? String)
        XCTAssertEqual(route["peripheralMatches"] as? Bool, true)
        XCTAssertEqual(route["characteristicMatches"] as? Bool, true)
        XCTAssertEqual(route["acceptsValue"] as? Bool, true)
        XCTAssertEqual(route["readBarrier"] as? Bool, false)
        XCTAssertEqual(route["notificationStateBarrier"] as? Bool, false)
        XCTAssertEqual(route["valueBarrier"] as? Bool, false)
        XCTAssertNil(route["activeCommandId"])
        XCTAssertEqual(controller.diagnosticSubscriptionCount, 1)
        XCTAssertNil(controller.nextNotificationAction(connection: connection))
        let foreign = controller.diagnosticNotificationRoute(
            connection: connection, peripheral: connection.peripheral, characteristic: duplicate
        )
        XCTAssertEqual(foreign["routeMatches"] as? Bool, false)
        XCTAssertEqual(foreign["characteristicMatches"] as? Bool, false)
        XCTAssertEqual(foreign["expectedCharacteristicObject"] as? String, route["characteristicObject"] as? String)
        assertNotificationValue(
            controller.didUpdateValue(
                connection: connection,
                peripheral: connection.peripheral,
                characteristic: characteristic,
                value: [3],
                error: nil
            ),
            command: enable,
            value: [3]
        )
        let afterValue = controller.diagnosticNotificationRoute(
            connection: connection, peripheral: connection.peripheral, characteristic: characteristic
        )
        XCTAssertEqual(afterValue as NSDictionary, route as NSDictionary)
        XCTAssertEqual(controller.pendingCount, 0)
        XCTAssertEqual(controller.diagnosticSubscriptionCount, 1)
    }

    func testRetiredReadBarrierBlocksNotificationEnableUntilLateReadArrives() throws {
        let controller = AppleGattIoController()
        let connection = connectionCorrelation(generation: 25, connectionId: 240)
        let characteristic = FakeIoCharacteristic(
            properties: properties(readable: true, notifiable: true)
        )
        let ioTarget = target(
            connection: connection,
            sessionId: 241,
            characteristicId: 242,
            characteristic: characteristic
        )
        try controller.enqueueRead(
            command: readCommand(
                commandId: 240,
                connectionId: 240,
                sessionId: 241,
                characteristicId: 242
            ),
            connection: connection,
            target: ioTarget
        )
        _ = try nextAction(controller, connection: connection)
        _ = controller.retireAll(
            connection: connection,
            error: eventError(category: .cancelled, message: "retired")
        )
        let enable = notificationCommand(
            commandId: 241,
            connectionId: 240,
            sessionId: 241,
            subscriptionId: 243,
            characteristicId: 242,
            action: .enable(mode: .platformDefault)
        )
        assertInvalidState {
            try controller.enqueueSetNotifications(
                command: enable,
                connection: connection,
                target: ioTarget
            )
        }
        for _ in 0..<2 {
            let diagnostic = controller.diagnosticNotificationRoute(
                connection: connection, peripheral: connection.peripheral, characteristic: characteristic
            )
            XCTAssertEqual(diagnostic["readBarrier"] as? Bool, true)
        }
        XCTAssertTrue(
            controller.consumeRetiredReadCallback(
                peripheral: connection.peripheral,
                characteristic: characteristic
            )
        )
        XCTAssertNoThrow(
            try controller.enqueueSetNotifications(
                command: enable,
                connection: connection,
                target: ioTarget
            )
        )
    }

    func testUnexpectedNativeEnableStatePreservesOldCorrelationUntilRetrySucceeds() throws {
        let controller = AppleGattIoController()
        let connection = connectionCorrelation(generation: 26, connectionId: 250)
        let characteristic = FakeIoCharacteristic(
            properties: properties(readable: true, notifiable: true)
        )
        let ioTarget = target(
            connection: connection,
            sessionId: 251,
            characteristicId: 252,
            characteristic: characteristic
        )
        let failedEnable = notificationCommand(
            commandId: 250,
            connectionId: 250,
            sessionId: 251,
            subscriptionId: 253,
            characteristicId: 252,
            action: .enable(mode: .platformDefault)
        )
        try controller.enqueueSetNotifications(
            command: failedEnable,
            connection: connection,
            target: ioTarget
        )
        _ = controller.nextNotificationAction(connection: connection)
        assertNotificationFailure(
            controller.didUpdateNotificationState(
                connection: connection,
                peripheral: connection.peripheral,
                characteristic: characteristic,
                isNotifying: false,
                error: nil
            ),
            command: failedEnable,
            category: .gatt
        )

        let retry = notificationCommand(
            commandId: 251,
            connectionId: 250,
            sessionId: 251,
            subscriptionId: 254,
            characteristicId: 252,
            action: .enable(mode: .platformDefault)
        )
        try controller.enqueueSetNotifications(command: retry, connection: connection, target: ioTarget)
        _ = controller.nextNotificationAction(connection: connection)
        assertNotificationValue(
            controller.didUpdateValue(
                connection: connection,
                peripheral: connection.peripheral,
                characteristic: characteristic,
                value: [1],
                error: nil
            ),
            command: failedEnable,
            value: [1]
        )
        assertNotificationEnabled(
            controller.didUpdateNotificationState(
                connection: connection,
                peripheral: connection.peripheral,
                characteristic: characteristic,
                isNotifying: true,
                error: nil
            ),
            command: retry,
            actualMode: .notification
        )
        assertNotificationValue(
            controller.didUpdateValue(
                connection: connection,
                peripheral: connection.peripheral,
                characteristic: characteristic,
                value: [2],
                error: nil
            ),
            command: retry,
            value: [2]
        )
    }

    func testRediscoveryRetiresActiveSubscriptionWithoutSyntheticCompletion() throws {
        let controller = AppleGattIoController()
        let connection = connectionCorrelation(generation: 27, connectionId: 260)
        let characteristic = FakeIoCharacteristic(properties: properties(notifiable: true))
        let ioTarget = target(
            connection: connection,
            sessionId: 261,
            characteristicId: 262,
            characteristic: characteristic
        )
        let enable = notificationCommand(
            commandId: 260,
            connectionId: 260,
            sessionId: 261,
            subscriptionId: 263,
            characteristicId: 262,
            action: .enable(mode: .platformDefault)
        )
        try controller.enqueueSetNotifications(command: enable, connection: connection, target: ioTarget)
        _ = controller.nextNotificationAction(connection: connection)
        _ = controller.didUpdateNotificationState(
            connection: connection,
            peripheral: connection.peripheral,
            characteristic: characteristic,
            isNotifying: true,
            error: nil
        )
        XCTAssertFalse(controller.hasPending(connection: connection))

        guard case .unsubscribeQueued = controller.prepareSubscriptionsForRediscovery(
            connection: connection
        ) else {
            return XCTFail("expected native subscription retirement before rediscovery")
        }
        XCTAssertTrue(controller.hasPending(connection: connection))
        let sibling = rssiCommand(commandId: 299, connectionId: 260)
        try controller.enqueueRssi(command: sibling, connection: connection)
        let internalHead = try XCTUnwrap(controller.diagnosticSnapshotGattIo(connectionId: connection.connectionId))
        XCTAssertEqual(internalHead.headKind, .retireSubscription)
        XCTAssertNil(internalHead.headCommandId)
        XCTAssertEqual(internalHead.headState, .awaitingDrive)
        XCTAssertEqual(internalHead.queuedCommandIds, [sibling.commandId], "drop head before compacting nil command IDs")
        XCTAssertEqual(controller.diagnosticSnapshotGattIo(connectionId: connection.connectionId), internalHead)
        guard case .retireForRediscovery(
            let actionConnection,
            let actionCharacteristic
        ) = try XCTUnwrap(controller.nextNotificationAction(connection: connection)) else {
            return XCTFail("expected native notification retirement before rediscovery")
        }
        XCTAssertEqual(actionConnection.generation, connection.generation)
        XCTAssertEqual(
            ObjectIdentifier(actionCharacteristic),
            ObjectIdentifier(characteristic)
        )
        let activeInternal = try XCTUnwrap(controller.diagnosticSnapshotGattIo(connectionId: connection.connectionId))
        XCTAssertEqual(activeInternal.headKind, .retireSubscription)
        XCTAssertNil(activeInternal.headCommandId)
        XCTAssertEqual(activeInternal.headState, .awaitingNativeCallback)
        XCTAssertEqual(activeInternal.queuedCommandIds, [sibling.commandId])
        XCTAssertEqual(controller.diagnosticSnapshotGattIo(connectionId: connection.connectionId), activeInternal)
        guard case .queued = controller.cancel(commandId: sibling.commandId) else {
            return XCTFail("expected queued sibling cancellation")
        }
        guard case .rediscoveryRetired = controller.didUpdateNotificationState(
            connection: connection,
            peripheral: connection.peripheral,
            characteristic: characteristic,
            isNotifying: false,
            error: nil
        ) else {
            return XCTFail("expected rediscovery notification retirement completion")
        }
        XCTAssertFalse(controller.hasPending(connection: connection))
        assertNotificationValue(
            controller.didUpdateValue(
                connection: connection,
                peripheral: connection.peripheral,
                characteristic: characteristic,
                value: [7],
                error: nil
            ),
            command: enable,
            value: [7]
        )
        XCTAssertNoThrow(
            try controller.enqueueSetNotifications(
                command: notificationCommand(
                    commandId: 261,
                    connectionId: 260,
                    sessionId: 261,
                    subscriptionId: 264,
                    characteristicId: 262,
                    action: .enable(mode: .platformDefault)
                ),
                connection: connection,
                target: ioTarget
            )
        )
        XCTAssertEqual(controller.diagnosticNotificationRoute(
            connection: connection, peripheral: connection.peripheral, characteristic: characteristic
        )["subscriptionGeneration"] as? String, "2", "snapshots must not allocate generations")
    }

    func testRediscoveryCapacityBoundaryQueuesExactlyLimitAndFailsClosedOneOver() throws {
        let maximum = AppleGattIoController.maximumPendingOperationsPerConnection
        let boundaryController = AppleGattIoController()
        let boundaryConnection = connectionCorrelation(generation: 28, connectionId: 270)
        try activateNotificationSubscriptions(
            count: maximum,
            controller: boundaryController,
            connection: boundaryConnection,
            sessionId: 271
        )

        guard case .unsubscribeQueued = boundaryController.prepareSubscriptionsForRediscovery(
            connection: boundaryConnection
        ) else {
            return XCTFail("expected the exact rediscovery capacity to queue native retirements")
        }
        XCTAssertEqual(boundaryController.pendingCount, maximum)
        XCTAssertNil(boundaryController.takeSafetyFault())

        let overflowController = AppleGattIoController()
        let overflowConnection = connectionCorrelation(generation: 29, connectionId: 280)
        try activateNotificationSubscriptions(
            count: maximum + 1,
            controller: overflowController,
            connection: overflowConnection,
            sessionId: 281
        )

        guard case .unsafe(let error) = overflowController.prepareSubscriptionsForRediscovery(
            connection: overflowConnection
        ) else {
            return XCTFail("expected one-over rediscovery capacity to fail closed")
        }
        XCTAssertEqual(error.category, .internal)
        XCTAssertEqual(overflowController.pendingCount, 0)
        XCTAssertEqual(overflowController.takeSafetyFault()?.category, .internal)
        XCTAssertNil(overflowController.takeSafetyFault())
    }

    func testSuccessfulDisablePreservesOldValuesAndAllowsReenableButBlocksRead() throws {
        let controller = AppleGattIoController()
        let connection = connectionCorrelation(generation: 25, connectionId: 240)
        let characteristic = FakeIoCharacteristic(
            properties: properties(readable: true, notifiable: true)
        )
        let ioTarget = target(
            connection: connection,
            sessionId: 241,
            characteristicId: 242,
            characteristic: characteristic
        )
        let enable = notificationCommand(
            commandId: 240,
            connectionId: 240,
            sessionId: 241,
            subscriptionId: 243,
            characteristicId: 242,
            action: .enable(mode: .platformDefault)
        )
        try controller.enqueueSetNotifications(command: enable, connection: connection, target: ioTarget)
        _ = controller.nextNotificationAction(connection: connection)
        _ = controller.didUpdateNotificationState(
            connection: connection,
            peripheral: connection.peripheral,
            characteristic: characteristic,
            error: nil
        )
        let disable = notificationCommand(
            commandId: 241,
            connectionId: 240,
            sessionId: 241,
            subscriptionId: 243,
            characteristicId: 242,
            action: .disable
        )
        try controller.enqueueSetNotifications(command: disable, connection: connection, target: ioTarget)
        _ = controller.nextNotificationAction(connection: connection)
        assertNotificationDisabled(
            controller.didUpdateNotificationState(
                connection: connection,
                peripheral: connection.peripheral,
                characteristic: characteristic,
                error: nil
            ),
            command: disable
        )
        assertNotificationValue(
            controller.didUpdateValue(
                connection: connection,
                peripheral: connection.peripheral,
                characteristic: characteristic,
                value: [9],
                error: nil
            ),
            command: enable,
            value: [9]
        )
        assertInvalidState {
            try controller.enqueueRead(
                command: readCommand(
                    commandId: 242,
                    connectionId: 240,
                    sessionId: 241,
                    characteristicId: 242
                ),
                connection: connection,
                target: ioTarget
            )
        }
        let reenable = notificationCommand(
            commandId: 243,
            connectionId: 240,
            sessionId: 241,
            subscriptionId: 244,
            characteristicId: 242,
            action: .enable(mode: .platformDefault)
        )
        try controller.enqueueSetNotifications(
            command: reenable,
            connection: connection,
            target: ioTarget
        )
        _ = controller.nextNotificationAction(connection: connection)
        assertNotificationValue(
            controller.didUpdateValue(
                connection: connection,
                peripheral: connection.peripheral,
                characteristic: characteristic,
                value: [10],
                error: nil
            ),
            command: enable,
            value: [10]
        )
        assertNotificationEnabled(
            controller.didUpdateNotificationState(
                connection: connection,
                peripheral: connection.peripheral,
                characteristic: characteristic,
                isNotifying: true,
                error: nil
            ),
            command: reenable,
            actualMode: .notification
        )
        assertNotificationValue(
            controller.didUpdateValue(
                connection: connection,
                peripheral: connection.peripheral,
                characteristic: characteristic,
                value: [11],
                error: nil
            ),
            command: reenable,
            value: [11]
        )

        controller.drain()
        XCTAssertNoThrow(
            try controller.enqueueRead(
                command: readCommand(
                    commandId: 244,
                    connectionId: 240,
                    sessionId: 241,
                    characteristicId: 242
                ),
                connection: connection,
                target: ioTarget
            )
        )
    }

    func testValueRouteBarrierOverflowRetains129thAndLatchesOneSafetyFault() throws {
        let controller = AppleGattIoController()
        var routes: [(
            connection: AppleConnectionCorrelation,
            characteristic: FakeIoCharacteristic,
            enable: SetNotificationsCommand
        )] = []

        for index in 0 ..< 130 {
            let value = UInt64(index)
            let connection = connectionCorrelation(
                generation: 300 + value,
                connectionId: 500 + value
            )
            let characteristic = FakeIoCharacteristic(properties: properties(notifiable: true))
            let enable = notificationCommand(
                commandId: 1_100 + value,
                connectionId: 500 + value,
                sessionId: 700 + value,
                subscriptionId: 1_400 + value,
                characteristicId: 900 + value,
                action: .enable(mode: .platformDefault)
            )
            try controller.enqueueSetNotifications(
                command: enable,
                connection: connection,
                target: target(
                    connection: connection,
                    sessionId: 700 + value,
                    characteristicId: 900 + value,
                    characteristic: characteristic
                )
            )
            _ = controller.nextNotificationAction(connection: connection)
            _ = controller.didUpdateNotificationState(
                connection: connection,
                peripheral: connection.peripheral,
                characteristic: characteristic,
                isNotifying: true,
                error: nil
            )
            routes.append((connection, characteristic, enable))
        }

        let survivorConnection = connectionCorrelation(generation: 2_000, connectionId: 2_001)
        let survivorCharacteristic = FakeIoCharacteristic(properties: properties(notifiable: true))
        let survivorEnable = notificationCommand(
            commandId: 2_002,
            connectionId: 2_001,
            sessionId: 2_003,
            subscriptionId: 2_004,
            characteristicId: 2_005,
            action: .enable(mode: .platformDefault)
        )
        try controller.enqueueSetNotifications(
            command: survivorEnable,
            connection: survivorConnection,
            target: target(
                connection: survivorConnection,
                sessionId: 2_003,
                characteristicId: 2_005,
                characteristic: survivorCharacteristic
            )
        )
        _ = controller.nextNotificationAction(connection: survivorConnection)
        _ = controller.didUpdateNotificationState(
            connection: survivorConnection,
            peripheral: survivorConnection.peripheral,
            characteristic: survivorCharacteristic,
            isNotifying: true,
            error: nil
        )

        for route in routes.prefix(129) {
            _ = controller.retireAll(
                connection: route.connection,
                error: eventError(category: .connection, message: "disconnected")
            )
        }
        let fault = try XCTUnwrap(controller.takeSafetyFault())
        XCTAssertEqual(fault.category, .internal)
        XCTAssertFalse(fault.message.isEmpty)
        XCTAssertNil(controller.takeSafetyFault())

        _ = controller.retireAll(
            connection: routes[129].connection,
            error: eventError(category: .connection, message: "disconnected")
        )
        XCTAssertNil(controller.takeSafetyFault())

        var retainedBarrierCount = 0
        for route in routes {
            guard case .notification = controller.didUpdateValue(
                connection: route.connection,
                peripheral: route.connection.peripheral,
                characteristic: route.characteristic,
                value: [1],
                error: nil
            ) else {
                continue
            }
            retainedBarrierCount += 1
        }
        XCTAssertEqual(retainedBarrierCount, 129)
        assertNotificationValue(
            controller.didUpdateValue(
                connection: survivorConnection,
                peripheral: survivorConnection.peripheral,
                characteristic: survivorCharacteristic,
                value: [2],
                error: nil
            ),
            command: survivorEnable,
            value: [2]
        )

        let freshConnection = connectionCorrelation(generation: 3_000, connectionId: 3_001)
        let freshCharacteristic = FakeIoCharacteristic(properties: properties(readable: true))
        let freshCommand = readCommand(
            commandId: 3_002,
            connectionId: 3_001,
            sessionId: 3_003,
            characteristicId: 3_004
        )
        let freshTarget = target(
            connection: freshConnection,
            sessionId: 3_003,
            characteristicId: 3_004,
            characteristic: freshCharacteristic
        )
        assertInvalidState {
            try controller.enqueueRead(
                command: freshCommand,
                connection: freshConnection,
                target: freshTarget
            )
        }

        controller.drain()
        XCTAssertNil(controller.takeSafetyFault())
        XCTAssertNoThrow(
            try controller.enqueueRead(
                command: freshCommand,
                connection: freshConnection,
                target: freshTarget
            )
        )
    }

    func testRetirementExposesOrderedUniqueActiveNotificationCleanupCharacteristics() throws {
        let controller = AppleGattIoController()
        let connection = connectionCorrelation(generation: 80, connectionId: 800)
        let firstCharacteristic = FakeIoCharacteristic(properties: properties(notifiable: true))
        let secondCharacteristic = FakeIoCharacteristic(properties: properties(notifiable: true))
        let queuedCharacteristic = FakeIoCharacteristic(properties: properties(notifiable: true))
        let sessionId: UInt64 = 801

        func enableAndComplete(
            commandId: UInt64,
            subscriptionId: UInt64,
            characteristicId: UInt64,
            characteristic: FakeIoCharacteristic
        ) throws -> SetNotificationsCommand {
            let command = notificationCommand(
                commandId: commandId,
                connectionId: 800,
                sessionId: sessionId,
                subscriptionId: subscriptionId,
                characteristicId: characteristicId,
                action: .enable(mode: .platformDefault)
            )
            try controller.enqueueSetNotifications(
                command: command,
                connection: connection,
                target: target(
                    connection: connection,
                    sessionId: sessionId,
                    characteristicId: characteristicId,
                    characteristic: characteristic
                )
            )
            _ = controller.nextNotificationAction(connection: connection)
            assertNotificationEnabled(
                controller.didUpdateNotificationState(
                    connection: connection,
                    peripheral: connection.peripheral,
                    characteristic: characteristic,
                    isNotifying: true,
                    error: nil
                ),
                command: command,
                actualMode: .notification
            )
            return command
        }

        _ = try enableAndComplete(
            commandId: 810,
            subscriptionId: 820,
            characteristicId: 830,
            characteristic: firstCharacteristic
        )
        _ = try enableAndComplete(
            commandId: 811,
            subscriptionId: 821,
            characteristicId: 831,
            characteristic: firstCharacteristic
        )
        _ = try enableAndComplete(
            commandId: 812,
            subscriptionId: 822,
            characteristicId: 832,
            characteristic: secondCharacteristic
        )

        let disable = notificationCommand(
            commandId: 813,
            connectionId: 800,
            sessionId: sessionId,
            subscriptionId: 820,
            characteristicId: 830,
            action: .disable
        )
        try controller.enqueueSetNotifications(
            command: disable,
            connection: connection,
            target: target(
                connection: connection,
                sessionId: sessionId,
                characteristicId: 830,
                characteristic: firstCharacteristic
            )
        )
        let queuedEnable = notificationCommand(
            commandId: 814,
            connectionId: 800,
            sessionId: sessionId,
            subscriptionId: 823,
            characteristicId: 833,
            action: .enable(mode: .platformDefault)
        )
        try controller.enqueueSetNotifications(
            command: queuedEnable,
            connection: connection,
            target: target(
                connection: connection,
                sessionId: sessionId,
                characteristicId: 833,
                characteristic: queuedCharacteristic
            )
        )

        let retirement = controller.retireAll(
            connection: connection,
            error: eventError(category: .connection, message: "disconnected")
        )
        XCTAssertEqual(retirement.completions.count, 2)
        XCTAssertEqual(retirement.notificationCompletions.count, 2)
        assertNotificationFailure(
            retirement.notificationCompletions[0],
            command: disable,
            category: .connection,
            message: "disconnected"
        )
        assertNotificationFailure(
            retirement.notificationCompletions[1],
            command: queuedEnable,
            category: .connection,
            message: "disconnected"
        )

        let cleanupCharacteristics = retirement.activeNotificationCharacteristicsToUnsubscribe
        XCTAssertEqual(cleanupCharacteristics.count, 2)
        XCTAssertEqual(
            cleanupCharacteristics.map(ObjectIdentifier.init),
            [ObjectIdentifier(firstCharacteristic), ObjectIdentifier(secondCharacteristic)]
        )
        XCTAssertFalse(
            cleanupCharacteristics.contains {
                ObjectIdentifier($0) == ObjectIdentifier(queuedCharacteristic)
            }
        )
        guard case .ready = controller.prepareSubscriptionsForRediscovery(
            connection: connection
        ) else {
            return XCTFail("expected rediscovery to be immediately ready after retirement")
        }
    }

    func testRetirementFailsNotificationCommandAndQuarantinesLateCallbacks() throws {
        let controller = AppleGattIoController()
        let peripheral = FakeIoPeripheral(identifier: UUID())
        let connection = connectionCorrelation(
            generation: 26,
            connectionId: 250,
            peripheral: peripheral
        )
        let characteristic = FakeIoCharacteristic(
            properties: properties(readable: true, notifiable: true)
        )
        let ioTarget = target(
            connection: connection,
            sessionId: 251,
            characteristicId: 252,
            characteristic: characteristic
        )
        let enable = notificationCommand(
            commandId: 250,
            connectionId: 250,
            sessionId: 251,
            subscriptionId: 253,
            characteristicId: 252,
            action: .enable(mode: .platformDefault)
        )
        try controller.enqueueSetNotifications(command: enable, connection: connection, target: ioTarget)
        _ = controller.nextNotificationAction(connection: connection)

        let retirement = controller.retireAll(
            connection: connection,
            error: eventError(category: .connection, message: "disconnected")
        )
        XCTAssertTrue(retirement.ioCompletions.isEmpty)
        XCTAssertEqual(controller.pendingCount, 0)
        XCTAssertEqual(controller.diagnosticSubscriptionCount, 0)
        XCTAssertEqual(controller.diagnosticCallbackRouteCount, 2, "state and value barriers survive retirement")
        XCTAssertEqual(controller.diagnosticAttributeIdentities.count, 1, "two routes own one instance")
        XCTAssertEqual(retirement.notificationCompletions.count, 1)
        assertNotificationFailure(
            retirement.notificationCompletions.first,
            command: enable,
            category: .connection,
            message: "disconnected"
        )
        XCTAssertNil(
            controller.didUpdateNotificationState(
                connection: connection,
                peripheral: peripheral,
                characteristic: characteristic,
                error: nil
            )
        )
        assertNotificationValue(
            controller.didUpdateValue(
                connection: connection,
                peripheral: peripheral,
                characteristic: characteristic,
                value: [1],
                error: nil
            ),
            command: enable,
            value: [1]
        )

        let replacement = connectionCorrelation(
            generation: 27,
            connectionId: 250,
            peripheral: peripheral
        )
        assertInvalidState {
            try controller.enqueueRead(
                command: readCommand(
                    commandId: 251,
                    connectionId: 250,
                    sessionId: 254,
                    characteristicId: 255
                ),
                connection: replacement,
                target: target(
                    connection: replacement,
                    sessionId: 254,
                    characteristicId: 255,
                    characteristic: characteristic
                )
            )
        }
    }

    func testRetireFailsAcceptedDisableWithoutSyntheticDisabledCompletion() throws {
        let controller = AppleGattIoController()
        let connection = connectionCorrelation(generation: 28, connectionId: 260)
        let characteristic = FakeIoCharacteristic(properties: properties(notifiable: true))
        let ioTarget = target(
            connection: connection,
            sessionId: 261,
            characteristicId: 262,
            characteristic: characteristic
        )
        let enable = notificationCommand(
            commandId: 260,
            connectionId: 260,
            sessionId: 261,
            subscriptionId: 263,
            characteristicId: 262,
            action: .enable(mode: .platformDefault)
        )
        try controller.enqueueSetNotifications(command: enable, connection: connection, target: ioTarget)
        _ = controller.nextNotificationAction(connection: connection)
        _ = controller.didUpdateNotificationState(
            connection: connection,
            peripheral: connection.peripheral,
            characteristic: characteristic,
            error: nil
        )
        let disable = notificationCommand(
            commandId: 261,
            connectionId: 260,
            sessionId: 261,
            subscriptionId: 263,
            characteristicId: 262,
            action: .disable
        )
        try controller.enqueueSetNotifications(command: disable, connection: connection, target: ioTarget)
        _ = controller.nextNotificationAction(connection: connection)

        let retirement = controller.retireAll(
            connection: connection,
            error: eventError(category: .connection, message: "lost")
        )
        XCTAssertEqual(retirement.notificationCompletions.count, 1)
        assertNotificationFailure(
            retirement.notificationCompletions.first,
            command: disable,
            category: .connection,
            message: "lost"
        )
    }

    func testRetireFailsAllAcceptedCommandsAndQuarantinesLateCallbackRoute() throws {
        let controller = AppleGattIoController()
        let peripheral = FakeIoPeripheral(identifier: UUID())
        let oldConnection = connectionCorrelation(
            generation: 12,
            connectionId: 120,
            peripheral: peripheral
        )
        let characteristic = FakeIoCharacteristic(
            properties: properties(readable: true, writableWithoutResponse: true)
        )
        let read = readCommand(
            commandId: 90,
            connectionId: 120,
            sessionId: 121,
            characteristicId: 122
        )
        let write = writeCommand(
            commandId: 91,
            connectionId: 120,
            sessionId: 121,
            characteristicId: 122,
            value: [1],
            writeType: .withoutResponse
        )
        let oldTarget = target(
            connection: oldConnection,
            sessionId: 121,
            characteristicId: 122,
            characteristic: characteristic
        )
        try controller.enqueueRead(command: read, connection: oldConnection, target: oldTarget)
        try controller.enqueueWrite(command: write, connection: oldConnection, target: oldTarget)
        _ = try nextAction(controller, connection: oldConnection)

        let retired = controller.retire(
            connection: oldConnection,
            error: eventError(category: .connection, message: "disconnected")
        )
        XCTAssertEqual(retired.count, 2)
        assertFailure(
            retired[0],
            commandId: read.commandId,
            category: .connection,
            message: "disconnected"
        )
        assertFailure(
            retired[1],
            commandId: write.commandId,
            category: .connection,
            message: "disconnected"
        )
        XCTAssertEqual(controller.pendingCount, 0)

        let newConnection = connectionCorrelation(
            generation: 13,
            connectionId: 120,
            peripheral: peripheral
        )
        let newCommand = readCommand(
            commandId: 92,
            connectionId: 120,
            sessionId: 123,
            characteristicId: 124
        )
        let newTarget = target(
            connection: newConnection,
            sessionId: 123,
            characteristicId: 124,
            characteristic: characteristic
        )
        assertInvalidState {
            try controller.enqueueRead(
                command: newCommand,
                connection: newConnection,
                target: newTarget
            )
        }
        XCTAssertTrue(
            controller.consumeRetiredReadCallback(
                peripheral: peripheral,
                characteristic: characteristic
            )
        )
        XCTAssertNoThrow(
            try controller.enqueueRead(
                command: newCommand,
                connection: newConnection,
                target: newTarget
            )
        )
    }

    func testRetiredReadBarrierOwnsExactCharacteristicUntilLateCallbackIsConsumed() throws {
        let controller = AppleGattIoController()
        let peripheral = FakeIoPeripheral(identifier: UUID())
        let oldConnection = connectionCorrelation(
            generation: 12,
            connectionId: 120,
            peripheral: peripheral
        )
        weak var retiredCharacteristic: FakeIoCharacteristic?
        do {
            let characteristic = FakeIoCharacteristic(properties: properties(readable: true))
            retiredCharacteristic = characteristic
            try controller.enqueueRead(
                command: readCommand(
                    commandId: 90,
                    connectionId: 120,
                    sessionId: 121,
                    characteristicId: 122
                ),
                connection: oldConnection,
                target: target(
                    connection: oldConnection,
                    sessionId: 121,
                    characteristicId: 122,
                    characteristic: characteristic
                )
            )
            _ = try nextAction(controller, connection: oldConnection)
            _ = controller.retire(
                connection: oldConnection,
                error: eventError(category: .timeout, message: "watchdog expired")
            )
        }
        XCTAssertEqual(controller.pendingCount, 0)
        XCTAssertNotNil(retiredCharacteristic, "the barrier must own its ObjectIdentifier's object")
        XCTAssertEqual(controller.diagnosticCallbackRouteCount, 1)
        XCTAssertEqual(controller.diagnosticAttributeIdentities.count, 1)

        let recovery = connectionCorrelation(
            generation: 13,
            connectionId: 130,
            peripheral: peripheral
        )
        let updates = FakeIoCharacteristic(properties: properties(readable: true))
        let read = readCommand(
            commandId: 91,
            connectionId: 130,
            sessionId: 131,
            characteristicId: 132
        )
        try controller.enqueueRead(
            command: read,
            connection: recovery,
            target: target(
                connection: recovery,
                sessionId: 131,
                characteristicId: 132,
                characteristic: updates
            )
        )
        assertReadAction(
            try XCTUnwrap(nextAction(controller, connection: recovery)),
            command: read,
            characteristic: updates
        )
        XCTAssertFalse(
            controller.consumeRetiredReadCallback(peripheral: peripheral, characteristic: updates)
        )
        do {
            let retired = try XCTUnwrap(retiredCharacteristic)
            XCTAssertFalse(
                controller.consumeRetiredWriteCallback(peripheral: peripheral, characteristic: retired)
            )
            assertInvalidState {
                try controller.enqueueRead(
                    command: read,
                    connection: recovery,
                    target: target(
                        connection: recovery,
                        sessionId: 131,
                        characteristicId: 132,
                        characteristic: retired
                    )
                )
            }
            XCTAssertNil(
                controller.didReadValue(
                    connection: recovery,
                    peripheral: peripheral,
                    characteristic: retired,
                    value: [0],
                    error: nil
                )
            )
        }
        XCTAssertNil(retiredCharacteristic, "consuming the barrier must release the retired object")
        XCTAssertEqual(controller.pendingCount, 1)
        assertReadCompletion(
            controller.didReadValue(
                connection: recovery,
                peripheral: peripheral,
                characteristic: updates,
                value: [1],
                error: nil
            ),
            command: read,
            value: [1]
        )
    }

    func testRetiredWriteBarrierOwnsCharacteristicUntilDrain() throws {
        let controller = AppleGattIoController()
        let connection = connectionCorrelation(generation: 14, connectionId: 140)
        weak var retiredCharacteristic: FakeIoCharacteristic?
        do {
            let characteristic = FakeIoCharacteristic(
                properties: properties(writableWithResponse: true)
            )
            retiredCharacteristic = characteristic
            try controller.enqueueWrite(
                command: writeCommand(
                    commandId: 100,
                    connectionId: 140,
                    sessionId: 141,
                    characteristicId: 142,
                    value: [1],
                    writeType: .withResponse
                ),
                connection: connection,
                target: target(
                    connection: connection,
                    sessionId: 141,
                    characteristicId: 142,
                    characteristic: characteristic
                )
            )
            _ = try nextAction(controller, connection: connection)
            _ = controller.retire(
                connection: connection,
                error: eventError(category: .timeout, message: "watchdog expired")
            )
        }
        XCTAssertNotNil(retiredCharacteristic)
        controller.drain()
        XCTAssertNil(retiredCharacteristic, "drain must release the barrier's exact characteristic")
    }

    func testStaleCallbackBarrierOverflowRetains129thAndLatchesOneSafetyFault() throws {
        let controller = AppleGattIoController()
        var routes: [(
            connection: AppleConnectionCorrelation,
            characteristic: WeakIoCharacteristic
        )] = []

        for index in 0 ..< 130 {
            let value = UInt64(index)
            let connection = connectionCorrelation(
                generation: 200 + value,
                connectionId: 300 + value
            )
            let characteristic = FakeIoCharacteristic(properties: properties(readable: true))
            try controller.enqueueRead(
                command: readCommand(
                    commandId: 400 + value,
                    connectionId: 300 + value,
                    sessionId: 500 + value,
                    characteristicId: 600 + value
                ),
                connection: connection,
                target: target(
                    connection: connection,
                    sessionId: 500 + value,
                    characteristicId: 600 + value,
                    characteristic: characteristic
                )
            )
            _ = try nextAction(controller, connection: connection)
            routes.append((connection, WeakIoCharacteristic(characteristic)))
        }

        let survivorConnection = connectionCorrelation(generation: 1_000, connectionId: 1_001)
        let survivorCharacteristic = FakeIoCharacteristic(properties: properties(readable: true))
        let survivorCommand = readCommand(
            commandId: 1_002,
            connectionId: 1_001,
            sessionId: 1_003,
            characteristicId: 1_004
        )
        try controller.enqueueRead(
            command: survivorCommand,
            connection: survivorConnection,
            target: target(
                connection: survivorConnection,
                sessionId: 1_003,
                characteristicId: 1_004,
                characteristic: survivorCharacteristic
            )
        )
        _ = try nextAction(controller, connection: survivorConnection)

        for route in routes.prefix(129) {
            _ = controller.retire(
                connection: route.connection,
                error: eventError(category: .connection, message: "disconnected")
            )
        }
        let fault = try XCTUnwrap(controller.takeSafetyFault())
        XCTAssertEqual(fault.category, .internal)
        XCTAssertFalse(fault.message.isEmpty)
        XCTAssertNil(controller.takeSafetyFault())

        _ = controller.retire(
            connection: routes[129].connection,
            error: eventError(category: .connection, message: "disconnected")
        )
        XCTAssertNil(controller.takeSafetyFault())

        XCTAssertEqual(routes.compactMap { $0.characteristic.value }.count, 129)
        XCTAssertNil(routes[129].characteristic.value, "the fail-closed 130th route must not be retained")
        var retainedBarrierCount = 0
        for route in routes {
            guard let characteristic = route.characteristic.value else {
                continue
            }
            if controller.consumeRetiredReadCallback(
                peripheral: route.connection.peripheral,
                characteristic: characteristic
            ) {
                retainedBarrierCount += 1
            }
        }
        XCTAssertEqual(retainedBarrierCount, 129)
        XCTAssertTrue(routes.allSatisfy { $0.characteristic.value == nil })
        assertReadCompletion(
            controller.didReadValue(
                connection: survivorConnection,
                peripheral: survivorConnection.peripheral,
                characteristic: survivorCharacteristic,
                value: [3],
                error: nil
            ),
            command: survivorCommand,
            value: [3]
        )

        let freshConnection = connectionCorrelation(generation: 2_000, connectionId: 2_001)
        let freshCharacteristic = FakeIoCharacteristic(properties: properties(readable: true))
        let freshCommand = readCommand(
            commandId: 2_002,
            connectionId: 2_001,
            sessionId: 2_003,
            characteristicId: 2_004
        )
        let freshTarget = target(
            connection: freshConnection,
            sessionId: 2_003,
            characteristicId: 2_004,
            characteristic: freshCharacteristic
        )
        assertInvalidState {
            try controller.enqueueRead(
                command: freshCommand,
                connection: freshConnection,
                target: freshTarget
            )
        }

        controller.drain()
        XCTAssertNil(controller.takeSafetyFault())
        XCTAssertNoThrow(
            try controller.enqueueRead(
                command: freshCommand,
                connection: freshConnection,
                target: freshTarget
            )
        )
    }

    func testDrainSilentlyClearsQueuesAndStaleBarriers() throws {
        let controller = AppleGattIoController()
        let connection = connectionCorrelation(generation: 14, connectionId: 130)
        let characteristic = FakeIoCharacteristic(
            properties: properties(writableWithResponse: true)
        )
        let command = writeCommand(
            commandId: 100,
            connectionId: 130,
            sessionId: 131,
            characteristicId: 132,
            value: [1],
            writeType: .withResponse
        )
        let ioTarget = target(
            connection: connection,
            sessionId: 131,
            characteristicId: 132,
            characteristic: characteristic
        )
        try controller.enqueueWrite(command: command, connection: connection, target: ioTarget)
        _ = try nextAction(controller, connection: connection)
        _ = controller.retire(
            connection: connection,
            error: eventError(category: .cancelled, message: "closed")
        )
        assertInvalidState {
            try controller.enqueueWrite(
                command: command,
                connection: connection,
                target: ioTarget
            )
        }

        XCTAssertEqual(controller.diagnosticCallbackRouteCount, 1)
        XCTAssertEqual(controller.diagnosticAttributeIdentities.count, 1)
        controller.drain()

        XCTAssertEqual(controller.diagnosticCallbackRouteCount, 0)
        XCTAssertEqual(controller.diagnosticSubscriptionCount, 0)
        XCTAssertTrue(controller.diagnosticAttributeIdentities.isEmpty)
        XCTAssertEqual(controller.pendingCount, 0)
        XCTAssertFalse(controller.hasPending(connection: connection))
        XCTAssertNil(
            controller.didWriteValue(
                connection: connection,
                peripheral: connection.peripheral,
                characteristic: characteristic,
                error: nil
            )
        )
        XCTAssertNoThrow(
            try controller.enqueueWrite(
                command: command,
                connection: connection,
                target: ioTarget
            )
        )
    }

    func testMixedGattInterruptionPreservesTerminalOrderAndIsolatesReplacementRoute() throws {
        enum Head: Int, CaseIterable {
            case responseWrite, capacityBlockedWrite, enable, disable
        }
        enum Interruption: CaseIterable {
            case disconnect, watchdog, close
        }

        for interruption in Interruption.allCases {
            for head in Head.allCases {
                // Dedicated capacity-watchdog tests cover its pre-native ownership boundary.
                if interruption == .watchdog && head == .capacityBlockedWrite {
                    continue
                }
                let context = "\(interruption), head=\(head)"
                let controller = AppleGattIoController()
                let peripheral = FakeIoPeripheral(identifier: UUID())
                let connection = connectionCorrelation(
                    generation: 1, connectionId: 10, peripheral: peripheral
                )
                let writes = FakeIoCharacteristic(
                    properties: properties(writableWithResponse: true, writableWithoutResponse: true)
                )
                let enabling = FakeIoCharacteristic(properties: properties(notifiable: true))
                let disabling = FakeIoCharacteristic(properties: properties(notifiable: true))
                let writeTarget = target(
                    connection: connection, sessionId: 20, characteristicId: 30,
                    characteristic: writes
                )
                let enableTarget = target(
                    connection: connection, sessionId: 20, characteristicId: 31,
                    characteristic: enabling
                )
                let disableTarget = target(
                    connection: connection, sessionId: 20, characteristicId: 32,
                    characteristic: disabling
                )
                let responseWrite = writeCommand(
                    commandId: 1, connectionId: 10, sessionId: 20, characteristicId: 30,
                    value: [1], writeType: .withResponse
                )
                let noResponseWrite = writeCommand(
                    commandId: 2, connectionId: 10, sessionId: 20, characteristicId: 30,
                    value: [2], writeType: .withoutResponse
                )
                let enable = notificationCommand(
                    commandId: 3, connectionId: 10, sessionId: 20, subscriptionId: 40,
                    characteristicId: 31, action: .enable(mode: .platformDefault)
                )
                let priorEnable = notificationCommand(
                    commandId: 5, connectionId: 10, sessionId: 20, subscriptionId: 41,
                    characteristicId: 32, action: .enable(mode: .platformDefault)
                )
                let disable = notificationCommand(
                    commandId: 4, connectionId: 10, sessionId: 20, subscriptionId: 41,
                    characteristicId: 32, action: .disable
                )
                try controller.enqueueSetNotifications(
                    command: priorEnable, connection: connection, target: disableTarget
                )
                _ = try XCTUnwrap(controller.nextNotificationAction(connection: connection))
                assertNotificationEnabled(
                    controller.didUpdateNotificationState(
                        connection: connection, peripheral: connection.peripheral,
                        characteristic: disabling, isNotifying: true, error: nil
                    ),
                    command: priorEnable, actualMode: .notification
                )

                let order = Array(Head.allCases[head.rawValue...])
                    + Array(Head.allCases[..<head.rawValue])
                for operation in order {
                    switch operation {
                    case .responseWrite:
                        try controller.enqueueWrite(
                            command: responseWrite, connection: connection, target: writeTarget
                        )
                    case .capacityBlockedWrite:
                        try controller.enqueueWrite(
                            command: noResponseWrite, connection: connection, target: writeTarget
                        )
                    case .enable:
                        try controller.enqueueSetNotifications(
                            command: enable, connection: connection, target: enableTarget
                        )
                    case .disable:
                        try controller.enqueueSetNotifications(
                            command: disable, connection: connection, target: disableTarget
                        )
                    }
                }
                switch head {
                case .responseWrite:
                    assertWriteWithResponseAction(
                        try XCTUnwrap(nextAction(controller, connection: connection)),
                        command: responseWrite, characteristic: writes
                    )
                case .capacityBlockedWrite:
                    assertWriteCapacityWaitAction(
                        try XCTUnwrap(nextAction(
                            controller, connection: connection, canSendWriteWithoutResponse: false
                        )),
                        command: noResponseWrite,
                        connection: connection
                    )
                    XCTAssertTrue(controller.didBecomeReadyToSendWriteWithoutResponse(
                        connection: connection, peripheral: connection.peripheral
                    ), context)
                    XCTAssertNil(controller.nextNotificationAction(connection: connection), context)
                case .enable, .disable:
                    assertSetNotifyValueAction(
                        try XCTUnwrap(controller.nextNotificationAction(connection: connection)),
                        command: head == .enable ? enable : disable,
                        characteristic: head == .enable ? enabling : disabling,
                        enabled: head == .enable
                    )
                }

                if interruption == .close {
                    // dispose detaches ingress before silently draining controller ownership.
                    controller.drain()
                    XCTAssertEqual(controller.diagnosticCallbackRouteCount, 0, context)
                    XCTAssertTrue(controller.diagnosticAttributeIdentities.isEmpty, context)
                } else {
                    let disconnected = eventError(category: .connection, message: "disconnected")
                    let timeout = eventError(category: .timeout, message: "active watchdog expired")
                    let aborted = eventError(category: .operationAborted, message: "queued operation retired")
                    let retirement = controller.retireAll(
                        connection: connection,
                        error: interruption == .watchdog ? aborted : disconnected,
                        activeOperationError: interruption == .watchdog ? timeout : nil,
                        queuedOperationError: interruption == .watchdog ? aborted : nil
                    )
                    let events = AppleBackendEventProjection.gattRetirementEvents(retirement)
                    XCTAssertEqual(retirement.completions.count, order.count, context)
                    XCTAssertEqual(events.count, order.count, context)
                    for (index, event) in events.enumerated() {
                        guard index < order.count,
                              case .commandCompleted(let commandId, let error) = event
                        else {
                            return XCTFail("interruption fabricated a domain event: \(context)")
                        }
                        XCTAssertEqual(commandId, CommandId(value: UInt64(order[index].rawValue + 1)), context)
                        let expected = interruption == .disconnect ? disconnected : (index == 0 ? timeout : aborted)
                        XCTAssertEqual(error, expected, context)
                    }
                    let cleanup = retirement.activeNotificationCharacteristicsToUnsubscribe
                        .map { ObjectIdentifier($0) }
                    XCTAssertEqual(
                        cleanup,
                        head == .enable
                            ? [ObjectIdentifier(disabling), ObjectIdentifier(enabling)]
                            : [ObjectIdentifier(disabling)],
                        context
                    )
                    XCTAssertEqual(controller.diagnosticCallbackRouteCount,
                                   head == .capacityBlockedWrite ? 2 : 3, context)
                }
                XCTAssertEqual(controller.pendingCount, 0, context)
                XCTAssertEqual(controller.diagnosticSubscriptionCount, 0, context)
                XCTAssertTrue(controller.retireAll(
                    connection: connection,
                    error: eventError(category: .connection, message: "duplicate disconnect")
                ).completions.isEmpty, context)

                let replacement = connectionCorrelation(
                    generation: 2, connectionId: 10, peripheral: peripheral
                )
                let replacementCharacteristic = FakeIoCharacteristic(
                    uuid: writes.appleGattUuid,
                    properties: properties(writableWithResponse: true)
                )
                let recoveryWrite = writeCommand(
                    commandId: 6, connectionId: 10, sessionId: 21, characteristicId: 33,
                    value: [3], writeType: .withResponse
                )
                try controller.enqueueWrite(
                    command: recoveryWrite, connection: replacement,
                    target: target(
                        connection: replacement, sessionId: 21, characteristicId: 33,
                        characteristic: replacementCharacteristic
                    )
                )
                assertWriteWithResponseAction(
                    try XCTUnwrap(nextAction(controller, connection: replacement)),
                    command: recoveryWrite, characteristic: replacementCharacteristic
                )
                XCTAssertNil(controller.didWriteValue(
                    connection: replacement, peripheral: connection.peripheral,
                    characteristic: writes, error: nil
                ), context)
                XCTAssertNil(controller.didSubmitWriteWithoutResponse(
                    connection: connection, peripheral: connection.peripheral,
                    characteristic: writes
                ), context)
                XCTAssertFalse(controller.didBecomeReadyToSendWriteWithoutResponse(
                    connection: connection, peripheral: connection.peripheral
                ), context)
                for characteristic in [enabling, disabling] {
                    XCTAssertNil(controller.didUpdateNotificationState(
                        connection: replacement, peripheral: connection.peripheral,
                        characteristic: characteristic, isNotifying: true, error: nil
                    ), context)
                }
                XCTAssertEqual(controller.pendingCount, 1, context)
                assertWriteCompletion(
                    controller.didWriteValue(
                        connection: replacement, peripheral: replacement.peripheral,
                        characteristic: replacementCharacteristic, error: nil
                    ),
                    command: recoveryWrite
                )
                XCTAssertEqual(controller.pendingCount, 0, context)
                controller.drain()
                XCTAssertEqual(controller.diagnosticCallbackRouteCount, 0, context)
                XCTAssertEqual(controller.diagnosticSubscriptionCount, 0, context)
                XCTAssertTrue(controller.diagnosticAttributeIdentities.isEmpty, context)
                XCTAssertNil(controller.takeSafetyFault(), context)
            }
        }
    }

    func testCapacityWaitExpiryRetiresTargetAndSiblingsWithoutNativeCallbackBarrier() throws {
        let controller = AppleGattIoController()
        let connections = AppleConnectionController()
        let peripheral = FakeIoPeripheral(identifier: UUID())
        let connect = ConnectCommand(
            commandId: CommandId(value: 90), connectionId: ConnectionId(value: 10),
            peripheralId: PeripheralId(value: 100)
        )
        let connection = try connections.begin(command: connect, peripheral: peripheral)
        XCTAssertEqual(connections.didConnect(peripheral: peripheral), .connected(connection))
        let characteristic = FakeIoCharacteristic(
            properties: properties(writableWithResponse: true, writableWithoutResponse: true)
        )
        let head = try enqueueCapacityWrite(controller, connection: connection, characteristic: characteristic, id: 1)
        let sibling = rssiCommand(commandId: 2, connectionId: 10)
        try controller.enqueueRssi(command: sibling, connection: connection)
        let registry = AppleGattWatchdogRegistry()
        let token = try armCapacityWait(controller, registry: registry, connection: connection, command: head)
        XCTAssertTrue(controller.isWaitingForWriteCapacity(connection: connection, commandId: head.commandId))
        XCTAssertFalse(controller.isWaitingForWriteCapacity(connection: connection, commandId: sibling.commandId))
        XCTAssertEqual(controller.diagnosticCallbackRouteCount, 1, "only the connection lane; no native callback")
        XCTAssertNil(controller.didSubmitWriteWithoutResponse(
            connection: connection, peripheral: peripheral, characteristic: characteristic
        ))

        XCTAssertNotNil(registry.takeIfCurrent(connectionId: connection.connectionId, token: token))
        XCTAssertTrue(connections.beginFailClosedDisconnect(connection: connection))
        let timeout = AppleEventMapper.operationTimeout("write capacity stalled")
        let aborted = AppleEventMapper.operationAborted("connection recovery")
        let retirement = controller.retireAll(
            connection: connection, error: aborted,
            activeOperationError: timeout, queuedOperationError: aborted
        )
        let events = AppleBackendEventProjection.gattRetirementEvents(retirement)
        XCTAssertEqual(events.count, 2)
        for (event, expected) in zip(events, [(head.commandId, timeout), (sibling.commandId, aborted)]) {
            guard case .commandCompleted(let commandId, let error) = event else {
                return XCTFail("capacity timeout fabricated a domain event")
            }
            XCTAssertEqual(commandId, expected.0)
            XCTAssertEqual(error, expected.1)
        }
        XCTAssertNil(connections.connectedCorrelation(for: connection.connectionId))
        XCTAssertNil(try nextAction(controller, connection: connection))
        XCTAssertFalse(controller.didBecomeReadyToSendWriteWithoutResponse(
            connection: connection, peripheral: peripheral
        ))
        XCTAssertNil(controller.didSubmitWriteWithoutResponse(
            connection: connection, peripheral: peripheral, characteristic: characteristic
        ))
        XCTAssertFalse(controller.consumeRetiredWriteCallback(peripheral: peripheral, characteristic: characteristic))
        XCTAssertEqual(controller.pendingCount, 0)
        XCTAssertEqual(controller.diagnosticCallbackRouteCount, 0)
        XCTAssertTrue(controller.diagnosticAttributeIdentities.isEmpty)
        XCTAssertTrue(controller.retireAll(connection: connection, error: aborted).completions.isEmpty)
        XCTAssertNil(registry.takeIfCurrent(connectionId: connection.connectionId, token: token))

        _ = connections.didDisconnect(peripheral: peripheral)
        let replacement = try connections.begin(command: connect, peripheral: peripheral)
        _ = connections.didConnect(peripheral: peripheral)
        XCTAssertNotEqual(replacement.generation, connection.generation)
        let recovery = try enqueueCapacityWrite(controller, connection: replacement, characteristic: characteristic, id: 3)
        _ = try XCTUnwrap(nextAction(controller, connection: replacement))
        XCTAssertNil(controller.didSubmitWriteWithoutResponse(
            connection: connection, peripheral: peripheral, characteristic: characteristic
        ))
        assertWriteCompletion(controller.didSubmitWriteWithoutResponse(
            connection: replacement, peripheral: peripheral, characteristic: characteristic
        ), command: recovery)
        XCTAssertEqual(controller.pendingCount, 0)
    }

    func testCapacityReadinessDoesNotExtendDeadlineAndCancelsItBeforeSubmission() throws {
        let controller = AppleGattIoController()
        let connection = connectionCorrelation(generation: 1, connectionId: 10)
        let characteristic = FakeIoCharacteristic(properties: properties(writableWithoutResponse: true))
        let head = try enqueueCapacityWrite(controller, connection: connection, characteristic: characteristic, id: 1)
        let registry = AppleGattWatchdogRegistry()
        let token = try armCapacityWait(controller, registry: registry, connection: connection, command: head)
        for _ in 0..<3 {
            XCTAssertTrue(controller.didBecomeReadyToSendWriteWithoutResponse(
                connection: connection, peripheral: connection.peripheral
            ))
            XCTAssertNil(try nextAction(controller, connection: connection, canSendWriteWithoutResponse: false),
                         "repeated drive must not request a fresh watchdog/deadline")
        }
        XCTAssertFalse(controller.didBecomeReadyToSendWriteWithoutResponse(
            connection: connection,
            peripheral: FakeIoPeripheral(identifier: connection.peripheral.identifier)
        ))
        let stale = connectionCorrelation(generation: 2, connectionId: 10)
        XCTAssertFalse(controller.isWaitingForWriteCapacity(connection: stale, commandId: head.commandId))
        XCTAssertFalse(controller.didBecomeReadyToSendWriteWithoutResponse(
            connection: stale, peripheral: stale.peripheral
        ))
        assertWriteWithoutResponseAction(
            try XCTUnwrap(nextAction(controller, connection: connection)),
            command: head, characteristic: characteristic
        )
        XCTAssertFalse(controller.isWaitingForWriteCapacity(connection: connection, commandId: head.commandId))
        let cancelled = try XCTUnwrap(registry.cancel(
            connectionId: connection.connectionId, kind: .writeCapacity(head.commandId)
        ))
        cancelled.cancel()
        XCTAssertNil(registry.takeIfCurrent(connectionId: connection.connectionId, token: token))
        guard case .active = controller.cancel(commandId: head.commandId) else {
            return XCTFail("submission, unlike capacity waiting, must not be queued-cancellable")
        }
        XCTAssertNil(controller.didWriteValue(
            connection: connection, peripheral: connection.peripheral, characteristic: characteristic, error: nil
        ))
        let completion = try XCTUnwrap(controller.didSubmitWriteWithoutResponse(
            connection: connection, peripheral: connection.peripheral, characteristic: characteristic
        ))
        // Written records the completed local submission, not a didWrite callback or remote ACK.
        XCTAssertEqual(AppleBackendEventProjection.gattIoEvents(completion), [
            .characteristicWritten(
                commandId: head.commandId,
                connectionId: head.connectionId,
                gattSessionId: head.gattSessionId,
                characteristicId: head.characteristicId
            ),
            .commandCompleted(commandId: head.commandId, error: nil),
        ])
        XCTAssertNil(controller.didWriteValue(
            connection: connection, peripheral: connection.peripheral, characteristic: characteristic, error: nil
        ))
        XCTAssertFalse(controller.didBecomeReadyToSendWriteWithoutResponse(
            connection: connection, peripheral: connection.peripheral
        ))
        XCTAssertNil(controller.didSubmitWriteWithoutResponse(
            connection: connection, peripheral: connection.peripheral, characteristic: characteristic
        ))
        XCTAssertEqual(registry.diagnosticCount, 0)
        XCTAssertEqual(controller.pendingCount, 0)
        XCTAssertEqual(controller.diagnosticCallbackRouteCount, 0)
    }

    func testCapacityHeadCancellationCannotCancelSiblingWatchdogOrReviveOldExpiry() throws {
        let controller = AppleGattIoController()
        let connection = connectionCorrelation(generation: 1, connectionId: 10)
        let characteristic = FakeIoCharacteristic(properties: properties(writableWithoutResponse: true))
        let first = try enqueueCapacityWrite(controller, connection: connection, characteristic: characteristic, id: 1)
        let second = try enqueueCapacityWrite(controller, connection: connection, characteristic: characteristic, id: 2)
        let sibling = rssiCommand(commandId: 3, connectionId: 10)
        try controller.enqueueRssi(command: sibling, connection: connection)
        let registry = AppleGattWatchdogRegistry()
        let firstToken = try armCapacityWait(controller, registry: registry, connection: connection, command: first)
        guard case .queued = controller.cancel(commandId: sibling.commandId) else {
            return XCTFail("sibling was not queued-cancellable")
        }
        XCTAssertNil(registry.cancel(connectionId: connection.connectionId, kind: .writeCapacity(sibling.commandId)))
        XCTAssertTrue(controller.isWaitingForWriteCapacity(connection: connection, commandId: first.commandId))
        XCTAssertNil(try nextAction(controller, connection: connection, canSendWriteWithoutResponse: false))
        guard case .queued = controller.cancel(commandId: first.commandId) else {
            return XCTFail("capacity-waiting head must remain queued-cancellable")
        }
        try XCTUnwrap(registry.cancel(connectionId: connection.connectionId, kind: .writeCapacity(first.commandId))).cancel()
        let secondToken = try armCapacityWait(controller, registry: registry, connection: connection, command: second)
        XCTAssertNil(registry.takeIfCurrent(connectionId: connection.connectionId, token: firstToken))
        XCTAssertFalse(controller.isWaitingForWriteCapacity(connection: connection, commandId: first.commandId))
        XCTAssertTrue(controller.isWaitingForWriteCapacity(connection: connection, commandId: second.commandId))
        XCTAssertEqual(controller.pendingCount, 1)

        _ = try XCTUnwrap(nextAction(controller, connection: connection))
        try XCTUnwrap(registry.cancel(connectionId: connection.connectionId, kind: .writeCapacity(second.commandId))).cancel()
        _ = controller.didSubmitWriteWithoutResponse(
            connection: connection, peripheral: connection.peripheral, characteristic: characteristic
        )
        let rssi = rssiCommand(commandId: 4, connectionId: 10)
        try controller.enqueueRssi(command: rssi, connection: connection)
        _ = try XCTUnwrap(nextAction(controller, connection: connection))
        let nativeToken = registry.arm(connectionId: connection.connectionId, kind: .io, workItem: DispatchWorkItem {}).token
        XCTAssertNil(registry.cancel(connectionId: connection.connectionId, kind: .writeCapacity(second.commandId)))
        XCTAssertNil(registry.takeIfCurrent(connectionId: connection.connectionId, token: secondToken))
        XCTAssertEqual(registry.diagnosticCount, 1)
        XCTAssertNotNil(registry.takeIfCurrent(connectionId: connection.connectionId, token: nativeToken))
        _ = controller.didReadRssi(connection: connection, peripheral: connection.peripheral, rssi: -60, error: nil)
        XCTAssertEqual(controller.pendingCount, 0)
    }

    func testCapacityWaitDisconnectAndDisposeRemoveTokensWithoutNativeBarriers() throws {
        for disposing in [false, true] {
            let controller = AppleGattIoController()
            let connection = connectionCorrelation(generation: 1, connectionId: 10)
            let characteristic = FakeIoCharacteristic(properties: properties(writableWithoutResponse: true))
            let head = try enqueueCapacityWrite(controller, connection: connection, characteristic: characteristic, id: 1)
            let registry = AppleGattWatchdogRegistry()
            let token = try armCapacityWait(controller, registry: registry, connection: connection, command: head)
            let other = ConnectionId(value: 11)
            let otherToken = registry.arm(connectionId: other, kind: .discovery, workItem: DispatchWorkItem {}).token
            if disposing {
                registry.cancelAll().forEach { $0.cancel() }
                controller.drain()
                XCTAssertEqual(registry.diagnosticCount, 0)
            } else {
                try XCTUnwrap(registry.cancel(connectionId: connection.connectionId)).cancel()
                let error = eventError(category: .connection, message: "disconnected before submission")
                let retirement = controller.retireAll(connection: connection, error: error)
                XCTAssertEqual(retirement.ioCompletions.count, 1)
                assertFailure(retirement.ioCompletions.first, commandId: head.commandId,
                              category: .connection, message: error.message)
                XCTAssertEqual(registry.diagnosticCount, 1, "connection teardown must not cancel its sibling")
                XCTAssertNotNil(registry.takeIfCurrent(connectionId: other, token: otherToken))
            }
            XCTAssertNil(registry.takeIfCurrent(connectionId: connection.connectionId, token: token))
            XCTAssertFalse(controller.isWaitingForWriteCapacity(connection: connection, commandId: head.commandId))
            XCTAssertFalse(controller.didBecomeReadyToSendWriteWithoutResponse(
                connection: connection, peripheral: connection.peripheral
            ))
            XCTAssertNil(try nextAction(controller, connection: connection))
            XCTAssertNil(controller.didSubmitWriteWithoutResponse(
                connection: connection, peripheral: connection.peripheral, characteristic: characteristic
            ))
            XCTAssertEqual(controller.pendingCount, 0)
            XCTAssertEqual(controller.diagnosticCallbackRouteCount, 0)
            XCTAssertTrue(controller.diagnosticAttributeIdentities.isEmpty)
        }
    }

    func testUnavailableAdapterOrAuthorizationRetiresActiveCccAndQueuedWriteWatchdog() throws {
        let inputs: [(CBManagerState, CBManagerAuthorization, BleErrorCategory)] = [
            (.poweredOff, .allowedAlways, .adapterUnavailable),
            (.poweredOn, .denied, .permission),
        ]
        for (state, authorization, category) in inputs {
            let context = "state=\(state), authorization=\(authorization)"
            let controller = AppleGattIoController()
            let connections = AppleConnectionController()
            let gatt = AppleGattController()
            let registry = AppleGattWatchdogRegistry()
            let peripheral = FakeIoPeripheral(identifier: UUID())
            let connect = ConnectCommand(
                commandId: CommandId(value: 90), connectionId: ConnectionId(value: 10),
                peripheralId: PeripheralId(value: 100)
            )
            let connection = try connections.begin(command: connect, peripheral: peripheral)
            XCTAssertEqual(connections.didConnect(peripheral: peripheral), .connected(connection))
            let characteristic = FakeIoCharacteristic(
                properties: properties(writableWithResponse: true, notifiable: true)
            )
            let ioTarget = target(
                connection: connection, sessionId: 20, characteristicId: 30,
                characteristic: characteristic
            )
            let enable = notificationCommand(
                commandId: 1, connectionId: 10, sessionId: 20, subscriptionId: 40,
                characteristicId: 30, action: .enable(mode: .platformDefault)
            )
            try controller.enqueueSetNotifications(command: enable, connection: connection, target: ioTarget)
            assertSetNotifyValueAction(
                try XCTUnwrap(controller.nextNotificationAction(connection: connection)),
                command: enable, characteristic: characteristic, enabled: true
            )
            let write = writeCommand(
                commandId: 2, connectionId: 10, sessionId: 20, characteristicId: 30,
                value: [1], writeType: .withResponse
            )
            try controller.enqueueWrite(command: write, connection: connection, target: ioTarget)
            let oldWork = DispatchWorkItem {}
            let oldToken = registry.arm(
                connectionId: connection.connectionId, kind: .io, workItem: oldWork
            ).token
            // A registry entry not owned by the drained routes must not be globally cancelled.
            let unrelatedId = ConnectionId(value: 11)
            let unrelatedWork = DispatchWorkItem {}
            let unrelatedToken = registry.arm(
                connectionId: unrelatedId, kind: .discovery, workItem: unrelatedWork
            ).token
            var nativeRetirements: [AppleConnectionCorrelation] = []
            func retire(_ state: CBManagerState, _ authorization: CBManagerAuthorization) -> [BleEvent] {
                AppleUnavailableConnectionRetirement.retireIfUnavailable(
                    state: state, authorization: authorization,
                    connections: connections, gatt: gatt, io: controller, watchdogs: registry
                ) { retiredConnection, retirement in
                    nativeRetirements.append(retiredConnection)
                    XCTAssertEqual(retirement.completions.count, 2, context)
                    XCTAssertEqual(retirement.activeNotificationCharacteristicsToUnsubscribe.count, 1, context)
                }
            }

            XCTAssertTrue(retire(.poweredOn, .allowedAlways).isEmpty, context)
            XCTAssertEqual(connections.connectedCorrelation(for: connection.connectionId), connection, context)
            XCTAssertEqual(controller.pendingCount, 2, context)
            XCTAssertEqual(registry.diagnosticCount, 2, context)
            XCTAssertFalse(oldWork.isCancelled, context)
            XCTAssertTrue(nativeRetirements.isEmpty, context)

            let error = try XCTUnwrap(AppleEventMapper.connectionAvailabilityError(
                state: state, authorization: authorization
            ))
            XCTAssertEqual(error.category, category, context)
            let events = retire(state, authorization)
            XCTAssertEqual(events, [
                .commandCompleted(commandId: enable.commandId, error: error),
                .commandCompleted(commandId: write.commandId, error: error),
                .connectionStateChanged(commandId: nil, connectionId: connection.connectionId,
                                        result: .err(error: error)),
            ], context)
            XCTAssertEqual(nativeRetirements, [connection], context)
            XCTAssertEqual(connections.activeCount, 0, context)
            XCTAssertEqual(controller.pendingCount, 0, context)
            XCTAssertEqual(controller.diagnosticSubscriptionCount, 0, context)
            XCTAssertGreaterThan(controller.diagnosticCallbackRouteCount, 0, "unresolved barriers must survive loss")
            XCTAssertTrue(controller.diagnosticAttributeIdentities.contains(ObjectIdentifier(characteristic)), context)
            XCTAssertTrue(oldWork.isCancelled, "retirement must cancel the active CCC watchdog: \(context)")
            XCTAssertEqual(registry.diagnosticCount, 1, "only the unrelated token may remain: \(context)")
            XCTAssertFalse(unrelatedWork.isCancelled, context)

            XCTAssertTrue(retire(state, authorization).isEmpty, "repeated loss must not duplicate terminals")
            XCTAssertEqual(nativeRetirements, [connection], context)
            XCTAssertEqual(registry.diagnosticCount, 1, context)
            XCTAssertTrue(retire(.poweredOn, .allowedAlways).isEmpty, context)

            let replacement = try connections.begin(command: ConnectCommand(
                commandId: CommandId(value: 91), connectionId: connect.connectionId,
                peripheralId: connect.peripheralId
            ), peripheral: peripheral)
            XCTAssertNotEqual(replacement.generation, connection.generation, context)
            XCTAssertEqual(connections.didConnect(peripheral: peripheral), .connected(replacement))
            // A fresh native characteristic does not erase the old CCC callback/value barriers.
            let freshCharacteristic = FakeIoCharacteristic(properties: properties(writableWithResponse: true))
            let recoveryWrite = writeCommand(
                commandId: 3, connectionId: 10, sessionId: 21, characteristicId: 31,
                value: [2], writeType: .withResponse
            )
            try controller.enqueueWrite(command: recoveryWrite, connection: replacement, target: target(
                connection: replacement, sessionId: 21, characteristicId: 31,
                characteristic: freshCharacteristic
            ))
            assertWriteWithResponseAction(
                try XCTUnwrap(nextAction(controller, connection: replacement)),
                command: recoveryWrite, characteristic: freshCharacteristic
            )
            let replacementToken = registry.arm(
                connectionId: replacement.connectionId, kind: .io, workItem: DispatchWorkItem {}
            ).token
            // This is the production expiry handler's registry gate, not a real Dispatch timer.
            XCTAssertNil(registry.takeIfCurrent(connectionId: connection.connectionId, token: oldToken), context)
            XCTAssertEqual(registry.diagnosticCount, 2, context)
            XCTAssertEqual(connections.connectedCorrelation(for: replacement.connectionId), replacement, context)
            XCTAssertTrue(controller.consumeRetiredNotificationStateCallback(
                peripheral: peripheral, characteristic: characteristic
            ), "late CCC callback must still encounter its retirement barrier")
            XCTAssertNil(controller.didUpdateNotificationState(
                connection: replacement, peripheral: peripheral, characteristic: characteristic,
                isNotifying: true, error: nil
            ), context)
            XCTAssertEqual(controller.pendingCount, 1, context)
            XCTAssertNotNil(registry.takeIfCurrent(connectionId: replacement.connectionId, token: replacementToken), context)
            assertWriteCompletion(controller.didWriteValue(
                connection: replacement, peripheral: peripheral,
                characteristic: freshCharacteristic, error: nil
            ), command: recoveryWrite)
            XCTAssertEqual(controller.pendingCount, 0, context)
            XCTAssertNotNil(registry.takeIfCurrent(connectionId: unrelatedId, token: unrelatedToken), context)
            XCTAssertEqual(registry.diagnosticCount, 0, context)
        }
    }

    private func enqueueCapacityWrite(
        _ controller: AppleGattIoController,
        connection: AppleConnectionCorrelation,
        characteristic: FakeIoCharacteristic,
        id: UInt64
    ) throws -> WriteCharacteristicCommand {
        let command = writeCommand(
            commandId: id, connectionId: connection.connectionId.value,
            sessionId: 20, characteristicId: 30, value: [1], writeType: .withoutResponse
        )
        try controller.enqueueWrite(command: command, connection: connection, target: target(
            connection: connection, sessionId: 20, characteristicId: 30, characteristic: characteristic
        ))
        return command
    }

    private func armCapacityWait(
        _ controller: AppleGattIoController,
        registry: AppleGattWatchdogRegistry,
        connection: AppleConnectionCorrelation,
        command: WriteCharacteristicCommand
    ) throws -> AppleGattWatchdogToken {
        assertWriteCapacityWaitAction(
            try XCTUnwrap(nextAction(controller, connection: connection, canSendWriteWithoutResponse: false)),
            command: command, connection: connection
        )
        return registry.arm(
            connectionId: connection.connectionId, kind: .writeCapacity(command.commandId),
            workItem: DispatchWorkItem {}
        ).token
    }

    private func assertWriteCapacityWaitAction(
        _ action: AppleGattIoAction,
        command: WriteCharacteristicCommand,
        connection: AppleConnectionCorrelation,
        file: StaticString = #filePath,
        line: UInt = #line
    ) {
        guard case .waitForWriteCapacity(let commandId, let owner) = action else {
            return XCTFail("expected capacity-wait action, not native submission", file: file, line: line)
        }
        XCTAssertEqual(commandId, command.commandId, file: file, line: line)
        XCTAssertEqual(owner, connection, file: file, line: line)
    }

    private func nextAction(
        _ controller: AppleGattIoController,
        connection: AppleConnectionCorrelation,
        canSendWriteWithoutResponse: Bool = true,
        maximumWriteWithResponse: Int = 512,
        maximumWriteWithoutResponse: Int = 512
    ) throws -> AppleGattIoAction? {
        try controller.nextAction(
            connection: connection,
            canSendWriteWithoutResponse: canSendWriteWithoutResponse,
            maximumWriteWithResponse: maximumWriteWithResponse,
            maximumWriteWithoutResponse: maximumWriteWithoutResponse
        )
    }

    private func connectionCorrelation(
        generation: UInt64,
        connectionId: UInt64,
        peripheral: FakeIoPeripheral = FakeIoPeripheral(identifier: UUID())
    ) -> AppleConnectionCorrelation {
        AppleConnectionCorrelation(
            generation: generation,
            connectCommandId: CommandId(value: generation),
            connectionId: ConnectionId(value: connectionId),
            peripheralId: PeripheralId(value: connectionId + 1_000),
            peripheral: peripheral
        )
    }

    private func target(
        connection: AppleConnectionCorrelation,
        sessionId: UInt64,
        characteristicId: UInt64,
        characteristic: any AppleGattCharacteristicObject
    ) -> AppleGattCharacteristicTarget {
        AppleGattCharacteristicTarget(
            connectionGeneration: connection.generation,
            gattSessionId: GattSessionId(value: sessionId),
            characteristicId: GattCharacteristicId(value: characteristicId),
            characteristic: characteristic
        )
    }

    private func rssiCommand(commandId: UInt64, connectionId: UInt64) -> ReadRssiCommand {
        ReadRssiCommand(
            commandId: CommandId(value: commandId),
            connectionId: ConnectionId(value: connectionId)
        )
    }

    private func readCommand(
        commandId: UInt64,
        connectionId: UInt64,
        sessionId: UInt64,
        characteristicId: UInt64
    ) -> ReadCharacteristicCommand {
        ReadCharacteristicCommand(
            commandId: CommandId(value: commandId),
            connectionId: ConnectionId(value: connectionId),
            gattSessionId: GattSessionId(value: sessionId),
            characteristicId: GattCharacteristicId(value: characteristicId)
        )
    }

    private func writeCommand(
        commandId: UInt64,
        connectionId: UInt64,
        sessionId: UInt64,
        characteristicId: UInt64,
        value: [UInt8],
        writeType: WriteType
    ) -> WriteCharacteristicCommand {
        WriteCharacteristicCommand(
            commandId: CommandId(value: commandId),
            connectionId: ConnectionId(value: connectionId),
            gattSessionId: GattSessionId(value: sessionId),
            characteristicId: GattCharacteristicId(value: characteristicId),
            value: Data(value),
            writeType: writeType
        )
    }

    private func notificationCommand(
        commandId: UInt64,
        connectionId: UInt64,
        sessionId: UInt64,
        subscriptionId: UInt64,
        characteristicId: UInt64,
        action: NotificationAction
    ) -> SetNotificationsCommand {
        SetNotificationsCommand(
            commandId: CommandId(value: commandId),
            connectionId: ConnectionId(value: connectionId),
            gattSessionId: GattSessionId(value: sessionId),
            subscriptionId: SubscriptionId(value: subscriptionId),
            characteristicId: GattCharacteristicId(value: characteristicId),
            action: action
        )
    }

    private func properties(
        readable: Bool = false,
        writableWithResponse: Bool = false,
        writableWithoutResponse: Bool = false,
        notifiable: Bool = false,
        indicatable: Bool = false
    ) -> CharacteristicProperties {
        CharacteristicProperties(
            readable: readable,
            writableWithResponse: writableWithResponse,
            writableWithoutResponse: writableWithoutResponse,
            notifiable: notifiable,
            indicatable: indicatable
        )
    }

    private func activateNotificationSubscriptions(
        count: Int,
        controller: AppleGattIoController,
        connection: AppleConnectionCorrelation,
        sessionId: UInt64
    ) throws {
        for index in 0..<count {
            let value = UInt64(index + 1)
            let characteristic = FakeIoCharacteristic(
                properties: properties(notifiable: true)
            )
            let command = notificationCommand(
                commandId: value,
                connectionId: connection.connectionId.value,
                sessionId: sessionId,
                subscriptionId: value,
                characteristicId: value,
                action: .enable(mode: .platformDefault)
            )
            try controller.enqueueSetNotifications(
                command: command,
                connection: connection,
                target: target(
                    connection: connection,
                    sessionId: sessionId,
                    characteristicId: value,
                    characteristic: characteristic
                )
            )
            guard case .setNotifyValue = controller.nextNotificationAction(
                connection: connection
            ) else {
                throw CocoaError(.coderInvalidValue)
            }
            guard case .enabled = controller.didUpdateNotificationState(
                connection: connection,
                peripheral: connection.peripheral,
                characteristic: characteristic,
                isNotifying: true,
                error: nil
            ) else {
                throw CocoaError(.coderInvalidValue)
            }
        }
    }

    private func eventError(
        category: BleErrorCategory,
        message: String
    ) -> BleEventError {
        BleEventError(category: category, message: message, platformDetails: nil)
    }

    private func assertInvalidRequest(
        _ operation: () throws -> Void,
        file: StaticString = #filePath,
        line: UInt = #line
    ) {
        XCTAssertThrowsError(try operation(), file: file, line: line) {
            guard case CommandDispatchError.InvalidRequest = $0 else {
                return XCTFail("unexpected error: \($0)", file: file, line: line)
            }
        }
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

    private func assertSetNotifyValueAction(
        _ action: AppleGattNotificationAction,
        command: SetNotificationsCommand,
        characteristic: any AppleGattCharacteristicObject,
        enabled: Bool,
        file: StaticString = #filePath,
        line: UInt = #line
    ) {
        guard case .setNotifyValue(
            let observedCommand,
            _,
            let observedCharacteristic,
            let observedEnabled
        ) = action else {
            return XCTFail("expected setNotifyValue action", file: file, line: line)
        }
        XCTAssertEqual(observedCommand, command, file: file, line: line)
        XCTAssertEqual(observedEnabled, enabled, file: file, line: line)
        XCTAssertEqual(
            ObjectIdentifier(observedCharacteristic),
            ObjectIdentifier(characteristic),
            file: file,
            line: line
        )
    }

    private func assertNotificationTerminalFailure(
        _ action: AppleGattNotificationAction,
        command: SetNotificationsCommand,
        category: BleErrorCategory,
        file: StaticString = #filePath,
        line: UInt = #line
    ) {
        guard case .terminal(let completion) = action else {
            return XCTFail("expected notification terminal action", file: file, line: line)
        }
        assertNotificationFailure(
            completion,
            command: command,
            category: category,
            file: file,
            line: line
        )
    }

    private func assertNotificationEnabled(
        _ completion: AppleGattNotificationCompletion?,
        command: SetNotificationsCommand,
        actualMode: NotificationMode,
        file: StaticString = #filePath,
        line: UInt = #line
    ) {
        guard case .enabled(let observedCommand, let observedMode) = completion else {
            return XCTFail("expected notification enabled completion", file: file, line: line)
        }
        XCTAssertEqual(observedCommand, command, file: file, line: line)
        XCTAssertEqual(observedMode, actualMode, file: file, line: line)
    }

    private func assertNotificationDisabled(
        _ completion: AppleGattNotificationCompletion?,
        command: SetNotificationsCommand,
        file: StaticString = #filePath,
        line: UInt = #line
    ) {
        guard case .disabled(let observedCommand) = completion else {
            return XCTFail("expected notification disabled completion", file: file, line: line)
        }
        XCTAssertEqual(observedCommand, command, file: file, line: line)
    }

    private func assertNotificationFailure(
        _ completion: AppleGattNotificationCompletion?,
        command: SetNotificationsCommand,
        category: BleErrorCategory,
        message: String? = nil,
        file: StaticString = #filePath,
        line: UInt = #line
    ) {
        guard case .failed(let observedCommand, let error) = completion else {
            return XCTFail("expected notification failure completion", file: file, line: line)
        }
        XCTAssertEqual(observedCommand, command, file: file, line: line)
        XCTAssertEqual(error.category, category, file: file, line: line)
        if let message {
            XCTAssertEqual(error.message, message, file: file, line: line)
        }
    }

    private func assertIgnoredValue(
        _ disposition: AppleGattValueDisposition,
        file: StaticString = #filePath,
        line: UInt = #line
    ) {
        guard case .ignored = disposition else {
            return XCTFail("expected ignored value disposition", file: file, line: line)
        }
    }

    private func assertNotificationValue(
        _ disposition: AppleGattValueDisposition,
        command: SetNotificationsCommand,
        value: [UInt8],
        file: StaticString = #filePath,
        line: UInt = #line
    ) {
        guard case .notification(let notification) = disposition else {
            return XCTFail("expected notification value disposition", file: file, line: line)
        }
        XCTAssertEqual(notification.connectionId, command.connectionId, file: file, line: line)
        XCTAssertEqual(notification.gattSessionId, command.gattSessionId, file: file, line: line)
        XCTAssertEqual(notification.subscriptionId, command.subscriptionId, file: file, line: line)
        XCTAssertEqual(notification.characteristicId, command.characteristicId, file: file, line: line)
        XCTAssertEqual(notification.value, value, file: file, line: line)
    }

    private func assertReadAction(
        _ action: AppleGattIoAction,
        command: ReadCharacteristicCommand,
        characteristic: any AppleGattCharacteristicObject,
        file: StaticString = #filePath,
        line: UInt = #line
    ) {
        guard case .read(let observedCommand, _, let observedCharacteristic) = action else {
            return XCTFail("expected read action", file: file, line: line)
        }
        XCTAssertEqual(observedCommand, command, file: file, line: line)
        XCTAssertEqual(
            ObjectIdentifier(observedCharacteristic),
            ObjectIdentifier(characteristic),
            file: file,
            line: line
        )
    }

    private func assertWriteWithResponseAction(
        _ action: AppleGattIoAction,
        command: WriteCharacteristicCommand,
        characteristic: any AppleGattCharacteristicObject,
        file: StaticString = #filePath,
        line: UInt = #line
    ) {
        guard case .writeWithResponse(
            let observedCommand,
            _,
            let observedCharacteristic
        ) = action else {
            return XCTFail("expected with-response write action", file: file, line: line)
        }
        XCTAssertEqual(observedCommand, command, file: file, line: line)
        XCTAssertEqual(
            ObjectIdentifier(observedCharacteristic),
            ObjectIdentifier(characteristic),
            file: file,
            line: line
        )
    }

    private func assertWriteWithoutResponseAction(
        _ action: AppleGattIoAction,
        command: WriteCharacteristicCommand,
        characteristic: any AppleGattCharacteristicObject,
        file: StaticString = #filePath,
        line: UInt = #line
    ) {
        guard case .writeWithoutResponse(
            let observedCommand,
            _,
            let observedCharacteristic
        ) = action else {
            return XCTFail("expected without-response write action", file: file, line: line)
        }
        XCTAssertEqual(observedCommand, command, file: file, line: line)
        XCTAssertEqual(
            ObjectIdentifier(observedCharacteristic),
            ObjectIdentifier(characteristic),
            file: file,
            line: line
        )
    }

    private func assertReadCompletion(
        _ completion: AppleGattIoCompletion?,
        command: ReadCharacteristicCommand,
        value: [UInt8],
        file: StaticString = #filePath,
        line: UInt = #line
    ) {
        guard case .read(let observedCommand, let observedValue) = completion else {
            return XCTFail("expected read completion", file: file, line: line)
        }
        XCTAssertEqual(observedCommand, command, file: file, line: line)
        XCTAssertEqual(observedValue, value, file: file, line: line)
    }

    private func assertWriteCompletion(
        _ completion: AppleGattIoCompletion?,
        command: WriteCharacteristicCommand,
        file: StaticString = #filePath,
        line: UInt = #line
    ) {
        guard case .write(let observedCommand) = completion else {
            return XCTFail("expected write completion", file: file, line: line)
        }
        XCTAssertEqual(observedCommand, command, file: file, line: line)
    }

    private func assertFailure(
        _ completion: AppleGattIoCompletion?,
        commandId: CommandId,
        category: BleErrorCategory,
        message: String,
        file: StaticString = #filePath,
        line: UInt = #line
    ) {
        guard case .failed(let command, let error) = completion else {
            return XCTFail("expected failure completion", file: file, line: line)
        }
        XCTAssertEqual(ioCommandId(command), commandId, file: file, line: line)
        XCTAssertEqual(error.category, category, file: file, line: line)
        XCTAssertEqual(error.message, message, file: file, line: line)
    }

    private func assertTerminalFailure(
        _ action: AppleGattIoAction,
        commandId: CommandId,
        category: BleErrorCategory,
        file: StaticString = #filePath,
        line: UInt = #line
    ) {
        guard case .terminal(let completion) = action,
              case .failed(let command, let error) = completion
        else {
            return XCTFail("expected terminal failure action", file: file, line: line)
        }
        XCTAssertEqual(ioCommandId(command), commandId, file: file, line: line)
        XCTAssertEqual(error.category, category, file: file, line: line)
    }

    private func ioCommandId(_ command: AppleGattIoCommand) -> CommandId {
        switch command {
        case .read(let command):
            return command.commandId
        case .write(let command):
            return command.commandId
        case .rssi(let command):
            return command.commandId
        }
    }
}

private final class FakeIoPeripheral: ApplePeripheralObject {
    let identifier: UUID

    init(identifier: UUID) {
        self.identifier = identifier
    }
}

private final class WeakIoCharacteristic {
    weak var value: FakeIoCharacteristic?

    init(_ value: FakeIoCharacteristic) {
        self.value = value
    }
}

private final class FakeIoCharacteristic: AppleGattCharacteristicObject {
    let appleGattUuid: BluetoothUuid
    let appleGattProperties: CharacteristicProperties

    init(
        uuid: BluetoothUuid = BluetoothUuid(value: "fff10000-0000-4000-8000-000000000000"),
        properties: CharacteristicProperties
    ) {
        appleGattUuid = uuid
        appleGattProperties = properties
    }
}
