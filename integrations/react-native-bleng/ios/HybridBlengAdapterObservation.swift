import BleHubUniFfi
import NitroModules

/// Owns only the Rust adapter-observation proxy, never the supervisor or the Bluetooth radio.
final class HybridBlengAdapterObservation: HybridBlengAdapterObservationSpec {
  private let observation: SupervisedAdapterObservation

  init(observation: SupervisedAdapterObservation) {
    self.observation = observation
    super.init()
  }

  deinit {
    observation.detach()
  }

  func dispose() {
    observation.detach()
  }

  func nextEvent(timeoutMs: Double) throws -> Promise<BlengAdapterNext> {
    let observation = self.observation
    return blengPromise {
      BlengAdapterMapping.observation(await observation.nextSnapshot(timeoutMs: timeoutMs))
    }
  }
}
