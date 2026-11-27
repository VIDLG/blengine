import BleHubUniFfi
import NitroModules

/// Owns only the Rust proxy, never the root HybridObject, Supervisor or Bleng owner.
final class HybridBlengScan: HybridBlengScanSpec {
  private let scan: SupervisedScan
  private let engineAdmission: AppleEngineAdmission?

  init(scan: SupervisedScan, engineAdmission: AppleEngineAdmission? = nil) {
    self.scan = scan
    self.engineAdmission = engineAdmission
    super.init()
  }

  deinit {
    scan.detach()
  }

  func dispose() {
    scan.detach()
  }

  func waitUntilStarted(timeoutMs: Double) throws -> Promise<BlengReceipt> {
    let scan = self.scan
    return blengPromise { BlengFailureMapping.command(await scan.startResult(timeoutMs: timeoutMs)) }
  }

  func cancelStart() throws -> Promise<BlengReceipt> {
    let scan = self.scan
    return blengPromise { BlengFailureMapping.command(await scan.cancelStartResult()) }
  }

  func nextEvent(timeoutMs: Double) throws -> Promise<BlengScanNext> {
    let scan = self.scan
    let engineAdmission = self.engineAdmission
    return blengPromise {
      BlengScanMapping.observation(
        await scan.nextObservation(timeoutMs: timeoutMs), engineAdmission: engineAdmission
      )
    }
  }

  func beginStop() throws -> BlengScanStopSubmission {
    guard let engineAdmission else { return BlengScanMapping.stopSubmission(scan.beginStopView()) }
    return engineAdmission.submit {
      BlengScanMapping.stopSubmission(scan.beginStopView())
    } ?? .second(BlengFailure(failure: BlengScanMapping.submissionUnavailable()))
  }

  func isScanning() -> Bool {
    scan.isScanning()
  }
}

final class HybridBlengScanStop: HybridBlengScanStopSpec {
  private let operation: SupervisedScanStop

  init(operation: SupervisedScanStop) {
    self.operation = operation
    super.init()
  }

  func wait(timeoutMs: Double) throws -> Promise<BlengReceipt> {
    let operation = self.operation
    return blengPromise { BlengFailureMapping.command(await operation.waitResult(timeoutMs: timeoutMs)) }
  }

  func cancel() throws -> Promise<BlengReceipt> {
    let operation = self.operation
    return blengPromise { BlengFailureMapping.command(await operation.cancelResult()) }
  }
}
