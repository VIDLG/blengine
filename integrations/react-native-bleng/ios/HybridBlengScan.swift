import BleHubUniFfi
import NitroModules

/// Native-created opaque capability. No JS identity or owner/context accessor is exported.
final class HybridBlengPeripheral: HybridBlengPeripheralSpec {
  private let peripheral: SupervisedPeripheral

  init(peripheral: SupervisedPeripheral) {
    self.peripheral = peripheral
    super.init()
  }
}

/// Owns only the Rust proxy, never the root HybridObject, Supervisor or Bleng owner.
final class HybridBlengScan: HybridBlengScanSpec {
  private let scan: SupervisedScan

  init(scan: SupervisedScan) {
    self.scan = scan
    super.init()
  }

  deinit {
    scan.detach()
  }

  func dispose() {
    scan.detach()
  }

  func waitUntilStarted(timeoutMs: Double) throws -> Promise<BlengScanCommandResult> {
    let scan = self.scan
    return scanPromise { BlengScanMapping.command(await scan.startResult(timeoutMs: timeoutMs)) }
  }

  func cancelStart() throws -> Promise<BlengScanCommandResult> {
    let scan = self.scan
    return scanPromise { BlengScanMapping.command(await scan.cancelStartResult()) }
  }

  func nextEvent(timeoutMs: Double) throws -> Promise<BlengScanNext> {
    let scan = self.scan
    return scanPromise { BlengScanMapping.observation(await scan.nextObservation(timeoutMs: timeoutMs)) }
  }

  func beginStop() throws -> BlengScanStopSubmission {
    BlengScanMapping.stopSubmission(scan.beginStopView())
  }
}

final class HybridBlengScanStop: HybridBlengScanStopSpec {
  private let operation: SupervisedScanStop

  init(operation: SupervisedScanStop) {
    self.operation = operation
    super.init()
  }

  func wait(timeoutMs: Double) throws -> Promise<BlengScanCommandResult> {
    let operation = self.operation
    return scanPromise { BlengScanMapping.command(await operation.waitResult(timeoutMs: timeoutMs)) }
  }

  func cancel() throws -> Promise<BlengScanCommandResult> {
    let operation = self.operation
    return scanPromise { BlengScanMapping.command(await operation.cancelResult()) }
  }
}

private func scanPromise<Value>(_ action: @escaping () async -> Value) -> Promise<Value> {
  let promise = Promise<Value>()
  Task {
    promise.resolve(withResult: await action())
  }
  return promise
}
