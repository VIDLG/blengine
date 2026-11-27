import BleHubUniFfi
import CoreBluetooth
import Foundation
import XCTest
@testable import BleHubApple

final class ScanControllerTests: XCTestCase {
    func testCanonicalizesBluetoothUuidWidths() {
        XCTAssertEqual(
            AppleAdvertisementMapper.canonicalUuid(CBUUID(string: "FFF0")).value,
            "0000fff0-0000-1000-8000-00805f9b34fb"
        )
        XCTAssertEqual(
            AppleAdvertisementMapper.canonicalUuid(CBUUID(string: "12345678")).value,
            "12345678-0000-1000-8000-00805f9b34fb"
        )
        XCTAssertEqual(
            AppleAdvertisementMapper.canonicalUuid(
                CBUUID(string: "12345678-9ABC-DEF0-1234-56789ABCDEF0")
            ).value,
            "12345678-9abc-def0-1234-56789abcdef0"
        )
    }

    func testMapsCoreBluetoothAdvertisementFields() {
        let observation = AppleAdvertisementMapper.observation(
            scanSessionId: ScanSessionId(value: 10),
            peripheralId: PeripheralId(value: 20),
            advertisementData: [
                CBAdvertisementDataLocalNameKey: "BleHub Fixture",
                CBAdvertisementDataServiceUUIDsKey: [CBUUID(string: "FFF0")],
                CBAdvertisementDataOverflowServiceUUIDsKey: [
                    CBUUID(string: "FFF0"),
                    CBUUID(string: "12345678"),
                ],
                CBAdvertisementDataManufacturerDataKey: Data([0x34, 0x12, 0xAA, 0xBB]),
                CBAdvertisementDataServiceDataKey: [
                    CBUUID(string: "180F"): Data([0x64]),
                ],
                CBAdvertisementDataIsConnectable: NSNumber(value: true),
            ],
            rssi: NSNumber(value: -61)
        )

        XCTAssertEqual(observation.scanSessionId, ScanSessionId(value: 10))
        XCTAssertEqual(observation.peripheralId, PeripheralId(value: 20))
        XCTAssertEqual(observation.localName, "BleHub Fixture")
        XCTAssertEqual(
            observation.advertisedServices.map(\.value),
            [
                "0000fff0-0000-1000-8000-00805f9b34fb",
                "12345678-0000-1000-8000-00805f9b34fb",
            ]
        )
        XCTAssertEqual(
            observation.manufacturerData,
            [ManufacturerData(companyId: 0x1234, payload: Data([0xAA, 0xBB]))]
        )
        XCTAssertEqual(
            observation.serviceData,
            [
                ServiceData(
                    serviceUuid: BluetoothUuid(
                        value: "0000180f-0000-1000-8000-00805f9b34fb"
                    ),
                    payload: Data([0x64])
                ),
            ]
        )
        XCTAssertEqual(observation.rssi, -61)
        XCTAssertEqual(observation.connectable, true)
    }

    func testScanPredicateRequiresEveryConfiguredField() {
        let observation = AppleAdvertisementMapper.observation(
            scanSessionId: ScanSessionId(value: 1),
            peripheralId: PeripheralId(value: 2),
            advertisementData: [
                CBAdvertisementDataLocalNameKey: "BleHub Fixture",
                CBAdvertisementDataServiceUUIDsKey: [CBUUID(string: "FFF0")],
                CBAdvertisementDataManufacturerDataKey: Data([0x34, 0x12]),
            ],
            rssi: NSNumber(value: -50)
        )
        let matching = ScanOptions(serviceUuids: [
                BluetoothUuid(value: "0000fff0-0000-1000-8000-00805f9b34fb"),
            ], namePrefix: "BleHub", exactName: nil, companyId: 0x1234, minRssi: nil, manufacturerData: nil, android: nil, allowDuplicates: true)

        XCTAssertTrue(matching.matches(observation))
        XCTAssertFalse(
            ScanOptions(serviceUuids: matching.serviceUuids, namePrefix: "Other", exactName: nil, companyId: matching.companyId, minRssi: nil, manufacturerData: nil, android: nil, allowDuplicates: false).matches(observation)
        )
        XCTAssertFalse(
            ScanOptions(serviceUuids: [], namePrefix: nil, exactName: nil, companyId: 0xFFFF, minRssi: nil, manufacturerData: nil, android: nil, allowDuplicates: false).matches(observation)
        )
    }

    func testNativeServiceFilterDoesNotFabricateAdvertisementServices() throws {
        let controller = AppleScanController()
        let serviceUuid = BluetoothUuid(
            value: "0000fff0-0000-1000-8000-00805f9b34fb"
        )
        _ = try controller.begin(
            command: StartScanCommand(
                commandId: CommandId(value: 1),
                scanSessionId: ScanSessionId(value: 10),
                options: ScanOptions(serviceUuids: [serviceUuid], namePrefix: nil, exactName: nil, companyId: nil, minRssi: nil, manufacturerData: nil, android: nil, allowDuplicates: true)
            )
        )

        let observation = try XCTUnwrap(
            controller.observation(
                peripheralIdentifier: UUID(),
                advertisementData: [
                    CBAdvertisementDataIsConnectable: NSNumber(value: true),
                ],
                rssi: NSNumber(value: -70),
                retainPeripheral: { _ in }
            )
        )
        XCTAssertTrue(observation.advertisedServices.isEmpty)
        XCTAssertTrue(ScanOptions(serviceUuids: [serviceUuid], namePrefix: nil, exactName: nil, companyId: nil, minRssi: nil, manufacturerData: nil, android: nil, allowDuplicates: true).matches(observation, nativeServiceFilterMatched: true))
    }

    func testMultipleNativeFiltersDoNotClaimWhichUuidMatched() {
        let serviceA = BluetoothUuid(value: "0000fff0-0000-1000-8000-00805f9b34fb")
        let serviceB = BluetoothUuid(value: "0000ffe0-0000-1000-8000-00805f9b34fb")
        let observation = AppleAdvertisementMapper.observation(
            scanSessionId: ScanSessionId(value: 1),
            peripheralId: PeripheralId(value: 2),
            advertisementData: [
                CBAdvertisementDataServiceUUIDsKey: [CBUUID(string: "FFF0")],
            ],
            rssi: NSNumber(value: -50)
        )

        XCTAssertEqual(observation.advertisedServices, [serviceA])
        XCTAssertFalse(observation.advertisedServices.contains(serviceB))
        XCTAssertTrue(ScanOptions(serviceUuids: [serviceA, serviceB], namePrefix: nil, exactName: nil, companyId: nil, minRssi: nil, manufacturerData: nil, android: nil, allowDuplicates: true).matches(observation, nativeServiceFilterMatched: true))
    }

    func testNativeServiceMatchDoesNotBypassMissingIdentityFields() {
        let serviceA = BluetoothUuid(value: "0000fff0-0000-1000-8000-00805f9b34fb")
        let observation = AppleAdvertisementMapper.observation(
            scanSessionId: ScanSessionId(value: 1),
            peripheralId: PeripheralId(value: 2),
            advertisementData: [:],
            rssi: NSNumber(value: -50)
        )

        XCTAssertFalse(ScanOptions(serviceUuids: [serviceA], namePrefix: "BleHub", exactName: nil, companyId: nil, minRssi: nil, manufacturerData: nil, android: nil, allowDuplicates: true).matches(observation, nativeServiceFilterMatched: true))
    }

    func testRejectedLocalFiltersDoNotAllocateOrRetainPeripheralIdentity() throws {
        let controller = AppleScanController()
        let command = StartScanCommand(
            commandId: CommandId(value: 1),
            scanSessionId: ScanSessionId(value: 100),
            options: ScanOptions(serviceUuids: [
                    BluetoothUuid(value: "0000fff0-0000-1000-8000-00805f9b34fb"),
                ], namePrefix: "BleHub", exactName: nil, companyId: 0x1234, minRssi: nil, manufacturerData: nil, android: nil, allowDuplicates: true)
        )
        _ = try controller.begin(command: command)

        let matchingAdvertisement: [String: Any] = [
            CBAdvertisementDataLocalNameKey: "BleHub Fixture",
            CBAdvertisementDataServiceUUIDsKey: [CBUUID(string: "FFF0")],
            CBAdvertisementDataManufacturerDataKey: Data([0x34, 0x12]),
        ]
        var retainedIds: [PeripheralId] = []

        XCTAssertNil(
            controller.observation(
                peripheralIdentifier: UUID(),
                advertisementData: matchingAdvertisement.merging([
                    CBAdvertisementDataLocalNameKey: "Other Fixture",
                ]) { _, replacement in replacement },
                rssi: NSNumber(value: -50),
                retainPeripheral: { retainedIds.append($0) }
            )
        )
        XCTAssertNil(
            controller.observation(
                peripheralIdentifier: UUID(),
                advertisementData: matchingAdvertisement.merging([
                    CBAdvertisementDataManufacturerDataKey: Data([0xFF, 0xFF]),
                ]) { _, replacement in replacement },
                rssi: NSNumber(value: -50),
                retainPeripheral: { retainedIds.append($0) }
            )
        )
        XCTAssertTrue(retainedIds.isEmpty)

        let acceptedIdentifier = UUID()
        let firstObservation = try XCTUnwrap(
            controller.observation(
                peripheralIdentifier: acceptedIdentifier,
                advertisementData: matchingAdvertisement,
                rssi: NSNumber(value: -50),
                retainPeripheral: { retainedIds.append($0) }
            )
        )
        let duplicateObservation = try XCTUnwrap(
            controller.observation(
                peripheralIdentifier: acceptedIdentifier,
                advertisementData: matchingAdvertisement,
                rssi: NSNumber(value: -49),
                retainPeripheral: { retainedIds.append($0) }
            )
        )

        XCTAssertEqual(firstObservation.peripheralId, PeripheralId(value: 0))
        XCTAssertEqual(duplicateObservation.peripheralId, firstObservation.peripheralId)
        XCTAssertEqual(retainedIds, [firstObservation.peripheralId, firstObservation.peripheralId])
    }

    func testPeripheralIdAllocatorExhaustionDoesNotReuseIds() throws {
        let defaultController = AppleScanController()
        let firstIdentifier = UUID()
        XCTAssertNil(defaultController.existingPeripheralId(for: firstIdentifier))
        XCTAssertEqual(
            defaultController.peripheralId(for: firstIdentifier),
            PeripheralId(value: 0)
        )
        XCTAssertEqual(
            defaultController.existingPeripheralId(for: firstIdentifier),
            PeripheralId(value: 0)
        )

        let boundaryController = AppleScanController(initialPeripheralId: UInt64.max)
        let lastIdentifier = UUID()
        let lastId = try XCTUnwrap(boundaryController.peripheralId(for: lastIdentifier))
        XCTAssertEqual(lastId, PeripheralId(value: UInt64.max))
        XCTAssertNil(boundaryController.peripheralId(for: UUID()))
        XCTAssertEqual(boundaryController.peripheralId(for: lastIdentifier), lastId)
    }

    func testScanLifecycleAndPeripheralIdentityPersistAcrossSessions() throws {
        let controller = AppleScanController()
        let first = startCommand(commandId: 1, scanSessionId: 100)
        let second = startCommand(commandId: 3, scanSessionId: 200)
        let identifier = UUID(uuidString: "12345678-9ABC-DEF0-1234-56789ABCDEF0")!

        let services = try controller.begin(command: first)
        XCTAssertEqual(
            services?.map(\.uuidString),
            ["0000FFF0-0000-1000-8000-00805F9B34FB"]
        )
        XCTAssertEqual(controller.activeSessionId, first.scanSessionId)
        XCTAssertThrowsError(try controller.begin(command: second))

        let firstPeripheralId = try XCTUnwrap(controller.peripheralId(for: identifier))
        XCTAssertEqual(firstPeripheralId, controller.peripheralId(for: identifier))
        XCTAssertEqual(controller.diagnosticPeerCacheCount, 1)
        XCTAssertThrowsError(
            try controller.stop(
                command: StopScanCommand(
                    commandId: CommandId(value: 2),
                    scanSessionId: ScanSessionId(value: 999)
                )
            )
        )

        let stop = StopScanCommand(
            commandId: CommandId(value: 2),
            scanSessionId: first.scanSessionId
        )
        XCTAssertEqual(try controller.stop(command: stop), .stopped(first.scanSessionId))
        XCTAssertEqual(try controller.stop(command: stop), .alreadyStopped)
        XCTAssertNil(controller.activeSessionId)
        XCTAssertEqual(controller.diagnosticPeerCacheCount, 1, "stopping does not erase warm identity")

        _ = try controller.begin(command: second)
        XCTAssertEqual(firstPeripheralId, controller.peripheralId(for: identifier))
        XCTAssertEqual(controller.diagnosticPeerCacheCount, 1)
        controller.dispose()
        XCTAssertNil(controller.activeSessionId)
        XCTAssertEqual(controller.diagnosticPeerCacheCount, 0)
    }

    private func startCommand(
        commandId: UInt64,
        scanSessionId: UInt64
    ) -> StartScanCommand {
        StartScanCommand(
            commandId: CommandId(value: commandId),
            scanSessionId: ScanSessionId(value: scanSessionId),
            options: ScanOptions(serviceUuids: [BluetoothUuid(value: "0000fff0-0000-1000-8000-00805f9b34fb")], namePrefix: nil, exactName: nil, companyId: nil, minRssi: nil, manufacturerData: nil, android: nil, allowDuplicates: true)
        )
    }
}
