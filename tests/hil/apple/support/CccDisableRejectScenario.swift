import BleHubUniFfi

import Darwin
import Dispatch
import Foundation

private let cccRuntimePrefix = "BLEHUB_NITRO_RUNTIME "

private struct NativeCccControl: Codable, Equatable {
    let session: String
    let flow: Int
    let mode: String
    let token: String

    var phases: [String] {
        [
            "baseline-value-sent", "armed", "held", "rejected",
            "continued-value-sent", "retry-allowed", "silence-started", "close-allowed",
        ]
    }

    var requests: [String] {
        [
            "baseline-enabled", "arm", "disable-dispatched", "reject",
            "reject-observed", "continued-notified", "disabled", "silence-done",
        ]
    }

    func notification(_ phase: String) -> String {
        "dev.blehub.rf.control.\(session).\(flow).\(mode).\(token).\(phase)"
    }

    func marker(_ kind: String, _ phase: String) -> String {
        "rf-control:\(kind):session=\(session):flow=\(flow):mode=\(mode):token=\(token):phase=\(phase)"
    }
}

private final class NativeCccRuntime {
    let session: String
    let nonce = UUID().uuidString.lowercased()
    private let root: URL
    private var lines: [String] = []
    private let lock = NSLock()

    init(session: String, root: URL) throws {
        self.session = session
        self.root = root
        guard FileManager.default.fileExists(atPath: root.path) else {
            throw RfLedgerError(description: "missing native Flow 12 coordination directory")
        }
        record("rf-ccc-disable-reject:runtime:session=\(session):nonce=\(nonce):phase=start")
    }

    func record(_ body: String) {
        let line = cccRuntimePrefix + body
        lock.lock()
        lines.append(line)
        let text = lines.joined(separator: "\n") + "\n"
        lock.unlock()
        print(line)
        let temporary = root.appendingPathComponent("runtime.log.tmp")
        let destination = root.appendingPathComponent("runtime.log")
        do {
            try text.write(to: temporary, atomically: false, encoding: .utf8)
            try? FileManager.default.removeItem(at: destination)
            try FileManager.default.moveItem(at: temporary, to: destination)
        } catch {
            print("BLEHUB_NATIVE_HIL runtime publication failed: \(error)")
        }
    }

    func event(_ fields: [String: Any]) {
        guard JSONSerialization.isValidJSONObject(fields),
              let data = try? JSONSerialization.data(withJSONObject: fields, options: [.sortedKeys]),
              let text = String(data: data, encoding: .utf8)
        else {
            record("rf-ccc-disable-reject:failed:event-serialization")
            return
        }
        record("rf-ccc-disable-reject:event:\(text)")
    }

    func step(_ name: String, _ fields: [String: String] = [:]) {
        let suffix = fields.keys.sorted().map { "\($0)=\(fields[$0]!)" }.joined(separator: ":")
        record("rf-ccc-disable-reject:step:\(name):passed\(suffix.isEmpty ? "" : ":\(suffix)")")
    }

    func control(_ marker: String) { record(marker) }

    func resources(_ phase: String, _ snapshot: [String: Int]) {
        let data = try! JSONSerialization.data(withJSONObject: snapshot, options: [.sortedKeys])
        record("rf-ccc-disable-reject:resources:phase=\(phase):snapshot=\(String(decoding: data, as: UTF8.self))")
    }

    func closeAndPass() {
        record("rf-ccc-disable-reject:step:close:passed:session=\(session)")
        record("rf-ccc-disable-reject:runtime:session=\(session):nonce=\(nonce):phase=close")
        record("rf-ccc-disable-reject:passed")
    }
}

private final class NativeCccDarwinObserver {
    weak var owner: NativeCccDarwinListener?
    let phase: String

    init(owner: NativeCccDarwinListener, phase: String) {
        self.owner = owner
        self.phase = phase
    }
}

private func nativeCccDarwinCallback(
    _: CFNotificationCenter?,
    observer: UnsafeMutableRawPointer?,
    _: CFNotificationName?,
    _: UnsafeRawPointer?,
    _: CFDictionary?
) {
    guard let observer else { return }
    let entry = Unmanaged<NativeCccDarwinObserver>.fromOpaque(observer).takeUnretainedValue()
    entry.owner?.receive(entry.phase)
}

private final class NativeCccDarwinListener {
    private let control: NativeCccControl
    private var observers: [NativeCccDarwinObserver] = []
    private let lock = NSLock()
    private var expected: String?
    private var delivered = Set<String>()
    private var failure: Error?
    private var semaphore = DispatchSemaphore(value: 0)

    init(control: NativeCccControl) throws {
        self.control = control
        let center = CFNotificationCenterGetDarwinNotifyCenter()
        for phase in control.phases {
            let observer = NativeCccDarwinObserver(owner: self, phase: phase)
            observers.append(observer)
            CFNotificationCenterAddObserver(
                center,
                Unmanaged.passUnretained(observer).toOpaque(),
                nativeCccDarwinCallback,
                control.notification(phase) as CFString,
                nil,
                .deliverImmediately
            )
        }
    }

    deinit {
        let center = CFNotificationCenterGetDarwinNotifyCenter()
        for observer in observers {
            CFNotificationCenterRemoveObserver(
                center,
                Unmanaged.passUnretained(observer).toOpaque(),
                nil,
                nil
            )
        }
    }

    func registered(_ runtime: NativeCccRuntime) {
        runtime.control(control.marker("listener", "registered"))
    }

    func expect(_ phase: String) {
        lock.lock()
        expected = phase
        semaphore = DispatchSemaphore(value: 0)
        lock.unlock()
    }

    func wait(for phase: String, timeout: TimeInterval = 30) throws {
        let result = semaphore.wait(timeout: .now() + timeout)
        lock.lock()
        let observedFailure = failure
        let observed = delivered.contains(phase)
        lock.unlock()
        if let observedFailure { throw observedFailure }
        guard result == .success, observed else {
            throw RfLedgerError(description: "timed out waiting for Darwin control phase \(phase)")
        }
    }

    fileprivate func receive(_ phase: String) {
        lock.lock()
        defer { lock.unlock() }
        guard failure == nil else { return }
        guard expected == phase, !delivered.contains(phase) else {
            failure = RfLedgerError(description: "foreign, duplicate or out-of-order Darwin phase \(phase)")
            semaphore.signal()
            return
        }
        delivered.insert(phase)
        semaphore.signal()
    }
}

private func nativeResourceSnapshot(_ backend: AppleBleBackend) -> [String: Int] {
    let semaphore = DispatchSemaphore(value: 0)
    var native: [String: Int] = [:]
    backend.diagnosticSnapshotRfResources { value in
        native = value
        semaphore.signal()
    }
    _ = semaphore.wait(timeout: .now() + 5)
    let fields = [
        "activeScans", "connections", "gattAttributes", "subscriptions", "pendingIo",
        "watchdogs", "callbackRoutes", "ingressRoots", "adapterDelegates", "peerCacheEntries",
        "backendLeases", "hubRoots", "handoffRoots", "handoffPending", "listeners",
        "workerThreads", "lifecycleOwners",
    ]
    var result = Dictionary(uniqueKeysWithValues: fields.map { ($0, native[$0] ?? 0) })
    if result["backendLeases"] ?? 0 > 0 { result["hubRoots"] = 1 }
    return result
}

private func waitForInitialization(_ fixture: SwiftProbeHarness, command: InitializeCommand) throws {
    let deadline = Date().addingTimeInterval(30)
    while Date() < deadline {
        let events = fixture.snapshot().events
        let hasSnapshot = events.contains { event in
            if case .adapterSnapshotChanged(let id, _) = event { return id == command.commandId }
            return false
        }
        let hasTerminal = events.contains { event in
            if case .commandCompleted(let id, let error) = event { return id == command.commandId && error == nil }
            return false
        }
        if hasSnapshot && hasTerminal { return }
        waitForCallbacks()
    }
    throw ProbeError.initializationTimeout
}

private func waitForState(
    _ fixture: SwiftProbeHarness,
    commandId: CommandId,
    connectionId: ConnectionId,
    state expected: ConnectionState,
    timeout: TimeInterval = 30
) throws {
    let deadline = Date().addingTimeInterval(timeout)
    while Date() < deadline {
        let events = fixture.snapshot().events
        if events.contains(where: { connectionState(expected, commandId: commandId, connectionId: connectionId, event: $0) }) &&
            commandSucceeded(commandId, in: events) { return }
        waitForCallbacks()
    }
    throw ProbeError.connectTimeout(fixture.snapshot().events)
}

private func waitForRead(
    _ fixture: SwiftProbeHarness,
    commandId: CommandId,
    connectionId: ConnectionId,
    gattSessionId: GattSessionId,
    characteristicId: GattCharacteristicId,
    expected: Data,
    timeout: TimeInterval = 30
) throws {
    let deadline = Date().addingTimeInterval(timeout)
    while Date() < deadline {
        let events = fixture.snapshot().events
        let read = events.contains { event in
            if case .characteristicRead(let id, let connection, let gatt, let characteristic, let value) = event {
                return id == commandId && connection == connectionId && gatt == gattSessionId && characteristic == characteristicId && value == expected
            }
            return false
        }
        if read && commandSucceeded(commandId, in: events) { return }
        waitForCallbacks()
    }
    throw ProbeError.gattIoTimeout("CCC read \(commandId.value)", fixture.snapshot().events)
}

private func waitForNotification(
    _ fixture: SwiftProbeHarness,
    connectionId: ConnectionId,
    gattSessionId: GattSessionId,
    subscriptionId: SubscriptionId,
    characteristicId: GattCharacteristicId,
    value expected: Data,
    timeout: TimeInterval = 45
) throws {
    let deadline = Date().addingTimeInterval(timeout)
    while Date() < deadline {
        if fixture.snapshot().events.contains(where: { event in
            if case .notificationReceived(let connection, let gatt, let subscription, let characteristic, let value) = event {
                return connection == connectionId && gatt == gattSessionId && subscription == subscriptionId && characteristic == characteristicId && value == expected
            }
            return false
        }) { return }
        waitForCallbacks()
    }
    throw ProbeError.notificationTimeout("CCC value \(hexValue(expected))", fixture.snapshot().events)
}

private func waitForNotificationState(
    _ fixture: SwiftProbeHarness,
    commandId: CommandId,
    connectionId: ConnectionId,
    gattSessionId: GattSessionId,
    subscriptionId: SubscriptionId,
    characteristicId: GattCharacteristicId,
    state expected: NotificationState,
    timeout: TimeInterval = 30
) throws {
    let deadline = Date().addingTimeInterval(timeout)
    while Date() < deadline {
        let events = fixture.snapshot().events
        let state = events.contains { event in
            if case .notificationStateChanged(let id, let connection, let gatt, let subscription, let characteristic, let observed) = event {
                return id == commandId && connection == connectionId && gatt == gattSessionId && subscription == subscriptionId && characteristic == characteristicId && observed == expected
            }
            return false
        }
        if state && commandSucceeded(commandId, in: events) { return }
        waitForCallbacks()
    }
    throw ProbeError.notificationTimeout("CCC state \(commandId.value)", fixture.snapshot().events)
}

private func emitEvents(
    _ fixture: SwiftProbeHarness,
    runtime: NativeCccRuntime,
    cursor: inout Int,
    serviceUuid: String? = nil
) {
    let events = fixture.snapshot().events
    guard cursor <= events.count else { return }
    for event in events[cursor...] {
        var fields: [String: Any] = ["runId": runtime.session, "nonce": runtime.nonce]
        switch event {
        case .adapterSnapshotChanged(let commandId, let snapshot):
            fields["kind"] = "adapterSnapshotChanged"
            fields["commandId"] = commandId.map { String($0.value) }
            fields["adapterState"] = snapshot.state == .poweredOn ? "PoweredOn" : String(describing: snapshot.state)
            fields["scanAuthorization"] = snapshot.scanAuthorization == .authorized ? "Authorized" : String(describing: snapshot.scanAuthorization)
        case .scanStarted(let commandId, let scan):
            fields["kind"] = "scanStarted"; fields["commandId"] = String(commandId.value); fields["scanSessionId"] = String(scan.value)
        case .advertisementObserved(let advertisement):
            fields["kind"] = "advertisementObserved"; fields["scanSessionId"] = String(advertisement.scanSessionId.value); fields["peripheralId"] = String(advertisement.peripheralId.value)
        case .scanStopped(let scan, let result):
            fields["kind"] = "scanStopped"; fields["scanSessionId"] = String(scan.value)
            if case .ok(let reason) = result, case .requested(let commandId) = reason { fields["commandId"] = String(commandId.value) }
        case .connectionStateChanged(let commandId, let connection, let result):
            fields["kind"] = "connectionStateChanged"; fields["commandId"] = commandId.map { String($0.value) }; fields["connectionId"] = String(connection.value)
            switch result {
            case .ok(let state): fields["connectionState"] = String(describing: state).prefix(1).uppercased() + String(describing: state).dropFirst()
            case .err(let error): fields["error"] = nativeError(error)
            }
        case .gattDiscovered(let commandId, let database):
            fields["kind"] = "gattDiscovered"; fields["commandId"] = String(commandId.value); fields["connectionId"] = String(database.connectionId.value); fields["gattSessionId"] = String(database.gattSessionId.value)
            let services = database.services.filter { serviceUuid == nil || $0.uuid.value == serviceUuid!.lowercased() }
            fields["gattServices"] = services.map { service in
                [
                    "id": String(service.id.value),
                    "uuid": service.uuid.value,
                    "primary": service.primary ?? false,
                    "characteristics": service.characteristics.map { characteristic in
                        [
                            "id": String(characteristic.id.value),
                            "uuid": characteristic.uuid.value,
                            "readable": characteristic.properties.readable,
                            "writableWithResponse": characteristic.properties.writableWithResponse,
                            "writableWithoutResponse": characteristic.properties.writableWithoutResponse,
                            "notifiable": characteristic.properties.notifiable,
                            "indicatable": characteristic.properties.indicatable,
                            "descriptors": characteristic.descriptors.map { descriptor in
                                ["id": String(descriptor.id.value), "uuid": descriptor.uuid.value]
                            },
                        ]
                    },
                ]
            }
        case .characteristicRead(let commandId, let connection, let gatt, let characteristic, let value):
            fields["kind"] = "characteristicRead"; fields["commandId"] = String(commandId.value); fields["connectionId"] = String(connection.value); fields["gattSessionId"] = String(gatt.value); fields["characteristicId"] = String(characteristic.value); fields["value"] = value.map(Int.init); fields["valueByteLength"] = value.count
        case .characteristicWritten(let commandId, let connection, let gatt, let characteristic):
            fields["kind"] = "characteristicWritten"; fields["commandId"] = String(commandId.value); fields["connectionId"] = String(connection.value); fields["gattSessionId"] = String(gatt.value); fields["characteristicId"] = String(characteristic.value)
        case .notificationStateChanged(let commandId, let connection, let gatt, let subscription, let characteristic, let state):
            fields["kind"] = "notificationStateChanged"; fields["commandId"] = String(commandId.value); fields["connectionId"] = String(connection.value); fields["gattSessionId"] = String(gatt.value); fields["subscriptionId"] = String(subscription.value); fields["characteristicId"] = String(characteristic.value)
            switch state { case .enabled(let mode): fields["notificationState"] = "Enabled"; fields["actualNotificationMode"] = String(describing: mode).lowercased().contains("notification") ? "Notification" : "Indication"; case .disabled: fields["notificationState"] = "Disabled" }
        case .notificationReceived(let connection, let gatt, let subscription, let characteristic, let value):
            fields["kind"] = "notificationReceived"; fields["connectionId"] = String(connection.value); fields["gattSessionId"] = String(gatt.value); fields["subscriptionId"] = String(subscription.value); fields["characteristicId"] = String(characteristic.value); fields["value"] = value.map(Int.init); fields["valueByteLength"] = value.count
        case .commandCompleted(let commandId, let error):
            fields["kind"] = "commandCompleted"; fields["commandId"] = String(commandId.value); if let error { fields["error"] = nativeError(error) }
        case .rssiRead(_, _, _), .backendError(_):
            fields["kind"] = String(describing: event)
        }
        runtime.event(fields)
    }
    cursor = events.count
}

private func nativeError(_ error: BleEventError) -> [String: Any] {
    let category: String
    switch error.category {
    case .gatt: category = "gatt"
    case .connection: category = "connection"
    case .permission: category = "permission"
    case .adapterUnavailable: category = "adapter_unavailable"
    case .invalidState: category = "invalid_state"
    case .notFound: category = "not_found"
    case .timeout: category = "timeout"
    case .cancelled: category = "cancelled"
    case .operationAborted: category = "operation_aborted"
    case .protocol: category = "protocol"
    case .notSupported: category = "not_supported"
    case .backend: category = "backend"
    case .internal: category = "internal"
    }
    var value: [String: Any] = ["category": category, "message": error.message]
    if let details = error.platformDetails {
        let platform: String
        switch details.platform {
        case .apple: platform = "platform_apple"
        case .android: platform = "platform_android"
        case .windows: platform = "platform_windows"
        case .linux: platform = "platform_linux"
        case .test: platform = "platform_test"
        }
        var platformDetails: [String: Any] = ["platform": platform]
        if let code = details.code { platformDetails["code"] = code }
        if let name = details.name { platformDetails["name"] = name }
        if let message = details.message { platformDetails["message"] = message }
        value["platformDetails"] = platformDetails
    }
    return value
}

func runIOSCccDisableRejectScenario(
    fixture: SwiftProbeHarness,
    backend: AppleBleBackend,
    serviceUuid: String,
    runId: String,
    coordinationPath: String,
    configuration: RfFixtureConfiguration,
    controlJSON: String
) throws -> [String] {
    guard let control = try? JSONDecoder().decode(NativeCccControl.self, from: Data(controlJSON.utf8)),
          control.session == runId, control.flow == 12, control.mode == "rf-ccc-disable-reject",
          UUID(uuidString: control.session) != nil, UUID(uuidString: control.token) != nil
    else { throw RfLedgerError(description: "invalid native Flow 12 control identity") }

    let root = URL(fileURLWithPath: coordinationPath)
    let runtime = try NativeCccRuntime(session: runId, root: root)
    let listener = try NativeCccDarwinListener(control: control)
    listener.registered(runtime)
    var cursor = 0
    let ids = RfCycleIds(cycle: 1, commandBase: 34000, routeBase: 34000)
    let initialize = InitializeCommand(commandId: ids.command(0))
    try fixture.initialize(command: initialize)
    try waitForInitialization(fixture, command: initialize)
    emitEvents(fixture, runtime: runtime, cursor: &cursor)
    runtime.step("initialize", ["commandId": String(initialize.commandId.value)])

    let scan = try runScan(fixture: fixture, serviceUuid: serviceUuid, scanDuration: 120, ids: ids)
    emitEvents(fixture, runtime: runtime, cursor: &cursor)
    runtime.step("scan", [
        "commandId": String(ids.command(3).value),
        "scanSessionId": String(ids.scan.value),
        "peripheralId": String(scan.advertisement.peripheralId.value),
    ])

    let connection = ConnectCommand(commandId: ids.command(5), connectionId: ids.connection, peripheralId: scan.advertisement.peripheralId)
    try fixture.connect(command: connection)
    try waitForState(fixture, commandId: connection.commandId, connectionId: connection.connectionId, state: .connected)
    emitEvents(fixture, runtime: runtime, cursor: &cursor)
    runtime.step("connected", [
        "commandId": String(connection.commandId.value),
        "connectionId": String(connection.connectionId.value),
        "peripheralId": String(scan.advertisement.peripheralId.value),
    ])

    let discovery = try runGattDiscovery(fixture: fixture, connectionId: connection.connectionId, expectedServiceUuid: serviceUuid, ids: ids, configuration: configuration, discoveryCommandId: ids.command(8))
    emitEvents(fixture, runtime: runtime, cursor: &cursor, serviceUuid: serviceUuid)
    guard let service = discovery.database.services.first(where: { $0.uuid.value == serviceUuid.lowercased() }),
          let characteristic = service.characteristics.first(where: { $0.uuid.value == "fff20000-0000-4000-8000-000000000000" }),
          let readCharacteristic = service.characteristics.first(where: { $0.uuid.value == "fff10000-0000-4000-8000-000000000000" })
    else { throw ProbeError.invalidGattDatabase("Flow 12 characteristic topology") }
    let route = [
        "connectionId": String(connection.connectionId.value),
        "gattSessionId": String(discovery.database.gattSessionId.value),
        "characteristicId": String(characteristic.id.value),
    ]
    let readRoute = [
        "connectionId": String(connection.connectionId.value),
        "gattSessionId": String(discovery.database.gattSessionId.value),
        "characteristicId": String(readCharacteristic.id.value),
    ]
    runtime.step("gatt", [
        "commandId": String(ids.command(8).value),
        "connectionId": String(connection.connectionId.value),
        "gattSessionId": String(discovery.database.gattSessionId.value),
        "characteristicId": String(characteristic.id.value),
        "readCharacteristicId": String(readCharacteristic.id.value),
    ])

    let initialValue = try configuration.initialValue
    let notificationValue = try configuration.notificationValue
    let baselineValue = Data([0xef])
    guard notificationValue == baselineValue else {
        throw ProbeError.invalidGattValue(
            "Flow 12 requires notification baseline ef, got \(hexValue(notificationValue))"
        )
    }
    let baselineRead = ReadCharacteristicCommand(commandId: ids.command(16), connectionId: connection.connectionId, gattSessionId: discovery.database.gattSessionId, characteristicId: readCharacteristic.id)
    try fixture.readCharacteristic(command: baselineRead)
    try waitForRead(fixture, commandId: baselineRead.commandId, connectionId: connection.connectionId, gattSessionId: discovery.database.gattSessionId, characteristicId: readCharacteristic.id, expected: initialValue)
    emitEvents(fixture, runtime: runtime, cursor: &cursor)
    runtime.step("baseline-read", readRoute.merging([
        "commandId": String(baselineRead.commandId.value),
        "value": hexValue(initialValue),
    ], uniquingKeysWith: { $1 }))

    let subscription = ids.subscription
    let enable = SetNotificationsCommand(commandId: ids.command(10), connectionId: connection.connectionId, gattSessionId: discovery.database.gattSessionId, subscriptionId: subscription, characteristicId: characteristic.id, action: .enable(mode: .platformDefault))
    try fixture.setNotifications(command: enable)
    try waitForNotificationState(fixture, commandId: enable.commandId, connectionId: connection.connectionId, gattSessionId: discovery.database.gattSessionId, subscriptionId: subscription, characteristicId: characteristic.id, state: .enabled(actualMode: .notification))
    emitEvents(fixture, runtime: runtime, cursor: &cursor)
    runtime.step("baseline-enabled", route.merging([
        "commandId": String(enable.commandId.value),
        "subscriptionId": String(subscription.value),
        "actualMode": "Notification",
    ], uniquingKeysWith: { $1 }))

    listener.expect("baseline-value-sent"); runtime.control(control.marker("ready", "baseline-enabled")); try listener.wait(for: "baseline-value-sent")
    try waitForNotification(fixture, connectionId: connection.connectionId, gattSessionId: discovery.database.gattSessionId, subscriptionId: subscription, characteristicId: characteristic.id, value: Data([0xef]))
    emitEvents(fixture, runtime: runtime, cursor: &cursor)
    runtime.control(control.marker("observed", "baseline-value-sent")); runtime.step("baseline-notified", route.merging([
        "subscriptionId": String(subscription.value),
        "value": hexValue(baselineValue),
    ], uniquingKeysWith: { $1 }))

    listener.expect("armed"); runtime.control(control.marker("ready", "arm")); try listener.wait(for: "armed"); runtime.control(control.marker("observed", "armed"))

    let rejected = SetNotificationsCommand(commandId: ids.command(11), connectionId: connection.connectionId, gattSessionId: discovery.database.gattSessionId, subscriptionId: subscription, characteristicId: characteristic.id, action: .disable)
    try fixture.setNotifications(command: rejected)
    runtime.step("disable-dispatched", route.merging([
        "commandId": String(rejected.commandId.value),
        "subscriptionId": String(subscription.value),
        "requestedMode": "platform_default",
    ], uniquingKeysWith: { $1 }))
    listener.expect("held"); runtime.control(control.marker("ready", "disable-dispatched")); try listener.wait(for: "held"); runtime.control(control.marker("observed", "held"))
    let queued = ReadCharacteristicCommand(commandId: ids.command(12), connectionId: connection.connectionId, gattSessionId: discovery.database.gattSessionId, characteristicId: readCharacteristic.id)
    try fixture.readCharacteristic(command: queued)
    emitEvents(fixture, runtime: runtime, cursor: &cursor)
    runtime.step("queued-read-dispatched", readRoute.merging([
        "commandId": String(queued.commandId.value),
    ], uniquingKeysWith: { $1 }))

    listener.expect("rejected"); runtime.control(control.marker("ready", "reject")); try listener.wait(for: "rejected"); runtime.control(control.marker("observed", "rejected"))
    let rejectedError = fixture.snapshot().events.compactMap { event -> BleEventError? in
        if case .commandCompleted(let id, let error) = event, id == rejected.commandId { return error }; return nil
    }.first
    _ = rejectedError
    let rejectDeadline = Date().addingTimeInterval(30)
    while Date() < rejectDeadline {
        emitEvents(fixture, runtime: runtime, cursor: &cursor)
        let events = fixture.snapshot().events
        if events.contains(where: { if case .commandCompleted(let id, let error) = $0 { return id == rejected.commandId && error?.category == .gatt }; return false }) { break }
        waitForCallbacks()
    }
    try waitForRead(fixture, commandId: queued.commandId, connectionId: connection.connectionId, gattSessionId: discovery.database.gattSessionId, characteristicId: readCharacteristic.id, expected: initialValue)
    emitEvents(fixture, runtime: runtime, cursor: &cursor)
    runtime.step("disable-rejected", route.merging([
        "commandId": String(rejected.commandId.value),
        "subscriptionId": String(subscription.value),
        "error": "gatt:code=14",
    ], uniquingKeysWith: { $1 }))
    runtime.step("queued-read", readRoute.merging([
        "commandId": String(queued.commandId.value),
        "value": hexValue(initialValue),
    ], uniquingKeysWith: { $1 }))
    runtime.resources("failed-disable", nativeResourceSnapshot(backend))

    listener.expect("continued-value-sent"); runtime.control(control.marker("ready", "reject-observed")); try listener.wait(for: "continued-value-sent")
    try waitForNotification(fixture, connectionId: connection.connectionId, gattSessionId: discovery.database.gattSessionId, subscriptionId: subscription, characteristicId: characteristic.id, value: Data([0xe1]))
    emitEvents(fixture, runtime: runtime, cursor: &cursor)
    runtime.control(control.marker("observed", "continued-value-sent")); runtime.step("continued-notified", route.merging([
        "subscriptionId": String(subscription.value),
        "value": "e1",
    ], uniquingKeysWith: { $1 }))

    listener.expect("retry-allowed"); runtime.control(control.marker("ready", "continued-notified")); try listener.wait(for: "retry-allowed"); runtime.control(control.marker("observed", "retry-allowed"))
    let disable = SetNotificationsCommand(commandId: ids.command(13), connectionId: connection.connectionId, gattSessionId: discovery.database.gattSessionId, subscriptionId: subscription, characteristicId: characteristic.id, action: .disable)
    try fixture.setNotifications(command: disable)
    try waitForNotificationState(fixture, commandId: disable.commandId, connectionId: connection.connectionId, gattSessionId: discovery.database.gattSessionId, subscriptionId: subscription, characteristicId: characteristic.id, state: .disabled)
    emitEvents(fixture, runtime: runtime, cursor: &cursor)
    runtime.step("disabled", route.merging([
        "commandId": String(disable.commandId.value),
        "subscriptionId": String(subscription.value),
    ], uniquingKeysWith: { $1 }))
    let postDisable = ReadCharacteristicCommand(commandId: ids.command(14), connectionId: connection.connectionId, gattSessionId: discovery.database.gattSessionId, characteristicId: readCharacteristic.id)
    try fixture.readCharacteristic(command: postDisable); try waitForRead(fixture, commandId: postDisable.commandId, connectionId: connection.connectionId, gattSessionId: discovery.database.gattSessionId, characteristicId: readCharacteristic.id, expected: initialValue)
    emitEvents(fixture, runtime: runtime, cursor: &cursor)
    runtime.step("post-disable-read", readRoute.merging([
        "commandId": String(postDisable.commandId.value),
        "value": hexValue(initialValue),
    ], uniquingKeysWith: { $1 }))

    listener.expect("silence-started"); runtime.control(control.marker("ready", "disabled")); try listener.wait(for: "silence-started"); runtime.control(control.marker("observed", "silence-started"))
    let silenceStart = Date(); Thread.sleep(forTimeInterval: 1.1); let elapsed = Int(Date().timeIntervalSince(silenceStart) * 1000)
    runtime.step("silence", route.merging([
        "subscriptionId": String(subscription.value),
        "elapsedMs": String(elapsed),
    ], uniquingKeysWith: { $1 }))
    let endSilence = ReadCharacteristicCommand(commandId: ids.command(15), connectionId: connection.connectionId, gattSessionId: discovery.database.gattSessionId, characteristicId: readCharacteristic.id)
    try fixture.readCharacteristic(command: endSilence); try waitForRead(fixture, commandId: endSilence.commandId, connectionId: connection.connectionId, gattSessionId: discovery.database.gattSessionId, characteristicId: readCharacteristic.id, expected: initialValue)
    emitEvents(fixture, runtime: runtime, cursor: &cursor)
    runtime.step("end-silence-read", readRoute.merging([
        "commandId": String(endSilence.commandId.value),
        "value": hexValue(initialValue),
    ], uniquingKeysWith: { $1 }))
    listener.expect("close-allowed"); runtime.control(control.marker("ready", "silence-done")); try listener.wait(for: "close-allowed"); runtime.control(control.marker("observed", "close-allowed"))

    let disconnect = DisconnectCommand(commandId: ids.command(6), connectionId: connection.connectionId)
    try fixture.disconnect(command: disconnect); try waitForState(fixture, commandId: disconnect.commandId, connectionId: connection.connectionId, state: .disconnected, timeout: 20)
    emitEvents(fixture, runtime: runtime, cursor: &cursor)
    runtime.step("disconnected", [
        "commandId": String(disconnect.commandId.value),
        "connectionId": String(connection.connectionId.value),
    ])

    try fixture.dispose()
    Thread.sleep(forTimeInterval: 2)
    runtime.resources("closed", nativeResourceSnapshot(backend))
    runtime.closeAndPass()
    return configuration.evidence + [
        "apple_ios_native_hil_boot_id=\(runtime.nonce)",
        "apple_ios_native_hil_ccc_disable_reject=passed",
        "apple_ios_corebluetooth_rf=passed",
    ]
}
