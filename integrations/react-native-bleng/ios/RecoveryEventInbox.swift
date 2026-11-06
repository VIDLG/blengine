import BleHubUniFfi
import Foundation

/// Owner-scoped terminal/radio history and an optional scan-scoped advertisement capture.
///
/// Advertisements have no subscriber outside recovery capture. Overflow within a capture or
/// an owner history stream is a safety failure, not a lossy backpressure policy.
final class AppleRecoveryEventInbox: @unchecked Sendable {
  private let terminalContinuation: AsyncStream<UInt64>.Continuation
  private var terminalIterator: AsyncStream<UInt64>.Iterator
  private let lock = NSLock()
  private let capacity: Int
  private var closed = false
  private weak var currentCapture: AppleRecoveryAdvertisementCapture?
  private let radioContinuation: AsyncStream<AdapterState>.Continuation
  private var radioIterator: AsyncStream<AdapterState>.Iterator

  init(capacity: Int = 64) {
    precondition(capacity > 0, "recovery event inbox capacity must be positive")

    var terminalContinuation: AsyncStream<UInt64>.Continuation!
    let terminalStream = AsyncStream<UInt64>(bufferingPolicy: .bufferingOldest(capacity)) {
      terminalContinuation = $0
    }

    var radioContinuation: AsyncStream<AdapterState>.Continuation!
    let radioStream = AsyncStream<AdapterState>(bufferingPolicy: .bufferingOldest(capacity)) {
      radioContinuation = $0
    }

    self.terminalContinuation = terminalContinuation
    self.terminalIterator = terminalStream.makeAsyncIterator()
    self.capacity = capacity
    self.radioContinuation = radioContinuation
    self.radioIterator = radioStream.makeAsyncIterator()
  }

  func offer(_ projection: RecoveryEventProjection) throws {
    switch projection {
    case let .terminal(commandId):
      guard case .enqueued = terminalContinuation.yield(commandId) else {
        throw SupervisorFfiError.Platform(
          detail: "Apple old-operation terminal buffer is unavailable"
        )
      }
    case let .advertisement(advertisement):
      lock.lock()
      let capture = currentCapture
      lock.unlock()
      try capture?.offer(advertisement)
    case let .radioState(state):
      guard case .enqueued = radioContinuation.yield(state) else {
        throw SupervisorFfiError.Platform(
          detail: "Apple adapter-state buffer is unavailable"
        )
      }
    }
  }

  func captureAdvertisements() throws -> AppleRecoveryAdvertisementCapture {
    lock.lock()
    defer { lock.unlock() }
    guard !closed else {
      throw SupervisorFfiError.Platform(
        detail: "Apple event reader closed before a recovery advertisement arrived"
      )
    }
    guard currentCapture?.isOpen != true else {
      throw SupervisorFfiError.Platform(
        detail: "Apple recovery advertisement capture is already active"
      )
    }
    let capture = AppleRecoveryAdvertisementCapture(capacity: capacity)
    currentCapture = capture
    return capture
  }

  func close() {
    lock.lock()
    closed = true
    let capture = currentCapture
    currentCapture = nil
    lock.unlock()
    capture?.close()
    terminalContinuation.finish()
    radioContinuation.finish()
  }

  func waitForRadioOn(
    sequence: UInt64,
    timeoutNanoseconds: UInt64 = 30_000_000_000
  ) async throws -> ForeignPlatformActionResult {
    try await withCancellationClosingInbox {
      try await self.waitForRadioOnUncancellable(
        sequence: sequence,
        timeoutNanoseconds: timeoutNanoseconds
      )
    }
  }

  private func waitForRadioOnUncancellable(
    sequence: UInt64,
    timeoutNanoseconds: UInt64
  ) async throws -> ForeignPlatformActionResult {
    try await withThrowingTaskGroup(of: ForeignPlatformActionResult.self) { group in
      group.addTask { [self] in
        while let state = await radioIterator.next() {
          if state == .poweredOn {
            return ForeignPlatformActionResult(sequence: sequence, signal: .radioOn)
          }
        }
        throw SupervisorFfiError.Platform(
          detail: "Apple event reader closed before the adapter became powered on"
        )
      }
      group.addTask {
        try await Task.sleep(nanoseconds: timeoutNanoseconds)
        throw SupervisorFfiError.Platform(detail: "Apple radio-on wait timed out")
      }
      defer { group.cancelAll() }
      return try await group.next()!
    }
  }


  func nextTerminal(
    matching remaining: [String],
    timeoutNanoseconds: UInt64 = 30_000_000_000
  ) async throws -> UInt64 {
    try await withCancellationClosingInbox {
      try await self.nextTerminalUncancellable(
        matching: remaining,
        timeoutNanoseconds: timeoutNanoseconds
      )
    }
  }

  private func nextTerminalUncancellable(
    matching remaining: [String],
    timeoutNanoseconds: UInt64
  ) async throws -> UInt64 {
    try await withThrowingTaskGroup(of: UInt64.self) { group in
      group.addTask { [self] in
        while let commandId = await terminalIterator.next() {
          if remaining.contains(String(commandId)) {
            return commandId
          }
        }
        throw SupervisorFfiError.Platform(
          detail: "Apple event reader closed before an old operation became terminal"
        )
      }
      group.addTask {
        try await Task.sleep(nanoseconds: timeoutNanoseconds)
        throw SupervisorFfiError.Platform(
          detail: "Apple old-operation terminal barrier timed out"
        )
      }
      defer { group.cancelAll() }
      return try await group.next()!
    }
  }

  private func withCancellationClosingInbox<Value>(
    _ operation: @escaping @Sendable () async throws -> Value
  ) async throws -> Value {
    try await withTaskCancellationHandler(operation: {
      do {
        return try await operation()
      } catch {
        if Task.isCancelled {
          throw CancellationError()
        }
        throw error
      }
    }, onCancel: { [self] in
      close()
    })
  }

}

/// A caller-owned, bounded one-shot subscription installed before starting a recovery scan.
final class AppleRecoveryAdvertisementCapture: @unchecked Sendable {
  private let lock = NSLock()
  private let stream: AsyncThrowingStream<AdvertisementObservation, Error>
  private let continuation: AsyncThrowingStream<AdvertisementObservation, Error>.Continuation
  private var open = true
  private var waiting = false
  private var failure: SupervisorFfiError?

  #if DEBUG
  // Tests configure this before starting the waiter to pause after admission, before draining.
  var waitPause: (@Sendable () async -> Void)?
  #endif

  fileprivate init(capacity: Int) {
    var continuation: AsyncThrowingStream<AdvertisementObservation, Error>.Continuation!
    stream = AsyncThrowingStream(bufferingPolicy: .bufferingOldest(capacity)) {
      continuation = $0
    }
    self.continuation = continuation
  }

  deinit {
    close()
  }

  fileprivate var isOpen: Bool {
    lock.lock()
    defer { lock.unlock() }
    return open
  }

  func offer(_ observation: AdvertisementObservation) throws {
    lock.lock()
    defer { lock.unlock() }
    // A callback may have obtained this lease just before close or replacement.
    guard open else { return }
    guard case .enqueued = continuation.yield(observation) else {
      let error = SupervisorFfiError.Platform(
        detail: "Apple recovery advertisement buffer is unavailable"
      )
      failure = error
      open = false
      continuation.finish(throwing: error)
      throw error
    }
  }

  func close() {
    lock.lock()
    defer { lock.unlock() }
    guard open else { return }
    open = false
    continuation.finish(throwing: Self.closedError)
  }

  func nextAdvertisement(
    scanSessionId: ScanSessionId,
    matching options: ScanOptions,
    timeoutNanoseconds: UInt64 = 30_000_000_000
  ) async throws -> AdvertisementObservation {
    // Admission must precede cleanup so a duplicate waiter cannot close the first wait.
    try beginWait()
    defer { close() }
    return try await withTaskCancellationHandler(operation: {
      do {
        #if DEBUG
        await waitPause?()
        #endif
        try Task.checkCancellation()
        return try await withThrowingTaskGroup(of: AdvertisementObservation.self) { group in
          group.addTask { [self] in
            var iterator = stream.makeAsyncIterator()
            while let observation = try await iterator.next() {
              try Task.checkCancellation()
              if observation.scanSessionId == scanSessionId,
                 matches(options: options, observation: observation) {
                return try complete(observation)
              }
            }
            throw Self.closedError
          }
          group.addTask {
            try await Task.sleep(nanoseconds: timeoutNanoseconds)
            throw SupervisorFfiError.Platform(detail: "Apple recovery advertisement timed out")
          }
          defer {
            // Unsubscribe before cancelling the iterator: late callbacks must see a closed
            // lease, not a terminated continuation that could look like overflow.
            close()
            group.cancelAll()
          }
          return try await group.next()!
        }
      } catch {
        if Task.isCancelled { throw CancellationError() }
        throw error
      }
    }, onCancel: { [self] in
      close()
    })
  }

  private func beginWait() throws {
    lock.lock()
    defer { lock.unlock() }
    if let failure { throw failure }
    guard open else { throw Self.closedError }
    guard !waiting else {
      throw SupervisorFfiError.Platform(
        detail: "Apple recovery advertisement wait is already active"
      )
    }
    waiting = true
  }

  private func complete(_ observation: AdvertisementObservation) throws -> AdvertisementObservation {
    lock.lock()
    defer { lock.unlock() }
    // AsyncThrowingStream can deliver buffered values before its terminal error. Linearize
    // success against overflow/close so a buffered match cannot conceal either failure.
    if let failure { throw failure }
    guard open else { throw Self.closedError }
    open = false
    continuation.finish()
    return observation
  }

  private static var closedError: SupervisorFfiError {
    SupervisorFfiError.Platform(
      detail: "Apple event reader closed before a recovery advertisement arrived"
    )
  }

  private func matches(
    options: ScanOptions,
    observation: AdvertisementObservation
  ) -> Bool {
    if let prefix = options.namePrefix,
       observation.localName?.hasPrefix(prefix) != true {
      return false
    }
    if !options.serviceUuids.isEmpty,
       !options.serviceUuids.contains(where: { required in
         observation.advertisedServices.contains(where: {
           $0.value.caseInsensitiveCompare(required.value) == .orderedSame
         })
       }) {
      return false
    }
    if let companyId = options.companyId,
       !observation.manufacturerData.contains(where: { $0.companyId == companyId }) {
      return false
    }
    return true
  }
}
