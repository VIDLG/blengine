import Foundation
import NitroModules
import BleHubApple
import BleHubUniFfi


private final class NitroTestSink: BleEventSink, @unchecked Sendable {
  private let handoff: BoundedEventHandoff<BleEvent>

  init(
    stateQueue: DispatchQueue,
    receive: @escaping (BleEvent) -> Void
  ) {
    handoff = BoundedEventHandoff(
      stateQueue: stateQueue,
      capacity: 256,
      deliver: receive
    )
  }

  func onEvent(event: BleEvent) throws {
    guard handoff.submit(event) else {
      throw EventSinkError.Rejected(
        detail: "Nitro event handoff is closed or at capacity"
      )
    }
  }

  func close() {
    handoff.close()
  }
}

private struct PendingReadDiagnostic {
  let commandId: String
  let connectionId: String
  let gattSessionId: String
  let characteristicId: String
  let command: CommandId
  let connection: ConnectionId
  let gattSession: GattSessionId
  let characteristic: GattCharacteristicId
}

private struct NativeBoundaryDiagnostic {
  let runId: String
  let probeId: String
  let read: PendingReadDiagnostic
}

private struct CloseTransition {
  let promise: Promise<Void>
  let currentHub: BleHub?
  let currentSink: NitroTestSink?
  let readDiagnostic: PendingReadDiagnostic?
  let started: Bool
  let disposeForTesting: (() throws -> Void)?
}

private enum NativeBoundaryDiagnosticKey {
  static let event: String = "event"
  static let runId: String = "runId"
  static let probeId: String = "probeId"
  static let commandId: String = "commandId"
  static let connectionId: String = "connectionId"
  static let gattSessionId: String = "gattSessionId"
  static let characteristicId: String = "characteristicId"
  static let error: String = "error"
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
  private static let pendingReadModeArgument = "--blehub-nitro-rf-pending-read-reload-service"
  private static let pendingReadNotificationArgument = "--blehub-nitro-rf-pending-read-reload-notification"
  private static let pendingReadNotificationPrefix = "dev.blehub.rn.reload."
  private static let nativeBoundaryNotification = Notification.Name(
    "dev.blehub.rn.pendingReadNativeBoundary"
  )

  private let queue = DispatchQueue(label: "dev.blehub.react-native.state")
  private let probeId: String
  private let pendingReadRunId: String?
  private var completionListener: ((CompletionEvent) -> Void)?
  private var rfScanListener: ((RfScanEvent) -> Void)?
  private var isClosed = false
  private var hasEmittedPendingReadAccepted = false
  private var pendingReadDiagnostic: PendingReadDiagnostic?
  private var hub: BleHub?
  private var backend: AppleBleBackend?
  private var sink: NitroTestSink?
  private var closePromise: Promise<Void>?
  private var disposeForTesting: (() throws -> Void)?

  override init() {
    probeId = UUID().uuidString.lowercased()
    pendingReadRunId = Self.pendingReadRunId(from: ProcessInfo.processInfo.arguments)
    disposeForTesting = nil
    super.init()
  }

  private init(disposeForTesting: @escaping () throws -> Void) {
    probeId = UUID().uuidString.lowercased()
    pendingReadRunId = nil
    self.disposeForTesting = disposeForTesting
    super.init()
  }

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
        if let hub { return hub }

        uniffiEnsureBlehubInitialized()
        let backend = try AppleBleBackend(
          queueLabel: "dev.blehub.react-native.apple"
        )
        let sink = NitroTestSink(stateQueue: queue) { [weak self] event in
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
        if commandValue == 12008,
           !hasEmittedPendingReadAccepted,
           let runId = pendingReadRunId
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

  func close() throws -> Promise<Void> {
    let transition = queue.sync {
      () -> CloseTransition in
      if let closePromise {
        // The first caller owns the cleanup job. Every later caller returns
        // the exact same Promise and must not start a second resolver.
        return CloseTransition(
          promise: closePromise,
          currentHub: nil,
          currentSink: nil,
          readDiagnostic: nil,
          started: false,
          disposeForTesting: nil
        )
      }

      let promise = Promise<Void>()
      closePromise = promise
      isClosed = true
      // Invalidate all listener routes before starting Hub cleanup. Events
      // already handed to Nitro may still drain after this Promise resolves.
      completionListener = nil
      rfScanListener = nil
      let currentHub = hub
      let currentSink = sink
      let readDiagnostic = pendingReadDiagnostic
      let disposeForTesting = self.disposeForTesting
      hub = nil
      backend = nil
      sink = nil
      self.disposeForTesting = nil
      currentSink?.close()
      return CloseTransition(
        promise: promise,
        currentHub: currentHub,
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
        promise.resolve()
      }
      return promise
    }

    let diagnostic = transition.readDiagnostic.flatMap { read in
      pendingReadRunId.map { runId in
        NativeBoundaryDiagnostic(runId: runId, probeId: probeId, read: read)
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
        if let diagnostic {
          Self.postNativeBoundaryDiagnostic(
            event: "dispose-returned",
            diagnostic: diagnostic
          )
          Self.submitLateDiagnosticRead(
            to: currentSink,
            diagnostic: diagnostic
          )
        }
        promise.resolve()
      } catch {
        if let diagnostic {
          Self.postNativeBoundaryDiagnostic(
            event: "dispose-failed",
            diagnostic: diagnostic,
            error: String(reflecting: error)
          )
          Self.submitLateDiagnosticRead(
            to: currentSink,
            diagnostic: diagnostic
          )
        }
        promise.reject(withError: error)
      }
    }
    return promise
  }

  /// Called only on `queue` by the native handoff. Do not add queue.sync here:
  /// the old implementation deadlocked by synchronously re-entering this queue.
  private func receive(event: BleEvent) {
    guard !isClosed else { return }

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
    listener(projected)
  }

  private static func postNativeBoundaryDiagnostic(
    event: String,
    diagnostic: NativeBoundaryDiagnostic,
    error: String? = nil
  ) {
    var userInfo: [String: Any] = [
      NativeBoundaryDiagnosticKey.event: event,
      NativeBoundaryDiagnosticKey.runId: diagnostic.runId,
      NativeBoundaryDiagnosticKey.probeId: diagnostic.probeId,
      NativeBoundaryDiagnosticKey.commandId: diagnostic.read.commandId,
      NativeBoundaryDiagnosticKey.connectionId: diagnostic.read.connectionId,
      NativeBoundaryDiagnosticKey.gattSessionId: diagnostic.read.gattSessionId,
      NativeBoundaryDiagnosticKey.characteristicId: diagnostic.read.characteristicId
    ]
    if let error {
      userInfo[NativeBoundaryDiagnosticKey.error] = error
    }
    NotificationCenter.default.post(
      name: nativeBoundaryNotification,
      object: nil,
      userInfo: userInfo
    )
  }

  private static func submitLateDiagnosticRead(
    to sink: NitroTestSink?,
    diagnostic: NativeBoundaryDiagnostic
  ) {
    guard let sink else { return }
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
      guard case .Rejected = error else { return }
      postNativeBoundaryDiagnostic(
        event: "late-submit-rejected",
        diagnostic: diagnostic
      )
    } catch {
      return
    }
  }

  private static func pendingReadRunId(from arguments: [String]) -> String? {
    guard arguments.contains(pendingReadModeArgument),
          let notificationIndex = arguments.firstIndex(of: pendingReadNotificationArgument),
          arguments.index(after: notificationIndex) < arguments.endIndex else {
      return nil
    }
    let notificationName = arguments[arguments.index(after: notificationIndex)]
    guard notificationName.hasPrefix(pendingReadNotificationPrefix) else { return nil }
    let uuidText = String(notificationName.dropFirst(pendingReadNotificationPrefix.count))
    guard let uuid = UUID(uuidString: uuidText) else { return nil }
    return uuid.uuidString.lowercased()
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
