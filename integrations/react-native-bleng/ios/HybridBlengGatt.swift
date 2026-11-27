import BleHubUniFfi
import Foundation
import NitroModules

/// Mechanical projection of Rust's GATT DTOs. No BLE outcome is inferred here.
enum BlengGattMapping {
  static func bridgeFailure() -> BlengFailureInfo {
    BlengFailureMapping.bridgeFailure(domain: .delivery, message: "Native GATT bridge failed.")
  }

  static func properties(_ value: CharacteristicProperties) -> BlengCharacteristicProperties {
    BlengCharacteristicProperties(
      readable: value.readable,
      writableWithResponse: value.writableWithResponse,
      writableWithoutResponse: value.writableWithoutResponse,
      notifiable: value.notifiable,
      indicatable: value.indicatable
    )
  }

  /// Absence of a platform report is its own state, not "secondary".
  static func kind(_ primary: Bool?) -> BlengServiceKind {
    guard let primary else { return .unknown }
    return primary ? .primary : .secondary
  }

  static func writeType(_ value: BlengWriteType) -> WriteType {
    switch value {
    case .withresponse: return .withResponse
    case .withoutresponse: return .withoutResponse
    }
  }

  static func discoverySubmission(
    _ value: ForeignGattDiscoveryBeginResult,
    engineAdmission: AppleEngineAdmission? = nil
  ) -> BlengGattDiscoverySubmission {
    switch (value.discovery, value.failure) {
    case let (.some(discovery), nil):
      return .first(BlengGattDiscoveryAccepted(discovery: HybridBlengGattDiscovery(discovery: discovery)))
    case let (nil, .some(error)):
      return .second(BlengFailure(failure: BlengFailureMapping.failure(error)))
    default:
      return .second(BlengFailure(failure: bridgeFailure()))
    }
  }

  static func discoveryResult(
    _ value: ForeignGattDiscoveryResult,
    engineAdmission: AppleEngineAdmission? = nil
  ) -> BlengGattDiscoveryResult {
    switch (value.session, value.failure) {
    case let (.some(session), nil):
      return .first(
        BlengGattSessionOutcome(
          session: HybridBlengGattSession(session: session, engineAdmission: engineAdmission)
        )
      )
    case let (nil, .some(error)):
      return .second(BlengFailure(failure: BlengFailureMapping.failure(error)))
    default:
      return .second(BlengFailure(failure: bridgeFailure()))
    }
  }

  static func readSubmission(
    _ value: ForeignReadBeginResult,
    engineAdmission: AppleEngineAdmission? = nil
  ) -> BlengReadSubmission {
    switch (value.read, value.failure) {
    case let (.some(read), nil):
      return .first(BlengReadAccepted(read: HybridBlengRead(read: read)))
    case let (nil, .some(error)):
      return .second(BlengFailure(failure: BlengFailureMapping.failure(error)))
    default:
      return .second(BlengFailure(failure: bridgeFailure()))
    }
  }

  static func readResult(_ value: ForeignReadValueResult) -> BlengReadResult {
    switch (value.value, value.failure) {
    case let (.some(bytes), nil):
      // A zero-length value is a real read; only the absent case is a failure.
      return .first(BlengReadOutcome(value: arrayBufferCopy(bytes)))
    case let (nil, .some(error)):
      return .second(BlengFailure(failure: BlengFailureMapping.failure(error)))
    default:
      return .second(BlengFailure(failure: bridgeFailure()))
    }
  }

  static func writeSubmission(
    _ value: ForeignWriteBeginResult,
    engineAdmission: AppleEngineAdmission? = nil
  ) -> BlengWriteSubmission {
    switch (value.write, value.failure) {
    case let (.some(write), nil):
      return .first(BlengWriteAccepted(write: HybridBlengWrite(write: write)))
    case let (nil, .some(error)):
      return .second(BlengFailure(failure: BlengFailureMapping.failure(error)))
    default:
      return .second(BlengFailure(failure: bridgeFailure()))
    }
  }

  static func rssiSubmission(
    _ value: ForeignRssiBeginResult,
    engineAdmission: AppleEngineAdmission? = nil
  ) -> BlengRssiSubmission {
    switch (value.rssi, value.failure) {
    case let (.some(rssi), nil):
      return .first(BlengRssiAccepted(rssi: HybridBlengRssi(rssi: rssi)))
    case let (nil, .some(error)):
      return .second(BlengFailure(failure: BlengFailureMapping.failure(error)))
    default:
      return .second(BlengFailure(failure: bridgeFailure()))
    }
  }

  static func invalidMtu(_ message: String) -> BlengFailureInfo {
    BlengFailureInfo(
      domain: .input,
      code: "bleng.invalid_request.mtu",
      message: message,
      platform: nil
    )
  }

  static func descriptorReadSubmission(
    _ value: ForeignDescriptorReadBeginResult
  ) -> BlengDescriptorReadSubmission {
    switch (value.read, value.failure) {
    case let (.some(read), nil):
      return .first(BlengDescriptorReadAccepted(read: HybridBlengDescriptorRead(read: read)))
    case let (nil, .some(error)):
      return .second(BlengFailure(failure: BlengFailureMapping.failure(error)))
    default:
      return .second(BlengFailure(failure: bridgeFailure()))
    }
  }

  static func descriptorReadResult(_ value: ForeignDescriptorReadValueResult) -> BlengDescriptorReadResult {
    switch (value.value, value.failure) {
    case let (.some(bytes), nil):
      return .first(BlengDescriptorReadOutcome(value: arrayBufferCopy(bytes)))
    case let (nil, .some(error)):
      return .second(BlengFailure(failure: BlengFailureMapping.failure(error)))
    default:
      return .second(BlengFailure(failure: bridgeFailure()))
    }
  }

  static func descriptorWriteSubmission(
    _ value: ForeignDescriptorWriteBeginResult
  ) -> BlengDescriptorWriteSubmission {
    switch (value.write, value.failure) {
    case let (.some(write), nil):
      return .first(BlengDescriptorWriteAccepted(write: HybridBlengDescriptorWrite(write: write)))
    case let (nil, .some(error)):
      return .second(BlengFailure(failure: BlengFailureMapping.failure(error)))
    default:
      return .second(BlengFailure(failure: bridgeFailure()))
    }
  }

  static func mtuSubmission(
    _ value: ForeignMtuBeginResult,
    engineAdmission: AppleEngineAdmission? = nil
  ) -> BlengMtuSubmission {
    switch (value.mtu, value.failure) {
    case let (.some(mtu), nil):
      return .first(BlengMtuAccepted(mtu: HybridBlengMtu(mtu: mtu)))
    case let (nil, .some(error)):
      return .second(BlengFailure(failure: BlengFailureMapping.failure(error)))
    default:
      return .second(BlengFailure(failure: bridgeFailure()))
    }
  }

  static func mtuInfo(_ value: MtuInfo) -> BlengMtuInfo {
    BlengMtuInfo(
      mtu: value.mtu.map { Double($0) },
      maximumWriteWithResponse: value.maximumWriteWithResponse.map { Double($0) },
      maximumWriteWithoutResponse: value.maximumWriteWithoutResponse.map { Double($0) }
    )
  }

  static func mtuResult(_ value: ForeignMtuValueResult) -> BlengMtuResult {
    switch (value.info, value.failure) {
    case let (.some(info), nil):
      return .first(BlengMtuOutcome(info: mtuInfo(info)))
    case let (nil, .some(error)):
      return .second(BlengFailure(failure: BlengFailureMapping.failure(error)))
    default:
      return .second(BlengFailure(failure: bridgeFailure()))
    }
  }

  static func rssiResult(_ value: ForeignRssiValueResult) -> BlengRssiResult {
    switch (value.rssi, value.failure) {
    case let (.some(rssi), nil):
      // Zero and negative readings are real values; only absence is a failure.
      return .first(BlengRssiOutcome(rssi: Double(rssi)))
    case let (nil, .some(error)):
      return .second(BlengFailure(failure: BlengFailureMapping.failure(error)))
    default:
      return .second(BlengFailure(failure: bridgeFailure()))
    }
  }

  // Same owned-copy mechanism as the scan projection, including empty Data.
  private static func arrayBufferCopy(_ data: Data) -> ArrayBuffer {
    let buffer = ArrayBuffer.allocate(size: data.count)
    data.withUnsafeBytes { rawBuffer in
      guard data.count > 0, let baseAddress = rawBuffer.baseAddress else { return }
      memcpy(buffer.data, baseAddress, data.count)
    }
    return buffer
  }
}

/// Holds only the original Rust discovery proxy.
final class HybridBlengGattDiscovery: HybridBlengGattDiscoverySpec {
  private let discovery: SupervisedGattDiscovery

  init(discovery: SupervisedGattDiscovery) {
    self.discovery = discovery
    super.init()
  }

  func wait(timeoutMs: Double) throws -> Promise<BlengGattDiscoveryResult> {
    let discovery = self.discovery
    return blengPromise {
      BlengGattMapping.discoveryResult(await discovery.waitResult(timeoutMs: timeoutMs))
    }
  }

  func cancel() throws -> Promise<BlengReceipt> {
    let discovery = self.discovery
    return blengPromise { BlengFailureMapping.command(await discovery.cancelResult()) }
  }
}

/// One discovered topology. Services are created per call from the Rust snapshot.
final class HybridBlengGattSession: HybridBlengGattSessionSpec {
  private let session: SupervisedGattSession
  private let engineAdmission: AppleEngineAdmission?

  init(session: SupervisedGattSession, engineAdmission: AppleEngineAdmission? = nil) {
    self.session = session
    self.engineAdmission = engineAdmission
    super.init()
  }

  func services() throws -> [(any HybridBlengGattServiceSpec)] {
    session.services().map {
      HybridBlengGattService(service: $0, engineAdmission: engineAdmission)
    }
  }
}

final class HybridBlengGattService: HybridBlengGattServiceSpec {
  private let service: SupervisedGattService
  private let engineAdmission: AppleEngineAdmission?

  init(service: SupervisedGattService, engineAdmission: AppleEngineAdmission? = nil) {
    self.service = service
    self.engineAdmission = engineAdmission
    super.init()
  }

  func uuid() throws -> String {
    service.uuid().value
  }

  func kind() throws -> BlengServiceKind {
    BlengGattMapping.kind(service.primary())
  }

  func characteristics() throws -> [(any HybridBlengGattCharacteristicSpec)] {
    service.characteristics().map {
      HybridBlengGattCharacteristic(characteristic: $0, engineAdmission: engineAdmission)
    }
  }
}

final class HybridBlengGattCharacteristic: HybridBlengGattCharacteristicSpec {
  private let characteristic: SupervisedGattCharacteristic
  private let engineAdmission: AppleEngineAdmission?

  init(characteristic: SupervisedGattCharacteristic, engineAdmission: AppleEngineAdmission? = nil) {
    self.characteristic = characteristic
    self.engineAdmission = engineAdmission
    super.init()
  }

  func uuid() throws -> String {
    characteristic.uuid().value
  }

  func properties() throws -> BlengCharacteristicProperties {
    BlengGattMapping.properties(characteristic.properties())
  }

  /// Descriptors are capabilities: each one keeps its native handle inside the bridge.
  func descriptors() throws -> [(any HybridBlengGattDescriptorSpec)] {
    characteristic.descriptors().map {
      HybridBlengGattDescriptor(descriptor: $0, engineAdmission: engineAdmission)
    }
  }

  func beginRead() throws -> BlengReadSubmission {
    guard let engineAdmission else {
      return BlengGattMapping.readSubmission(characteristic.beginRead())
    }
    return engineAdmission.submit {
      BlengGattMapping.readSubmission(characteristic.beginRead())
    } ?? .second(BlengFailure(failure: BlengGattMapping.bridgeFailure()))
  }

  /// Copies the payload here: this Nitro method runs synchronously on the JS thread, where a
  /// non-owning `ArrayBuffer` is still readable. Length validation belongs to Rust.
  func beginWrite(value: ArrayBuffer, writeType: BlengWriteType) throws -> BlengWriteSubmission {
    let bytes = Data(bytes: value.data, count: Int(value.size))
    guard let engineAdmission else {
      return BlengGattMapping.writeSubmission(
        characteristic.beginWrite(value: bytes, writeType: BlengGattMapping.writeType(writeType))
      )
    }
    return engineAdmission.submit {
      BlengGattMapping.writeSubmission(
        characteristic.beginWrite(value: bytes, writeType: BlengGattMapping.writeType(writeType))
      )
    } ?? .second(BlengFailure(failure: BlengGattMapping.bridgeFailure()))
  }

  func beginSubscription(
    mode: BlengNotificationModeRequest
  ) throws -> BlengSubscriptionSubmission {
    let request = BlengNotificationMapping.request(mode)
    guard let engineAdmission else {
      return BlengNotificationMapping.submission(
        characteristic.beginEnableNotifications(mode: request)
      )
    }
    return engineAdmission.submit {
      BlengNotificationMapping.submission(
        characteristic.beginEnableNotifications(mode: request)
      )
    } ?? .second(BlengFailure(failure: BlengNotificationMapping.bridgeFailure()))
  }
}

final class HybridBlengRead: HybridBlengReadSpec {
  private let read: SupervisedRead

  init(read: SupervisedRead) {
    self.read = read
    super.init()
  }

  func wait(timeoutMs: Double) throws -> Promise<BlengReadResult> {
    let read = self.read
    return blengPromise { BlengGattMapping.readResult(await read.waitResult(timeoutMs: timeoutMs)) }
  }

  func cancel() throws -> Promise<BlengReceipt> {
    let read = self.read
    return blengPromise { BlengFailureMapping.command(await read.cancelResult()) }
  }
}

final class HybridBlengWrite: HybridBlengWriteSpec {
  private let write: SupervisedWrite

  init(write: SupervisedWrite) {
    self.write = write
    super.init()
  }

  func wait(timeoutMs: Double) throws -> Promise<BlengReceipt> {
    let write = self.write
    return blengPromise { BlengFailureMapping.command(await write.waitResult(timeoutMs: timeoutMs)) }
  }

  func cancel() throws -> Promise<BlengReceipt> {
    let write = self.write
    return blengPromise { BlengFailureMapping.command(await write.cancelResult()) }
  }
}

final class HybridBlengRssi: HybridBlengRssiSpec {
  private let rssi: SupervisedRssi

  init(rssi: SupervisedRssi) {
    self.rssi = rssi
    super.init()
  }

  func wait(timeoutMs: Double) throws -> Promise<BlengRssiResult> {
    let rssi = self.rssi
    return blengPromise { BlengGattMapping.rssiResult(await rssi.waitResult(timeoutMs: timeoutMs)) }
  }

  func cancel() throws -> Promise<BlengReceipt> {
    let rssi = self.rssi
    return blengPromise { BlengFailureMapping.command(await rssi.cancelResult()) }
  }
}

final class HybridBlengGattDescriptor: HybridBlengGattDescriptorSpec {
  private let descriptor: SupervisedGattDescriptor
  private let engineAdmission: AppleEngineAdmission?

  init(descriptor: SupervisedGattDescriptor, engineAdmission: AppleEngineAdmission? = nil) {
    self.descriptor = descriptor
    self.engineAdmission = engineAdmission
    super.init()
  }

  func uuid() throws -> String {
    descriptor.uuid().value
  }

  func beginRead() throws -> BlengDescriptorReadSubmission {
    guard let engineAdmission else {
      return BlengGattMapping.descriptorReadSubmission(descriptor.beginRead())
    }
    return engineAdmission.submit {
      BlengGattMapping.descriptorReadSubmission(descriptor.beginRead())
    } ?? .second(BlengFailure(failure: BlengGattMapping.bridgeFailure()))
  }

  func beginWrite(value: ArrayBuffer) throws -> BlengDescriptorWriteSubmission {
    // Copy synchronously on the JS thread where the non-owning buffer is still readable.
    let bytes = Data(bytes: value.data, count: Int(value.size))
    guard let engineAdmission else {
      return BlengGattMapping.descriptorWriteSubmission(descriptor.beginWrite(value: bytes))
    }
    return engineAdmission.submit {
      BlengGattMapping.descriptorWriteSubmission(descriptor.beginWrite(value: bytes))
    } ?? .second(BlengFailure(failure: BlengGattMapping.bridgeFailure()))
  }
}

final class HybridBlengDescriptorRead: HybridBlengDescriptorReadSpec {
  private let read: SupervisedDescriptorRead

  init(read: SupervisedDescriptorRead) {
    self.read = read
    super.init()
  }

  func wait(timeoutMs: Double) throws -> Promise<BlengDescriptorReadResult> {
    let read = self.read
    return blengPromise {
      BlengGattMapping.descriptorReadResult(await read.waitResult(timeoutMs: timeoutMs))
    }
  }

  func cancel() throws -> Promise<BlengReceipt> {
    let read = self.read
    return blengPromise { BlengFailureMapping.command(await read.cancelResult()) }
  }
}

final class HybridBlengDescriptorWrite: HybridBlengDescriptorWriteSpec {
  private let write: SupervisedDescriptorWrite

  init(write: SupervisedDescriptorWrite) {
    self.write = write
    super.init()
  }

  func wait(timeoutMs: Double) throws -> Promise<BlengReceipt> {
    let write = self.write
    return blengPromise {
      BlengFailureMapping.command(await write.waitResult(timeoutMs: timeoutMs))
    }
  }

  func cancel() throws -> Promise<BlengReceipt> {
    let write = self.write
    return blengPromise { BlengFailureMapping.command(await write.cancelResult()) }
  }
}

final class HybridBlengMtu: HybridBlengMtuSpec {
  private let mtu: SupervisedMtu

  init(mtu: SupervisedMtu) {
    self.mtu = mtu
    super.init()
  }

  func wait(timeoutMs: Double) throws -> Promise<BlengMtuResult> {
    let mtu = self.mtu
    return blengPromise { BlengGattMapping.mtuResult(await mtu.waitResult(timeoutMs: timeoutMs)) }
  }

  func cancel() throws -> Promise<BlengReceipt> {
    let mtu = self.mtu
    return blengPromise { BlengFailureMapping.command(await mtu.cancelResult()) }
  }
}
