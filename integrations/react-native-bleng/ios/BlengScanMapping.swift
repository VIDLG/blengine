import BleHubUniFfi
import Foundation
import NitroModules

/// Mechanical projection of Rust's scan DTOs. No BLE outcome is inferred at this boundary.
enum BlengScanMapping {
  static func submissionUnavailable() -> BlengFailureInfo {
    BlengFailureInfo(
      domain: .lifecycle,
      code: "submissionUnavailable",
      message: "No started session is available for scan submission.",
      platform: nil
    )
  }

  static func bridgeFailure() -> BlengFailureInfo {
    BlengFailureMapping.bridgeFailure(domain: .delivery, message: "Native scan bridge failed.")
  }

  static func failure(_ value: ForeignOperationFailure) -> BlengFailureInfo {
    BlengFailureMapping.failure(value)
  }

  static func submission(
    _ value: ForeignScanBeginResult,
    engineAdmission: AppleEngineAdmission? = nil
  ) -> BlengScanSubmission {
    switch (value.scan, value.failure) {
    case let (.some(scan), nil):
      return .first(BlengScanAccepted(scan: HybridBlengScan(scan: scan, engineAdmission: engineAdmission)))
    case let (nil, .some(error)):
      return .second(BlengFailure(failure: failure(error)))
    default:
      return .second(BlengFailure(failure: bridgeFailure()))
    }
  }

  static func stopSubmission(_ value: ForeignScanStopResult) -> BlengScanStopSubmission {
    switch (value.operation, value.failure) {
    case let (.some(operation), nil):
      return .first(BlengScanStopAccepted(stop: HybridBlengScanStop(operation: operation)))
    case let (nil, .some(error)):
      return .second(BlengFailure(failure: failure(error)))
    default:
      return .second(BlengFailure(failure: bridgeFailure()))
    }
  }

  static func observation(
    _ value: ForeignScanObservation,
    engineAdmission: AppleEngineAdmission? = nil
  ) -> BlengScanNext {
    switch (value.advertisement, value.ended, value.failure) {
    case let (.some(value), nil, nil):
      return .first(
        BlengScanAdvertisementEvent(advertisement: advertisement(value, engineAdmission: engineAdmission))
      )
    case let (nil, .some(value), nil):
      let reason: BlengEndReason
      switch value.reason {
      case .requested: reason = .requested
      case .systemSuspended: reason = .systemsuspended
      case .failed: reason = .failed
      case .ownerClosed: reason = .ownerclosed
      case .overflow: reason = .overflowed
      }
      return .second(BlengScanEndedEvent(ended: BlengEnd(
        reason: reason, failure: value.failure.map(failure)
      )))
    case let (nil, nil, .some(error)):
      return .third(BlengFailure(failure: failure(error)))
    default:
      return .third(BlengFailure(failure: bridgeFailure()))
    }
  }

  private static func advertisement(
    _ value: ForeignScanAdvertisement,
    engineAdmission: AppleEngineAdmission?
  ) -> BlengScanAdvertisement {
    BlengScanAdvertisement(
      peripheral: HybridBlengPeripheral(
        peripheral: value.peripheral, engineAdmission: engineAdmission
      ),
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
