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

    func testDefaultIoLedgerStillRequiresAllFiveOperationsAndDisconnect() throws {
        let ids = RfCycleIds()
        let valid = cycleEvents(ids: ids, scenario: .io)
        try validateRfCycleLedger(valid, ids: ids, scenario: .io)
        XCTAssertThrowsError(try validateRfCycleLedger(valid.filter {
            rfCommandId($0) != ids.command(9)
        }, ids: ids, scenario: .io))
        XCTAssertThrowsError(try validateRfCycleLedger(valid + [valid[10]], ids: ids, scenario: .io))
    }

    func testEnableGateChecksEntireTupleBeforeSignallingHost() throws {
        let ids = RfCycleIds()
        let enable = BleEvent.notificationStateChanged(
            commandId: ids.command(6), connectionId: ids.connection, gattSessionId: ids.gatt,
            subscriptionId: ids.subscription, characteristicId: GattCharacteristicId(value: 2),
            state: .enabled(actualMode: .notification)
        )
        let terminal = BleEvent.commandCompleted(commandId: ids.command(6), error: nil)
        func validate(_ events: [BleEvent]) throws {
            try validateNotificationEventLedger(events, database: database(ids: ids), subscriptionId: ids.subscription,
                                                characteristicId: GattCharacteristicId(value: 2), expectedValue: Data([0xDE, 0xAD, 0xBE, 0xEF]),
                                                ids: ids, expectDisable: false, expectValue: false)
        }
        try validate([enable, terminal])
        let wrong = BleEvent.notificationStateChanged(
            commandId: ids.command(6), connectionId: ids.connection, gattSessionId: ids.gatt,
            subscriptionId: SubscriptionId(value: 99), characteristicId: GattCharacteristicId(value: 2),
            state: .enabled(actualMode: .notification)
        )
        for events in [[wrong, terminal], [enable, terminal, terminal], [terminal, enable],
                       [enable, terminal, notification(ids: ids)]] {
            XCTAssertThrowsError(try validate(events))
        }
    }

    func testRssiRequiresOneCorrelatedDomainBeforeOneSuccessfulTerminal() throws {
        let ids = RfCycleIds()
        let command = ReadRssiCommand(commandId: ids.command(12), connectionId: ids.connection)
        let domain = BleEvent.rssiRead(commandId: command.commandId, connectionId: ids.connection, rssi: -47)
        let terminal = BleEvent.commandCompleted(commandId: command.commandId, error: nil)
        XCTAssertEqual(try validateRssiLedger([domain, terminal], command: command), -47)
        for events in [[terminal], [terminal, domain], [domain, terminal, terminal], [domain, domain, terminal],
                       [.rssiRead(commandId: command.commandId, connectionId: ConnectionId(value: 99), rssi: -47), terminal],
                       [domain, .commandCompleted(commandId: command.commandId, error: failure(.gatt))]] {
            XCTAssertThrowsError(try validateRssiLedger(events, command: command))
        }
    }

    func testStrictIndicationRejectsDowngradeUnknownModeAndDualPropertySuccess() throws {
        let ids = RfCycleIds()
        let valid = cycleEvents(ids: ids, scenario: .indication)
        try validateRfCycleLedger(valid, ids: ids, scenario: .indication)
        for mode: NotificationMode? in [.notification, nil] {
            let downgraded = valid.map { event -> BleEvent in
                if case .notificationStateChanged(let command, let connection, let gatt, let sub, let char, .enabled) = event {
                    return .notificationStateChanged(commandId: command, connectionId: connection, gattSessionId: gatt,
                                                     subscriptionId: sub, characteristicId: char, state: .enabled(actualMode: mode))
                }
                return event
            }
            XCTAssertThrowsError(try validateRfCycleLedger(downgraded, ids: ids, scenario: .indication))
        }
        for error: BleEventError? in [nil, failure(.gatt)] {
            let wrongRejection = valid.map { event -> BleEvent in
                if case .commandCompleted(let command, _) = event, command == ids.command(13) {
                    return .commandCompleted(commandId: command, error: error)
                }
                return event
            }
            XCTAssertThrowsError(try validateRfCycleLedger(wrongRejection, ids: ids, scenario: .indication))
        }
        let spuriousState = BleEvent.notificationStateChanged(
            commandId: ids.command(13), connectionId: ids.connection, gattSessionId: ids.gatt,
            subscriptionId: ids.rejectedSubscription, characteristicId: GattCharacteristicId(value: 2),
            state: .enabled(actualMode: .notification)
        )
        XCTAssertThrowsError(try validateRfCycleLedger([spuriousState] + valid, ids: ids, scenario: .indication))
    }

    func testNotificationLedgerRejectsWrongTupleDuplicateAndOutOfWindowValues() throws {
        let ids = RfCycleIds()
        let valid = cycleEvents(ids: ids, scenario: .notification)
        try validateRfCycleLedger(valid, ids: ids, scenario: .notification)
        let valueIndex = try XCTUnwrap(valid.firstIndex { if case .notificationReceived = $0 { return true }; return false })
        for wrongValue in [
            notification(ids: ids, connection: 99), notification(ids: ids, gatt: 99),
            notification(ids: ids, subscription: 99), notification(ids: ids, characteristic: 99),
            notification(ids: ids, value: Data([0xCA, 0xFE, 0xBA, 0xBE])),
        ] {
            var wrong = valid
            wrong[valueIndex] = wrongValue
            XCTAssertThrowsError(try validateRfCycleLedger(wrong, ids: ids, scenario: .notification))
        }
        var duplicate = valid
        duplicate.insert(valid[valueIndex], at: valueIndex)
        XCTAssertThrowsError(try validateRfCycleLedger(duplicate, ids: ids, scenario: .notification))
        var late = valid
        let value = late.remove(at: valueIndex)
        late.append(value)
        XCTAssertThrowsError(try validateRfCycleLedger(late, ids: ids, scenario: .notification))
        var early = valid
        early.remove(at: valueIndex)
        early.insert(value, at: 0)
        XCTAssertThrowsError(try validateRfCycleLedger(early, ids: ids, scenario: .notification))
    }

    func testPassiveDisconnectRequiresActiveExactValueAndNoCommandIdThenSilence() throws {
        let ids = RfCycleIds()
        let valid = cycleEvents(ids: ids, scenario: .passiveDisconnect)
        try validateRfCycleLedger(valid, ids: ids, scenario: .passiveDisconnect)
        var graceful = valid
        graceful[graceful.count - 1] = .connectionStateChanged(commandId: nil, connectionId: ids.connection, result: .ok(state: .disconnected))
        try validateRfCycleLedger(graceful, ids: ids, scenario: .passiveDisconnect)
        for replacement in [
            BleEvent.connectionStateChanged(commandId: ids.command(8), connectionId: ids.connection, result: .err(error: failure(.connection))),
            .connectionStateChanged(commandId: nil, connectionId: ConnectionId(value: 99), result: .err(error: failure(.connection))),
            .connectionStateChanged(commandId: nil, connectionId: ids.connection, result: .err(error: failure(.gatt))),
        ] {
            var wrong = valid
            wrong[wrong.count - 1] = replacement
            XCTAssertThrowsError(try validateRfCycleLedger(wrong, ids: ids, scenario: .passiveDisconnect))
        }
        XCTAssertThrowsError(try validateRfCycleLedger(valid.filter {
            if case .notificationReceived = $0 { return false }; return true
        }, ids: ids, scenario: .passiveDisconnect))
        for late in [notification(ids: ids), valid.last!,
                     .rssiRead(commandId: ids.command(12), connectionId: ids.connection, rssi: -47)] {
            XCTAssertThrowsError(try validateRfCycleLedger(valid + [late], ids: ids, scenario: .passiveDisconnect))
        }
    }

    func testRecoveryRechecksAllThreeCyclesAndRejectsLateOldIds() throws {
        var history: [BleEvent] = []
        var cycles: [RfCycleEvidence] = []
        for cycle: UInt64 in 1 ... 3 {
            let ids = RfCycleIds(cycle: cycle)
            let start = history.count
            history += cycleEvents(ids: ids, scenario: .recovery)
            cycles.append(RfCycleEvidence(ids: ids, range: start ..< history.count))
            try validateRfHistory(history, cycles: cycles, scenario: .recovery)
        }
        let old = RfCycleIds()
        for late in [
            notification(ids: old), notification(ids: RfCycleIds(cycle: 3), gatt: 1),
            notification(ids: RfCycleIds(cycle: 3), subscription: 1),
            BleEvent.commandCompleted(commandId: old.command(6), error: nil),
            .rssiRead(commandId: CommandId(value: 999), connectionId: old.connection, rssi: -42),
            .advertisementObserved(advertisement: advertisement(session: 1, peripheral: 1, services: [])),
        ] {
            XCTAssertThrowsError(try validateRfHistory(history + [late], cycles: cycles, scenario: .recovery))
        }
        // Unknown future IDs in ACK/dispose gaps must not disappear between ranges.
        XCTAssertThrowsError(try validateRfHistory(history + [notification(ids: RfCycleIds(cycle: 99))],
                                                 cycles: cycles, scenario: .recovery))
        var gapHistory = history
        gapHistory.insert(notification(ids: RfCycleIds(cycle: 99)), at: cycles[1].range.lowerBound)
        let gapCycles = [cycles[0]] + cycles.dropFirst().map {
            RfCycleEvidence(ids: $0.ids, range: ($0.range.lowerBound + 1) ..< ($0.range.upperBound + 1))
        }
        XCTAssertThrowsError(try validateRfHistory(gapHistory, cycles: gapCycles, scenario: .recovery))
        // Reusing the first cycle's IDs is detected, even with otherwise valid events.
        let reused = history + cycleEvents(ids: old, scenario: .recovery)
        XCTAssertThrowsError(try validateRfHistory(reused, cycles: cycles, scenario: .recovery))
        try validateRfPostDispose(history, before: history)
        XCTAssertThrowsError(try validateRfPostDispose(history + [.backendError(error: failure(.gatt))], before: history))
        XCTAssertThrowsError(try validateRfPostDispose(Array(history.dropLast()), before: history))
    }

    func testCycleLedgerRejectsLateTerminalsWrongDomainsAndPostStopAdvertisements() throws {
        let ids = RfCycleIds()
        let valid = cycleEvents(ids: ids, scenario: .rssi)
        try validateRfCycleLedger(valid, ids: ids, scenario: .rssi)
        for extra in [valid[0], valid[1],
                      .gattDiscovered(commandId: ids.command(5), database: database(ids: RfCycleIds(cycle: 99))),
                      .advertisementObserved(advertisement: advertisement(session: 1, peripheral: 1, services: []))] {
            XCTAssertThrowsError(try validateRfCycleLedger(valid + [extra], ids: ids, scenario: .rssi))
        }
        var reordered = valid
        reordered.swapAt(0, 1)
        XCTAssertThrowsError(try validateRfCycleLedger(reordered, ids: ids, scenario: .rssi))
    }

    func testRunDirectoryRejectsReuseAndTraversalAndUsesRealMarkerFiles() throws {
        let parent = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: parent, withIntermediateDirectories: false)
        defer { try? FileManager.default.removeItem(at: parent) }
        let runId = UUID().uuidString
        let path = parent.appendingPathComponent("blehub-rf-\(runId)").path
        try prepareRfCoordinationDirectory(path: path, runId: runId)
        try signalProbeHost(path: path, marker: "notification-enabled", value: "\(runId)\n")
        try waitForProbeHost(path: path, marker: "notification-enabled", timeout: 0.1)
        XCTAssertEqual(try String(contentsOfFile: path + "/notification-enabled", encoding: .utf8), "\(runId)\n")
        XCTAssertThrowsError(try prepareRfCoordinationDirectory(path: path, runId: runId))
        XCTAssertThrowsError(try prepareRfCoordinationDirectory(path: path, runId: "../escape"))
        XCTAssertThrowsError(try waitForProbeHost(path: path, marker: "wrong-run", timeout: 0.01))
    }

    private func failure(_ category: BleErrorCategory) -> BleEventError {
        BleEventError(category: category, message: "test failure", platformDetails: nil)
    }

    private func database(ids: RfCycleIds) -> GattDatabase {
        GattDatabase(connectionId: ids.connection, gattSessionId: ids.gatt, services: [
            GattService(id: GattServiceId(value: 1), uuid: serviceA, primary: true, characteristics: [
                GattCharacteristic(id: GattCharacteristicId(value: 1), uuid: BluetoothUuid(value: "fff10000-0000-4000-8000-000000000000"),
                                   properties: CharacteristicProperties(readable: true, writableWithResponse: true, writableWithoutResponse: true,
                                                                        notifiable: false, indicatable: false), descriptors: []),
                GattCharacteristic(id: GattCharacteristicId(value: 2), uuid: BluetoothUuid(value: "fff20000-0000-4000-8000-000000000000"),
                                   properties: CharacteristicProperties(readable: true, writableWithResponse: false, writableWithoutResponse: false,
                                                                        notifiable: true, indicatable: true), descriptors: []),
                GattCharacteristic(id: GattCharacteristicId(value: 5), uuid: BluetoothUuid(value: "fff50000-0000-4000-8000-000000000000"),
                                   properties: CharacteristicProperties(readable: true, writableWithResponse: false, writableWithoutResponse: false,
                                                                        notifiable: false, indicatable: true), descriptors: []),
            ]),
        ])
    }

    private func notification(ids: RfCycleIds, connection: UInt64? = nil, gatt: UInt64? = nil,
                              subscription: UInt64? = nil, characteristic: UInt64 = 2,
                              value: Data = Data([0xDE, 0xAD, 0xBE, 0xEF])) -> BleEvent {
        .notificationReceived(connectionId: ConnectionId(value: connection ?? ids.connection.value),
                              gattSessionId: GattSessionId(value: gatt ?? ids.gatt.value),
                              subscriptionId: SubscriptionId(value: subscription ?? ids.subscription.value),
                              characteristicId: GattCharacteristicId(value: characteristic), value: value)
    }

    private func cycleEvents(ids: RfCycleIds, scenario: AppleRfScenario) -> [BleEvent] {
        func terminal(_ local: UInt64, error: BleEventError? = nil) -> BleEvent {
            .commandCompleted(commandId: ids.command(local), error: error)
        }
        var events: [BleEvent] = [
            .scanStarted(commandId: ids.command(2), scanId: ids.scan), terminal(2),
            .advertisementObserved(advertisement: advertisement(session: ids.scan.value, peripheral: 1, services: [])),
            .scanStopped(scanId: ids.scan, result: .ok(reason: .requested(commandId: ids.command(3)))), terminal(3),
            .connectionStateChanged(commandId: ids.command(4), connectionId: ids.connection, result: .ok(state: .connecting)),
            .connectionStateChanged(commandId: ids.command(4), connectionId: ids.connection, result: .ok(state: .connected)), terminal(4),
            .gattDiscovered(commandId: ids.command(5), database: database(ids: ids)), terminal(5),
        ]
        if scenario == .indication { events.append(terminal(13, error: failure(.notSupported))) }
        if scenario.usesNotifications {
            let char = GattCharacteristicId(value: scenario == .indication ? 5 : 2)
            events += [
                .notificationStateChanged(commandId: ids.command(6), connectionId: ids.connection, gattSessionId: ids.gatt,
                                          subscriptionId: ids.subscription, characteristicId: char,
                                          state: .enabled(actualMode: scenario == .indication ? .indication : .notification)),
                terminal(6), notification(ids: ids, characteristic: char.value),
            ]
            if scenario != .passiveDisconnect && scenario != .recovery {
                events += [.notificationStateChanged(commandId: ids.command(7), connectionId: ids.connection, gattSessionId: ids.gatt,
                                                     subscriptionId: ids.subscription, characteristicId: char, state: .disabled), terminal(7)]
            }
        } else if scenario == .rssi {
            events += [.rssiRead(commandId: ids.command(12), connectionId: ids.connection, rssi: -47), terminal(12)]
        }
        if scenario == .io {
            for local: UInt64 in 6 ... 10 {
                if [6, 8, 10].contains(local) {
                    let value = local == 6 ? Data([0x00]) : (local == 8 ? Data([0x10, 0x20, 0x30]) : Data([0xAB, 0xCD]))
                    events.append(.characteristicRead(commandId: ids.command(local), connectionId: ids.connection,
                                                      gattSessionId: ids.gatt, characteristicId: GattCharacteristicId(value: 1), value: value))
                } else {
                    events.append(.characteristicWritten(commandId: ids.command(local), connectionId: ids.connection,
                                                         gattSessionId: ids.gatt, characteristicId: GattCharacteristicId(value: 1)))
                }
                events.append(terminal(local))
            }
        }
        if scenario == .passiveDisconnect || scenario == .recovery {
            events.append(.connectionStateChanged(commandId: nil, connectionId: ids.connection, result: .err(error: failure(.connection))))
        } else {
            let local: UInt64 = scenario == .io ? 11 : (scenario.usesNotifications ? 8 : 6)
            events += [.connectionStateChanged(commandId: ids.command(local), connectionId: ids.connection, result: .ok(state: .disconnected)), terminal(local)]
        }
        return events
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
