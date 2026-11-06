import Foundation

/// Named standalone gate; no BLE state or additional JS/native API.
struct NarrowRejectControlGate {
  let mode: String
  private let flow: Int
  let session: String
  let token: String
  private(set) var failed = false
  private var nonce: String?
  private var listener = false
  private var ready = false
  private var next = 0
  private var closed = false
  static let cccPhases = ["armed", "held", "rejected", "value-sent", "disable-allowed", "silence-started", "close-allowed"]
  static let cccRequests = ["arm", "enable-dispatched", "reject", "recovery-enabled", "recovery-notified", "disabled", "silence-done"]
  static let cccDisablePhases = ["baseline-value-sent", "armed", "held", "rejected", "continued-value-sent", "retry-allowed", "silence-started", "close-allowed"]
  static let cccDisableRequests = ["baseline-enabled", "arm", "disable-dispatched", "reject", "reject-observed", "continued-notified", "disabled", "silence-done"]
  private let phases: [String]
  private let requests: [String]

  init?(_ raw: String, mode: String = "rf-write-reject") {
    guard mode == "rf-write-reject" || mode == "rf-ccc-enable-reject" || mode == "rf-ccc-disable-reject" else { return nil }
    let flow = mode == "rf-ccc-disable-reject" ? 12 : mode == "rf-ccc-enable-reject" ? 11 : 10
    guard let data = raw.data(using: .utf8),
          let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          Set(value.keys) == Set(["session", "flow", "mode", "token"]),
          value["flow"] as? Int == flow, value["mode"] as? String == mode,
          let session = value["session"] as? String, UUID(uuidString: session)?.uuidString.lowercased() == session,
          let token = value["token"] as? String, UUID(uuidString: token)?.uuidString.lowercased() == token else { return nil }
    self.mode = mode
    self.flow = flow
    self.phases = mode == "rf-ccc-disable-reject" ? Self.cccDisablePhases : mode == "rf-ccc-enable-reject" ? Self.cccPhases : ["armed", "held", "rejected", "close-allowed"]
    self.requests = mode == "rf-ccc-disable-reject" ? Self.cccDisableRequests : mode == "rf-ccc-enable-reject" ? Self.cccRequests : ["arm", "write-dispatched", "reject", "recovery-done"]
    self.session = session
    self.token = token
  }

  private var identity: String { "session=\(session):flow=\(flow):mode=\(mode):token=\(token)" }
  mutating func fail() { failed = true }

  mutating func observe(_ message: String) {
    let line = message.components(separatedBy: "BLEHUB_NITRO_RUNTIME ").last ?? message
    if line.hasPrefix("\(mode):runtime:") {
      let prefix = "\(mode):runtime:session=\(session):nonce="
      guard line.hasPrefix(prefix), !failed else { fail(); return }
      let parts = String(line.dropFirst(prefix.count)).components(separatedBy: ":phase=")
      guard parts.count == 2, !parts[0].isEmpty else { fail(); return }
      if parts[1] == "start", nonce == nil, !closed { nonce = parts[0] }
      else if parts[1] == "close", nonce == parts[0], !closed { closed = true }
      else { fail() }
    } else if line.hasPrefix("rf-control:listener:") {
      guard nonce != nil, !listener, !closed, !failed,
            line == "rf-control:listener:\(identity):phase=registered" else { fail(); return }
      listener = true
    } else if line.hasPrefix("rf-control:ready:") {
      guard listener, !ready, !closed, !failed, next < requests.count,
            line == "rf-control:ready:\(identity):phase=\(requests[next])" else { fail(); return }
      ready = true
    } else if line.hasPrefix("\(mode):failed:") { fail() }
  }

  mutating func accept(_ url: String) -> Bool {
    guard !failed, !closed, listener, ready, next < phases.count,
          url == "blehub-rf://control/\(session)/\(flow)/\(mode)/\(token)/\(phases[next])" else { fail(); return false }
    next += 1
    ready = false
    return true
  }
}
