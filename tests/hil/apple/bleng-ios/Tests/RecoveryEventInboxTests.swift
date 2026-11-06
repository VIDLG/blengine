import BleHubUniFfi
import XCTest

#if DEBUG
private actor RecoveryAdvertisementWaitPause {
  private let admitted: XCTestExpectation
  private var continuation: CheckedContinuation<Void, Never>?
  private var resumed = false

  init(admitted: XCTestExpectation) {
    self.admitted = admitted
  }

  func wait() async {
    await withCheckedContinuation { continuation in
      if resumed {
        continuation.resume()
      } else {
        self.continuation = continuation
      }
      admitted.fulfill()
    }
  }

  func resume() {
    resumed = true
    continuation?.resume()
    continuation = nil
  }
}
#endif

final class RecoveryEventInboxTests: XCTestCase {
  func testIdleAdvertisementsDoNotOverflowOrDisturbTerminalAndRadioHistory() async throws {
    let inbox = AppleRecoveryEventInbox()
    try inbox.offer(.terminal(42))
    try inbox.offer(.radioState(.poweredOff))
    try inbox.offer(.radioState(.poweredOn))

    for _ in 0..<65 {
      try inbox.offer(.advertisement(AdvertisementObservation(
        scanSessionId: ScanSessionId(value: 1),
        peripheralId: PeripheralId(value: 7),
        localName: "recovery-peer",
        advertisedServices: [],
        manufacturerData: [],
        serviceData: [],
        rssi: -42,
        connectable: true
      )))
    }

    let terminal = try await inbox.nextTerminal(matching: ["42"], timeoutNanoseconds: 100_000_000)
    XCTAssertEqual(terminal, 42)
    let radio = try await inbox.waitForRadioOn(sequence: 17, timeoutNanoseconds: 100_000_000)
    XCTAssertEqual(radio.sequence, 17)
    if case .radioOn = radio.signal {
      // Expected.
    } else {
      XCTFail("idle advertisements disturbed radio evidence")
    }
  }

  func testCaptureBuffersCallbacksBeforeWaitAndSuccessIsOneShot() async throws {
    let inbox = AppleRecoveryEventInbox(capacity: 1)
    let capture = try inbox.captureAdvertisements()
    let expected = advertisement(scan: 42)
    try inbox.offer(.advertisement(expected))

    let result = try await capture.nextAdvertisement(
      scanSessionId: ScanSessionId(value: 42), matching: scanOptions,
      timeoutNanoseconds: 100_000_000
    )
    XCTAssertEqual(result, expected)
    await assertAdvertisementFailure(capture, contains: "closed")
    let fresh = try inbox.captureAdvertisements()
    fresh.close()
  }

  func testCaptureRequiresExactScanIdentityEvenWithIdenticalOptions() async throws {
    let inbox = AppleRecoveryEventInbox(capacity: 2)
    let previous = try inbox.captureAdvertisements()
    previous.close()
    let capture = try inbox.captureAdvertisements()
    try inbox.offer(.advertisement(advertisement(scan: 1, peer: 10)))
    let expected = advertisement(scan: 2, peer: 20)
    try inbox.offer(.advertisement(expected))

    let result = try await capture.nextAdvertisement(
      scanSessionId: ScanSessionId(value: 2), matching: scanOptions,
      timeoutNanoseconds: 100_000_000
    )
    XCTAssertEqual(result, expected)
  }

  func testCapturePreservesNameServiceAndCompanyMatching() async throws {
    let inbox = AppleRecoveryEventInbox(capacity: 4)
    let capture = try inbox.captureAdvertisements()
    let service = BluetoothUuid(value: "abcdef00-0000-1000-8000-00805f9b34fb")
    let otherService = BluetoothUuid(value: "0000180f-0000-1000-8000-00805f9b34fb")
    let options = ScanOptions(
      serviceUuids: [otherService, service], namePrefix: "recovery-",
      companyId: 0x1234, allowDuplicates: false
    )
    try inbox.offer(.advertisement(advertisement(
      name: "other-peer", services: [service], company: 0x1234
    )))
    try inbox.offer(.advertisement(advertisement(
      services: [], company: 0x1234
    )))
    try inbox.offer(.advertisement(advertisement(
      services: [service], company: 0x5678
    )))
    let expected = advertisement(
      services: [BluetoothUuid(value: service.value.uppercased())], company: 0x1234
    )
    try inbox.offer(.advertisement(expected))

    let result = try await capture.nextAdvertisement(
      scanSessionId: ScanSessionId(value: 1), matching: options,
      timeoutNanoseconds: 100_000_000
    )
    XCTAssertEqual(result, expected)
  }

  func testOnlyOneOpenCaptureIsAdmittedWithoutDisturbingIt() async throws {
    let inbox = AppleRecoveryEventInbox()
    let capture = try inbox.captureAdvertisements()
    XCTAssertThrowsError(try inbox.captureAdvertisements()) { error in
      XCTAssertTrue(String(describing: error).contains("already"))
    }
    let expected = advertisement()
    try inbox.offer(.advertisement(expected))
    let result = try await capture.nextAdvertisement(
      scanSessionId: ScanSessionId(value: 1), matching: scanOptions,
      timeoutNanoseconds: 100_000_000
    )
    XCTAssertEqual(result, expected)
  }

  func testConcurrentDuplicateWaitDoesNotCloseTheFirstWait() async throws {
    let inbox = AppleRecoveryEventInbox()
    let capture = try inbox.captureAdvertisements()
    let expected = advertisement()
    let options = scanOptions
    let results = await withTaskGroup(of: Result<AdvertisementObservation, Error>.self) { group in
      for _ in 0..<2 {
        group.addTask {
          do {
            return .success(try await capture.nextAdvertisement(
              scanSessionId: ScanSessionId(value: 1), matching: options,
              timeoutNanoseconds: 1_000_000_000
            ))
          } catch {
            // The rejected duplicate supplies the observation only after the first wait is admitted.
            do {
              try inbox.offer(.advertisement(expected))
            } catch {
              return .failure(error)
            }
            return .failure(error)
          }
        }
      }
      var results: [Result<AdvertisementObservation, Error>] = []
      for await result in group { results.append(result) }
      return results
    }
    var successes = 0
    var duplicates = 0
    for result in results {
      switch result {
      case let .success(observation):
        successes += 1
        XCTAssertEqual(observation, expected)
      case let .failure(error):
        duplicates += 1
        XCTAssertTrue(String(describing: error).contains("already"))
      }
    }
    XCTAssertEqual(successes, 1)
    XCTAssertEqual(duplicates, 1)
  }

  func testCaptureOverflowLatchesFailureAheadOfBufferedMatchingEvidence() async throws {
    let inbox = AppleRecoveryEventInbox(capacity: 1)
    let capture = try inbox.captureAdvertisements()
    try inbox.offer(.advertisement(advertisement()))
    var overflow: String?
    XCTAssertThrowsError(try inbox.offer(.advertisement(advertisement()))) { error in
      overflow = String(describing: error)
      XCTAssertTrue(overflow?.contains("unavailable") == true)
    }
    guard let overflow else {
      return XCTFail("active capture overflow must throw")
    }
    await assertAdvertisementFailure(capture, equalTo: overflow)
    await assertAdvertisementFailure(capture, equalTo: overflow)
    try await assertTerminalAndRadioStillWork(inbox)

    let fresh = try inbox.captureAdvertisements()
    fresh.close()
  }

  func testCaptureUsesTheInboxCapacityRatherThanTheDefault() async throws {
    for capacity in [2, 64] {
      let inbox = capacity == 64 ? AppleRecoveryEventInbox() : AppleRecoveryEventInbox(capacity: capacity)
      let capture = try inbox.captureAdvertisements()
      for _ in 0..<capacity {
        try inbox.offer(.advertisement(advertisement(scan: 99)))
      }
      XCTAssertThrowsError(try inbox.offer(.advertisement(advertisement())))
      await assertAdvertisementFailure(capture, contains: "unavailable")
    }
  }

  #if DEBUG
  func testOverflowFinishesAWaitingCaptureWithTheLatchedFailure() async throws {
    let inbox = AppleRecoveryEventInbox(capacity: 1)
    let capture = try inbox.captureAdvertisements()
    let admitted = expectation(description: "advertisement waiter admitted before overflow")
    let pause = RecoveryAdvertisementWaitPause(admitted: admitted)
    capture.waitPause = { await pause.wait() }
    let waiting = Task {
      try await capture.nextAdvertisement(
        scanSessionId: ScanSessionId(value: 1), matching: scanOptions,
        timeoutNanoseconds: 1_000_000_000
      )
    }
    await fulfillment(of: [admitted], timeout: 1)
    var overflow: String?
    do {
      // The admitted waiter cannot drain: exactly capacity + 1 matching values must overflow.
      try inbox.offer(.advertisement(advertisement()))
      XCTAssertThrowsError(try inbox.offer(.advertisement(advertisement()))) { error in
        overflow = String(describing: error)
        XCTAssertTrue(overflow?.contains("unavailable") == true)
      }
    } catch {
      waiting.cancel()
      await pause.resume()
      _ = await waiting.result
      throw error
    }
    await pause.resume()
    do {
      _ = try await waiting.value
      XCTFail("overflow must fail the admitted waiter, not return its buffered match")
    } catch let error as SupervisorFfiError {
      XCTAssertEqual(String(describing: error), overflow)
    } catch {
      XCTFail("unexpected error: \(error)")
    }
    if let overflow {
      await assertAdvertisementFailure(capture, equalTo: overflow)
    }
  }
  #endif

  func testAdvertisementTimeoutReleasesOnlyTheCapture() async throws {
    let inbox = AppleRecoveryEventInbox()
    let capture = try inbox.captureAdvertisements()
    await assertAdvertisementFailure(capture, contains: "timed out", timeoutNanoseconds: 10_000_000)
    await assertAdvertisementFailure(capture, contains: "closed")
    try await assertTerminalAndRadioStillWork(inbox)
    let fresh = try inbox.captureAdvertisements()
    fresh.close()
  }

  #if DEBUG
  func testAdvertisementCancellationReleasesOnlyTheCapture() async throws {
    let inbox = AppleRecoveryEventInbox()
    let capture = try inbox.captureAdvertisements()
    let admitted = expectation(description: "advertisement waiter admitted before cancellation")
    let pause = RecoveryAdvertisementWaitPause(admitted: admitted)
    capture.waitPause = { await pause.wait() }
    let waiting = Task {
      try await capture.nextAdvertisement(
        scanSessionId: ScanSessionId(value: 1), matching: scanOptions,
        timeoutNanoseconds: 1_000_000_000
      )
    }
    await fulfillment(of: [admitted], timeout: 1)
    waiting.cancel()
    await pause.resume()
    do {
      _ = try await waiting.value
      XCTFail("advertisement cancellation must remain cancellation")
    } catch is CancellationError {
      // Expected.
    } catch {
      XCTFail("unexpected error: \(error)")
    }
    await assertAdvertisementFailure(capture, contains: "closed")
    try await assertTerminalAndRadioStillWork(inbox)
    let fresh = try inbox.captureAdvertisements()
    fresh.close()
  }
  #endif

  func testCloseIsIdempotentAndAnOldCaptureCannotStealFreshObservations() async throws {
    let inbox = AppleRecoveryEventInbox(capacity: 1)
    let old = try inbox.captureAdvertisements()
    try inbox.offer(.advertisement(advertisement(peer: 10)))
    old.close()
    old.close()
    await assertAdvertisementFailure(old, contains: "closed")
    let fresh = try inbox.captureAdvertisements()
    // A callback that obtained the old weak lease before close is now unsubscribed.
    try old.offer(advertisement(peer: 11))
    let expected = advertisement(peer: 20)
    try inbox.offer(.advertisement(expected))
    let result = try await fresh.nextAdvertisement(
      scanSessionId: ScanSessionId(value: 1), matching: scanOptions,
      timeoutNanoseconds: 100_000_000
    )
    XCTAssertEqual(result, expected)
    try await assertTerminalAndRadioStillWork(inbox)
  }

  func testDroppingCaptureReleasesAdmissionWithoutAnInboxRetainCycle() throws {
    let inbox = AppleRecoveryEventInbox()
    var capture: AppleRecoveryAdvertisementCapture? = try inbox.captureAdvertisements()
    weak var dropped = capture
    try inbox.offer(.advertisement(advertisement()))
    capture = nil
    XCTAssertNil(dropped)
    for _ in 0..<65 { try inbox.offer(.advertisement(advertisement())) }
    do {
      let fresh = try inbox.captureAdvertisements()
      dropped = fresh
      fresh.close()
    }
    XCTAssertNil(dropped)
  }

  #if DEBUG
  func testOwnerCloseWakesAdvertisementWaitAndRefusesNewCaptures() async throws {
    let inbox = AppleRecoveryEventInbox()
    let capture = try inbox.captureAdvertisements()
    let admitted = expectation(description: "advertisement waiter admitted before owner close")
    let pause = RecoveryAdvertisementWaitPause(admitted: admitted)
    capture.waitPause = { await pause.wait() }
    let waiting = Task {
      try await capture.nextAdvertisement(
        scanSessionId: ScanSessionId(value: 1), matching: scanOptions,
        timeoutNanoseconds: 1_000_000_000
      )
    }
    await fulfillment(of: [admitted], timeout: 1)
    inbox.close()
    inbox.close()
    await pause.resume()
    do {
      _ = try await waiting.value
      XCTFail("owner close must wake the advertisement waiter")
    } catch let error as SupervisorFfiError {
      XCTAssertTrue(String(describing: error).contains("closed"))
    } catch {
      XCTFail("unexpected error: \(error)")
    }
    XCTAssertThrowsError(try inbox.captureAdvertisements()) { error in
      XCTAssertTrue(String(describing: error).contains("closed"))
    }
    XCTAssertThrowsError(try inbox.offer(.terminal(42)))
    XCTAssertThrowsError(try inbox.offer(.radioState(.poweredOn)))
    try inbox.offer(.advertisement(advertisement()))
  }
  #endif

  private var scanOptions: ScanOptions {
    ScanOptions(serviceUuids: [], namePrefix: "recovery-", companyId: nil, allowDuplicates: false)
  }

  private func advertisement(
    scan: UInt64 = 1,
    peer: UInt64 = 7,
    name: String? = "recovery-peer",
    services: [BluetoothUuid] = [],
    company: UInt16? = nil
  ) -> AdvertisementObservation {
    AdvertisementObservation(
      scanSessionId: ScanSessionId(value: scan), peripheralId: PeripheralId(value: peer),
      localName: name, advertisedServices: services,
      manufacturerData: company.map { [ManufacturerData(companyId: $0, payload: Data())] } ?? [],
      serviceData: [], rssi: -42, connectable: true
    )
  }

  private func assertAdvertisementFailure(
    _ capture: AppleRecoveryAdvertisementCapture,
    contains fragment: String? = nil,
    equalTo expected: String? = nil,
    timeoutNanoseconds: UInt64 = 100_000_000,
    file: StaticString = #filePath,
    line: UInt = #line
  ) async {
    do {
      _ = try await capture.nextAdvertisement(
        scanSessionId: ScanSessionId(value: 1), matching: scanOptions,
        timeoutNanoseconds: timeoutNanoseconds
      )
      XCTFail("capture should fail instead of returning buffered evidence", file: file, line: line)
    } catch let error as SupervisorFfiError {
      let detail = String(describing: error)
      if let fragment { XCTAssertTrue(detail.contains(fragment), detail, file: file, line: line) }
      if let expected { XCTAssertEqual(detail, expected, file: file, line: line) }
    } catch {
      XCTFail("unexpected error: \(error)", file: file, line: line)
    }
  }

  private func assertTerminalAndRadioStillWork(
    _ inbox: AppleRecoveryEventInbox,
    file: StaticString = #filePath,
    line: UInt = #line
  ) async throws {
    try inbox.offer(.terminal(42))
    try inbox.offer(.radioState(.poweredOn))
    let terminal = try await inbox.nextTerminal(matching: ["42"], timeoutNanoseconds: 100_000_000)
    XCTAssertEqual(terminal, 42, file: file, line: line)
    let radio = try await inbox.waitForRadioOn(sequence: 17, timeoutNanoseconds: 100_000_000)
    XCTAssertEqual(radio.sequence, 17, file: file, line: line)
    if case .radioOn = radio.signal {
      // Expected.
    } else {
      XCTFail("advertisement lifecycle disturbed radio evidence", file: file, line: line)
    }
  }

  func testRadioWaitSkipsPoweredOffAndReturnsTheActionSequence() async throws {
    let inbox = AppleRecoveryEventInbox()
    try inbox.offer(.radioState(.poweredOff))
    try inbox.offer(.radioState(.poweredOn))

    let result = try await inbox.waitForRadioOn(sequence: 17, timeoutNanoseconds: 100_000_000)

    XCTAssertEqual(result.sequence, 17)
    if case .radioOn = result.signal {
      // Expected.
    } else {
      XCTFail("radio wait returned an unexpected signal")
    }
  }

  func testTerminalWaitSkipsLateTerminalEvidenceForOtherCommands() async throws {
    let inbox = AppleRecoveryEventInbox()
    try inbox.offer(.terminal(7))
    try inbox.offer(.terminal(42))

    let commandId = try await inbox.nextTerminal(
      matching: ["42"],
      timeoutNanoseconds: 100_000_000
    )

    XCTAssertEqual(commandId, 42)
  }

  func testCloseFailsAWaiterInsteadOfLeavingItBlocked() async {
    let inbox = AppleRecoveryEventInbox()
    inbox.close()

    do {
      _ = try await inbox.waitForRadioOn(sequence: 1, timeoutNanoseconds: 100_000_000)
      XCTFail("closed inbox should fail the pending radio wait")
    } catch let error as SupervisorFfiError {
      XCTAssertTrue(String(describing: error).contains("closed"))
    } catch {
      XCTFail("unexpected error: \(error)")
    }
  }

  func testOverflowFailsClosedInsteadOfDroppingEvidence() throws {
    let inbox = AppleRecoveryEventInbox(capacity: 1)
    try inbox.offer(.radioState(.poweredOff))

    XCTAssertThrowsError(try inbox.offer(.radioState(.poweredOff))) { error in
      XCTAssertTrue(String(describing: error).contains("unavailable"))
    }
  }

  func testCallerCancellationIsNotReclassifiedAsPlatformTimeout() async {
    let inbox = AppleRecoveryEventInbox()
    let waiting = Task {
      try await inbox.waitForRadioOn(sequence: 1, timeoutNanoseconds: 10_000_000_000)
    }
    waiting.cancel()

    do {
      _ = try await waiting.value
      XCTFail("caller cancellation should cancel the radio wait")
    } catch is CancellationError {
      // Expected.
    } catch {
      XCTFail("unexpected error: \(error)")
    }
  }

  func testEmptyRadioStreamTimesOutWithAPlatformFailure() async {
    let inbox = AppleRecoveryEventInbox()

    do {
      _ = try await inbox.waitForRadioOn(sequence: 1, timeoutNanoseconds: 10_000_000)
      XCTFail("empty inbox should time out")
    } catch let error as SupervisorFfiError {
      XCTAssertTrue(String(describing: error).contains("timed out"))
    } catch {
      XCTFail("unexpected error: \(error)")
    }
  }
}
