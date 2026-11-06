import Dispatch
import Foundation

/// Opt-in stderr evidence only. Never retains native BLE objects or participates in routing.
internal final class AppleNotificationDiagnostics {
    static let environmentKey = "BLEHUB_APPLE_NOTIFICATION_DIAGNOSTICS"
    static let prefix = "BLEHUB_APPLE_NOTIFICATION_DIAGNOSTIC "
    static let recordLimit = 128
    static let byteLimit = 3000

    let isEnabled: Bool
    private let owner = UUID().uuidString.lowercased()
    private let lock = NSLock()
    private var records = 0
    private let write: (Data) -> Void

    init(
        enabled: Bool = ProcessInfo.processInfo.environment[environmentKey] == "1",
        write: @escaping (Data) -> Void = { try? FileHandle.standardError.write(contentsOf: $0) }
    ) {
        isEnabled = enabled
        self.write = write
    }

    func record(_ event: String, fields: @autoclosure () -> [String: Any]) {
        guard isEnabled else { return }
        lock.withLock {
            guard records < Self.recordLimit else { return }
            records += 1
            var row: [String: Any] = [
                "owner": owner, "ownerKind": "backend", "pid": ProcessInfo.processInfo.processIdentifier,
                "sequence": records, "uptimeNs": String(DispatchTime.now().uptimeNanoseconds),
                "event": bounded(event, bytes: 80),
            ]
            if records == Self.recordLimit {
                row["event"] = "overflow"
                row["suppressedEvent"] = bounded(event, bytes: 80)
                row["recordLimit"] = Self.recordLimit
                row["droppedAtLeast"] = 1
            } else {
                let source = fields()
                var values: [String: Any] = [:]
                var truncated: [String] = []
                for key in source.keys.sorted().prefix(48) {
                    let name = bounded(key, bytes: 64)
                    let value = source[key]!
                    if let text = value as? String {
                        let short = bounded(text, bytes: 160)
                        values[name] = short
                        if short != text { truncated.append(name) }
                    } else if value is NSNull || value is Bool || value is Int {
                        values[name] = value
                    } else {
                        values[name] = "unsupported-field-type"
                        truncated.append(name)
                    }
                    if name != key { truncated.append(name) }
                }
                row["fields"] = values
                row["truncated"] = !truncated.isEmpty || source.count > 48 || event.utf8.count > 80
                if !truncated.isEmpty { row["truncatedFields"] = truncated }
                if source.count > 48 { row["omittedFields"] = source.count - 48 }
            }
            guard var data = encode(row) else { return }
            if data.count > Self.byteLimit {
                // Keep framing/identity and make the missing diagnostic payload explicit.
                row.removeValue(forKey: "fields")
                row.removeValue(forKey: "truncatedFields")
                row["truncated"] = true
                row["truncationReason"] = "record-byte-limit"
                row["originalBytes"] = data.count
                guard let fallback = encode(row) else { return }
                data = fallback
            }
            guard data.count <= Self.byteLimit else { return }
            write(data)
        }
    }

    private func bounded(_ value: String, bytes: Int) -> String {
        var result = String(value.prefix(bytes))
        while result.utf8.count > bytes { result.removeLast() }
        return result
    }

    private func encode(_ row: [String: Any]) -> Data? {
        guard let json = try? JSONSerialization.data(withJSONObject: row, options: [.sortedKeys]) else { return nil }
        var data = Data(Self.prefix.utf8)
        data.append(json)
        data.append(0x0a)
        return data
    }
}
