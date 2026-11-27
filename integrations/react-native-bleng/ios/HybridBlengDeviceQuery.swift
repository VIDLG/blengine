import BleHubUniFfi
import NitroModules

/// One read-only inventory snapshot; no native handle or address is retained here.
final class HybridBlengDeviceQuery: HybridBlengDeviceQuerySpec {
  private let query: SupervisedDeviceQuery

  init(query: SupervisedDeviceQuery) {
    self.query = query
    super.init()
  }

  func wait(timeoutMs: Double) throws -> Promise<BlengDeviceQueryResult> {
    let query = self.query
    return blengPromise { BlengAdapterMapping.deviceList(await query.waitResult(timeoutMs: timeoutMs)) }
  }

  func cancel() throws -> Promise<BlengReceipt> {
    let query = self.query
    return blengPromise { BlengFailureMapping.command(await query.cancelResult()) }
  }
}
