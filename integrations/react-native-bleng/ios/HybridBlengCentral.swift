import BleHubUniFfi
import Foundation
import NitroModules

private actor AppleBlengSessionLifecycle {
  nonisolated let sessionGate = AppleStartedSessionGate()
  private let ownerFactory: @Sendable () throws -> AppleRustSupervisorOwner
  private var owner: AppleRustSupervisorOwner?
  private var terminalClose: BlengCloseResult?
  private var pendingLifecycleCalls = 0
  private var lifecycleDrained: CheckedContinuation<Void, Never>?
  private var engineRetirement: Task<BlengCloseResult, Never>?

  init(ownerFactory: @escaping @Sendable () throws -> AppleRustSupervisorOwner) {
    self.ownerFactory = ownerFactory
  }

  func start(
    ticket: AppleStartedSessionGate.StartTicket,
    ownerFactoryOverride: (@Sendable () throws -> AppleRustSupervisorOwner)? = nil
  ) async throws -> BlengAdapterSnapshot {
    try sessionGate.validate(ticket: ticket)
    pendingLifecycleCalls += 1
    defer { finishLifecycleCall() }
    let owner: AppleRustSupervisorOwner
    if let current = self.owner {
      owner = current
    } else {
      owner = try (ownerFactoryOverride ?? ownerFactory)()
      self.owner = owner
      terminalClose = nil
    }
    let start = try await owner.start()
    sessionGate.publish(supervisor: owner.supervisor, start: start, ticket: ticket)
    return BlengAdapterSnapshot(adapterSnapshot: start.adapterSnapshot)
  }

  func close() async -> BlengCloseResult {
    if let engineRetirement { return await engineRetirement.value }
    return await closeOwner()
  }

  func invalidateForEngine() async -> BlengCloseResult {
    if let engineRetirement { return await engineRetirement.value }
    let retirement = Task {
      // Invalidation cannot turn a retryable Supervisor busy response into a close receipt.
      await waitForLifecycleDrain()
      return await closeOwner()
    }
    engineRetirement = retirement
    return await retirement.value
  }

  private func waitForLifecycleDrain() async {
    if pendingLifecycleCalls == 0 { return }
    await withCheckedContinuation { lifecycleDrained = $0 }
  }

  private func finishLifecycleCall() {
    pendingLifecycleCalls -= 1
    if pendingLifecycleCalls == 0 {
      let continuation = lifecycleDrained
      lifecycleDrained = nil
      continuation?.resume()
    }
  }

  private func closeOwner() async -> BlengCloseResult {
    // A newer start may enter the actor before this queued close.
    sessionGate.revoke()
    if let terminalClose { return terminalClose }
    guard let owner else {
      let result = BlengCloseResult(closed: true, failure: nil)
      terminalClose = result
      return result
    }
    pendingLifecycleCalls += 1
    defer { finishLifecycleCall() }
    let receipt = await owner.closeResult()
    let result = BlengCloseResult(
      closed: receipt.ownerClosed,
      failure: receipt.failure.map {
        BlengFailureInfo(domain: .lifecycle, code: $0.code, message: $0.message, platform: nil)
      }
    )
    // Actor reentrancy may let another close finish and an explicit start replace this owner.
    if receipt.ownerClosed, self.owner === owner {
      self.owner = nil
      terminalClose = result
    }
    return result
  }
}

private enum BlengEngineStartupFailure: LocalizedError {
  case invalidDeadline, observationCapacity, retirementTimedOut, predecessorFailed

  var errorDescription: String? {
    switch self {
    case .invalidDeadline: return "Invalid engine retirement observation deadline."
    case .observationCapacity: return "Engine retirement observation capacity exhausted."
    case .retirementTimedOut: return "Previous engine retirement was not observed before its deadline."
    case .predecessorFailed: return "Previous engine startup or native retirement failed."
    }
  }

  /// Stable code for the JS failure union; display text stays in `errorDescription`.
  var blengCode: String {
    switch self {
    case .invalidDeadline: return "invalid_deadline"
    case .observationCapacity: return "observation_capacity"
    case .retirementTimedOut: return "retirement_timed_out"
    case .predecessorFailed: return "predecessor_failed"
    }
  }
}

private struct BlengEngineRetirementRecord: @unchecked Sendable {
  let results: [BlengCloseResult]
  let startupFailed: Bool
}

/// One RN module instance owns one engine's native membership; never a global current runtime.
@objc(BlengEngineLifetime)
public final class BlengEngineLifetime: NSObject, @unchecked Sendable {
  static let maximumOwners = 64
  // Serializes immutable predecessor linking only; contains no engine/owner registry.
  private static let linkageLock = NSLock()
  private let lock = NSLock()
  private var owners: [HybridBlengCentral] = []
  private var retirement: Task<[BlengCloseResult], Never>?
  private var completedRetirement: BlengEngineRetirementRecord?
  private var observers: [UUID: CheckedContinuation<BlengEngineRetirementRecord, Error>] = [:]
  private var startupPermission: Task<Void, Error>?
  private var predecessorConfigured = false
  private var successorClaimed = false
  private var ownerFactoryForTesting: (@Sendable () throws -> AppleRustSupervisorOwner)?
  private var hostAdmissionRejected = false

  @objc public override init() { super.init() }

  @objc public func bindOwner(_ pointer: UnsafeMutableRawPointer) -> Bool {
    // Consume the generated wrapper's retain even if membership is rejected.
    let wrapper = HybridBlengCentralSpec_cxx.fromUnsafe(pointer)
    guard let owner = wrapper.getHybridBlengCentralSpec() as? HybridBlengCentral else { return false }
    return bind(owner)
  }

  func bind(_ owner: HybridBlengCentral) -> Bool {
    lock.withLock {
      guard !hostAdmissionRejected, retirement == nil, owners.count < Self.maximumOwners,
            owner.claimEngineBinding(startupPermission: startupPermission, ownerFactory: ownerFactoryForTesting)
      else { return false }
      owners.append(owner)
      return true
    }
  }

  /// Seal module-creation failure without impersonating RN's later invalidation callback.
  @objc public func rejectHostAdmission() {
    lock.withLock { hostAdmissionRejected = true }
  }

  /// The owning host supplies the exact predecessor before any owner is bound.
  @objc public func configurePredecessor(_ predecessor: BlengEngineLifetime, timeoutMs: UInt64) -> Bool {
    guard predecessor !== self, (1...30_000).contains(timeoutMs) else { return false }
    return Self.linkageLock.withLock {
      lock.withLock {
        guard owners.isEmpty, retirement == nil, !predecessorConfigured, !successorClaimed else { return false }
        let claimed = predecessor.lock.withLock {
          if predecessor.successorClaimed { return false }
          predecessor.successorClaimed = true
          return true
        }
        guard claimed else { return false }
        predecessorConfigured = true
        startupPermission = Task { try await predecessor.requireSuccessfulRetirement(timeoutMs: timeoutMs) }
        return true
      }
    }
  }

  // Native-only, per-lifetime injection for the real package-created objects in consumer tests.
  func configureOwnerFactoryForTesting(_ factory: @escaping @Sendable () throws -> AppleRustSupervisorOwner) -> Bool {
    lock.withLock {
      guard owners.isEmpty, retirement == nil, ownerFactoryForTesting == nil, !successorClaimed else { return false }
      ownerFactoryForTesting = factory
      return true
    }
  }

  @objc public func invalidate() {
    lock.withLock {
      guard retirement == nil else { return }
      // This only seals admission and starts tasks, not async native cleanup under this lock.
      let pending = owners.map { $0.invalidateForEngine() }
      let permission = startupPermission
      permission?.cancel()
      owners.removeAll()
      retirement = Task {
        var startupFailed = false
        if let permission, case .failure = await permission.result { startupFailed = true }
        var results: [BlengCloseResult] = []
        for task in pending { results.append(await task.value) }
        completeRetirement(BlengEngineRetirementRecord(results: results, startupFailed: startupFailed))
        return results
      }
    }
  }

  private func completeRetirement(_ record: BlengEngineRetirementRecord) {
    let waiting = lock.withLock {
      completedRetirement = record
      let waiting = Array(observers.values)
      observers.removeAll()
      return waiting
    }
    for continuation in waiting { continuation.resume(returning: record) }
  }

  private func observeRetirement() async throws -> BlengEngineRetirementRecord {
    let id = UUID()
    return try await withTaskCancellationHandler {
      try await withCheckedThrowingContinuation { continuation in
        let immediate: Result<BlengEngineRetirementRecord, Error>? = lock.withLock {
          if Task.isCancelled { return .failure(CancellationError()) }
          if let completedRetirement { return .success(completedRetirement) }
          guard observers.count < 8 else { return .failure(BlengEngineStartupFailure.observationCapacity) }
          observers[id] = continuation
          return nil
        }
        if let immediate { continuation.resume(with: immediate) }
      }
    } onCancel: {
      let continuation = self.lock.withLock { self.observers.removeValue(forKey: id) }
      continuation?.resume(throwing: CancellationError())
    }
  }

  private func retirementBeforeDeadline(timeoutMs: UInt64) async throws -> BlengEngineRetirementRecord {
    guard (1...30_000).contains(timeoutMs) else { throw BlengEngineStartupFailure.invalidDeadline }
    return try await withThrowingTaskGroup(of: BlengEngineRetirementRecord.self) { group in
      group.addTask { try await self.observeRetirement() }
      group.addTask {
        try await Task.sleep(nanoseconds: timeoutMs * 1_000_000)
        throw BlengEngineStartupFailure.retirementTimedOut
      }
      defer { group.cancelAll() }
      return try await group.next()!
    }
  }

  private func requireSuccessfulRetirement(timeoutMs: UInt64) async throws {
    let record = try await retirementBeforeDeadline(timeoutMs: timeoutMs)
    try Task.checkCancellation()
    guard !record.startupFailed, record.results.allSatisfy({ $0.closed && $0.failure == nil }) else {
      throw BlengEngineStartupFailure.predecessorFailed
    }
  }

  /// Waits for a future real invalidation callback as well as completion; never starts cleanup.
  func waitForRetirement(timeoutMs: UInt64) async throws -> [BlengCloseResult] {
    try await retirementBeforeDeadline(timeoutMs: timeoutMs).results
  }

  var boundOwnerCount: Int { lock.withLock { owners.count } }
  var isInvalidated: Bool { lock.withLock { retirement != nil } }

  /// Observe only; a missing RN callback must not be replaced by starting retirement here.
  func waitForRetirement() async -> [BlengCloseResult]? {
    let task = lock.withLock { retirement }
    return await task?.value
  }
}

/// iOS Nitro lifecycle and scan bridge for the formal BLeng package line.
///
/// The bridge serializes lifecycle calls through one native supervisor and does not expose
/// CoreBluetooth objects, UniFFI handles, operation IDs, or recovery policy to JavaScript.
final class HybridBlengCentral: HybridBlengCentralSpec {
  private let lifecycle: AppleBlengSessionLifecycle
  private let beforeStartAdmission: (@Sendable () async -> Void)?
  private let requiresEngineBinding: Bool
  private let engineRetirementLock = NSLock()
  private var engineRetirement: Task<BlengCloseResult, Never>?
  private var engineBound = false
  private var startupPermission: Task<Void, Error>?
  private var engineOwnerFactory: (@Sendable () throws -> AppleRustSupervisorOwner)?

  override init() {
    lifecycle = AppleBlengSessionLifecycle {
      try AppleRustSupervisorOwner(queueLabel: "BleHubReactNativeBleng")
    }
    beforeStartAdmission = nil
    requiresEngineBinding = true
    super.init()
  }

  // Native-only injection; the hook permits deterministic tests of delayed actor admission.
  init(
    ownerFactory: @escaping @Sendable () throws -> AppleRustSupervisorOwner,
    beforeStartAdmission: (@Sendable () async -> Void)? = nil
  ) {
    lifecycle = AppleBlengSessionLifecycle(ownerFactory: ownerFactory)
    self.beforeStartAdmission = beforeStartAdmission
    requiresEngineBinding = false
    super.init()
  }

  func initialize() throws -> Promise<BlengInitializeResult> {
    let ticket = lifecycle.sessionGate.captureTicket()
    let (bindingReady, permission, factory) = engineRetirementLock.withLock {
      (!requiresEngineBinding || engineBound, startupPermission, engineOwnerFactory)
    }
    return blengPromise { [lifecycle, beforeStartAdmission] in
      do {
        guard bindingReady else { throw AppleStartedSessionGate.StartFailure.engineUnbound }
        try await permission?.value
        await beforeStartAdmission?()
        let snapshot = try await lifecycle.start(ticket: ticket, ownerFactoryOverride: factory)
        return .first(BlengInitializeOutcome(snapshot: snapshot))
      } catch {
        // A transition failure is data here, exactly like `close()` reports one. Native work may
        // still be running, so the returned failure is a classification, not proof of a stop.
        return .second(BlengFailure(failure: Self.initializeFailure(error)))
      }
    }
  }

  /// Classifies only the owner-assembly errors `initialize()` can raise. Everything else is a
  /// bridge-level failure rather than an invented BLE outcome.
  private static func initializeFailure(_ error: Error) -> BlengFailureInfo {
    switch error {
    case let error as SupervisorFfiError:
      return BlengFailureMapping.supervisorFailure(error)
    case let error as AppleStartedSessionGate.StartFailure:
      return lifecycleFailure(code: error.blengCode, error: error)
    case let error as BlengEngineStartupFailure:
      return lifecycleFailure(code: error.blengCode, error: error)
    case is CancellationError:
      return BlengFailureInfo(
        domain: .lifecycle, code: "cancelled", message: "Session start was cancelled.", platform: nil)
    default:
      return BlengFailureMapping.bridgeFailure(
        domain: .lifecycle, message: String(describing: error))
    }
  }

  private static func lifecycleFailure(code: String, error: Error) -> BlengFailureInfo {
    let message = (error as? LocalizedError)?.errorDescription ?? String(describing: error)
    return BlengFailureInfo(domain: .lifecycle, code: code, message: message, platform: nil)
  }

  func beginScan(options: BlengScanOptions) throws -> BlengScanSubmission {
    let gate = lifecycle.sessionGate
    return gate.submit { receipt in
      guard let receipt else {
        return BlengScanSubmission.second(BlengFailure(failure: BlengScanMapping.submissionUnavailable()))
      }
      return BlengScanMapping.submission(receipt.supervisor.beginScanView(
        context: receipt.context,
        options: ForeignScanOptions(
          serviceUuids: options.serviceUuids,
          namePrefix: options.namePrefix,
          exactName: options.exactName,
          companyId: options.companyId,
          minRssi: options.minRssi,
          manufacturerData: options.manufacturerData.map { filter in
            ForeignManufacturerDataFilter(
              companyId: filter.companyId,
              data: Data(bytes: filter.data.data, count: Int(filter.data.size)),
              mask: filter.mask.map {
                Data(bytes: $0.data, count: Int($0.size))
              }
            )
          },
          android: options.android.map { android in
            ForeignAndroidScanOptions(
              scanMode: android.scanMode.map { mode in
                switch mode {
                case .lowpower: return .lowPower
                case .balanced: return .balanced
                case .lowlatency: return .lowLatency
                case .opportunistic: return .opportunistic
                }
              },
              phy: android.phy.map { phy in
                switch phy {
                case .le1m: return .le1m
                case .le2m: return .le2m
                case .lecoded: return .leCoded
                }
              },
              reportDelayMs: android.reportDelayMs,
              legacy: android.legacy
            )
          },
          allowDuplicates: options.allowDuplicates
        )
      ), engineAdmission: gate.engineAdmission)
    } ?? .second(BlengFailure(failure: BlengScanMapping.submissionUnavailable()))
  }

  func beginAdapterObservation() throws -> BlengAdapterObservationSubmission {
    let gate = lifecycle.sessionGate
    return gate.submit { receipt in
      guard let receipt else {
        return BlengAdapterObservationSubmission.second(
          BlengFailure(failure: BlengAdapterMapping.submissionUnavailable()))
      }
      return BlengAdapterMapping.submission(
        receipt.supervisor.beginObserveAdapterView(context: receipt.context))
    } ?? .second(BlengFailure(failure: BlengAdapterMapping.submissionUnavailable()))
  }

  func beginAdapterControl(enabled: Bool) throws -> BlengAdapterControlSubmission {
    let gate = lifecycle.sessionGate
    return gate.submit { receipt in
      guard let receipt else {
        return BlengAdapterControlSubmission.second(
          BlengFailure(failure: BlengAdapterMapping.submissionUnavailable()))
      }
      return BlengAdapterMapping.controlSubmission(
        receipt.supervisor.beginSetAdapterEnabledView(
          context: receipt.context, enabled: enabled))
    } ?? .second(BlengFailure(failure: BlengAdapterMapping.submissionUnavailable()))
  }

  func beginCompanionAssociation(
    serviceUuids: [String], displayName: String?, deviceProfile: String?,
    notifyOnDeviceNearby: Bool?
  ) throws -> BlengCompanionAssociationSubmission {
    let gate = lifecycle.sessionGate
    return gate.submit { receipt in
      guard let receipt else {
        return BlengCompanionAssociationSubmission.second(
          BlengFailure(failure: BlengAdapterMapping.submissionUnavailable()))
      }
      return BlengAdapterMapping.companionAssociationSubmission(
        receipt.supervisor.beginAssociateCompanionView(
          context: receipt.context, serviceUuids: serviceUuids, displayName: displayName,
          deviceProfile: deviceProfile, notifyOnDeviceNearby: notifyOnDeviceNearby ?? false))
    } ?? .second(BlengFailure(failure: BlengAdapterMapping.submissionUnavailable()))
  }

  func beginCompanionPresence() throws -> BlengCompanionPresenceSubmission {
    // Companion-device presence is an Android platform feature. Returning a typed refusal is the
    // honest answer; opening a lane that could never report would be a silent ignore.
    .second(
      BlengFailure(
        failure: BlengAdapterMapping.notSupported(
          "companion-presence observation is an Android platform feature; "
            + "CoreBluetooth exposes no equivalent")))
  }

  func beginDeviceQuery(
    kind: BlengDeviceQueryKind,
    serviceUuids: [String]
  ) throws -> BlengDeviceQuerySubmission {
    let gate = lifecycle.sessionGate
    return gate.submit { receipt in
      guard let receipt else {
        return BlengDeviceQuerySubmission.second(
          BlengFailure(failure: BlengAdapterMapping.submissionUnavailable()))
      }
      return BlengAdapterMapping.querySubmission(
        receipt.supervisor.beginQueryDevicesView(
          context: receipt.context,
          kind: BlengAdapterMapping.deviceQueryKind(kind),
          serviceUuids: serviceUuids)
      )
    } ?? .second(BlengFailure(failure: BlengAdapterMapping.submissionUnavailable()))
  }

  func adoptPeripheral(key: String) throws -> BlengPeripheralAdoptSubmission {
    let gate = lifecycle.sessionGate
    return gate.submit { receipt in
      guard let receipt else {
        return BlengPeripheralAdoptSubmission.second(
          BlengFailure(failure: BlengAdapterMapping.submissionUnavailable()))
      }
      return BlengAdapterMapping.adoptSubmission(
        receipt.supervisor.adoptPeripheral(context: receipt.context, key: key))
    } ?? .second(BlengFailure(failure: BlengAdapterMapping.submissionUnavailable()))
  }

  func close() throws -> Promise<BlengCloseResult> {
    // Revoke before scheduling the actor hop, not after an asynchronous shutdown starts.
    lifecycle.sessionGate.revoke()
    let retirement = engineRetirementLock.withLock { engineRetirement }
    return run { [lifecycle] in
      if let retirement { return await retirement.value }
      return await lifecycle.close()
    }
  }

  fileprivate func claimEngineBinding(
    startupPermission: Task<Void, Error>?,
    ownerFactory: (@Sendable () throws -> AppleRustSupervisorOwner)?
  ) -> Bool {
    engineRetirementLock.withLock {
      guard !engineBound, engineRetirement == nil else { return false }
      self.startupPermission = startupPermission
      engineOwnerFactory = ownerFactory
      engineBound = true
      return true
    }
  }

  /// Native-only barrier invoked by the owning RN engine; never a JS Promise.
  func invalidateForEngine() -> Task<BlengCloseResult, Never> {
    engineRetirementLock.withLock {
      if let engineRetirement { return engineRetirement }
      lifecycle.sessionGate.invalidateEngine()
      let retirement = Task { [lifecycle] in await lifecycle.invalidateForEngine() }
      engineRetirement = retirement
      return retirement
    }
  }

  private func run<Value>(_ action: @escaping () async throws -> Value) -> Promise<Value> {
    let promise = Promise<Value>()
    Task {
      do {
        let result = try await action()
        promise.resolve(withResult: result)
      } catch {
        promise.reject(withError: error)
      }
    }
    return promise
  }
}

extension BlengAdapterSnapshot {
  init(adapterSnapshot snapshot: AdapterSnapshot) {
    let state: BlengAdapterState
    switch snapshot.state {
    case .unknown: state = .unknown
    case .unavailable: state = .unavailable
    case .poweredOff: state = .poweredoff
    case .poweredOn: state = .poweredon
    }

    func authorization(_ value: AuthorizationState) -> BlengAuthorizationState {
      switch value {
      case .notRequired: return .notrequired
      case .notDetermined: return .notdetermined
      case .denied: return .denied
      case .restricted: return .restricted
      case .authorized: return .authorized
      }
    }

    self.init(
      state: state,
      scanAuthorization: authorization(snapshot.scanAuthorization),
      connectionAuthorization: authorization(snapshot.connectionAuthorization),
      name: snapshot.name
    )
  }
}
