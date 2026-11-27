import BleHubUniFfi
import Dispatch
import Foundation
import XCTest
@testable import BleHubApple

final class GattWatchdogOrchestrationTests: XCTestCase {
    func testRepeatedCapacityDriveSchedulesOnceAndCurrentExpiryRetiresOnlyItsOwnerOnce() throws {
        let fixture = Fixture()
        let connection = try fixture.connect(id: 10, peer: 100)
        let other = try fixture.connect(id: 11, peer: 101)
        let head = try fixture.enqueueWrite(connection, id: 1)
        try fixture.io.enqueueRssi(command: fixture.rssi(connection, id: 2), connection: connection)
        try fixture.io.enqueueRssi(command: fixture.rssi(other, id: 3), connection: other)
        let otherToken = fixture.arm(other, kind: .io, context: "other connection")

        guard case .waitForWriteCapacity(let id, let owner) = try fixture.drive(connection, ready: false) else {
            return XCTFail("capacity wait did not reach the scheduling seam")
        }
        XCTAssertEqual(id, head.commandId)
        XCTAssertEqual(owner, connection)
        XCTAssertEqual(fixture.jobs.count, 2)
        let waiting = fixture.jobs[1]
        let token = try XCTUnwrap(fixture.tokens.last)
        XCTAssertEqual(token.kind, .writeCapacity(head.commandId))
        for _ in 0..<3 {
            XCTAssertTrue(fixture.io.didBecomeReadyToSendWriteWithoutResponse(
                connection: connection, peripheral: connection.peripheral
            ))
            XCTAssertNil(try fixture.drive(connection, ready: false))
        }
        XCTAssertEqual(fixture.jobs.count, 2, "repeated drive must not enqueue another deadline")
        XCTAssertEqual(fixture.jobs[1].deadline, waiting.deadline)
        XCTAssertFalse(waiting.item.isCancelled)
        XCTAssertEqual(fixture.watchdogs.diagnosticCount, 2)
        XCTAssertTrue(fixture.submissions.isEmpty)

        waiting.item.perform()
        let timeout = AppleEventMapper.operationTimeout(
            "Apple GATT operation watchdog expired: write without response capacity"
        )
        let aborted = AppleEventMapper.operationAborted("Apple GATT connection recovery retired a pending operation")
        let expected: [BleEvent] = [
            .commandCompleted(commandId: head.commandId, error: timeout),
            .commandCompleted(commandId: CommandId(value: 2), error: aborted),
        ]
        XCTAssertEqual(fixture.events, expected)
        XCTAssertEqual(fixture.order, ["cleanup:10", "publish:10"])
        XCTAssertEqual(fixture.cleaned, [connection])
        XCTAssertEqual(fixture.nativeLookups, 1)
        XCTAssertNil(fixture.connections.connectedCorrelation(for: connection.connectionId))
        XCTAssertEqual(fixture.connections.activeCount, 2, "expiry invalidates, but awaits disconnect to remove the route")
        XCTAssertEqual(fixture.connections.connectedCorrelation(for: other.connectionId), other)
        XCTAssertEqual(fixture.io.pendingCount, 1, "only the other connection remains")
        XCTAssertEqual(fixture.watchdogs.diagnosticCount, 1)
        XCTAssertFalse(fixture.jobs[0].item.isCancelled)
        XCTAssertFalse(fixture.io.didBecomeReadyToSendWriteWithoutResponse(
            connection: connection, peripheral: connection.peripheral
        ))
        XCTAssertNil(try fixture.drive(connection, ready: true))
        XCTAssertFalse(fixture.io.consumeRetiredWriteCallback(
            peripheral: connection.peripheral, characteristic: fixture.characteristic
        ), "capacity wait must not create a native write callback barrier")

        // Invoke the same expiry entry even if GCD would skip a cancelled item.
        fixture.expire(connection, token: token, context: "duplicate capacity expiry")
        XCTAssertEqual(fixture.events, expected)
        XCTAssertEqual(fixture.cleaned, [connection])
        XCTAssertEqual(fixture.nativeLookups, 1)
        XCTAssertNotNil(fixture.connections.didDisconnect(peripheral: connection.peripheral))
        let replacement = try fixture.connect(id: 12, peer: 100, peripheral: connection.peripheral)
        XCTAssertNotEqual(replacement.generation, connection.generation)
        _ = try fixture.enqueueWrite(replacement, id: 4)
        _ = try fixture.drive(replacement, ready: false)
        fixture.expire(connection, token: token, context: "old owner expiry")
        XCTAssertEqual(fixture.connections.connectedCorrelation(for: replacement.connectionId), replacement)
        XCTAssertEqual(fixture.watchdogs.diagnosticCount, 2)
        XCTAssertEqual(fixture.events, expected)
        XCTAssertEqual(fixture.cleaned, [connection])
        XCTAssertNotEqual(try XCTUnwrap(fixture.tokens.last), otherToken)
    }

    func testReadinessCancelsMatchingCapacityDeadlineBeforeSubmissionAndOldExpiryCannotTakeSuccessor() throws {
        let fixture = Fixture()
        let connection = try fixture.connect(id: 10, peer: 100)
        let head = try fixture.enqueueWrite(connection, id: 1)
        let sibling = fixture.rssi(connection, id: 2)
        try fixture.io.enqueueRssi(command: sibling, connection: connection)
        _ = try fixture.drive(connection, ready: false)
        let capacityToken = try XCTUnwrap(fixture.tokens.last)
        let waiting = try XCTUnwrap(fixture.jobs.last)
        guard case .writeWithoutResponse(let command, let owner, let characteristic) = try fixture.drive(connection, ready: true) else {
            return XCTFail("readiness did not select local submission")
        }
        XCTAssertFalse(waiting.item.isCancelled, "native route validation still precedes submission cancellation")
        let completion = AppleGattWatchdogOrchestration.submitWithoutResponse(
            command: command, connection: owner, peripheral: connection.peripheral,
            characteristic: characteristic, io: fixture.io, watchdogs: fixture.watchdogs
        ) {
            XCTAssertTrue(waiting.item.isCancelled, "cancel must precede the native submission port")
            XCTAssertEqual(fixture.watchdogs.diagnosticCount, 0)
            fixture.submissions.append(command.commandId)
        }
        XCTAssertEqual(fixture.submissions, [head.commandId])
        let expected: [BleEvent] = [
            .characteristicWritten(commandId: head.commandId, connectionId: head.connectionId,
                                   gattSessionId: head.gattSessionId, characteristicId: head.characteristicId),
            .commandCompleted(commandId: head.commandId, error: nil),
        ]
        XCTAssertEqual(AppleBackendEventProjection.gattIoEvents(try XCTUnwrap(completion)), expected)
        XCTAssertEqual(fixture.jobs.count, 1)
        XCTAssertEqual(fixture.io.pendingCount, 1)
        guard case .rssi(let selected, _) = try fixture.drive(connection, ready: true) else {
            return XCTFail("successor did not retain the FIFO head")
        }
        XCTAssertEqual(selected, sibling)
        let successor = fixture.arm(connection, kind: .io, context: "RSSI read")
        XCTAssertNotEqual(successor, capacityToken)
        fixture.expire(connection, token: capacityToken, context: "late capacity expiry")
        XCTAssertEqual(fixture.watchdogs.diagnosticCount, 1)
        XCTAssertFalse(try XCTUnwrap(fixture.jobs.last).item.isCancelled)
        XCTAssertEqual(fixture.io.pendingCount, 1)
        XCTAssertTrue(fixture.cleaned.isEmpty)
        XCTAssertTrue(fixture.events.isEmpty)
        XCTAssertEqual(fixture.nativeLookups, 0, "stale token must be rejected before native cleanup lookup")
        XCTAssertEqual(fixture.connections.connectedCorrelation(for: connection.connectionId), connection)
        try XCTUnwrap(fixture.jobs.last).item.perform()
        XCTAssertEqual(fixture.events, [
            .commandCompleted(commandId: sibling.commandId, error: AppleEventMapper.operationTimeout(
                "Apple GATT operation watchdog expired: RSSI read"
            )),
        ])
        XCTAssertEqual(fixture.cleaned, [connection])
        XCTAssertEqual(fixture.watchdogs.diagnosticCount, 0)
        XCTAssertEqual(fixture.io.pendingCount, 0)
    }

    func testDiscoveryExpiryRetiresDraftBeforeNativeCleanupAndKeepsTimeoutClassification() throws {
        let fixture = Fixture()
        let connection = try fixture.connect(id: 10, peer: 100)
        let command = DiscoverGattCommand(
            commandId: CommandId(value: 5), connectionId: connection.connectionId,
            gattSessionId: GattSessionId(value: 20)
        )
        _ = try fixture.gatt.begin(command: command, connection: connection)
        let token = fixture.arm(connection, kind: .discovery, context: "service discovery")
        try XCTUnwrap(fixture.jobs.last).item.perform()
        let expected: [BleEvent] = [
            .commandCompleted(commandId: command.commandId, error: AppleEventMapper.operationTimeout(
                "Apple GATT operation watchdog expired: service discovery"
            )),
        ]
        XCTAssertEqual(fixture.events, expected)
        XCTAssertEqual(fixture.gatt.pendingCount, 0)
        XCTAssertEqual(fixture.watchdogs.diagnosticCount, 0)
        XCTAssertEqual(fixture.cleaned, [connection])
        XCTAssertEqual(fixture.order, ["cleanup:10", "publish:10"])
        fixture.expire(connection, token: token, context: "late discovery expiry")
        XCTAssertEqual(fixture.events, expected)
        XCTAssertEqual(fixture.cleaned, [connection])
    }

    func testExpiryKeepsNativeAvailabilityGuardAndRejectsCancelledRegisteredItem() throws {
        for nativeAvailable in [false, true] {
            let fixture = Fixture()
            let connection = try fixture.connect(id: 10, peer: 100)
            _ = try fixture.enqueueWrite(connection, id: 1)
            _ = try fixture.drive(connection, ready: false)
            let token = try XCTUnwrap(fixture.tokens.last)
            fixture.nativeAvailable = nativeAvailable
            if nativeAvailable {
                try XCTUnwrap(fixture.jobs.last).item.cancel()
            }
            fixture.expire(connection, token: token, context: "ineligible expiry")
            XCTAssertTrue(fixture.cleaned.isEmpty)
            XCTAssertTrue(fixture.events.isEmpty)
            XCTAssertEqual(fixture.nativeLookups, nativeAvailable ? 0 : 1)
            XCTAssertEqual(fixture.watchdogs.diagnosticCount, 0)
            XCTAssertEqual(fixture.io.pendingCount, 1)
            XCTAssertEqual(fixture.connections.connectedCorrelation(for: connection.connectionId), connection)
        }
    }

    private final class Peripheral: ApplePeripheralObject {
        let identifier = UUID()
    }

    private final class Characteristic: AppleGattCharacteristicObject {
        let appleGattUuid = BluetoothUuid(value: "fff10000-0000-4000-8000-000000000000")
        let appleGattProperties = CharacteristicProperties(
            readable: false, writableWithResponse: false, writableWithoutResponse: true,
            notifiable: false, indicatable: false
        )
    }

    private final class Fixture {
        let connections = AppleConnectionController()
        let gatt = AppleGattController()
        let io = AppleGattIoController()
        let watchdogs = AppleGattWatchdogRegistry()
        let characteristic = Characteristic()
        var jobs: [(deadline: DispatchTime, item: DispatchWorkItem)] = []
        var tokens: [AppleGattWatchdogToken] = []
        var events: [BleEvent] = []
        var cleaned: [AppleConnectionCorrelation] = []
        var submissions: [CommandId] = []
        var order: [String] = []
        var nativeAvailable = true
        var nativeLookups = 0

        func connect(id: UInt64, peer: UInt64, peripheral: (any ApplePeripheralObject)? = nil) throws -> AppleConnectionCorrelation {
            let peripheral = peripheral ?? Peripheral()
            let connection = try connections.begin(command: ConnectCommand(
                commandId: CommandId(value: id + 100), connectionId: ConnectionId(value: id),
                peripheralId: PeripheralId(value: peer), autoConnect: false
            ), peripheral: peripheral)
            XCTAssertEqual(connections.didConnect(peripheral: peripheral), .connected(connection))
            return connection
        }

        func enqueueWrite(_ connection: AppleConnectionCorrelation, id: UInt64) throws -> WriteCharacteristicCommand {
            let command = WriteCharacteristicCommand(
                commandId: CommandId(value: id), connectionId: connection.connectionId,
                gattSessionId: GattSessionId(value: 20), characteristicId: GattCharacteristicId(value: 30),
                value: Data([1]), writeType: .withoutResponse
            )
            try io.enqueueWrite(command: command, connection: connection, target: AppleGattCharacteristicTarget(
                connectionGeneration: connection.generation, gattSessionId: command.gattSessionId,
                characteristicId: command.characteristicId, characteristic: characteristic
            ))
            return command
        }

        func rssi(_ connection: AppleConnectionCorrelation, id: UInt64) -> ReadRssiCommand {
            ReadRssiCommand(commandId: CommandId(value: id), connectionId: connection.connectionId)
        }

        func drive(_ connection: AppleConnectionCorrelation, ready: Bool) throws -> AppleGattIoAction? {
            try AppleGattWatchdogOrchestration.nextAction(
                connection: connection, io: io, canSendWriteWithoutResponse: ready,
                maximumWriteWithResponse: 512, maximumWriteWithoutResponse: 512
            ) { owner, commandId in
                _ = self.arm(owner, kind: .writeCapacity(commandId), context: "write without response capacity")
            }
        }

        @discardableResult
        func arm(_ connection: AppleConnectionCorrelation, kind: AppleGattWatchdogKind, context: String) -> AppleGattWatchdogToken {
            let token = AppleGattWatchdogOrchestration.arm(
                connection: connection, kind: kind, watchdogs: watchdogs, delay: .milliseconds(10_000),
                schedule: { deadline, item in self.jobs.append((deadline, item)) },
                expire: { [weak self] token in self?.expire(connection, token: token, context: context) }
            )
            tokens.append(token)
            return token
        }

        func expire(_ connection: AppleConnectionCorrelation, token: AppleGattWatchdogToken, context: String) {
            if let expired = AppleGattWatchdogOrchestration.expire(
                connection: connection, token: token, context: context, watchdogs: watchdogs,
                connections: connections, gatt: gatt, io: io,
                resolveNative: {
                    self.nativeLookups += 1
                    return self.nativeAvailable ? connection.peripheral as? Peripheral : nil
                },
                retireNative: { peripheral, retirement in
                    XCTAssertTrue(peripheral === connection.peripheral)
                    XCTAssertFalse(self.io.hasPending(connection: connection))
                    XCTAssertFalse(self.gatt.hasPendingDiscovery(connection: connection))
                    XCTAssertNil(self.connections.connectedCorrelation(for: connection.connectionId))
                    XCTAssertTrue(retirement.activeNotificationCharacteristicsToUnsubscribe.isEmpty)
                    self.cleaned.append(connection)
                    self.order.append("cleanup:\(connection.connectionId.value)")
                }
            ) {
                order.append("publish:\(connection.connectionId.value)")
                events.append(contentsOf: expired)
            }
        }
    }
}
