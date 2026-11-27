import BleHubUniFfi

/// Host-side Rust recovery owner seam. Production Apple owners implement this without exposing
/// BLE handles or recovery policy to the runner's callers.
protocol RustRecoveryOwner: AnyObject {
  func configureOldOperationBarrier(
    oldLink: ForeignRecoveryLink?,
    expectedOldOperationIds: [String]
  ) throws
  func observeExternal(signal: ForeignExternalRecoverySignal) throws -> ForeignRecoveryPlan
  func drivePlatformActionsUntilBoundary() async throws -> ForeignRecoveryPlan
  func retireForRecovery() async throws -> ForeignRecoveryPlan
  func createFreshOwner(context: ForeignOwnerContext) async throws -> ForeignRecoveryPlan
}

/// Host-side orchestration for the Rust recovery supervisor.
///
/// This runner does not interpret BLE evidence or own policy. It consumes only the first action in
/// each Rust plan, delegates platform work back to the Rust-owned session seam, and keeps
/// owner-boundary transitions explicit. It is deliberately outside Nitro/JS and production policy.
final class RustSupervisorRecoveryRunner {
  enum Failure: Error, CustomStringConvertible {
    case transitionLimitExceeded(Int)
    case freshOwnerContextRequired

    var description: String {
      switch self {
      case .transitionLimitExceeded(let limit):
        return "Rust recovery runner exceeded its transition limit: \(limit)"
      case .freshOwnerContextRequired:
        return "Rust recovery runner requires a provider-supplied fresh-owner context"
      }
    }
  }

  private let owner: any RustRecoveryOwner
  private let transitionLimit: Int

  init(owner: any RustRecoveryOwner, transitionLimit: Int = 32) {
    precondition(transitionLimit > 0, "recovery runner transition limit must be positive")
    self.owner = owner
    self.transitionLimit = transitionLimit
  }

  /// Installs baseline recovery evidence before the first loss observation.
  func configureOldOperationBarrier(
    oldLink: ForeignRecoveryLink?,
    expectedOldOperationIds: [String]
  ) throws {
    try owner.configureOldOperationBarrier(
      oldLink: oldLink,
      expectedOldOperationIds: expectedOldOperationIds
    )
  }

  /// Drives one recovery transition until Rust returns a terminal plan or the next owner boundary.
  ///
  /// Platform actions are never reinterpreted or retried here. Rust owns their ordering and
  /// sequence fencing; the host only supplies the fresh context after the provider has established
  /// the new native identity.
  func run(
    signal: ForeignExternalRecoverySignal,
    freshOwnerContext: ForeignOwnerContext? = nil
  ) async throws -> ForeignRecoveryPlan {
    var plan = try owner.observeExternal(signal: signal)
    for _ in 0..<transitionLimit {
      guard let action = plan.actions.first else { return plan }
      switch action {
      case .retireOldOwner:
        plan = try await owner.retireForRecovery()
      case .createFreshOwner:
        guard let freshOwnerContext else {
          throw Failure.freshOwnerContextRequired
        }
        plan = try await owner.createFreshOwner(context: freshOwnerContext)
      case .waitForRadioOn,
           .awaitOldOperationTerminals,
           .attemptSameOwnerRecovery,
           .recreateCentral,
           .attemptFreshOwnerRecovery:
        plan = try await owner.drivePlatformActionsUntilBoundary()
      }
    }
    throw Failure.transitionLimitExceeded(transitionLimit)
  }
}
