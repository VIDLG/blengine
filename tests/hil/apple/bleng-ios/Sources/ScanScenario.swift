import BleHubUniFfi
import Foundation

enum ScanProbeFailure: Error {
    case invalid(String)
}

actor ScanEvidence {
    private let service: BluetoothUuid
    private var started: [ScanSessionId] = []
    private var stopped: [ScanSessionId] = []
    private var matching: [AdvertisementObservation] = []
    private var afterStop = false
    private var connectionStates: [(CommandId?, ConnectionId, ConnectionState)] = []
    private var connectionEvents: [(index: Int, event: BleEvent)] = []
    private var disconnectIndices: [ConnectionId: Int] = [:]
    private var databases: [GattDatabase] = []
    private var rssiEvents: [(CommandId, ConnectionId, Int16)] = []
    private var notificationEventIndex = 0
    private var notificationStates: [(
        index: Int, command: CommandId, connection: ConnectionId, session: GattSessionId,
        subscription: SubscriptionId, characteristic: GattCharacteristicId, state: NotificationState
    )] = []
    private var notificationValues: [(
        index: Int, connection: ConnectionId, session: GattSessionId,
        subscription: SubscriptionId, characteristic: GattCharacteristicId, value: Data
    )] = []
    private var commandCompletions: [(index: Int, command: CommandId, failed: Bool)] = []

    init(service: BluetoothUuid) { self.service = service }

    func record(_ event: BleEvent) {
        let eventIndex = notificationEventIndex
        notificationEventIndex += 1
        switch event {
        case .scanStarted(_, let session):
            started.append(session)
        case .advertisementObserved(let advertisement):
            if stopped.contains(advertisement.scanSessionId) { afterStop = true }
            if advertisement.advertisedServices.contains(service) {
                matching.append(advertisement)
            }
        case .scanStopped(let session, let result):
            if case .ok(.requested(_)) = result { stopped.append(session) }
        case .connectionStateChanged(let command, let connection, let result):
            connectionEvents.append((index: eventIndex, event: event))
            switch result {
            case .ok(let state):
                connectionStates.append((command, connection, state))
                if state == .disconnected && disconnectIndices[connection] == nil {
                    disconnectIndices[connection] = eventIndex
                }
            case .err(let error):
                if error.category == .connection && disconnectIndices[connection] == nil {
                    disconnectIndices[connection] = eventIndex
                }
            }
        case .gattDiscovered(_, let database):
            databases.append(database)
        case .rssiRead(let command, let connection, let value):
            rssiEvents.append((command, connection, value))
        case .notificationStateChanged(
            let command, let connection, let session, let subscription, let characteristic, let state
        ):
            notificationStates.append((
                index: eventIndex, command: command, connection: connection, session: session,
                subscription: subscription, characteristic: characteristic, state: state
            ))
        case .notificationReceived(
            let connection, let session, let subscription, let characteristic, let value
        ):
            notificationValues.append((
                index: eventIndex, connection: connection, session: session,
                subscription: subscription, characteristic: characteristic, value: value
            ))
        case .commandCompleted(let command, let error):
            commandCompletions.append((index: eventIndex, command: command, failed: error != nil))
        default:
            break
        }
    }

    func advertisement(for session: ScanSessionId) -> AdvertisementObservation? {
        guard started.contains(session) else { return nil }
        return matching.first { $0.scanSessionId == session }
    }

    func completedStop(for session: ScanSessionId) -> Bool { stopped.contains(session) }
    func hasPostStopAdvertisement() -> Bool { afterStop }

    func connected(_ connection: ConnectionId) -> Bool {
        let states = connectionStates.filter { $0.1 == connection }
        guard states.count >= 2,
              let command = states[0].0,
              states[0].2 == .connecting,
              states[1].0 == command,
              states[1].2 == .connected
        else { return false }
        return true
    }

    func database(connection: ConnectionId, session: GattSessionId) -> GattDatabase? {
        databases.first { $0.connectionId == connection && $0.gattSessionId == session }
    }

    func observedRssi(connection: ConnectionId, value: Int16) -> Bool {
        let events = rssiEvents.filter { $0.1 == connection }
        return events.count == 1 && events[0].2 == value
    }

    func activelyDisconnected(_ connection: ConnectionId) -> Bool {
        let states = connectionStates.filter { $0.1 == connection }
        guard connected(connection), states.count == 3,
              let connectCommand = states[0].0,
              let disconnectCommand = states[2].0,
              disconnectCommand != connectCommand,
              states[2].2 == .disconnected
        else { return false }
        return true
    }

    func passiveTerminal(
        _ connection: ConnectionId, after valueIndex: Int
    ) -> (index: Int, result: String, errorCategory: String?)? {
        let events = connectionEvents.filter {
            guard case .connectionStateChanged(_, let observed, _) = $0.event else { return false }
            return observed == connection
        }
        guard connected(connection), events.count == 3,
              case .connectionStateChanged(nil, _, let result) = events[2].event,
              events[2].index > valueIndex else { return nil }
        switch result {
        case .ok(.disconnected):
            return (events[2].index, "disconnected", nil)
        case .err(let error) where error.category == .connection:
            return (events[2].index, "connection-error", String(describing: error.category))
        default:
            return nil
        }
    }

    func connectionLifecycleDescription(_ connection: ConnectionId) -> String {
        connectionEvents.compactMap { entry in
            guard case .connectionStateChanged(let command, let observed, let result) = entry.event,
                  observed == connection else { return nil }
            let commandDescription = command.map { String($0.value) } ?? "nil"
            return "index=\(entry.index) command=\(commandDescription) result=\(result)"
        }.joined(separator: "; ")
    }

    func hasLateValueRoutes() -> Bool {
        notificationValues.contains { value in
            guard let index = disconnectIndices[value.connection] else { return false }
            return value.index > index
        }
    }

    func notificationEnabled(
        connection: ConnectionId, session: GattSessionId, subscription: SubscriptionId,
        characteristic: GattCharacteristicId, mode modeExpected: NotificationMode
    ) -> (index: Int, command: CommandId)? {
        guard let event = notificationStates.first(where: {
            guard $0.connection == connection, $0.session == session,
                  $0.subscription == subscription, $0.characteristic == characteristic,
                  case .enabled(let mode) = $0.state else { return false }
            return mode == modeExpected
        }) else { return nil }
        return (event.index, event.command)
    }

    func notificationDisabled(
        connection: ConnectionId, session: GattSessionId, subscription: SubscriptionId,
        characteristic: GattCharacteristicId
    ) -> (index: Int, command: CommandId)? {
        guard let event = notificationStates.first(where: {
            $0.connection == connection && $0.session == session &&
                $0.subscription == subscription && $0.characteristic == characteristic &&
                $0.state == .disabled
        }) else { return nil }
        return (event.index, event.command)
    }

    func commandCompletion(
        _ command: CommandId, after index: Int
    ) -> (index: Int, failed: Bool)? {
        guard let event = commandCompletions.first(where: {
            $0.command == command && $0.index > index
        }) else { return nil }
        return (event.index, event.failed)
    }

    func notificationValue(
        after index: Int, connection: ConnectionId, session: GattSessionId,
        subscription: SubscriptionId, characteristic: GattCharacteristicId
    ) -> (index: Int, value: Data)? {
        guard let event = notificationValues.first(where: {
            $0.index > index && $0.connection == connection && $0.session == session &&
                $0.subscription == subscription && $0.characteristic == characteristic
        }) else { return nil }
        return (event.index, event.value)
    }

    func hasSubscriptionActivity(characteristic: GattCharacteristicId) -> Bool {
        notificationStates.contains { $0.characteristic == characteristic } ||
            notificationValues.contains { $0.characteristic == characteristic }
    }

    func hasNotificationAfter(
        _ index: Int, connection: ConnectionId, session: GattSessionId,
        subscription: SubscriptionId, characteristic: GattCharacteristicId
    ) -> Bool {
        notificationValues.contains {
            $0.index > index && $0.connection == connection && $0.session == session &&
                $0.subscription == subscription && $0.characteristic == characteristic
        }
    }
}

func awaitScanEvidence<T>(
    _ label: String, seconds: Int, check: () async -> T?
) async throws -> T {
    for _ in 0..<(seconds * 10) {
        if let value = await check() { return value }
        try await Task.sleep(nanoseconds: 100_000_000)
    }
    throw ScanProbeFailure.invalid("timed out waiting for \(label)")
}

func validateFixtureDatabase(
    _ database: GattDatabase, service: BluetoothUuid, indicationOnly: Bool
) throws -> Int {
    let services = database.services.filter { $0.uuid == service && $0.primary == true }
    guard services.count == 1 else {
        throw ScanProbeFailure.invalid("expected one primary fixture service")
    }
    let fixture = services[0]
    let expected: [String: CharacteristicProperties] = [
        "fff10000-0000-4000-8000-000000000000": CharacteristicProperties(
            readable: true, writableWithResponse: true, writableWithoutResponse: true,
            notifiable: false, indicatable: false
        ),
        "fff20000-0000-4000-8000-000000000000": CharacteristicProperties(
            readable: true, writableWithResponse: false, writableWithoutResponse: false,
            notifiable: true, indicatable: true
        ),
        "fff30000-0000-4000-8000-000000000000": CharacteristicProperties(
            readable: true, writableWithResponse: false, writableWithoutResponse: false,
            notifiable: false, indicatable: false
        ),
        "fff40000-0000-4000-8000-000000000000": CharacteristicProperties(
            readable: false, writableWithResponse: true, writableWithoutResponse: true,
            notifiable: false, indicatable: false
        ),
        "fff50000-0000-4000-8000-000000000000": CharacteristicProperties(
            readable: true, writableWithResponse: false, writableWithoutResponse: false,
            notifiable: false, indicatable: true
        ),
    ]
    let expectedCount = indicationOnly ? 5 : 4
    guard fixture.characteristics.count == expectedCount else {
        throw ScanProbeFailure.invalid("fixture characteristic count mismatch")
    }
    let cccd = BluetoothUuid(value: "00002902-0000-1000-8000-00805f9b34fb")
    var cccdCount = 0
    for characteristic in fixture.characteristics {
        guard let properties = expected[characteristic.uuid.value],
              (indicationOnly || characteristic.uuid.value != "fff50000-0000-4000-8000-000000000000"),
              characteristic.properties == properties else {
            throw ScanProbeFailure.invalid("unexpected fixture characteristic or properties")
        }
        if characteristic.uuid.value == "fff20000-0000-4000-8000-000000000000"
            || characteristic.uuid.value == "fff50000-0000-4000-8000-000000000000" {
            guard characteristic.descriptors.contains(where: { $0.uuid == cccd }) else {
                throw ScanProbeFailure.invalid("fixture subscription characteristic lacks CCCD")
            }
            cccdCount += 1
        }
    }
    let serviceIds = database.services.map(\.id)
    let characteristicIds = database.services.flatMap { $0.characteristics.map(\.id) }
    let descriptorIds = database.services.flatMap {
        $0.characteristics.flatMap { $0.descriptors.map(\.id) }
    }
    guard Set(fixture.characteristics.map { $0.uuid.value }).count == expectedCount,
          Set(serviceIds).count == serviceIds.count,
          Set(characteristicIds).count == characteristicIds.count,
          Set(descriptorIds).count == descriptorIds.count else {
        throw ScanProbeFailure.invalid("duplicate GATT UUID or opaque attribute ID")
    }
    return cccdCount
}

struct RfIoInputs {
    let initial: String
    let withResponse: String
    let withoutResponse: String
}

struct RfOutcome {
    let snapshot: AdapterSnapshot
    let advertisement: AdvertisementObservation
    let connection: ConnectionId?
    let gatt: GattSessionId?
    let cccdCount: Int?
    let io: RfIoInputs?
    let rssi: Int16?
}

struct SubscriptionRfOutcome {
    let snapshot: AdapterSnapshot
    let advertisement: AdvertisementObservation
    let connection: ConnectionId
    let gatt: GattSessionId
    let cccdCount: Int
    let subscription: SubscriptionId
    let valueHex: String
    let actualMode: NotificationMode
}

private let rfMarkerNames = [
    "notification-enabled", "notification-disabled", "indication-enabled",
    "indication-disabled", "post-disable-triggered"
]

func prepareBlengRfRun(runId: String) throws -> URL {
    let documents = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
    let directory = documents.appendingPathComponent("bleng-rf-\(runId)", isDirectory: true)
    guard !FileManager.default.fileExists(atPath: directory.path) else {
        throw ScanProbeFailure.invalid("refusing to reuse BLeng RF run directory")
    }
    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false)
    return directory
}

func writeBlengRfMarker(_ directory: URL, marker: String) throws {
    guard rfMarkerNames.contains(marker) else {
        throw ScanProbeFailure.invalid("invalid BLeng RF marker")
    }
    try Data("ready\n".utf8).write(
        to: directory.appendingPathComponent(marker), options: .atomic
    )
}

private func waitForBlengRfMarker(
    _ directory: URL, marker: String, seconds: Int
) async throws {
    guard rfMarkerNames.contains(marker) else {
        throw ScanProbeFailure.invalid("invalid BLeng RF marker")
    }
    _ = try await awaitScanEvidence("BLeng RF marker \(marker)", seconds: seconds) {
        FileManager.default.fileExists(atPath: directory.appendingPathComponent(marker).path)
            ? true : nil
    }
}

private func hexValue(_ data: Data) -> String {
    data.map { String(format: "%02x", $0) }.joined()
}

private func bytes(_ hex: String) throws -> Data {
    let source = Array(hex.utf8)
    guard !source.isEmpty, source.count.isMultiple(of: 2),
          source.allSatisfy({ (48...57).contains($0) || (65...70).contains($0) || (97...102).contains($0) }) else {
        throw ScanProbeFailure.invalid("invalid fixture value hex")
    }
    return Data(stride(from: 0, to: source.count, by: 2).map { offset in
        UInt8(String(decoding: source[offset..<(offset + 2)], as: UTF8.self), radix: 16)!
    })
}

private func verifyIo(
    owner: Bleng, database: GattDatabase, service: BluetoothUuid, inputs: RfIoInputs
) async throws {
    guard let characteristic = database.services.first(where: { $0.uuid == service })?
        .characteristics.first(where: {
            $0.uuid.value == "fff10000-0000-4000-8000-000000000000"
        }) else {
        throw ScanProbeFailure.invalid("fixture read/write characteristic is missing")
    }
    let connection = database.connectionId
    let session = database.gattSessionId
    let initial = try bytes(inputs.initial)
    let response = try bytes(inputs.withResponse)
    let noResponse = try bytes(inputs.withoutResponse)
    guard initial != response, initial != noResponse, response != noResponse else {
        throw ScanProbeFailure.invalid("fixture I/O values must be distinct")
    }
    let before = try await owner.readCharacteristic(
        connection: connection, session: session,
        characteristic: characteristic.id, timeoutMs: 10_000
    )
    guard before == initial else { throw ScanProbeFailure.invalid("initial read mismatch") }
    try await owner.writeCharacteristic(
        connection: connection, session: session, characteristic: characteristic.id,
        value: response, writeType: .withResponse, timeoutMs: 10_000
    )
    let confirmed = try await owner.readCharacteristic(
        connection: connection, session: session,
        characteristic: characteristic.id, timeoutMs: 10_000
    )
    guard confirmed == response else {
        throw ScanProbeFailure.invalid("write-with-response readback mismatch")
    }
    try await owner.writeCharacteristic(
        connection: connection, session: session, characteristic: characteristic.id,
        value: noResponse, writeType: .withoutResponse, timeoutMs: 10_000
    )
    // A no-response terminal is local submission, not remote delivery confirmation.
    try await Task.sleep(nanoseconds: 500_000_000)
    let delayed = try await owner.readCharacteristic(
        connection: connection, session: session,
        characteristic: characteristic.id, timeoutMs: 10_000
    )
    guard delayed == noResponse else {
        throw ScanProbeFailure.invalid("write-without-response delayed readback mismatch")
    }
}

func verifyAppleRf(
    serviceUuid: String, connect: Bool = false, discover: Bool = false,
    indicationOnly: Bool = false, ioInputs: RfIoInputs? = nil,
    readRssi: Bool = false
) async throws -> RfOutcome {
    if ioInputs != nil && !discover || readRssi && !connect {
        throw ScanProbeFailure.invalid("I/O requires discovery and RSSI requires connection")
    }
    guard let uuid = UUID(uuidString: serviceUuid) else {
        throw ScanProbeFailure.invalid("invalid fixture service UUID")
    }
    let service = BluetoothUuid(value: uuid.uuidString.lowercased())
    let backend = try AppleBleBackend(queueLabel: "dev.blehub.bleng.ios-scan-probe")
    do {
        let owner = try Bleng()
        do {
            try await owner.assembleBackend(backend: backend)
            let snapshot = try await owner.initialize(timeoutMs: 30_000)
            guard snapshot.state == .poweredOn, snapshot.scanAuthorization == .authorized,
                  !connect || snapshot.connectionAuthorization == .authorized else {
                throw ScanProbeFailure.invalid("CoreBluetooth is not powered on and scan-authorized")
            }
            let evidence = ScanEvidence(service: service)
            let reader = Task {
                while let event = try await owner.nextEvent(timeoutMs: 86_400_000) {
                    await evidence.record(event)
                }
            }
            do {
                let session = try await owner.startScan(
                    options: ScanOptions(
                        serviceUuids: [service], namePrefix: nil,
                        companyId: nil, allowDuplicates: false
                    ),
                    timeoutMs: 10_000
                )
                let advertisement = try await awaitScanEvidence("profile advertisement", seconds: 30) {
                    await evidence.advertisement(for: session)
                }
                try await owner.stopScan(session: session, timeoutMs: 10_000)
                _ = try await awaitScanEvidence("requested scan stop", seconds: 5) {
                    await evidence.completedStop(for: session) ? true : nil
                }
                try await Task.sleep(nanoseconds: 1_000_000_000)
                guard !(await evidence.hasPostStopAdvertisement()) else {
                    throw ScanProbeFailure.invalid("advertisement observed after requested scan stop")
                }
                var connection: ConnectionId?
                var gatt: GattSessionId?
                var cccdCount: Int?
                var rssi: Int16?
                if connect {
                    let id = try await owner.connect(
                        peripheral: advertisement.peripheralId, timeoutMs: 20_000
                    )
                    _ = try await awaitScanEvidence("ordered Connecting/Connected events", seconds: 5) {
                        await evidence.connected(id) ? true : nil
                    }
                    if discover {
                        let session = try await owner.discoverGatt(connection: id, timeoutMs: 25_000)
                        let database = try await awaitScanEvidence("correlated GATT database", seconds: 5) {
                            await evidence.database(connection: id, session: session)
                        }
                        cccdCount = try validateFixtureDatabase(
                            database, service: service, indicationOnly: indicationOnly
                        )
                        gatt = session
                        if let ioInputs {
                            try await verifyIo(
                                owner: owner, database: database, service: service, inputs: ioInputs
                            )
                        }
                    }
                    if readRssi {
                        let value = try await owner.readRssi(connection: id, timeoutMs: 10_000)
                        _ = try await awaitScanEvidence("correlated RSSI event", seconds: 5) {
                            await evidence.observedRssi(connection: id, value: value) ? true : nil
                        }
                        rssi = value
                    }
                    try await owner.disconnect(connection: id, timeoutMs: 20_000)
                    _ = try await awaitScanEvidence("active Disconnected event", seconds: 5) {
                        await evidence.activelyDisconnected(id) ? true : nil
                    }
                    connection = id
                }
                try await owner.shutdown()
                try await reader.value
                return RfOutcome(
                    snapshot: snapshot, advertisement: advertisement,
                    connection: connection, gatt: gatt, cccdCount: cccdCount,
                    io: ioInputs, rssi: rssi
                )
            } catch {
                try? await owner.shutdown()
                _ = try? await reader.value
                throw error
            }
        } catch {
            try? await owner.shutdown()
            throw error
        }
    } catch {
        try? backend.dispose()
        throw error
    }
}

func verifyAppleSubscriptionRf(
    serviceUuid: String, notificationValueHex: String, indicationOnly: Bool,
    runDirectory: URL, strictIndication: Bool = false
) async throws -> SubscriptionRfOutcome {
    guard let uuid = UUID(uuidString: serviceUuid) else {
        throw ScanProbeFailure.invalid("invalid fixture service UUID")
    }
    let service = BluetoothUuid(value: uuid.uuidString.lowercased())
    let expectedValue = try bytes(notificationValueHex)
    let backend = try AppleBleBackend(queueLabel: "dev.blehub.bleng.ios-notification-probe")
    do {
        let owner = try Bleng()
        do {
            try await owner.assembleBackend(backend: backend)
            let snapshot = try await owner.initialize(timeoutMs: 30_000)
            guard snapshot.state == .poweredOn, snapshot.scanAuthorization == .authorized,
                  snapshot.connectionAuthorization == .authorized else {
                throw ScanProbeFailure.invalid("CoreBluetooth is not powered on and scan-authorized")
            }
            let evidence = ScanEvidence(service: service)
            let reader = Task {
                while let event = try await owner.nextEvent(timeoutMs: 86_400_000) {
                    await evidence.record(event)
                }
            }
            do {
                let scanSession = try await owner.startScan(
                    options: ScanOptions(
                        serviceUuids: [service], namePrefix: nil,
                        companyId: nil, allowDuplicates: false
                    ),
                    timeoutMs: 10_000
                )
                let advertisement = try await awaitScanEvidence("profile advertisement", seconds: 30) {
                    await evidence.advertisement(for: scanSession)
                }
                try await owner.stopScan(session: scanSession, timeoutMs: 10_000)
                _ = try await awaitScanEvidence("requested scan stop", seconds: 5) {
                    await evidence.completedStop(for: scanSession) ? true : nil
                }
                try await Task.sleep(nanoseconds: 1_000_000_000)
                guard !(await evidence.hasPostStopAdvertisement()) else {
                    throw ScanProbeFailure.invalid("advertisement observed after requested scan stop")
                }

                let connection = try await owner.connect(
                    peripheral: advertisement.peripheralId, timeoutMs: 20_000
                )
                _ = try await awaitScanEvidence("ordered Connecting/Connected events", seconds: 5) {
                    await evidence.connected(connection) ? true : nil
                }
                let gatt = try await owner.discoverGatt(connection: connection, timeoutMs: 25_000)
                let database = try await awaitScanEvidence("correlated GATT database", seconds: 5) {
                    await evidence.database(connection: connection, session: gatt)
                }
                let cccdCount = try validateFixtureDatabase(
                    database, service: service, indicationOnly: indicationOnly
                )
                let characteristics = database.services.first(where: { $0.uuid == service })?.characteristics ?? []
                guard let dual = characteristics.first(where: {
                    $0.uuid.value == "fff20000-0000-4000-8000-000000000000"
                }), dual.properties.notifiable, dual.properties.indicatable else {
                    throw ScanProbeFailure.invalid("fixture FFF2 dual-mode characteristic is missing")
                }
                if strictIndication {
                    guard indicationOnly, let indication = characteristics.first(where: {
                        $0.uuid.value == "fff50000-0000-4000-8000-000000000000"
                    }), indication.properties.indicatable, !indication.properties.notifiable else {
                        throw ScanProbeFailure.invalid("fixture FFF5 indication-only characteristic is missing")
                    }
                    do {
                        _ = try await owner.enableNotifications(
                            connection: connection, session: gatt, characteristic: dual.id,
                            mode: .requireIndication, timeoutMs: 15_000
                        )
                        throw ScanProbeFailure.invalid("FFF2 strict Indication unexpectedly succeeded")
                    } catch BlengError.Operation(let error)
                        where error.category == .notSupported {
                        // The BLeng facade preserves the canonical BLE error payload.
                    }
                    guard !(await evidence.hasSubscriptionActivity(characteristic: dual.id)) else {
                        throw ScanProbeFailure.invalid("FFF2 strict rejection created an active route")
                    }
                    // A fresh enable on FFF2 must be possible after the rejected request.
                    let probe = try await owner.enableNotifications(
                        connection: connection, session: gatt, characteristic: dual.id,
                        mode: .platformDefault, timeoutMs: 15_000
                    )
                    let probeEnabled = try await awaitScanEvidence("FFF2 fallback enabled event", seconds: 5) {
                        await evidence.notificationEnabled(
                            connection: connection, session: gatt, subscription: probe,
                            characteristic: dual.id, mode: .notification
                        )
                    }
                    let probeEnableCompletion = try await awaitScanEvidence(
                        "FFF2 fallback enable completion", seconds: 5
                    ) {
                        await evidence.commandCompletion(probeEnabled.command, after: probeEnabled.index)
                    }
                    guard !probeEnableCompletion.failed else {
                        throw ScanProbeFailure.invalid("FFF2 fallback notification enable failed")
                    }
                    try await owner.disableNotifications(
                        connection: connection, session: gatt, subscription: probe, timeoutMs: 15_000
                    )
                    let probeDisabled = try await awaitScanEvidence("FFF2 fallback disabled event", seconds: 5) {
                        await evidence.notificationDisabled(
                            connection: connection, session: gatt, subscription: probe,
                            characteristic: dual.id
                        )
                    }
                    let probeCompletion = try await awaitScanEvidence(
                        "FFF2 fallback disable completion", seconds: 5
                    ) {
                        await evidence.commandCompletion(probeDisabled.command, after: probeDisabled.index)
                    }
                    guard !probeCompletion.failed else {
                        throw ScanProbeFailure.invalid("FFF2 fallback notification disable failed")
                    }
                }
                let characteristic = strictIndication ? characteristics.first(where: {
                    $0.uuid.value == "fff50000-0000-4000-8000-000000000000"
                })! : dual
                let mode: NotificationMode = strictIndication ? .indication : .notification
                let label = strictIndication ? "indication" : "notification"
                let subscription = try await owner.enableNotifications(
                    connection: connection, session: gatt, characteristic: characteristic.id,
                    mode: strictIndication ? .requireIndication : .platformDefault, timeoutMs: 15_000
                )
                let enabled = try await awaitScanEvidence("notification enabled event", seconds: 5) {
                    await evidence.notificationEnabled(
                        connection: connection, session: gatt, subscription: subscription,
                        characteristic: characteristic.id, mode: mode
                    )
                }
                let enableCompletion = try await awaitScanEvidence(
                    "notification enable completion", seconds: 5
                ) {
                    await evidence.commandCompletion(enabled.command, after: enabled.index)
                }
                guard !enableCompletion.failed else {
                    throw ScanProbeFailure.invalid("notification enable completed with an error")
                }
                try writeBlengRfMarker(runDirectory, marker: "\(label)-enabled")

                let valueEvent = try await awaitScanEvidence("notification value", seconds: 45) {
                    await evidence.notificationValue(
                        after: enabled.index, connection: connection, session: gatt,
                        subscription: subscription, characteristic: characteristic.id
                    )
                }
                guard valueEvent.value == expectedValue else {
                    throw ScanProbeFailure.invalid(
                        "notification value was \(hexValue(valueEvent.value)); expected \(notificationValueHex.lowercased())"
                    )
                }

                try await owner.disableNotifications(
                    connection: connection, session: gatt, subscription: subscription,
                    timeoutMs: 15_000
                )
                let disabled = try await awaitScanEvidence("notification disabled event", seconds: 5) {
                    await evidence.notificationDisabled(
                        connection: connection, session: gatt, subscription: subscription,
                        characteristic: characteristic.id
                    )
                }
                let disableCompletion = try await awaitScanEvidence(
                    "notification disable completion", seconds: 5
                ) {
                    await evidence.commandCompletion(disabled.command, after: disabled.index)
                }
                guard !disableCompletion.failed else {
                    throw ScanProbeFailure.invalid("notification disable completed with an error")
                }
                try writeBlengRfMarker(runDirectory, marker: "\(label)-disabled")
                try await waitForBlengRfMarker(
                    runDirectory, marker: "post-disable-triggered", seconds: 60
                )
                try await Task.sleep(nanoseconds: 2_000_000_000)
                guard !(await evidence.hasNotificationAfter(
                    disableCompletion.index, connection: connection, session: gatt,
                    subscription: subscription, characteristic: characteristic.id
                )) else {
                    throw ScanProbeFailure.invalid("notification value observed after disable completion")
                }

                try await owner.disconnect(connection: connection, timeoutMs: 20_000)
                _ = try await awaitScanEvidence("active Disconnected event", seconds: 5) {
                    await evidence.activelyDisconnected(connection) ? true : nil
                }
                try await owner.shutdown()
                try await reader.value
                return SubscriptionRfOutcome(
                    snapshot: snapshot, advertisement: advertisement,
                    connection: connection, gatt: gatt, cccdCount: cccdCount,
                    subscription: subscription, valueHex: hexValue(expectedValue), actualMode: mode
                )
            } catch {
                try? await owner.shutdown()
                _ = try? await reader.value
                throw error
            }
        } catch {
            try? await owner.shutdown()
            throw error
        }
    } catch {
        try? backend.dispose()
        throw error
    }
}
