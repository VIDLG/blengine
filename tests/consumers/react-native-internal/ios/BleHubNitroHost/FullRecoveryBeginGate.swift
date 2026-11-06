import Foundation

/// Host-only phase ledger; it authorizes URLs, never fabricates a BLE result.
struct FullRecoveryBeginGate {
  let runId: String
  private(set) var nonce: String?
  private(set) var awaiting = false
  private(set) var begun = false
  private(set) var closed = false
  private(set) var failed = false
  private var suitePassed = false

  mutating func observe(_ message: String) {
    let line = message.components(separatedBy: "BLEHUB_NITRO_RUNTIME ").last ?? message
    if line.hasPrefix("rf-lab-full:runtime:") {
      let prefix = "rf-lab-full:runtime:session=\(runId):nonce="
      guard line.hasPrefix(prefix) else { failed = true; return }
      let parts = String(line.dropFirst(prefix.count)).components(separatedBy: ":phase=")
      guard parts.count == 2, !parts[0].isEmpty, !failed else { failed = true; return }
      if parts[1] == "start", nonce == nil, !closed { nonce = parts[0] }
      else if parts[1] == "close", nonce == parts[0], !closed { closed = true }
      else { failed = true }
    } else if line.hasPrefix("rf-lab-suite:runtime:") {
      guard let nonce,
            line == "rf-lab-suite:runtime:session=\(runId):nonce=\(nonce):phase=start" ||
            line == "rf-lab-suite:runtime:session=\(runId):nonce=\(nonce):phase=close" else { failed = true; return }
    } else if line == "rf-lab-suite:passed:flows=10:passedCount=10:failureCount=0" {
      if nonce == nil || suitePassed || begun || closed { failed = true }
      else { suitePassed = true }
    } else if line.hasPrefix("rf-lab-full:recovery-awaiting:") {
      if line != "rf-lab-full:recovery-awaiting:runId=\(runId)" || !suitePassed || awaiting || begun || closed || failed { failed = true }
      else { awaiting = true }
    } else if line.hasPrefix("rf-lab-full:failed:") || line.hasPrefix("rf-lab-suite:failed:") || line.hasPrefix("rf-lab-suite:flow-failed:") {
      failed = true
    }
  }

  mutating func acceptBegin(_ url: String, controlsComplete: Bool) -> Bool {
    guard !failed, !closed, awaiting, !begun, controlsComplete,
          url == "blehub-rn-spike://recover/\(runId)/begin" else { failed = true; return false }
    begun = true
    awaiting = false
    return true
  }
}

#if DEBUG
// Foundation-only debug entry shared by the app and hostless native tests.
@objc(BleHubFullRecoveryGateProbe)
final class FullRecoveryGateProbe: NSObject {
  @objc static func evaluate(_ input: NSDictionary) -> NSDictionary {
    guard let runId = input["runId"] as? String,
          let markers = input["markers"] as? [String],
          let urls = input["urls"] as? [String],
          let complete = input["complete"] as? [Bool], urls.count == complete.count else {
      return ["invalid": true]
    }
    var gate = FullRecoveryBeginGate(runId: runId)
    markers.forEach { gate.observe($0) }
    let accepted = zip(urls, complete).map { gate.acceptBegin($0.0, controlsComplete: $0.1) }
    return ["accepted": accepted, "failed": gate.failed, "begun": gate.begun, "nonce": gate.nonce ?? ""]
  }
}
#endif
