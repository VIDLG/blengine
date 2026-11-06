import BleHubUniFfi
import Foundation

/// HIL-only adapter around the production Rust owner.
///
/// These methods are intentionally outside `AppleRustSupervisorOwner`: the formal Nitro owner only
/// owns lifecycle, while this adapter exposes owner-bound evidence and recovery orchestration to the
/// host-side HIL runner.
final class AppleRustSupervisorHILAdapter: RustRecoveryOwner {
  private let owner: AppleRustSupervisorOwner

  init(owner: AppleRustSupervisorOwner) {
    self.owner = owner
  }

  func activeOwnerStartForHost() throws -> ForeignOwnerStart {
    guard let start = try owner.supervisor.activeOwnerStart() else {
      throw SupervisorFfiError.NoActiveContext
    }
    return start
  }

  func activeContextForHost() throws -> ForeignOwnerContext {
    guard let context = try owner.supervisor.activeContext() else {
      throw SupervisorFfiError.NoActiveContext
    }
    return context
  }

  func activeOwnerForHost() throws -> Bleng {
    guard let session = owner.factory.currentSessionForHost() else {
      throw SupervisorFfiError.Platform(detail: "Apple platform session is not active")
    }
    return try session.activeOwnerForHost()
  }

  func lastRetiredResourcesForHost() throws -> [ForeignResourceCount] {
    try owner.supervisor.lastRetiredResources() ?? []
  }

  func configureOldOperationBarrier(
    oldLink: ForeignRecoveryLink?,
    expectedOldOperationIds: [String]
  ) throws {
    try owner.supervisor.configureOldOperationBarrier(
      oldLink: oldLink,
      expectedOldOperationIds: expectedOldOperationIds
    )
  }

  func observeExternal(signal: ForeignExternalRecoverySignal) throws -> ForeignRecoveryPlan {
    try owner.supervisor.observeExternalSignal(signal: signal)
  }

  func observeInternalForTest(signal: ForeignRecoverySignal) throws -> ForeignRecoveryPlan {
    try owner.supervisor.observeSignal(signal: signal)
  }

  func drivePlatformActionsUntilBoundary() async throws -> ForeignRecoveryPlan {
    try await owner.supervisor.drivePlatformActionsUntilBoundary()
  }

  func retireForRecovery() async throws -> ForeignRecoveryPlan {
    try await owner.supervisor.retireForRecovery()
  }

  func createFreshOwner(context: ForeignOwnerContext) async throws -> ForeignRecoveryPlan {
    try await owner.supervisor.createFreshOwner(context: context)
  }

  static func recoveryTarget(
    serviceUuid: String,
    characteristicUuid: String,
    expectedValue: Data,
    scanNamePrefix: String? = nil
  ) -> ForeignRecoveryTarget {
    let service = BluetoothUuid(value: serviceUuid)
    return ForeignRecoveryTarget(
      scanOptions: ScanOptions(
        serviceUuids: [service],
        namePrefix: scanNamePrefix,
        companyId: nil,
        allowDuplicates: false
      ),
      serviceUuid: service,
      characteristicUuid: BluetoothUuid(value: characteristicUuid),
      notificationMode: .platformDefault,
      expectedValue: expectedValue
    )
  }
}
