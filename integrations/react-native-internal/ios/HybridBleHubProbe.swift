import Foundation
import NitroModules
@_spi(AppleDiagnostics) import BleHubApple
import BleHubUniFfi


// Private to this RN owner; no diagnostic ABI crosses the backend/module boundary.
private final class NitroNotificationDiagnostics {
  private let enabled = ProcessInfo.processInfo.environment["BLEHUB_APPLE_NOTIFICATION_DIAGNOSTICS"] == "1"
  private let owner = UUID().uuidString.lowercased()
  private let lock = NSLock()
  private var records = 0

  func record(_ stage: String, event: BleEvent, note: String) {
    guard enabled, case let .notificationReceived(connection, session, subscription, characteristic, value) = event else { return }
    lock.withLock {
      guard records < 128 else { return }
      records += 1
      var row: [String: Any] = [
        "owner": owner, "ownerKind": "probe", "pid": ProcessInfo.processInfo.processIdentifier,
        "sequence": records, "uptimeNs": String(DispatchTime.now().uptimeNanoseconds),
        "event": String(stage.prefix(80)), "note": String(note.prefix(160)),
        "connectionId": String(connection.value), "gattSessionId": String(session.value),
        "subscriptionId": String(subscription.value), "characteristicId": String(characteristic.value),
        "valueLength": value.count, "valueHex": value.prefix(16).map { String(format: "%02x", $0) }.joined(),
        "truncated": value.count > 16,
      ]
      if records == 128 {
        row["event"] = "overflow"
        row["suppressedEvent"] = String(stage.prefix(80))
        row["recordLimit"] = 128
        row["droppedAtLeast"] = 1
      }
      func encode() -> Data? {
        guard let json = try? JSONSerialization.data(withJSONObject: row, options: [.sortedKeys]) else { return nil }
        var line = Data("BLEHUB_APPLE_NOTIFICATION_DIAGNOSTIC ".utf8)
        line.append(json); line.append(0x0a)
        return line
      }
      guard var line = encode() else { return }
      if line.count > 3000 {
        row.removeValue(forKey: "valueHex")
        row.removeValue(forKey: "note")
        row["truncated"] = true
        row["truncationReason"] = "record-byte-limit"
        guard let fallback = encode() else { return }
        line = fallback
      }
      if line.count <= 3000 { try? FileHandle.standardError.write(contentsOf: line) }
    }
  }
}

private final class NitroTestSink: BleEventSink, @unchecked Sendable {
  private let handoff: BoundedEventHandoff<BleEvent>
  private let notificationDiagnostics: NitroNotificationDiagnostics

  init(
    stateQueue: DispatchQueue,
    notificationDiagnostics: NitroNotificationDiagnostics,
    receive: @escaping (BleEvent) -> Void
  ) {
    self.notificationDiagnostics = notificationDiagnostics
    handoff = BoundedEventHandoff(
      stateQueue: stateQueue,
      capacity: 256,
      deliver: receive
    )
  }

  func onEvent(event: BleEvent) throws {
    let accepted = handoff.submit(event)
    notificationDiagnostics.record("handoff.submit", event: event, note: accepted ? "accepted" : "rejected")
    guard accepted else {
      throw EventSinkError.Rejected(
        detail: "Nitro event handoff is closed or at capacity"
      )
    }
  }

  var diagnosticPendingCount: Int { handoff.diagnosticPendingCount }

  func closeAndDrain(_ completion: @escaping () -> Void) {
    handoff.closeAndDrain(completion)
  }

  func close() {
    handoff.close()
  }
}

fileprivate struct PendingReadDiagnostic {
  let commandId: String
  let connectionId: String
  let gattSessionId: String
  let characteristicId: String
  let command: CommandId
  let connection: ConnectionId
  let gattSession: GattSessionId
  let characteristic: GattCharacteristicId
}

fileprivate struct NativeBoundaryDiagnostic {
  let runId: String
  let probeId: String
  let read: PendingReadDiagnostic
}

private struct CloseTransition {
  let promise: Promise<Void>
  let currentHub: BleHub?
  let currentBackend: AppleBleBackend?
  let currentSink: NitroTestSink?
  let readDiagnostic: PendingReadDiagnostic?
  let started: Bool
  let disposeForTesting: (() throws -> Void)?
}

private extension BleEventError {
  func toNitroError() -> EventError {
    EventError(
      category: eventErrorCategory,
      message: message,
      platformDetails: platformDetails?.toNitroDetails()
    )
  }

  var eventErrorCategory: EventErrorCategory {
    switch category {
    case .permission: .permission
    case .adapterUnavailable: .adapterUnavailable
    case .invalidState: .invalidState
    case .notFound: .notFound
    case .timeout: .timeout
    case .cancelled: .cancelled
    case .operationAborted: .operationAborted
    case .connection: .connection
    case .gatt: .gatt
    case .protocol: .protocol
    case .notSupported: .notSupported
    case .backend: .backend
    case .internal: .internal
    }
  }
}

private extension PlatformErrorDetails {
  func toNitroDetails() -> NitroPlatformErrorDetails {
    NitroPlatformErrorDetails(
      platform: nitroPlatform,
      code: code,
      name: name,
      message: message
    )
  }

  var nitroPlatform: NitroPlatform {
    switch platform {
    case .windows: .platformWindows
    case .android: .platformAndroid
    case .apple: .platformApple
    case .linux: .platformLinux
    case .test: .platformTest
    }
  }
}

private extension CommandDispatchError {
  func toNitroError() -> DispatchError {
    switch self {
    case .Closed:
      DispatchError(category: .closed, detail: "BleHub probe is closed", platformDetails: nil)
    case let .InvalidRequest(detail):
      DispatchError(category: .invalidRequest, detail: detail, platformDetails: nil)
    case let .InvalidState(detail):
      DispatchError(category: .invalidState, detail: detail, platformDetails: nil)
    case let .OperationNotFound(targetCommandId):
      DispatchError(
        category: .operationNotFound,
        detail: "operation \(targetCommandId.value) was not found: cancellation target is not active",
        platformDetails: nil
      )
    case let .OperationAlreadyCompleted(targetCommandId):
      DispatchError(
        category: .operationAlreadyCompleted,
        detail: "operation \(targetCommandId.value) has already completed",
        platformDetails: nil
      )
    case let .OperationNotCancellable(targetCommandId):
      DispatchError(
        category: .operationNotCancellable,
        detail: "operation \(targetCommandId.value) is not cancellable",
        platformDetails: nil
      )
    case let .ResourceExhausted(detail):
      DispatchError(category: .resourceExhausted, detail: detail, platformDetails: nil)
    case let .BackendUnavailable(detail, platformDetails):
      DispatchError(
        category: .backendUnavailable,
        detail: detail,
        platformDetails: platformDetails?.toNitroDetails()
      )
    case let .FfiContract(detail):
      DispatchError(category: .ffiContract, detail: detail, platformDetails: nil)
    }
  }
}

private extension BleHubError {
  var detailText: String {
    switch self {
    case let .BackendAssembly(detail): detail
    case let .BackendCleanup(detail, _): detail
    case let .ResourceBusy(detail): detail
    case let .InvalidConfiguration(field, detail): "\(field): \(detail)"
    case let .FfiContract(detail): detail
    }
  }
}

/// Internal Nitro probe backed by the production Apple backend and UniFFI BleHub
/// object. This is a host integration probe, not a public React Native API.
/// BLE outcomes remain native events; the probe projects completion and the
/// RF scan and connection slices into the small Nitro surface used by this integration.
final class HybridBleHubProbe: HybridBleHubProbeSpec {
  private let queue = DispatchQueue(label: "dev.blehub.react-native.state")
  private let stateQueueKey = DispatchSpecificKey<UInt8>()
  private let diagnosticTestSeam: Bool
  private var closedRfResources: [String: Int]?
  private let probeId: String
  fileprivate var pendingProbeId: String { probeId }
  private var completionListener: ((CompletionEvent) -> Void)?
  private var rfScanListener: ((RfScanEvent) -> Void)?
  private let notificationDiagnostics = NitroNotificationDiagnostics()
  private var isClosed = false
  private var hasEmittedPendingReadAccepted = false
  private var pendingReadDiagnostic: PendingReadDiagnostic?
  private var hub: BleHub?
  private var backend: AppleBleBackend?
  private var sink: NitroTestSink?
  private var closePromise: Promise<Void>?
  private var disposeForTesting: (() throws -> Void)?
  fileprivate weak var pendingOwner: PendingReloadOwner?
  private var engineClose = false
  private var syntheticClosed = false

  override init() {
    diagnosticTestSeam = false
    probeId = UUID().uuidString.lowercased()
    disposeForTesting = nil
    super.init()
    queue.setSpecific(key: stateQueueKey, value: 1)
  }

  private init(disposeForTesting: @escaping () throws -> Void) {
    diagnosticTestSeam = true
    probeId = UUID().uuidString.lowercased()
    self.disposeForTesting = disposeForTesting
    super.init()
    queue.setSpecific(key: stateQueueKey, value: 1)
  }

#if DEBUG
  // Native lifecycle tests only: never returned by the generated Nitro factory.
  // snapshotRfResources continues to reject this seam; it is not RF/backend evidence.
  static func forPendingLifecycleTest(dispose: @escaping () throws -> Void) -> HybridBleHubProbe {
    HybridBleHubProbe(disposeForTesting: dispose)
  }

  func preparePendingReadForLifecycleTest() {
    precondition(diagnosticTestSeam)
    onStateQueue {
      let read = PendingReadDiagnostic(commandId: "12011", connectionId: "12004", gattSessionId: "12008", characteristicId: "20",
        command: CommandId(value: 12011), connection: ConnectionId(value: 12004),
        gattSession: GattSessionId(value: 12008), characteristic: GattCharacteristicId(value: 20))
      sink = NitroTestSink(stateQueue: queue, notificationDiagnostics: notificationDiagnostics) { _ in }
      pendingReadDiagnostic = read
      pendingOwner?.acceptRead(read)
    }
  }
#endif

  /// Creates a probe whose cleanup is deliberately blocked by the supplied gate.
  /// This is an internal native-test seam; the Nitro spec and production factory
  /// remain unchanged and always assemble the real Apple backend.
  static func forDisposeWaitTest(_ gate: DispatchSemaphore) -> HybridBleHubProbe {
    HybridBleHubProbe(disposeForTesting: {
      gate.wait()
    })
  }

  func initialize(commandId: String) throws -> DispatchError? {
    guard let value = Self.parseCanonicalU64(commandId) else {
      return DispatchError(
        category: .invalidRequest,
        detail: "commandId must be a canonical unsigned decimal u64",
        platformDetails: nil
      )
    }

    do {
      let hub = try queue.sync { () -> BleHub in
        guard !isClosed else {
          throw ProbeError.closed
        }
        if (BleHubPendingReloadBridge.enabled || pendingOwner != nil) && pendingOwner?.canInitialize != true {
          throw CommandDispatchError.InvalidState(detail: "native pending-reload barrier is not ready")
        }
        if let hub { return hub }

        uniffiEnsureBlehubInitialized()
        let backend = try AppleBleBackend(
          queueLabel: "dev.blehub.react-native.apple"
        )
        let sink = NitroTestSink(stateQueue: queue, notificationDiagnostics: notificationDiagnostics) { [weak self] event in
          // BoundedEventHandoff invokes this inline on the state queue, never
          // on the synchronous foreign sink stack. There is deliberately no
          // queue.async or queue.sync here.
          self?.receive(event: event)
        }
        let hub = try BleHub(backend: backend, downstream: sink)
        self.backend = backend
        self.sink = sink
        self.hub = hub
        return hub
      }

      try hub.initialize(command: InitializeCommand(commandId: CommandId(value: value)))
      return nil
    } catch let error as CommandDispatchError {
      return error.toNitroError()
    } catch ProbeError.closed {
      return DispatchError(
        category: .closed,
        detail: "BleHub probe is closed",
        platformDetails: nil
      )
    } catch let error as BleHubError {
      return DispatchError(
        category: .ffiContract,
        detail: error.detailText,
        platformDetails: nil
      )
    } catch {
      return DispatchError(
        category: .ffiContract,
        detail: String(reflecting: error),
        platformDetails: nil
      )
    }
  }

  func cancelOperation(targetCommandId: String) throws -> DispatchError? {
    guard let commandValue = Self.parseCanonicalU64(targetCommandId) else {
      return DispatchError(
        category: .invalidRequest,
        detail: "targetCommandId must be a canonical unsigned decimal u64",
        platformDetails: nil
      )
    }
    do {
      try queue.sync {
        guard !isClosed else { throw ProbeError.closed }
        guard let hub else { throw ProbeError.notInitialized }
        try hub.cancelOperation(targetCommandId: CommandId(value: commandValue))
      }
      return nil
    } catch let error as CommandDispatchError {
      return error.toNitroError()
    } catch ProbeError.closed {
      return DispatchError(category: .closed, detail: "BleHub probe is closed", platformDetails: nil)
    } catch ProbeError.notInitialized {
      return DispatchError(category: .invalidState, detail: "BleHub probe is not initialized", platformDetails: nil)
    } catch {
      return DispatchError(category: .ffiContract, detail: String(reflecting: error), platformDetails: nil)
    }
  }

  func setCompletionListener(listener: @escaping (CompletionEvent) -> Void) throws {
    queue.sync {
      guard !isClosed else { return }
      completionListener = listener
    }
  }

  func removeCompletionListener() throws {
    queue.sync {
      completionListener = nil
    }
  }

  func setRfScanListener(listener: @escaping (RfScanEvent) -> Void) throws {
    queue.sync {
      guard !isClosed else { return }
      rfScanListener = listener
    }
  }

  func removeRfScanListener() throws {
    queue.sync {
      rfScanListener = nil
    }
  }

  func startRfScan(
    commandId: String,
    scanSessionId: String,
    serviceUuid: String
  ) throws -> DispatchError? {
    guard let commandValue = Self.parseCanonicalU64(commandId),
          let scanSessionValue = Self.parseCanonicalU64(scanSessionId) else {
      return DispatchError(
        category: .invalidRequest,
        detail: "commandId and scanSessionId must be canonical unsigned decimal u64 values",
        platformDetails: nil
      )
    }
    let serviceValues = serviceUuid
      .split(separator: ",")
      .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
      .filter { !$0.isEmpty }
    guard !serviceValues.isEmpty else {
      return DispatchError(
        category: .invalidRequest,
        detail: "serviceUuid must contain one or more canonical UUIDs",
        platformDetails: nil
      )
    }

    do {
      try queue.sync {
        guard !isClosed else {
          throw ProbeError.closed
        }
        guard let hub else {
          throw ProbeError.notInitialized
        }
        try hub.startScan(
          command: StartScanCommand(
            commandId: CommandId(value: commandValue),
            scanSessionId: ScanSessionId(value: scanSessionValue),
            options: ScanOptions(
              serviceUuids: serviceValues.map { BluetoothUuid(value: $0) },
              namePrefix: nil,
              companyId: nil,
              allowDuplicates: false
            )
          )
        )
      }
      return nil
    } catch let error as CommandDispatchError {
      return error.toNitroError()
    } catch ProbeError.closed {
      return DispatchError(
        category: .closed,
        detail: "BleHub probe is closed",
        platformDetails: nil
      )
    } catch ProbeError.notInitialized {
      return DispatchError(
        category: .invalidState,
        detail: "BleHub probe is not initialized",
        platformDetails: nil
      )
    } catch {
      return DispatchError(
        category: .ffiContract,
        detail: String(reflecting: error),
        platformDetails: nil
      )
    }
  }

  func stopRfScan(
    commandId: String,
    scanSessionId: String
  ) throws -> DispatchError? {
    guard let commandValue = Self.parseCanonicalU64(commandId),
          let scanSessionValue = Self.parseCanonicalU64(scanSessionId) else {
      return DispatchError(
        category: .invalidRequest,
        detail: "commandId and scanSessionId must be canonical unsigned decimal u64 values",
        platformDetails: nil
      )
    }

    do {
      try queue.sync {
        guard !isClosed else {
          throw ProbeError.closed
        }
        guard let hub else {
          throw ProbeError.notInitialized
        }
        try hub.stopScan(
          command: StopScanCommand(
            commandId: CommandId(value: commandValue),
            scanSessionId: ScanSessionId(value: scanSessionValue)
          )
        )
      }
      return nil
    } catch let error as CommandDispatchError {
      return error.toNitroError()
    } catch ProbeError.closed {
      return DispatchError(
        category: .closed,
        detail: "BleHub probe is closed",
        platformDetails: nil
      )
    } catch ProbeError.notInitialized {
      return DispatchError(
        category: .invalidState,
        detail: "BleHub probe is not initialized",
        platformDetails: nil
      )
    } catch {
      return DispatchError(
        category: .ffiContract,
        detail: String(reflecting: error),
        platformDetails: nil
      )
    }
  }

  func connectRfPeripheral(
    commandId: String,
    connectionId: String,
    peripheralId: String
  ) throws -> DispatchError? {
    guard let commandValue = Self.parseCanonicalU64(commandId),
          let connectionValue = Self.parseCanonicalU64(connectionId),
          let peripheralValue = Self.parseCanonicalU64(peripheralId) else {
      return DispatchError(
        category: .invalidRequest,
        detail: "commandId, connectionId and peripheralId must be canonical unsigned decimal u64 values",
        platformDetails: nil
      )
    }
    do {
      try queue.sync {
        guard !isClosed else { throw ProbeError.closed }
        guard let hub else { throw ProbeError.notInitialized }
        try hub.connect(
          command: ConnectCommand(
            commandId: CommandId(value: commandValue),
            connectionId: ConnectionId(value: connectionValue),
            peripheralId: PeripheralId(value: peripheralValue)
          )
        )
      }
      return nil
    } catch let error as CommandDispatchError {
      return error.toNitroError()
    } catch ProbeError.closed {
      return DispatchError(category: .closed, detail: "BleHub probe is closed", platformDetails: nil)
    } catch ProbeError.notInitialized {
      return DispatchError(category: .invalidState, detail: "BleHub probe is not initialized", platformDetails: nil)
    } catch {
      return DispatchError(category: .ffiContract, detail: String(reflecting: error), platformDetails: nil)
    }
  }

  func disconnectRfPeripheral(
    commandId: String,
    connectionId: String
  ) throws -> DispatchError? {
    guard let commandValue = Self.parseCanonicalU64(commandId),
          let connectionValue = Self.parseCanonicalU64(connectionId) else {
      return DispatchError(
        category: .invalidRequest,
        detail: "commandId and connectionId must be canonical unsigned decimal u64 values",
        platformDetails: nil
      )
    }
    do {
      try queue.sync {
        guard !isClosed else { throw ProbeError.closed }
        guard let hub else { throw ProbeError.notInitialized }
        try hub.disconnect(
          command: DisconnectCommand(
            commandId: CommandId(value: commandValue),
            connectionId: ConnectionId(value: connectionValue)
          )
        )
      }
      return nil
    } catch let error as CommandDispatchError {
      return error.toNitroError()
    } catch ProbeError.closed {
      return DispatchError(category: .closed, detail: "BleHub probe is closed", platformDetails: nil)
    } catch ProbeError.notInitialized {
      return DispatchError(category: .invalidState, detail: "BleHub probe is not initialized", platformDetails: nil)
    } catch {
      return DispatchError(category: .ffiContract, detail: String(reflecting: error), platformDetails: nil)
    }
  }

  func readRfRssi(
    commandId: String,
    connectionId: String
  ) throws -> DispatchError? {
    guard let commandValue = Self.parseCanonicalU64(commandId),
          let connectionValue = Self.parseCanonicalU64(connectionId) else {
      return DispatchError(
        category: .invalidRequest,
        detail: "commandId and connectionId must be canonical unsigned decimal u64 values",
        platformDetails: nil
      )
    }
    do {
      try queue.sync {
        guard !isClosed else { throw ProbeError.closed }
        guard let hub else { throw ProbeError.notInitialized }
        try hub.readRssi(
          command: ReadRssiCommand(
            commandId: CommandId(value: commandValue),
            connectionId: ConnectionId(value: connectionValue)
          )
        )
      }
      return nil
    } catch let error as CommandDispatchError {
      return error.toNitroError()
    } catch ProbeError.closed {
      return DispatchError(category: .closed, detail: "BleHub probe is closed", platformDetails: nil)
    } catch ProbeError.notInitialized {
      return DispatchError(category: .invalidState, detail: "BleHub probe is not initialized", platformDetails: nil)
    } catch {
      return DispatchError(category: .ffiContract, detail: String(reflecting: error), platformDetails: nil)
    }
  }

  func discoverRfGatt(
    commandId: String,
    connectionId: String,
    gattSessionId: String
  ) throws -> DispatchError? {
    guard let commandValue = Self.parseCanonicalU64(commandId),
          let connectionValue = Self.parseCanonicalU64(connectionId),
          let gattSessionValue = Self.parseCanonicalU64(gattSessionId) else {
      return DispatchError(
        category: .invalidRequest,
        detail: "commandId, connectionId and gattSessionId must be canonical unsigned decimal u64 values",
        platformDetails: nil
      )
    }
    do {
      try queue.sync {
        guard !isClosed else { throw ProbeError.closed }
        guard let hub else { throw ProbeError.notInitialized }
        try hub.discoverGatt(
          command: DiscoverGattCommand(
            commandId: CommandId(value: commandValue),
            connectionId: ConnectionId(value: connectionValue),
            gattSessionId: GattSessionId(value: gattSessionValue)
          )
        )
      }
      return nil
    } catch let error as CommandDispatchError {
      return error.toNitroError()
    } catch ProbeError.closed {
      return DispatchError(category: .closed, detail: "BleHub probe is closed", platformDetails: nil)
    } catch ProbeError.notInitialized {
      return DispatchError(category: .invalidState, detail: "BleHub probe is not initialized", platformDetails: nil)
    } catch {
      return DispatchError(category: .ffiContract, detail: String(reflecting: error), platformDetails: nil)
    }
  }

  func readRfCharacteristic(
    commandId: String,
    connectionId: String,
    gattSessionId: String,
    characteristicId: String
  ) throws -> DispatchError? {
    guard let commandValue = Self.parseCanonicalU64(commandId),
          let connectionValue = Self.parseCanonicalU64(connectionId),
          let gattSessionValue = Self.parseCanonicalU64(gattSessionId),
          let characteristicValue = Self.parseCanonicalU64(characteristicId) else {
      return DispatchError(
        category: .invalidRequest,
        detail: "commandId, connectionId, gattSessionId and characteristicId must be canonical unsigned decimal u64 values",
        platformDetails: nil
      )
    }
    do {
      try queue.sync {
        guard !isClosed else { throw ProbeError.closed }
        guard let hub else { throw ProbeError.notInitialized }
        try hub.readCharacteristic(
          command: ReadCharacteristicCommand(
            commandId: CommandId(value: commandValue),
            connectionId: ConnectionId(value: connectionValue),
            gattSessionId: GattSessionId(value: gattSessionValue),
            characteristicId: GattCharacteristicId(value: characteristicValue)
          )
        )
        if commandValue == 12011, connectionValue == 12004, gattSessionValue == 12008,
           pendingOwner?.generation == 1,
           !hasEmittedPendingReadAccepted,
           let runId = pendingOwner?.session.runId
        {
          hasEmittedPendingReadAccepted = true
          let read = PendingReadDiagnostic(
            commandId: commandId,
            connectionId: connectionId,
            gattSessionId: gattSessionId,
            characteristicId: characteristicId,
            command: CommandId(value: commandValue),
            connection: ConnectionId(value: connectionValue),
            gattSession: GattSessionId(value: gattSessionValue),
            characteristic: GattCharacteristicId(value: characteristicValue)
          )
          pendingReadDiagnostic = read
          pendingOwner?.acceptRead(read)
          Self.postNativeBoundaryDiagnostic(
            event: "read-accepted",
            diagnostic: NativeBoundaryDiagnostic(
              runId: runId,
              probeId: probeId,
              read: read
            )
          )
        }
      }
      return nil
    } catch let error as CommandDispatchError {
      return error.toNitroError()
    } catch ProbeError.closed {
      return DispatchError(category: .closed, detail: "BleHub probe is closed", platformDetails: nil)
    } catch ProbeError.notInitialized {
      return DispatchError(category: .invalidState, detail: "BleHub probe is not initialized", platformDetails: nil)
    } catch {
      return DispatchError(category: .ffiContract, detail: String(reflecting: error), platformDetails: nil)
    }
  }

  func writeRfCharacteristic(
    commandId: String,
    connectionId: String,
    gattSessionId: String,
    characteristicId: String,
    value: ArrayBuffer,
    writeType: RfWriteType
  ) throws -> DispatchError? {
    guard let commandValue = Self.parseCanonicalU64(commandId),
          let connectionValue = Self.parseCanonicalU64(connectionId),
          let gattSessionValue = Self.parseCanonicalU64(gattSessionId),
          let characteristicValue = Self.parseCanonicalU64(characteristicId) else {
      return DispatchError(
        category: .invalidRequest,
        detail: "commandId, connectionId, gattSessionId and characteristicId must be canonical unsigned decimal u64 values",
        platformDetails: nil
      )
    }
    let nativeWriteType: WriteType
    switch writeType {
    case .withResponse: nativeWriteType = .withResponse
    case .withoutResponse: nativeWriteType = .withoutResponse
    }
    do {
      try queue.sync {
        guard !isClosed else { throw ProbeError.closed }
        guard let hub else { throw ProbeError.notInitialized }
        try hub.writeCharacteristic(
          command: WriteCharacteristicCommand(
            commandId: CommandId(value: commandValue),
            connectionId: ConnectionId(value: connectionValue),
            gattSessionId: GattSessionId(value: gattSessionValue),
            characteristicId: GattCharacteristicId(value: characteristicValue),
            value: value.toData(copyIfNeeded: true),
            writeType: nativeWriteType
          )
        )
      }
      return nil
    } catch let error as CommandDispatchError {
      return error.toNitroError()
    } catch ProbeError.closed {
      return DispatchError(category: .closed, detail: "BleHub probe is closed", platformDetails: nil)
    } catch ProbeError.notInitialized {
      return DispatchError(category: .invalidState, detail: "BleHub probe is not initialized", platformDetails: nil)
    } catch {
      return DispatchError(category: .ffiContract, detail: String(reflecting: error), platformDetails: nil)
    }
  }

  func setRfNotifications(
    commandId: String,
    connectionId: String,
    gattSessionId: String,
    subscriptionId: String,
    characteristicId: String,
    enabled: Bool,
    mode: RfNotificationMode
  ) throws -> DispatchError? {
    guard let commandValue = Self.parseCanonicalU64(commandId),
          let connectionValue = Self.parseCanonicalU64(connectionId),
          let gattSessionValue = Self.parseCanonicalU64(gattSessionId),
          let subscriptionValue = Self.parseCanonicalU64(subscriptionId),
          let characteristicValue = Self.parseCanonicalU64(characteristicId) else {
      return DispatchError(
        category: .invalidRequest,
        detail: "commandId, connectionId, gattSessionId, subscriptionId and characteristicId must be canonical unsigned decimal u64 values",
        platformDetails: nil
      )
    }
    let action: NotificationAction
    if enabled {
      let request: NotificationModeRequest
      switch mode {
      case .platformDefault: request = .platformDefault
      case .requireNotification: request = .requireNotification
      case .requireIndication: request = .requireIndication
      }
      action = .enable(mode: request)
    } else {
      action = .disable
    }
    do {
      try queue.sync {
        guard !isClosed else { throw ProbeError.closed }
        guard let hub else { throw ProbeError.notInitialized }
        try hub.setNotifications(
          command: SetNotificationsCommand(
            commandId: CommandId(value: commandValue),
            connectionId: ConnectionId(value: connectionValue),
            gattSessionId: GattSessionId(value: gattSessionValue),
            subscriptionId: SubscriptionId(value: subscriptionValue),
            characteristicId: GattCharacteristicId(value: characteristicValue),
            action: action
          )
        )
      }
      return nil
    } catch let error as CommandDispatchError {
      return error.toNitroError()
    } catch ProbeError.closed {
      return DispatchError(category: .closed, detail: "BleHub probe is closed", platformDetails: nil)
    } catch ProbeError.notInitialized {
      return DispatchError(category: .invalidState, detail: "BleHub probe is not initialized", platformDetails: nil)
    } catch {
      return DispatchError(category: .ffiContract, detail: String(reflecting: error), platformDetails: nil)
    }
  }

  func roundTrip(sample: TransportSample) throws -> TransportSample {
    TransportSample(
      id: sample.id,
      bytes: ArrayBuffer.copy(of: sample.bytes),
      error: sample.error
    )
  }

  func awaitPendingReloadReady(runId: String, generation: Double) throws -> Promise<Void> {
    let promise = Promise<Void>()
    guard let owner = onStateQueue({ pendingOwner }) else {
      promise.reject(withError: PendingReloadSession.error("probe is not bound to a native runtime owner"))
      return promise
    }
    owner.session.awaitReady(owner, runId: runId, generation: generation) { result in
      // This Promise belongs to the LIVE runtime. Old cleanup uses only native completions.
      switch result {
      case .success: promise.resolve()
      case .failure(let error): promise.reject(withError: error)
      }
    }
    return promise
  }

  // SPI observations run asynchronously on the same queue as backend commands.
  // Probe roots are captured at the enqueue boundary; handoffPending is an
  // independent lock-stable handler count, not a native/JS queue-drain guarantee.
  func snapshotRfResources() throws -> Promise<RfResourceSnapshot> {
    let promise = Promise<RfResourceSnapshot>()
    queue.async { [self] in
      guard !diagnosticTestSeam else {
        promise.reject(withError: diagnosticUnavailable())
        return
      }
      if let closedRfResources {
        Self.resolveRfSnapshot(closedRfResources, promise: promise)
      } else if let closePromise {
        closePromise.then { [self] _ in
          queue.async { [self] in
            guard let closedRfResources else {
              promise.reject(withError: diagnosticUnavailable())
              return
            }
            Self.resolveRfSnapshot(closedRfResources, promise: promise)
          }
        }.catch { promise.reject(withError: $0) }
      } else {
        let probeCounts = diagnosticProbeCounts()
        if let backend {
          backend.diagnosticSnapshotRfResources { counts in
            Self.resolveRfSnapshot(
              counts.merging(probeCounts) { _, probe in probe }, promise: promise
            )
          }
        } else {
          // Structural absence before initialize, never a substitute for sampling
          // the disposed production backend while it is still retained.
          Self.resolveRfSnapshot(
            Self.absentBackendCounts.merging(probeCounts) { _, probe in probe }, promise: promise
          )
        }
      }
    }
    return promise
  }

  private static func resolveRfSnapshot(
    _ counts: [String: Int], promise: Promise<RfResourceSnapshot>
  ) {
    // Promise listeners can synchronously re-enter command dispatch. Settle
    // outside both owned serial queues, with only primitive counts captured.
    DispatchQueue.global(qos: .utility).async {
      promise.resolve(withResult: counts.toRfResourceSnapshot())
    }
  }

  private func diagnosticUnavailable() -> NSError {
    NSError(domain: "BleHubNitroProbe", code: 2, userInfo: [
      NSLocalizedDescriptionKey: "RF resource diagnostics require production Apple cleanup evidence",
    ])
  }

  private static let absentBackendCounts: [String: Int] = Dictionary(uniqueKeysWithValues: [
    "activeScans", "connections", "gattAttributes", "subscriptions", "pendingIo", "watchdogs",
    "callbackRoutes", "ingressRoots", "adapterDelegates", "peerCacheEntries", "backendLeases",
  ].map { ($0, 0) })

  private func diagnosticProbeCounts() -> [String: Int] {
    [
      "hubRoots": hub == nil ? 0 : 1,
      "handoffRoots": sink == nil ? 0 : 1,
      "handoffPending": sink?.diagnosticPendingCount ?? 0,
      "listeners": (completionListener == nil ? 0 : 1) + (rfScanListener == nil ? 0 : 1),
      // GCD queues use shared system workers; this adapter creates no dedicated threads.
      "workerThreads": 0,
      // Only the explicit pending-reload test mode binds a runtime owner.
      "lifecycleOwners": pendingOwner?.membershipCount ?? 0,
    ]
  }

  private func onStateQueue<T>(_ action: () throws -> T) rethrows -> T {
    if DispatchQueue.getSpecific(key: stateQueueKey) == 1 { return try action() }
    return try queue.sync(execute: action)
  }

  func close() throws -> Promise<Void> { closeInternal(engineInvalidation: false) }

  fileprivate func closeForEngineInvalidation() { _ = closeInternal(engineInvalidation: true) }

  fileprivate func bindPendingOwner(_ owner: PendingReloadOwner) -> Bool {
    onStateQueue {
      guard pendingOwner == nil, !isClosed, hub == nil else { return false }
      pendingOwner = owner
      return true
    }
  }

  private func closeInternal(engineInvalidation: Bool) -> Promise<Void> {
    let transition = onStateQueue {
      () -> CloseTransition in
      if let closePromise {
        if engineInvalidation && !engineClose && pendingOwner?.generation == 1 { pendingOwner?.session.fail("first-close-was-not-engine-invalidation") }
        // The first caller owns the cleanup job. Every later caller returns
        // the exact same Promise and must not start a second resolver.
        return CloseTransition(
          promise: closePromise,
          currentHub: nil,
          currentBackend: nil,
          currentSink: nil,
          readDiagnostic: nil,
          started: false,
          disposeForTesting: nil
        )
      }

      let promise = Promise<Void>()
      closePromise = promise
      engineClose = engineInvalidation
      pendingOwner?.beginClose(engineInvalidation: engineInvalidation, read: pendingReadDiagnostic)
      isClosed = true
      // Invalidate all listener routes before starting Hub cleanup. Events
      // already handed to Nitro may still drain after this Promise resolves.
      completionListener = nil
      rfScanListener = nil
      let currentHub = hub
      let currentBackend = backend
      let currentSink = sink
      let readDiagnostic = pendingReadDiagnostic
      let disposeForTesting = self.disposeForTesting
      self.disposeForTesting = nil
      currentSink?.close()
      return CloseTransition(
        promise: promise,
        currentHub: currentHub,
        currentBackend: currentBackend,
        currentSink: currentSink,
        readDiagnostic: readDiagnostic,
        started: true,
        disposeForTesting: disposeForTesting
      )
    }

    let promise = transition.promise
    guard transition.started else { return promise }
    guard transition.currentHub != nil || transition.disposeForTesting != nil else {
      // Closing an uninitialized probe has no native cleanup, but it still
      // resolves asynchronously and shares this same Promise on later calls.
      DispatchQueue.global(qos: .utility).async {
        self.finishNativeClose(
          backendCounts: Self.absentBackendCounts,
          currentSink: transition.currentSink,
          promise: promise
        )
      }
      return promise
    }

    let diagnostic = transition.readDiagnostic.flatMap { read in
      pendingOwner.map { owner in
        let runId = owner.session.runId
        return NativeBoundaryDiagnostic(runId: runId, probeId: probeId, read: read)
      }
    }
    let currentSink = transition.currentSink
    let dispose: () throws -> Void
    if let currentHub = transition.currentHub {
      dispose = { try currentHub.dispose() }
    } else {
      dispose = transition.disposeForTesting!
    }

    // BleHub.dispose() is synchronous and may wait for backend/ingress cleanup.
    // Never run it on the Nitro/JS caller stack or the probe state queue.
    DispatchQueue.global(qos: .utility).async {
      do {
        try dispose()
        if let diagnostic, self.engineClose {
          Self.postNativeBoundaryDiagnostic(
            event: "dispose-returned",
            diagnostic: diagnostic
          )
          self.syntheticClosed = Self.submitLateDiagnosticRead(
            to: currentSink,
            diagnostic: diagnostic
          )
          if !self.syntheticClosed { self.pendingOwner?.session.fail("synthetic-submit-was-not-closed") }
        }
        if let currentBackend = transition.currentBackend {
          // Observe disposed ownership BEFORE releasing the backend reference.
          currentBackend.diagnosticSnapshotRfResources { counts in
            self.finishNativeClose(backendCounts: counts, currentSink: currentSink, promise: promise)
          }
        } else {
          self.finishNativeClose(
            backendCounts: Self.absentBackendCounts, currentSink: currentSink, promise: promise
          )
        }
      } catch {
        self.pendingOwner?.session.fail("dispose-failed")
        promise.reject(withError: error)
      }
    }
    return promise
  }

  private func finishNativeClose(
    backendCounts: [String: Int],
    currentSink: NitroTestSink?,
    promise: Promise<Void>
  ) {
    let finish = { [self] in
      // Runs on the state queue after every accepted handoff handler returned.
      let pending = currentSink?.diagnosticPendingCount ?? 0
      precondition(pending == 0)
      hub = nil
      backend = nil
      sink = nil
      pendingReadDiagnostic = nil
      var counts = diagnosticProbeCounts()
      counts["handoffPending"] = pending
      let resources = backendCounts.merging(counts) { _, probe in probe }
      let owner = pendingOwner
      // Retire native membership and verify every other root before publishing readiness.
      if let owner { owner.session.completeClose(owner, resources: resources, syntheticClosed: syntheticClosed) }
      counts["lifecycleOwners"] = owner?.membershipCount ?? 0
      closedRfResources = backendCounts.merging(counts) { _, probe in probe }
      DispatchQueue.global(qos: .utility).async { promise.resolve() }
    }
    if let currentSink {
      currentSink.closeAndDrain(finish)
    } else {
      queue.async(execute: DispatchWorkItem(block: finish))
    }
  }

  /// Called only on `queue` by the native handoff. Do not add queue.sync here:
  /// the old implementation deadlocked by synchronously re-entering this queue.
  private func receive(event: BleEvent) {
    notificationDiagnostics.record("receive.entry", event: event, note: isClosed ? "closed-drop" : rfScanListener == nil ? "no-rf-listener" : "rf-listener-present")
    guard !isClosed else { return }
    if let owner = pendingOwner, owner.generation == 1, let read = pendingReadDiagnostic {
      switch event {
      case .characteristicRead(let command, _, _, _, _), .commandCompleted(let command, _):
        if command == read.command { owner.session.fail("old-pending-read-settled-before-invalidation") }
      default: break
      }
    }

    if case let .commandCompleted(commandId, error) = event,
       let listener = completionListener {
      // Nitro generated void callbacks use Nitro's asynchronous dispatcher. This
      // native call only submits to that dispatcher and does not synchronously
      // execute JS or permit JS-side reentry into this object.
      listener(
        CompletionEvent(
          commandId: String(commandId.value),
          error: error?.toNitroError()
        )
      )
    }

    guard let listener = rfScanListener,
          let projected = Self.projectRfScanEvent(event) else { return }

    // The bounded handoff has already moved execution off the foreign sink
    // callback stack. Nitro's generated callback then performs its own async JS
    // dispatch; this call never invokes JS synchronously on the native callback.
    notificationDiagnostics.record("listener.dispatch", event: event, note: "submitting-to-Nitro")
    listener(projected)
    notificationDiagnostics.record("listener.returned", event: event, note: "native-call-returned;JS-delivery-not-proven")
  }

  fileprivate static func postNativeBoundaryDiagnostic(
    event: String,
    diagnostic: NativeBoundaryDiagnostic
  ) {
    let suffix = event == "late-submit-rejected" ? ":source=synthetic:result=closed" : ""
    let line = "BLEHUB_NITRO_RUNTIME rf-pending-read-reload:native:\(event):runId=\(diagnostic.runId):probeId=\(diagnostic.probeId):commandId=\(diagnostic.read.commandId):connectionId=\(diagnostic.read.connectionId):gattSessionId=\(diagnostic.read.gattSessionId):characteristicId=\(diagnostic.read.characteristicId)\(suffix)"
    PendingReloadSession.write(line)
  }

  private static func submitLateDiagnosticRead(
    to sink: NitroTestSink?,
    diagnostic: NativeBoundaryDiagnostic
  ) -> Bool {
    guard let sink else { return false }
    do {
      try sink.onEvent(
        event: .characteristicRead(
          commandId: diagnostic.read.command,
          connectionId: diagnostic.read.connection,
          gattSessionId: diagnostic.read.gattSession,
          characteristicId: diagnostic.read.characteristic,
          value: Data([0])
        )
      )
    } catch let error as EventSinkError {
      guard case .Rejected = error else { return false }
      postNativeBoundaryDiagnostic(
        event: "late-submit-rejected",
        diagnostic: diagnostic
      )
      return true
    } catch {
      return false
    }
    return false
  }

  private static func projectRfScanEvent(_ event: BleEvent) -> RfScanEvent? {
    switch event {
    case let .adapterSnapshotChanged(commandId, snapshot):
      return RfScanEvent(
        kind: "adapterSnapshotChanged",
        commandId: commandId.map { String($0.value) },
        scanSessionId: nil,
        peripheralId: nil,
        connectionId: nil,
        connectionState: nil,
        gattSessionId: nil,
        characteristicId: nil,
        subscriptionId: nil,
        gattServices: nil,
        value: nil,
        notificationState: nil,
        actualNotificationMode: nil,
        rssi: nil,
        localName: nil,
        advertisedServices: nil,
        adapterState: adapterStateText(snapshot.state),
        scanAuthorization: authorizationText(snapshot.scanAuthorization),
        error: nil
      )

    case let .scanStarted(commandId, scanId):
      return RfScanEvent(
        kind: "scanStarted",
        commandId: String(commandId.value),
        scanSessionId: String(scanId.value),
        peripheralId: nil,
        connectionId: nil,
        connectionState: nil,
        gattSessionId: nil,
        characteristicId: nil,
        subscriptionId: nil,
        gattServices: nil,
        value: nil,
        notificationState: nil,
        actualNotificationMode: nil,
        rssi: nil,
        localName: nil,
        advertisedServices: nil,
        adapterState: nil,
        scanAuthorization: nil,
        error: nil
      )

    case let .advertisementObserved(advertisement):
      return RfScanEvent(
        kind: "advertisementObserved",
        commandId: nil,
        scanSessionId: String(advertisement.scanSessionId.value),
        peripheralId: String(advertisement.peripheralId.value),
        connectionId: nil,
        connectionState: nil,
        gattSessionId: nil,
        characteristicId: nil,
        subscriptionId: nil,
        gattServices: nil,
        value: nil,
        notificationState: nil,
        actualNotificationMode: nil,
        rssi: nil,
        localName: advertisement.localName,
        advertisedServices: advertisement.advertisedServices.map(\.value),
        adapterState: nil,
        scanAuthorization: nil,
        error: nil
      )

    case let .scanStopped(scanId, result):
      var commandId: String?
      var error: EventError?
      switch result {
      case let .ok(reason):
        if case let .requested(requestedCommandId) = reason {
          commandId = String(requestedCommandId.value)
        }
      case let .err(scanError):
        error = scanError.toNitroError()
      }
      return RfScanEvent(
        kind: "scanStopped",
        commandId: commandId,
        scanSessionId: String(scanId.value),
        peripheralId: nil,
        connectionId: nil,
        connectionState: nil,
        gattSessionId: nil,
        characteristicId: nil,
        subscriptionId: nil,
        gattServices: nil,
        value: nil,
        notificationState: nil,
        actualNotificationMode: nil,
        rssi: nil,
        localName: nil,
        advertisedServices: nil,
        adapterState: nil,
        scanAuthorization: nil,
        error: error
      )

    case let .connectionStateChanged(commandId, connectionId, result):
      let state: String?
      let error: EventError?
      switch result {
      case let .ok(connectionState):
        switch connectionState {
        case .connecting: state = "Connecting"
        case .connected: state = "Connected"
        case .disconnecting: state = "Disconnecting"
        case .disconnected: state = "Disconnected"
        }
        error = nil
      case let .err(connectionError):
        state = nil
        error = connectionError.toNitroError()
      }
      return RfScanEvent(
        kind: "connectionStateChanged",
        commandId: commandId.map { String($0.value) },
        scanSessionId: nil,
        peripheralId: nil,
        connectionId: String(connectionId.value),
        connectionState: state,
        gattSessionId: nil,
        characteristicId: nil,
        subscriptionId: nil,
        gattServices: nil,
        value: nil,
        notificationState: nil,
        actualNotificationMode: nil,
        rssi: nil,
        localName: nil,
        advertisedServices: nil,
        adapterState: nil,
        scanAuthorization: nil,
        error: error
      )

    case let .gattDiscovered(commandId, database):
      let services = database.services.map { service in
        RfGattService(
          id: String(service.id.value),
          uuid: service.uuid.value,
          primary: service.primary,
          characteristics: service.characteristics.map { characteristic in
            RfGattCharacteristic(
              id: String(characteristic.id.value),
              uuid: characteristic.uuid.value,
              readable: characteristic.properties.readable,
              writableWithResponse: characteristic.properties.writableWithResponse,
              writableWithoutResponse: characteristic.properties.writableWithoutResponse,
              notifiable: characteristic.properties.notifiable,
              indicatable: characteristic.properties.indicatable,
              descriptors: characteristic.descriptors.map { descriptor in
                RfGattDescriptor(
                  id: String(descriptor.id.value),
                  uuid: descriptor.uuid.value
                )
              }
            )
          }
        )
      }
      return RfScanEvent(
        kind: "gattDiscovered",
        commandId: String(commandId.value),
        scanSessionId: nil,
        peripheralId: nil,
        connectionId: String(database.connectionId.value),
        connectionState: nil,
        gattSessionId: String(database.gattSessionId.value),
        characteristicId: nil,
        subscriptionId: nil,
        gattServices: services,
        value: nil,
        notificationState: nil,
        actualNotificationMode: nil,
        rssi: nil,
        localName: nil,
        advertisedServices: nil,
        adapterState: nil,
        scanAuthorization: nil,
        error: nil
      )

    case let .rssiRead(commandId, connectionId, rssi):
      return RfScanEvent(
        kind: "rssiRead",
        commandId: String(commandId.value),
        scanSessionId: nil,
        peripheralId: nil,
        connectionId: String(connectionId.value),
        connectionState: nil,
        gattSessionId: nil,
        characteristicId: nil,
        subscriptionId: nil,
        gattServices: nil,
        value: nil,
        notificationState: nil,
        actualNotificationMode: nil,
        rssi: Double(rssi),
        localName: nil,
        advertisedServices: nil,
        adapterState: nil,
        scanAuthorization: nil,
        error: nil
      )

    case let .characteristicRead(commandId, connectionId, gattSessionId, characteristicId, value):
      return RfScanEvent(
        kind: "characteristicRead",
        commandId: String(commandId.value),
        scanSessionId: nil,
        peripheralId: nil,
        connectionId: String(connectionId.value),
        connectionState: nil,
        gattSessionId: String(gattSessionId.value),
        characteristicId: String(characteristicId.value),
        subscriptionId: nil,
        gattServices: nil,
        value: Self.arrayBufferCopy(value),
        notificationState: nil,
        actualNotificationMode: nil,
        rssi: nil,
        localName: nil,
        advertisedServices: nil,
        adapterState: nil,
        scanAuthorization: nil,
        error: nil
      )

    case let .characteristicWritten(commandId, connectionId, gattSessionId, characteristicId):
      return RfScanEvent(
        kind: "characteristicWritten",
        commandId: String(commandId.value),
        scanSessionId: nil,
        peripheralId: nil,
        connectionId: String(connectionId.value),
        connectionState: nil,
        gattSessionId: String(gattSessionId.value),
        characteristicId: String(characteristicId.value),
        subscriptionId: nil,
        gattServices: nil,
        value: nil,
        notificationState: nil,
        actualNotificationMode: nil,
        rssi: nil,
        localName: nil,
        advertisedServices: nil,
        adapterState: nil,
        scanAuthorization: nil,
        error: nil
      )

    case let .notificationStateChanged(commandId, connectionId, gattSessionId, subscriptionId, characteristicId, state):
      let notificationState: String
      let actualNotificationMode: String?
      switch state {
      case let .enabled(mode):
        notificationState = "Enabled"
        switch mode {
        case .notification: actualNotificationMode = "Notification"
        case .indication: actualNotificationMode = "Indication"
        case nil: actualNotificationMode = nil
        }
      case .disabled:
        notificationState = "Disabled"
        actualNotificationMode = nil
      }
      return RfScanEvent(
        kind: "notificationStateChanged",
        commandId: String(commandId.value),
        scanSessionId: nil,
        peripheralId: nil,
        connectionId: String(connectionId.value),
        connectionState: nil,
        gattSessionId: String(gattSessionId.value),
        characteristicId: String(characteristicId.value),
        subscriptionId: String(subscriptionId.value),
        gattServices: nil,
        value: nil,
        notificationState: notificationState,
        actualNotificationMode: actualNotificationMode,
        rssi: nil,
        localName: nil,
        advertisedServices: nil,
        adapterState: nil,
        scanAuthorization: nil,
        error: nil
      )

    case let .notificationReceived(connectionId, gattSessionId, subscriptionId, characteristicId, value):
      return RfScanEvent(
        kind: "notificationReceived",
        commandId: nil,
        scanSessionId: nil,
        peripheralId: nil,
        connectionId: String(connectionId.value),
        connectionState: nil,
        gattSessionId: String(gattSessionId.value),
        characteristicId: String(characteristicId.value),
        subscriptionId: String(subscriptionId.value),
        gattServices: nil,
        value: Self.arrayBufferCopy(value),
        notificationState: nil,
        actualNotificationMode: nil,
        rssi: nil,
        localName: nil,
        advertisedServices: nil,
        adapterState: nil,
        scanAuthorization: nil,
        error: nil
      )

    case let .commandCompleted(commandId, error):
      return RfScanEvent(
        kind: "commandCompleted",
        commandId: String(commandId.value),
        scanSessionId: nil,
        peripheralId: nil,
        connectionId: nil,
        connectionState: nil,
        gattSessionId: nil,
        characteristicId: nil,
        subscriptionId: nil,
        gattServices: nil,
        value: nil,
        notificationState: nil,
        actualNotificationMode: nil,
        rssi: nil,
        localName: nil,
        advertisedServices: nil,
        adapterState: nil,
        scanAuthorization: nil,
        error: error?.toNitroError()
      )

    case let .backendError(error):
      return RfScanEvent(
        kind: "backendError",
        commandId: nil,
        scanSessionId: nil,
        peripheralId: nil,
        connectionId: nil,
        connectionState: nil,
        gattSessionId: nil,
        characteristicId: nil,
        subscriptionId: nil,
        gattServices: nil,
        value: nil,
        notificationState: nil,
        actualNotificationMode: nil,
        rssi: nil,
        localName: nil,
        advertisedServices: nil,
        adapterState: nil,
        scanAuthorization: nil,
        error: error.toNitroError()
      )

    default:
      return nil
    }
  }

  private static func arrayBufferCopy(_ data: Data) -> ArrayBuffer {
    let buffer = ArrayBuffer.allocate(size: data.count)
    data.withUnsafeBytes { rawBuffer in
      guard data.count > 0, let baseAddress = rawBuffer.baseAddress else { return }
      memcpy(buffer.data, baseAddress, data.count)
    }
    return buffer
  }

  private static func adapterStateText(_ state: AdapterState) -> String {
    switch state {
    case .unknown: "Unknown"
    case .unavailable: "Unavailable"
    case .poweredOff: "PoweredOff"
    case .poweredOn: "PoweredOn"
    }
  }

  private static func authorizationText(_ authorization: AuthorizationState) -> String {
    switch authorization {
    case .notRequired: "NotRequired"
    case .notDetermined: "NotDetermined"
    case .denied: "Denied"
    case .restricted: "Restricted"
    case .authorized: "Authorized"
    }
  }

  private static func parseCanonicalU64(_ value: String) -> UInt64? {
    guard !value.isEmpty, value == "0" || !value.hasPrefix("0") else { return nil }
    guard value.allSatisfy({ $0.isNumber && $0.isASCII }) else { return nil }
    guard let parsed = UInt64(value) else { return nil }
    return String(parsed) == value ? parsed : nil
  }
}

private enum ProbeError: Error {
  case closed
  case notInitialized
}


private extension Dictionary where Key == String, Value == Int {
  func toRfResourceSnapshot() -> RfResourceSnapshot {
    precondition(count == 17 && values.allSatisfy { $0 >= 0 && $0 <= 9_007_199_254_740_991 })
    // Int counts are integral logical roots, exactly representable as JS numbers.
    return RfResourceSnapshot(
      activeScans: Double(self["activeScans"]!),
      connections: Double(self["connections"]!),
      gattAttributes: Double(self["gattAttributes"]!),
      subscriptions: Double(self["subscriptions"]!),
      pendingIo: Double(self["pendingIo"]!),
      watchdogs: Double(self["watchdogs"]!),
      callbackRoutes: Double(self["callbackRoutes"]!),
      ingressRoots: Double(self["ingressRoots"]!),
      adapterDelegates: Double(self["adapterDelegates"]!),
      peerCacheEntries: Double(self["peerCacheEntries"]!),
      backendLeases: Double(self["backendLeases"]!),
      hubRoots: Double(self["hubRoots"]!),
      handoffRoots: Double(self["handoffRoots"]!),
      handoffPending: Double(self["handoffPending"]!),
      listeners: Double(self["listeners"]!),
      workerThreads: Double(self["workerThreads"]!),
      lifecycleOwners: Double(self["lifecycleOwners"]!)
    )
  }
}


// Test-layer ownership only. No executor, routing registry or BLE state is added to Hub.
// This SPI lives in this file because the pod deliberately compiles only the two authored sources.
final class PendingReloadOwner {
  let id: String
  let generation: Int
  unowned let session: PendingReloadSession
  var probe: HybridBleHubProbe?
  var registered = false
  var invalidating = false
  var ready = false
  var awaited = false
  var closed = false
  var closeStartedByEngine = false
  var nonce: String?
  var awaitingRoute: String?
  var armed = false
  var dispatched = false
  fileprivate var read: PendingReadDiagnostic?

  init(id: String, generation: Int, session: PendingReloadSession) {
    self.id = id
    self.generation = generation
    self.session = session
  }

  var membershipCount: Int { session.lock.withLock { registered ? 1 : 0 } }
  var canInitialize: Bool { session.lock.withLock { ready && !invalidating && !closed && session.failure == nil } }

  fileprivate func acceptRead(_ read: PendingReadDiagnostic) {
    session.lock.withLock {
      guard generation == 1, ready, armed, !invalidating, self.read == nil,
            awaitingRoute == PendingReloadSession.route(read) else {
        session.fail("read-acceptance-outside-armed-runtime")
        return
      }
      self.read = read
    }
  }

  fileprivate func beginClose(engineInvalidation: Bool, read: PendingReadDiagnostic?) {
    session.lock.withLock {
      if generation == 1 {
        guard session.failure == nil, engineInvalidation, invalidating, session.reloadRequested, let read,
              self.read != nil, dispatched else {
          session.fail("first-close-not-authorized-engine-invalidation")
          return
        }
        closeStartedByEngine = true
        HybridBleHubProbe.postNativeBoundaryDiagnostic(event: "engine-invalidation-close-started",
          diagnostic: NativeBoundaryDiagnostic(runId: session.runId, probeId: probe!.pendingProbeId, read: read))
      }
    }
  }
}

final class PendingReloadSession {
  let lock = NSRecursiveLock()
  let runId: String
  let notificationName: String
  let bootId: String
  let pid = ProcessInfo.processInfo.processIdentifier
  private let emit: (String) -> Void
  private var owners: [PendingReloadOwner] = []
  private(set) var failure: Error?
  fileprivate var reloadRequested = false
  private var waiting: [(PendingReloadOwner, (Result<Void, Error>) -> Void)] = []

  init(runId: String, notificationName: String, bootId: String, emit: @escaping (String) -> Void = PendingReloadSession.write) {
    self.runId = runId
    self.notificationName = notificationName
    self.bootId = bootId
    self.emit = emit
  }

  static func error(_ detail: String) -> NSError {
    NSError(domain: "BleHubPendingReload", code: 1, userInfo: [NSLocalizedDescriptionKey: detail])
  }

  static func write(_ line: String) {
    FileHandle.standardError.write(Data((line + "\n").utf8))
  }

  private func record(_ event: String, owner: PendingReloadOwner? = nil, detail: String? = nil) {
    var fields: [String: Any] = ["event": event, "runId": runId, "pid": pid, "bootId": bootId]
    if let owner { fields["generation"] = owner.generation; fields["ownerId"] = owner.id }
    if let detail { fields["detail"] = detail }
    if let data = try? JSONSerialization.data(withJSONObject: fields, options: [.sortedKeys]),
       let json = String(data: data, encoding: .utf8) {
      emit("BLEHUB_NITRO_RUNTIME rf-pending-reload-host:" + json)
    }
  }

  func fail(_ reason: String) {
    lock.withLock {
      guard failure == nil else { return }
      failure = Self.error(reason)
      record("failed", detail: reason)
      let callbacks = waiting.map { $0.1 }
      waiting.removeAll()
      for callback in callbacks { settle(callback, .failure(failure!)) }
    }
  }

  private func settle(_ callback: @escaping (Result<Void, Error>) -> Void, _ result: Result<Void, Error>) {
    DispatchQueue.global(qos: .utility).async { callback(result) }
  }

  func install(_ id: String) -> PendingReloadOwner? {
    lock.withLock {
      guard failure == nil, ProcessInfo.processInfo.processIdentifier == pid,
            UUID(uuidString: id)?.uuidString.lowercased() == id,
            owners.count < 2, !owners.contains(where: { $0.id == id }),
            owners.isEmpty || reloadRequested else {
        fail("unexpected-runtime-installation"); return nil
      }
      let owner = PendingReloadOwner(id: id, generation: owners.count + 1, session: self)
      owners.append(owner)
      record("runtime-installed", owner: owner)
      return owner
    }
  }

  func owner(_ id: String) -> PendingReloadOwner? { lock.withLock { owners.first { $0.id == id } } }

  func bind(_ probe: HybridBleHubProbe, owner: PendingReloadOwner) -> Bool {
    let reserved = lock.withLock { () -> Bool in
      guard failure == nil, owners.contains(where: { $0 === owner }), owner.probe == nil,
            !owner.invalidating, !owners.contains(where: { $0.probe === probe }) else {
        fail("foreign-or-duplicate-probe-binding"); return false
      }
      owner.probe = probe
      owner.registered = true
      return true
    }
    guard reserved else { return false }
    guard probe.bindPendingOwner(owner) else { fail("probe-already-initialized-or-bound"); return false }
    return true
  }

  func awaitReady(_ owner: PendingReloadOwner, runId: String, generation: Double,
                  completion: @escaping (Result<Void, Error>) -> Void) {
    lock.withLock {
      guard failure == nil, runId == self.runId, generation == Double(owner.generation),
            owner.registered, !owner.awaited, !owner.invalidating else {
        fail("invalid-native-readiness-request")
        settle(completion, .failure(failure!)); return
      }
      owner.awaited = true
      if owner.generation == 1 || owners[0].closed {
        owner.ready = true
        record("native-ready", owner: owner)
        settle(completion, .success(()))
      } else {
        waiting.append((owner, completion))
      }
    }
  }

  // Invoked ONLY by the RCTInvalidating module. The module never closes on reload request.
  func invalidate(_ owner: PendingReloadOwner) {
    let probe = lock.withLock { () -> HybridBleHubProbe? in
      guard !owner.invalidating else { fail("duplicate-module-invalidation"); return nil }
      owner.invalidating = true
      record("module-invalidation-entered", owner: owner)
      guard let probe = owner.probe else { fail("invalidated-runtime-had-no-bound-probe"); return nil }
      return probe
    }
    probe?.closeForEngineInvalidation()
  }

  fileprivate func completeClose(_ owner: PendingReloadOwner, resources: [String: Int], syntheticClosed: Bool) {
    lock.withLock {
      owner.registered = false
      guard resources.count == 17, resources["lifecycleOwners"] == 1, resources.allSatisfy({ $0.key == "lifecycleOwners" || $0.value == 0 }) else {
        fail("native-close-resources-not-zero"); return
      }
      guard failure == nil else { return }
      if owner.generation == 1 {
        guard owner.closeStartedByEngine, owner.invalidating, syntheticClosed, let read = owner.read,
              let probe = owner.probe else { fail("incomplete-engine-close"); return }
        HybridBleHubProbe.postNativeBoundaryDiagnostic(event: "engine-close-complete",
          diagnostic: NativeBoundaryDiagnostic(runId: runId, probeId: probe.pendingProbeId, read: read))
      }
      owner.closed = true
      owner.probe = nil
      record("native-close-complete", owner: owner)
      let callbacks = waiting
      waiting.removeAll()
      for (next, callback) in callbacks {
        guard next.registered, !next.invalidating else {
          fail("new-owner-retired-before-readiness"); settle(callback, .failure(failure!)); continue
        }
        next.ready = true
        record("native-ready", owner: next)
        settle(callback, .success(()))
      }
    }
  }

  fileprivate static func route(_ read: PendingReadDiagnostic) -> String {
    "commandId=\(read.commandId):connectionId=\(read.connectionId):gattSessionId=\(read.gattSessionId):characteristicId=\(read.characteristicId)"
  }

  func observe(_ marker: String) {
    let prefix = "BLEHUB_NITRO_RUNTIME rf-pending-read-reload:"
    guard marker.hasPrefix(prefix) else { return }
    let body = String(marker.dropFirst(prefix.count))
    lock.withLock {
      guard failure == nil else { return }
      if body == "passed:runId=\(runId)" {
        guard owners.count == 2, owners[1].closed else { fail("success-before-native-close"); return }
        record("javascript-complete", owner: owners[1])
        return
      }
      guard let owner = owners.last, body.hasPrefix("generation=\(owner.generation):") else {
        // A teardown cleanup marker can still come from the old engine.
        if reloadRequested && body == "generation=1:cleanup-started" { return }
        fail("foreign-generation-marker"); return
      }
      let detail = String(body.dropFirst("generation=\(owner.generation):".count))
      if detail.contains("failed:") { fail("javascript-failed"); return }
      if detail.hasPrefix("runtime-start:") {
        let start = "runtime-start:runId=\(runId):nonce="
        guard detail.hasPrefix(start), owner.nonce == nil else { fail("invalid-runtime-nonce"); return }
        let nonce = String(detail.dropFirst(start.count))
        guard !nonce.isEmpty, !nonce.contains(":"), !nonce.contains(where: { $0.isWhitespace }),
              !owners.contains(where: { $0.nonce == nonce }) else { fail("reused-runtime-nonce"); return }
        owner.nonce = nonce
      } else if detail.hasPrefix("awaiting-arm:") {
        let base = owner.generation == 1 ? 12000 : 13000
        let routePrefix = "commandId=\(base + 11):connectionId=\(base + 4):gattSessionId=\(base + 8):characteristicId="
        let route = String(detail.dropFirst("awaiting-arm:".count))
        guard owner.ready, owner.nonce != nil, owner.awaitingRoute == nil, route.hasPrefix(routePrefix),
              let attribute = UInt64(route.dropFirst(routePrefix.count)), attribute > 0,
              String(attribute) == String(route.dropFirst(routePrefix.count)) else { fail("invalid-arm-readiness"); return }
        owner.awaitingRoute = route
      } else if detail.hasPrefix("read-dispatched:") {
        guard owner.armed, !owner.dispatched, let route = owner.awaitingRoute else { fail("invalid-read-dispatch"); return }
        if owner.generation == 1 {
          guard let read = owner.read, route == Self.route(read),
                detail.hasPrefix("read-dispatched:" + route + ":peripheralId="),
                let peripheral = UInt64(detail.dropFirst(("read-dispatched:" + route + ":peripheralId=").count)),
                String(peripheral) == String(detail.dropFirst(("read-dispatched:" + route + ":peripheralId=").count)) else {
            fail("unmatched-native-read-dispatch"); return
          }
        } else if detail != "read-dispatched:" + route { fail("invalid-new-read-route"); return }
        owner.dispatched = true
      }
    }
  }

  // The app delegate calls this before recovery/default Linking handling.
  func acceptControl(_ url: String) -> String? {
    lock.withLock {
      guard failure == nil, ProcessInfo.processInfo.processIdentifier == pid, let owner = owners.last else {
        fail("control-without-runtime"); return nil
      }
      if url == "blehub-rn-spike://arm/\(runId)" {
        guard owner.ready, owner.nonce != nil, owner.awaitingRoute != nil, !owner.armed,
              !owner.invalidating, owner.generation == 1 || owners[0].closed else {
          fail("early-or-duplicate-arm-url"); return nil
        }
        owner.armed = true
        record("arm-url-accepted", owner: owner, detail: url)
        return "arm"
      }
      if url == "blehub-rn-spike://reload/\(runId)" {
        guard owner.generation == 1, owner.dispatched, owner.read != nil, !reloadRequested,
              !owner.invalidating, !owner.closed else { fail("early-or-duplicate-reload-url"); return nil }
        reloadRequested = true
        record("reload-url-accepted", owner: owner, detail: url)
        return "reload"
      }
      fail("foreign-or-malformed-control-url")
      return nil
    }
  }
}

/// Authored test SPI. ObjC++ passes the ACTUAL generated Swift wrapper, never a JS probe ID.
@objc(BleHubPendingReloadBridge)
public final class BleHubPendingReloadBridge: NSObject {
  private static let configurationLock = NSLock()
  private static var session: PendingReloadSession?

#if DEBUG
  // Reachable only by the native XCTest harness, never by a Nitro/JS factory.
  @objc public static func beginRuntimeBindingTest() -> Bool {
    guard NSClassFromString("XCTestCase") != nil, !enabled else { return false }
    return configure(runId: "abcdefab-1234-4234-8234-123456789abc",
      notificationName: "dev.blehub.rn.reload.abcdefab-1234-4234-8234-123456789abc", bootId: "native-unit-test")
  }

  @objc public static func endRuntimeBindingTest(_ ownerId: String) -> NSDictionary {
    guard NSClassFromString("XCTestCase") != nil, let current = session,
          current.bootId == "native-unit-test", let owner = current.owner(ownerId) else { return [:] }
    let invalidated = current.lock.withLock { owner.invalidating }
    let hasProbe = current.lock.withLock { owner.probe != nil }
    if let probe = current.lock.withLock({ owner.probe }) {
      let done = DispatchSemaphore(value: 0)
      _ = try? probe.close().then { _ in done.signal() }
      guard done.wait(timeout: .now() + 5) == .success else { return ["timeout": true] }
    }
    let result: NSDictionary = ["invalidated": invalidated, "hadBoundProbe": hasProbe,
      "failedWithoutAcceptedRead": current.failure != nil, "membership": owner.membershipCount]
    configurationLock.withLock { session = nil }
    return result
  }
#endif

  @objc public static var enabled: Bool { configurationLock.withLock { session != nil } }

  @objc public static func configure(runId: String, notificationName: String, bootId: String) -> Bool {
    configurationLock.withLock {
      guard session == nil, UUID(uuidString: runId)?.uuidString.lowercased() == runId,
            notificationName == "dev.blehub.rn.reload." + runId else { return false }
      session = PendingReloadSession(runId: runId, notificationName: notificationName, bootId: bootId)
      return true
    }
  }

  @objc public static func installOwner(_ id: String) -> NSDictionary? {
    guard let session, let owner = session.install(id) else { return nil }
    return ["runtimeMode": "rf-pending-read-reload", "rfPendingReadRunId": session.runId,
            "rfPendingReadGeneration": owner.generation, "rfReloadNotificationName": session.notificationName,
            "rfPendingReadOwnerId": id]
  }

  @objc public static func bindProbe(_ pointer: UnsafeMutableRawPointer, ownerId: String) -> Bool {
    let wrapper = HybridBleHubProbeSpec_cxx.fromUnsafe(pointer)
    guard let probe = wrapper.getHybridBleHubProbeSpec() as? HybridBleHubProbe,
          let session, let owner = session.owner(ownerId) else { return false }
    return session.bind(probe, owner: owner)
  }

  @objc public static func invalidateOwner(_ id: String) {
    guard let session, let owner = session.owner(id) else { return }
    session.invalidate(owner)
  }

  @objc public static func observe(_ marker: String) { session?.observe(marker) }
  @objc public static func acceptControl(_ url: String) -> String? { session?.acceptControl(url) }
  @objc public static func fail(_ reason: String) { session?.fail(reason) }
}
