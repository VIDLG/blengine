import NitroModules
import XCTest
@testable import BleHubReactNative

final class BleHubNitroHostNativeTests: XCTestCase {
  // Keep these in the class selected by host-ios-native-lifecycle-test.
  func testWriteRejectNativeControlRequiresReadinessAndExactFourPhaseIdentity() throws {
    let session = "12345678-1234-4234-8234-123456789abc"
    let token = "11111111-1111-4111-8111-111111111111"
    let raw = "{\"session\":\"\(session)\",\"flow\":10,\"mode\":\"rf-write-reject\",\"token\":\"\(token)\"}"
    let identity = "session=\(session):flow=10:mode=rf-write-reject:token=\(token)"
    let prefix = "blehub-rf://control/\(session)/10/rf-write-reject/\(token)/"
    let initial = [["marker": "rf-write-reject:runtime:session=\(session):nonce=boot:phase=start"],
                   ["marker": "rf-control:listener:\(identity):phase=registered"]]
    func evaluate(_ actions: [[String: String]], _ control: String? = nil) throws -> NSDictionary {
      guard var gate = NarrowRejectControlGate(control ?? raw) else { return ["invalid": true] }
      var accepted: [Bool] = []
      for action in actions {
        if let marker = action["marker"] { gate.observe(marker) }
        else if let url = action["url"] { accepted.append(gate.accept(url)) }
        else { gate.fail() }
      }
      return ["accepted": accepted, "failed": gate.failed]
    }
    var all = initial
    for (request, phase) in zip(["arm", "write-dispatched", "reject", "recovery-done"], ["armed", "held", "rejected", "close-allowed"]) {
      all.append(["marker": "rf-control:ready:\(identity):phase=\(request)"])
      all.append(["url": prefix + phase])
    }
    XCTAssertEqual(try evaluate(all)["accepted"] as? [Bool], [true, true, true, true])
    XCTAssertEqual(try evaluate(all)["failed"] as? Bool, false)
    XCTAssertEqual(try evaluate(initial + [["url": prefix + "armed"]])["accepted"] as? [Bool], [false])
    let ready = initial + [["marker": "rf-control:ready:\(identity):phase=arm"]]
    for invalid in [prefix + "held", prefix + "armed/", prefix + "armed?x=1", prefix + "armed#x",
                    (prefix + "armed").replacingOccurrences(of: token, with: session), "blehub-rn-spike://reload"] {
      XCTAssertEqual(try evaluate(ready + [["url": invalid], ["url": prefix + "armed"]])["accepted"] as? [Bool], [false, false])
    }
    XCTAssertEqual(try evaluate(all + [["url": prefix + "close-allowed"]])["accepted"] as? [Bool], [true, true, true, true, false])
    let closed = ready + [["marker": "rf-write-reject:runtime:session=\(session):nonce=boot:phase=close"], ["url": prefix + "armed"]]
    XCTAssertEqual(try evaluate(closed)["accepted"] as? [Bool], [false])
    for marker in ["rf-control:ready:\(identity):phase=read-dispatched",
                   "rf-write-reject:runtime:session=\(session):nonce=changed:phase=start"] {
      XCTAssertEqual(try evaluate(ready + [["marker": marker], ["url": prefix + "armed"]])["accepted"] as? [Bool], [false])
    }
    for invalid in [raw.replacingOccurrences(of: "10", with: "8"), raw.replacingOccurrences(of: token, with: "BAD"),
                    raw.replacingOccurrences(of: "rf-write-reject", with: "rf-cancellation")] {
      XCTAssertEqual(try evaluate([], invalid)["invalid"] as? Bool, true)
    }
  }
  func testCccRejectRequiresExactReadyAckPairsAndRetainsNativeIdentity() throws {
    let session = "12345678-1234-4234-8234-123456789abc"
    let token = "11111111-1111-4111-8111-111111111111"
    for (mode, flow, requests, phases) in [
      ("rf-ccc-enable-reject", 11,
       ["arm", "enable-dispatched", "reject", "recovery-enabled", "recovery-notified", "disabled", "silence-done"],
       ["armed", "held", "rejected", "value-sent", "disable-allowed", "silence-started", "close-allowed"]),
      ("rf-ccc-disable-reject", 12,
       ["baseline-enabled", "arm", "disable-dispatched", "reject", "reject-observed", "continued-notified", "disabled", "silence-done"],
       ["baseline-value-sent", "armed", "held", "rejected", "continued-value-sent", "retry-allowed", "silence-started", "close-allowed"]),
    ] {
      let raw = "{\"session\":\"\(session)\",\"flow\":\(flow),\"mode\":\"\(mode)\",\"token\":\"\(token)\"}"
      let identity = "session=\(session):flow=\(flow):mode=\(mode):token=\(token)"
      let prefix = "blehub-rf://control/\(session)/\(flow)/\(mode)/\(token)/"
      func prepared() throws -> NarrowRejectControlGate {
        var gate = try XCTUnwrap(NarrowRejectControlGate(raw, mode: mode))
        gate.observe("\(mode):runtime:session=\(session):nonce=boot:phase=start")
        gate.observe("rf-control:listener:\(identity):phase=registered")
        return gate
      }
      var gate = try prepared()
      for (request, phase) in zip(requests, phases) {
        gate.observe("rf-control:ready:\(identity):phase=\(request)")
        XCTAssertTrue(gate.accept(prefix + phase))
      }
      gate.observe("\(mode):runtime:session=\(session):nonce=boot:phase=close")
      XCTAssertFalse(gate.failed)
      XCTAssertFalse(gate.accept(prefix + "close-allowed"))
      for index in phases.indices {
        var ready = try prepared()
        for prior in 0..<index {
          ready.observe("rf-control:ready:\(identity):phase=\(requests[prior])")
          XCTAssertTrue(ready.accept(prefix + phases[prior]))
        }
        var early = ready
        XCTAssertFalse(early.accept(prefix + phases[index]))
        ready.observe("rf-control:ready:\(identity):phase=\(requests[index])")
        let expected = prefix + phases[index]
        for invalid in [expected + "/", expected + "?x=1", expected + "#x",
                        expected.replacingOccurrences(of: token, with: session),
                        expected.replacingOccurrences(of: "/\(flow)/", with: "/10/"),
                        expected.replacingOccurrences(of: mode, with: "rf-write-reject"),
                        prefix + phases[(index + 1) % phases.count], "blehub-rn-spike://reload"] {
          var invalidGate = ready
          XCTAssertFalse(invalidGate.accept(invalid))
          XCTAssertFalse(invalidGate.accept(expected))
        }
        var replay = ready
        XCTAssertTrue(replay.accept(expected))
        XCTAssertFalse(replay.accept(expected))
        var duplicate = ready
        duplicate.observe("rf-control:ready:\(identity):phase=\(requests[index])")
        XCTAssertFalse(duplicate.accept(expected))
      }
      for marker in ["\(mode):runtime:session=\(session):nonce=changed:phase=start",
                     "rf-control:listener:\(identity):phase=registered",
                     "rf-control:ready:\(identity):phase=write-dispatched"] {
        var invalid = try prepared()
        invalid.observe(marker)
        XCTAssertTrue(invalid.failed)
        XCTAssertFalse(invalid.accept(prefix + "armed"))
      }
      XCTAssertNil(NarrowRejectControlGate(raw))
      for invalid in [raw.replacingOccurrences(of: ":\(flow)", with: ":10"),
                      raw.replacingOccurrences(of: token, with: "BAD"),
                      raw.replacingOccurrences(of: mode, with: "rf-write-reject")] {
        XCTAssertNil(NarrowRejectControlGate(invalid, mode: mode))
      }
    }
  }

  func testRfSnapshotCountsListenersAndCachesZeroAfterClose() throws {
    let probe = HybridBleHubProbe()
    try probe.setCompletionListener { _ in }
    try probe.setRfScanListener { _ in }
    let observed = expectation(description: "uninitialized ownership")
    try probe.snapshotRfResources().then { counts in
      XCTAssertEqual(counts.listeners, 2)
      XCTAssertEqual(counts.hubRoots, 0)
      XCTAssertEqual(counts.handoffRoots, 0)
      XCTAssertEqual(counts.backendLeases, 0)
      observed.fulfill()
    }.catch { error in
      XCTFail("snapshot rejected: \(error)")
      observed.fulfill()
    }
    wait(for: [observed], timeout: 5)

    let closed = expectation(description: "cached primitive zero after reentrant close")
    let first = try probe.close()
    first.then { _ in
      do {
        XCTAssertTrue(try probe.close() === first)
        try probe.snapshotRfResources().then { counts in
          self.assertAllResourcesZero(counts)
          closed.fulfill()
        }.catch { error in
          XCTFail("closed snapshot rejected: \(error)")
          closed.fulfill()
        }
      } catch {
        XCTFail("reentrant close failed: \(error)")
        closed.fulfill()
      }
    }.catch { error in
      XCTFail("close rejected: \(error)")
      closed.fulfill()
    }
    wait(for: [closed], timeout: 5)
  }

  func testProductionResourcesAreObservedBeforeAndAfterNativeDispose() throws {
    let probe = HybridBleHubProbe()
    XCTAssertNil(try probe.initialize(commandId: "1"))
    let observed = expectation(description: "live production roots")
    try probe.snapshotRfResources().then { counts in
      XCTAssertEqual(counts.hubRoots, 1)
      XCTAssertEqual(counts.handoffRoots, 1)
      XCTAssertEqual(counts.ingressRoots, 1)
      XCTAssertEqual(counts.backendLeases, 1)
      XCTAssertEqual(counts.adapterDelegates, 1)
      observed.fulfill()
    }.catch { error in
      XCTFail("production snapshot rejected: \(error)")
      observed.fulfill()
    }
    wait(for: [observed], timeout: 5)

    let closed = expectation(description: "native close completed and snapshot cached")
    _ = try probe.close()
    // A snapshot issued during close must wait for actual cleanup, not infer
    // absence from dropping the probe's backend reference.
    try probe.snapshotRfResources().then { counts in
      self.assertAllResourcesZero(counts)
      closed.fulfill()
    }.catch { error in
      XCTFail("post-dispose snapshot rejected: \(error)")
      closed.fulfill()
    }
    wait(for: [closed], timeout: 5)
  }

  func testDisposeTestSeamCannotProducePositiveRfEvidence() throws {
    let gate = DispatchSemaphore(value: 0)
    let probe = HybridBleHubProbe.forDisposeWaitTest(gate)
    let rejected = expectation(description: "unsupported diagnostic rejects")
    try probe.snapshotRfResources().then { _ in
      XCTFail("test-only dispose seam fabricated RF evidence")
      rejected.fulfill()
    }.catch { _ in rejected.fulfill() }
    wait(for: [rejected], timeout: 5)
    gate.signal()
    let closed = expectation(description: "test seam still closes")
    try probe.close().then { _ in closed.fulfill() }.catch { error in
      XCTFail("test close rejected: \(error)")
      closed.fulfill()
    }
    wait(for: [closed], timeout: 5)
    let stillRejected = expectation(description: "cached test seam also rejects")
    try probe.snapshotRfResources().then { _ in
      XCTFail("closed test seam fabricated RF evidence")
      stillRejected.fulfill()
    }.catch { _ in stillRejected.fulfill() }
    wait(for: [stillRejected], timeout: 5)
  }

  func testHandoffDrainAccountsExecutingAndQueuedHandlersAndRejectsLateSubmit() {
    let queue = DispatchQueue(label: "dev.blehub.tests.handoff-drain")
    let entered = DispatchSemaphore(value: 0)
    let release = DispatchSemaphore(value: 0)
    let drained = DispatchSemaphore(value: 0)
    let handoff = BoundedEventHandoff<Int>(stateQueue: queue, capacity: 4) { event in
      XCTAssertEqual(event, 1, "queued event must be discarded after close")
      entered.signal()
      _ = release.wait(timeout: .now() + 5)
    }
    XCTAssertTrue(handoff.submit(1))
    XCTAssertEqual(entered.wait(timeout: .now() + 5), .success)
    XCTAssertTrue(handoff.submit(2))
    XCTAssertEqual(handoff.diagnosticPendingCount, 2)
    handoff.closeAndDrain { drained.signal() }
    handoff.close()
    XCTAssertFalse(handoff.submit(3))
    XCTAssertEqual(drained.wait(timeout: .now() + 0.1), .timedOut)
    release.signal()
    XCTAssertEqual(drained.wait(timeout: .now() + 5), .success)
    XCTAssertEqual(handoff.diagnosticPendingCount, 0)
    let repeated = expectation(description: "repeated drain is asynchronous and reentrant")
    queue.async {
      handoff.closeAndDrain {
        XCTAssertEqual(handoff.diagnosticPendingCount, 0)
        repeated.fulfill()
      }
    }
    wait(for: [repeated], timeout: 5)
  }

  func testHandoffCloseDiscardsAcceptedEventAndRejectsLateSubmission() {
    let queue = DispatchQueue(label: "dev.blehub.tests.handoff-close")
    var delivered: [Int] = []
    let handoff = BoundedEventHandoff<Int>(stateQueue: queue, capacity: 1) {
      delivered.append($0)
    }
    queue.suspend()
    XCTAssertTrue(handoff.submit(1))
    handoff.close()
    handoff.close()
    XCTAssertFalse(handoff.submit(2))
    queue.resume()
    queue.sync {}
    XCTAssertTrue(delivered.isEmpty)
    XCTAssertFalse(handoff.submit(3))
  }

  func testHandoffCapacityOverflowRejectsWithoutDroppingAcceptedEvent() {
    let queue = DispatchQueue(label: "dev.blehub.tests.handoff-capacity")
    var delivered: [Int] = []
    let handoff = BoundedEventHandoff<Int>(stateQueue: queue, capacity: 1) {
      delivered.append($0)
    }
    queue.suspend()
    XCTAssertTrue(handoff.submit(1))
    XCTAssertFalse(handoff.submit(2))
    queue.resume()
    queue.sync {}
    XCTAssertEqual(delivered, [1])
    XCTAssertFalse(handoff.submit(3))
  }

  func testHandoffCanCloseAndDrainInsideExecutingDelivery() {
    let queue = DispatchQueue(label: "dev.blehub.tests.handoff-reentrant")
    let drained = expectation(description: "delivery returned before drain completion")
    var handoff: BoundedEventHandoff<Int>!
    handoff = BoundedEventHandoff(stateQueue: queue, capacity: 1) { _ in
      XCTAssertEqual(handoff.diagnosticPendingCount, 1)
      handoff.closeAndDrain {
        XCTAssertEqual(handoff.diagnosticPendingCount, 0)
        drained.fulfill()
      }
      XCTAssertEqual(handoff.diagnosticPendingCount, 1)
    }
    XCTAssertTrue(handoff.submit(1))
    wait(for: [drained], timeout: 5)
    handoff = nil // Break the deliberately reentrant test closure's capture box.
  }

  private func assertAllResourcesZero(
    _ counts: RfResourceSnapshot, file: StaticString = #filePath, line: UInt = #line
  ) {
    let values = [
      counts.activeScans, counts.connections, counts.gattAttributes, counts.subscriptions,
      counts.pendingIo, counts.watchdogs, counts.callbackRoutes, counts.ingressRoots,
      counts.adapterDelegates, counts.peerCacheEntries, counts.backendLeases, counts.hubRoots,
      counts.handoffRoots, counts.handoffPending, counts.listeners, counts.workerThreads,
      counts.lifecycleOwners,
    ]
    XCTAssertEqual(values.count, 17, file: file, line: line)
    XCTAssertTrue(values.allSatisfy { $0 == 0 }, "post-close counts: \(values)", file: file, line: line)
  }

  func testClosePromiseRemainsPendingUntilDisposeReturnsAndSettlesOnce() throws {
    let disposeGate = DispatchSemaphore(value: 0)
    let probe = HybridBleHubProbe.forDisposeWaitTest(disposeGate)
    let settled = DispatchSemaphore(value: 0)
    let lock = NSLock()
    var settleCount = 0
    var rejection: Error?

    let first = try probe.close()
    let second = try probe.close()
    XCTAssertTrue(first === second)

    first.then { _ in
      lock.lock()
      settleCount += 1
      lock.unlock()
      settled.signal()
    }.catch { error in
      lock.lock()
      rejection = error
      settleCount += 1
      lock.unlock()
      settled.signal()
    }

    XCTAssertEqual(
      settled.wait(timeout: .now() + 0.1),
      .timedOut,
      "close Promise settled before the native dispose operation returned"
    )

    disposeGate.signal()
    XCTAssertEqual(settled.wait(timeout: .now() + 5), .success)

    lock.lock()
    let count = settleCount
    let failure = rejection
    lock.unlock()
    XCTAssertNil(failure)
    XCTAssertEqual(count, 1)
    XCTAssertTrue(try probe.close() === first)
  }
}


final class FullRecoveryBeginGateTests: XCTestCase {
  private let runId = "12345678-1234-4234-8234-123456789abc"
  private var begin: String { "blehub-rn-spike://recover/\(runId)/begin" }
  private var markers: [String] { [
    "rf-lab-full:runtime:session=\(runId):nonce=boot:phase=start",
    "rf-lab-suite:runtime:session=\(runId):nonce=boot:phase=start",
    "rf-lab-suite:runtime:session=\(runId):nonce=boot:phase=close",
    "rf-lab-suite:passed:flows=10:passedCount=10:failureCount=0",
    "rf-lab-full:recovery-awaiting:runId=\(runId)",
  ] }

  private func evaluate(_ markers: [String], _ urls: [String], _ complete: [Bool]) throws -> NSDictionary {
    let host: AnyObject = try XCTUnwrap(NSClassFromString("BleHubFullRecoveryGateProbe"))
    let input: NSDictionary = ["runId": runId, "markers": markers, "urls": urls, "complete": complete]
    return try XCTUnwrap(host.perform(NSSelectorFromString("evaluate:"), with: input)?.takeUnretainedValue() as? NSDictionary)
  }

  func testBeginRequiresBasePassAwaitingAndAllNativeControlDeliveries() throws {
    XCTAssertEqual(try evaluate([], [begin], [true])["accepted"] as? [Bool], [false])
    XCTAssertEqual(try evaluate(markers, [begin, begin], [false, true])["accepted"] as? [Bool], [false, false])
    let ready = try evaluate(markers, [begin, begin], [true, true])
    XCTAssertEqual(ready["accepted"] as? [Bool], [true, false])
    XCTAssertEqual(ready["begun"] as? Bool, true)
    XCTAssertEqual(ready["nonce"] as? String, "boot")
  }

  func testURLIsExactAndAnyFailureIsSticky() throws {
    for suffix in ["/", "?token=other", "#fragment", "/0"] {
      XCTAssertEqual(try evaluate(markers, [begin + suffix, begin], [true, true])["accepted"] as? [Bool], [false, false])
    }
    let failed = markers + ["rf-lab-suite:flow-failed:index=9:mode=rf-watchdog:detail=close"]
    XCTAssertEqual(try evaluate(failed, [begin], [true])["accepted"] as? [Bool], [false])
  }

  func testRuntimeNonceForeignSessionAndClosedPhaseRejectTransition() throws {
    for extra in ["rf-lab-suite:runtime:session=\(runId):nonce=changed:phase=close",
                  "rf-lab-full:runtime:session=\(runId):nonce=boot:phase=close"] {
      XCTAssertEqual(try evaluate(markers + [extra], [begin], [true])["accepted"] as? [Bool], [false])
    }
    let foreign = begin.replacingOccurrences(of: runId, with: "00000000-0000-0000-0000-000000000000")
    XCTAssertEqual(try evaluate(markers, [foreign], [true])["accepted"] as? [Bool], [false])
  }
}


/// Deterministic owner/close tests. No JS engine, BLE controller or RF receipt is fabricated.
final class PendingReloadNativeTests: XCTestCase {
  private let runId = "12345678-1234-4234-8234-123456789abc"
  private let route = "commandId=12011:connectionId=12004:gattSessionId=12008:characteristicId=20"
  private func session() -> PendingReloadSession {
    PendingReloadSession(runId: runId, notificationName: "dev.blehub.rn.reload." + runId,
      bootId: UUID().uuidString.lowercased(), emit: { _ in })
  }
  private func bound(_ session: PendingReloadSession, _ probe: HybridBleHubProbe) throws -> PendingReloadOwner {
    let owner = try XCTUnwrap(session.install(UUID().uuidString.lowercased()))
    XCTAssertTrue(session.bind(probe, owner: owner))
    return owner
  }
  private func ready(_ probe: HybridBleHubProbe, generation: Double) throws {
    let done = expectation(description: "native readiness")
    try probe.awaitPendingReloadReady(runId: runId, generation: generation).then { _ in done.fulfill() }.catch {
      XCTFail("readiness failed: \($0)"); done.fulfill()
    }
    wait(for: [done], timeout: 5)
  }
  private func armAndRequestReload(_ session: PendingReloadSession, _ probe: HybridBleHubProbe) {
    session.observe("BLEHUB_NITRO_RUNTIME rf-pending-read-reload:generation=1:runtime-start:runId=\(runId):nonce=old-js")
    session.observe("BLEHUB_NITRO_RUNTIME rf-pending-read-reload:generation=1:awaiting-arm:" + route)
    XCTAssertEqual(session.acceptControl("blehub-rn-spike://arm/" + runId), "arm")
    probe.preparePendingReadForLifecycleTest()
    session.observe("BLEHUB_NITRO_RUNTIME rf-pending-read-reload:generation=1:read-dispatched:" + route + ":peripheralId=0")
    XCTAssertEqual(session.acceptControl("blehub-rn-spike://reload/" + runId), "reload")
  }

  func testActualHermesProbeBindingAndRnModuleManagerInvalidationFailClosedWithoutPendingRead() throws {
    let harness: AnyObject = try XCTUnwrap(NSClassFromString("BleHubPendingReloadRuntimeTest"))
    let result = try XCTUnwrap(harness.perform(NSSelectorFromString("evaluate"))?.takeUnretainedValue() as? NSDictionary)
    XCTAssertEqual(result["plainObjectRejected"] as? Bool, true)
    XCTAssertEqual(result["nativeGeneration"] as? Bool, true)
    XCTAssertEqual(result["configurationImmutable"] as? Bool, true)
    XCTAssertEqual(result["invalidated"] as? Bool, true)
    XCTAssertEqual(result["hadBoundProbe"] as? Bool, true)
    XCTAssertEqual(result["failedWithoutAcceptedRead"] as? Bool, true)
    XCTAssertEqual(result["membership"] as? Int, 0)
    XCTAssertNil(result["timeout"])
  }

  func testNewRuntimeCannotInitializeUntilOldNativeDisposeAndHandoffDrainComplete() throws {
    let session = session()
    let entered = expectation(description: "dispose entered outside invalidation stack")
    let release = DispatchSemaphore(value: 0)
    let old = HybridBleHubProbe.forPendingLifecycleTest {
      entered.fulfill()
      guard release.wait(timeout: .now() + 10) == .success else { throw PendingReloadSession.error("test gate timeout") }
    }
    let first = try bound(session, old)
    try ready(old, generation: 1)
    armAndRequestReload(session, old)
    let next = HybridBleHubProbe()
    let second = try bound(session, next)
    let newReady = expectation(description: "old native completion releases new runtime")
    let nativePromise = try next.awaitPendingReloadReady(runId: runId, generation: 2)
    nativePromise.then { _ in newReady.fulfill() }.catch { XCTFail("new readiness: \($0)"); newReady.fulfill() }
    XCTAssertFalse(second.canInitialize)
    XCTAssertEqual(try next.initialize(commandId: "13000")?.category, .invalidState)
    session.invalidate(first)
    wait(for: [entered], timeout: 5)
    XCTAssertFalse(second.canInitialize)
    XCTAssertEqual(first.membershipCount, 1)
    release.signal()
    wait(for: [newReady], timeout: 5)
    XCTAssertTrue(first.closeStartedByEngine)
    XCTAssertTrue(first.closed)
    XCTAssertEqual(first.membershipCount, 0)
    XCTAssertTrue(second.canInitialize)
    let done = expectation(description: "new owner cleanup")
    try next.close().then { _ in done.fulfill() }
    wait(for: [done], timeout: 5)
  }

  func testEffectCloseCannotBeRelabeledAsEngineInvalidation() throws {
    let session = session()
    let probe = HybridBleHubProbe.forPendingLifecycleTest {}
    let owner = try bound(session, probe)
    try ready(probe, generation: 1)
    armAndRequestReload(session, probe)
    let closed = expectation(description: "manual cleanup still completes")
    try probe.close().then { _ in closed.fulfill() }
    session.invalidate(owner)
    wait(for: [closed], timeout: 5)
    XCTAssertFalse(owner.closeStartedByEngine)
    XCTAssertNotNil(session.failure)
    XCTAssertNil(session.install(UUID().uuidString.lowercased()))
  }

  func testDisposeFailureRejectsNewNativeBarrierWithoutAnOldJsPromise() throws {
    let session = session()
    let old = HybridBleHubProbe.forPendingLifecycleTest { throw PendingReloadSession.error("deliberate dispose failure") }
    let first = try bound(session, old)
    try ready(old, generation: 1)
    armAndRequestReload(session, old)
    let next = HybridBleHubProbe()
    let second = try bound(session, next)
    let rejected = expectation(description: "native failure rejects readiness")
    try next.awaitPendingReloadReady(runId: runId, generation: 2).then { _ in
      XCTFail("failed old dispose released new runtime"); rejected.fulfill()
    }.catch { _ in rejected.fulfill() }
    session.invalidate(first)
    wait(for: [rejected], timeout: 5)
    XCTAssertFalse(second.canInitialize)
    XCTAssertFalse(first.closed)
    XCTAssertNotNil(session.failure)
    _ = try next.close()
  }

  func testExactSessionOnceOnlyControlAndActualProbeOwnershipAreFailClosed() throws {
    for suffix in ["/", "?other=1", "#fragment", "/other"] {
      let session = session()
      let probe = HybridBleHubProbe()
      _ = try bound(session, probe)
      XCTAssertNil(session.acceptControl("blehub-rn-spike://arm/" + runId + suffix))
      XCTAssertNotNil(session.failure)
      _ = try probe.close()
    }
    let duplicate = session()
    let old = HybridBleHubProbe.forPendingLifecycleTest {}
    _ = try bound(duplicate, old)
    try ready(old, generation: 1)
    armAndRequestReload(duplicate, old)
    XCTAssertNil(duplicate.acceptControl("blehub-rn-spike://reload/" + runId))
    XCTAssertNotNil(duplicate.failure)
    _ = try old.close()

    let rebinding = session()
    let probe = HybridBleHubProbe()
    let owner = try bound(rebinding, probe)
    XCTAssertFalse(rebinding.bind(probe, owner: owner))
    XCTAssertNotNil(rebinding.failure)
    _ = try probe.close()
  }

  func testNoAcceptedReadNoNativeBindingAndMissingReadinessNeverAuthorizeInitialization() throws {
    let session = session()
    let old = HybridBleHubProbe()
    let owner = try bound(session, old)
    XCTAssertEqual(try old.initialize(commandId: "12000")?.category, .invalidState)
    XCTAssertNil(session.acceptControl("blehub-rn-spike://reload/" + runId))
    session.invalidate(owner)
    XCTAssertNotNil(session.failure)
    XCTAssertFalse(owner.closeStartedByEngine)
    let rejected = expectation(description: "unbound probe rejects barrier")
    try HybridBleHubProbe().awaitPendingReloadReady(runId: runId, generation: 2).catch { _ in rejected.fulfill() }
    wait(for: [rejected], timeout: 5)
  }
}


extension BleHubNitroHostNativeTests {
  private var adapterRun: String { "12345678-1234-4234-8234-123456789abc" }
  private var adapterBoot: String { "11111111-1111-4111-8111-111111111111" }
  private var adapterArguments: [String] {
    ["host", "--blehub-nitro-rf-adapter-service", adapterBoot,
     "--blehub-nitro-rf-adapter-run-id", adapterRun,
     "--blehub-nitro-rf-adapter-mode", "startup-off",
     "--blehub-nitro-rf-adapter-local-name", "BleHubNrf52840"]
  }
  private func adapterLaunch(_ arguments: [String]? = nil, authorized: Bool = true,
                             bundle: String = "org.reactjs.native.example.BleHubNitroHost",
                             executable: URL = URL(fileURLWithPath: "/Application Support/BleHubNitroHost.app/BleHubNitroHost")) -> AdapterStartupLaunch? {
    AdapterStartupLaunch(arguments: arguments ?? adapterArguments, authorized: authorized,
      pid: 123, bootId: adapterBoot, bundleId: bundle, executable: executable)
  }
  private func adapterMarker(_ body: String) -> String { AdapterStartupGate.prefix + body }
  private var adapterStart: String { adapterMarker("runtime:runId=\(adapterRun):nonce=boot-123-abc:phase=start") }
  private var adapterClose: String { adapterMarker("runtime:runId=\(adapterRun):nonce=boot-123-abc:phase=close") }
  private var adapterZeros: String {
    "{" + AdapterStartupGate.resourceFields.sorted().map { "\"\($0)\":0" }.joined(separator: ",") + "}"
  }
  private var adapterOffMarkers: [String] {
    [adapterStart,
     adapterMarker("off:event:{\"kind\":\"adapterSnapshotChanged\",\"commandId\":\"31000\",\"adapterState\":\"PoweredOff\",\"scanAuthorization\":\"Authorized\"}"),
     adapterMarker("off:event:{\"kind\":\"commandCompleted\",\"commandId\":\"31000\"}"),
     adapterMarker("off:event:{\"kind\":\"commandCompleted\",\"commandId\":\"31002\",\"error\":{\"category\":\"adapter_unavailable\",\"message\":\"synthetic adapter unavailable\"}}"),
     adapterMarker("off:resources:" + adapterZeros), adapterMarker("off:passed"),
     adapterMarker("awaiting-on:runId=\(adapterRun)")]
  }
  private var adapterReadyURL: String { "blehub-rn-spike://adapter/\(adapterRun)/ready" }
  private var adapterOnURL: String { "blehub-rn-spike://adapter/\(adapterRun)/on" }
  private var adapterHostReady: String { adapterMarker("host-window-ready:runId=\(adapterRun):source=linking") }
  private var adapterOnRequest: String { adapterMarker("on-request:runId=\(adapterRun):source=ui") }
  private var adapterIoEnvelopes: [String] {
    ["rf:start:serviceUuid=\(adapterBoot):expectedLocalName=BleHubNrf52840:initCommandId=9001:startCommandId=9002:stopCommandId=9003:scanSessionId=9004",
     "rf:listener:registered-before-initialize",
     "rf:step:initialize:passed:commandId=9001:adapterState=PoweredOn:scanAuthorization=Authorized",
     "rf:step:scan-start:passed:startCommandId=9002:scanSessionId=9004",
     "rf:step:advertisement:passed:scanSessionId=9004:peripheralId=42:localName=missing:filterServiceUuid=\(adapterBoot):payloadAdvertisedServices=none:payloadContainsFilterUuid=false",
     "rf:step:scan-stop:passed:stopCommandId=9003:scanSessionId=9004:requested=true",
     "rf:step:silence:passed:scanSessionId=9004:observationMs=2000",
     "rf:step:connecting:passed:commandId=9005:connectionId=9007:peripheralId=42:serviceUuid=\(adapterBoot)",
     "rf:step:connected:passed:commandId=9005:connectionId=9007:peripheralId=42:serviceUuid=\(adapterBoot)",
     "rf:step:gatt-discovery:passed:commandId=9008:connectionId=9007:peripheralId=42:serviceUuid=\(adapterBoot):gattSessionId=9009:requestedGattSessionId=9009:serviceCount=1:discoveredServiceCount=2:characteristicCount=5:descriptorCount=2",
     "rf:step:read-initial:passed:commandId=9010:connectionId=9007:gattSessionId=9009:peripheralId=42:serviceUuid=\(adapterBoot):characteristicId=fff10000:value=00",
     "rf:step:write-response:passed:commandId=9011:connectionId=9007:gattSessionId=9009:peripheralId=42:serviceUuid=\(adapterBoot):characteristicId=fff10000:value=10",
     "rf:step:read-response:passed:commandId=9012:connectionId=9007:gattSessionId=9009:peripheralId=42:serviceUuid=\(adapterBoot):characteristicId=fff10000:value=10",
     "rf:step:write-without-response:passed:commandId=9013:connectionId=9007:gattSessionId=9009:peripheralId=42:serviceUuid=\(adapterBoot):characteristicId=fff10000:value=ab:terminal=local-submission",
     "rf:step:read-without-response:passed:commandId=9014:connectionId=9007:gattSessionId=9009:peripheralId=42:serviceUuid=\(adapterBoot):characteristicId=fff10000:value=ab",
     "rf:step:disconnected:passed:commandId=9006:connectionId=9007:peripheralId=42:serviceUuid=\(adapterBoot)",
     "rf:step:connection-silence:passed:connectionId=9007:peripheralId=42:serviceUuid=\(adapterBoot):observationMs=1000",
     "rf:listener:removed", "rf:close:resolved",
     "rf:step:close:passed:probe-closed=true:listener-removed=true",
     "rf-io:passed:serviceUuid=\(adapterBoot):peripheralId=42:connectionId=9007:connectCommandId=9005:gattCommandId=9008:readCommandId=9010:writeResponseCommandId=9011:writeWithoutResponseCommandId=9013:disconnectCommandId=9006:scanSessionId=9004"]
  }

  private func adapterDirectory() throws -> URL {
    let url = FileManager.default.temporaryDirectory.appendingPathComponent("adapter-tests-" + UUID().uuidString, isDirectory: true)
    try FileManager.default.createDirectory(at: url, withIntermediateDirectories: false)
    addTeardownBlock { try FileManager.default.removeItem(at: url) }
    return url
  }
  private func adapterEnvelope(_ journal: AdapterMarkerJournal) throws -> [String: Any] {
    try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: journal.url)) as? [String: Any])
  }

  func testAdapterStartupLaunchAdmissionRejectsUnauthorizedAmbiguousAndConflictingModes() throws {
    let launch = try XCTUnwrap(adapterLaunch())
    XCTAssertEqual(launch.properties["runtimeMode"], "rf-adapter-transition")
    XCTAssertEqual(launch.properties["rfAdapterMode"], "startup-off")
    XCTAssertEqual(launch.properties["rfServiceUuid"], adapterBoot)
    XCTAssertEqual(launch.properties["rfExpectedLocalName"], "BleHubNrf52840")
    let raw = try XCTUnwrap(launch.properties["rfAdapterNativeIdentity"]?.data(using: .utf8))
    let identity = try XCTUnwrap(JSONSerialization.jsonObject(with: raw) as? [String: Any])
    XCTAssertEqual(Set(identity.keys), Set(["run_id", "adapter_mode", "pid", "boot_id", "bundle_id", "executable", "source"]))
    XCTAssertEqual(identity["run_id"] as? String, adapterRun)
    XCTAssertEqual(identity["boot_id"] as? String, adapterBoot)
    XCTAssertEqual(identity["pid"] as? Int, 123)
    XCTAssertEqual(identity["source"] as? String, "launch")
    XCTAssertEqual(identity["executable"] as? String, "file:///Application%20Support/BleHubNitroHost.app/BleHubNitroHost")
    XCTAssertNil(adapterLaunch(authorized: false))
    XCTAssertNil(adapterLaunch(bundle: "foreign.host"))
    for url in ["https://example.test/host", "file://foreign/host", "file:///host?query=1", "file:///host/#fragment"] {
      XCTAssertNil(adapterLaunch(executable: try XCTUnwrap(URL(string: url))))
    }
    for index in stride(from: 1, to: adapterArguments.count, by: 2) {
      let option = adapterArguments[index]
      XCTAssertNil(adapterLaunch(adapterArguments + [option, adapterArguments[index + 1]]))
      var missing = adapterArguments
      missing.removeSubrange(index...index + 1)
      if option.hasSuffix("-mode") { XCTAssertNotNil(adapterLaunch(missing)) }
      else { XCTAssertNil(adapterLaunch(missing)) }
      var absentValue = adapterArguments
      absentValue.remove(at: index + 1)
      XCTAssertNil(adapterLaunch(absentValue))
    }
    for (index, value) in [(2, adapterRun.uppercased()), (4, "BAD"), (6, "active-loss"),
                            (8, ""), (8, " name"), (8, "bad\nname"), (8, String(repeating: "é", count: 65))] {
      var arguments = adapterArguments
      arguments[index] = value
      XCTAssertNil(adapterLaunch(arguments), value)
    }
    for option in ["--blehub-nitro-basic", "--blehub-nitro-stress", "--blehub-nitro-reload",
                   "--blehub-nitro-rf-io-service", "--blehub-nitro-rf-lab-suite-flow",
                   "--blehub-nitro-permission-phase", "--blehub-nitro-rf-write-reject-control",
                   "--blehub-nitro-rf-pending-read-reload-service", "--blehub-nitro-rf-adapter-unknown"] {
      XCTAssertNil(adapterLaunch(adapterArguments + [option, "value"]), option)
    }
    XCTAssertFalse(AdapterStartupLaunch.requested(["host", "--blehub-nitro-basic"]))
  }

  func testAdapterStartupExactReadyAndOnPathsRequireEveryCommittedPhase() throws {
    for count in 0..<adapterOffMarkers.count {
      var gate = AdapterStartupGate(runId: adapterRun)
      adapterOffMarkers.prefix(count).forEach { gate.observe($0) }
      XCTAssertNil(gate.accept(adapterReadyURL))
      adapterOffMarkers.dropFirst(count).forEach { gate.observe($0) }
      XCTAssertNil(gate.accept(adapterReadyURL))
      XCTAssertTrue(gate.failed)
    }
    for target in ["ready", "on"] {
      var prepared = AdapterStartupGate(runId: adapterRun)
      adapterOffMarkers.forEach { prepared.observe($0) }
      if target == "on" {
        XCTAssertEqual(prepared.accept(adapterReadyURL), "ready")
        prepared.observe(adapterHostReady)
        prepared.observe(adapterOnRequest)
      }
      let exact = target == "ready" ? adapterReadyURL : adapterOnURL
      for invalid in [exact + "/", exact + "?x=1", exact + "#fragment", exact.uppercased(),
                      exact.replacingOccurrences(of: adapterRun, with: adapterBoot),
                      exact.replacingOccurrences(of: "/" + target, with: "/%" + (target == "ready" ? "72eady" : "6Fn")),
                      "blehub-rn-spike://adapter/\(adapterRun)/active-loss/on", "blehub-rn-spike://reload"] {
        var gate = prepared
        XCTAssertNil(gate.accept(invalid), invalid)
        XCTAssertNil(gate.accept(exact))
      }
      var gate = prepared
      XCTAssertNotNil(gate.accept(exact))
      XCTAssertNil(gate.accept(exact))
      XCTAssertTrue(gate.failed)
    }
    var gate = AdapterStartupGate(runId: adapterRun)
    adapterOffMarkers.forEach { gate.observe($0) }
    XCTAssertEqual(gate.accept(adapterReadyURL), "ready")
    XCTAssertNil(gate.accept(adapterOnURL))
    gate.observe(adapterHostReady)
    gate.observe(adapterOnRequest)
    XCTAssertNil(gate.accept(adapterOnURL))
  }

  func testAdapterStartupNonceMarkersAndSeventeenZeroBoundaryFailSticky() {
    let wrongResources = [adapterZeros.replacingOccurrences(of: ":0", with: ":false"),
      adapterZeros.replacingOccurrences(of: ":0", with: ":-0"), adapterZeros.replacingOccurrences(of: ":0", with: ":0.0"),
      adapterZeros.replacingOccurrences(of: ":0", with: ":1"),
      adapterZeros.replacingOccurrences(of: "activeScans", with: "foreignField"),
      String(adapterZeros.dropLast()) + ",\"activeScans\":0}", "{}"]
    for raw in wrongResources {
      var gate = AdapterStartupGate(runId: adapterRun)
      gate.observe(adapterStart)
      gate.observe(adapterMarker("off:resources:" + raw))
      gate.observe(adapterMarker("off:passed"))
      gate.observe(adapterMarker("awaiting-on:runId=\(adapterRun)"))
      XCTAssertTrue(gate.failed, raw)
      XCTAssertNil(gate.accept(adapterReadyURL))
    }
    for marker in [adapterStart, adapterStart.replacingOccurrences(of: "boot-123-abc", with: "different"),
                   adapterStart.replacingOccurrences(of: adapterRun, with: adapterBoot),
                   adapterStart.replacingOccurrences(of: "phase=start", with: "phase=unknown"),
                   adapterMarker("off:passed"), adapterMarker("unknown"), adapterHostReady, adapterOnRequest,
                   adapterMarker("awaiting-on:runId=\(adapterBoot)"), adapterMarker("failed:cancelled")] {
      var gate = AdapterStartupGate(runId: adapterRun)
      gate.observe(adapterStart)
      gate.observe(marker)
      XCTAssertTrue(gate.failed, marker)
      XCTAssertNil(gate.accept(adapterReadyURL))
    }
    var gate = AdapterStartupGate(runId: adapterRun)
    adapterOffMarkers.forEach { gate.observe($0) }
    gate.observe(adapterMarker("off:resources:" + adapterZeros))
    XCTAssertTrue(gate.failed)
    for marker in [adapterHostReady, adapterOnRequest] {
      var duplicate = AdapterStartupGate(runId: adapterRun)
      adapterOffMarkers.forEach { duplicate.observe($0) }
      XCTAssertNotNil(duplicate.accept(adapterReadyURL))
      duplicate.observe(adapterHostReady)
      if marker == adapterOnRequest { duplicate.observe(adapterOnRequest) }
      duplicate.observe(marker)
      XCTAssertTrue(duplicate.failed)
      XCTAssertNil(duplicate.accept(adapterOnURL))
    }
  }

  func testAdapterStartupCloseNeverReopensAndOnlyExactPostCloseTerminalIsAllowed() {
    for close in [adapterClose, adapterClose.replacingOccurrences(of: "boot-123-abc", with: "foreign")] {
      var gate = AdapterStartupGate(runId: adapterRun)
      adapterOffMarkers.forEach { gate.observe($0) }
      gate.observe(close)
      XCTAssertNil(gate.accept(adapterReadyURL))
      XCTAssertNil(gate.accept(adapterOnURL))
      XCTAssertTrue(gate.failed)
    }
    var complete = AdapterStartupGate(runId: adapterRun)
    adapterOffMarkers.forEach { complete.observe($0) }
    XCTAssertEqual(complete.accept(adapterReadyURL), "ready")
    complete.observe(adapterHostReady)
    complete.observe(adapterOnRequest)
    XCTAssertEqual(complete.accept(adapterOnURL), "signal")
    complete.observe(adapterMarker("on-signal:runId=\(adapterRun):source=linking"))
    complete.observe(adapterMarker("on:started"))
    adapterIoEnvelopes.forEach { complete.observe(adapterMarker("on:flow:" + $0)) }
    complete.observe(adapterMarker("on:resources:" + adapterZeros))
    complete.observe(adapterMarker("on:passed"))
    complete.observe(adapterClose)
    XCTAssertFalse(complete.failed)
    var unknown = complete
    unknown.observe(adapterHostReady)
    XCTAssertTrue(unknown.failed)
    complete.observe(adapterMarker("passed:runId=\(adapterRun)"))
    XCTAssertFalse(complete.failed)
    complete.observe(adapterMarker("passed:runId=\(adapterRun)"))
    XCTAssertTrue(complete.failed)
  }

  func testAdapterStartupAcceptanceIsDurableBeforeLinkingAndKeepsRealCallbackSource() throws {
    let launch = try XCTUnwrap(adapterLaunch())
    let journal = try AdapterMarkerJournal(directory: adapterDirectory(), runId: adapterRun)
    let session = AdapterStartupSession(identity: launch.identity, journal: journal)
    let bootstrap = adapterMarker("native:bootstrap:" + launch.properties["rfAdapterNativeIdentity"]!)
    XCTAssertTrue(session.capture(bootstrap, fromJavaScript: false))
    adapterOffMarkers.forEach { XCTAssertTrue(session.capture($0, fromJavaScript: true)) }
    var forwarded = 0
    func deliver(_ url: String, kind: String, acknowledgement: String) -> Bool {
      session.deliver(url, record: { session.capture($0, fromJavaScript: false) }, forward: {
        do {
          let envelope = try self.adapterEnvelope(journal)
          let records = try XCTUnwrap(envelope["records"] as? [String])
          let last = try XCTUnwrap(records.last)
          let prefix = self.adapterMarker("native:" + kind + ":")
          XCTAssertTrue(last.hasPrefix(prefix))
          let record = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(last.dropFirst(prefix.count).utf8)) as? [String: Any])
          XCTAssertEqual(Set(record.keys), Set(["run_id", "adapter_mode", "pid", "boot_id", "bundle_id", "executable", "source", "url", "nonce"]))
          XCTAssertEqual(record["source"] as? String, "application-open")
          XCTAssertEqual(record["url"] as? String, url)
          XCTAssertEqual(record["nonce"] as? String, "boot-123-abc")
          XCTAssertEqual(record["pid"] as? Int, 123)
          XCTAssertEqual(record["boot_id"] as? String, self.adapterBoot)
          XCTAssertTrue(envelope["error"] is NSNull)
        } catch { XCTFail("acceptance was not committed before Linking: \(error)"); return false }
        forwarded += 1
        return session.capture(acknowledgement, fromJavaScript: true)
      })
    }
    XCTAssertTrue(deliver(adapterReadyURL, kind: "ready", acknowledgement: adapterHostReady))
    XCTAssertTrue(session.capture(adapterOnRequest, fromJavaScript: true))
    XCTAssertTrue(deliver(adapterOnURL, kind: "signal", acknowledgement: adapterMarker("on-signal:runId=\(adapterRun):source=linking")))
    XCTAssertEqual(forwarded, 2)
    XCTAssertTrue(session.capture(adapterMarker("on:started"), fromJavaScript: true))
    adapterIoEnvelopes.forEach { XCTAssertTrue(session.capture(adapterMarker("on:flow:" + $0), fromJavaScript: true)) }
    for line in [adapterMarker("on:resources:" + adapterZeros), adapterMarker("on:passed"),
                 adapterClose, adapterMarker("passed:runId=\(adapterRun)")] {
      XCTAssertTrue(session.capture(line, fromJavaScript: true))
    }
    XCTAssertNil(session.failure)
    let crossing = try Data(contentsOf: journal.url)
    let attachment = XCTAttachment(data: crossing, uniformTypeIdentifier: "public.json")
    attachment.name = "ios-adapter-journal-crossing"
    attachment.lifetime = .keepAlways
    add(attachment)
    XCTAssertFalse(session.deliver(adapterOnURL, record: { _ in XCTFail("duplicate acceptance"); return true }, forward: { XCTFail("duplicate Linking"); return true }))
    XCTAssertNotNil(session.failure)
    XCTAssertNotNil(try adapterEnvelope(journal)["error"] as? String)
  }

  func testAdapterStartupLinkingCallbackCanSynchronouslyCaptureOnAnotherQueue() throws {
    let journal = try AdapterMarkerJournal(directory: adapterDirectory(), runId: adapterRun)
    let session = AdapterStartupSession(identity: try XCTUnwrap(adapterLaunch()).identity, journal: journal)
    adapterOffMarkers.forEach { XCTAssertTrue(session.capture($0, fromJavaScript: true)) }
    let completed = DispatchSemaphore(value: 0)
    let delivered = session.deliver(adapterReadyURL, record: { session.capture($0, fromJavaScript: false) }, forward: {
      DispatchQueue.global().async {
        XCTAssertTrue(session.capture(self.adapterHostReady, fromJavaScript: true))
        completed.signal()
      }
      return completed.wait(timeout: .now() + 2) == .success
    })
    XCTAssertTrue(delivered)
    XCTAssertNil(session.failure)
    XCTAssertTrue(session.capture(adapterOnRequest, fromJavaScript: true))
  }

  func testAdapterStartupRealIoFamiliesRejectUnknownFailureAndMalformedNonce() {
    var gate = AdapterStartupGate(runId: adapterRun)
    adapterOffMarkers.forEach { gate.observe($0) }
    XCTAssertEqual(gate.accept(adapterReadyURL), "ready")
    gate.observe(adapterHostReady)
    gate.observe(adapterOnRequest)
    XCTAssertEqual(gate.accept(adapterOnURL), "signal")
    gate.observe(adapterMarker("on-signal:runId=\(adapterRun):source=linking"))
    gate.observe(adapterMarker("on:started"))
    for body in ["rf:step:read-initial:failed:error", "rf:close:rejected:error", "rf-io:failed:error",
                 "unknown:passed", "rf-lab-full:passed"] {
      var invalid = gate
      invalid.observe(adapterMarker("on:flow:" + body))
      XCTAssertTrue(invalid.failed, body)
    }
    adapterIoEnvelopes.forEach { gate.observe(adapterMarker("on:flow:" + $0)) }
    XCTAssertFalse(gate.failed)
    for nonce in ["boot-123", "Boot-123-abc", "boot_123-abc-def", "é-123-abc", "a-b-c:phase=close"] {
      var invalid = AdapterStartupGate(runId: adapterRun)
      invalid.observe(adapterStart.replacingOccurrences(of: "boot-123-abc", with: nonce))
      XCTAssertTrue(invalid.failed, nonce)
    }
  }

  func testAdapterStartupLateRequestCannotRepairEarlyURLAndCommitOrLinkingFailureBlocksReplay() throws {
    for failure in ["early-url", "record", "journal", "linking"] {
      var rejectWrite = false
      let journal = try AdapterMarkerJournal(directory: adapterDirectory(), runId: adapterRun, write: { data, url, options in
        if rejectWrite { throw CocoaError(.fileWriteOutOfSpace) }
        try data.write(to: url, options: options)
      })
      let session = AdapterStartupSession(identity: try XCTUnwrap(adapterLaunch()).identity, journal: journal)
      adapterOffMarkers.forEach { XCTAssertTrue(session.capture($0, fromJavaScript: true)) }
      XCTAssertTrue(session.deliver(adapterReadyURL, record: { session.capture($0, fromJavaScript: false) }, forward: { true }))
      XCTAssertTrue(session.capture(adapterHostReady, fromJavaScript: true))
      if failure != "early-url" { XCTAssertTrue(session.capture(adapterOnRequest, fromJavaScript: true)) }
      rejectWrite = failure == "journal"
      var forwarded = 0
      XCTAssertFalse(session.deliver(adapterOnURL, record: { line in
        if failure == "record" { return false }
        return session.capture(line, fromJavaScript: false)
      }, forward: { forwarded += 1; return false }))
      XCTAssertEqual(forwarded, failure == "linking" ? 1 : 0)
      XCTAssertFalse(session.capture(adapterOnRequest, fromJavaScript: true))
      XCTAssertFalse(session.deliver(adapterOnURL, record: { _ in XCTFail("failed session recorded acceptance"); return true },
        forward: { XCTFail("failed session forwarded URL"); return true }))
      XCTAssertNotNil(session.failure)
      if failure == "journal" {
        // Persistent disk failure cannot publish an error envelope, but never authorizes Linking.
        XCTAssertEqual(journal.error, "journal-write-error")
        let records = try XCTUnwrap(adapterEnvelope(journal)["records"] as? [String])
        XCTAssertFalse(records.contains { $0.hasPrefix(self.adapterMarker("native:signal:")) })
      } else { XCTAssertNotNil(try adapterEnvelope(journal)["error"] as? String) }
    }
  }

  func testAdapterJournalAtomicEnvelopePreservesHistoricalAttemptAndRawPrefixes() throws {
    let directory = try adapterDirectory()
    var options: [Data.WritingOptions] = []
    let journal = try AdapterMarkerJournal(directory: directory, runId: adapterRun, write: { data, url, option in
      options.append(option)
      try data.write(to: url, options: option)
      _ = try JSONSerialization.jsonObject(with: Data(contentsOf: url))
    })
    let raw = adapterStart
    XCTAssertTrue(journal.append(raw))
    let envelope = try adapterEnvelope(journal)
    XCTAssertEqual(Set(envelope.keys), Set(["schema_version", "run_id", "records", "error"]))
    XCTAssertEqual(envelope["schema_version"] as? Int, 1)
    XCTAssertEqual(envelope["run_id"] as? String, adapterRun)
    XCTAssertEqual(envelope["records"] as? [String], [raw])
    XCTAssertTrue(envelope["error"] is NSNull)
    XCTAssertEqual(options, [.withoutOverwriting, .atomic])
    let before = try Data(contentsOf: journal.url)
    XCTAssertThrowsError(try AdapterMarkerJournal(directory: directory, runId: adapterRun))
    XCTAssertEqual(try Data(contentsOf: journal.url), before)
    let linkDirectory = try adapterDirectory()
    let destination = linkDirectory.appendingPathComponent(adapterRun + ".json")
    try FileManager.default.createSymbolicLink(at: destination, withDestinationURL: linkDirectory.appendingPathComponent("missing"))
    XCTAssertThrowsError(try AdapterMarkerJournal(directory: linkDirectory, runId: adapterRun))
  }

  func testAdapterJournalRecordUtf8ByteCapsAndWriteErrorsLatchWithoutTruncation() throws {
    let journal = try AdapterMarkerJournal(directory: adapterDirectory(), runId: adapterRun)
    for _ in 0..<AdapterMarkerJournal.maxRecords { XCTAssertTrue(journal.append(adapterStart)) }
    XCTAssertFalse(journal.append("overflow"))
    XCTAssertEqual((try adapterEnvelope(journal)["records"] as? [String])?.count, 256)
    XCTAssertNotNil(try adapterEnvelope(journal)["error"] as? String)
    XCTAssertFalse(journal.append(adapterMarker("passed:runId=\(adapterRun)")))
    for line in [String(repeating: "é", count: 16385), "one\ntwo"] {
      let bounded = try AdapterMarkerJournal(directory: adapterDirectory(), runId: adapterRun)
      XCTAssertFalse(bounded.append(line))
      XCTAssertEqual(try adapterEnvelope(bounded)["records"] as? [String], [])
      XCTAssertNotNil(bounded.error)
    }
    let bytes = try AdapterMarkerJournal(directory: adapterDirectory(), runId: adapterRun)
    let line = String(repeating: "x", count: AdapterMarkerJournal.maxLineBytes)
    var count = 0
    while bytes.append(line) { count += 1 }
    XCTAssertEqual(count, 31)
    XCTAssertEqual((try adapterEnvelope(bytes)["records"] as? [String])?.count, count)
    XCTAssertLessThanOrEqual(try Data(contentsOf: bytes.url).count, AdapterMarkerJournal.maxBytes)
    XCTAssertEqual(bytes.error, "journal-byte-overflow")
    var writes = 0
    let broken = try AdapterMarkerJournal(directory: adapterDirectory(), runId: adapterRun, write: { data, url, options in
      writes += 1
      if writes == 3 { throw CocoaError(.fileWriteOutOfSpace) }
      try data.write(to: url, options: options)
    })
    XCTAssertTrue(broken.append(adapterStart))
    XCTAssertFalse(broken.append(adapterMarker("off:passed")))
    let saved = try adapterEnvelope(broken)
    XCTAssertEqual(saved["records"] as? [String], [adapterStart])
    XCTAssertEqual(saved["error"] as? String, "journal-write-error")
    XCTAssertFalse(broken.append(adapterMarker("passed:runId=\(adapterRun)")))
    XCTAssertEqual(writes, 4)
  }
}
