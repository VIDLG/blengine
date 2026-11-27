import BleHubUniFfi
import Foundation
import NitroModules

/// The one native projection of Rust's operation failure. Scan and connection views classify the
/// same failure with this code instead of keeping a second mapping.
enum BlengFailureMapping {
  static func bridgeFailure(domain: BlengFailureDomain, message: String) -> BlengFailureInfo {
    BlengFailureInfo(domain: domain, code: "bridgeFailure", message: message, platform: nil)
  }

  /// The one classification of a supervisor boundary error. `Timeout` keeps its own stable code so
  /// callers can distinguish a bounded owner-assembly wait from a genuine platform failure.
  static func supervisorFailure(_ value: SupervisorFfiError) -> BlengFailureInfo {
    switch value {
    case .InvalidContext(let detail):
      return BlengFailureInfo(
        domain: .input, code: "bleng.invalid_request.context", message: detail, platform: nil)
    case .InvalidConfiguration(let detail):
      return BlengFailureInfo(
        domain: .input, code: "bleng.invalid_request.configuration", message: detail, platform: nil)
    case .NoActiveContext:
      return BlengFailureInfo(
        domain: .lifecycle, code: "submissionUnavailable",
        message: "Supervisor has no active owner context.", platform: nil)
    case .Timeout(let detail):
      return BlengFailureInfo(domain: .lifecycle, code: "bleng.timeout", message: detail, platform: nil)
    case .Busy:
      return BlengFailureInfo(
        domain: .lifecycle, code: "supervisor.busy",
        message: "Supervisor operation is already in progress.", platform: nil)
    case .Platform(let detail):
      return BlengFailureInfo(domain: .lifecycle, code: "bridgeFailure", message: detail, platform: nil)
    }
  }

  static func failure(_ value: ForeignOperationFailure) -> BlengFailureInfo {
    let domain: BlengFailureDomain
    switch value.domain {
    case .input: domain = .input
    case .submission: domain = .submission
    case .operation: domain = .operation
    case .observation: domain = .observation
    case .delivery: domain = .delivery
    case .lifecycle: domain = .lifecycle
    }
    return BlengFailureInfo(
      domain: domain,
      code: value.code,
      message: value.message,
      platform: value.platformDetails.map(platformDetails)
    )
  }

  static func command(_ value: ForeignOperationCommandResult) -> BlengReceipt {
    BlengReceipt(failure: value.failure.map(failure))
  }

  private static func platform(_ value: Platform) -> String {
    switch value {
    case .apple: return "apple"
    case .android: return "android"
    case .windows: return "windows"
    case .linux: return "linux"
    case .test: return "test"
    }
  }

  private static func platformDetails(_ value: PlatformErrorDetails) -> BlengPlatformDetails {
    BlengPlatformDetails(
      platform: platform(value.platform), code: value.code, name: value.name, message: value.message,
      status: value.status.map { Double($0) }, attCode: value.attCode.map { Double($0) }
    )
  }
}

/// Resolves a bridge Promise from one Rust operation result; never invents a native outcome.
func blengPromise<Value>(_ action: @escaping () async -> Value) -> Promise<Value> {
  let promise = Promise<Value>()
  Task {
    promise.resolve(withResult: await action())
  }
  return promise
}
