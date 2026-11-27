import BleHubUniFfi
import XCTest

final class RecoveryEventProjectionTests: XCTestCase {
  func testCommandCorrelatedAdapterSnapshotDoesNotBecomeRadioEvidence() {
    let event = BleEvent.adapterSnapshotChanged(
      commandId: CommandId(value: 1),
      snapshot: snapshot(state: .poweredOn)
    )

    XCTAssertNil(projectRecoveryEvent(event))
  }

  func testUncorrelatedPoweredOnSnapshotBecomesRadioEvidence() {
    let event = BleEvent.adapterSnapshotChanged(
      commandId: nil,
      snapshot: snapshot(state: .poweredOn)
    )

    XCTAssertEqual(
      projectRecoveryEvent(event),
      RecoveryEventProjection.radioState(.poweredOn)
    )
  }

  func testCommandCompletionBecomesTerminalEvidence() {
    let event = BleEvent.commandCompleted(
      commandId: CommandId(value: 42),
      error: nil
    )

    XCTAssertEqual(
      projectRecoveryEvent(event),
      RecoveryEventProjection.terminal(42)
    )
  }

  private func snapshot(state: AdapterState) -> AdapterSnapshot {
    AdapterSnapshot(
      state: state,
      scanAuthorization: .authorized,
      connectionAuthorization: .authorized,
      name: nil
    )
  }
}
