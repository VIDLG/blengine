import CoreFoundation
import Foundation

// Keep this Foundation-only mechanism in the existing staging unit: the macOS
// HIL runner copies this file, while the formal host compiles it by source reference.
struct NativeProcessIdentityContext {
    let bundleId: String?
    let executable: String?
    let pid: Int32
    let makeBootId: () -> String

    static var current: Self {
        Self(
            bundleId: Bundle.main.bundleIdentifier, executable: Bundle.main.executableURL?.path,
            pid: ProcessInfo.processInfo.processIdentifier, makeBootId: { UUID().uuidString.lowercased() }
        )
    }
}

enum NativeProcessIdentityFiles {
    struct InvalidIdentity: Error {}

    static func isCanonicalUuid(_ value: String) -> Bool {
        UUID(uuidString: value)?.uuidString.lowercased() == value
    }

    static func prepareDirectory(_ url: URL) throws {
        guard url.standardizedFileURL.path == url.resolvingSymlinksInPath().standardizedFileURL.path else {
            throw InvalidIdentity()
        }
        if !FileManager.default.fileExists(atPath: url.path) {
            try FileManager.default.createDirectory(at: url, withIntermediateDirectories: false)
        }
        try validateDirectory(url)
    }

    private static func validateDirectory(_ url: URL) throws {
        guard url.standardizedFileURL.path == url.resolvingSymlinksInPath().standardizedFileURL.path,
              try FileManager.default.attributesOfItem(atPath: url.path)[.type] as? FileAttributeType == .typeDirectory else {
            throw InvalidIdentity()
        }
    }

    static func validate(_ root: URL) throws {
        try validateDirectory(root)
        for name in ["native-identity.json", "identity-request.json", "native-inspection.json"] {
            let file = root.appendingPathComponent(name)
            do {
                let attributes = try FileManager.default.attributesOfItem(atPath: file.path)
                guard attributes[.type] as? FileAttributeType == .typeRegular,
                      attributes[.referenceCount] as? Int == 1 else { throw InvalidIdentity() }
            } catch CocoaError.fileReadNoSuchFile {
                continue
            }
        }
    }
}

// A challenge proves a live native boot, not a BLE owner or successful cleanup.
final class NativeProcessIdentityResponder {
    enum Namespace: String {
        case nativeHil = "dev.blehub.native-hil"
        case formalRN = "dev.blehub.formal-rn"
    }

    private let root: URL
    private let runId: String
    private let identity: [String: Any]
    private let data: Data
    private let namespace: Namespace
    private var inspectionPrepared = false

    init(root: URL, runId: String, namespace: Namespace, context: NativeProcessIdentityContext) throws {
        guard NativeProcessIdentityFiles.isCanonicalUuid(runId),
              let bundleId = context.bundleId, !bundleId.isEmpty,
              let executable = context.executable, executable.hasPrefix("/"), context.pid > 0 else {
            throw NativeProcessIdentityFiles.InvalidIdentity()
        }
        // Resolve platform aliases such as /var only at admission, never after
        // accepting the run root (a later symlink replacement must be refused).
        guard try FileManager.default.attributesOfItem(atPath: root.path)[.type] as? FileAttributeType == .typeDirectory else {
            throw NativeProcessIdentityFiles.InvalidIdentity()
        }
        self.root = root.resolvingSymlinksInPath().standardizedFileURL
        try NativeProcessIdentityFiles.validate(self.root)
        let bootId = context.makeBootId()
        guard NativeProcessIdentityFiles.isCanonicalUuid(bootId) else {
            throw NativeProcessIdentityFiles.InvalidIdentity()
        }
        self.runId = runId
        self.namespace = namespace
        identity = [
            "run_id": runId, "boot_id": bootId, "pid": context.pid,
            "bundle_id": bundleId, "executable": executable,
        ]
        data = try JSONSerialization.data(withJSONObject: identity, options: [.sortedKeys])
        let name = "\(namespace.rawValue).\(runId).\(bootId).inspect"
        CFNotificationCenterAddObserver(
            CFNotificationCenterGetDarwinNotifyCenter(), Unmanaged.passUnretained(self).toOpaque(),
            { _, observer, _, _, _ in
                guard let observer else { return }
                Unmanaged<NativeProcessIdentityResponder>.fromOpaque(observer).takeUnretainedValue().respond()
            }, name as CFString, nil, .deliverImmediately
        )
    }

    func matches(root: URL, runId: String) -> Bool {
        self.root.path == root.standardizedFileURL.path && self.runId == runId
    }

    // The in-module observer makes ordering/write failure testable without
    // exposing identity fields or filesystem operations to the RN bridge.
    func publish(beforeIdentityWrite: () throws -> Void = {}) throws -> String {
        try NativeProcessIdentityFiles.validate(root)
        if namespace == .formalRN {
            let inspection = root.appendingPathComponent("native-inspection.json")
            if FileManager.default.fileExists(atPath: inspection.path) {
                try validateInspection(inspection)
            } else if inspectionPrepared {
                throw NativeProcessIdentityFiles.InvalidIdentity()
            }
            if !inspectionPrepared {
                // Publish readiness before the identity becomes visible. Null is
                // not challenge success or live proof; only respond() writes a UUID.
                let sentinel = try JSONSerialization.data(
                    withJSONObject: ["identity": identity, "challenge": NSNull()], options: [.sortedKeys]
                )
                try sentinel.write(to: inspection, options: .atomic)
                inspectionPrepared = true
            }
        }
        // A retry after identity-write failure, or any later publish, preserves
        // the prepared inspection (including a real response), never resets it.
        try beforeIdentityWrite()
        try NativeProcessIdentityFiles.validate(root)
        try data.write(to: root.appendingPathComponent("native-identity.json"), options: .atomic)
        return "apple_native_hil_identity=\(String(decoding: data, as: UTF8.self))\n"
    }

    private func validateInspection(_ url: URL) throws {
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        guard let bytes = try handle.read(upToCount: 4097), bytes.count <= 4096,
              let prior = try JSONSerialization.jsonObject(with: bytes) as? [String: Any],
              Set(prior.keys) == ["identity", "challenge"],
              let priorIdentity = prior["identity"] as? [String: Any],
              Set(priorIdentity.keys) == ["run_id", "boot_id", "pid", "bundle_id", "executable"],
              priorIdentity["run_id"] as? String == runId,
              priorIdentity["bundle_id"] as? String == identity["bundle_id"] as? String,
              priorIdentity["executable"] as? String == identity["executable"] as? String,
              let bootId = priorIdentity["boot_id"] as? String, NativeProcessIdentityFiles.isCanonicalUuid(bootId),
              let pid = priorIdentity["pid"] as? NSNumber, CFGetTypeID(pid) != CFBooleanGetTypeID(),
              pid.doubleValue > 0, pid.doubleValue <= Double(Int32.max), pid.doubleValue == Double(pid.int64Value),
              prior["challenge"] is NSNull || (prior["challenge"] as? String).map(NativeProcessIdentityFiles.isCanonicalUuid) == true else {
            throw NativeProcessIdentityFiles.InvalidIdentity()
        }
        // History is written by this native producer in sorted, compact form.
        // Do not replace ambiguous/duplicate-key or otherwise noncanonical bytes
        // with a sentinel and thereby hide evidence the host would reject.
        guard try JSONSerialization.data(withJSONObject: prior, options: [.sortedKeys]) == bytes,
              (!inspectionPrepared && bootId != identity["boot_id"] as? String)
                || priorIdentity as NSDictionary == identity as NSDictionary else {
            throw NativeProcessIdentityFiles.InvalidIdentity()
        }
    }

    private func respond() {
        guard (try? NativeProcessIdentityFiles.validate(root)) != nil,
              let handle = try? FileHandle(forReadingFrom: root.appendingPathComponent("identity-request.json")) else { return }
        defer { try? handle.close() }
        // Bound the read itself, not just the size of an already loaded file.
        guard let requestData = try? handle.read(upToCount: 1025), requestData.count <= 1024,
              let request = try? JSONSerialization.jsonObject(with: requestData) as? [String: String],
              Set(request.keys) == ["boot_id", "challenge"],
              let bootId = request["boot_id"], NativeProcessIdentityFiles.isCanonicalUuid(bootId),
              bootId == identity["boot_id"] as? String,
              let challenge = request["challenge"], NativeProcessIdentityFiles.isCanonicalUuid(challenge) else { return }
        // Accept only canonical boot_id/challenge key order and literal UUIDs.
        // Semantic checks exclude whitespace within keys/values, so removing ASCII
        // JSON whitespace is safe. Sorted-key bytes reject duplicates and escaped
        // spellings Foundation collapses, but retain legacy json.dumps spaces,
        // compact sorted requests and whitespace padding.
        guard let canonical = try? JSONSerialization.data(withJSONObject: request, options: [.sortedKeys]),
              canonical == Data(requestData.filter { $0 != 0x20 && $0 != 0x09 && $0 != 0x0a && $0 != 0x0d }),
              let response = try? JSONSerialization.data(
                  withJSONObject: ["identity": identity, "challenge": challenge], options: [.sortedKeys]
              ), (try? NativeProcessIdentityFiles.validate(root)) != nil else { return }
        try? response.write(to: root.appendingPathComponent("native-inspection.json"), options: .atomic)
    }

    deinit {
        CFNotificationCenterRemoveObserver(
            CFNotificationCenterGetDarwinNotifyCenter(), Unmanaged.passUnretained(self).toOpaque(), nil, nil
        )
    }
}

// Preserve the HIL v2 receipt, files and notification namespace. Republishing
// the same native launch must not turn a live boot into a new identity.
private var nativeHilIdentityResponder: NativeProcessIdentityResponder?

func publishNativeHilIdentity(path: String, runId: String) throws -> String {
    guard NativeProcessIdentityFiles.isCanonicalUuid(runId),
          Bundle.main.object(forInfoDictionaryKey: "BleHubNativeHilProtocolVersion") as? Int == 2 else {
        throw RfFixtureConfiguration.InvalidConfiguration(description: "invalid native HIL launch identity")
    }
    let root = URL(fileURLWithPath: path).resolvingSymlinksInPath().standardizedFileURL
    if let responder = nativeHilIdentityResponder {
        guard responder.matches(root: root, runId: runId) else { throw NativeProcessIdentityFiles.InvalidIdentity() }
        return try responder.publish()
    }
    let responder = try NativeProcessIdentityResponder(root: root, runId: runId, namespace: .nativeHil, context: .current)
    let receipt = try responder.publish()
    nativeHilIdentityResponder = responder
    return receipt
}

struct RfFixtureConfiguration: Decodable {
    let profileId: String
    let serviceUuid: String
    let hasIndicationOnly: Bool
    let initialValueHex: String
    let notificationValueHex: String
    let writeWithResponseHex: String
    let writeWithoutResponseHex: String

    static let android = RfFixtureConfiguration(
        profileId: "blehub-android-peripheral-v1",
        serviceUuid: "fff00000-0000-4000-8000-000000000000",
        hasIndicationOnly: true,
        initialValueHex: "00",
        notificationValueHex: "deadbeef",
        writeWithResponseHex: "102030",
        writeWithoutResponseHex: "abcd"
    )

    struct InvalidConfiguration: Error, CustomStringConvertible {
        let description: String
    }

    static func scanNamePrefix(_ arguments: [String]) throws -> String? {
        guard let index = arguments.firstIndex(of: "--scan-name-prefix") else {
            if arguments.contains("--native-hil") {
                throw InvalidConfiguration(description: "native HIL requires explicit --scan-name-prefix")
            }
            return nil
        }
        guard arguments.indices.contains(index + 1), !arguments[index + 1].isEmpty else {
            throw InvalidConfiguration(description: "missing scan name prefix")
        }
        return arguments[index + 1]
    }

    static func fromArguments(_ arguments: [String], serviceUuid: String?) throws -> Self {
        guard let index = arguments.firstIndex(of: "--rf-fixture-config") else { return .android }
        guard arguments.indices.contains(index + 1) else {
            throw InvalidConfiguration(description: "missing --rf-fixture-config JSON")
        }
        let config = try JSONDecoder().decode(Self.self, from: Data(arguments[index + 1].utf8))
        guard let serviceUuid, let expected = UUID(uuidString: serviceUuid),
              UUID(uuidString: config.serviceUuid) == expected,
              config.profileId.range(of: "^[A-Za-z0-9_-]{1,128}$", options: .regularExpression) != nil else {
            throw InvalidConfiguration(description: "fixture profile identity/service UUID mismatch")
        }
        let values = try [config.initialValue, config.writeWithResponse, config.writeWithoutResponse]
        guard Set(values).count == 3 else {
            throw InvalidConfiguration(description: "initial and both write values must differ")
        }
        return config
    }

    private func decodeHex(_ value: String) throws -> Data {
        guard value.range(of: "^(?:[0-9a-fA-F]{2}){1,512}$", options: .regularExpression) != nil else {
            throw InvalidConfiguration(description: "IO values must be 1–512 bytes of unseparated hexadecimal")
        }
        let bytes = Array(value.utf8)
        return Data(stride(from: 0, to: bytes.count, by: 2).map {
            UInt8(String(decoding: bytes[$0 ..< $0 + 2], as: UTF8.self), radix: 16)!
        })
    }

    var initialValue: Data { get throws { try decodeHex(initialValueHex) } }
    var notificationValue: Data { get throws { try decodeHex(notificationValueHex) } }
    var writeWithResponse: Data { get throws { try decodeHex(writeWithResponseHex) } }
    var writeWithoutResponse: Data { get throws { try decodeHex(writeWithoutResponseHex) } }

    var evidence: [String] {
        [
            "apple_rf_profile_id=\(profileId)",
            "apple_rf_service_uuid=\(serviceUuid)",
            "apple_rf_indication_only_required=\(hasIndicationOnly)",
            "apple_rf_initial_value=\(initialValueHex)",
            "apple_rf_notification_value=\(notificationValueHex)",
            "apple_rf_write_with_response_value=\(writeWithResponseHex)",
            "apple_rf_write_without_response_value=\(writeWithoutResponseHex)",
        ]
    }
}
