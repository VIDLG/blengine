import BleHubUniFfi
import Foundation
import NitroModules

/// Mechanical projection of Rust's adapter DTOs. No radio outcome is inferred at this boundary:
/// what arrives is what the platform reported, and an end is an end of observation only.
enum BlengAdapterMapping {
  static func submissionUnavailable() -> BlengFailureInfo {
    BlengFailureInfo(
      domain: .lifecycle,
      code: "submissionUnavailable",
      message: "No started session is available for adapter observation.",
      platform: nil
    )
  }

  static func bridgeFailure() -> BlengFailureInfo {
    BlengFailureMapping.bridgeFailure(domain: .delivery, message: "Native adapter bridge failed.")
  }

  /// A platform fact, not a transient error: this build can never provide the capability.
  static func notSupported(_ message: String) -> BlengFailureInfo {
    BlengFailureInfo(domain: .submission, code: "not_supported", message: message, platform: nil)
  }

  static func submission(
    _ value: ForeignAdapterObservationBeginResult
  ) -> BlengAdapterObservationSubmission {
    switch (value.adapterObservation, value.failure) {
    case let (.some(observation), nil):
      return .first(
        BlengAdapterObservationAccepted(
          adapterObservation: HybridBlengAdapterObservation(observation: observation)))
    case let (nil, .some(error)):
      return .second(BlengFailure(failure: BlengFailureMapping.failure(error)))
    default:
      return .second(BlengFailure(failure: bridgeFailure()))
    }
  }

  static func controlSubmission(
    _ value: ForeignAdapterControlBeginResult
  ) -> BlengAdapterControlSubmission {
    switch (value.control, value.failure) {
    case let (.some(control), nil):
      return .first(
        BlengAdapterControlAccepted(control: HybridBlengAdapterControl(control: control)))
    case let (nil, .some(error)):
      return .second(BlengFailure(failure: BlengFailureMapping.failure(error)))
    default:
      return .second(BlengFailure(failure: bridgeFailure()))
    }
  }

  static func companionAssociationSubmission(
    _ value: ForeignCompanionAssociationBeginResult
  ) -> BlengCompanionAssociationSubmission {
    switch (value.association, value.failure) {
    case let (.some(association), nil):
      return .first(
        BlengCompanionAssociationAccepted(
          association: HybridBlengCompanionAssociation(association: association)))
    case let (nil, .some(error)):
      return .second(BlengFailure(failure: BlengFailureMapping.failure(error)))
    default:
      return .second(BlengFailure(failure: bridgeFailure()))
    }
  }

  static func companionPresenceSubmission(
    _ value: ForeignCompanionPresenceBeginResult
  ) -> BlengCompanionPresenceSubmission {
    switch (value.presenceObservation, value.failure) {
    case let (.some(observation), nil):
      return .first(
        BlengCompanionPresenceAccepted(
          companionPresence: HybridBlengCompanionPresence(observation: observation)))
    case let (nil, .some(error)):
      return .second(BlengFailure(failure: BlengFailureMapping.failure(error)))
    default:
      return .second(BlengFailure(failure: bridgeFailure()))
    }
  }

  static func companionPresenceNext(
    _ value: ForeignCompanionPresenceObservation
  ) -> BlengCompanionPresenceNext {
    switch (value.presence, value.ended, value.failure) {
    case let (.some(presence), nil, nil):
      return .first(
        BlengCompanionPresenceEvent(
          present: presence.present,
          key: presence.key,
          localName: presence.localName))
    case let (nil, .some(ended), nil):
      let reason: BlengEndReason =
        ended.reason == .ownerClosed ? .ownerclosed : .detached
      return .second(
        BlengCompanionPresenceEndedEvent(
          ended: BlengEnd(reason: reason, failure: nil)))
    case let (nil, nil, .some(error)):
      return .third(BlengFailure(failure: BlengFailureMapping.failure(error)))
    default:
      return .third(BlengFailure(failure: bridgeFailure()))
    }
  }

  static func deviceQueryKind(_ value: BlengDeviceQueryKind) -> DeviceQueryKind {
    switch value {
    case .connected: return .connected
    case .bonded: return .bonded
    case .companion: return .companion
    }
  }

  static func querySubmission(
    _ value: ForeignDeviceQueryBeginResult
  ) -> BlengDeviceQuerySubmission {
    switch (value.query, value.failure) {
    case let (.some(query), nil):
      return .first(BlengDeviceQueryAccepted(query: HybridBlengDeviceQuery(query: query)))
    case let (nil, .some(error)):
      return .second(BlengFailure(failure: BlengFailureMapping.failure(error)))
    default:
      return .second(BlengFailure(failure: bridgeFailure()))
    }
  }

  static func adoptSubmission(
    _ value: ForeignPeripheralAdoptResult
  ) -> BlengPeripheralAdoptSubmission {
    switch (value.peripheral, value.failure) {
    case let (.some(peripheral), nil):
      return .first(
        BlengPeripheralAdoptAccepted(peripheral: HybridBlengPeripheral(peripheral: peripheral)))
    case let (nil, .some(error)):
      return .second(BlengFailure(failure: BlengFailureMapping.failure(error)))
    default:
      return .second(BlengFailure(failure: bridgeFailure()))
    }
  }

  static func deviceList(_ value: ForeignDeviceListResult) -> BlengDeviceQueryResult {
    switch (value.devices, value.failure) {
    case let (.some(devices), nil):
      return .first(
        BlengDeviceQueryOutcome(
          devices: devices.map {
            BlengDeviceInfo(
              key: $0.key,
              localName: $0.localName,
              connected: $0.connected,
              bonded: $0.bonded
            )
          }))
    case let (nil, .some(error)):
      return .second(BlengFailure(failure: BlengFailureMapping.failure(error)))
    default:
      return .second(BlengFailure(failure: bridgeFailure()))
    }
  }

  static func observation(_ value: ForeignAdapterObservation) -> BlengAdapterNext {
    switch (value.snapshot, value.ended, value.failure) {
    case let (.some(snapshot), nil, nil):
      return .first(
        BlengAdapterSnapshotEvent(snapshot: BlengAdapterSnapshot(adapterSnapshot: snapshot)))
    case let (nil, .some(ended), nil):
      let reason: BlengEndReason =
        ended.reason == .ownerClosed ? .ownerclosed : .detached
      return .second(BlengAdapterEndedEvent(ended: BlengEnd(reason: reason, failure: nil)))
    case let (nil, nil, .some(error)):
      return .third(BlengFailure(failure: BlengFailureMapping.failure(error)))
    default:
      return .third(BlengFailure(failure: bridgeFailure()))
    }
  }
}
