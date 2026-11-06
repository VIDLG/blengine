import BleHubUniFfi
import XCTest

final class RustSupervisorRecoveryRunnerTests: XCTestCase {
  func testPlatformActionIsDrivenUntilTerminalPlan() async throws {
    let owner = FakeRustRecoveryOwner(
      initial: plan(actions: [.waitForRadioOn]),
      driven: [plan()]
    )
    let runner = RustSupervisorRecoveryRunner(owner: owner)

    let result = try await runner.run(signal: .radioLost)

    XCTAssertTrue(result.actions.isEmpty)
    XCTAssertEqual(owner.driveCount, 1)
    XCTAssertEqual(owner.retireCount, 0)
  }

  func testRetirementBoundaryUsesDedicatedRustCall() async throws {
    let owner = FakeRustRecoveryOwner(
      initial: plan(actions: [.retireOldOwner]),
      retired: [plan()]
    )
    let runner = RustSupervisorRecoveryRunner(owner: owner)

    _ = try await runner.run(signal: .radioLost)

    XCTAssertEqual(owner.driveCount, 0)
    XCTAssertEqual(owner.retireCount, 1)
  }

  func testFreshOwnerBoundaryRequiresAndPassesProviderContext() async throws {
    let context = context(owner: "fresh-owner")
    let owner = FakeRustRecoveryOwner(
      initial: plan(actions: [.createFreshOwner]),
      fresh: [plan()]
    )
    let runner = RustSupervisorRecoveryRunner(owner: owner)

    _ = try await runner.run(signal: .radioLost, freshOwnerContext: context)

    XCTAssertEqual(owner.createdContexts, [context])
  }

  func testRunnerStopsAfterItsTransitionLimit() async {
    let repeated = plan(actions: [.waitForRadioOn])
    let owner = FakeRustRecoveryOwner(initial: repeated, driven: [repeated, repeated])
    let runner = RustSupervisorRecoveryRunner(owner: owner, transitionLimit: 2)

    do {
      _ = try await runner.run(signal: .radioLost)
      XCTFail("runner should reject an unbounded platform transition")
    } catch RustSupervisorRecoveryRunner.Failure.transitionLimitExceeded(let limit) {
      XCTAssertEqual(limit, 2)
    } catch {
      XCTFail("unexpected error: \(error)")
    }
  }

  private func context(owner: String = "owner") -> ForeignOwnerContext {
    ForeignOwnerContext(
      ownerId: owner,
      backendId: "backend-\(owner)",
      ownerGeneration: 1,
      recoveryAttempt: 1
    )
  }

  private func plan(
    actions: [ForeignRecoveryAction] = []
  ) -> ForeignRecoveryPlan {
    ForeignRecoveryPlan(
      executionContext: context(),
      sequence: 1,
      actions: actions,
      outcome: nil
    )
  }
}

private final class FakeRustRecoveryOwner: RustRecoveryOwner {
  let initial: ForeignRecoveryPlan
  var driven: [ForeignRecoveryPlan]
  var retired: [ForeignRecoveryPlan]
  var fresh: [ForeignRecoveryPlan]
  private(set) var driveCount = 0
  private(set) var retireCount = 0
  private(set) var createdContexts: [ForeignOwnerContext] = []

  init(
    initial: ForeignRecoveryPlan,
    driven: [ForeignRecoveryPlan] = [],
    retired: [ForeignRecoveryPlan] = [],
    fresh: [ForeignRecoveryPlan] = []
  ) {
    self.initial = initial
    self.driven = driven
    self.retired = retired
    self.fresh = fresh
  }

  func configureOldOperationBarrier(
    oldLink: ForeignRecoveryLink?,
    expectedOldOperationIds: [String]
  ) throws {}

  func observeExternal(signal: ForeignExternalRecoverySignal) throws -> ForeignRecoveryPlan {
    initial
  }

  func drivePlatformActionsUntilBoundary() async throws -> ForeignRecoveryPlan {
    driveCount += 1
    return driven.isEmpty ? initial : driven.removeFirst()
  }

  func retireForRecovery() async throws -> ForeignRecoveryPlan {
    retireCount += 1
    return retired.removeFirst()
  }

  func createFreshOwner(context: ForeignOwnerContext) async throws -> ForeignRecoveryPlan {
    createdContexts.append(context)
    return fresh.removeFirst()
  }
}
