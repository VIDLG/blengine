import Foundation

struct AdapterStartupIdentity: Encodable {
  let run_id: String
  let adapter_mode = "startup-off"
  let pid: Int32
  let boot_id: String
  let bundle_id: String
  let executable: String
  var source = "launch"
}

struct AdapterStartupLaunch {
  static let optionPrefix = "--blehub-nitro-rf-adapter-"
  let identity: AdapterStartupIdentity
  let properties: [String: String]

  static func requested(_ arguments: [String]) -> Bool {
    arguments.contains { $0.hasPrefix(optionPrefix) }
  }

  init?(arguments: [String], authorized: Bool, pid: Int32, bootId: String,
        bundleId: String?, executable: URL?) {
    let allowed = Set(["service", "run-id", "mode", "local-name"].map { Self.optionPrefix + $0 })
    guard authorized, Self.requested(arguments), pid > 0,
          Self.canonicalUUID(bootId), bundleId == "org.reactjs.native.example.BleHubNitroHost",
          let executable, executable.isFileURL, executable.host == nil || executable.host == "",
          executable.query == nil, executable.fragment == nil,
          !executable.path.hasSuffix("/"), !executable.path.isEmpty,
          !executable.path.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) }) else { return nil }
    var values: [String: String] = [:]
    for (index, option) in arguments.enumerated() where option.hasPrefix("--blehub-nitro-") {
      guard allowed.contains(option), values[option] == nil, arguments.indices.contains(index + 1),
            !arguments[index + 1].hasPrefix("--") else { return nil }
      values[option] = arguments[index + 1]
    }
    guard let service = values[Self.optionPrefix + "service"], Self.canonicalUUID(service),
          let runId = values[Self.optionPrefix + "run-id"], Self.canonicalUUID(runId),
          values[Self.optionPrefix + "mode"] == nil || values[Self.optionPrefix + "mode"] == "startup-off",
          let name = values[Self.optionPrefix + "local-name"], !name.isEmpty, name.utf8.count <= 128,
          name == name.trimmingCharacters(in: .whitespacesAndNewlines),
          !name.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) }) else { return nil }
    let identity = AdapterStartupIdentity(run_id: runId, pid: pid, boot_id: bootId,
      bundle_id: bundleId!, executable: executable.absoluteString)
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
    guard let data = try? encoder.encode(identity), let json = String(data: data, encoding: .utf8) else { return nil }
    self.identity = identity
    properties = ["runtimeMode": "rf-adapter-transition", "rfAdapterMode": "startup-off",
      "rfAdapterRunId": runId, "rfServiceUuid": service, "rfExpectedLocalName": name,
      "rfAdapterNativeIdentity": json]
  }

  static func canonicalUUID(_ value: String) -> Bool {
    value.utf8.count == 36 && UUID(uuidString: value)?.uuidString.lowercased() == value
  }
}

/// Authorizes only startup-off URL boundaries; it does not infer native BLE results.
struct AdapterStartupGate {
  static let prefix = "BLEHUB_NITRO_RUNTIME rf-adapter-transition:"
  static let resourceFields: Set<String> = [
    "activeScans", "connections", "gattAttributes", "subscriptions", "pendingIo", "watchdogs",
    "callbackRoutes", "ingressRoots", "adapterDelegates", "peerCacheEntries", "backendLeases",
    "hubRoots", "handoffRoots", "handoffPending", "listeners", "workerThreads", "lifecycleOwners",
  ]
  private enum Phase {
    case start, off, offZero, offPassed, awaiting, ready, hostReady, requested, signalled,
         on, onZero, onPassed, closed, passed
  }
  let runId: String
  private(set) var nonce: String?
  private(set) var failure: String?
  private var phase = Phase.start
  var failed: Bool { failure != nil }

  mutating func fail(_ reason: String) {
    if failure == nil { failure = reason }
  }

  mutating func observe(_ message: String) {
    guard !failed else { return }
    guard message.utf8.count <= AdapterMarkerJournal.maxLineBytes,
          message.hasPrefix(Self.prefix), !message.contains("\n"), !message.contains("\r") else {
      fail("foreign-or-unbounded-marker"); return
    }
    let line = String(message.dropFirst(Self.prefix.count))
    let runtime = "runtime:runId=\(runId):nonce="
    if line.hasPrefix(runtime) {
      let parts = String(line.dropFirst(runtime.count)).components(separatedBy: ":phase=")
      guard parts.count == 2, parts[0].utf8.count <= 128,
            parts[0].range(of: "^[0-9a-z]+-[0-9a-z]+-[0-9a-z]+$", options: .regularExpression) != nil else {
        fail("runtime-nonce-or-phase"); return
      }
      if parts[1] == "start", nonce == nil, phase == .start {
        nonce = parts[0]; phase = .off
      } else if parts[1] == "close", nonce == parts[0], nonce != nil,
                phase != .closed, phase != .passed {
        phase = .closed
      } else { fail("runtime-nonce-or-phase") }
      return
    }
    switch line {
    case let value where value.hasPrefix("off:event:"):
      guard phase == .off, let data = String(value.dropFirst("off:event:".count)).data(using: .utf8),
            (try? JSONSerialization.jsonObject(with: data)) is [String: Any] else { fail("off-event-phase"); return }
    case let value where value.hasPrefix("off:resources:"):
      guard phase == .off, Self.seventeenZeros(String(value.dropFirst("off:resources:".count))) else { fail("off-resource-boundary"); return }
      phase = .offZero
    case "off:passed":
      guard phase == .offZero else { fail("off-passed-phase"); return }
      phase = .offPassed
    case "awaiting-on:runId=\(runId)":
      guard phase == .offPassed else { fail("awaiting-phase"); return }
      phase = .awaiting
    case "host-window-ready:runId=\(runId):source=linking":
      guard phase == .ready else { fail("host-ready-phase"); return }
      phase = .hostReady
    case "on-request:runId=\(runId):source=ui":
      guard phase == .hostReady else { fail("on-request-phase"); return }
      phase = .requested
    case "on-signal:runId=\(runId):source=linking":
      guard phase == .signalled else { fail("on-signal-phase"); return }
      // The separate on:started marker must precede all on-flow telemetry.
    case "on:started":
      guard phase == .signalled, onSignalObserved else { fail("on-start-phase"); return }
      phase = .on
    case let value where value.hasPrefix("on:flow:rf:") || value.hasPrefix("on:flow:rf-io:"):
      guard phase == .on, !value.contains(":failed"), !value.contains(":close:rejected:") else { fail("on-flow-phase-or-failure"); return }
    case let value where value.hasPrefix("on:resources:"):
      guard phase == .on, Self.seventeenZeros(String(value.dropFirst("on:resources:".count))) else { fail("on-resource-boundary"); return }
      phase = .onZero
    case "on:passed":
      guard phase == .onZero else { fail("on-passed-phase"); return }
      phase = .onPassed
    case "passed:runId=\(runId)":
      guard phase == .closed, completedBeforeClose else { fail("passed-phase"); return }
      phase = .passed
    default: fail("unknown-stale-or-failed-marker")
    }
    if line == "on-signal:runId=\(runId):source=linking" {
      if onSignalObserved { fail("duplicate-on-signal") }
      onSignalObserved = true
    }
    if phase == .onPassed { completedBeforeClose = true }
  }

  private var onSignalObserved = false
  private var completedBeforeClose = false

  mutating func accept(_ url: String) -> String? {
    guard !failed, nonce != nil else { fail("early-or-failed-url"); return nil }
    if phase == .awaiting, url == "blehub-rn-spike://adapter/\(runId)/ready" {
      phase = .ready
      return "ready"
    }
    if phase == .requested, url == "blehub-rn-spike://adapter/\(runId)/on" {
      phase = .signalled
      return "signal"
    }
    fail("foreign-early-duplicate-or-closed-url")
    return nil
  }

  static func seventeenZeros(_ raw: String) -> Bool {
    // JSON.stringify emits plain integer zero. Check lexical members too: JSONSerialization
    // alone erases duplicate keys, negative zero and the boolean/number distinction.
    guard raw.first == "{", raw.last == "}",
          let data = raw.data(using: .utf8),
          let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          Set(object.keys) == resourceFields else { return false }
    let members = raw.dropFirst().dropLast().components(separatedBy: ",")
    guard members.count == 17 else { return false }
    return members.allSatisfy { member in
      let parts = member.components(separatedBy: ":").map { $0.trimmingCharacters(in: .whitespaces) }
      return parts.count == 2 && parts[1] == "0" && resourceFields.contains(where: { parts[0] == "\"\($0)\"" })
    }
  }
}

/// The session serializes this sole writer; successful updates replace a complete envelope.
final class AdapterMarkerJournal {
  static let maxRecords = 256
  static let maxBytes = 1024 * 1024
  static let maxLineBytes = 32768
  private struct Envelope: Encodable {
    let schema_version = 1
    let run_id: String
    let records: [String]
    let error: String?
    enum CodingKeys: String, CodingKey { case schema_version, run_id, records, error }
    func encode(to encoder: Encoder) throws {
      var container = encoder.container(keyedBy: CodingKeys.self)
      try container.encode(schema_version, forKey: .schema_version)
      try container.encode(run_id, forKey: .run_id)
      try container.encode(records, forKey: .records)
      if let error { try container.encode(error, forKey: .error) }
      else { try container.encodeNil(forKey: .error) }
    }
  }
  let url: URL
  private let runId: String
  private(set) var error: String?
  private var records: [String] = []
  private let write: (Data, URL, Data.WritingOptions) throws -> Void

  init(directory: URL, runId: String,
       write: @escaping (Data, URL, Data.WritingOptions) throws -> Void = { try $0.write(to: $1, options: $2) }) throws {
    guard AdapterStartupLaunch.canonicalUUID(runId) else { throw CocoaError(.fileWriteInvalidFileName) }
    self.runId = runId
    self.url = directory.appendingPathComponent(runId + ".json", isDirectory: false)
    self.write = write
    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    let attributes = try directory.resourceValues(forKeys: [.isSymbolicLinkKey, .isDirectoryKey])
    guard attributes.isSymbolicLink == false, attributes.isDirectory == true else {
      throw CocoaError(.fileWriteInvalidFileName)
    }
    // Exclusive initial publication refuses an existing attempt, including a dangling symlink.
    try write(encoded(records: [], error: nil), url, .withoutOverwriting)
  }

  private func encoded(records: [String], error: String?) throws -> Data {
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
    return try encoder.encode(Envelope(run_id: runId, records: records, error: error))
  }

  @discardableResult
  func append(_ message: String) -> Bool {
    guard error == nil else { return false }
    guard message.utf8.count <= Self.maxLineBytes, !message.contains("\n"), !message.contains("\r"),
          records.count < Self.maxRecords else { fail("journal-record-or-line-overflow"); return false }
    let next = records + [message]
    do {
      let data = try encoded(records: next, error: nil)
      // Reserve room for a bounded error without dropping any previously committed record.
      guard try encoded(records: next, error: String(repeating: "x", count: 128)).count <= Self.maxBytes else {
        fail("journal-byte-overflow"); return false
      }
      try write(data, url, .atomic)
      records = next
      return true
    } catch { fail("journal-write-error"); return false }
  }

  func fail(_ reason: String) {
    guard error == nil else { return }
    error = reason.utf8.count <= 128 ? reason : "journal-error-detail-overflow"
    // A persistently unwritable filesystem cannot carry its own error; memory and stderr
    // remain failed and no subsequent URL can be forwarded or success accepted.
    if let data = try? encoded(records: records, error: error) { try? write(data, url, .atomic) }
  }
}

/// One opt-in host session, no executor, timer, surface reload or BLE ownership.
final class AdapterStartupSession {
  private let lock = NSRecursiveLock()
  private var gate: AdapterStartupGate
  private let identity: AdapterStartupIdentity
  private let journal: AdapterMarkerJournal

  init(identity: AdapterStartupIdentity, journal: AdapterMarkerJournal) {
    self.identity = identity
    self.journal = journal
    gate = AdapterStartupGate(runId: identity.run_id)
  }

  var failure: String? { lock.withLock { gate.failure ?? journal.error } }

  @discardableResult
  func capture(_ message: String, fromJavaScript: Bool) -> Bool {
    lock.withLock {
      guard journal.append(message) else { gate.fail(journal.error ?? "journal-error"); return false }
      if fromJavaScript { gate.observe(message) }
      if let failure = gate.failure { journal.fail(failure) }
      return !gate.failed && journal.error == nil
    }
  }

  func fail(_ reason: String) {
    lock.withLock { gate.fail(reason); journal.fail(reason) }
  }

  func deliver(_ url: String, record: (String) -> Bool, forward: () -> Bool) -> Bool {
    let committed = lock.withLock {
      guard journal.error == nil, let kind = gate.accept(url), let nonce = gate.nonce else {
        gate.fail("url-rejected"); journal.fail(gate.failure!); return false
      }
      do {
        let data = try JSONEncoder().encode(identity)
        guard var fields = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
          fail("acceptance-encoding-failed"); return false
        }
        fields["source"] = "application-open"
        fields["url"] = url
        fields["nonce"] = nonce
        let accepted = try JSONSerialization.data(withJSONObject: fields, options: [.sortedKeys, .withoutEscapingSlashes])
        let message = AdapterStartupGate.prefix + "native:\(kind):" + String(decoding: accepted, as: UTF8.self)
        // record uses the same raw-marker hook as JS/bootstrap. Its atomic commit must
        // succeed before Linking can synchronously produce any JS acknowledgement.
        guard record(message), !gate.failed, journal.error == nil else { fail("acceptance-record-failed"); return false }
        return true
      } catch { fail("acceptance-encoding-failed"); return false }
    }
    guard committed else { return false }
    // Acceptance linearizes at its durable commit. Linking may synchronously call
    // back on another queue, so no session lock may be held across the framework.
    guard forward() else { fail("linking-rejected"); return false }
    return failure == nil
  }
}
