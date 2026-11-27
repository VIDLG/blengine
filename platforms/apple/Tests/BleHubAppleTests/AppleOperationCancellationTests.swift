import BleHubUniFfi
import Foundation
import XCTest
@testable import BleHubApple

final class AppleOperationCancellationTests: XCTestCase {
    func testLiveConnectIsNotCancellableButCompletedOrRetiredConnectIsNotFound() throws {
        for completes in [false, true] {
            let fixture = Fixture()
            let connection = try fixture.begin()
            fixture.assertRejected(1, live: true)
            fixture.assertRejected(1, live: true)
            fixture.assertRejected(999, live: false)
            XCTAssertEqual(fixture.connections.activeCount, 1)
            if completes {
                _ = fixture.connections.didConnect(peripheral: fixture.peripheral)
                fixture.assertRejected(1, live: false)
            }
            _ = try fixture.connections.beginDisconnect(command: DisconnectCommand(
                commandId: CommandId(value: 2), connectionId: connection.connectionId
            ))
            fixture.assertRejected(1, live: !completes)
            XCTAssertNotNil(fixture.connections.didDisconnect(peripheral: fixture.peripheral))
            fixture.assertRejected(1, live: false)
            XCTAssertEqual(fixture.connections.activeCount, 0)
        }
        let failed = Fixture()
        _ = try failed.begin()
        XCTAssertNotNil(failed.connections.didFailToConnect(peripheral: failed.peripheral))
        failed.assertRejected(1, live: false)
    }

    func testLiveDiscoveryIsNotCancellableUntilCompletionFailureOrRetirement() throws {
        for ending in 0..<3 {
            let fixture = Fixture()
            let connection = try fixture.begin()
            _ = fixture.connections.didConnect(peripheral: fixture.peripheral)
            let command = DiscoverGattCommand(
                commandId: CommandId(value: 3), connectionId: connection.connectionId,
                gattSessionId: GattSessionId(value: 20)
            )
            _ = try fixture.gatt.begin(command: command, connection: connection)
            fixture.assertRejected(3, live: true)
            fixture.assertRejected(3, live: true)
            fixture.assertRejected(1, live: false)
            fixture.assertRejected(999, live: false)
            XCTAssertTrue(fixture.gatt.hasPendingDiscovery(connection: connection))
            XCTAssertEqual(fixture.gatt.pendingCount, 1)
            XCTAssertEqual(fixture.connections.activeCount, 1)
            switch ending {
            case 0:
                guard case .completed = try fixture.gatt.didDiscoverServices(
                    connection: connection, services: []
                ) else { return XCTFail("discovery did not complete") }
                XCTAssertNotNil(fixture.gatt.activeDatabase(connection: connection))
            case 1:
                XCTAssertEqual(fixture.gatt.failCurrent(connection: connection), command)
            default:
                XCTAssertEqual(fixture.gatt.retire(connection: connection), command)
            }
            fixture.assertRejected(3, live: false)
            XCTAssertFalse(fixture.gatt.hasPendingDiscovery(connection: connection))
            XCTAssertEqual(fixture.gatt.pendingCount, 0)
            XCTAssertEqual(fixture.connections.activeCount, 1)
        }
    }

    func testQueuedIoStillCancelsAndActiveIoIsRejectedWithoutRemovingHead() throws {
        let fixture = Fixture()
        let connection = try fixture.begin()
        _ = fixture.connections.didConnect(peripheral: fixture.peripheral)
        for id in [UInt64(4), 5] {
            try fixture.io.enqueueRssi(command: ReadRssiCommand(
                commandId: CommandId(value: id), connectionId: connection.connectionId
            ), connection: connection)
        }
        guard case .rssi = try fixture.io.nextAction(
            connection: connection, canSendWriteWithoutResponse: true,
            maximumWriteWithResponse: 512, maximumWriteWithoutResponse: 512
        ) else { return XCTFail("RSSI did not become active") }
        fixture.assertRejected(4, live: true)
        XCTAssertEqual(fixture.io.pendingCount, 2)
        XCTAssertEqual(try fixture.cancel(5), connection)
        XCTAssertEqual(fixture.io.pendingCount, 1)
        fixture.assertRejected(5, live: false)
        fixture.assertRejected(999, live: false)
        fixture.assertRejected(4, live: true)
        XCTAssertEqual(fixture.io.pendingCount, 1)
        XCTAssertEqual(fixture.connections.activeCount, 1)
    }

    private final class Peripheral: ApplePeripheralObject {
        let identifier = UUID()
    }

    private final class Fixture {
        let connections = AppleConnectionController()
        let gatt = AppleGattController()
        let io = AppleGattIoController()
        let peripheral = Peripheral()

        func begin() throws -> AppleConnectionCorrelation {
            try connections.begin(command: ConnectCommand(
                commandId: CommandId(value: 1), connectionId: ConnectionId(value: 10),
                peripheralId: PeripheralId(value: 100), autoConnect: false
            ), peripheral: peripheral)
        }

        func cancel(_ id: UInt64) throws -> AppleConnectionCorrelation {
            try AppleOperationCancellation.cancel(
                commandId: CommandId(value: id), connections: connections, gatt: gatt, io: io
            )
        }

        func assertRejected(
            _ id: UInt64, live: Bool, file: StaticString = #filePath, line: UInt = #line
        ) {
            XCTAssertThrowsError(try cancel(id), file: file, line: line) { error in
                switch error {
                case CommandDispatchError.OperationNotCancellable(let target) where live:
                    XCTAssertEqual(target, CommandId(value: id), file: file, line: line)
                case CommandDispatchError.OperationNotFound(let target) where !live:
                    XCTAssertEqual(target, CommandId(value: id), file: file, line: line)
                default:
                    XCTFail("unexpected cancellation rejection: \(error); live=\(live)", file: file, line: line)
                }
            }
        }
    }
}
