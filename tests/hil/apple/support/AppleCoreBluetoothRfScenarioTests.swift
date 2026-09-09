import BleHubUniFfi
import XCTest

final class AppleCoreBluetoothRfScenarioTests: XCTestCase {
    private let serviceA = BluetoothUuid(value: "0000fff0-0000-1000-8000-00805f9b34fb")
    private let serviceB = BluetoothUuid(value: "0000ffe0-0000-1000-8000-00805f9b34fb")

    func testSingleProviderFilterAcceptsHiddenServicesOnlyInItsSession() throws {
        let scan = command(services: [serviceA])
        let foreign = advertisement(session: 99, peripheral: 1, services: [serviceA])
        let hidden = advertisement(session: 10, peripheral: 2, services: [])
        XCTAssertNil(matchingAdvertisement(
            scanCommand: scan, serviceUuid: serviceA, in: [.advertisementObserved(advertisement: foreign)]
        ))
        let selected = try XCTUnwrap(matchingAdvertisement(
            scanCommand: scan, serviceUuid: serviceA,
            in: [.advertisementObserved(advertisement: foreign), .advertisementObserved(advertisement: hidden)]
        ))
        XCTAssertEqual(selected, hidden)
        XCTAssertTrue(selected.advertisedServices.isEmpty)
    }

    func testUnfilteredAndMultipleFiltersRequireActualProviderEvidence() {
        let hidden = advertisement(session: 10, peripheral: 1, services: [])
        let other = advertisement(session: 10, peripheral: 2, services: [serviceB])
        let provider = advertisement(session: 10, peripheral: 3, services: [serviceA])
        for services in [[], [serviceA, serviceB], [serviceB]] {
            let scan = command(services: services)
            XCTAssertNil(matchingAdvertisement(
                scanCommand: scan, serviceUuid: serviceA,
                in: [.advertisementObserved(advertisement: hidden), .advertisementObserved(advertisement: other)]
            ))
            XCTAssertEqual(matchingAdvertisement(
                scanCommand: scan, serviceUuid: serviceA,
                in: [.advertisementObserved(advertisement: other), .advertisementObserved(advertisement: provider)]
            ), provider)
        }
        // [A, B] matching A cannot establish that B was advertised or matched.
        XCTAssertNil(matchingAdvertisement(
            scanCommand: command(services: [serviceA, serviceB]), serviceUuid: serviceB,
            in: [.advertisementObserved(advertisement: provider)]
        ))
    }

    func testExplicitDiagnosticTargetCannotFallBackToAnotherPeripheral() {
        let targetId = PeripheralId(value: 2)
        let stranger = advertisement(session: 10, peripheral: 1, services: [serviceA])
        let wrongSession = advertisement(session: 99, peripheral: 2, services: [])
        let target = advertisement(session: 10, peripheral: 2, services: [])
        for services in [[], [serviceA]] {
            let scan = command(services: services)
            XCTAssertNil(matchingAdvertisement(
                scanCommand: scan, serviceUuid: serviceA, targetPeripheralId: targetId,
                in: [.advertisementObserved(advertisement: stranger), .advertisementObserved(advertisement: wrongSession)]
            ))
            XCTAssertEqual(matchingAdvertisement(
                scanCommand: scan, serviceUuid: serviceA, targetPeripheralId: targetId,
                in: [.advertisementObserved(advertisement: stranger), .advertisementObserved(advertisement: target)]
            ), target)
        }
    }

    func testPostStopSelectionStillDetectsHiddenProviderAdvertisements() {
        let scan = command(services: [serviceA])
        let hidden = advertisement(session: 10, peripheral: 2, services: [])
        // runScan supplies only events after the stop boundary to this helper.
        XCTAssertNotNil(matchingAdvertisement(
            scanCommand: scan, serviceUuid: serviceA, in: [.advertisementObserved(advertisement: hidden)]
        ))
        XCTAssertNil(matchingAdvertisement(scanCommand: scan, serviceUuid: serviceA, in: []))
    }

    private func command(services: [BluetoothUuid]) -> StartScanCommand {
        StartScanCommand(
            commandId: CommandId(value: 2), scanSessionId: ScanSessionId(value: 10),
            options: ScanOptions(
                serviceUuids: services, namePrefix: nil, companyId: nil, allowDuplicates: true
            )
        )
    }

    private func advertisement(
        session: UInt64, peripheral: UInt64, services: [BluetoothUuid]
    ) -> AdvertisementObservation {
        AdvertisementObservation(
            scanSessionId: ScanSessionId(value: session), peripheralId: PeripheralId(value: peripheral),
            localName: nil, advertisedServices: services, manufacturerData: [], serviceData: [],
            rssi: nil, connectable: nil
        )
    }
}
