import BleHubUniFfi
import NitroModules

/// One bounded GATT-cache refresh receipt; no native handle is retained here.
final class HybridBlengGattCacheRefresh: HybridBlengGattCacheRefreshSpec {
  private let refresh: SupervisedRefreshGattCache

  init(refresh: SupervisedRefreshGattCache) {
    self.refresh = refresh
    super.init()
  }

  func wait(timeoutMs: Double) throws -> Promise<BlengReceipt> {
    let refresh = self.refresh
    return blengPromise {
      BlengFailureMapping.command(await refresh.waitResult(timeoutMs: timeoutMs))
    }
  }

  func cancel() throws -> Promise<BlengReceipt> {
    let refresh = self.refresh
    return blengPromise { BlengFailureMapping.command(await refresh.cancelResult()) }
  }
}
