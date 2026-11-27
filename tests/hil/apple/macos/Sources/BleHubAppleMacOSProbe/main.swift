import AppKit
@_spi(AppleDiagnostics) import BleHubApple
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
            "CoreBluetooth returned an unexpected external fixture database: \(detail)"
        case .invalidGattValue(let detail):
            "CoreBluetooth returned an unexpected external fixture value: \(detail)"
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

func argument(after flag: String) -> String? {
    guard let index = CommandLine.arguments.firstIndex(of: flag),
          CommandLine.arguments.indices.contains(index + 1)
    else {
        return nil
    }
    return CommandLine.arguments[index + 1]
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

func matchingAdvertisement(
    scanId: ScanSessionId,
    serviceUuid: BluetoothUuid,
    in events: [BleEvent]
) -> AdvertisementObservation? {
    for event in events {
        if case .advertisementObserved(let advertisement) = event,
           advertisement.scanSessionId == scanId,
           advertisement.advertisedServices.contains(serviceUuid)
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

func passiveDisconnectState(
    connectionId: ConnectionId,
    event: BleEvent
) -> Bool {
    guard case .connectionStateChanged(
        let commandId,
        let observedConnectionId,
        let result
    ) = event,
          commandId == nil,
          observedConnectionId == connectionId,
          case .err(let eventError) = result else {
        return false
    }
    return eventError.category == .connection
}

func runScan(
    fixture: SwiftProbeHarness,
    serviceUuid value: String,
    namePrefix: String? = nil
) throws -> (output: [String], advertisement: AdvertisementObservation) {
    let serviceUuid = BluetoothUuid(value: value.lowercased())
    let startCommand = StartScanCommand(
        commandId: CommandId(value: 2),
        scanSessionId: ScanSessionId(value: 1),
        options: ScanOptions(serviceUuids: [serviceUuid], namePrefix: namePrefix, exactName: nil, companyId: nil, minRssi: nil, manufacturerData: nil, android: nil, allowDuplicates: true)
    )
    let eventOffset = fixture.snapshot().events.count
    try fixture.startScan(command: startCommand)

    let scanDeadline = Date().addingTimeInterval(30)
    var advertisement: AdvertisementObservation?
    while Date() < scanDeadline {
        let events = Array(fixture.snapshot().events.dropFirst(eventOffset))
        if scanStarted(startCommand, in: events),
           commandSucceeded(startCommand.commandId, in: events),
           let matching = matchingAdvertisement(
               scanId: startCommand.scanSessionId,
               serviceUuid: serviceUuid,
               in: events
           )
        {
            advertisement = matching
            break
        }
        waitForCallbacks()
    }
    guard let advertisement else {
        throw ProbeError.scanTimeout(fixture.snapshot().events)
    }
    if let namePrefix, advertisement.localName?.hasPrefix(namePrefix) != true {
        throw ProbeError.invalidGattDatabase("scan returned a name outside the requested prefix")
    }

    let stopCommand = StopScanCommand(
        commandId: CommandId(value: 3),
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
    if matchingAdvertisement(
        scanId: startCommand.scanSessionId,
        serviceUuid: serviceUuid,
        in: Array(eventsAfterStop)
    ) != nil {
        throw ProbeError.postStopAdvertisement
    }

    let rssi = advertisement.rssi.map { String($0) } ?? "absent"
    let connectable = advertisement.connectable.map { String($0) } ?? "absent"
    return (
        output: [
            "apple_macos_corebluetooth_scan_service_uuid=\(serviceUuid.value)",
            "apple_macos_corebluetooth_scan_peripheral_id=\(advertisement.peripheralId.value)",
            "apple_macos_corebluetooth_scan_rssi=\(rssi)",
            "apple_macos_corebluetooth_scan_connectable=\(connectable)",
            "apple_macos_corebluetooth_scan_started=passed",
            "apple_macos_corebluetooth_advertisement=passed",
            "apple_macos_corebluetooth_scan_stopped=passed",
            "apple_macos_corebluetooth_post_stop_silence=passed",
            "apple_macos_corebluetooth_scan=passed",
        ] + (namePrefix.map { [
            "apple_macos_corebluetooth_scan_filter=service+name",
            "apple_macos_corebluetooth_scan_name_prefix=\($0)",
        ] } ?? []),
        advertisement: advertisement
    )
}

func runGattDiscovery(
    fixture: SwiftProbeHarness,
    connectionId: ConnectionId,
    expectedServiceUuid: String,
    configuration: RfFixtureConfiguration = .android
) throws -> (output: [String], database: GattDatabase) {
    let command = DiscoverGattCommand(
        commandId: CommandId(value: 5),
        connectionId: connectionId,
        gattSessionId: GattSessionId(value: 1)
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
    throw ProbeError.notificationTimeout("host marker \(marker)", [])
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
    strictIndication: Bool = false,
    configuration: RfFixtureConfiguration
) throws -> (
    output: [String],
    ledgerOffset: Int,
    subscriptionId: SubscriptionId,
    characteristicId: GattCharacteristicId,
    expectedValue: Data
) {
    let serviceUuid = BluetoothUuid(value: expectedServiceUuid.lowercased())
    let characteristicUuid = BluetoothUuid(
        value: strictIndication && configuration.hasIndicationOnly
            ? "fff50000-0000-4000-8000-000000000000"
            : "fff20000-0000-4000-8000-000000000000"
    )
    guard let service = database.services.first(where: { $0.uuid == serviceUuid }),
          let characteristic = service.characteristics.first(where: { $0.uuid == characteristicUuid })
    else {
        throw ProbeError.invalidGattDatabase("updates characteristic is missing")
    }

    let subscriptionId = SubscriptionId(value: 1)
    let expectedMode: NotificationMode = strictIndication ? .indication : .notification
    let requestedMode: NotificationModeRequest = strictIndication
        ? .requireIndication
        : .platformDefault
    let enable = SetNotificationsCommand(
        commandId: CommandId(value: 6),
        connectionId: database.connectionId,
        gattSessionId: database.gattSessionId,
        subscriptionId: subscriptionId,
        characteristicId: characteristic.id,
        action: .enable(mode: requestedMode)
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

    try signalProbeHost(path: fixtureCoordinationPath, marker: "notification-enabled")
    let expectedValue = try configuration.notificationValue
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

    let disable = SetNotificationsCommand(
        commandId: CommandId(value: 7),
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
    expectedMode: NotificationMode = .notification
) throws {
    let enableId = CommandId(value: 6)
    let disableId = CommandId(value: 7)
    for (commandId, enabled) in [(enableId, true), (disableId, false)] {
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
            guard case .enabled(let actualMode) = states[0].5 else {
                throw ProbeError.invalidNotification(
                    "enable did not report an enabled mode; expected \(expectedMode)"
                )
            }
            guard actualMode == expectedMode else {
                throw ProbeError.invalidNotification(
                    "enable reported \(String(describing: actualMode)), expected \(expectedMode)"
                )
            }
        } else if states[0].5 != .disabled {
            throw ProbeError.invalidNotification("disable did not report Disabled state")
        }
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
    runRssiAfterConnect: Bool,
    runPassiveDisconnectAfterConnect: Bool,
    strictIndication: Bool,
    fixtureCoordinationPath: String?,
    configuration: RfFixtureConfiguration = .android
) throws -> [String] {
    let connect = ConnectCommand(
        commandId: CommandId(value: 4),
        connectionId: ConnectionId(value: 1),
        peripheralId: peripheralId, autoConnect: false
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
            let notification = try runNotifications(
                fixture: fixture,
                database: discovery.database,
                expectedServiceUuid: discoverGattServiceUuid,
                fixtureCoordinationPath: fixtureCoordinationPath,
                strictIndication: strictIndication,
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

    if runRssiAfterConnect {
        let rssiCommand = ReadRssiCommand(
            commandId: CommandId(value: 12),
            connectionId: connect.connectionId
        )
        let rssiOffset = fixture.snapshot().events.count
        try fixture.readRssi(command: rssiCommand)
        let rssiDeadline = Date().addingTimeInterval(15)
        var observedRssi: Int16?
        var terminalIndex: Int?
        while Date() < rssiDeadline {
            let events = Array(fixture.snapshot().events.dropFirst(rssiOffset))
            if let index = events.firstIndex(where: { event in
                guard case .rssiRead(let commandId, let connectionId, _) = event else {
                    return false
                }
                return commandId == rssiCommand.commandId &&
                    connectionId == rssiCommand.connectionId
            }) {
                if case .rssiRead(_, _, let rssi) = events[index] {
                    observedRssi = rssi
                }
            }
            if let index = events.firstIndex(where: { event in
                guard case .commandCompleted(let commandId, let error) = event else {
                    return false
                }
                return commandId == rssiCommand.commandId && error == nil
            }) {
                terminalIndex = index
            }
            if observedRssi != nil, let terminalIndex {
                guard let rssiIndex = events.firstIndex(where: { event in
                    if case .rssiRead(let commandId, let connectionId, _) = event {
                        return commandId == rssiCommand.commandId &&
                            connectionId == rssiCommand.connectionId
                    }
                    return false
                }), rssiIndex < terminalIndex else {
                    throw ProbeError.unexpectedEvents(events)
                }
                break
            }
            waitForCallbacks()
        }
        guard let observedRssi else {
            throw ProbeError.gattIoTimeout("RSSI", Array(fixture.snapshot().events.dropFirst(rssiOffset)))
        }
        output.append("apple_macos_corebluetooth_rssi_value=\(observedRssi)")
        output.append("apple_macos_corebluetooth_rssi=passed")
    }

    if runPassiveDisconnectAfterConnect {
        try signalProbeHost(path: fixtureCoordinationPath, marker: "connected")
        try waitForProbeHost(path: fixtureCoordinationPath, marker: "fault-triggered", timeout: 45)
        let passiveDeadline = Date().addingTimeInterval(45)
        var disconnectedEventIndex: Int?
        while Date() < passiveDeadline {
            let events = fixture.snapshot().events
            if let index = events.firstIndex(where: {
                passiveDisconnectState(connectionId: connect.connectionId, event: $0)
            }) {
                disconnectedEventIndex = index
                break
            }
            waitForCallbacks()
        }
        guard let disconnectedEventIndex else {
            throw ProbeError.disconnectTimeout(fixture.snapshot().events)
        }
        try signalProbeHost(path: fixtureCoordinationPath, marker: "passive-disconnected")
        Thread.sleep(forTimeInterval: 1)
        let finalEvents = fixture.snapshot().events
        let eventsAfterDisconnect = finalEvents.dropFirst(disconnectedEventIndex + 1)
        guard !eventsAfterDisconnect.contains(where: { event in
            if case .connectionStateChanged(_, let connectionId, _) = event {
                return connectionId == connect.connectionId
            }
            return false
        }), !eventsAfterDisconnect.contains(where: { event in
            switch event {
            case .gattDiscovered(_, let database):
                return database.connectionId == connect.connectionId
            case .characteristicRead(_, let connectionId, _, _, _),
                 .characteristicWritten(_, let connectionId, _, _),
                 .notificationStateChanged(_, let connectionId, _, _, _, _),
                 .notificationReceived(let connectionId, _, _, _, _):
                return connectionId == connect.connectionId
            default:
                return false
            }
        }) else {
            throw ProbeError.postDisconnectGattEvent
        }
        output.append("apple_macos_corebluetooth_passive_disconnect=passed")
        output.append("apple_macos_corebluetooth_passive_disconnect_silence=passed")
        return output
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
        commandId: CommandId(value: disconnectCommandId),
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
            expectedMode: strictIndication ? .indication : .notification
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
    runRssiAfterConnect: Bool,
    runPassiveDisconnectAfterConnect: Bool,
    strictIndication: Bool,
    fixtureCoordinationPath: String?,
    configuration: RfFixtureConfiguration = .android
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
        let scan = try runScan(
            fixture: fixture, serviceUuid: scanServiceUuid,
            namePrefix: try RfFixtureConfiguration.scanNamePrefix(CommandLine.arguments)
        )
        output.append(contentsOf: scan.output)
        output.append(contentsOf: AppleAdvertisementDiagnostics.snapshot().map {
            "apple_macos_corebluetooth_raw_advertisement=\($0)"
        })
        if connectAfterScan {
            output.append(
                contentsOf: try runConnection(
                    fixture: fixture,
                    peripheralId: scan.advertisement.peripheralId,
                    discoverGattServiceUuid: discoverGattAfterConnect ? scanServiceUuid : nil,
                    runGattIoAfterDiscovery: runGattIoAfterDiscovery,
                    runNotificationsAfterDiscovery: runNotificationsAfterDiscovery,
                    runRssiAfterConnect: runRssiAfterConnect,
                    runPassiveDisconnectAfterConnect: runPassiveDisconnectAfterConnect,
                    strictIndication: strictIndication,
                    fixtureCoordinationPath: fixtureCoordinationPath,
                    configuration: configuration
                )
            )
        }
    }
    if CommandLine.arguments.contains("--native-hil") {
        let beforeDispose = fixture.snapshot().events
        try fixture.dispose()
        Thread.sleep(forTimeInterval: 2)
        guard fixture.snapshot().events == beforeDispose else {
            throw ProbeError.invalidGattDatabase("events changed after native HIL close")
        }
        output.append("apple_native_hil_close=passed")
    }
    return configuration.evidence + output
}

func executeProbe(
    waitForAuthorization: Bool,
    scanServiceUuid: String?,
    connectAfterScan: Bool,
    discoverGattAfterConnect: Bool,
    runGattIoAfterDiscovery: Bool,
    runNotificationsAfterDiscovery: Bool,
    runRssiAfterConnect: Bool,
    runPassiveDisconnectAfterConnect: Bool,
    strictIndication: Bool,
    fixtureCoordinationPath: String?,
    resultPath: String?
) -> Never {
    var identity = ""
    do {
        if CommandLine.arguments.contains("--native-hil") {
            guard let path = fixtureCoordinationPath, let runId = argument(after: "--run-id") else {
                throw ProbeError.invalidGattDatabase("native HIL requires coordination and run identity")
            }
            identity = try publishNativeHilIdentity(path: path, runId: runId)
        }
        try signalProbeHost(
            path: fixtureCoordinationPath,
            marker: "probe-pid",
            value: "\(getpid())\n"
        )
        let configuration = try RfFixtureConfiguration.fromArguments(CommandLine.arguments, serviceUuid: scanServiceUuid)
        let output = try runProbe(
            waitForAuthorization: waitForAuthorization,
            scanServiceUuid: scanServiceUuid,
            connectAfterScan: connectAfterScan,
            discoverGattAfterConnect: discoverGattAfterConnect,
            runGattIoAfterDiscovery: runGattIoAfterDiscovery,
            runNotificationsAfterDiscovery: runNotificationsAfterDiscovery,
            runRssiAfterConnect: runRssiAfterConnect,
            runPassiveDisconnectAfterConnect: runPassiveDisconnectAfterConnect,
            strictIndication: strictIndication,
            fixtureCoordinationPath: fixtureCoordinationPath,
            configuration: configuration
        )
        let text = identity + output.joined(separator: "\n") + "\n"
        print(text, terminator: "")
        if let resultPath {
            try text.write(toFile: resultPath, atomically: true, encoding: .utf8)
        }
        exit(EXIT_SUCCESS)
    } catch {
        let diagnostics = AppleAdvertisementDiagnostics.snapshot().map {
            "apple_macos_corebluetooth_raw_advertisement=\($0)"
        }.joined(separator: "\n")
        let suffix = diagnostics.isEmpty ? "" : "\n\(diagnostics)"
        let text = identity + "apple_macos_corebluetooth_probe=failed: \(error)\(suffix)\n"
        fputs(text, stderr)
        if let resultPath {
            try? text.write(toFile: resultPath, atomically: true, encoding: .utf8)
        }
        exit(EXIT_FAILURE)
    }
}

func makeAuthorizationWindow() -> NSWindow {
    let window = NSWindow(
        contentRect: NSRect(x: 0, y: 0, width: 460, height: 150),
        styleMask: [.titled, .closable],
        backing: .buffered,
        defer: false
    )
    window.title = "BleHub Bluetooth Authorization"
    let label = NSTextField(
        labelWithString: "Waiting for macOS Bluetooth permission.\nPlease click Allow in the system prompt."
    )
    label.alignment = .center
    label.frame = NSRect(x: 30, y: 45, width: 400, height: 55)
    window.contentView?.addSubview(label)
    window.center()
    window.makeKeyAndOrderFront(nil)
    return window
}

let waitForAuthorization = CommandLine.arguments.contains("--wait-for-authorization")
let scanServiceUuid = argument(after: "--scan-service-uuid")
let connectAfterScan = CommandLine.arguments.contains("--connect-after-scan")
let discoverGattAfterConnect = CommandLine.arguments.contains("--discover-gatt-after-connect")
let runGattIoAfterDiscovery = CommandLine.arguments.contains("--run-gatt-io")
let runNotificationsAfterDiscovery = CommandLine.arguments.contains("--run-notifications")
let runRssiAfterConnect = CommandLine.arguments.contains("--run-rssi")
let runPassiveDisconnectAfterConnect = CommandLine.arguments.contains("--run-passive-disconnect")
let strictIndication = CommandLine.arguments.contains("--strict-indication")
let fixtureCoordinationPath = argument(after: "--fixture-coordination-path")
let resultPath = argument(after: "--result-path")
if waitForAuthorization {
    _ = NSApplication.shared
    NSApp.setActivationPolicy(.regular)
    NSApp.finishLaunching()
    let authorizationWindow = makeAuthorizationWindow()
    NSApp.activate(ignoringOtherApps: true)
    DispatchQueue.global(qos: .userInitiated).async {
        executeProbe(
            waitForAuthorization: true,
            scanServiceUuid: scanServiceUuid,
            connectAfterScan: connectAfterScan,
            discoverGattAfterConnect: discoverGattAfterConnect,
            runGattIoAfterDiscovery: runGattIoAfterDiscovery,
            runNotificationsAfterDiscovery: runNotificationsAfterDiscovery,
            runRssiAfterConnect: runRssiAfterConnect,
            runPassiveDisconnectAfterConnect: runPassiveDisconnectAfterConnect,
            strictIndication: strictIndication,
            fixtureCoordinationPath: fixtureCoordinationPath,
            resultPath: resultPath
        )
    }
    _ = authorizationWindow
    NSApp.run()
    fatalError("NSApplication event loop exited before the authorization probe")
} else {
    executeProbe(
        waitForAuthorization: false,
        scanServiceUuid: scanServiceUuid,
        connectAfterScan: connectAfterScan,
        discoverGattAfterConnect: discoverGattAfterConnect,
        runGattIoAfterDiscovery: runGattIoAfterDiscovery,
        runNotificationsAfterDiscovery: runNotificationsAfterDiscovery,
        runRssiAfterConnect: runRssiAfterConnect,
        runPassiveDisconnectAfterConnect: runPassiveDisconnectAfterConnect,
        strictIndication: strictIndication,
        fixtureCoordinationPath: fixtureCoordinationPath,
        resultPath: resultPath
    )
}
