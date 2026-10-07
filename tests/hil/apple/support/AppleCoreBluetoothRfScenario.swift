

import BleHubUniFfi
import Darwin
import Dispatch
import Foundation

enum ProbeError: Error, CustomStringConvertible {
    case initializationTimeout
    case authorizationTimeout
    case authorizationRejected(AuthorizationState)
    case scanUnavailable(AdapterSnapshot)
    case scanTimeout([BleEvent])
    case scanStopTimeout([BleEvent])
    case postStopAdvertisement
    case connectTimeout([BleEvent])
    case gattTimeout([BleEvent])
    case gattIoTimeout(String, [BleEvent])
    case gattIoFailed(String, BleEventError)
    case notificationTimeout(String, [BleEvent])
    case notificationFailed(String, BleEventError)
    case invalidGattDatabase(String)
    case invalidGattValue(String)
    case invalidNotification(String)
    case disconnectTimeout([BleEvent])
    case postDisconnectConnectionState
    case postDisconnectGattEvent
    case unexpectedEvents([BleEvent])

    var description: String {
        switch self {
        case .initializationTimeout:
            "timed out waiting for CoreBluetooth initialization events"
        case .authorizationTimeout:
            "timed out waiting for macOS Bluetooth authorization"
        case .authorizationRejected(let authorization):
            "macOS Bluetooth authorization became \(authorization)"
        case .scanUnavailable(let snapshot):
            "CoreBluetooth scan is unavailable with snapshot \(snapshot)"
        case .scanTimeout(let events):
            "timed out waiting for the matching advertisement: \(events)"
        case .scanStopTimeout(let events):
            "timed out waiting for requested scan stop events: \(events)"
        case .postStopAdvertisement:
            "received an advertisement after ScanStopped"
        case .connectTimeout(let events):
            "timed out waiting for CoreBluetooth connection events: \(events)"
        case .gattTimeout(let events):
            "timed out waiting for CoreBluetooth GATT discovery events: \(events)"
        case .gattIoTimeout(let operation, let events):
            "timed out waiting for CoreBluetooth GATT \(operation) events: \(events)"
        case .gattIoFailed(let operation, let error):
            "CoreBluetooth GATT \(operation) failed: \(error)"
        case .notificationTimeout(let operation, let events):
            "timed out waiting for CoreBluetooth notification \(operation): \(events)"
        case .notificationFailed(let operation, let error):
            "CoreBluetooth notification \(operation) failed: \(error)"
        case .invalidGattDatabase(let detail):
            "CoreBluetooth returned an unexpected Android fixture database: \(detail)"
        case .invalidGattValue(let detail):
            "CoreBluetooth returned an unexpected Android fixture value: \(detail)"
        case .invalidNotification(let detail):
            "CoreBluetooth returned an unexpected notification result: \(detail)"
        case .disconnectTimeout(let events):
            "timed out waiting for CoreBluetooth disconnect events: \(events)"
        case .postDisconnectConnectionState:
            "received a connection state after the logical disconnect completed"
        case .postDisconnectGattEvent:
            "received a GATT database after the logical disconnect completed"
        case .unexpectedEvents(let events):
            "unexpected initialization event sequence: \(events)"
        }
    }
}

enum AppleRfScenario: String {
    case io, notification, indication, rssi
    case passiveDisconnect = "passive-disconnect"
    case recovery

    var usesNotifications: Bool {
        self == .notification || self == .indication || self == .passiveDisconnect || self == .recovery
    }

    var needsHost: Bool { usesNotifications }
}

// Caller-owned IDs: initialization is 1; each recovery cycle reserves a disjoint
// command block. Opaque attribute IDs remain backend-owned and session-scoped.
struct RfCycleIds {
    var cycle: UInt64 = 1
    func command(_ local: UInt64) -> CommandId { CommandId(value: (cycle - 1) * 100 + local) }
    var scan: ScanSessionId { ScanSessionId(value: cycle) }
    var connection: ConnectionId { ConnectionId(value: cycle) }
    var gatt: GattSessionId { GattSessionId(value: cycle) }
    var subscription: SubscriptionId { SubscriptionId(value: cycle) }
    var rejectedSubscription: SubscriptionId { SubscriptionId(value: 1000 + cycle) }
}

struct RfLedgerError: Error, CustomStringConvertible {
    let description: String
}

func rfCommandId(_ event: BleEvent) -> CommandId? {
    switch event {
    case .adapterSnapshotChanged(let id, _), .connectionStateChanged(let id, _, _): return id
    case .scanStarted(let id, _), .gattDiscovered(let id, _),
         .characteristicRead(let id, _, _, _, _), .characteristicWritten(let id, _, _, _),
         .notificationStateChanged(let id, _, _, _, _, _), .rssiRead(let id, _, _),
         .commandCompleted(let id, _): return id
    case .scanStopped(_, .ok(.requested(let id))): return id
    default: return nil
    }
}

// Validate all domains with this command ID, not merely the first matching tuple.
// Returning their indices lets the cycle ledger reject every unaccounted event.
@discardableResult
func validateRfCommand(
    _ events: [BleEvent], id: CommandId, expectedError: BleErrorCategory? = nil,
    domains: [(BleEvent) -> Bool]
) throws -> [Int] {
    let indices = events.indices.filter { rfCommandId(events[$0]) == id }
    guard indices.count == domains.count + 1,
          let last = indices.last,
          case .commandCompleted(_, let error) = events[last],
          error?.category == expectedError,
          zip(indices.dropLast(), domains).allSatisfy({ $0.1(events[$0.0]) })
    else {
        throw RfLedgerError(description: "command \(id.value): expected \(domains.count) correlated domains before exactly one \(String(describing: expectedError)) terminal; events=\(events)")
    }
    return indices
}

func waitForRfTerminal(
    fixture: SwiftProbeHarness, commandId: CommandId, operation: String, timeout: TimeInterval
) throws {
    let deadline = Date().addingTimeInterval(timeout)
    while Date() < deadline {
        if fixture.snapshot().events.contains(where: {
            if case .commandCompleted(let id, _) = $0 { return id == commandId }; return false
        }) { return }
        waitForCallbacks()
    }
    throw ProbeError.gattIoTimeout(operation, fixture.snapshot().events)
}

@discardableResult
func validateRssiLedger(_ events: [BleEvent], command: ReadRssiCommand) throws -> Int16 {
    var value: Int16 = 0
    try validateRfCommand(events, id: command.commandId, domains: [{
        guard case .rssiRead(_, let connection, let rssi) = $0,
              connection == command.connectionId else { return false }
        value = rssi
        return true
    }])
    return value
}

func runStrictIndicationRejection(
    fixture: SwiftProbeHarness, database: GattDatabase, serviceUuid: String, ids: RfCycleIds
) throws {
    guard let characteristic = database.services.first(where: { $0.uuid.value == serviceUuid.lowercased() })?
        .characteristics.first(where: { $0.uuid.value == "fff20000-0000-4000-8000-000000000000" }),
        characteristic.properties.notifiable, characteristic.properties.indicatable
    else { throw ProbeError.invalidGattDatabase("strict rejection requires dual-property FFF2") }
    let command = SetNotificationsCommand(
        commandId: ids.command(13), connectionId: database.connectionId,
        gattSessionId: database.gattSessionId, subscriptionId: ids.rejectedSubscription,
        characteristicId: characteristic.id, action: .enable(mode: .requireIndication)
    )
    try fixture.setNotifications(command: command)
    try waitForRfTerminal(fixture: fixture, commandId: command.commandId, operation: "dual-property strict Indication", timeout: 10)
    try validateRfCommand(fixture.snapshot().events, id: command.commandId, expectedError: .notSupported, domains: [])
}

func isRfPassiveDisconnect(_ event: BleEvent, connectionId: ConnectionId) -> Bool {
    guard case .connectionStateChanged(nil, let observed, let result) = event,
          observed == connectionId else { return false }
    switch result {
    case .ok(let state): return state == .disconnected
    case .err(let error): return error.category == .connection
    }
}

// A completed cycle retains its range in the append-only sink. Later cycles never
// clear that sink: this check catches even a wrong-command callback carrying an
// old connection, GATT session, subscription, or scan session.
func validateRetiredRfEvents(_ events: [BleEvent], after end: Int, ids: RfCycleIds) throws {
    for (offset, event) in events.dropFirst(end).enumerated() {
        var retired = rfCommandId(event).map {
            $0.value > (ids.cycle - 1) * 100 && $0.value < ids.cycle * 100
        } ?? false
        switch event {
        case .scanStarted(_, let scan), .scanStopped(let scan, _): retired = retired || scan == ids.scan
        case .advertisementObserved(let ad): retired = retired || ad.scanSessionId == ids.scan
        case .connectionStateChanged(_, let connection, _), .rssiRead(_, let connection, _):
            retired = retired || connection == ids.connection
        case .gattDiscovered(_, let database):
            retired = retired || database.connectionId == ids.connection || database.gattSessionId == ids.gatt
        case .characteristicRead(_, let connection, let gatt, _, _),
             .characteristicWritten(_, let connection, let gatt, _):
            retired = retired || connection == ids.connection || gatt == ids.gatt
        case .notificationStateChanged(_, let connection, let gatt, let sub, _, _),
             .notificationReceived(let connection, let gatt, let sub, _, _):
            retired = retired || connection == ids.connection || gatt == ids.gatt ||
                sub == ids.subscription || sub == ids.rejectedSubscription
        case .backendError: retired = true
        default: break
        }
        if retired {
            throw RfLedgerError(description: "late event for retired cycle \(ids.cycle) at index \(end + offset): \(event)")
        }
    }
}

// Complete accounting for one RF cycle, including rejected strict requests and
// no-command domains. Unknown, duplicate, reordered, or miscorrelated events fail.
func validateRfCycleLedger(_ events: [BleEvent], ids: RfCycleIds, scenario: AppleRfScenario,
                           configuration: RfFixtureConfiguration = .android) throws {
    var accounted = Set<Int>()
    var previousTerminal = -1
    func claim(_ local: UInt64, error: BleErrorCategory? = nil, _ domains: [(BleEvent) -> Bool]) throws {
        let indices = try validateRfCommand(events, id: ids.command(local), expectedError: error, domains: domains)
        guard let first = indices.first, let last = indices.last, first > previousTerminal else {
            throw RfLedgerError(description: "command \(ids.command(local).value) crossed the preceding command's terminal boundary")
        }
        previousTerminal = last
        accounted.formUnion(indices)
    }
    try claim(2, [{ if case .scanStarted(_, let scan) = $0 { return scan == ids.scan }; return false }])
    try claim(3, [{ requestedScanStopped(scanId: ids.scan, commandId: ids.command(3), event: $0) }])
    try claim(4, [.connecting, .connected].map { state in
        { connectionState(state, commandId: ids.command(4), connectionId: ids.connection, event: $0) }
    })
    var discovered: GattDatabase?
    try claim(5, [{
        guard case .gattDiscovered(_, let database) = $0,
              database.connectionId == ids.connection, database.gattSessionId == ids.gatt else { return false }
        discovered = database
        return true
    }])
    guard let database = discovered else { throw ProbeError.gattTimeout(events) }
    let startIndex = events.firstIndex { if case .scanStarted = $0 { return true }; return false }!
    let stopIndex = events.firstIndex { if case .scanStopped = $0 { return true }; return false }!
    var advertisements = 0
    for index in events.indices {
        if case .advertisementObserved(let ad) = events[index], ad.scanSessionId == ids.scan,
           index > startIndex, index < stopIndex {
            advertisements += 1
            accounted.insert(index)
        }
    }
    guard advertisements > 0 else { throw ProbeError.scanTimeout(events) }
    if scenario == .indication { try claim(13, error: .notSupported, []) }
    if scenario.usesNotifications {
        let uuid = scenario == .indication ? "fff50000-0000-4000-8000-000000000000" : "fff20000-0000-4000-8000-000000000000"
        guard let characteristic = database.services.flatMap({ $0.characteristics }).first(where: { $0.uuid.value == uuid }) else {
            throw ProbeError.invalidGattDatabase("missing \(uuid)")
        }
        let mode: NotificationMode = scenario == .indication ? .indication : .notification
        try validateNotificationEventLedger(
            events,
            database: database,
            subscriptionId: ids.subscription,
            characteristicId: characteristic.id,
            expectedValue: try configuration.notificationValue,
            ids: ids,
            expectedMode: mode,
            expectDisable: scenario != .passiveDisconnect && scenario != .recovery
        )
        for local: UInt64 in (scenario == .passiveDisconnect || scenario == .recovery) ? [6] : [6, 7] {
            try claim(local, [{
                guard case .notificationStateChanged(_, let connection, let gatt, let sub, let char, let state) = $0 else { return false }
                return connection == ids.connection && gatt == ids.gatt && sub == ids.subscription && char == characteristic.id &&
                    state == (local == 6 ? .enabled(actualMode: mode) : .disabled)
            }])
        }
        accounted.formUnion(events.indices.filter { if case .notificationReceived = events[$0] { return true }; return false })
    } else if scenario == .rssi {
        try validateRssiLedger(events, command: ReadRssiCommand(commandId: ids.command(12), connectionId: ids.connection))
        try claim(12, [{ if case .rssiRead(_, let connection, _) = $0 { return connection == ids.connection }; return false }])
    } else {
        guard let characteristic = database.services.flatMap({ $0.characteristics }).first(where: {
            $0.uuid.value == "fff10000-0000-4000-8000-000000000000"
        }) else { throw ProbeError.invalidGattDatabase("missing IO characteristic") }
        try validateGattIoEventLedger(events, database: database, characteristicId: characteristic.id, configuration: configuration)
        for local: UInt64 in [6, 7, 8, 9, 10] {
            try claim(local, [{ event in
                switch event {
                case .characteristicRead(_, let connection, let gatt, let char, _):
                    return [6, 8, 10].contains(local) && connection == ids.connection && gatt == ids.gatt && char == characteristic.id
                case .characteristicWritten(_, let connection, let gatt, let char):
                    return [7, 9].contains(local) && connection == ids.connection && gatt == ids.gatt && char == characteristic.id
                default: return false
                }
            }])
        }
    }
    let disconnectIndex: Int
    if scenario == .passiveDisconnect || scenario == .recovery {
        let indices = events.indices.filter { isRfPassiveDisconnect(events[$0], connectionId: ids.connection) }
        guard indices.count == 1 else { throw ProbeError.disconnectTimeout(events) }
        disconnectIndex = indices[0]
        accounted.insert(disconnectIndex)
    } else {
        let local: UInt64 = scenario == .io ? 11 : (scenario.usesNotifications ? 8 : 6)
        try claim(local, [{ connectionState(.disconnected, commandId: ids.command(local), connectionId: ids.connection, event: $0) }])
        disconnectIndex = events.firstIndex {
            connectionState(.disconnected, commandId: ids.command(local), connectionId: ids.connection, event: $0)
        }!
    }
    // All GATT/value/RSSI activity must precede the disconnected domain, not just
    // the return from a polling loop. The logical disconnect terminal alone may follow.
    for index in events.indices where index > disconnectIndex {
        if case .commandCompleted = events[index], scenario != .passiveDisconnect { continue }
        throw RfLedgerError(description: "event after disconnect boundary: \(events[index])")
    }
    guard accounted.count == events.count else {
        let unexpected = events.indices.filter { !accounted.contains($0) }.map { "\($0): \(events[$0])" }
        throw RfLedgerError(description: "unaccounted RF cycle \(ids.cycle) events: \(unexpected)")
    }
}

func prepareRfCoordinationDirectory(path: String, runId: String) throws {
    guard !runId.isEmpty, runId != "missing", runId.utf8.count <= 128,
          runId.unicodeScalars.allSatisfy({ CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "-_")).contains($0) }),
          path.hasPrefix("/"), URL(fileURLWithPath: path).lastPathComponent == "blehub-rf-\(runId)"
    else { throw RfLedgerError(description: "invalid RF run ID or absolute Documents coordination path") }
    guard !FileManager.default.fileExists(atPath: path) else {
        throw RfLedgerError(description: "RF run directory already exists; use a fresh run ID: \(runId)")
    }
    try FileManager.default.createDirectory(atPath: path, withIntermediateDirectories: false)
}

func validateRfPostDispose(_ events: [BleEvent], before: [BleEvent]) throws {
    guard events == before else {
        throw RfLedgerError(description: "events changed after dispose; before=\(before.count), after=\(events)")
    }
}

func runIOSRfScenario(
    fixture: SwiftProbeHarness, scenario: AppleRfScenario, serviceUuid: String,
    runId: String, coordinationPath: String, unfiltered: Bool = false,
    allowDuplicates: Bool = true, diagnosticTargetPeripheralId: (() -> PeripheralId?)? = nil,
    scanDuration: TimeInterval = 30,
    configuration: RfFixtureConfiguration = .android,
    externalProvider: String? = nil
) throws -> [String] {
    var output = configuration.evidence
    var cycles: [RfCycleEvidence] = []
    let historyStart = fixture.snapshot().events.count
    for cycle in 1 ... (scenario == .recovery ? 3 : 1) {
        let ids = RfCycleIds(cycle: UInt64(cycle))
        let path: String
        if scenario == .recovery {
            path = URL(fileURLWithPath: coordinationPath).appendingPathComponent("cycle-\(cycle)").path
            try FileManager.default.createDirectory(atPath: path, withIntermediateDirectories: false)
            try signalProbeHost(path: path, marker: "cycle-ready", value: "\(runId)\n")
            try waitForProbeHost(path: path, marker: "fixture-ready", timeout: 90)
        } else { path = coordinationPath }
        // Include the host-ACK wait in the next cycle's accounting; never skip
        // events by taking a fresh offset after that wait.
        let start = cycles.last?.range.upperBound ?? historyStart
        let scan = try runScan(fixture: fixture, serviceUuid: serviceUuid, unfiltered: unfiltered,
                               allowDuplicates: allowDuplicates, diagnosticTargetPeripheralId: diagnosticTargetPeripheralId,
                               scanDuration: scanDuration, ids: ids)
        output.append(contentsOf: scan.output)
        // Preserve the historical diagnostic scan-only path, but never call it an RF pass.
        if unfiltered && diagnosticTargetPeripheralId == nil {
            guard scenario == .io else {
                throw RfLedgerError(description: "a complete RF scenario requires provider-filtered scan or an explicit target")
            }
            output.append("apple_ios_corebluetooth_unfiltered_scan=passed")
            return output
        }
        output.append(contentsOf: try runConnection(
            fixture: fixture, peripheralId: scan.advertisement.peripheralId,
            discoverGattServiceUuid: serviceUuid, runGattIoAfterDiscovery: scenario == .io,
            runNotificationsAfterDiscovery: scenario.usesNotifications,
            fixtureCoordinationPath: scenario.needsHost ? path : nil,
            ids: ids,
            rfScenario: scenario,
            configuration: configuration,
            externalProvider: externalProvider
        ))
        cycles.append(RfCycleEvidence(ids: ids, range: start ..< fixture.snapshot().events.count))
        try validateRfHistory(fixture.snapshot().events, cycles: cycles, scenario: scenario, configuration: configuration)
        if scenario == .recovery {
            try signalProbeHost(path: path, marker: "cycle-passed", value: "\(runId)\n")
            output.append("apple_ios_rf_recovery_cycle_\(cycle)=passed")
        }
    }
    // No pending operations remain. Keep the same sink alive to observe disposal
    // and native callbacks; a second backend/Hub would hide the boundary under test.
    let beforeDispose = fixture.snapshot().events
    try fixture.dispose()
    Thread.sleep(forTimeInterval: 2)
    let finalEvents = fixture.snapshot().events
    try validateRfPostDispose(finalEvents, before: beforeDispose)
    try validateRfHistory(finalEvents, cycles: cycles, scenario: scenario, configuration: configuration)
    output.append("apple_ios_rf_ledger=passed")
    output.append("apple_ios_rf_post_dispose_silence=passed")
    output.append("apple_ios_rf_\(scenario.rawValue)=passed")
    output.append("apple_ios_corebluetooth_rf=passed")
    return output
}

struct RfCycleEvidence {
    let ids: RfCycleIds
    let range: Range<Int>
}

func validateRfHistory(_ events: [BleEvent], cycles: [RfCycleEvidence], scenario: AppleRfScenario,
                       configuration: RfFixtureConfiguration = .android) throws {
    for (index, cycle) in cycles.enumerated() {
        guard cycle.ids.cycle == UInt64(index + 1),
              index == 0 || cycle.range.lowerBound == cycles[index - 1].range.upperBound else {
            throw RfLedgerError(description: "RF cycle IDs or event ranges were reused or skipped")
        }
        guard cycle.range.upperBound <= events.count else {
            throw RfLedgerError(description: "append-only RF history was truncated")
        }
        try validateRfCycleLedger(Array(events[cycle.range]), ids: cycle.ids, scenario: scenario, configuration: configuration)
        try validateRetiredRfEvents(events, after: cycle.range.upperBound, ids: cycle.ids)
    }
    if let end = cycles.last?.range.upperBound, end != events.count {
        throw RfLedgerError(description: "unaccounted events after completed RF cycles: \(Array(events.dropFirst(end)))")
    }
}

func waitForCallbacks() {
    Thread.sleep(forTimeInterval: 0.05)
}

func latestAdapterSnapshot(in events: [BleEvent]) -> AdapterSnapshot? {
    for event in events.reversed() {
        if case .adapterSnapshotChanged(_, let snapshot) = event {
            return snapshot
        }
    }
    return nil
}

func commandSucceeded(_ commandId: CommandId, in events: [BleEvent]) -> Bool {
    events.contains { event in
        guard case .commandCompleted(let observedCommandId, let error) = event else {
            return false
        }
        return observedCommandId == commandId && error == nil
    }
}

func scanStarted(_ command: StartScanCommand, in events: [BleEvent]) -> Bool {
    events.contains { event in
        guard case .scanStarted(let commandId, let scanId) = event else {
            return false
        }
        return commandId == command.commandId && scanId == command.scanSessionId
    }
}

// Pass the issued command only after its ScanStarted and successful completion.
func matchingAdvertisement(
    scanCommand: StartScanCommand,
    serviceUuid: BluetoothUuid,
    targetPeripheralId: PeripheralId? = nil,
    in events: [BleEvent]
) -> AdvertisementObservation? {
    // A single provider filter proves the match even when iOS hides its UUID.
    // Multiple filters prove only an OR, not which provider matched. Neither
    // case changes the actual advertisedServices payload.
    let providerFilterMatched = scanCommand.options.serviceUuids == [serviceUuid]
    for event in events {
        guard case .advertisementObserved(let advertisement) = event,
              advertisement.scanSessionId == scanCommand.scanSessionId
        else {
            continue
        }
        if let targetPeripheralId {
            if advertisement.peripheralId == targetPeripheralId {
                return advertisement
            }
        } else if providerFilterMatched || advertisement.advertisedServices.contains(serviceUuid) {
            return advertisement
        }
    }
    return nil
}

func firstAdvertisement(
    scanId: ScanSessionId,
    in events: [BleEvent]
) -> AdvertisementObservation? {
    for event in events {
        if case .advertisementObserved(let advertisement) = event,
           advertisement.scanSessionId == scanId
        {
            return advertisement
        }
    }
    return nil
}

func requestedScanStopped(
    scanId: ScanSessionId,
    commandId: CommandId,
    event: BleEvent
) -> Bool {
    guard case .scanStopped(let observedScanId, let result) = event,
          observedScanId == scanId,
          case .ok(let reason) = result,
          case .requested(let observedCommandId) = reason
    else {
        return false
    }
    return observedCommandId == commandId
}

func connectionState(
    _ expectedState: ConnectionState,
    commandId: CommandId?,
    connectionId: ConnectionId,
    event: BleEvent
) -> Bool {
    guard case .connectionStateChanged(
        let observedCommandId,
        let observedConnectionId,
        let result
    ) = event,
        observedCommandId == commandId,
        observedConnectionId == connectionId,
        case .ok(let state) = result
    else {
        return false
    }
    return state == expectedState
}

func runScan(
    fixture: SwiftProbeHarness,
    serviceUuid value: String,
    unfiltered: Bool = false,
    allowDuplicates: Bool = true,
    diagnosticTargetPeripheralId: (() -> PeripheralId?)? = nil,
    scanDuration: TimeInterval = 30,
    ids: RfCycleIds = RfCycleIds()
) throws -> (output: [String], advertisement: AdvertisementObservation) {
    let serviceUuid = BluetoothUuid(value: value.lowercased())
    let startCommand = StartScanCommand(
        commandId: ids.command(2),
        scanSessionId: ids.scan,
        options: ScanOptions(
            serviceUuids: unfiltered ? [] : [serviceUuid],
            namePrefix: nil,
            companyId: nil,
            allowDuplicates: allowDuplicates
        )
    )
    let eventOffset = fixture.snapshot().events.count
    try fixture.startScan(command: startCommand)

    let scanDeadline = Date().addingTimeInterval(scanDuration)
    let diagnosticScanOnly = unfiltered && diagnosticTargetPeripheralId == nil
    var advertisement: AdvertisementObservation?
    while Date() < scanDeadline {
        let events = Array(fixture.snapshot().events.dropFirst(eventOffset))
        if scanStarted(startCommand, in: events),
           commandSucceeded(startCommand.commandId, in: events)
        {
            if diagnosticScanOnly {
                advertisement = advertisement ?? firstAdvertisement(
                    scanId: startCommand.scanSessionId,
                    in: events
                )
            } else if let diagnosticTargetPeripheralId {
                if let targetPeripheralId = diagnosticTargetPeripheralId(),
                   let matching = matchingAdvertisement(
                       scanCommand: startCommand,
                       serviceUuid: serviceUuid,
                       targetPeripheralId: targetPeripheralId,
                       in: events
                   )
                {
                    advertisement = matching
                    break
                }
            } else if let matching = matchingAdvertisement(
                scanCommand: startCommand,
                serviceUuid: serviceUuid,
                in: events
            ) {
                advertisement = matching
                break
            }
        }
        waitForCallbacks()
    }
    guard let advertisement else {
        throw ProbeError.scanTimeout(fixture.snapshot().events)
    }

    let stopCommand = StopScanCommand(
        commandId: ids.command(3),
        scanSessionId: startCommand.scanSessionId
    )
    try fixture.stopScan(command: stopCommand)
    let stopDeadline = Date().addingTimeInterval(5)
    var stoppedEventIndex: Int?
    while Date() < stopDeadline {
        let events = fixture.snapshot().events
        if commandSucceeded(stopCommand.commandId, in: events),
           let index = events.firstIndex(where: {
               requestedScanStopped(
                   scanId: startCommand.scanSessionId,
                   commandId: stopCommand.commandId,
                   event: $0
               )
           })
        {
            stoppedEventIndex = index
            break
        }
        waitForCallbacks()
    }
    guard let stoppedEventIndex else {
        throw ProbeError.scanStopTimeout(fixture.snapshot().events)
    }

    Thread.sleep(forTimeInterval: 1)
    let finalEvents = fixture.snapshot().events
    let eventsAfterStop = finalEvents.dropFirst(stoppedEventIndex + 1)
    let postStopAdvertisement: AdvertisementObservation?
    if diagnosticScanOnly {
        postStopAdvertisement = firstAdvertisement(
            scanId: startCommand.scanSessionId,
            in: Array(eventsAfterStop)
        )
    } else if let diagnosticTargetPeripheralId {
        postStopAdvertisement = diagnosticTargetPeripheralId().flatMap {
            matchingAdvertisement(
                scanCommand: startCommand,
                serviceUuid: serviceUuid,
                targetPeripheralId: $0,
                in: Array(eventsAfterStop)
            )
        }
    } else {
        postStopAdvertisement = matchingAdvertisement(
            scanCommand: startCommand,
            serviceUuid: serviceUuid,
            in: Array(eventsAfterStop)
        )
    }
    if postStopAdvertisement != nil {
        throw ProbeError.postStopAdvertisement
    }

    let rssi = advertisement.rssi.map { String($0) } ?? "absent"
    let connectable = advertisement.connectable.map { String($0) } ?? "absent"
    return (
        output: [
            "apple_macos_corebluetooth_scan_service_uuid=\(serviceUuid.value)",
            "apple_macos_corebluetooth_scan_filter=\(unfiltered ? "unfiltered" : "service")",
            "apple_macos_corebluetooth_scan_selection=\(diagnosticTargetPeripheralId == nil ? (diagnosticScanOnly ? "diagnostic-scan-only" : "service") : "native-identifier")",
            "apple_macos_corebluetooth_scan_allow_duplicates=\(allowDuplicates)",
            "apple_macos_corebluetooth_scan_peripheral_id=\(advertisement.peripheralId.value)",
            "apple_macos_corebluetooth_scan_rssi=\(rssi)",
            "apple_macos_corebluetooth_scan_connectable=\(connectable)",
            "apple_macos_corebluetooth_scan_advertised_services=\(advertisement.advertisedServices.map(\.value).joined(separator: ","))",
            "apple_macos_corebluetooth_scan_local_name=\(advertisement.localName ?? "absent")",
            "apple_macos_corebluetooth_scan_started=passed",
            "apple_macos_corebluetooth_advertisement=passed",
            "apple_macos_corebluetooth_scan_stopped=passed",
            "apple_macos_corebluetooth_post_stop_silence=passed",
            "apple_macos_corebluetooth_scan=passed",
        ],
        advertisement: advertisement
    )
}

func runGattDiscovery(
    fixture: SwiftProbeHarness,
    connectionId: ConnectionId,
    expectedServiceUuid: String,
    ids: RfCycleIds = RfCycleIds(),
    configuration: RfFixtureConfiguration = .android
) throws -> (output: [String], database: GattDatabase) {
    let command = DiscoverGattCommand(
        commandId: ids.command(5),
        connectionId: connectionId,
        gattSessionId: ids.gatt
    )
    let eventOffset = fixture.snapshot().events.count
    try fixture.discoverGatt(command: command)
    let deadline = Date().addingTimeInterval(30)
    var database: GattDatabase?
    while Date() < deadline {
        let events = Array(fixture.snapshot().events.dropFirst(eventOffset))
        guard let discoveredIndex = events.firstIndex(where: { event in
            guard case .gattDiscovered(let commandId, _) = event else {
                return false
            }
            return commandId == command.commandId
        }), let completedIndex = events.firstIndex(where: { event in
            guard case .commandCompleted(let commandId, let error) = event else {
                return false
            }
            return commandId == command.commandId && error == nil
        }), discoveredIndex < completedIndex else {
            waitForCallbacks()
            continue
        }
        if case .gattDiscovered(_, let discoveredDatabase) = events[discoveredIndex] {
            database = discoveredDatabase
            break
        }
    }
    guard let database else {
        throw ProbeError.gattTimeout(Array(fixture.snapshot().events.dropFirst(eventOffset)))
    }

    try validateAndroidGattDatabase(
        database,
        command: command,
        expectedServiceUuid: BluetoothUuid(value: expectedServiceUuid.lowercased()),
        configuration: configuration
    )
    let descriptorCount = database.services.reduce(0) { serviceTotal, service in
        serviceTotal + service.characteristics.reduce(0) { characteristicTotal, characteristic in
            characteristicTotal + characteristic.descriptors.count
        }
    }
    return (
        output: [
            "apple_macos_corebluetooth_gatt_service_count=\(database.services.count)",
        "apple_macos_corebluetooth_gatt_characteristic_count=\(database.services.reduce(0) { $0 + $1.characteristics.count })",
        "apple_macos_corebluetooth_gatt_descriptor_count=\(descriptorCount)",
        "apple_macos_corebluetooth_gatt_event_order=passed",
            "apple_macos_corebluetooth_gatt_database=passed",
            "apple_macos_corebluetooth_gatt=passed",
        ],
        database: database
    )
}

func validateAndroidGattDatabase(
    _ database: GattDatabase,
    command: DiscoverGattCommand,
    expectedServiceUuid: BluetoothUuid,
    configuration: RfFixtureConfiguration = .android
) throws {
    guard database.connectionId == command.connectionId,
          database.gattSessionId == command.gattSessionId
    else {
        throw ProbeError.invalidGattDatabase("connection or GATT session correlation mismatch")
    }
    let matchingServices = database.services.filter { $0.uuid == expectedServiceUuid }
    guard matchingServices.count == 1, let service = matchingServices.first, service.primary == true else {
        throw ProbeError.invalidGattDatabase("expected exactly one primary \(expectedServiceUuid.value) service")
    }

    var expectedCharacteristics: [BluetoothUuid: CharacteristicProperties] = [
        BluetoothUuid(value: "fff10000-0000-4000-8000-000000000000"): CharacteristicProperties(
            readable: true,
            writableWithResponse: true,
            writableWithoutResponse: true,
            notifiable: false,
            indicatable: false
        ),
        BluetoothUuid(value: "fff20000-0000-4000-8000-000000000000"): CharacteristicProperties(
            readable: true,
            writableWithResponse: false,
            writableWithoutResponse: false,
            notifiable: true,
            indicatable: true
        ),
        BluetoothUuid(value: "fff50000-0000-4000-8000-000000000000"): CharacteristicProperties(
            readable: true,
            writableWithResponse: false,
            writableWithoutResponse: false,
            notifiable: false,
            indicatable: true
        ),
        BluetoothUuid(value: "fff30000-0000-4000-8000-000000000000"): CharacteristicProperties(
            readable: true,
            writableWithResponse: false,
            writableWithoutResponse: false,
            notifiable: false,
            indicatable: false
        ),
        BluetoothUuid(value: "fff40000-0000-4000-8000-000000000000"): CharacteristicProperties(
            readable: false,
            writableWithResponse: true,
            writableWithoutResponse: true,
            notifiable: false,
            indicatable: false
        ),
    ]
    if !configuration.hasIndicationOnly {
        expectedCharacteristics.removeValue(forKey: BluetoothUuid(value: "fff50000-0000-4000-8000-000000000000"))
    }
    guard service.characteristics.count == expectedCharacteristics.count,
          Set(service.characteristics.map(\.uuid)).count == expectedCharacteristics.count else {
        throw ProbeError.invalidGattDatabase(
            "expected \(expectedCharacteristics.count) fixture characteristics, got \(service.characteristics.count)"
        )
    }
    for characteristic in service.characteristics {
        guard let expectedProperties = expectedCharacteristics[characteristic.uuid] else {
            throw ProbeError.invalidGattDatabase(
                "unexpected characteristic \(characteristic.uuid.value)"
            )
        }
        guard characteristic.properties == expectedProperties else {
            throw ProbeError.invalidGattDatabase(
                "property mismatch for characteristic \(characteristic.uuid.value)"
            )
        }
    }

    let updateUuid = BluetoothUuid(value: "fff20000-0000-4000-8000-000000000000")
    let cccdUuid = BluetoothUuid(value: "00002902-0000-1000-8000-00805f9b34fb")
    let indicationUuid = BluetoothUuid(value: "fff50000-0000-4000-8000-000000000000")
    let subscribedUuids = configuration.hasIndicationOnly ? [updateUuid, indicationUuid] : [updateUuid]
    for uuid in subscribedUuids {
        guard let characteristic = service.characteristics.first(where: { $0.uuid == uuid }),
              characteristic.descriptors.contains(where: { $0.uuid == cccdUuid }) else {
            throw ProbeError.invalidGattDatabase("updates characteristic \(uuid.value) is missing its CCCD")
        }
    }

    let serviceIds = database.services.map(\.id)
    let characteristicIds = database.services.flatMap { $0.characteristics.map(\.id) }
    let descriptorIds = database.services.flatMap {
        $0.characteristics.flatMap { $0.descriptors.map(\.id) }
    }
    guard Set(serviceIds).count == serviceIds.count,
          Set(characteristicIds).count == characteristicIds.count,
          Set(descriptorIds).count == descriptorIds.count
    else {
        throw ProbeError.invalidGattDatabase("opaque attribute IDs are not unique")
    }
}

func runCharacteristicRead(
    fixture: SwiftProbeHarness,
    connectionId: ConnectionId,
    gattSessionId: GattSessionId,
    characteristicId: GattCharacteristicId,
    commandId: UInt64,
    operation: String
) throws -> Data {
    let command = ReadCharacteristicCommand(
        commandId: CommandId(value: commandId),
        connectionId: connectionId,
        gattSessionId: gattSessionId,
        characteristicId: characteristicId
    )
    let eventOffset = fixture.snapshot().events.count
    try fixture.readCharacteristic(command: command)
    let deadline = Date().addingTimeInterval(10)
    while Date() < deadline {
        let events = Array(fixture.snapshot().events.dropFirst(eventOffset))
        guard let completedIndex = events.firstIndex(where: { event in
            guard case .commandCompleted(let observedCommandId, _) = event else {
                return false
            }
            return observedCommandId == command.commandId
        }) else {
            waitForCallbacks()
            continue
        }
        if case .commandCompleted(_, let error) = events[completedIndex], let error {
            throw ProbeError.gattIoFailed(operation, error)
        }
        guard let readIndex = events.firstIndex(where: { event in
            guard case .characteristicRead(
                let observedCommandId,
                let observedConnectionId,
                let observedGattSessionId,
                let observedCharacteristicId,
                _
            ) = event else {
                return false
            }
            return observedCommandId == command.commandId &&
                observedConnectionId == command.connectionId &&
                observedGattSessionId == command.gattSessionId &&
                observedCharacteristicId == command.characteristicId
        }), readIndex < completedIndex else {
            waitForCallbacks()
            continue
        }
        if case .characteristicRead(_, _, _, _, let value) = events[readIndex] {
            return value
        }
    }
    throw ProbeError.gattIoTimeout(
        operation,
        Array(fixture.snapshot().events.dropFirst(eventOffset))
    )
}

func runCharacteristicWrite(
    fixture: SwiftProbeHarness,
    connectionId: ConnectionId,
    gattSessionId: GattSessionId,
    characteristicId: GattCharacteristicId,
    commandId: UInt64,
    value: Data,
    writeType: WriteType,
    operation: String
) throws {
    let command = WriteCharacteristicCommand(
        commandId: CommandId(value: commandId),
        connectionId: connectionId,
        gattSessionId: gattSessionId,
        characteristicId: characteristicId,
        value: value,
        writeType: writeType
    )
    let eventOffset = fixture.snapshot().events.count
    try fixture.writeCharacteristic(command: command)
    let deadline = Date().addingTimeInterval(10)
    while Date() < deadline {
        let events = Array(fixture.snapshot().events.dropFirst(eventOffset))
        guard let completedIndex = events.firstIndex(where: { event in
            guard case .commandCompleted(let observedCommandId, _) = event else {
                return false
            }
            return observedCommandId == command.commandId
        }) else {
            waitForCallbacks()
            continue
        }
        if case .commandCompleted(_, let error) = events[completedIndex], let error {
            throw ProbeError.gattIoFailed(operation, error)
        }
        guard let writtenIndex = events.firstIndex(where: { event in
            guard case .characteristicWritten(
                let observedCommandId,
                let observedConnectionId,
                let observedGattSessionId,
                let observedCharacteristicId
            ) = event else {
                return false
            }
            return observedCommandId == command.commandId &&
                observedConnectionId == command.connectionId &&
                observedGattSessionId == command.gattSessionId &&
                observedCharacteristicId == command.characteristicId
        }), writtenIndex < completedIndex else {
            waitForCallbacks()
            continue
        }
        return
    }
    throw ProbeError.gattIoTimeout(
        operation,
        Array(fixture.snapshot().events.dropFirst(eventOffset))
    )
}

func hexValue(_ value: Data) -> String {
    value.map { String(format: "%02x", $0) }.joined()
}

func signalProbeHost(path: String?, marker: String, value: String = "ready\n") throws {
    guard let path else {
        return
    }
    let markerUrl = URL(fileURLWithPath: path, isDirectory: true).appendingPathComponent(marker)
    try value.write(to: markerUrl, atomically: true, encoding: .utf8)
}

func waitForProbeHost(path: String?, marker: String, timeout: TimeInterval) throws {
    guard let path else {
        throw ProbeError.invalidNotification("fixture coordination path is missing")
    }
    let markerUrl = URL(fileURLWithPath: path, isDirectory: true).appendingPathComponent(marker)
    let deadline = Date().addingTimeInterval(timeout)
    while Date() < deadline {
        if FileManager.default.fileExists(atPath: markerUrl.path) {
            return
        }
        waitForCallbacks()
    }
    throw ProbeError.notificationTimeout("host marker \(marker) at \(path)", [])
}

func runGattIo(
    fixture: SwiftProbeHarness,
    database: GattDatabase,
    expectedServiceUuid: String,
    configuration: RfFixtureConfiguration = .android
) throws -> [String] {
    let serviceUuid = BluetoothUuid(value: expectedServiceUuid.lowercased())
    let characteristicUuid = BluetoothUuid(value: "fff10000-0000-4000-8000-000000000000")
    guard let service = database.services.first(where: { $0.uuid == serviceUuid }),
          let characteristic = service.characteristics.first(where: { $0.uuid == characteristicUuid })
    else {
        throw ProbeError.invalidGattDatabase("read/write characteristic is missing")
    }

    let initialValue = try runCharacteristicRead(
        fixture: fixture,
        connectionId: database.connectionId,
        gattSessionId: database.gattSessionId,
        characteristicId: characteristic.id,
        commandId: 6,
        operation: "initial read"
    )
    guard initialValue == (try configuration.initialValue) else {
        throw ProbeError.invalidGattValue(
            "initial FFF1 value was \(hexValue(initialValue)); expected \(configuration.initialValueHex)"
        )
    }

    let withResponseValue = try configuration.writeWithResponse
    try runCharacteristicWrite(
        fixture: fixture,
        connectionId: database.connectionId,
        gattSessionId: database.gattSessionId,
        characteristicId: characteristic.id,
        commandId: 7,
        value: withResponseValue,
        writeType: .withResponse,
        operation: "write with response"
    )
    let withResponseReadback = try runCharacteristicRead(
        fixture: fixture,
        connectionId: database.connectionId,
        gattSessionId: database.gattSessionId,
        characteristicId: characteristic.id,
        commandId: 8,
        operation: "write-with-response readback"
    )
    guard withResponseReadback == withResponseValue else {
        throw ProbeError.invalidGattValue(
            "write-with-response readback was \(hexValue(withResponseReadback)); expected \(hexValue(withResponseValue))"
        )
    }

    let withoutResponseValue = try configuration.writeWithoutResponse
    try runCharacteristicWrite(
        fixture: fixture,
        connectionId: database.connectionId,
        gattSessionId: database.gattSessionId,
        characteristicId: characteristic.id,
        commandId: 9,
        value: withoutResponseValue,
        writeType: .withoutResponse,
        operation: "write without response local submission"
    )
    Thread.sleep(forTimeInterval: 0.5)
    let withoutResponseReadback = try runCharacteristicRead(
        fixture: fixture,
        connectionId: database.connectionId,
        gattSessionId: database.gattSessionId,
        characteristicId: characteristic.id,
        commandId: 10,
        operation: "write-without-response readback"
    )
    guard withoutResponseReadback == withoutResponseValue else {
        throw ProbeError.invalidGattValue(
            "write-without-response readback was \(hexValue(withoutResponseReadback)); expected \(hexValue(withoutResponseValue))"
        )
    }

    return [
        "apple_macos_corebluetooth_gatt_initial_read=\(hexValue(initialValue))",
        "apple_macos_corebluetooth_write_with_response=passed",
        "apple_macos_corebluetooth_write_with_response_readback=passed",
        "apple_macos_corebluetooth_write_without_response_local_submission=passed",
        "apple_macos_corebluetooth_write_without_response_readback=passed",
    ]
}

func runNotifications(
    fixture: SwiftProbeHarness,
    database: GattDatabase,
    expectedServiceUuid: String,
    fixtureCoordinationPath: String?,
    ids: RfCycleIds = RfCycleIds(),
    strictIndication: Bool = false,
    leaveActive: Bool = false,
    configuration: RfFixtureConfiguration = .android
) throws -> (
    output: [String],
    ledgerOffset: Int,
    subscriptionId: SubscriptionId,
    characteristicId: GattCharacteristicId,
    expectedValue: Data
) {
    let serviceUuid = BluetoothUuid(value: expectedServiceUuid.lowercased())
    let characteristicUuid = BluetoothUuid(value: strictIndication
        ? "fff50000-0000-4000-8000-000000000000"
        : "fff20000-0000-4000-8000-000000000000")
    let expectedMode: NotificationMode = strictIndication ? .indication : .notification
    guard let service = database.services.first(where: { $0.uuid == serviceUuid }),
          let characteristic = service.characteristics.first(where: { $0.uuid == characteristicUuid })
    else {
        throw ProbeError.invalidGattDatabase("updates characteristic is missing")
    }

    let subscriptionId = ids.subscription
    let enable = SetNotificationsCommand(
        commandId: ids.command(6),
        connectionId: database.connectionId,
        gattSessionId: database.gattSessionId,
        subscriptionId: subscriptionId,
        characteristicId: characteristic.id,
        action: .enable(mode: strictIndication ? .requireIndication : .platformDefault)
    )
    let ledgerOffset = fixture.snapshot().events.count
    try fixture.setNotifications(command: enable)
    let enableDeadline = Date().addingTimeInterval(10)
    while Date() < enableDeadline {
        let events = Array(fixture.snapshot().events.dropFirst(ledgerOffset))
        if let completedIndex = events.firstIndex(where: { event in
            guard case .commandCompleted(let commandId, _) = event else {
                return false
            }
            return commandId == enable.commandId
        }) {
            if case .commandCompleted(_, let error) = events[completedIndex], let error {
                throw ProbeError.notificationFailed("enable", error)
            }
            guard let stateIndex = events.firstIndex(where: { event in
                guard case .notificationStateChanged(
                    let commandId,
                    let connectionId,
                    let gattSessionId,
                    let observedSubscriptionId,
                    let characteristicId,
                    let state
                ) = event,
                    commandId == enable.commandId,
                    connectionId == enable.connectionId,
                    gattSessionId == enable.gattSessionId,
                    observedSubscriptionId == enable.subscriptionId,
                    characteristicId == enable.characteristicId,
                    case .enabled(let actualMode) = state,
                    actualMode == expectedMode
                else {
                    return false
                }
                return true
            }), stateIndex < completedIndex else {
                waitForCallbacks()
                continue
            }
            break
        }
        waitForCallbacks()
    }
    let enabledEvents = Array(fixture.snapshot().events.dropFirst(ledgerOffset))
    guard commandSucceeded(enable.commandId, in: enabledEvents),
          enabledEvents.contains(where: { event in
              guard case .notificationStateChanged(
                  let commandId,
                  _,
                  _,
                  _,
                  _,
                  let state
              ) = event,
                  commandId == enable.commandId,
                  case .enabled(let actualMode) = state
              else {
                  return false
              }
              return actualMode == expectedMode
          })
    else {
        throw ProbeError.notificationTimeout("enable", enabledEvents)
    }

    let expectedValue = try configuration.notificationValue
    try validateNotificationEventLedger(
        enabledEvents, database: database, subscriptionId: subscriptionId,
        characteristicId: characteristic.id, expectedValue: expectedValue,
        ids: ids, expectedMode: expectedMode, expectDisable: false, expectValue: false
    )
    try signalProbeHost(path: fixtureCoordinationPath, marker: "notification-enabled")
    let valueDeadline = Date().addingTimeInterval(45)
    while Date() < valueDeadline {
        let events = Array(fixture.snapshot().events.dropFirst(ledgerOffset))
        if events.contains(where: { event in
            guard case .notificationReceived(
                let connectionId,
                let gattSessionId,
                let observedSubscriptionId,
                let characteristicId,
                let value
            ) = event else {
                return false
            }
            return connectionId == database.connectionId &&
                gattSessionId == database.gattSessionId &&
                observedSubscriptionId == subscriptionId &&
                characteristicId == characteristic.id &&
                value == expectedValue
        }) {
            break
        }
        waitForCallbacks()
    }
    let valueEvents = Array(fixture.snapshot().events.dropFirst(ledgerOffset))
    guard valueEvents.contains(where: { event in
        guard case .notificationReceived(
            let connectionId,
            let gattSessionId,
            let observedSubscriptionId,
            let characteristicId,
            let value
        ) = event else {
            return false
        }
        return connectionId == database.connectionId &&
            gattSessionId == database.gattSessionId &&
            observedSubscriptionId == subscriptionId &&
            characteristicId == characteristic.id &&
            value == expectedValue
    }) else {
        throw ProbeError.notificationTimeout("value delivery", valueEvents)
    }

    try validateNotificationEventLedger(
        valueEvents, database: database, subscriptionId: subscriptionId,
        characteristicId: characteristic.id, expectedValue: expectedValue,
        ids: ids, expectedMode: expectedMode, expectDisable: !leaveActive
    )
    if leaveActive {
        return ([
            "apple_macos_corebluetooth_notification_actual_mode=notification",
            "apple_macos_corebluetooth_notification_value=\(hexValue(expectedValue))",
            "apple_macos_corebluetooth_passive_disconnect_active_subscription=passed",
        ], ledgerOffset, subscriptionId, characteristic.id, expectedValue)
    }

    let disable = SetNotificationsCommand(
        commandId: ids.command(7),
        connectionId: database.connectionId,
        gattSessionId: database.gattSessionId,
        subscriptionId: subscriptionId,
        characteristicId: characteristic.id,
        action: .disable
    )
    try fixture.setNotifications(command: disable)
    let disableDeadline = Date().addingTimeInterval(10)
    var disableCompletedIndex: Int?
    while Date() < disableDeadline {
        let events = Array(fixture.snapshot().events.dropFirst(ledgerOffset))
        guard let completedIndex = events.firstIndex(where: { event in
            guard case .commandCompleted(let commandId, _) = event else {
                return false
            }
            return commandId == disable.commandId
        }) else {
            waitForCallbacks()
            continue
        }
        if case .commandCompleted(_, let error) = events[completedIndex], let error {
            throw ProbeError.notificationFailed("disable", error)
        }
        guard let stateIndex = events.firstIndex(where: { event in
            guard case .notificationStateChanged(
                let commandId,
                let connectionId,
                let gattSessionId,
                let observedSubscriptionId,
                let characteristicId,
                let state
            ) = event else {
                return false
            }
            return commandId == disable.commandId &&
                connectionId == disable.connectionId &&
                gattSessionId == disable.gattSessionId &&
                observedSubscriptionId == disable.subscriptionId &&
                characteristicId == disable.characteristicId &&
                state == .disabled
        }), stateIndex < completedIndex else {
            waitForCallbacks()
            continue
        }
        disableCompletedIndex = completedIndex
        break
    }
    guard let disableCompletedIndex else {
        throw ProbeError.notificationTimeout(
            "disable",
            Array(fixture.snapshot().events.dropFirst(ledgerOffset))
        )
    }

    try signalProbeHost(path: fixtureCoordinationPath, marker: "notification-disabled")
    try waitForProbeHost(
        path: fixtureCoordinationPath,
        marker: "post-disable-triggered",
        timeout: 60
    )
    Thread.sleep(forTimeInterval: 2)
    let finalEvents = Array(fixture.snapshot().events.dropFirst(ledgerOffset))
    let postDisableEvents = finalEvents.dropFirst(disableCompletedIndex + 1)
    guard !postDisableEvents.contains(where: { event in
        if case .notificationReceived = event {
            return true
        }
        return false
    }) else {
        throw ProbeError.invalidNotification("received a value after disable completed")
    }
    try validateNotificationEventLedger(
        finalEvents,
        database: database,
        subscriptionId: subscriptionId,
        characteristicId: characteristic.id,
        expectedValue: expectedValue,
        ids: ids,
        expectedMode: expectedMode
    )

    return (
        output: [
            "apple_macos_corebluetooth_notification_actual_mode=\(strictIndication ? "indication" : "notification")",
            "apple_macos_corebluetooth_notification_value=\(hexValue(expectedValue))",
            "apple_macos_corebluetooth_notification_enable_disable_order=passed",
            "apple_macos_corebluetooth_notification_post_disable_silence=passed",
            "apple_macos_corebluetooth_notification=passed",
        ],
        ledgerOffset: ledgerOffset,
        subscriptionId: subscriptionId,
        characteristicId: characteristic.id,
        expectedValue: expectedValue
    )
}

func validateNotificationEventLedger(
    _ events: [BleEvent],
    database: GattDatabase,
    subscriptionId: SubscriptionId,
    characteristicId: GattCharacteristicId,
    expectedValue: Data,
    ids: RfCycleIds = RfCycleIds(),
    expectedMode: NotificationMode = .notification,
    expectDisable: Bool = true,
    expectValue: Bool = true
) throws {
    let enableId = ids.command(6)
    let disableId = ids.command(7)
    let commands = expectDisable ? [(enableId, true), (disableId, false)] : [(enableId, true)]
    for (commandId, enabled) in commands {
        let terminals = events.enumerated().compactMap { index, event -> (Int, BleEventError?)? in
            guard case .commandCompleted(let observedCommandId, let error) = event,
                  observedCommandId == commandId
            else {
                return nil
            }
            return (index, error)
        }
        guard terminals.count == 1, terminals[0].1 == nil else {
            throw ProbeError.invalidNotification(
                "command \(commandId.value) produced \(terminals.count) terminals or a failed terminal"
            )
        }
        let states = events.enumerated().compactMap {
            index, event -> (
                Int,
                ConnectionId,
                GattSessionId,
                SubscriptionId,
                GattCharacteristicId,
                NotificationState
            )? in
            guard case .notificationStateChanged(
                let observedCommandId,
                let connectionId,
                let gattSessionId,
                let observedSubscriptionId,
                let observedCharacteristicId,
                let state
            ) = event,
                observedCommandId == commandId
            else {
                return nil
            }
            return (
                index,
                connectionId,
                gattSessionId,
                observedSubscriptionId,
                observedCharacteristicId,
                state
            )
        }
        guard states.count == 1,
              states[0].0 < terminals[0].0,
              states[0].1 == database.connectionId,
              states[0].2 == database.gattSessionId,
              states[0].3 == subscriptionId,
              states[0].4 == characteristicId
        else {
            throw ProbeError.invalidNotification(
                "command \(commandId.value) state count or event order was invalid"
            )
        }
        if enabled {
            guard case .enabled(let actualMode) = states[0].5,
                  actualMode == expectedMode
            else {
                throw ProbeError.invalidNotification("enable did not report expected mode \(expectedMode)")
            }
        } else if states[0].5 != .disabled {
            throw ProbeError.invalidNotification("disable did not report Disabled state")
        }
    }

    if !expectValue {
        guard !events.contains(where: { if case .notificationReceived = $0 { return true }; return false }) else {
            throw ProbeError.invalidNotification("value arrived before host trigger")
        }
        return
    }
    let values = events.compactMap {
        event -> (ConnectionId, GattSessionId, SubscriptionId, GattCharacteristicId, Data)? in
        guard case .notificationReceived(
            let connectionId,
            let gattSessionId,
            let observedSubscriptionId,
            let observedCharacteristicId,
            let value
        ) = event else {
            return nil
        }
        return (
            connectionId,
            gattSessionId,
            observedSubscriptionId,
            observedCharacteristicId,
            value
        )
    }
    guard values.count == 1,
          values[0].0 == database.connectionId,
          values[0].1 == database.gattSessionId,
          values[0].2 == subscriptionId,
          values[0].3 == characteristicId,
          values[0].4 == expectedValue
    else {
        throw ProbeError.invalidNotification(
            "expected exactly one correctly correlated \(hexValue(expectedValue)) value"
        )
    }
    let valueIndex = events.firstIndex { if case .notificationReceived = $0 { return true }; return false }!
    let enableIndex = events.firstIndex {
        if case .commandCompleted(let id, _) = $0 { return id == enableId }; return false
    }!
    let disableIndex = events.firstIndex {
        if case .notificationStateChanged(let id, _, _, _, _, _) = $0 { return id == disableId }; return false
    } ?? events.count
    guard enableIndex < valueIndex, valueIndex < disableIndex else {
        throw ProbeError.invalidNotification("value was outside the enabled subscription interval")
    }
}

private enum ExpectedGattIoDomain {
    case read
    case write
}

func validateGattIoEventLedger(
    _ events: [BleEvent],
    database: GattDatabase,
    characteristicId: GattCharacteristicId,
    configuration: RfFixtureConfiguration = .android
) throws {
    let readValues: [UInt64: Data] = try [
        6: configuration.initialValue, 8: configuration.writeWithResponse, 10: configuration.writeWithoutResponse,
    ]
    let expected: [(UInt64, ExpectedGattIoDomain)] = [
        (6, .read),
        (7, .write),
        (8, .read),
        (9, .write),
        (10, .read),
    ]
    for (commandValue, expectedDomain) in expected {
        let commandId = CommandId(value: commandValue)
        let terminals = events.enumerated().compactMap {
            (index, event) -> (Int, BleEventError?)? in
            guard case .commandCompleted(let observedCommandId, let error) = event,
                  observedCommandId == commandId
            else {
                return nil
            }
            return (index, error)
        }
        guard terminals.count == 1, terminals[0].1 == nil else {
            throw ProbeError.invalidGattValue(
                "command \(commandValue) produced \(terminals.count) terminal events or a failed terminal"
            )
        }

        let domains = events.enumerated().compactMap {
            (index, event) -> (
                Int,
                ExpectedGattIoDomain,
                ConnectionId,
                GattSessionId,
                GattCharacteristicId
            )? in
            switch event {
            case .characteristicRead(
                let observedCommandId,
                let connectionId,
                let gattSessionId,
                let observedCharacteristicId,
                _
            ) where observedCommandId == commandId:
                return (
                    index,
                    ExpectedGattIoDomain.read,
                    connectionId,
                    gattSessionId,
                    observedCharacteristicId
                )
            case .characteristicWritten(
                let observedCommandId,
                let connectionId,
                let gattSessionId,
                let observedCharacteristicId
            ) where observedCommandId == commandId:
                return (
                    index,
                    ExpectedGattIoDomain.write,
                    connectionId,
                    gattSessionId,
                    observedCharacteristicId
                )
            default:
                return nil
            }
        }
        guard domains.count == 1 else {
            throw ProbeError.invalidGattValue(
                "command \(commandValue) produced \(domains.count) characteristic domain events"
            )
        }
        let domain = domains[0]
        if case .characteristicRead(_, _, _, _, let value) = events[domain.0],
           value != readValues[commandValue] {
            throw ProbeError.invalidGattValue("command \(commandValue) readback differs from configured payload")
        }
        guard domain.1 == expectedDomain,
              domain.2 == database.connectionId,
              domain.3 == database.gattSessionId,
              domain.4 == characteristicId,
              domain.0 < terminals[0].0
        else {
            throw ProbeError.invalidGattValue(
                "command \(commandValue) domain correlation or event order was invalid"
            )
        }
    }
}

func runConnection(
    fixture: SwiftProbeHarness,
    peripheralId: PeripheralId,
    discoverGattServiceUuid: String?,
    runGattIoAfterDiscovery: Bool,
    runNotificationsAfterDiscovery: Bool,
    fixtureCoordinationPath: String?,
    ids: RfCycleIds = RfCycleIds(),
    rfScenario: AppleRfScenario? = nil,
    configuration: RfFixtureConfiguration = .android,
    externalProvider: String? = nil
) throws -> [String] {
    let connect = ConnectCommand(
        commandId: ids.command(4),
        connectionId: ids.connection,
        peripheralId: peripheralId
    )
    let connectOffset = fixture.snapshot().events.count
    try fixture.connect(command: connect)
    let connectDeadline = Date().addingTimeInterval(30)
    while Date() < connectDeadline {
        let events = Array(fixture.snapshot().events.dropFirst(connectOffset))
        let connecting = events.contains {
            connectionState(
                .connecting,
                commandId: connect.commandId,
                connectionId: connect.connectionId,
                event: $0
            )
        }
        let connected = events.contains {
            connectionState(
                .connected,
                commandId: connect.commandId,
                connectionId: connect.connectionId,
                event: $0
            )
        }
        if connecting, connected, commandSucceeded(connect.commandId, in: events) {
            break
        }
        waitForCallbacks()
    }
    let connectEvents = Array(fixture.snapshot().events.dropFirst(connectOffset))
    guard connectEvents.contains(where: {
        connectionState(
            .connecting,
            commandId: connect.commandId,
            connectionId: connect.connectionId,
            event: $0
        )
    }), connectEvents.contains(where: {
        connectionState(
            .connected,
            commandId: connect.commandId,
            connectionId: connect.connectionId,
            event: $0
        )
    }), commandSucceeded(connect.commandId, in: connectEvents) else {
        throw ProbeError.connectTimeout(connectEvents)
    }

    var output = [
        "apple_macos_corebluetooth_connection_peripheral_id=\(peripheralId.value)",
        "apple_macos_corebluetooth_connecting=passed",
        "apple_macos_corebluetooth_connected=passed",
    ]
    var gattIoLedgerOffset: Int?
    var gattIoDatabase: GattDatabase?
    var gattIoCharacteristicId: GattCharacteristicId?
    var notificationLedgerOffset: Int?
    var notificationDatabase: GattDatabase?
    var notificationSubscriptionId: SubscriptionId?
    var notificationCharacteristicId: GattCharacteristicId?
    var notificationExpectedValue: Data?
    if let discoverGattServiceUuid {
        let discovery = try runGattDiscovery(
            fixture: fixture,
            connectionId: connect.connectionId,
            expectedServiceUuid: discoverGattServiceUuid,
            ids: ids,
            configuration: configuration
        )
        output.append(contentsOf: discovery.output)
        if runGattIoAfterDiscovery {
            let characteristicUuid = BluetoothUuid(
                value: "fff10000-0000-4000-8000-000000000000"
            )
            guard let service = discovery.database.services.first(where: {
                $0.uuid.value == discoverGattServiceUuid.lowercased()
            }), let characteristic = service.characteristics.first(where: {
                $0.uuid == characteristicUuid
            }) else {
                throw ProbeError.invalidGattDatabase("read/write characteristic is missing")
            }
            gattIoLedgerOffset = fixture.snapshot().events.count
            gattIoDatabase = discovery.database
            gattIoCharacteristicId = characteristic.id
            output.append(
                contentsOf: try runGattIo(
                    fixture: fixture,
                    database: discovery.database,
                    expectedServiceUuid: discoverGattServiceUuid,
                    configuration: configuration
                )
            )
        } else if runNotificationsAfterDiscovery {
            if rfScenario == .indication {
                try runStrictIndicationRejection(fixture: fixture, database: discovery.database,
                                               serviceUuid: discoverGattServiceUuid, ids: ids)
                output.append("apple_macos_corebluetooth_indication_dual_property_not_supported=passed")
            }
            let notification = try runNotifications(
                fixture: fixture,
                database: discovery.database,
                expectedServiceUuid: discoverGattServiceUuid,
                fixtureCoordinationPath: fixtureCoordinationPath,
                ids: ids,
                strictIndication: rfScenario == .indication,
                leaveActive: rfScenario == .passiveDisconnect ||
                    (rfScenario == .recovery && externalProvider == "nrf52840"),
                configuration: configuration
            )
            output.append(contentsOf: notification.output)
            notificationLedgerOffset = notification.ledgerOffset
            notificationDatabase = discovery.database
            notificationSubscriptionId = notification.subscriptionId
            notificationCharacteristicId = notification.characteristicId
            notificationExpectedValue = notification.expectedValue
        }
    }

    if rfScenario == .recovery && externalProvider == "nrf52840" {
        try signalProbeHost(path: fixtureCoordinationPath, marker: "connected")
        try waitForProbeHost(path: fixtureCoordinationPath, marker: "fault-triggered", timeout: 60)
        let deadline = Date().addingTimeInterval(60)
        while Date() < deadline {
            if fixture.snapshot().events.contains(where: {
                isRfPassiveDisconnect($0, connectionId: ids.connection)
            }) {
                break
            }
            waitForCallbacks()
        }
        guard fixture.snapshot().events.contains(where: {
            isRfPassiveDisconnect($0, connectionId: ids.connection)
        }) else {
            throw ProbeError.disconnectTimeout(fixture.snapshot().events)
        }
        try signalProbeHost(path: fixtureCoordinationPath, marker: "passive-disconnected")
        try waitForProbeHost(path: fixtureCoordinationPath, marker: "recovery-ready", timeout: 90)
        Thread.sleep(forTimeInterval: 2)
        return output + [
            "apple_macos_corebluetooth_passive_disconnect=passed",
            "apple_macos_corebluetooth_passive_disconnect_silence=passed",
        ]
    }

    if rfScenario == .rssi {
        let command = ReadRssiCommand(commandId: ids.command(12), connectionId: connect.connectionId)
        try fixture.readRssi(command: command)
        try waitForRfTerminal(fixture: fixture, commandId: command.commandId, operation: "RSSI", timeout: 15)
        let rssi = try validateRssiLedger(fixture.snapshot().events, command: command)
        output.append("apple_macos_corebluetooth_rssi_value=\(rssi)")
        output.append("apple_macos_corebluetooth_rssi=passed")
    }
    if rfScenario == .passiveDisconnect {
        // runNotifications returned only after enable + one exact, correlated payload,
        // without disabling. The host must cut the Android radio, not cancelConnection.
        try signalProbeHost(path: fixtureCoordinationPath, marker: "radio-off-ready")
        try waitForProbeHost(path: fixtureCoordinationPath, marker: "radio-off-confirmed", timeout: 60)
        let deadline = Date().addingTimeInterval(60)
        while Date() < deadline {
            if fixture.snapshot().events.contains(where: { isRfPassiveDisconnect($0, connectionId: ids.connection) }) {
                break
            }
            waitForCallbacks()
        }
        guard fixture.snapshot().events.contains(where: { isRfPassiveDisconnect($0, connectionId: ids.connection) }) else {
            throw ProbeError.disconnectTimeout(fixture.snapshot().events)
        }
        try signalProbeHost(path: fixtureCoordinationPath, marker: "passive-disconnected")
        try waitForProbeHost(path: fixtureCoordinationPath, marker: "radio-restored", timeout: 90)
        Thread.sleep(forTimeInterval: 2)
        return output + [
            "apple_macos_corebluetooth_passive_disconnect=passed",
            "apple_macos_corebluetooth_passive_disconnect_silence=passed",
        ]
    }

    let disconnectCommandId: UInt64
    if runGattIoAfterDiscovery {
        disconnectCommandId = 11
    } else if runNotificationsAfterDiscovery {
        disconnectCommandId = 8
    } else if discoverGattServiceUuid != nil {
        disconnectCommandId = 6
    } else {
        disconnectCommandId = 5
    }
    let disconnect = DisconnectCommand(
        commandId: ids.command(disconnectCommandId),
        connectionId: connect.connectionId
    )
    try fixture.disconnect(command: disconnect)
    let disconnectDeadline = Date().addingTimeInterval(10)
    var disconnectedEventIndex: Int?
    while Date() < disconnectDeadline {
        let events = fixture.snapshot().events
        if commandSucceeded(disconnect.commandId, in: events),
           let index = events.firstIndex(where: {
               connectionState(
                   .disconnected,
                   commandId: disconnect.commandId,
                   connectionId: disconnect.connectionId,
                   event: $0
               )
           })
        {
            disconnectedEventIndex = index
            break
        }
        waitForCallbacks()
    }
    guard let disconnectedEventIndex else {
        throw ProbeError.disconnectTimeout(fixture.snapshot().events)
    }

    Thread.sleep(forTimeInterval: 1)
    let finalEvents = fixture.snapshot().events
    let eventsAfterDisconnect = finalEvents.dropFirst(disconnectedEventIndex + 1)
    if eventsAfterDisconnect.contains(where: { event in
        if case .connectionStateChanged(_, let connectionId, _) = event {
            return connectionId == disconnect.connectionId
        }
        return false
    }) {
        throw ProbeError.postDisconnectConnectionState
    }
    if eventsAfterDisconnect.contains(where: { event in
        switch event {
        case .gattDiscovered(_, let database):
            return database.connectionId == disconnect.connectionId
        case .characteristicRead(_, let connectionId, _, _, _),
             .characteristicWritten(_, let connectionId, _, _),
             .notificationStateChanged(_, let connectionId, _, _, _, _),
             .notificationReceived(let connectionId, _, _, _, _):
            return connectionId == disconnect.connectionId
        default:
            return false
        }
    }) {
        throw ProbeError.postDisconnectGattEvent
    }
    if let gattIoLedgerOffset, let gattIoDatabase, let gattIoCharacteristicId {
        try validateGattIoEventLedger(
            Array(finalEvents.dropFirst(gattIoLedgerOffset)),
            database: gattIoDatabase,
            characteristicId: gattIoCharacteristicId,
            configuration: configuration
        )
        output.append("apple_macos_corebluetooth_gatt_io_event_order=passed")
        output.append("apple_macos_corebluetooth_gatt_io=passed")
    }
    if let notificationLedgerOffset,
       let notificationDatabase,
       let notificationSubscriptionId,
       let notificationCharacteristicId,
       let notificationExpectedValue
    {
        try validateNotificationEventLedger(
            Array(finalEvents.dropFirst(notificationLedgerOffset)),
            database: notificationDatabase,
            subscriptionId: notificationSubscriptionId,
            characteristicId: notificationCharacteristicId,
            expectedValue: notificationExpectedValue,
            ids: ids,
            expectedMode: rfScenario == .indication ? .indication : .notification
        )
    }

    output.append(contentsOf: [
        "apple_macos_corebluetooth_logical_disconnect=passed",
        "apple_macos_corebluetooth_post_disconnect_silence=passed",
        "apple_macos_corebluetooth_connection=passed",
    ])
    return output
}

func runProbe(
    waitForAuthorization: Bool,
    scanServiceUuid: String?,
    connectAfterScan: Bool,
    discoverGattAfterConnect: Bool,
    runGattIoAfterDiscovery: Bool,
    runNotificationsAfterDiscovery: Bool,
    fixtureCoordinationPath: String?
) throws -> [String] {
    let backend = try AppleBleBackend(
        queueLabel: "dev.blehub.apple.macos-initialization-probe"
    )
    let fixture = try SwiftProbeHarness(backend: backend)
    defer {
        try? fixture.dispose()
    }

    let commandId = CommandId(value: 1)
    try fixture.initialize(command: InitializeCommand(commandId: commandId))

    let needsAuthorization = waitForAuthorization || scanServiceUuid != nil
    let initializationDeadline = Date().addingTimeInterval(needsAuthorization ? 120 : 30)
    var initialSnapshot: AdapterSnapshot?
    while Date() < initializationDeadline {
        let events = fixture.snapshot().events
        if events.count >= 2 {
            guard case .adapterSnapshotChanged(let observedCommandId, let snapshot) = events[0],
                  observedCommandId == commandId,
                  case .commandCompleted(let completedCommandId, let error) = events[1],
                  completedCommandId == commandId,
                  error == nil
            else {
                throw ProbeError.unexpectedEvents(events)
            }
            initialSnapshot = snapshot
            break
        }
        waitForCallbacks()
    }
    guard let initialSnapshot else {
        throw ProbeError.initializationTimeout
    }

    var output = [
        "apple_macos_corebluetooth_adapter_state=\(initialSnapshot.state)",
        "apple_macos_corebluetooth_scan_authorization=\(initialSnapshot.scanAuthorization)",
        "apple_macos_corebluetooth_connection_authorization=\(initialSnapshot.connectionAuthorization)",
        "apple_macos_corebluetooth_initialize=passed",
    ]
    guard needsAuthorization else {
        return output
    }

    let authorizationDeadline = Date().addingTimeInterval(120)
    var authorizedSnapshot: AdapterSnapshot?
    while Date() < authorizationDeadline {
        if let snapshot = latestAdapterSnapshot(in: fixture.snapshot().events) {
            switch snapshot.scanAuthorization {
            case .authorized:
                authorizedSnapshot = snapshot
            case .denied, .restricted:
                throw ProbeError.authorizationRejected(snapshot.scanAuthorization)
            case .notDetermined, .notRequired:
                break
            }
        }
        if authorizedSnapshot != nil {
            break
        }
        waitForCallbacks()
    }
    guard let authorizedSnapshot else {
        throw ProbeError.authorizationTimeout
    }
    output.append(
        "apple_macos_corebluetooth_authorized_adapter_state=\(authorizedSnapshot.state)"
    )
    output.append("apple_macos_corebluetooth_authorization=passed")

    if let scanServiceUuid {
        guard authorizedSnapshot.state == .poweredOn else {
            throw ProbeError.scanUnavailable(authorizedSnapshot)
        }
        let scan = try runScan(fixture: fixture, serviceUuid: scanServiceUuid)
        output.append(contentsOf: scan.output)
        if connectAfterScan {
            output.append(
                contentsOf: try runConnection(
                    fixture: fixture,
                    peripheralId: scan.advertisement.peripheralId,
                    discoverGattServiceUuid: discoverGattAfterConnect ? scanServiceUuid : nil,
                    runGattIoAfterDiscovery: runGattIoAfterDiscovery,
                    runNotificationsAfterDiscovery: runNotificationsAfterDiscovery,
                    fixtureCoordinationPath: fixtureCoordinationPath
                )
            )
        }
    }
    return output
}

func executeProbe(
    waitForAuthorization: Bool,
    scanServiceUuid: String?,
    connectAfterScan: Bool,
    discoverGattAfterConnect: Bool,
    runGattIoAfterDiscovery: Bool,
    runNotificationsAfterDiscovery: Bool,
    fixtureCoordinationPath: String?,
    resultPath: String?
) -> Never {
    do {
        try signalProbeHost(
            path: fixtureCoordinationPath,
            marker: "probe-pid",
            value: "\(getpid())\n"
        )
        let output = try runProbe(
            waitForAuthorization: waitForAuthorization,
            scanServiceUuid: scanServiceUuid,
            connectAfterScan: connectAfterScan,
            discoverGattAfterConnect: discoverGattAfterConnect,
            runGattIoAfterDiscovery: runGattIoAfterDiscovery,
            runNotificationsAfterDiscovery: runNotificationsAfterDiscovery,
            fixtureCoordinationPath: fixtureCoordinationPath
        )
        let text = output.joined(separator: "\n") + "\n"
        print(text, terminator: "")
        if let resultPath {
            try text.write(toFile: resultPath, atomically: true, encoding: .utf8)
        }
        exit(EXIT_SUCCESS)
    } catch {
        let text = "apple_macos_corebluetooth_probe=failed: \(error)\n"
        fputs(text, stderr)
        if let resultPath {
            try? text.write(toFile: resultPath, atomically: true, encoding: .utf8)
        }
        exit(EXIT_FAILURE)
    }
}
