import BleHubUniFfi
import Foundation
import NitroModules

/// Mechanical projection of Rust's product DTOs. No BLE outcome is inferred at this boundary.
enum BlengScanMapping {
  static func submissionUnavailable() -> BlengScanFailureInfo {
    BlengScanFailureInfo(
      domain: .lifecycle,
      code: "submissionUnavailable",
      message: "No started session is available for scan submission.",
      platform: nil
    )
  }

  static func bridgeFailure() -> BlengScanFailureInfo {
    BlengScanFailureInfo(
      domain: .delivery,
      code: "bridgeFailure",
      message: "Native scan bridge failed.",
      platform: nil
    )
  }

  static func failure(_ value: ForeignScanFailure) -> BlengScanFailureInfo {
    let domain: BlengScanFailureDomain
    switch value.domain {
    case .input: domain = .input
    case .submission: domain = .submission
    case .operation: domain = .operation
    case .observation: domain = .observation
    case .delivery: domain = .delivery
    case .lifecycle: domain = .lifecycle
    }
    return BlengScanFailureInfo(
      domain: domain,
      code: value.code,
      message: value.message,
      platform: value.platformDetails.map(platformDetails)
    )
  }

  private static func platform(_ value: Platform) -> String {
    switch value {
    case .apple: return "apple"
    case .android: return "android"
    case .windows: return "windows"
    case .linux: return "linux"
    case .test: return "test"
    }
  }

  private static func platformDetails(_ value: PlatformErrorDetails) -> BlengScanPlatformDetails {
    BlengScanPlatformDetails(
      platform: platform(value.platform), code: value.code, name: value.name, message: value.message
    )
  }

  static func command(_ value: ForeignScanCommandResult) -> BlengScanCommandResult {
    BlengScanCommandResult(failure: value.failure.map(failure))
  }

  static func submission(_ value: ForeignScanBeginResult) -> BlengScanSubmission {
    switch (value.scan, value.failure) {
    case let (.some(scan), nil):
      return .first(BlengScanAccepted(scan: HybridBlengScan(scan: scan)))
    case let (nil, .some(error)):
      return .second(BlengScanFailure(failure: failure(error)))
    default:
      return .second(BlengScanFailure(failure: bridgeFailure()))
    }
  }

  static func stopSubmission(_ value: ForeignScanStopResult) -> BlengScanStopSubmission {
    switch (value.operation, value.failure) {
    case let (.some(operation), nil):
      return .first(BlengScanStopAccepted(operation: HybridBlengScanStop(operation: operation)))
    case let (nil, .some(error)):
      return .second(BlengScanFailure(failure: failure(error)))
    default:
      return .second(BlengScanFailure(failure: bridgeFailure()))
    }
  }

  static func observation(_ value: ForeignScanObservation) -> BlengScanNext {
    switch (value.advertisement, value.ended, value.failure) {
    case let (.some(value), nil, nil):
      return .first(BlengScanAdvertisementEvent(advertisement: advertisement(value)))
    case let (nil, .some(value), nil):
      let reason: BlengScanEndReason
      switch value.reason {
      case .requested: reason = .requested
      case .systemSuspended: reason = .systemsuspended
      case .failed: reason = .failed
      case .ownerClosed: reason = .ownerclosed
      }
      return .second(BlengScanEndedEvent(ended: BlengScanEnd(
        reason: reason, failure: value.failure.map(failure)
      )))
    case let (nil, nil, .some(error)):
      return .third(BlengScanFailure(failure: failure(error)))
    default:
      return .third(BlengScanFailure(failure: bridgeFailure()))
    }
  }

  private static func advertisement(_ value: ForeignScanAdvertisement) -> BlengScanAdvertisement {
    BlengScanAdvertisement(
      peripheral: HybridBlengPeripheral(peripheral: value.peripheral),
      localName: value.localName,
      advertisedServices: value.advertisedServices,
      manufacturerData: value.manufacturerData.map {
        BlengManufacturerData(companyId: Double($0.companyId), payload: arrayBufferCopy($0.payload))
      },
      serviceData: value.serviceData.map {
        BlengServiceData(serviceUuid: $0.serviceUuid.value, payload: arrayBufferCopy($0.payload))
      },
      rssi: value.rssi.map { Double($0) },
      connectable: value.connectable
    )
  }

  // Same owned-copy mechanism as the existing Apple bridge, including empty Data.
  private static func arrayBufferCopy(_ data: Data) -> ArrayBuffer {
    let buffer = ArrayBuffer.allocate(size: data.count)
    data.withUnsafeBytes { rawBuffer in
      guard data.count > 0, let baseAddress = rawBuffer.baseAddress else { return }
      memcpy(buffer.data, baseAddress, data.count)
    }
    return buffer
  }
}
