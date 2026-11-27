#if canImport(BleHubApple)
@_spi(AppleDiagnostics) import BleHubApple
#endif
import BleHubUniFfi
import Foundation

/// Drains one trusted native reader; an observation timeout is not end-of-stream.
func drainBlengEvents(
  nextEvent: @Sendable () async throws -> BleEvent?,
  onEvent: @Sendable (BleEvent) throws -> Void
) async throws {
  while true {
    try Task.checkCancellation()
    let event: BleEvent?
    do {
      event = try await nextEvent()
    } catch BlengError.Timeout {
      continue
    }
    guard let event else { return }
    // Delivery failures, including typed Timeout, must not be treated as an idle read.
    try onEvent(event)
  }
}

private actor RecoveryScanLease {
  private var scanId: ScanSessionId?

  func set(_ scanId: ScanSessionId) {
    self.scanId = scanId
  }

  func clear() {
    scanId = nil
  }

  func stop(using owner: Bleng) async {
    guard let scanId else { return }
    _ = try? await owner.beginStopScan(session: scanId).wait(timeoutMs: 5_000)
    self.scanId = nil
  }
}

/// Backend plus native diagnostics retained by one Rust-owned Apple session.
///
/// HIL may wrap `backend` with a witness while keeping the production native backend as the
/// diagnostic owner. Production construction uses the default lease and does not inject a wrapper.
struct ApplePlatformBackendLease {
  let backend: any BleBackend
  let resourceSnapshot: @Sendable () async -> [String: Int]
  let recreateCentral: @Sendable () async -> [String: String]
}

/// Migration-only Apple adapter for the Rust supervisor FFI contract.
///
/// The backend remains the owner of CoreBluetooth queues, callbacks, native
/// retirement, and resource accounting. This adapter only supplies the
/// supervisor-facing session lifetime and drains the one public Bleng event
/// stream through the Rust-owned scan projection and host-owned recovery projection.
final class ApplePlatformSession: ForeignPlatformOwnerSession, @unchecked Sendable {
  private let sessionContext: ForeignOwnerContext
  private let backendObject: any BleBackend
  private let snapshotResources: @Sendable () async -> [String: Int]
  private let recreateCentral: @Sendable () async -> [String: String]
  private let eventReaderLock = NSLock()
  private let eventSink: (@Sendable (BleEvent) -> Void)?
  private let scanEventSink: (@Sendable (ForeignScanEventEnvelope) -> Void)?
  private let recoveryEvents: AppleRecoveryEventInbox
  private var owner: Bleng?
  private var eventReader: Task<Result<Void, Error>, Never>?

  static let requiredZeroResourceNames = [
    "activeScans",
    "connections",
    "gattAttributes",
    "subscriptions",
    "pendingIo",
    "watchdogs",
    "callbackRoutes",
    "ingressRoots",
    "peerCacheEntries",
    "adapterDelegates",
    "backendLeases",
  ]

  /// Stable across launches so iOS can hand the central (and its connections) back to a fresh
  /// process. Restoration still needs the App's `bluetooth-central` background mode; the backend
  /// keeps the restored peers native until the host explicitly adopts an inventory key.
  static let defaultRestoreIdentifier = "dev.blehub.react-native-bleng.central"

  init(
    context: ForeignOwnerContext,
    lease: ApplePlatformBackendLease,
    eventSink: (@Sendable (BleEvent) -> Void)? = nil,
    scanEventSink: (@Sendable (ForeignScanEventEnvelope) -> Void)? = nil
  ) {
    self.sessionContext = context
    self.backendObject = lease.backend
    self.snapshotResources = lease.resourceSnapshot
    self.recreateCentral = lease.recreateCentral
    self.eventSink = eventSink
    self.scanEventSink = scanEventSink
    self.recoveryEvents = AppleRecoveryEventInbox()
  }

  func context() -> ForeignOwnerContext {
    sessionContext
  }

  func backend() -> any BleBackend {
    backendObject
  }

  /// HIL-only access to the same owner assembled by Rust Supervisor.
  /// Production Nitro callers never receive this handle.
  func activeOwnerForHost() throws -> Bleng {
    guard let owner = withEventReaderLock({ self.owner }) else {
      throw SupervisorFfiError.Platform(detail: "Apple BLeng owner is not active")
    }
    return owner
  }

  func disposeUnassembled() throws {
    do {
      try backendObject.dispose()
    } catch {
      throw SupervisorFfiError.Platform(detail: String(describing: error))
    }
  }

  func startEventReader(owner: Bleng, eventProjection: SessionEventProjection) throws {
    eventReaderLock.lock()
    defer { eventReaderLock.unlock() }
    guard eventReader == nil else {
      throw SupervisorFfiError.Platform(detail: "Apple event reader already started")
    }

    self.owner = owner
    let recoveryEvents = self.recoveryEvents
    eventReader = Task { [owner, eventProjection, recoveryEvents] in
      defer { recoveryEvents.close() }
      do {
        try await drainBlengEvents(nextEvent: {
          try await owner.nextEvent(timeoutMs: 300_000)
        }, onEvent: { event in
          if let envelope = eventProjection.project(event: event) {
            // Delivery is direct. A sink that queues independently must check the full source
            // context; retirement cannot retract envelopes already returned or delivered.
            scanEventSink?(envelope)
          }
          eventSink?(event)
          if let projection = projectRecoveryEvent(event) {
            try recoveryEvents.offer(projection)
          }
        })
        return .success(())
      } catch {
        return .failure(error)
      }
    }
  }

  func waitEventReader() async throws {
    let reader = withEventReaderLock {
      let reader = eventReader
      eventReader = nil
      return reader
    }
    guard let reader else {
      return
    }
    switch await reader.value {
    case .success:
      return
    case .failure(let error):
      throw SupervisorFfiError.Platform(detail: "Apple event reader failed: \(error)")
    }
  }

  func executeRecoveryAction(
    sequence: UInt64,
    action: ForeignPlatformRecoveryAction
  ) async throws -> ForeignPlatformActionResult {
    switch action {
    case .waitForRadioOn:
      return try await waitForRadioOn(sequence: sequence)
    case let .awaitOldOperationTerminals(remaining):
      let commandId = try await recoveryEvents.nextTerminal(matching: remaining)
      return ForeignPlatformActionResult(
        sequence: sequence,
        signal: .oldOperationTerminal(operationId: String(commandId))
      )
    case let .attemptSameOwnerRecovery(_, target):
      do {
        return try await recoverTarget(sequence: sequence, target: target, freshOwner: false)
      } catch is CancellationError {
        throw CancellationError()
      } catch {
        return ForeignPlatformActionResult(
          sequence: sequence,
          signal: .sameOwnerRecoveryFailed(reason: String(describing: error))
        )
      }
    case let .attemptFreshOwnerRecovery(target):
      do {
        return try await recoverTarget(sequence: sequence, target: target, freshOwner: true)
      } catch is CancellationError {
        throw CancellationError()
      } catch {
        return ForeignPlatformActionResult(
          sequence: sequence,
          signal: .freshOwnerRecoveryFailed(reason: String(describing: error))
        )
      }
    case .recreateCentral:
      let diagnostic = await recreateCentral()
      if diagnostic["recreated"] == "true" {
        return ForeignPlatformActionResult(
          sequence: sequence,
          signal: .centralRecreated
        )
      }

      return ForeignPlatformActionResult(
        sequence: sequence,
        signal: .centralRecreationFailed(
          reason: diagnostic["reason"] ?? "Apple Central recreation was rejected"
        )
      )
    default:
      throw SupervisorFfiError.Platform(
        detail: "Apple recovery action is not implemented: \(action)"
      )
    }
  }

  private func recoverTarget(
    sequence: UInt64,
    target: ForeignRecoveryTarget,
    freshOwner: Bool
  ) async throws -> ForeignPlatformActionResult {
    // Same guarded access as `activeOwnerForHost`: a missing owner is a typed failure, not a trap.
    guard let owner = withEventReaderLock({ self.owner }) else {
      throw SupervisorFfiError.Platform(detail: "Apple recovery requires an active owner")
    }

    let scanLease = RecoveryScanLease()
    do {
      return try await withThrowingTaskGroup(of: ForeignPlatformActionResult.self) { group in
        group.addTask {
          // Subscribe before submission: advertisement callbacks can precede start.wait().
          // This capture belongs only to this attempt, not to the owner's ordinary scan stream.
          let advertisements = try self.recoveryEvents.captureAdvertisements()
          defer { advertisements.close() }
          let scanId = try await owner.beginStartScan(options: target.scanOptions)
            .wait(timeoutMs: 30_000)
          await scanLease.set(scanId)
          let advertisement = try await advertisements.nextAdvertisement(
            scanSessionId: scanId, matching: target.scanOptions
          )
          _ = try await owner.beginStopScan(session: scanId).wait(timeoutMs: 30_000)
          await scanLease.clear()
          let connectionId = try await owner.beginConnect(peripheral: advertisement.peripheralId)
            .wait(timeoutMs: 30_000)
          let database = try await owner.beginDiscoverGatt(connection: connectionId)
            .wait(timeoutMs: 30_000)
          guard let service = database.services.first(where: {
            $0.uuid.value.caseInsensitiveCompare(target.serviceUuid.value) == .orderedSame
          }), let characteristic = service.characteristics.first(where: {
            $0.uuid.value.caseInsensitiveCompare(target.characteristicUuid.value) == .orderedSame
          }) else {
            throw SupervisorFfiError.Platform(detail: "Apple recovery GATT target was not found")
          }
          let receipt = try await owner.beginEnableNotifications(
            connection: connectionId,
            session: database.gattSessionId,
            characteristic: characteristic.id,
            mode: target.notificationMode
          ).wait(timeoutMs: 30_000)
          let subscriptionId = receipt.subscription
          let value = try await owner.beginReadCharacteristic(
            connection: connectionId,
            session: database.gattSessionId,
            characteristic: characteristic.id
          ).wait(timeoutMs: 30_000)
          guard value == target.expectedValue else {
            throw SupervisorFfiError.Platform(
              detail: "Apple recovery value did not match the expected target value"
            )
          }
          let link = ForeignRecoveryLink(
            connectionId: String(connectionId.value),
            gattSessionId: String(database.gattSessionId.value),
            subscriptionId: String(subscriptionId.value)
          )
          return ForeignPlatformActionResult(
            sequence: sequence,
            signal: freshOwner
              ? .freshOwnerRecovered(link: link)
              : .sameOwnerRecovered(link: link)
          )
        }
        group.addTask {
          try await Task.sleep(nanoseconds: 60_000_000_000)
          throw SupervisorFfiError.Platform(
            detail: "Apple \(freshOwner ? "fresh-owner" : "same-owner") recovery timed out"
          )
        }
        defer { group.cancelAll() }
        return try await group.next()!
      }
    } catch is CancellationError {
      await scanLease.stop(using: owner)
      throw CancellationError()
    } catch let error as SupervisorFfiError {
      await scanLease.stop(using: owner)
      throw error
    } catch {
      await scanLease.stop(using: owner)
      throw SupervisorFfiError.Platform(
        detail: "Apple \(freshOwner ? "fresh-owner" : "same-owner") recovery failed: \(error)"
      )
    }
  }

  private func waitForRadioOn(sequence: UInt64) async throws -> ForeignPlatformActionResult {
    try await recoveryEvents.waitForRadioOn(sequence: sequence)
  }


  private func withEventReaderLock<Value>(_ body: () -> Value) -> Value {
    eventReaderLock.lock()
    defer { eventReaderLock.unlock() }
    return body()
  }

  func resourceSnapshot() async throws -> [ForeignResourceCount] {
    let snapshot = await snapshotResources()
    let counts = try snapshot.map { name, count in
      guard count >= 0 else {
        throw SupervisorFfiError.Platform(
          detail: "Apple resource count is negative for \(name): \(count)"
        )
      }
      return ForeignResourceCount(name: name, count: UInt64(count))
    }
    return counts.sorted { $0.name < $1.name }
  }
}

/// Migration-only Apple implementation of the foreign platform factory.
final class ApplePlatformSessionFactory: ForeignPlatformOwnerSessionFactory, @unchecked Sendable {
  private let queueLabel: String
  private let eventSink: (@Sendable (BleEvent) -> Void)?
  private let scanEventSink: (@Sendable (ForeignScanEventEnvelope) -> Void)?
  private let backendFactory: @Sendable (String) throws -> ApplePlatformBackendLease
  private let sessionLock = NSLock()
  private var latestSession: ApplePlatformSession?

  init(
    queueLabel: String,
    eventSink: (@Sendable (BleEvent) -> Void)? = nil,
    scanEventSink: (@Sendable (ForeignScanEventEnvelope) -> Void)? = nil,
    backendFactory: (@Sendable (String) throws -> ApplePlatformBackendLease)? = nil
  ) {
    self.queueLabel = queueLabel
    self.eventSink = eventSink
    self.scanEventSink = scanEventSink
    self.backendFactory = backendFactory ?? { queueLabel in
      try Self.makeDefaultBackend(queueLabel: queueLabel)
    }
  }

  func currentSessionForHost() -> ApplePlatformSession? {
    sessionLock.lock()
    defer { sessionLock.unlock() }
    return latestSession
  }

  func requiredZeroResourceNames() -> [String] {
    ApplePlatformSession.requiredZeroResourceNames
  }

  private static func makeDefaultBackend(
    queueLabel: String
  ) throws -> ApplePlatformBackendLease {
    let native = try AppleBleBackend(
      queueLabel: queueLabel,
      restoreIdentifier: ApplePlatformSession.defaultRestoreIdentifier,
    )
    return ApplePlatformBackendLease(
      backend: native,
      resourceSnapshot: {
        await withCheckedContinuation { continuation in
          native.diagnosticSnapshotRfResources { values in
            continuation.resume(returning: values)
          }
        }
      },
      recreateCentral: {
        await withCheckedContinuation { continuation in
          native.diagnosticRecreateCentral { values in
            continuation.resume(returning: values)
          }
        }
      }
    )
  }

  func create(context: ForeignOwnerContext) throws -> any ForeignPlatformOwnerSession {
    do {
      let lease = try backendFactory("\(queueLabel)-g\(context.ownerGeneration)")
      let session = ApplePlatformSession(
        context: context,
        lease: lease,
        eventSink: eventSink,
        scanEventSink: scanEventSink
      )
      sessionLock.lock()
      latestSession = session
      sessionLock.unlock()
      return session
    } catch {
      throw SupervisorFfiError.Platform(detail: String(describing: error))
    }
  }
}
