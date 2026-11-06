import BleHubUniFfi
import Foundation
import NitroModules

private actor AppleBlengSessionLifecycle {
  nonisolated let scanGate = AppleStartedSessionGate()
  private let ownerFactory: @Sendable () throws -> AppleRustSupervisorOwner
  private var owner: AppleRustSupervisorOwner?
  private var terminalClose: BlengCloseResult?

  init(ownerFactory: @escaping @Sendable () throws -> AppleRustSupervisorOwner) {
    self.ownerFactory = ownerFactory
  }

  func start(ticket: AppleStartedSessionGate.StartTicket) async throws -> BlengAdapterSnapshot {
    try scanGate.validate(ticket: ticket)
    let owner: AppleRustSupervisorOwner
    if let current = self.owner {
      owner = current
    } else {
      owner = try ownerFactory()
      self.owner = owner
      terminalClose = nil
    }
    let start = try await owner.start()
    scanGate.publish(supervisor: owner.supervisor, start: start, ticket: ticket)
    return BlengAdapterSnapshot(adapterSnapshot: start.adapterSnapshot)
  }

  func close() async -> BlengCloseResult {
    // A newer start may enter the actor before this queued close.
    scanGate.revoke()
    if let terminalClose { return terminalClose }
    guard let owner else {
      let result = BlengCloseResult(ownerClosed: true, failure: nil)
      terminalClose = result
      return result
    }
    let receipt = await owner.closeResult()
    let result = BlengCloseResult(
      ownerClosed: receipt.ownerClosed,
      failure: receipt.failure.map { BlengCloseFailure(code: $0.code, message: $0.message) }
    )
    // Actor reentrancy may let another close finish and an explicit start replace this owner.
    if receipt.ownerClosed, self.owner === owner {
      self.owner = nil
      terminalClose = result
    }
    return result
  }
}

/// iOS Nitro lifecycle and scan bridge for the formal BLeng package line.
///
/// The bridge serializes lifecycle calls through one native supervisor and does not expose
/// CoreBluetooth objects, UniFFI handles, operation IDs, or recovery policy to JavaScript.
final class HybridBlengSessionOwner: HybridBlengSessionOwnerSpec {
  private let lifecycle: AppleBlengSessionLifecycle
  private let beforeStartAdmission: (@Sendable () async -> Void)?

  override init() {
    lifecycle = AppleBlengSessionLifecycle {
      try AppleRustSupervisorOwner(queueLabel: "BleHubReactNativeBleng")
    }
    beforeStartAdmission = nil
    super.init()
  }

  // Native-only injection; the hook permits deterministic tests of delayed actor admission.
  init(
    ownerFactory: @escaping @Sendable () throws -> AppleRustSupervisorOwner,
    beforeStartAdmission: (@Sendable () async -> Void)? = nil
  ) {
    lifecycle = AppleBlengSessionLifecycle(ownerFactory: ownerFactory)
    self.beforeStartAdmission = beforeStartAdmission
    super.init()
  }

  func start() throws -> Promise<BlengAdapterSnapshot> {
    let ticket = lifecycle.scanGate.captureTicket()
    return run { [lifecycle, beforeStartAdmission] in
      await beforeStartAdmission?()
      return try await lifecycle.start(ticket: ticket)
    }
  }

  func beginScan(options: BlengScanOptions) throws -> BlengScanSubmission {
    guard let receipt = lifecycle.scanGate.snapshot() else {
      return .second(BlengScanFailure(failure: BlengScanMapping.submissionUnavailable()))
    }
    return BlengScanMapping.submission(receipt.supervisor.beginScanView(
      context: receipt.context,
      options: ForeignScanOptions(
        serviceUuids: options.serviceUuids,
        namePrefix: options.namePrefix,
        companyId: options.companyId,
        allowDuplicates: options.allowDuplicates
      )
    ))
  }

  func close() throws -> Promise<BlengCloseResult> {
    // Revoke before scheduling the actor hop, not after an asynchronous shutdown starts.
    lifecycle.scanGate.revoke()
    return run { [lifecycle] in await lifecycle.close() }
  }

  private func run<Value>(_ action: @escaping () async throws -> Value) -> Promise<Value> {
    let promise = Promise<Value>()
    Task {
      do {
        let result = try await action()
        promise.resolve(withResult: result)
      } catch {
        promise.reject(withError: error)
      }
    }
    return promise
  }
}

extension BlengAdapterSnapshot {
  init(adapterSnapshot snapshot: AdapterSnapshot) {
    let state: BlengAdapterState
    switch snapshot.state {
    case .unknown: state = .unknown
    case .unavailable: state = .unavailable
    case .poweredOff: state = .poweredoff
    case .poweredOn: state = .poweredon
    }

    func authorization(_ value: AuthorizationState) -> BlengAuthorizationState {
      switch value {
      case .notRequired: return .notrequired
      case .notDetermined: return .notdetermined
      case .denied: return .denied
      case .restricted: return .restricted
      case .authorized: return .authorized
      }
    }

    self.init(
      state: state,
      scanAuthorization: authorization(snapshot.scanAuthorization),
      connectionAuthorization: authorization(snapshot.connectionAuthorization),
      name: snapshot.name
    )
  }
}
