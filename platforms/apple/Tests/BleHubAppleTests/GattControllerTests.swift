import BleHubUniFfi
import CoreBluetooth
import Foundation
import XCTest
@testable import BleHubApple

final class GattControllerTests: XCTestCase {
    func testDiscoveryPreservesDuplicateInstancesAndCommitsOnlyAfterEveryLevel() throws {
        let controller = AppleGattController()
        let connection = connectionCorrelation(generation: 1)
        let command = discoveryCommand(commandId: 10, sessionId: 20)
        _ = try controller.begin(command: command, connection: connection)

        let duplicateServiceUuid = uuid("fff00000-0000-4000-8000-000000000000")
        let duplicateCharacteristicUuid = uuid("fff10000-0000-4000-8000-000000000000")
        let duplicateDescriptorUuid = uuid("00002902-0000-1000-8000-00805f9b34fb")
        let firstService = FakeService(uuid: duplicateServiceUuid, primary: true)
        let secondService = FakeService(uuid: duplicateServiceUuid, primary: false)
        let firstCharacteristic = FakeCharacteristic(
            uuid: duplicateCharacteristicUuid,
            properties: properties(readable: true, notifiable: true)
        )
        let secondCharacteristic = FakeCharacteristic(
            uuid: duplicateCharacteristicUuid,
            properties: properties(writableWithResponse: true, indicatable: true)
        )
        let firstDescriptor = FakeDescriptor(uuid: duplicateDescriptorUuid)
        let secondDescriptor = FakeDescriptor(uuid: duplicateDescriptorUuid)

        assertDiscoversCharacteristics(
            try controller.didDiscoverServices(
                connection: connection,
                services: [firstService, secondService]
            ),
            expected: firstService
        )
        assertDiscoversDescriptors(
            try controller.didDiscoverCharacteristics(
                connection: connection,
                service: firstService,
                characteristics: [firstCharacteristic, secondCharacteristic]
            ),
            expected: firstCharacteristic
        )
        assertDiscoversDescriptors(
            try controller.didDiscoverDescriptors(
                connection: connection,
                characteristic: firstCharacteristic,
                descriptors: [firstDescriptor, secondDescriptor]
            ),
            expected: secondCharacteristic
        )
        assertDiscoversCharacteristics(
            try controller.didDiscoverDescriptors(
                connection: connection,
                characteristic: secondCharacteristic,
                descriptors: []
            ),
            expected: secondService
        )

        let completion = try unwrapCompletion(
            controller.didDiscoverCharacteristics(
                connection: connection,
                service: secondService,
                characteristics: []
            )
        )
        let database = completion.database
        XCTAssertEqual(database.connectionId, command.connectionId)
        XCTAssertEqual(database.gattSessionId, command.gattSessionId)
        XCTAssertEqual(database.services.count, 2)
        XCTAssertEqual(database.services.map(\.uuid), [duplicateServiceUuid, duplicateServiceUuid])
        XCTAssertEqual(database.services.map(\.id.value), [1, 2])
        XCTAssertEqual(database.services.map(\.primary), [true, false])
        XCTAssertEqual(database.services[0].characteristics.map(\.id.value), [3, 4])
        XCTAssertEqual(
            database.services[0].characteristics.map(\.uuid),
            [duplicateCharacteristicUuid, duplicateCharacteristicUuid]
        )
        XCTAssertEqual(
            database.services[0].characteristics[0].descriptors.map(\.id.value),
            [5, 6]
        )
        XCTAssertEqual(
            database.services[0].characteristics[0].descriptors.map(\.uuid),
            [duplicateDescriptorUuid, duplicateDescriptorUuid]
        )
        XCTAssertEqual(completion.nativeMap.services.count, 2)
        XCTAssertEqual(completion.nativeMap.characteristics.count, 2)
        XCTAssertEqual(completion.nativeMap.descriptors.count, 2)
        XCTAssertEqual(controller.pendingCount, 0)
        XCTAssertEqual(controller.activeCount, 1)
        XCTAssertEqual(controller.activeDatabase(connection: connection), database)
    }

    func testCharacteristicResolverUsesActiveSessionAndExactNativeInstance() throws {
        let controller = AppleGattController()
        let connection = connectionCorrelation(generation: 2)
        let command = discoveryCommand(commandId: 21, sessionId: 22)
        let service = FakeService(uuid: uuid("180d"), primary: true)
        let characteristic = FakeCharacteristic(
            uuid: uuid("2a37"),
            properties: properties(readable: true)
        )
        _ = try controller.begin(command: command, connection: connection)
        _ = try controller.didDiscoverServices(connection: connection, services: [service])
        _ = try controller.didDiscoverCharacteristics(
            connection: connection,
            service: service,
            characteristics: [characteristic]
        )
        let completion = try unwrapCompletion(
            controller.didDiscoverDescriptors(
                connection: connection,
                characteristic: characteristic,
                descriptors: []
            )
        )
        let characteristicId = completion.database.services[0].characteristics[0].id

        let target = try controller.resolveCharacteristic(
            connection: connection,
            gattSessionId: command.gattSessionId,
            characteristicId: characteristicId
        )
        XCTAssertEqual(target.connectionGeneration, connection.generation)
        XCTAssertEqual(target.gattSessionId, command.gattSessionId)
        XCTAssertEqual(target.characteristicId, characteristicId)
        XCTAssertEqual(
            ObjectIdentifier(target.characteristic),
            ObjectIdentifier(characteristic)
        )

        XCTAssertThrowsError(
            try controller.resolveCharacteristic(
                connection: connection,
                gattSessionId: GattSessionId(value: 23),
                characteristicId: characteristicId
            )
        ) { error in
            guard case CommandDispatchError.InvalidState = error else {
                return XCTFail("unexpected error: \(error)")
            }
        }
        XCTAssertThrowsError(
            try controller.resolveCharacteristic(
                connection: connection,
                gattSessionId: command.gattSessionId,
                characteristicId: GattCharacteristicId(value: 999)
            )
        ) { error in
            guard case CommandDispatchError.InvalidRequest = error else {
                return XCTFail("unexpected error: \(error)")
            }
        }

        _ = try controller.begin(
            command: discoveryCommand(commandId: 24, sessionId: 25),
            connection: connection
        )
        XCTAssertThrowsError(
            try controller.resolveCharacteristic(
                connection: connection,
                gattSessionId: command.gattSessionId,
                characteristicId: characteristicId
            )
        ) { error in
            guard case CommandDispatchError.InvalidState = error else {
                return XCTFail("unexpected error: \(error)")
            }
        }
    }

    func testFailureDropsOnlyPendingDraftAndPreservesCommittedSession() throws {
        let controller = AppleGattController()
        let connection = connectionCorrelation(generation: 2)
        let firstCommand = discoveryCommand(commandId: 30, sessionId: 40)
        _ = try controller.begin(command: firstCommand, connection: connection)
        let firstCompletion = try unwrapCompletion(
            controller.didDiscoverServices(connection: connection, services: [])
        )

        let secondCommand = discoveryCommand(commandId: 31, sessionId: 41)
        _ = try controller.begin(command: secondCommand, connection: connection)
        let service = FakeService(uuid: uuid("180d"), primary: true)
        let characteristic = FakeCharacteristic(
            uuid: uuid("2a37"),
            properties: properties(notifiable: true)
        )
        assertDiscoversCharacteristics(
            try controller.didDiscoverServices(connection: connection, services: [service]),
            expected: service
        )
        assertDiscoversDescriptors(
            try controller.didDiscoverCharacteristics(
                connection: connection,
                service: service,
                characteristics: [characteristic]
            ),
            expected: characteristic
        )

        XCTAssertEqual(
            controller.failDescriptors(connection: connection, characteristic: characteristic),
            secondCommand
        )
        XCTAssertEqual(controller.pendingCount, 0)
        XCTAssertEqual(controller.activeCount, 1)
        XCTAssertEqual(
            controller.activeDatabase(connection: connection),
            firstCompletion.database
        )
        XCTAssertNil(
            try controller.didDiscoverDescriptors(
                connection: connection,
                characteristic: characteristic,
                descriptors: []
            )
        )
    }

    func testConnectionGenerationAndNativeObjectIdentityRejectStaleCallbacks() throws {
        let controller = AppleGattController()
        let oldConnection = connectionCorrelation(generation: 3)
        let newConnection = connectionCorrelation(generation: 4)
        let command = discoveryCommand(commandId: 50, sessionId: 60)
        _ = try controller.begin(command: command, connection: newConnection)
        let expectedService = FakeService(uuid: uuid("180f"), primary: true)
        let wrongService = FakeService(uuid: expectedService.appleGattUuid, primary: true)

        XCTAssertNil(
            try controller.didDiscoverServices(connection: oldConnection, services: [wrongService])
        )
        assertDiscoversCharacteristics(
            try controller.didDiscoverServices(
                connection: newConnection,
                services: [expectedService]
            ),
            expected: expectedService
        )
        XCTAssertNil(
            try controller.didDiscoverCharacteristics(
                connection: newConnection,
                service: wrongService,
                characteristics: []
            )
        )
        XCTAssertNil(controller.failCharacteristics(connection: oldConnection, service: expectedService))
        XCTAssertEqual(controller.pendingCount, 1)
        XCTAssertEqual(controller.retire(connection: newConnection), command)
        XCTAssertEqual(controller.pendingCount, 0)
        XCTAssertNil(
            try controller.didDiscoverCharacteristics(
                connection: newConnection,
                service: expectedService,
                characteristics: []
            )
        )
    }

    func testServiceInvalidationQuarantinesPendingDiscoveryUntilConnectionRetires() throws {
        let controller = AppleGattController()
        let peripheral = FakeGattPeripheral(identifier: UUID())
        let connection = connectionCorrelation(generation: 5, peripheral: peripheral)
        let firstCommand = discoveryCommand(commandId: 70, sessionId: 80)
        _ = try controller.begin(command: firstCommand, connection: connection)

        XCTAssertEqual(controller.invalidateServices(connection: connection), firstCommand)
        XCTAssertEqual(controller.pendingCount, 0)
        XCTAssertEqual(controller.activeCount, 0)
        XCTAssertThrowsError(
            try controller.begin(
                command: discoveryCommand(commandId: 71, sessionId: 81),
                connection: connection
            )
        ) { error in
            guard case CommandDispatchError.InvalidState = error else {
                return XCTFail("unexpected error: \(error)")
            }
        }
        XCTAssertEqual(controller.pendingCount, 0)
        XCTAssertNil(
            try controller.didDiscoverServices(
                connection: connection,
                services: [FakeService(uuid: uuid("180d"), primary: true)]
            )
        )

        XCTAssertNil(controller.retire(connection: connection))
        let reconnected = connectionCorrelation(generation: 6, peripheral: peripheral)
        XCTAssertNoThrow(
            try controller.begin(
                command: discoveryCommand(commandId: 72, sessionId: 82),
                connection: reconnected
            )
        )
    }

    func testRetireClearsPendingAndActiveStateBeforeLateCallbacks() throws {
        let controller = AppleGattController()
        let connection = connectionCorrelation(generation: 7)
        let firstCommand = discoveryCommand(commandId: 90, sessionId: 100)
        _ = try controller.begin(command: firstCommand, connection: connection)
        _ = try unwrapCompletion(
            controller.didDiscoverServices(connection: connection, services: [])
        )
        let secondCommand = discoveryCommand(commandId: 91, sessionId: 101)
        _ = try controller.begin(command: secondCommand, connection: connection)

        XCTAssertEqual(controller.retire(connection: connection), secondCommand)
        XCTAssertEqual(controller.pendingCount, 0)
        XCTAssertEqual(controller.activeCount, 0)
        XCTAssertNil(controller.activeDatabase(connection: connection))
        XCTAssertNil(
            try controller.didDiscoverServices(
                connection: connection,
                services: [FakeService(uuid: uuid("180f"), primary: true)]
            )
        )
        XCTAssertNil(controller.failServices(connection: connection))
    }

    func testDrainClearsServiceInvalidationQuarantine() throws {
        let controller = AppleGattController()
        let connection = connectionCorrelation(generation: 8)
        let command = discoveryCommand(commandId: 110, sessionId: 120)
        _ = try controller.begin(command: command, connection: connection)
        XCTAssertEqual(controller.invalidateServices(connection: connection), command)

        XCTAssertEqual(controller.drain(), [])
        XCTAssertNoThrow(
            try controller.begin(
                command: discoveryCommand(commandId: 111, sessionId: 121),
                connection: connection
            )
        )
    }

    func testServiceDiscoveryAllocationGuardAcceptsBoundaryAndRejectsOneOver() throws {
        let maximum = AppleGattController.maximumDiscoveredServices
        let connection = connectionCorrelation(generation: 9)
        let acceptedController = AppleGattController()
        let acceptedCommand = discoveryCommand(commandId: 130, sessionId: 140)
        _ = try acceptedController.begin(command: acceptedCommand, connection: connection)
        let boundaryServices = (0..<maximum).map { _ in
            FakeService(uuid: uuid("180d"), primary: true)
        }
        assertDiscoversCharacteristics(
            try acceptedController.didDiscoverServices(
                connection: connection,
                services: boundaryServices
            ),
            expected: boundaryServices[0]
        )
        XCTAssertEqual(acceptedController.retire(connection: connection), acceptedCommand)

        let rejectedController = AppleGattController()
        let rejectedCommand = discoveryCommand(commandId: 131, sessionId: 141)
        _ = try rejectedController.begin(command: rejectedCommand, connection: connection)
        let oversizedServices = boundaryServices + [
            FakeService(uuid: uuid("180f"), primary: false),
        ]
        XCTAssertThrowsError(
            try rejectedController.didDiscoverServices(
                connection: connection,
                services: oversizedServices
            )
        ) {
            self.assertDiscoveryLimitError(
                $0,
                collection: .services,
                maximum: maximum,
                actual: maximum + 1
            )
        }
        XCTAssertNil(rejectedController.activeDatabase(connection: connection))
        XCTAssertEqual(rejectedController.failCurrent(connection: connection), rejectedCommand)
    }

    func testCharacteristicDiscoveryAllocationGuardAcceptsBoundaryAndRejectsOneOver() throws {
        let maximum = AppleGattController.maximumDiscoveredCharacteristics
        let connection = connectionCorrelation(generation: 10)
        let service = FakeService(uuid: uuid("180d"), primary: true)
        let boundaryCharacteristics = (0..<maximum).map { _ in
            FakeCharacteristic(uuid: uuid("2a37"), properties: properties(readable: true))
        }

        let acceptedController = AppleGattController()
        let acceptedCommand = discoveryCommand(commandId: 150, sessionId: 160)
        _ = try acceptedController.begin(command: acceptedCommand, connection: connection)
        _ = try acceptedController.didDiscoverServices(connection: connection, services: [service])
        assertDiscoversDescriptors(
            try acceptedController.didDiscoverCharacteristics(
                connection: connection,
                service: service,
                characteristics: boundaryCharacteristics
            ),
            expected: boundaryCharacteristics[0]
        )
        XCTAssertEqual(acceptedController.retire(connection: connection), acceptedCommand)

        let rejectedController = AppleGattController()
        let rejectedCommand = discoveryCommand(commandId: 151, sessionId: 161)
        _ = try rejectedController.begin(command: rejectedCommand, connection: connection)
        _ = try rejectedController.didDiscoverServices(connection: connection, services: [service])
        XCTAssertThrowsError(
            try rejectedController.didDiscoverCharacteristics(
                connection: connection,
                service: service,
                characteristics: boundaryCharacteristics + [
                    FakeCharacteristic(uuid: uuid("2a38"), properties: properties())
                ]
            )
        ) {
            self.assertDiscoveryLimitError(
                $0,
                collection: .characteristics,
                maximum: maximum,
                actual: maximum + 1
            )
        }
        XCTAssertNil(rejectedController.activeDatabase(connection: connection))
        XCTAssertEqual(rejectedController.failCurrent(connection: connection), rejectedCommand)
    }

    func testDescriptorDiscoveryAllocationGuardPreservesCommittedSessionOneOver() throws {
        let maximum = AppleGattController.maximumDiscoveredDescriptors
        let connection = connectionCorrelation(generation: 11)
        let service = FakeService(uuid: uuid("180d"), primary: true)
        let characteristic = FakeCharacteristic(
            uuid: uuid("2a37"),
            properties: properties(readable: true)
        )
        let boundaryDescriptors = (0..<maximum).map { _ in
            FakeDescriptor(uuid: uuid("2902"))
        }
        let controller = AppleGattController()
        let acceptedCommand = discoveryCommand(commandId: 170, sessionId: 180)
        _ = try controller.begin(command: acceptedCommand, connection: connection)
        _ = try controller.didDiscoverServices(connection: connection, services: [service])
        _ = try controller.didDiscoverCharacteristics(
            connection: connection,
            service: service,
            characteristics: [characteristic]
        )
        let completion = try unwrapCompletion(
            controller.didDiscoverDescriptors(
                connection: connection,
                characteristic: characteristic,
                descriptors: boundaryDescriptors
            )
        )
        XCTAssertEqual(
            completion.database.services[0].characteristics[0].descriptors.count,
            maximum
        )

        let rejectedCommand = discoveryCommand(commandId: 171, sessionId: 181)
        _ = try controller.begin(command: rejectedCommand, connection: connection)
        _ = try controller.didDiscoverServices(connection: connection, services: [service])
        _ = try controller.didDiscoverCharacteristics(
            connection: connection,
            service: service,
            characteristics: [characteristic]
        )
        XCTAssertThrowsError(
            try controller.didDiscoverDescriptors(
                connection: connection,
                characteristic: characteristic,
                descriptors: boundaryDescriptors + [FakeDescriptor(uuid: uuid("2901"))]
            )
        ) {
            self.assertDiscoveryLimitError(
                $0,
                collection: .descriptors,
                maximum: maximum,
                actual: maximum + 1
            )
        }
        XCTAssertEqual(controller.activeDatabase(connection: connection), completion.database)
        XCTAssertEqual(controller.failCurrent(connection: connection), rejectedCommand)
    }

    func testSessionCollisionAndPropertyMappingBoundaries() throws {
        let controller = AppleGattController()
        let connection = connectionCorrelation(generation: 5)
        let command = discoveryCommand(commandId: 70, sessionId: 80)
        _ = try controller.begin(command: command, connection: connection)
        _ = try unwrapCompletion(
            controller.didDiscoverServices(connection: connection, services: [])
        )

        XCTAssertThrowsError(try controller.begin(command: command, connection: connection)) {
            guard case CommandDispatchError.InvalidRequest = $0 else {
                return XCTFail("unexpected error: \($0)")
            }
        }

        let mapped = AppleGattMapper.properties([
            .read,
            .write,
            .writeWithoutResponse,
            .notify,
            .indicate,
            .broadcast,
        ])
        XCTAssertEqual(
            mapped,
            CharacteristicProperties(
                readable: true,
                writableWithResponse: true,
                writableWithoutResponse: true,
                notifiable: true,
                indicatable: true
            )
        )
    }

    private func assertDiscoveryLimitError(
        _ error: Error,
        collection: AppleGattDiscoveryCollection,
        maximum: Int,
        actual: Int,
        file: StaticString = #filePath,
        line: UInt = #line
    ) {
        guard case AppleGattControllerError.discoveryLimitExceeded(
            let observedCollection,
            let observedMaximum,
            let observedActual
        ) = error else {
            return XCTFail("unexpected error: \(error)", file: file, line: line)
        }
        XCTAssertEqual(observedCollection, collection, file: file, line: line)
        XCTAssertEqual(observedMaximum, maximum, file: file, line: line)
        XCTAssertEqual(observedActual, actual, file: file, line: line)
    }

    private func connectionCorrelation(
        generation: UInt64,
        peripheral: FakeGattPeripheral = FakeGattPeripheral(
            identifier: UUID(uuidString: "00000000-0000-0000-0000-000000000004")!
        )
    ) -> AppleConnectionCorrelation {
        AppleConnectionCorrelation(
            generation: generation,
            connectCommandId: CommandId(value: 1),
            connectionId: ConnectionId(value: 2),
            peripheralId: PeripheralId(value: 3),
            peripheral: peripheral
        )
    }

    private func discoveryCommand(commandId: UInt64, sessionId: UInt64) -> DiscoverGattCommand {
        DiscoverGattCommand(
            commandId: CommandId(value: commandId),
            connectionId: ConnectionId(value: 2),
            gattSessionId: GattSessionId(value: sessionId)
        )
    }

    private func uuid(_ value: String) -> BluetoothUuid {
        AppleAdvertisementMapper.canonicalUuid(CBUUID(string: value))
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

    private func assertDiscoversCharacteristics(
        _ action: AppleGattDiscoveryAction?,
        expected: any AppleGattServiceObject,
        file: StaticString = #filePath,
        line: UInt = #line
    ) {
        guard case .discoverCharacteristics(let observed) = action else {
            return XCTFail("expected characteristic discovery action", file: file, line: line)
        }
        XCTAssertEqual(ObjectIdentifier(observed), ObjectIdentifier(expected), file: file, line: line)
    }

    private func assertDiscoversDescriptors(
        _ action: AppleGattDiscoveryAction?,
        expected: any AppleGattCharacteristicObject,
        file: StaticString = #filePath,
        line: UInt = #line
    ) {
        guard case .discoverDescriptors(let observed) = action else {
            return XCTFail("expected descriptor discovery action", file: file, line: line)
        }
        XCTAssertEqual(ObjectIdentifier(observed), ObjectIdentifier(expected), file: file, line: line)
    }

    private func unwrapCompletion(
        _ action: AppleGattDiscoveryAction?,
        file: StaticString = #filePath,
        line: UInt = #line
    ) throws -> AppleGattDiscoveryCompletion {
        guard case .completed(let completion) = action else {
            XCTFail("expected completed discovery action", file: file, line: line)
            throw CocoaError(.coderInvalidValue)
        }
        return completion
    }
}

private final class FakeGattPeripheral: ApplePeripheralObject {
    let identifier: UUID

    init(identifier: UUID) {
        self.identifier = identifier
    }
}

private final class FakeService: AppleGattServiceObject {
    let appleGattUuid: BluetoothUuid
    let appleGattIsPrimary: Bool

    init(uuid: BluetoothUuid, primary: Bool) {
        appleGattUuid = uuid
        appleGattIsPrimary = primary
    }
}

private final class FakeCharacteristic: AppleGattCharacteristicObject {
    let appleGattUuid: BluetoothUuid
    let appleGattProperties: CharacteristicProperties

    init(uuid: BluetoothUuid, properties: CharacteristicProperties) {
        appleGattUuid = uuid
        appleGattProperties = properties
    }
}

private final class FakeDescriptor: AppleGattDescriptorObject {
    let appleGattUuid: BluetoothUuid

    init(uuid: BluetoothUuid) {
        appleGattUuid = uuid
    }
}
