import BleHubUniFfi
import Foundation

/// Native Swift seam for the Rust Supervisor ownership path.
///
/// The former Swift lifecycle supervisor is archived. This type constructs the Rust Supervisor
/// and owns its initial start/close lifecycle; recovery actions remain internal to the fenced
/// Supervisor flow and are not exposed as a JS `replace()` method.
final class AppleRustSupervisorOwner: @unchecked Sendable {
  let supervisor: Supervisor
  let factory: ApplePlatformSessionFactory

  /// `recoveryTarget` is injected by a host/provider that owns the live fixture contract.
  /// The RN lifecycle bridge leaves it nil because start/close do not perform recovery.
  init(
    queueLabel: String,
    recoveryTarget: ForeignRecoveryTarget? = nil,
    oldLink: ForeignRecoveryLink? = nil,
    expectedOldOperationIds: [String] = [],
    allowCentralRecreation: Bool = false,
    allowFreshOwner: Bool = true,
    eventSink: (@Sendable (BleEvent) -> Void)? = nil,
    scanEventSink: (@Sendable (ForeignScanEventEnvelope) -> Void)? = nil,
    backendFactory: (@Sendable (String) throws -> ApplePlatformBackendLease)? = nil
  ) throws {
    let context = ForeignOwnerContext(
      ownerId: UUID().uuidString,
      backendId: UUID().uuidString,
      ownerGeneration: 1,
      recoveryAttempt: 1
    )
    let config = ForeignSupervisorConfig(
      recovery: ForeignRecoveryPolicyConfig(
        initialContext: context,
        oldLink: oldLink,
        expectedOldOperationIds: expectedOldOperationIds,
        allowCentralRecreation: allowCentralRecreation,
        allowFreshOwner: allowFreshOwner
      ),
      recoveryTarget: recoveryTarget
    )
    let factory = ApplePlatformSessionFactory(
      queueLabel: queueLabel,
      eventSink: eventSink,
      scanEventSink: scanEventSink,
      backendFactory: backendFactory
    )
    self.factory = factory
    supervisor = try Supervisor(
      factory: factory,
      config: config
    )
  }

  func start() async throws -> ForeignOwnerStart {
    try await supervisor.start()
  }

  func close() async throws -> [ForeignResourceCount] {
    try await supervisor.shutdown()
  }

  func closeResult() async -> ForeignOwnerClose {
    await supervisor.shutdownView()
  }

}

/// Shared only for admission serialization; carries no root, Supervisor or native owner handle.
final class AppleEngineAdmission: @unchecked Sendable {
  fileprivate let lock = NSLock()
  fileprivate var invalidated = false

  /// Holds admission through Rust's bounded submit, never through an async wait or BLE callback.
  func submit<Value>(_ action: () -> Value) -> Value? {
    lock.withLock {
      guard !invalidated else { return nil }
      return action()
    }
  }
}

/// A synchronous submission capability, published only from the actual successful start receipt.
/// A ticket fences both delayed actor admission and publication after an awaited start.
final class AppleStartedSessionGate: @unchecked Sendable {
  struct StartTicket: Sendable {
    fileprivate let revision: UInt64
  }

  enum StartFailure: LocalizedError {
    case supersededByClose
    case engineInvalidated
    case engineUnbound

    var errorDescription: String? {
      switch self {
      case .supersededByClose: return "Session start was superseded by close."
      case .engineInvalidated: return "Session belongs to an invalidated React engine."
      case .engineUnbound: return "Session is not bound to a React engine."
      }
    }

    /// Stable code for the JS failure union; display text stays in `errorDescription`.
    var blengCode: String {
      switch self {
      case .supersededByClose: return "superseded_by_close"
      case .engineInvalidated: return "engine_invalidated"
      case .engineUnbound: return "engine_unbound"
      }
    }
  }

  struct Receipt {
    let supervisor: Supervisor
    let context: ForeignOwnerContext
  }

  let engineAdmission = AppleEngineAdmission()
  private var lock: NSLock { engineAdmission.lock }
  private var revision: UInt64 = 0
  private var receipt: Receipt?

  func captureTicket() -> StartTicket {
    lock.withLock { StartTicket(revision: revision) }
  }

  func validate(ticket: StartTicket) throws {
    try lock.withLock {
      guard !engineAdmission.invalidated else { throw StartFailure.engineInvalidated }
      guard revision == ticket.revision else { throw StartFailure.supersededByClose }
    }
  }

  func publish(supervisor: Supervisor, start: ForeignOwnerStart, ticket: StartTicket) {
    lock.withLock {
      guard !engineAdmission.invalidated, revision == ticket.revision else { return }
      receipt = Receipt(supervisor: supervisor, context: start.context)
    }
  }

  func revoke() {
    lock.withLock {
      revision &+= 1
      receipt = nil
    }
  }

  func invalidateEngine() {
    lock.withLock {
      engineAdmission.invalidated = true
      revision &+= 1
      receipt = nil
    }
  }

  func submit<Value>(_ action: (Receipt?) -> Value) -> Value? {
    engineAdmission.submit { action(receipt) }
  }
}
