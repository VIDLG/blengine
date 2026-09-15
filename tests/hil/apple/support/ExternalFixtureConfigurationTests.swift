import BleHubUniFfi
import XCTest

final class ExternalFixtureConfigurationTests: XCTestCase {
    private let service = BluetoothUuid(value: "fd000000-0000-4000-8000-000000000000")
    private let external = RfFixtureConfiguration(
        profileId: "external-basic-v1", serviceUuid: "fd000000-0000-4000-8000-000000000000",
        hasIndicationOnly: false, initialValueHex: "00", notificationValueHex: "ef", writeWithResponseHex: "10", writeWithoutResponseHex: "ab"
    )

    func testFourAndFiveCharacteristicContractsRemainStrictAndSeparate() throws {
        let command = DiscoverGattCommand(commandId: CommandId(value: 5), connectionId: ConnectionId(value: 1),
                                          gattSessionId: GattSessionId(value: 1))
        func validate(_ database: GattDatabase, _ config: RfFixtureConfiguration) throws {
            try validateAndroidGattDatabase(database, command: command, expectedServiceUuid: service, configuration: config)
        }
        try validate(database(indicationOnly: false), external)
        try validate(database(indicationOnly: true), .android)
        XCTAssertThrowsError(try validate(database(indicationOnly: false), .android))
        XCTAssertThrowsError(try validate(database(indicationOnly: true), external))
        for config in [external, .android] {
            XCTAssertThrowsError(try validate(database(indicationOnly: config.hasIndicationOnly, cccd: false), config))
            var invalid = database(indicationOnly: config.hasIndicationOnly)
            invalid.services[0].characteristics[0].properties.writableWithoutResponse = false
            XCTAssertThrowsError(try validate(invalid, config))
            invalid = database(indicationOnly: config.hasIndicationOnly)
            invalid.services[0].characteristics[3] = invalid.services[0].characteristics[2]
            XCTAssertThrowsError(try validate(invalid, config))
        }
    }

    func testExactOneByteReadbackCannotPassAndroidOrWrongValueLedger() throws {
        let db = database(indicationOnly: false)
        func events(_ values: [UInt64: Data]) -> [BleEvent] {
            (UInt64(6) ... 10).flatMap { id -> [BleEvent] in
                let command = CommandId(value: id)
                let domain: BleEvent
                if let value = values[id] {
                    domain = .characteristicRead(commandId: command, connectionId: db.connectionId,
                                                 gattSessionId: db.gattSessionId, characteristicId: GattCharacteristicId(value: 1), value: value)
                } else {
                    domain = .characteristicWritten(commandId: command, connectionId: db.connectionId,
                                                    gattSessionId: db.gattSessionId, characteristicId: GattCharacteristicId(value: 1))
                }
                return [domain, .commandCompleted(commandId: command, error: nil)]
            }
        }
        let oneByte = events([6: Data([0]), 8: Data([0x10]), 10: Data([0xAB])])
        func validate(_ events: [BleEvent], _ config: RfFixtureConfiguration) throws {
            try validateGattIoEventLedger(events, database: db, characteristicId: GattCharacteristicId(value: 1), configuration: config)
        }
        try validate(oneByte, external)
        XCTAssertThrowsError(try validate(oneByte, .android))
        let android = events([6: Data([0]), 8: Data([0x10, 0x20, 0x30]), 10: Data([0xAB, 0xCD])])
        try validate(android, .android)
        XCTAssertThrowsError(try validate(android, external))
        XCTAssertThrowsError(try validate(events([6: Data([0]), 8: Data([0]), 10: Data([0xAB])]), external))
        XCTAssertThrowsError(try validate(oneByte + [oneByte[0]], external))
    }

    func testAppTransportRejectsInvalidConfigInsteadOfFallingBack() throws {
        let document: [String: Any] = [
            "profileId": external.profileId, "serviceUuid": service.value, "hasIndicationOnly": false,
            "initialValueHex": "00", "notificationValueHex": "ef", "writeWithResponseHex": "10", "writeWithoutResponseHex": "ab",
        ]
        func parse(_ document: [String: Any], serviceUuid: String? = nil) throws -> RfFixtureConfiguration {
            let json = String(decoding: try JSONSerialization.data(withJSONObject: document), as: UTF8.self)
            return try .fromArguments(["probe", "--rf-fixture-config", json], serviceUuid: serviceUuid ?? service.value)
        }
        let parsed = try parse(document)
        XCTAssertFalse(parsed.hasIndicationOnly)
        XCTAssertEqual(try parsed.writeWithResponse, Data([0x10]))
        XCTAssertEqual(try parsed.writeWithoutResponse, Data([0xAB]))
        XCTAssertTrue(try RfFixtureConfiguration.fromArguments([], serviceUuid: nil).hasIndicationOnly)
        XCTAssertThrowsError(try parse(document, serviceUuid: RfFixtureConfiguration.android.serviceUuid))
        for badValue in ["", "0", "gg", "00", "ab", String(repeating: "10", count: 513)] {
            var bad = document
            bad["writeWithResponseHex"] = badValue
            XCTAssertThrowsError(try parse(bad))
        }
        XCTAssertThrowsError(try RfFixtureConfiguration.fromArguments(["--rf-fixture-config"], serviceUuid: service.value))
    }

    private func database(indicationOnly: Bool, cccd: Bool = true) -> GattDatabase {
        var characteristics: [GattCharacteristic] = []
        for number in 1 ... (indicationOnly ? 5 : 4) {
            let writable = number == 1 || number == 4
            let updates = number == 2 || number == 5
            let properties = CharacteristicProperties(
                readable: number != 4, writableWithResponse: writable,
                writableWithoutResponse: writable, notifiable: number == 2, indicatable: updates
            )
            var descriptors: [GattDescriptor] = []
            if cccd && updates {
                descriptors.append(GattDescriptor(
                    id: GattDescriptorId(value: UInt64(number)),
                    uuid: BluetoothUuid(value: "00002902-0000-1000-8000-00805f9b34fb")
                ))
            }
            characteristics.append(GattCharacteristic(
                id: GattCharacteristicId(value: UInt64(number)),
                uuid: BluetoothUuid(value: "fff\(number)0000-0000-4000-8000-000000000000"),
                properties: properties, descriptors: descriptors
            ))
        }
        return GattDatabase(connectionId: ConnectionId(value: 1), gattSessionId: GattSessionId(value: 1), services: [
            GattService(id: GattServiceId(value: 1), uuid: service, primary: true, characteristics: characteristics),
        ])
    }
}
