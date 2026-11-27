import BleHubUniFfi
import NitroModules

/// Owns only the Rust presence-observation proxy, never the supervisor or the Bluetooth radio.
final class HybridBlengCompanionPresence: HybridBlengCompanionPresenceSpec {
  private let observation: SupervisedCompanionPresenceObservation

  init(observation: SupervisedCompanionPresenceObservation) {
    self.observation = observation
    super.init()
  }

  deinit {
    observation.detach()
  }

  func dispose() {
    observation.detach()
  }

  func nextEvent(timeoutMs: Double) throws -> Promise<BlengCompanionPresenceNext> {
    let observation = self.observation
    return blengPromise {
      BlengAdapterMapping.companionPresenceNext(
        await observation.nextEvent(timeoutMs: timeoutMs))
    }
  }
}
