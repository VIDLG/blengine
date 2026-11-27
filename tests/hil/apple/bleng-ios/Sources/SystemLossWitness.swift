import BleHubUniFfi
import CryptoKit
import Foundation

// Witness-only seam. Backend acceptance is deliberately NOT an actor-admission receipt.
enum SystemLossFailure: Error {
    case invalid(String)
}

enum SystemLossStopped: Error { case host }

func systemLossRequire(_ condition: Bool, _ message: String) throws {
    guard condition else { throw SystemLossFailure.invalid(message) }
}

enum SystemLossCase: String, CaseIterable {
    case writeEnable = "response-write-queued-ccc-enable"
    case disableWrite = "ccc-disable-queued-response-write"
}

enum SystemLossJSON {
    // The provider's _canonical uses ASCII schema/UUID/hex strings, sorted keys,
    // compact separators and explicit nulls. Never round opaque u64 IDs via JSON numbers.
    static func canonical(_ value: Any) throws -> Data {
        // JSONSerialization otherwise normalizes 1.0 to 1. Python's provider
        // rejects float schema versions/PIDs; do not erase that distinction.
        func validateNumbers(_ value: Any) throws {
            if let object = value as? [String: Any] {
                for child in object.values { try validateNumbers(child) }
            } else if let array = value as? [Any] {
                for child in array { try validateNumbers(child) }
            } else if let number = value as? NSNumber {
                try systemLossRequire(!["f", "d"].contains(String(cString: number.objCType)), "floating-point schema number")
            }
        }
        try validateNumbers(value)
        let data = try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys, .withoutEscapingSlashes, .fragmentsAllowed])
        try systemLossRequire(data.allSatisfy { $0 < 128 }, "non-ASCII witness JSON")
        return data
    }

    static func object(_ data: Data) throws -> [String: Any] {
        try systemLossRequire(data.count <= 65_536, "document bound")
        guard let object = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw SystemLossFailure.invalid("expected JSON object")
        }
        return object
    }

    static func digest(_ data: Data) throws -> String {
        let normalized = try canonical(object(data))
        return SHA256.hash(data: normalized).map { String(format: "%02x", $0) }.joined()
    }

    static func id(_ value: String) throws {
        try systemLossRequire(value.first.map { ("1"..."9").contains(String($0)) } == true
            && value.utf8.allSatisfy { (48...57).contains($0) }
            && UInt64(value).map { $0 > 0 } == true, "noncanonical nonzero u64 ID")
    }

    static func uuid(_ value: String) throws {
        try systemLossRequire(UUID(uuidString: value)?.uuidString.lowercased() == value, "noncanonical UUID")
    }

    static func hex(_ value: String) throws -> Data {
        try systemLossRequire(!value.isEmpty && value.count <= 40 && value.count.isMultiple(of: 2)
            && value.utf8.allSatisfy { (48...57).contains($0) || (97...102).contains($0) }, "noncanonical bounded hex")
        var data = Data()
        var index = value.startIndex
        while index < value.endIndex {
            let end = value.index(index, offsetBy: 2)
            guard let byte = UInt8(value[index..<end], radix: 16) else {
                throw SystemLossFailure.invalid("invalid hex byte")
            }
            data.append(byte)
            index = end
        }
        return data
    }

    static func hex(_ value: Data) -> String {
        value.map { String(format: "%02x", $0) }.joined()
    }
}

struct SystemLossRoute {
    static let readUuid = "fff10000-0000-4000-8000-000000000000"
    static let updatesUuid = "fff20000-0000-4000-8000-000000000000"
    static let cccUuid = "00002902-0000-1000-8000-00805f9b34fb"
    let database: GattDatabase
    let read: GattCharacteristic
    let updates: GattCharacteristic
    let ccc: GattDescriptor
    let document: [String: Any]

    init(database: GattDatabase, serviceUuid: String) throws {
        try SystemLossJSON.uuid(serviceUuid)
        let services = database.services.filter { $0.uuid.value == serviceUuid && $0.primary == true }
        try systemLossRequire(services.count == 1, "one discovered primary service required")
        let reads = services[0].characteristics.filter { $0.uuid.value == Self.readUuid }
        let updates = services[0].characteristics.filter { $0.uuid.value == Self.updatesUuid }
        try systemLossRequire(reads.count == 1 && updates.count == 1, "unique discovered RW/updates roles required")
        let descriptors = updates[0].descriptors.filter { $0.uuid.value == Self.cccUuid }
        try systemLossRequire(descriptors.count == 1, "one discovered CCC required")
        let read = reads[0], update = updates[0], ccc = descriptors[0]
        func properties(_ value: CharacteristicProperties) -> [String] {
            [(value.readable, "read"), (value.writableWithResponse, "write-response"),
             (value.writableWithoutResponse, "write-command"), (value.notifiable, "notify"),
             (value.indicatable, "indicate")].compactMap { $0.0 ? $0.1 : nil }
        }
        try systemLossRequire(properties(read.properties) == ["read", "write-response", "write-command"]
            && properties(update.properties) == ["read", "notify", "indicate"], "discovered role properties")
        let ids = [read.id.value, update.id.value, ccc.id.value]
        try systemLossRequire(Set(ids).count == 3, "distinct discovered attribute IDs required")
        for id in ids + [database.connectionId.value, database.gattSessionId.value] {
            try SystemLossJSON.id(String(id))
        }
        self.database = database
        self.read = read
        self.updates = update
        self.ccc = ccc
        document = [
            "service_uuid": serviceUuid, "connection_id": String(database.connectionId.value),
            "gatt_session_id": String(database.gattSessionId.value), "read_characteristic_id": String(read.id.value),
            "updates_characteristic_id": String(update.id.value), "ccc_descriptor_id": String(ccc.id.value),
            "read_uuid": read.uuid.value, "updates_uuid": update.uuid.value, "ccc_uuid": ccc.uuid.value,
            "read_properties": properties(read.properties), "updates_properties": properties(update.properties),
        ]
    }
}

final class SystemLossWitness: @unchecked Sendable {
    private enum Accepted {
        case initialize(CommandId)
        case write(WriteCharacteristicCommand)
        case notifications(SetNotificationsCommand)
    }
    private static let bootId = UUID().uuidString.lowercased()
    private var ownerId = UUID().uuidString.lowercased()
    private var backendId = UUID().uuidString.lowercased()
    private var sessionIdentityBound = false
    private let lock = NSLock()
    private var bindCount = 0
    private var initializeCount = 0
    private var disposeCount = 0
    private var invalid: String?
    private var sealed = false
    private var accepted: [CommandId: Accepted] = [:]
    private var raw: [BleEvent] = []
    private var setupData: Data?
    private var controlData: Data?
    private var witnessData: Data?
    private var route: SystemLossRoute?
    private var scenario: SystemLossCase?
    private var before = Data()
    private var requested = Data()
    private var baseline: SubscriptionId?
    private var armed = false
    private var armedTicket: Data?
    private var armedStatusWire: String?
    private var pendingPublished = false
    private var nativeFailure: String?
    private var lossAuthorized = false

    private func sync<T>(_ body: () throws -> T) rethrows -> T {
        lock.lock()
        defer { lock.unlock() }
        return try body()
    }

    private func requireLive() throws {
        try systemLossRequire(invalid == nil && nativeFailure == nil && !sealed && disposeCount == 0,
                              invalid ?? nativeFailure ?? "closed witness")
    }

    func willBind() throws {
        try sync {
            bindCount += 1
            if bindCount != 1 { invalid = "backend rebound to another owner" }
            try requireLive()
        }
    }

    func willInitialize() throws {
        try sync {
            initializeCount += 1
            if initializeCount != 1 || bindCount != 1 { invalid = "duplicate/unbound initialize" }
            try requireLive()
        }
    }

    /// Binds evidence to the Supervisor's actual owner/backend identity after
    /// initialization and before setup is exported. Fresh owners never call
    /// this method and therefore cannot mutate the old witness identity.
    func bindSessionIdentity(ownerId: String, backendId: String) throws {
        try sync {
            try requireLive()
            guard !sessionIdentityBound, setupData == nil, initializeCount == 1 else {
                throw SystemLossFailure.invalid("session identity was rebound or bound after setup")
            }
            try systemLossRequire(!ownerId.isEmpty && !backendId.isEmpty, "empty supervisor session identity")
            self.ownerId = ownerId
            self.backendId = backendId
            sessionIdentityBound = true
        }
    }

    // Idempotent teardown never forwards dispose twice, even on assembly failure.
    func willDispose() -> Bool {
        sync {
            guard disposeCount == 0 else { return false }
            disposeCount = 1
            sealed = true
            return true
        }
    }

    func counts() -> (bind: Int, initialize: Int, dispose: Int) {
        sync { (bindCount, initializeCount, disposeCount) }
    }

    // Observational recording must not turn an actual successful delegate return
    // into a dispatch rejection. Evidence faults instead poison future exports.
    private func accept(_ command: CommandId, _ value: Accepted) {
        sync {
            guard !sealed else { return }
            guard command.value != 0, accepted[command] == nil, accepted.count < 16 else {
                invalid = "duplicate/zero/unbounded successful dispatch identity"
                return
            }
            accepted[command] = value
        }
    }

    func returned(_ command: InitializeCommand) { accept(command.commandId, .initialize(command.commandId)) }
    func returned(_ command: WriteCharacteristicCommand) { accept(command.commandId, .write(command)) }
    func returned(_ command: SetNotificationsCommand) { accept(command.commandId, .notifications(command)) }
    // Delegate-return facts only. Never use this getter as a BLeng actor fence.
    func successfulCommandIds() -> Set<CommandId> { sync { Set(accepted.keys) } }

    func record(_ event: BleEvent) {
        sync {
            guard !sealed else { return }
            switch event {
            case .backendError(let error), .commandCompleted(_, .some(let error)), .connectionStateChanged(_, _, .err(let error)):
                if !lossAuthorized && nativeFailure == nil {
                    nativeFailure = String(describing: event) + ": " + String(describing: error)
                }
            case .connectionStateChanged(_, _, .ok(.disconnected)):
                if !lossAuthorized && nativeFailure == nil {
                    nativeFailure = "unexpected native disconnect during witness setup"
                }
            default: break
            }
            switch event {
            case .gattDiscovered, .characteristicRead, .notificationStateChanged, .notificationReceived,
                 .commandCompleted, .characteristicWritten, .connectionStateChanged, .backendError:
                guard raw.count < 128 else { invalid = "raw event bound"; return }
                raw.append(event)
            default: break
            }
        }
    }

    func observationError() -> String? { sync { nativeFailure ?? invalid } }

    func authorizeLossObservations() throws {
        try sync {
            try systemLossRequire(!lossAuthorized, "loss observations already authorized")
            lossAuthorized = true
        }
    }

    func observedRead(command: CommandId, route: SystemLossRoute, value: Data) -> Bool {
        sync {
            raw.contains {
                if case .characteristicRead(command, route.database.connectionId, route.database.gattSessionId, route.read.id, value) = $0 { return true }
                return false
            }
        }
    }

    func prepare(
        scenario: SystemLossCase, sessionId: String, serviceUuid: String, database: GattDatabase,
        initializeCommand: CommandId, readCommand: CommandId, before: Data, requested: Data,
        baselineCommand: CommandId?, baselineSubscription: SubscriptionId?
    ) throws -> Data {
        try sync {
            try requireLive()
            try systemLossRequire(setupData == nil && bindCount == 1 && initializeCount == 1, "setup/lifetime already used")
            try SystemLossJSON.uuid(sessionId)
            guard case .initialize(initializeCommand)? = accepted[initializeCommand] else {
                throw SystemLossFailure.invalid("actual initialize successful return required")
            }
            _ = try SystemLossJSON.hex(SystemLossJSON.hex(before))
            _ = try SystemLossJSON.hex(SystemLossJSON.hex(requested))
            try systemLossRequire(requested != before && requested.count <= before.count, "write intent bounds")
            let route = try SystemLossRoute(database: database, serviceUuid: serviceUuid)
            try systemLossRequire(raw.contains {
                if case .gattDiscovered(_, let value) = $0 { return value == database }
                return false
            }, "typed database must match public discovery event")
            try systemLossRequire(raw.contains {
                if case .characteristicRead(readCommand, database.connectionId, database.gattSessionId, route.read.id, before) = $0 { return true }
                return false
            }, "observed before bytes required")
            var events: [[String: Any]] = [["kind": "discovered", "source": "native-gatt-discovery", "route": route.document]]
            if scenario == .disableWrite {
                guard let command = baselineCommand, let subscription = baselineSubscription,
                      case .notifications(let actual)? = accepted[command] else {
                    throw SystemLossFailure.invalid("observed baseline dispatch IDs required")
                }
                try SystemLossJSON.id(String(subscription.value))
                try systemLossRequire(command != initializeCommand && actual.connectionId == database.connectionId
                    && actual.gattSessionId == database.gattSessionId && actual.characteristicId == route.updates.id
                    && actual.subscriptionId == subscription && actual.action == .enable(mode: .requireNotification), "baseline dispatch route")
                guard let enabled = raw.firstIndex(where: {
                    if case .notificationStateChanged(command, database.connectionId, database.gattSessionId, subscription, route.updates.id, .enabled(actualMode: .notification)) = $0 { return true }
                    return false
                }), raw.dropFirst(enabled + 1).contains(where: {
                    if case .notificationReceived(database.connectionId, database.gattSessionId, subscription, route.updates.id, Data([0xef])) = $0 { return true }
                    return false
                }) else { throw SystemLossFailure.invalid("ordered real baseline enabled/EF events required") }
                events += [
                    ["kind": "baseline-enabled", "source": "native-domain-event", "command_id": String(command.value), "subscription_id": String(subscription.value)],
                    ["kind": "baseline-value", "source": "native-domain-event", "subscription_id": String(subscription.value), "value_hex": "ef"],
                ]
            } else {
                try systemLossRequire(baselineCommand == nil && baselineSubscription == nil, "unexpected baseline IDs")
            }
            let identity: [String: Any] = [
                "pid": Int(ProcessInfo.processInfo.processIdentifier), "boot_id": Self.bootId,
                "owner_id": ownerId, "backend_id": backendId, "initialize_command_id": String(initializeCommand.value),
            ]
            let setup: [String: Any] = [
                "schema_version": 1, "session_id": sessionId, "case": scenario.rawValue,
                "identity": identity, "route": route.document,
                "baseline_command_id": baselineCommand.map { String($0.value) } as Any? ?? NSNull(),
                "baseline_subscription_id": baselineSubscription.map { String($0.value) } as Any? ?? NSNull(),
                "write_before": SystemLossJSON.hex(before), "write_requested": SystemLossJSON.hex(requested),
            ]
            let data = try SystemLossJSON.canonical(setup)
            self.route = route
            self.scenario = scenario
            self.before = before
            self.requested = requested
            baseline = baselineSubscription
            setupData = data
            // No control digest exists yet: this private prefix is published only
            // after the host supplies control. Host arm consumes that base witness.
            witnessData = try SystemLossJSON.canonical(["schema_version": 1, "identity": identity, "events": events])
            return data
        }
    }

    func bindControl(_ data: Data) throws -> Data {
        try sync {
            try requireLive()
            guard let setupData, let witnessData else { throw SystemLossFailure.invalid("setup required") }
            try systemLossRequire(controlData == nil, "control already frozen")
            var control = try SystemLossJSON.object(data)
            guard let token = control.removeValue(forKey: "token") as? String else {
                throw SystemLossFailure.invalid("control token required")
            }
            try SystemLossJSON.uuid(token)
            try systemLossRequire(try SystemLossJSON.canonical(control) == setupData, "control differs from observed setup")
            var witness = try SystemLossJSON.object(witnessData)
            witness["control_sha256"] = try SystemLossJSON.digest(data)
            let base = try SystemLossJSON.canonical(witness)
            controlData = data
            self.witnessData = base
            return base
        }
    }

    func observeArmed(_ data: Data) throws {
        try sync {
            try requireLive()
            guard let controlData, let witnessData else { throw SystemLossFailure.invalid("bound control required") }
            try systemLossRequire(!armed, "already armed")
            let physical = try SystemLossJSON.object(data)
            let control = try SystemLossJSON.object(controlData)
            let version = try SystemLossJSON.canonical(physical["schema_version"] ?? NSNull())
            let radio = try SystemLossJSON.canonical(physical["radio_authorized"] ?? NSNull())
            try systemLossRequire(version == Data("1".utf8)
                && physical["scope"] as? String == "system-loss-provider-only"
                && physical["state"] as? String == "armed"
                && physical["session_id"] as? String == control["session_id"] as? String
                && physical["control_sha256"] as? String == SystemLossJSON.digest(controlData)
                && radio == Data("false".utf8)
                && physical["recovery"] as? String == "not-evaluated"
                && physical["primary_error"] is NSNull, "matching provider ARMED required")
            let boundaries = physical["boundaries"] as? [[String: Any]] ?? []
            guard let row = boundaries.last, row["boundary"] as? String == "armed",
                  let base = row["witness"] as? [String: Any] else {
                throw SystemLossFailure.invalid("provider armed boundary/base witness required")
            }
            try systemLossRequire(try SystemLossJSON.canonical(base) == witnessData, "provider armed a different base")
            let status = try providerStatus(row)
            armedTicket = try SystemLossJSON.canonical(status.ticket)
            armedStatusWire = status.wire
            armed = true
        }
    }

    // Treat the provider's complete portable ticket as opaque identity. The
    // provider owns BTP decoding/status validation; do not duplicate its codec.
    private func providerStatus(_ row: [String: Any]) throws -> (ticket: [String: Any], wire: String) {
        guard let status = row["status"] as? [String: Any],
              let ticket = status["ticket"] as? [String: Any], let wire = status["status_wire"] as? String,
              let ticketWire = ticket["wire_hex"] as? String else {
            throw SystemLossFailure.invalid("complete provider status/ticket required")
        }
        func hex(_ text: String) -> Bool {
            !text.isEmpty && text.count.isMultiple(of: 2)
                && text.utf8.allSatisfy { (48...57).contains($0) || (97...102).contains($0) }
        }
        try systemLossRequire(Set(status.keys) == ["protocol_version", "ticket", "status_wire"]
            && SystemLossJSON.canonical(status["protocol_version"] ?? NSNull()) == Data("2".utf8)
            && Set(ticket.keys) == ["wire_hex", "sequence", "handle"] && ticketWire.count == 110
            && hex(ticketWire) && hex(wire), "provider status/ticket schema")
        try SystemLossJSON.id(String(decoding: SystemLossJSON.canonical(ticket["sequence"] ?? NSNull()), as: UTF8.self))
        try SystemLossJSON.id(String(decoding: SystemLossJSON.canonical(ticket["handle"] ?? NSNull()), as: UTF8.self))
        return (ticket, wire)
    }

    func observeHeldPending(_ data: Data) throws {
        try sync {
            try requireLive()
            guard pendingPublished, let controlData, let witnessData, let armedTicket, let armedStatusWire else {
                throw SystemLossFailure.invalid("actual pending witness and ARMED ticket required before provider HELD")
            }
            let physical = try SystemLossJSON.object(data)
            let control = try SystemLossJSON.object(controlData)
            try systemLossRequire(SystemLossJSON.canonical(physical["schema_version"] ?? NSNull()) == Data("1".utf8)
                && physical["scope"] as? String == "system-loss-provider-only"
                && physical["state"] as? String == "held-pending"
                && physical["session_id"] as? String == control["session_id"] as? String
                && physical["control_sha256"] as? String == SystemLossJSON.digest(controlData)
                && SystemLossJSON.canonical(physical["radio_authorized"] ?? NSNull()) == Data("false".utf8)
                && physical["recovery"] as? String == "not-evaluated"
                && physical["primary_error"] is NSNull, "matching provider HELD required")
            let boundaries = physical["boundaries"] as? [[String: Any]] ?? []
            guard let row = boundaries.last, row["boundary"] as? String == "held-pending",
                  let pending = row["witness"] as? [String: Any] else {
                throw SystemLossFailure.invalid("provider HELD boundary/pending witness required")
            }
            try systemLossRequire(try SystemLossJSON.canonical(pending) == witnessData, "provider HELD witness drift")
            let status = try providerStatus(row)
            try systemLossRequire(try SystemLossJSON.canonical(status.ticket) == armedTicket && status.wire != armedStatusWire,
                                  "provider HELD ticket changed or stale ARMED status")
            let events = pending["events"] as? [[String: Any]] ?? []
            guard events.count >= 4, let queue = events.last else { throw SystemLossFailure.invalid("pending queue binding missing") }
            let binding: [String: Any] = [
                "schema_version": 1, "control_sha256": try SystemLossJSON.digest(controlData),
                "head_command_id": queue["head_command_id"] ?? NSNull(),
                "sibling_command_id": queue["sibling_command_id"] ?? NSNull(),
                "subscription_id": queue["subscription_id"] ?? NSNull(), "witness_sha256": try SystemLossJSON.digest(witnessData),
            ]
            try systemLossRequire(try SystemLossJSON.canonical(physical["binding"] ?? NSNull()) == SystemLossJSON.canonical(binding)
                && physical["binding_sha256"] as? String == SystemLossJSON.digest(SystemLossJSON.canonical(binding)), "provider HELD binding drift")
        }
    }

    func requireArmedForAttempt() throws {
        try sync {
            try requireLive()
            try systemLossRequire(armed && !pendingPublished, "fresh matching ARMED required for one-shot attempt")
        }
    }

    // ACTUAL successful delegate DTOs plus the production lane SPI establish
    // this observation. No actor-ack inference or guaranteed admission is needed.
    func pending(snapshot: AppleGattIoSnapshot?, head: CommandId, sibling: CommandId) throws -> Data {
        try sync {
            try requireLive()
            guard armed, !pendingPublished, let route, let scenario, let controlData, let witnessData,
                  let snapshot else { throw SystemLossFailure.invalid("armed fresh native lane required") }
            let control = try SystemLossJSON.object(controlData)
            let identity = control["identity"] as? [String: Any] ?? [:]
            let initId = identity["initialize_command_id"] as? String ?? ""
            let commands = [initId, String(head.value), String(sibling.value)]
                + ((control["baseline_command_id"] as? String).map { [$0] } ?? [])
            try systemLossRequire(Set(commands).count == commands.count, "distinct actual dispatch IDs required")
            for value in commands { try SystemLossJSON.id(value) }
            try systemLossRequire(nativeFailure == nil, nativeFailure ?? "native failure")
            try systemLossRequire(!raw.contains {
                switch $0 {
                case .commandCompleted(let id, _), .characteristicWritten(let id, _, _, _),
                     .notificationStateChanged(let id, _, _, _, _, _):
                    return id == head || id == sibling
                default: return false
                }
            }, "completed native command cannot be pending in a stale snapshot")
            try systemLossRequire(snapshot.connectionId == route.database.connectionId && snapshot.headKind == .command
                && snapshot.headState == .awaitingNativeCallback && snapshot.headCommandId == head
                && snapshot.queuedCommandIds == [sibling], "exact callback-owned native FIFO required")
            var events = try SystemLossJSON.object(witnessData)["events"] as? [[String: Any]] ?? []
            var subscription: SubscriptionId?
            for (role, id, write) in [("head", head, scenario == .writeEnable), ("sibling", sibling, scenario != .writeEnable)] {
                let operation: String
                let characteristic: GattCharacteristicId
                var sub: SubscriptionId?
                if write {
                    guard case .write(let command)? = accepted[id] else { throw SystemLossFailure.invalid("actual successful write return required") }
                    try systemLossRequire(command.connectionId == route.database.connectionId && command.gattSessionId == route.database.gattSessionId
                        && command.characteristicId == route.read.id && command.writeType == .withResponse
                        && command.value == requested, "actual write route/type/payload drift")
                    operation = "response-write"
                    characteristic = command.characteristicId
                } else {
                    guard case .notifications(let command)? = accepted[id] else { throw SystemLossFailure.invalid("actual successful notifications return required") }
                    let enabling = scenario == .writeEnable
                    try systemLossRequire(command.connectionId == route.database.connectionId && command.gattSessionId == route.database.gattSessionId
                        && command.characteristicId == route.updates.id
                        && command.action == (enabling ? .enable(mode: .requireNotification) : .disable)
                        && (enabling || command.subscriptionId == baseline), "actual CCC route/action/subscription drift")
                    try SystemLossJSON.id(String(command.subscriptionId.value))
                    operation = enabling ? "ccc-enable" : "ccc-disable"
                    characteristic = command.characteristicId
                    sub = command.subscriptionId
                    subscription = sub
                }
                events.append([
                    "kind": "dispatch-accepted", "source": "backend-dispatch-return", "role": role,
                    "command_id": String(id.value), "operation": operation, "accepted": true,
                    "connection_id": String(route.database.connectionId.value), "gatt_session_id": String(route.database.gattSessionId.value),
                    "characteristic_id": String(characteristic.value),
                    "subscription_id": sub.map { String($0.value) } as Any? ?? NSNull(),
                    "descriptor_id": sub.map { _ in String(route.ccc.id.value) } as Any? ?? NSNull(),
                    "before_hex": write ? SystemLossJSON.hex(before) : (scenario == .writeEnable ? "0000" : "0100"),
                    "requested_hex": write ? SystemLossJSON.hex(requested) : (scenario == .writeEnable ? "0100" : "0000"),
                ])
            }
            guard let subscription else { throw SystemLossFailure.invalid("actual subscription required") }
            events.append(["kind": "queue-pending", "source": "native-io-lane", "head_command_id": String(head.value),
                           "sibling_command_id": String(sibling.value), "subscription_id": String(subscription.value),
                           "head_state": "pending", "sibling_state": "queued"])
            var output = try SystemLossJSON.object(witnessData)
            output["events"] = events
            let data = try SystemLossJSON.canonical(output)
            self.witnessData = data
            pendingPublished = true
            return data
        }
    }

    func witness() throws -> Data {
        try sync {
            guard controlData != nil, let witnessData else { throw SystemLossFailure.invalid("base not bound") }
            return witnessData
        }
    }

    func stop() { sync { sealed = true } }
}

// The live adapter captures a real typed operation in wait; the thin test image
// supplies only an admission outcome and dispatches via the actual BleBackend seam.
struct SystemLossPendingOperation: Sendable {
    let commandId: CommandId
    let wait: @Sendable (UInt64) async throws -> Void
}

struct SystemLossAttemptOperationError: Error, CustomStringConvertible {
    let role: String
    let commandId: CommandId
    let underlying: Error
    var description: String { "\(role) operation \(commandId.value): \(underlying)" }
}

private final class SystemLossOperationResult: @unchecked Sendable {
    private let lock = NSLock()
    private var value: Result<Void, Error>?
    func set(_ value: Result<Void, Error>) {
        lock.lock()
        defer { lock.unlock() }
        self.value = value
    }
    func get() -> Result<Void, Error>? {
        lock.lock()
        defer { lock.unlock() }
        return value
    }
}

// One attempt, not a retry coordinator. Successful wrapper returns are readiness
// facts, NOT an actor fence. Busy remains a precise failed attempt. Closures retain
// all typed handles through explicit stop/deadline and same-owner cleanup.
final class SystemLossOneShotAttempt {
    private struct Retained {
        let role: String
        let operation: SystemLossPendingOperation
        let result: SystemLossOperationResult
        let waiter: Task<Void, Never>
    }
    private let witness: SystemLossWitness
    private var retained: [Retained] = []
    private(set) var state = "not-started"
    private(set) var failure: String?
    private var headSubmissions = 0
    private var siblingSubmissions = 0
    private var lossAuthorized = false
    private var terminals: [CommandId: String] = [:]

    init(witness: SystemLossWitness) { self.witness = witness }

    static func remainingMs(until deadline: ContinuousClock.Instant) throws -> UInt64 {
        let remaining = ContinuousClock.now.duration(to: deadline)
        try systemLossRequire(remaining > .zero, "absolute system-loss attempt deadline")
        let parts = remaining.components
        return max(1, UInt64(parts.seconds * 1_000 + parts.attoseconds / 1_000_000_000_000_000))
    }

    private func retain(_ operation: SystemLossPendingOperation, role: String, deadline: ContinuousClock.Instant) {
        // Retain even if the absolute deadline elapsed during synchronous begin.
        // This is a delivery wait budget, never a native watchdog extension.
        let timeout = (try? Self.remainingMs(until: deadline)) ?? 1
        let result = SystemLossOperationResult()
        let waiter = Task {
            do { try await operation.wait(timeout); result.set(.success(())) }
            catch { result.set(.failure(error)) }
        }
        retained.append(Retained(role: role, operation: operation, result: result, waiter: waiter))
    }

    private func terminal(_ error: Error) -> String {
        // The deterministic witness target links the thin FFI image and must not
        // grow a private dependency on a generated BLeng error type. The typed
        // wait result is still the source of truth; only its stable description
        // is projected into the portable host category.
        let description = String(describing: error).lowercased()
        if description.contains("disconnected") || description.contains("connection lost") {
            return "connection-lost"
        }
        if description.contains("timeout") {
            return "watchdog-timeout"
        }
        if description.contains("cancel") || description.contains("aborted") {
            return "operation-aborted"
        }
        return "backend-error"
    }

    func authorizeLoss() throws {
        try systemLossRequire(state == "pending-observed", "loss authorization requires native pending witness")
        try systemLossRequire(!lossAuthorized, "loss authorization already consumed")
        try witness.authorizeLossObservations()
        lossAuthorized = true
    }

    func checkOperations() throws {
        if let error = witness.observationError() { throw SystemLossFailure.invalid(error) }
        for item in retained {
            guard let result = item.result.get() else { continue }
            if terminals[item.operation.commandId] != nil { continue }
            switch result {
            case .success:
                throw SystemLossFailure.invalid("\(item.role) operation \(item.operation.commandId.value) completed successfully after loss boundary")
            case .failure(let error):
                guard lossAuthorized else {
                    throw SystemLossAttemptOperationError(role: item.role, commandId: item.operation.commandId, underlying: error)
                }
                terminals[item.operation.commandId] = terminal(error)
            }
        }
    }

    func terminalResults() throws -> [[String: Any]] {
        try checkOperations()
        try systemLossRequire(lossAuthorized && terminals.count == retained.count,
                              "each loss operation must have exactly one terminal")
        return retained.map {
            ["command_id": String($0.operation.commandId.value),
             "result": terminals[$0.operation.commandId] as Any]
        }
    }

    func run(
        deadline: ContinuousClock.Instant,
        beginHead: () throws -> SystemLossPendingOperation,
        beginSibling: () throws -> SystemLossPendingOperation,
        check: () throws -> Void,
        snapshot: () async throws -> AppleGattIoSnapshot?,
        publish: (Data) throws -> Void
    ) async throws -> Data {
        try systemLossRequire(state == "not-started", "one-shot attempt already used; retry forbidden")
        do {
            try witness.requireArmedForAttempt()
            func guardAttempt() throws {
                try checkOperations()
                try check()
                _ = try Self.remainingMs(until: deadline)
            }
            try guardAttempt()
            state = "head-submitting"
            headSubmissions += 1
            let head = try beginHead()
            retain(head, role: "head", deadline: deadline)
            while !witness.successfulCommandIds().contains(head.commandId) {
                try guardAttempt()
                try await Task.sleep(nanoseconds: 5_000_000)
            }
            // The parent-authorized fallback: submit exactly once after ACTUAL
            // backend success, accepting that actor admission may still reject Busy.
            try guardAttempt()
            state = "sibling-submitting"
            siblingSubmissions += 1
            let sibling = try beginSibling()
            retain(sibling, role: "sibling", deadline: deadline)
            while !witness.successfulCommandIds().contains(sibling.commandId) {
                try guardAttempt()
                try await Task.sleep(nanoseconds: 5_000_000)
            }
            state = "native-pending-sampling"
            while true {
                try guardAttempt()
                let sample = try await snapshot()
                try guardAttempt()
                if let sample, sample.headKind == .command, sample.headState == .awaitingNativeCallback,
                   sample.headCommandId == head.commandId, sample.queuedCommandIds == [sibling.commandId] {
                    let data = try witness.pending(snapshot: sample, head: head.commandId, sibling: sibling.commandId)
                    try publish(data)
                    state = "pending-observed"
                    return data
                }
                // Poll readiness, never count elapsed sleep as admission/queue proof.
                try await Task.sleep(nanoseconds: 5_000_000)
            }
        } catch SystemLossStopped.host {
            state = "stopped"
            witness.stop()
            throw SystemLossStopped.host
        } catch {
            failure = String(describing: error)
            state = "failed"
            witness.stop()
            throw error
        }
    }

    func stop(failure: String?) {
        if let failure { self.failure = failure; state = "failed" }
        else if state != "failed" { state = "stopped" }
        witness.stop()
    }

    func retireWaiters() {
        for item in retained { item.waiter.cancel() }
    }

    func status() -> [String: Any] {
        ["state": state, "failure": failure as Any? ?? NSNull(),
         "loss_authorized": lossAuthorized, "terminals": terminals.map {
             ["command_id": String($0.key.value), "result": $0.value]
         }.sorted { ($0["command_id"] ?? "") < ($1["command_id"] ?? "") },
         "head_submissions": headSubmissions, "sibling_submissions": siblingSubmissions,
         "operations": retained.map { item -> [String: Any] in
             let result: String
             switch item.result.get() {
             case .success?: result = "completed"
             case .failure(let error)?: result = String(describing: error)
             case nil: result = "pending-at-stop"
             }
             return ["role": item.role, "command_id": String(item.operation.commandId.value),
                     "backend_accepted": witness.successfulCommandIds().contains(item.operation.commandId), "result": result]
         }]
    }
}

// Single-writer, run-local atomic export. Inputs and every published prefix stay
// byte-frozen; failures are written separately instead of repairing old evidence.
final class SystemLossWitnessFiles {
    private let directory: URL
    private var frozen: [String: Data] = [:]
    private var finished = false

    init(directory: URL) throws {
        self.directory = directory
        let values = try directory.resourceValues(forKeys: [.isDirectoryKey, .isSymbolicLinkKey])
        try systemLossRequire(values.isDirectory == true && values.isSymbolicLink != true, "private run directory required")
        let names = try FileManager.default.contentsOfDirectory(atPath: directory.path)
        try systemLossRequire(!names.contains { $0.hasPrefix("system-loss-") }, "stale system-loss run files")
    }

    func read(_ name: String) throws -> Data? {
        try systemLossRequire(["system-loss-control.json", "system-loss-physical.json", "system-loss-authorize.json",
                               "system-loss-restore.json", "system-loss-stop.json"].contains(name), "unknown input name")
        let path = directory.appendingPathComponent(name)
        guard FileManager.default.fileExists(atPath: path.path) else { return nil }
        let data = try boundedRead(path)
        // devicectl copy-to exposes the destination before its contents become
        // visible to the App. An empty provider physical file is therefore a
        // transient relay state, not evidence; let the bounded caller poll.
        if name == "system-loss-physical.json" && data.isEmpty { return nil }
        return data
    }

    private func boundedRead(_ path: URL) throws -> Data {
        let values = try path.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey, .fileSizeKey])
        try systemLossRequire(values.isRegularFile == true && values.isSymbolicLink != true
            && (values.fileSize ?? 65_537) <= 65_536, "bounded regular coordination file required")
        let data = try Data(contentsOf: path)
        try systemLossRequire(data.count <= 65_536, "coordination file grew beyond bound")
        return data
    }

    func verify() throws {
        for (name, data) in frozen {
            try systemLossRequire(try boundedRead(directory.appendingPathComponent(name)) == data, "immutable \(name) changed")
        }
    }

    func publishSetup(_ data: Data) throws {
        try systemLossRequire(frozen["system-loss-setup.json"] == nil, "setup already published")
        try publish("system-loss-setup.json", data)
    }

    func freezeControl(_ data: Data) throws {
        try systemLossRequire(!finished && frozen["system-loss-control.json"] == nil, "control already frozen/closed")
        try verify()
        try systemLossRequire(try read("system-loss-control.json") == data, "control changed during bind")
        frozen["system-loss-control.json"] = data
    }

    func publishLossStarted(_ data: Data) throws {
        let object = try SystemLossJSON.object(data)
        try systemLossRequire(object["schema_version"] as? Int == 1
            && object["state"] as? String == "loss-started", "loss start export schema")
        try publish("system-loss-loss-started.json", data)
    }

    func publishLossTerminals(_ data: Data) throws {
        let object = try SystemLossJSON.object(data)
        try systemLossRequire(object["schema_version"] as? Int == 1
            && object["state"] as? String == "loss-terminals", "loss terminal export schema")
        try publish("system-loss-terminals.json", data)
    }

    func publishRecovery(_ data: Data) throws {
        let object = try SystemLossJSON.object(data)
        try systemLossRequire(object["schema_version"] as? Int == 1
            && object["state"] as? String == "same-owner-recovered", "recovery export schema")
        try publish("system-loss-recovery.json", data)
    }

    func publishFreshOwnerRecovery(_ data: Data) throws {
        let object = try SystemLossJSON.object(data)
        try systemLossRequire(object["schema_version"] as? Int == 1
            && object["state"] as? String == "fresh-owner-recovered"
            && object["recovery_mode"] as? String == "fresh-owner",
            "fresh-owner recovery export schema")
        try publish("system-loss-fresh-owner-recovery.json", data)
    }

    func publishDiagnostic(_ data: Data) throws {
        let object = try SystemLossJSON.object(data)
        try systemLossRequire(object["schema_version"] as? Int == 1
            && object["state"] as? String == "loss-diagnostic", "loss diagnostic export schema")
        try publish("system-loss-diagnostic.json", data)
    }

    func publishRecoveryDiagnostic(_ data: Data) throws {
        let object = try SystemLossJSON.object(data)
        try systemLossRequire(object["schema_version"] as? Int == 1
            && object["state"] as? String == "recovery-diagnostic", "recovery diagnostic export schema")
        try publish("system-loss-recovery-diagnostic.json", data)
    }

    func publishWitness(_ data: Data) throws {
        let object = try SystemLossJSON.object(data)
        guard let events = object["events"] as? [[String: Any]], events.count <= 6 else {
            throw SystemLossFailure.invalid("witness event bound/schema")
        }
        if let prior = frozen["system-loss-witness.json"] {
            var old = try SystemLossJSON.object(prior)
            var next = object
            let prefix = old.removeValue(forKey: "events") as? [[String: Any]] ?? []
            next.removeValue(forKey: "events")
            try systemLossRequire(events.count >= prefix.count
                && SystemLossJSON.canonical(old) == SystemLossJSON.canonical(next)
                && SystemLossJSON.canonical(prefix) == SystemLossJSON.canonical(Array(events.prefix(prefix.count))), "cumulative witness prefix/identity changed")
        }
        try publish("system-loss-witness.json", data)
    }

    private func publish(_ name: String, _ data: Data) throws {
        try systemLossRequire(!finished, "terminal witness export")
        try verify()
        let path = directory.appendingPathComponent(name)
        if frozen[name] == nil {
            try systemLossRequire(!FileManager.default.fileExists(atPath: path.path), "unexpected output already exists")
        }
        try data.write(to: path, options: .atomic)
        frozen[name] = data
    }

    func finish(_ status: [String: Any]) throws {
        try systemLossRequire(!finished, "status already terminal")
        finished = true
        let path = directory.appendingPathComponent("system-loss-status.json")
        try systemLossRequire(!FileManager.default.fileExists(atPath: path.path), "terminal status already exists")
        // Do not verify/repair damaged evidence here: preserve the primary failure.
        try JSONSerialization.data(withJSONObject: status, options: [.sortedKeys, .withoutEscapingSlashes])
            .write(to: path, options: .atomic)
    }
}
