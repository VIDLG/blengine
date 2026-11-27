import BleHubUniFfi
import Foundation
import NitroModules

/// Mechanical projection of Rust's notification DTOs. No BLE outcome is inferred here.
enum BlengNotificationMapping {
  static func bridgeFailure() -> BlengFailureInfo {
    BlengFailureMapping.bridgeFailure(
      domain: .delivery, message: "Native notification bridge failed."
    )
  }

  static func request(_ value: BlengNotificationModeRequest) -> NotificationModeRequest {
    switch value {
    case .platformdefault: return .platformDefault
    case .requirenotification: return .requireNotification
    case .requireindication: return .requireIndication
    }
  }

  /// An unreported mode stays absent; it is never guessed from the requested mode.
  static func mode(_ value: NotificationMode?) -> BlengNotificationMode? {
    guard let value else { return nil }
    switch value {
    case .notification: return .notification
    case .indication: return .indication
    }
  }

  static func submission(
    _ value: ForeignNotificationSubscribeBeginResult,
    engineAdmission: AppleEngineAdmission? = nil
  ) -> BlengSubscriptionSubmission {
    switch (value.notifications, value.failure) {
    case let (.some(notifications), nil):
      return .first(
        BlengSubscriptionAccepted(
          notifications: HybridBlengSubscription(notifications: notifications)
        )
      )
    case let (nil, .some(error)):
      return .second(BlengFailure(failure: BlengFailureMapping.failure(error)))
    default:
      return .second(BlengFailure(failure: bridgeFailure()))
    }
  }

  static func subscribeResult(
    _ value: ForeignNotificationSubscribeResult
  ) -> BlengSubscriptionResult {
    guard let error = value.failure else {
      return .first(BlengSubscriptionOutcome(mode: mode(value.actualMode)))
    }
    return .second(BlengFailure(failure: BlengFailureMapping.failure(error)))
  }

  static func observation(_ value: ForeignNotificationObservation) -> BlengSubscriptionNext {
    switch (value.value, value.ended, value.failure) {
    case let (.some(bytes), nil, nil):
      // An empty value is a real notification; only its absence is a failure.
      return .first(BlengSubscriptionValueEvent(value: arrayBufferCopy(bytes)))
    case let (nil, .some(ended), nil):
      let reason: BlengEndReason
      switch ended.reason {
      case .disabled: reason = .requested
      case .connectionClosed: reason = .connectionclosed
      case .ownerClosed: reason = .ownerclosed
      case .overflow: reason = .overflowed
      }
      return .second(BlengSubscriptionEndedEvent(ended: BlengEnd(reason: reason, failure: nil)))
    case let (nil, nil, .some(error)):
      return .third(BlengFailure(failure: BlengFailureMapping.failure(error)))
    default:
      return .third(BlengFailure(failure: bridgeFailure()))
    }
  }

  static func disableSubmission(
    _ value: ForeignNotificationDisableResult,
    engineAdmission: AppleEngineAdmission? = nil
  ) -> BlengSubscriptionDisableSubmission {
    switch (value.operation, value.failure) {
    case let (.some(operation), nil):
      return .first(
        BlengSubscriptionDisableAccepted(
          disable: HybridBlengSubscriptionDisable(operation: operation)
        )
      )
    case let (nil, .some(error)):
      return .second(BlengFailure(failure: BlengFailureMapping.failure(error)))
    default:
      return .second(BlengFailure(failure: bridgeFailure()))
    }
  }

  // Same owned-copy mechanism as the scan and GATT projections, including empty Data.
  private static func arrayBufferCopy(_ data: Data) -> ArrayBuffer {
    let buffer = ArrayBuffer.allocate(size: data.count)
    data.withUnsafeBytes { rawBuffer in
      guard data.count > 0, let baseAddress = rawBuffer.baseAddress else { return }
      memcpy(buffer.data, baseAddress, data.count)
    }
    return buffer
  }
}

/// Holds only the Rust lane endpoint; never the subscription identity.
final class HybridBlengSubscription: HybridBlengSubscriptionSpec {
  private let notifications: SupervisedNotifications

  init(notifications: SupervisedNotifications) {
    self.notifications = notifications
    super.init()
  }

  func waitUntilEnabled(timeoutMs: Double) throws -> Promise<BlengSubscriptionResult> {
    let notifications = self.notifications
    return blengPromise {
      BlengNotificationMapping.subscribeResult(await notifications.waitResult(timeoutMs: timeoutMs))
    }
  }

  func nextEvent(timeoutMs: Double) throws -> Promise<BlengSubscriptionNext> {
    let notifications = self.notifications
    return blengPromise {
      BlengNotificationMapping.observation(await notifications.nextObservation(timeoutMs: timeoutMs))
    }
  }

  func beginDisable() throws -> BlengSubscriptionDisableSubmission {
    BlengNotificationMapping.disableSubmission(notifications.beginDisableView())
  }

  func detach() {
    notifications.detach()
  }
}

final class HybridBlengSubscriptionDisable: HybridBlengSubscriptionDisableSpec {
  private let operation: SupervisedNotificationDisable

  init(operation: SupervisedNotificationDisable) {
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
