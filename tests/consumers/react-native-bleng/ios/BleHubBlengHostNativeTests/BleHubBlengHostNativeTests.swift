import BleHubUniFfi
import NitroModules
import XCTest
@testable import BleHubReactNativeBleng

final class BleHubBlengHostNativeTests: XCTestCase {
  func testDrainBlengEventsDeliversEventsAfterObservationTimeoutsUntilEOF() async throws {
    let first = BleEvent.scanStarted(commandId: CommandId(value: 1), scanId: ScanSessionId(value: 2))
    let second = BleEvent.commandCompleted(commandId: CommandId(value: 1), error: nil)
    let sample = BlengEventReadSample([
      .failure(BlengError.Timeout),
      .failure(BlengError.Timeout),
      .success(first),
      .failure(BlengError.Timeout),
      .success(second),
      .success(nil),
    ])
    let evidence = ScanWiringEvidence()

    try await drainBlengEvents(
      nextEvent: { try await sample.next() },
      onEvent: { evidence.record($0) }
    )

    XCTAssertEqual(evidence.snapshot().raw, [first, second])
    let readCount = await sample.readCount
    XCTAssertEqual(readCount, 6)
  }

  func testDrainBlengEventsDoesNotReadPastEOF() async throws {
    let sample = BlengEventReadSample([
      .success(nil),
      .success(.commandCompleted(commandId: CommandId(value: 1), error: nil)),
    ])
    let evidence = ScanWiringEvidence()

    try await drainBlengEvents(
      nextEvent: { try await sample.next() },
      onEvent: { evidence.record($0) }
    )

    XCTAssertTrue(evidence.snapshot().raw.isEmpty)
    let readCount = await sample.readCount
    XCTAssertEqual(readCount, 1)
  }

  func testDrainBlengEventsPropagatesReadTransportAndWorkerFailures() async throws {
    for failure in [BlengError.TransportFault, .WorkerStopped] {
      let sample = BlengEventReadSample([.failure(failure), .success(nil)])
      let evidence = ScanWiringEvidence()
      do {
        try await drainBlengEvents(
          nextEvent: { try await sample.next() },
          onEvent: { evidence.record($0) }
        )
        XCTFail("Read failure must propagate")
      } catch let error as BlengError {
        switch (failure, error) {
        case (.TransportFault, .TransportFault), (.WorkerStopped, .WorkerStopped):
          break
        default:
          XCTFail("Unexpected read failure: \(error)")
        }
      }
      XCTAssertTrue(evidence.snapshot().raw.isEmpty)
      let readCount = await sample.readCount
      XCTAssertEqual(readCount, 1)
    }
  }

  func testDrainBlengEventsPropagatesReadCancellation() async throws {
    let failures: [any Error] = [CancellationError(), BlengError.Cancelled]
    for failure in failures {
      let sample = BlengEventReadSample([.failure(failure), .success(nil)])
      let evidence = ScanWiringEvidence()
      do {
        try await drainBlengEvents(
          nextEvent: { try await sample.next() },
          onEvent: { evidence.record($0) }
        )
        XCTFail("Read cancellation must propagate")
      } catch {
        switch (failure, error) {
        case (is CancellationError, is CancellationError), (BlengError.Cancelled, BlengError.Cancelled):
          break
        default:
          XCTFail("Unexpected read cancellation: \(error)")
        }
      }
      XCTAssertTrue(evidence.snapshot().raw.isEmpty)
      let readCount = await sample.readCount
      XCTAssertEqual(readCount, 1)
    }
  }

  func testDrainBlengEventsPropagatesDeliveryTimeoutWithoutReadingAgain() async throws {
    let event = BleEvent.commandCompleted(commandId: CommandId(value: 1), error: nil)
    let sample = BlengEventReadSample([.success(event), .success(event), .success(nil)])
    let evidence = ScanWiringEvidence()

    do {
      try await drainBlengEvents(
        nextEvent: { try await sample.next() },
        onEvent: {
          evidence.record($0)
          throw BlengError.Timeout
        }
      )
      XCTFail("Delivery timeout must propagate, not become an idle read")
    } catch BlengError.Timeout {
      // The same typed error is terminal when it comes from delivery.
    }

    XCTAssertEqual(evidence.snapshot().raw, [event])
    let readCount = await sample.readCount
    XCTAssertEqual(readCount, 1)
  }

  func testDrainBlengEventsTaskCancellationStopsBeforeNextReadAfterTimeout() async throws {
    let sample = BlengEventReadSample([
      .failure(BlengError.Timeout),
      .success(.commandCompleted(commandId: CommandId(value: 1), error: nil)),
      .success(nil),
    ])
    let evidence = ScanWiringEvidence()
    let reader = Task {
      try await drainBlengEvents(
        nextEvent: {
          // Cancel inside the first read so cancellation precedes the next loop check deterministically.
          withUnsafeCurrentTask { $0?.cancel() }
          return try await sample.next()
        },
        onEvent: { evidence.record($0) }
      )
    }

    do {
      try await reader.value
      XCTFail("Cancelled reader must stop before reading again")
    } catch is CancellationError {
    }

    XCTAssertTrue(evidence.snapshot().raw.isEmpty)
    let readCount = await sample.readCount
    XCTAssertEqual(readCount, 1)
  }

  func testPeerCacheRetirementRequiresAnExplicitZeroCount() async throws {
    let peerCacheCounts: [Int?] = [nil, 0, 1]
    for peerCacheCount in peerCacheCounts {
      let backend = ScanWiringBackend()
      let owner = try AppleRustSupervisorOwner(
        queueLabel: "BleHubPeerCacheRetirementNoRadio",
        backendFactory: { _ in
          ApplePlatformBackendLease(
            backend: backend,
            resourceSnapshot: {
              var resources = backend.resources()
              resources["peerCacheEntries"] = peerCacheCount
              return resources
            },
            recreateCentral: { ["recreated": "false"] }
          )
        }
      )
      addTeardownBlock { _ = await owner.closeResult() }
      _ = try await owner.start()
      let expectedFailure: String?
      switch peerCacheCount {
      case nil: expectedFailure = "missing_resource"
      case 0: expectedFailure = nil
      default: expectedFailure = "native_resources_not_zero"
      }
      for _ in 0..<2 {
        let receipt = await owner.closeResult()
        XCTAssertTrue(receipt.ownerClosed)
        XCTAssertEqual(receipt.failure?.code, expectedFailure)
        XCTAssertEqual(backend.disposeCount, 1)
      }
    }
  }

  func testFormalRustSupervisorOwnerStartsAndClosesWithoutProbeSurface() async throws {
    let owner = try AppleRustSupervisorOwner(queueLabel: "BleHubFormalConsumerNativeContract")
    addTeardownBlock {
      let receipt = await owner.closeResult()
      XCTAssertTrue(receipt.ownerClosed)
      XCTAssertNil(receipt.failure)
    }

    let start = try await owner.start()
    XCTAssertFalse(start.context.ownerId.isEmpty)
    XCTAssertFalse(start.context.backendId.isEmpty)
    let session = try XCTUnwrap(owner.factory.currentSessionForHost())
    _ = try session.activeOwnerForHost()

    let resources = try await owner.close()
    XCTAssertEqual(
      Set(resources.map(\.name)),
      Set(ApplePlatformSession.requiredZeroResourceNames)
    )
    XCTAssertTrue(resources.allSatisfy { $0.count == 0 })
    for _ in 0..<2 {
      let receipt = await owner.closeResult()
      XCTAssertTrue(receipt.ownerClosed)
      XCTAssertNil(receipt.failure)
    }
  }

  func testSingleReaderFeedsScanSinkWithoutLosingLegacyTerminals() async throws {
    let backend = ScanWiringBackend()
    let evidence = ScanWiringEvidence()
    let scansDelivered = expectation(description: "all three scan envelopes delivered")
    scansDelivered.expectedFulfillmentCount = 3
    let owner = try AppleRustSupervisorOwner(
      queueLabel: "BleHubScanWiringNoRadio",
      eventSink: { evidence.record($0) },
      scanEventSink: {
        evidence.record($0)
        scansDelivered.fulfill()
      },
      backendFactory: { _ in
        ApplePlatformBackendLease(
          backend: backend,
          resourceSnapshot: { backend.resources() },
          recreateCentral: { ["recreated": "false"] }
        )
      }
    )
    addTeardownBlock { _ = try await owner.close() }
    let initialized = try await owner.start()
    let session = try XCTUnwrap(owner.factory.currentSessionForHost())
    let start = try owner.supervisor.beginStartScan(
      context: initialized.context,
      options: ScanOptions(serviceUuids: [], namePrefix: nil, exactName: nil, companyId: nil, minRssi: nil, manufacturerData: nil, android: nil, allowDuplicates: false)
    )
    let scan = try await start.wait(timeoutMs: 2_000)
    let stop = try owner.supervisor.beginStopScan(context: initialized.context, session: scan)
    try await stop.wait(timeoutMs: 2_000)
    await fulfillment(of: [scansDelivered], timeout: 2)
    // This is the legacy inbox, not another nextEvent reader.
    let terminal = try await session.executeRecoveryAction(
      sequence: 7, action: .awaitOldOperationTerminals(remaining: [String(stop.commandId().value)])
    )
    XCTAssertEqual(terminal, ForeignPlatformActionResult(
      sequence: 7, signal: .oldOperationTerminal(operationId: String(stop.commandId().value))
    ))
    let snapshot = evidence.snapshot()
    XCTAssertEqual(snapshot.scans, [
      ForeignScanEventEnvelope(
        sourceContext: initialized.context, event: .started(commandId: start.commandId(), scanId: scan)
      ),
      ForeignScanEventEnvelope(
        sourceContext: initialized.context, event: .advertisement(advertisement: backend.advertisement(scan))
      ),
      ForeignScanEventEnvelope(
        sourceContext: initialized.context,
        event: .stopped(scanId: scan, result: .ok(reason: .requested(commandId: stop.commandId())))
      ),
    ])
    XCTAssertEqual(snapshot.raw.count, 7)
    XCTAssertEqual(snapshot.raw, [
      .adapterSnapshotChanged(
        commandId: CommandId(value: initialized.initializationCommandId), snapshot: initialized.adapterSnapshot
      ),
      .commandCompleted(commandId: CommandId(value: initialized.initializationCommandId), error: nil),
      .scanStarted(commandId: start.commandId(), scanId: scan),
      .advertisementObserved(advertisement: backend.advertisement(scan)),
      .commandCompleted(commandId: start.commandId(), error: nil),
      .scanStopped(scanId: scan, result: .ok(reason: .requested(commandId: stop.commandId()))),
      .commandCompleted(commandId: stop.commandId(), error: nil),
    ])
    XCTAssertEqual(snapshot.raw.compactMap { event -> CommandId? in
      if case .commandCompleted(let id, nil) = event { return id }
      return nil
    }, [CommandId(value: initialized.initializationCommandId), start.commandId(), stop.commandId()])
  }

  func testFailedRecoveryCaptureDoesNotEndSingleReaderDuringOrdinaryScan() async throws {
    let backend = ScanWiringBackend(rejectFirstScan: true)
    let evidence = ScanWiringEvidence()
    let advertisementReceipt = DispatchSemaphore(value: 0)
    let owner = try AppleRustSupervisorOwner(
      queueLabel: "BleHubIdleRecoveryScanNoRadio",
      eventSink: {
        evidence.record($0)
        if case .advertisementObserved = $0 { advertisementReceipt.signal() }
      },
      scanEventSink: { evidence.record($0) },
      backendFactory: { _ in
        ApplePlatformBackendLease(
          backend: backend,
          resourceSnapshot: { backend.resources() },
          recreateCentral: { ["recreated": "false"] }
        )
      }
    )
    addTeardownBlock {
      let resources = try await owner.close()
      XCTAssertEqual(Set(resources.map(\.name)), Set(ApplePlatformSession.requiredZeroResourceNames))
      XCTAssertTrue(resources.allSatisfy { $0.count == 0 })
    }
    let initialized = try await owner.start()
    let session = try XCTUnwrap(owner.factory.currentSessionForHost())
    let options = ScanOptions(serviceUuids: [], namePrefix: nil, exactName: nil, companyId: nil, minRssi: nil, manufacturerData: nil, android: nil, allowDuplicates: false)
    // Exercise production capture cleanup before startScan fails, not Rust recovery policy or RF.
    let failedRecovery = try await session.executeRecoveryAction(
      sequence: 8,
      action: .attemptSameOwnerRecovery(
        afterCentralRecreation: false,
        target: ForeignRecoveryTarget(
          scanOptions: options,
          serviceUuid: BluetoothUuid(value: "0000180f-0000-1000-8000-00805f9b34fb"),
          characteristicUuid: BluetoothUuid(value: "00002a19-0000-1000-8000-00805f9b34fb"),
          notificationMode: .platformDefault,
          expectedValue: Data([0, 0x80, 0xff])
        )
      )
    )
    XCTAssertEqual(failedRecovery.sequence, 8)
    guard case let .sameOwnerRecoveryFailed(reason) = failedRecovery.signal else {
      return XCTFail("Expected the fixture's recovery scan rejection, got \(failedRecovery)")
    }
    XCTAssertTrue(reason.contains(ScanWiringBackend.rejectedScanDetail), reason)

    let start = try owner.supervisor.beginStartScan(context: initialized.context, options: options)
    let scan = try await start.wait(timeoutMs: 2_000)
    // startScan emits advertisement zero. Handshake every receipt so the Rust public FIFO
    // never sees a 65-event burst; only an idle/leaked recovery capture can accumulate them.
    for sequence in UInt8(0)..<65 {
      if sequence > 0 { try backend.emitAdvertisement(scan, sequence: sequence) }
      let received = await Task.detached {
        ScanWiringEvidence.waitForReceipt(advertisementReceipt)
      }.value
      guard received else {
        return XCTFail("Single native reader did not deliver advertisement \(sequence)")
      }
    }
    let stop = try owner.supervisor.beginStopScan(context: initialized.context, session: scan)
    try await stop.wait(timeoutMs: 2_000)
    // Operation wait is not a reader flush. Consume the real stop terminal via the original inbox.
    let terminal = try await session.executeRecoveryAction(
      sequence: 9, action: .awaitOldOperationTerminals(remaining: [String(stop.commandId().value)])
    )
    XCTAssertEqual(terminal, ForeignPlatformActionResult(
      sequence: 9, signal: .oldOperationTerminal(operationId: String(stop.commandId().value))
    ))
    let snapshot = evidence.snapshot()
    let advertisements = (UInt8(0)..<65).map { backend.advertisement(scan, sequence: $0) }
    XCTAssertEqual(snapshot.scans.compactMap { envelope -> AdvertisementObservation? in
      if case let .advertisement(advertisement) = envelope.event { return advertisement }
      return nil
    }, advertisements)
    XCTAssertEqual(snapshot.raw.compactMap { event -> AdvertisementObservation? in
      if case let .advertisementObserved(advertisement) = event { return advertisement }
      return nil
    }, advertisements)
    XCTAssertTrue(snapshot.scans.allSatisfy { $0.sourceContext == initialized.context })
    XCTAssertTrue(snapshot.scans.contains(ForeignScanEventEnvelope(
      sourceContext: initialized.context, event: .started(commandId: start.commandId(), scanId: scan)
    )))
    XCTAssertEqual(snapshot.scans.last, ForeignScanEventEnvelope(
      sourceContext: initialized.context,
      event: .stopped(scanId: scan, result: .ok(reason: .requested(commandId: stop.commandId())))
    ))
    XCTAssertEqual(Array(snapshot.raw.suffix(2)), [
      .scanStopped(scanId: scan, result: .ok(reason: .requested(commandId: stop.commandId()))),
      .commandCompleted(commandId: stop.commandId(), error: nil),
    ])
    XCTAssertTrue(owner.factory.currentSessionForHost() === session)
  }

  func testCancelledRecoveryStopsScanWithoutEndingSingleReader() async throws {
    let backend = ScanWiringBackend(emitOnStart: false)
    let evidence = ScanWiringEvidence()
    let scanStartedReceipt = DispatchSemaphore(value: 0)
    let scanStoppedReceipt = DispatchSemaphore(value: 0)
    let advertisementReceipt = DispatchSemaphore(value: 0)
    let owner = try AppleRustSupervisorOwner(
      queueLabel: "BleHubCancelledRecoveryScanNoRadio",
      eventSink: {
        evidence.record($0)
        switch $0 {
        case .scanStarted: scanStartedReceipt.signal()
        case .scanStopped: scanStoppedReceipt.signal()
        case .advertisementObserved: advertisementReceipt.signal()
        default: break
        }
      },
      scanEventSink: { evidence.record($0) },
      backendFactory: { _ in
        ApplePlatformBackendLease(
          backend: backend,
          resourceSnapshot: { backend.resources() },
          recreateCentral: { ["recreated": "false"] }
        )
      }
    )
    addTeardownBlock {
      let resources = try await owner.close()
      XCTAssertEqual(Set(resources.map(\.name)), Set(ApplePlatformSession.requiredZeroResourceNames))
      XCTAssertTrue(resources.allSatisfy { $0.count == 0 })
    }
    let initialized = try await owner.start()
    let session = try XCTUnwrap(owner.factory.currentSessionForHost())
    let options = ScanOptions(serviceUuids: [], namePrefix: nil, exactName: nil, companyId: nil, minRssi: nil, manufacturerData: nil, android: nil, allowDuplicates: false)
    let recovery = Task {
      try await session.executeRecoveryAction(
        sequence: 10,
        action: .attemptSameOwnerRecovery(
          afterCentralRecreation: false,
          target: ForeignRecoveryTarget(
            scanOptions: options,
            serviceUuid: BluetoothUuid(value: "0000180f-0000-1000-8000-00805f9b34fb"),
            characteristicUuid: BluetoothUuid(value: "00002a19-0000-1000-8000-00805f9b34fb"),
            notificationMode: .platformDefault,
            expectedValue: Data([0, 0x80, 0xff])
          )
        )
      )
    }
    addTeardownBlock {
      recovery.cancel()
      _ = await recovery.result
    }
    // Cancel only after the real production reader has delivered the recovery scan's start.
    let started = await Task.detached {
      ScanWiringEvidence.waitForReceipt(scanStartedReceipt)
    }.value
    guard started else {
      return XCTFail("Recovery scan did not reach the single native reader")
    }
    recovery.cancel()
    do {
      let result = try await recovery.value
      XCTFail("Cancellation must throw CancellationError, not return \(result)")
    } catch is CancellationError {
      // Cancellation must survive both recoverTarget and executeRecoveryAction unchanged.
    } catch {
      XCTFail("Recovery cancellation threw a different error: \(error)")
    }
    let stopped = await Task.detached {
      ScanWiringEvidence.waitForReceipt(scanStoppedReceipt)
    }.value
    guard stopped else {
      return XCTFail("Cancelled recovery did not stop its real scan")
    }
    let recoveryStart = try XCTUnwrap(evidence.snapshot().raw.compactMap { event -> (CommandId, ScanSessionId)? in
      if case let .scanStarted(commandId, scanId) = event { return (commandId, scanId) }
      return nil
    }.first)
    let cleanupStop = try XCTUnwrap(evidence.snapshot().raw.compactMap { event -> (ScanSessionId, CommandId)? in
      if case let .scanStopped(scanId, result) = event,
         case let .ok(reason) = result,
         case let .requested(commandId) = reason {
        return (scanId, commandId)
      }
      return nil
    }.first)
    XCTAssertEqual(cleanupStop.0, recoveryStart.1)
    // A cancelled action must not close the owner's terminal history with its capture.
    let cleanupTerminal = try await session.executeRecoveryAction(
      sequence: 11, action: .awaitOldOperationTerminals(remaining: [String(cleanupStop.1.value)])
    )
    XCTAssertEqual(cleanupTerminal, ForeignPlatformActionResult(
      sequence: 11, signal: .oldOperationTerminal(operationId: String(cleanupStop.1.value))
    ))
    let recoverySnapshot = evidence.snapshot()
    XCTAssertEqual(recoverySnapshot.raw, [
      .adapterSnapshotChanged(
        commandId: CommandId(value: initialized.initializationCommandId), snapshot: initialized.adapterSnapshot
      ),
      .commandCompleted(commandId: CommandId(value: initialized.initializationCommandId), error: nil),
      .scanStarted(commandId: recoveryStart.0, scanId: recoveryStart.1),
      .commandCompleted(commandId: recoveryStart.0, error: nil),
      .scanStopped(scanId: cleanupStop.0, result: .ok(reason: .requested(commandId: cleanupStop.1))),
      .commandCompleted(commandId: cleanupStop.1, error: nil),
    ])
    XCTAssertEqual(recoverySnapshot.scans, [
      ForeignScanEventEnvelope(
        sourceContext: initialized.context, event: .started(commandId: recoveryStart.0, scanId: recoveryStart.1)
      ),
      ForeignScanEventEnvelope(
        sourceContext: initialized.context,
        event: .stopped(scanId: cleanupStop.0, result: .ok(reason: .requested(commandId: cleanupStop.1)))
      ),
    ])

    let start = try owner.supervisor.beginStartScan(context: initialized.context, options: options)
    let scan = try await start.wait(timeoutMs: 2_000)
    XCTAssertNotEqual(scan, recoveryStart.1)
    for sequence in UInt8(0)..<65 {
      try backend.emitAdvertisement(scan, sequence: sequence)
      let received = await Task.detached {
        ScanWiringEvidence.waitForReceipt(advertisementReceipt)
      }.value
      guard received else {
        return XCTFail("Single native reader lost advertisement \(sequence) after recovery cancellation")
      }
    }
    let stop = try owner.supervisor.beginStopScan(context: initialized.context, session: scan)
    try await stop.wait(timeoutMs: 2_000)
    let terminal = try await session.executeRecoveryAction(
      sequence: 12, action: .awaitOldOperationTerminals(remaining: [String(stop.commandId().value)])
    )
    XCTAssertEqual(terminal, ForeignPlatformActionResult(
      sequence: 12, signal: .oldOperationTerminal(operationId: String(stop.commandId().value))
    ))
    let advertisements = (UInt8(0)..<65).map { backend.advertisement(scan, sequence: $0) }
    let snapshot = evidence.snapshot()
    XCTAssertEqual(snapshot.scans, recoverySnapshot.scans + [
      ForeignScanEventEnvelope(
        sourceContext: initialized.context, event: .started(commandId: start.commandId(), scanId: scan)
      ),
    ] + advertisements.map {
      ForeignScanEventEnvelope(sourceContext: initialized.context, event: .advertisement(advertisement: $0))
    } + [
      ForeignScanEventEnvelope(
        sourceContext: initialized.context,
        event: .stopped(scanId: scan, result: .ok(reason: .requested(commandId: stop.commandId())))
      ),
    ])
    XCTAssertEqual(snapshot.raw, recoverySnapshot.raw + [
      .scanStarted(commandId: start.commandId(), scanId: scan),
      .commandCompleted(commandId: start.commandId(), error: nil),
    ] + advertisements.map { .advertisementObserved(advertisement: $0) } + [
      .scanStopped(scanId: scan, result: .ok(reason: .requested(commandId: stop.commandId()))),
      .commandCompleted(commandId: stop.commandId(), error: nil),
    ])
    XCTAssertTrue(owner.factory.currentSessionForHost() === session)
  }

  func testAdapterSnapshotProjectionPreservesFullMatrixAndOptionalName() {
    let states: [(AdapterState, String)] = [
      (.unknown, "unknown"),
      (.unavailable, "unavailable"),
      (.poweredOff, "poweredOff"),
      (.poweredOn, "poweredOn"),
    ]
    let authorizations: [(AuthorizationState, String)] = [
      (.notRequired, "notRequired"),
      (.notDetermined, "notDetermined"),
      (.denied, "denied"),
      (.restricted, "restricted"),
      (.authorized, "authorized"),
    ]
    let names: [String?] = [nil, "", "蓝牙 🦷 café e\u{301}"]

    for (state, expectedState) in states {
      for (scanAuthorization, expectedScanAuthorization) in authorizations {
        for (connectionAuthorization, expectedConnectionAuthorization) in authorizations {
          for name in names {
            let snapshot = AdapterSnapshot(
              state: state,
              scanAuthorization: scanAuthorization,
              connectionAuthorization: connectionAuthorization,
              name: name
            )
            let projected = BlengAdapterSnapshot(adapterSnapshot: snapshot)

            XCTAssertEqual(projected.state.stringValue, expectedState)
            XCTAssertEqual(projected.scanAuthorization.stringValue, expectedScanAuthorization)
            XCTAssertEqual(projected.connectionAuthorization.stringValue, expectedConnectionAuthorization)
            XCTAssertEqual(projected.name, name)
          }
        }
      }
    }
  }

  func testEngineLifetimeRejectsDuplicateCrossEngineAndRetiredMembership() async throws {
    let first = BlengEngineLifetime()
    let second = BlengEngineLifetime()
    let owner = HybridBlengCentral()
    let beforeInvalidation = await first.waitForRetirement()
    XCTAssertNil(beforeInvalidation)
    XCTAssertTrue(first.bind(owner))
    XCTAssertFalse(first.bind(owner))
    XCTAssertFalse(second.bind(owner))
    XCTAssertEqual(first.boundOwnerCount, 1)
    XCTAssertEqual(second.boundOwnerCount, 0)
    first.invalidate()
    first.invalidate()
    let results = await first.waitForRetirement()
    XCTAssertEqual(results?.count, 1)
    XCTAssertTrue(results?.allSatisfy { $0.closed && $0.failure == nil } == true)
    XCTAssertEqual(first.boundOwnerCount, 0)
    let fresh = HybridBlengCentral()
    XCTAssertFalse(first.bind(fresh))
    XCTAssertTrue(second.bind(fresh), "Rejected binding must not claim the fresh owner")
    second.invalidate()
    let secondResults = await second.waitForRetirement()
    XCTAssertEqual(secondResults?.count, 1)
  }

  func testEngineLifetimeBoundsNativeMembershipWithoutPoisoningRejectedOwners() async throws {
    let lifetime = BlengEngineLifetime()
    for _ in 0..<BlengEngineLifetime.maximumOwners {
      XCTAssertTrue(lifetime.bind(HybridBlengCentral()))
    }
    let overflow = HybridBlengCentral()
    XCTAssertFalse(lifetime.bind(overflow))
    XCTAssertEqual(lifetime.boundOwnerCount, BlengEngineLifetime.maximumOwners)
    let independent = BlengEngineLifetime()
    XCTAssertTrue(independent.bind(overflow))
    lifetime.invalidate()
    independent.invalidate()
    let results = await lifetime.waitForRetirement()
    XCTAssertEqual(results?.count, BlengEngineLifetime.maximumOwners)
    XCTAssertTrue(results?.allSatisfy { $0.closed && $0.failure == nil } == true)
    _ = await independent.waitForRetirement()
  }

  func testEnginePredecessorLinkageIsImmutableAndRejectsCycles() async throws {
    let first = BlengEngineLifetime()
    let second = BlengEngineLifetime()
    let third = BlengEngineLifetime()
    XCTAssertFalse(first.configurePredecessor(first, timeoutMs: 1_000))
    XCTAssertFalse(second.configurePredecessor(first, timeoutMs: 0))
    XCTAssertTrue(second.configurePredecessor(first, timeoutMs: 1_000))
    XCTAssertFalse(second.configurePredecessor(third, timeoutMs: 1_000))
    XCTAssertFalse(first.configurePredecessor(second, timeoutMs: 1_000))
    XCTAssertFalse(third.configurePredecessor(first, timeoutMs: 1_000))
    XCTAssertTrue(third.configurePredecessor(second, timeoutMs: 1_000))
    for lifetime in [third, second, first] { lifetime.invalidate() }
    for lifetime in [first, second, third] { _ = try await lifetime.waitForRetirement(timeoutMs: 2_000) }
  }

  func testMissingPredecessorCallbackTimesOutAndCannotBeBypassedByAnIdleMiddleEngine() async throws {
    let previous = BlengEngineLifetime()
    let middle = BlengEngineLifetime()
    let fixture = ScanBridgeFixture()
    XCTAssertTrue(middle.configurePredecessor(previous, timeoutMs: 50))
    XCTAssertTrue(middle.configureOwnerFactoryForTesting { try fixture.makeOwner() })
    let owner = HybridBlengCentral()
    XCTAssertTrue(middle.bind(owner))
    await assertInitializeFailed(try owner.initialize())
    XCTAssertFalse(previous.isInvalidated, "A deadline must not manufacture the missing RN callback")
    XCTAssertEqual(fixture.creationCount, 0)
    middle.invalidate()
    _ = try await middle.waitForRetirement(timeoutMs: 1_000)

    let next = BlengEngineLifetime()
    let nextFixture = ScanBridgeFixture()
    XCTAssertTrue(next.configurePredecessor(middle, timeoutMs: 1_000))
    XCTAssertTrue(next.configureOwnerFactoryForTesting { try nextFixture.makeOwner() })
    let nextOwner = HybridBlengCentral()
    XCTAssertTrue(next.bind(nextOwner))
    await assertInitializeFailed(try nextOwner.initialize())
    XCTAssertEqual(nextFixture.creationCount, 0)
    previous.invalidate()
    next.invalidate()
    _ = try await previous.waitForRetirement(timeoutMs: 1_000)
    _ = try await next.waitForRetirement(timeoutMs: 1_000)
  }

  func testInvalidatingAnEngineWaitingForItsPredecessorDoesNotWaitForThatCallback() async throws {
    let previous = BlengEngineLifetime()
    let next = BlengEngineLifetime()
    let fixture = ScanBridgeFixture()
    XCTAssertTrue(next.configurePredecessor(previous, timeoutMs: 5_000))
    XCTAssertTrue(next.configureOwnerFactoryForTesting { try fixture.makeOwner() })
    let owner = HybridBlengCentral()
    XCTAssertTrue(next.bind(owner))
    let starting = try owner.initialize()
    next.invalidate()
    let receipt = try await next.waitForRetirement(timeoutMs: 1_000)
    XCTAssertEqual(receipt.count, 1)
    XCTAssertTrue(receipt.allSatisfy { $0.closed && $0.failure == nil })
    await assertInitializeFailed(starting)
    XCTAssertEqual(fixture.creationCount, 0)
    XCTAssertFalse(previous.isInvalidated)
    previous.invalidate()
    _ = try await previous.waitForRetirement(timeoutMs: 1_000)
  }

  func testRCTHostReloadWaitsForOldNativeRetirementBeforeNewJSStartCompletes() async throws {
    try await verifyRCTHostReload(oldCleanupFails: false)
  }

  func testRCTHostReloadPreservesOldCleanupFailureAndRejectsNewJSStartup() async throws {
    try await verifyRCTHostReload(oldCleanupFails: true)
  }

  func testRejectedHostConfigurationStillRegistersForRNInvalidationAndCleanup() async throws {
    let rejected = expectation(description: "native configuration hook rejection recorded")
    let bundle = try XCTUnwrap(Bundle(for: Self.self).url(forResource: "BlengHostReload", withExtension: "jsbundle"))
    let host = await MainActor.run {
      BlengHostReloadHarness(bundleURL: bundle, configureLifetime: { _, _ in
        XCTFail("Configuration failure must precede injected backend configuration")
      }, event: { record in
        if record["stage"] as? String == "native-failed" &&
            record["error"] as? String == "lifetime-configuration-failed" { rejected.fulfill() }
      })
    }
    addTeardownBlock { await MainActor.run { host.stop() } }
    await MainActor.run {
      host.enableConfigurationFailure()
      host.start()
    }
    await fulfillment(of: [rejected], timeout: 10)
    let lifetime = try XCTUnwrap(host.lifetimes.first as? BlengEngineLifetime)
    XCTAssertEqual(host.lifetimes.count, 1)
    XCTAssertEqual(lifetime.boundOwnerCount, 0)
    XCTAssertFalse(lifetime.isInvalidated, "Rejecting creation must not manufacture RN invalidation")
    await MainActor.run { host.stop() }
    let results = try await lifetime.waitForRetirement(timeoutMs: 5_000)
    XCTAssertTrue(lifetime.isInvalidated)
    XCTAssertTrue(results.isEmpty, "Rejected binding must never initialize a backend")
    XCTAssertFalse(host.events.contains { ["start-resolved", "start-submitted"].contains($0["stage"] as? String ?? "") })
  }

  func testRenderedHostReloadRemountsOnlyAfterNativeStartupPermission() async throws {
    try await verifyRCTHostReload(oldCleanupFails: false, rendered: true)
  }

  func testRenderedHostReloadDisplaysOldCleanupFailureWithoutStartingNewBackend() async throws {
    try await verifyRCTHostReload(oldCleanupFails: true, rendered: true)
  }

  private func waitForRenderedStatus(_ status: String, host: BlengHostReloadHarness) async throws {
    // Native UIKit observation, independent of the JS evidence channel and its settlement marker.
    for _ in 0..<60 {
      if await MainActor.run(body: { host.hasRenderedStatus(status) }) { return }
      try await Task.sleep(nanoseconds: 50_000_000)
    }
    XCTFail("Fabric subtree did not expose rendered status: \(status)")
  }

  private func verifyRCTHostReload(oldCleanupFails: Bool, rendered: Bool = false) async throws {
    let snapshot = ScanStartAdmissionGate()
    let firstFixture = ScanBridgeFixture(
      backend: ScanWiringBackend(reportRetirementLeak: oldCleanupFails), snapshotGate: snapshot
    )
    let secondFixture = ScanBridgeFixture()
    let firstReady = expectation(description: "generation one actual JS startup resolved")
    let secondSubmitted = expectation(description: "generation two actual JS startup submitted")
    let secondSettled = expectation(description: "generation two actual JS startup settled")
    let bundle = try XCTUnwrap(Bundle(for: Self.self).url(forResource: "BlengHostReload", withExtension: "jsbundle"))
    let configuration: (NSObject, UInt) -> Void = { object, generation in
      guard let lifetime = object as? BlengEngineLifetime else { return XCTFail("Unexpected lifetime type") }
      let fixture = generation == 1 ? firstFixture : secondFixture
      XCTAssertTrue(lifetime.configureOwnerFactoryForTesting { try fixture.makeOwner() })
    }
    let event: ([AnyHashable: Any]) -> Void = { record in
      let generation = (record["generation"] as? NSNumber)?.intValue
      let stage = record["stage"] as? String
      if generation == 1 && stage == "start-resolved" { firstReady.fulfill() }
      if generation == 2 && stage == "start-submitted" { secondSubmitted.fulfill() }
      if generation == 2 && (stage == "start-resolved" || stage == "start-rejected") { secondSettled.fulfill() }
    }
    let host = await MainActor.run {
      BlengHostReloadHarness(bundleURL: bundle, configureLifetime: configuration, event: event)
    }
    addTeardownBlock {
      await snapshot.release()
      await MainActor.run { host.stop() }
      for object in host.lifetimes {
        guard let lifetime = object as? BlengEngineLifetime else { continue }
        do { _ = try await lifetime.waitForRetirement(timeoutMs: 5_000) }
        catch { XCTFail("Host release did not yield RN/native retirement: \(error)") }
      }
      let data = try JSONSerialization.data(withJSONObject: host.events, options: [.sortedKeys, .prettyPrinted])
      let attachment = XCTAttachment(data: data, uniformTypeIdentifier: "public.json")
      attachment.name = "RCTHost-\(rendered ? "rendered" : "surface-free")-reload-\(host.runId)"
      attachment.lifetime = .keepAlways
      self.add(attachment)
    }
    await MainActor.run {
      if rendered { host.enableRenderedSurface() }
      host.start()
    }
    await fulfillment(of: [firstReady], timeout: 10)
    if rendered { try await waitForRenderedStatus("started", host: host) }
    guard host.lifetimes.count == 1 else { return XCTFail("First engine did not create exactly one lifecycle module") }
    XCTAssertEqual(firstFixture.creationCount, 1)
    XCTAssertEqual(firstFixture.backend.commandCounts().initializations, 1)
    await MainActor.run { host.reload() }
    await fulfillment(of: [secondSubmitted], timeout: 10)
    let entered = await Task.detached { ScanWiringEvidence.waitForReceipt(snapshot.entered) }.value
    guard entered else { return XCTFail("Old RN engine did not reach its held native snapshot") }
    XCTAssertTrue(host.sameHostAfterReload)
    XCTAssertEqual(host.lifetimes.count, 2)
    XCTAssertEqual(secondFixture.creationCount, 0, "Replacement must not construct its backend while old cleanup is held")
    XCTAssertEqual(secondFixture.backend.commandCounts().initializations, 0)
    if rendered { try await waitForRenderedStatus("pending", host: host) }
    XCTAssertFalse(host.events.contains {
      ($0["generation"] as? NSNumber)?.intValue == 2 &&
        ["start-resolved", "start-rejected"].contains($0["stage"] as? String ?? "")
    })
    await snapshot.release()
    await fulfillment(of: [secondSettled], timeout: 10)
    if rendered { try await waitForRenderedStatus(oldCleanupFails ? "failed" : "started", host: host) }
    let firstLifetime = try XCTUnwrap(host.lifetimes.first as? BlengEngineLifetime)
    let oldResults = try await firstLifetime.waitForRetirement(timeoutMs: 5_000)
    XCTAssertEqual(oldResults.count, 1)
    XCTAssertTrue(oldResults.allSatisfy { $0.closed })
    XCTAssertEqual(oldResults.first?.failure?.code, oldCleanupFails ? "native_resources_not_zero" : nil)
    XCTAssertEqual(firstFixture.backend.disposeCount, 1)
    XCTAssertEqual(secondFixture.creationCount, oldCleanupFails ? 0 : 1)
    XCTAssertEqual(secondFixture.backend.commandCounts().initializations, oldCleanupFails ? 0 : 1)
    let settlement = host.events.filter {
      ($0["generation"] as? NSNumber)?.intValue == 2 &&
        ["start-resolved", "start-rejected"].contains($0["stage"] as? String ?? "")
    }
    XCTAssertEqual(settlement.count, 1)
    XCTAssertEqual(settlement.first?["stage"] as? String, oldCleanupFails ? "start-rejected" : "start-resolved")
    XCTAssertFalse(host.events.contains { ($0["stage"] as? String) == "native-failed" })
    XCTAssertNotNil(UUID(uuidString: host.runId))
    for (index, record) in host.events.enumerated() {
      XCTAssertEqual(record["runId"] as? String, host.runId)
      XCTAssertEqual((record["pid"] as? NSNumber)?.int32Value, ProcessInfo.processInfo.processIdentifier)
      XCTAssertEqual((record["sequence"] as? NSNumber)?.intValue, index + 1)
    }
  }

  func testRealHermesManagerInvalidationRetiresOnlyItsBoundNativeOwner() async throws {
    let snapshot = ScanStartAdmissionGate()
    let firstFixture = ScanBridgeFixture(snapshotGate: snapshot)
    let secondFixture = ScanBridgeFixture()
    let first = HybridBlengCentral(ownerFactory: { try firstFixture.makeOwner() })
    let second = HybridBlengCentral(ownerFactory: { try secondFixture.makeOwner() })
    let runtime = BlengEngineRuntimeHarness(
      firstOwner: first.getCxxWrapper().toUnsafe(), secondOwner: second.getCxxWrapper().toUnsafe()
    )
    let firstLifetime = try XCTUnwrap(runtime.firstLifetime as? BlengEngineLifetime)
    let secondLifetime = try XCTUnwrap(runtime.secondLifetime as? BlengEngineLifetime)
    addTeardownBlock {
      await snapshot.release()
      runtime.invalidateFirst()
      runtime.invalidateSecond()
      _ = await firstLifetime.waitForRetirement()
      _ = await secondLifetime.waitForRetirement()
      runtime.destroyFirstRuntime()
      runtime.destroySecondRuntime()
    }
    for key in ["plainObjectRejected", "duplicateBindingRejected", "crossEngineBindingRejected", "binderImmutable"] {
      XCTAssertEqual(runtime.checks[key], NSNumber(value: true), key)
    }
    XCTAssertFalse(firstLifetime.isInvalidated)
    XCTAssertFalse(secondLifetime.isInvalidated)
    XCTAssertEqual(firstLifetime.boundOwnerCount, 1)
    XCTAssertEqual(secondLifetime.boundOwnerCount, 1)
    await assertInitializeSucceeded(try first.initialize())
    await assertInitializeSucceeded(try second.initialize())
    let oldScan = try scanBridgeAccepted(first)
    await assertSettlement(try oldScan.waitUntilStarted(timeoutMs: 2_000)) { XCTAssertNil($0.failure) }

    runtime.invalidateFirst()
    XCTAssertTrue(firstLifetime.isInvalidated, "Only RN's manager calls the module invalidation hook")
    XCTAssertFalse(secondLifetime.isInvalidated)
    let fresh = HybridBlengCentral()
    XCTAssertTrue(runtime.rejectsBinding(afterFirstInvalidation: fresh.getCxxWrapper().toUnsafe()))
    runtime.destroyFirstRuntime()
    let entered = await Task.detached { ScanWiringEvidence.waitForReceipt(snapshot.entered) }.value
    guard entered else { return XCTFail("RN-triggered retirement did not reach the snapshot barrier") }
    XCTAssertEqual(firstFixture.backend.disposeCount, 1)
    XCTAssertEqual(secondFixture.backend.disposeCount, 0)
    await assertInitializeFailed(try first.initialize())
    let newScan = try scanBridgeAccepted(second)
    await assertSettlement(try newScan.waitUntilStarted(timeoutMs: 2_000)) { XCTAssertNil($0.failure) }
    await snapshot.release()
    let results = await firstLifetime.waitForRetirement()
    XCTAssertEqual(results?.count, 1)
    XCTAssertTrue(results?.allSatisfy { $0.closed && $0.failure == nil } == true)
    runtime.invalidateSecond()
    runtime.destroySecondRuntime()
    let secondResults = await secondLifetime.waitForRetirement()
    XCTAssertEqual(secondResults?.count, 1)
    XCTAssertTrue(secondResults?.allSatisfy { $0.closed && $0.failure == nil } == true)
    XCTAssertEqual(secondFixture.backend.disposeCount, 1)
  }

  func testRealHermesManagerInvalidationPreservesNativeCleanupFailure() async throws {
    let fixture = ScanBridgeFixture(backend: ScanWiringBackend(reportRetirementLeak: true))
    let first = HybridBlengCentral(ownerFactory: { try fixture.makeOwner() })
    let second = HybridBlengCentral()
    let runtime = BlengEngineRuntimeHarness(
      firstOwner: first.getCxxWrapper().toUnsafe(), secondOwner: second.getCxxWrapper().toUnsafe()
    )
    let lifetime = try XCTUnwrap(runtime.firstLifetime as? BlengEngineLifetime)
    let secondLifetime = try XCTUnwrap(runtime.secondLifetime as? BlengEngineLifetime)
    addTeardownBlock {
      runtime.invalidateFirst()
      runtime.invalidateSecond()
      _ = await lifetime.waitForRetirement()
      _ = await secondLifetime.waitForRetirement()
      runtime.destroyFirstRuntime()
      runtime.destroySecondRuntime()
    }
    await assertInitializeSucceeded(try first.initialize())
    runtime.invalidateFirst()
    runtime.destroyFirstRuntime()
    for _ in 0..<2 {
      let results = await lifetime.waitForRetirement()
      XCTAssertEqual(results?.count, 1)
      XCTAssertTrue(results?.first?.closed == true)
      XCTAssertEqual(results?.first?.failure?.code, "native_resources_not_zero")
    }
    await assertInitializeFailed(try first.initialize())
    XCTAssertEqual(fixture.creationCount, 1)
    XCTAssertEqual(fixture.backend.disposeCount, 1)
  }

  func testEngineInvalidationRejectsDelayedAndFutureStartsWithoutConstructingAnOwner() async throws {
    let fixture = ScanBridgeFixture()
    let admission = ScanStartAdmissionGate()
    let root = HybridBlengCentral(
      ownerFactory: { try fixture.makeOwner() },
      beforeStartAdmission: { await admission.wait() }
    )
    addTeardownBlock { await admission.release(); _ = await root.invalidateForEngine().value }
    let starting = try root.initialize()
    let entered = await Task.detached {
      ScanWiringEvidence.waitForReceipt(admission.entered)
    }.value
    guard entered else { return XCTFail("Start did not reach delayed actor admission") }

    let receipt = await root.invalidateForEngine().value
    XCTAssertTrue(receipt.closed)
    XCTAssertNil(receipt.failure)
    await admission.release()
    await assertInitializeFailed(starting)
    await assertInitializeFailed(try root.initialize())
    guard case let .second(rejected) = try root.beginScan(options: scanBridgeOptions()) else {
      return XCTFail("Invalidated owner must synchronously reject scan submission")
    }
    XCTAssertEqual(rejected.failure.code, "submissionUnavailable")
    XCTAssertEqual(fixture.creationCount, 0)
    XCTAssertEqual(fixture.backend.commandCounts().initializations, 0)
  }

  func testEngineInvalidationWaitsForAcceptedStartWithoutPublishingItsLateReceipt() async throws {
    let fixture = ScanBridgeFixture(backend: ScanWiringBackend(holdInitialization: true))
    let root = HybridBlengCentral(ownerFactory: { try fixture.makeOwner() })
    addTeardownBlock {
      try? fixture.backend.completeHeldInitialization()
      _ = await root.invalidateForEngine().value
    }
    let starting = try root.initialize()
    let submitted = await Task.detached {
      ScanWiringEvidence.waitForReceipt(fixture.backend.initializationSubmitted)
    }.value
    guard submitted else { return XCTFail("Initialization was not dispatched") }
    let retirement = root.invalidateForEngine()
    let closing = try root.close()
    XCTAssertEqual(fixture.backend.disposeCount, 0)
    try fixture.backend.completeHeldInitialization()
    await assertInitializeSucceeded(starting)
    guard case let .second(rejected) = try root.beginScan(options: scanBridgeOptions()) else {
      return XCTFail("Accepted initialization must not republish a sealed engine receipt")
    }
    XCTAssertEqual(rejected.failure.code, "submissionUnavailable")
    let receipt = await retirement.value
    XCTAssertTrue(receipt.closed)
    XCTAssertNil(receipt.failure)
    await assertSettlement(closing) { XCTAssertTrue($0.closed); XCTAssertNil($0.failure) }
    await assertInitializeFailed(try root.initialize())
    XCTAssertEqual(fixture.creationCount, 1)
    XCTAssertEqual(fixture.backend.disposeCount, 1)
  }

  func testEngineInvalidationDrainsActiveScanAndSnapshotBeforeItsNativeReceipt() async throws {
    let snapshot = ScanStartAdmissionGate()
    let fixture = ScanBridgeFixture(snapshotGate: snapshot)
    let root = HybridBlengCentral(ownerFactory: { try fixture.makeOwner() })
    addTeardownBlock { await snapshot.release(); _ = await root.invalidateForEngine().value }
    await assertInitializeSucceeded(try root.initialize())
    let scan = try scanBridgeAccepted(root)
    await assertSettlement(try scan.waitUntilStarted(timeoutMs: 2_000)) { XCTAssertNil($0.failure) }
    let retirement = root.invalidateForEngine()
    let repeated = root.invalidateForEngine()
    guard case let .second(rejected) = try root.beginScan(options: scanBridgeOptions()) else {
      return XCTFail("Engine invalidation must revoke scan synchronously")
    }
    XCTAssertEqual(rejected.failure.code, "submissionUnavailable")
    guard case let .second(stopRejected) = try scan.beginStop() else {
      return XCTFail("Old scan children must share the engine submission fence")
    }
    XCTAssertEqual(stopRejected.failure.code, "submissionUnavailable")
    let entered = await Task.detached { ScanWiringEvidence.waitForReceipt(snapshot.entered) }.value
    guard entered else { return XCTFail("Retirement did not reach the post-reader snapshot barrier") }
    XCTAssertEqual(fixture.backend.disposeCount, 1)
    XCTAssertEqual(fixture.backend.commandCounts().stops, 0)

    // A different native owner must not be sealed by this object's retirement.
    let independentFixture = ScanBridgeFixture()
    let independent = HybridBlengCentral(ownerFactory: { try independentFixture.makeOwner() })
    addTeardownBlock { await self.assertSettlement(try independent.close()) }
    await assertInitializeSucceeded(try independent.initialize())
    await assertSettlement(try independent.close())

    await snapshot.release()
    let receipt = await retirement.value
    let repeatedReceipt = await repeated.value
    XCTAssertTrue(receipt.closed)
    XCTAssertNil(receipt.failure)
    XCTAssertTrue(repeatedReceipt.closed)
    XCTAssertNil(repeatedReceipt.failure)
    await assertSettlement(try scan.nextEvent(timeoutMs: 2_000)) {
      guard case let .second(event) = $0 else { return XCTFail("Expected Rust scan owner retirement") }
      XCTAssertEqual(event.ended.reason.stringValue, "ownerClosed")
      XCTAssertNil(event.ended.failure)
    }
    XCTAssertTrue(fixture.backend.resources().values.allSatisfy { $0 == 0 })
    XCTAssertEqual(fixture.backend.disposeCount, 1)
    await assertInitializeFailed(try root.initialize())
    XCTAssertEqual(fixture.creationCount, 1)
  }

  func testEngineRetirementOutlivesTheHybridRootWithoutAnOldJSPromise() async throws {
    let snapshot = ScanStartAdmissionGate()
    let fixture = ScanBridgeFixture(snapshotGate: snapshot)
    var root: HybridBlengCentral? = HybridBlengCentral(ownerFactory: { try fixture.makeOwner() })
    weak var weakRoot = root
    await assertInitializeSucceeded(try XCTUnwrap(root).initialize())
    let scan = try scanBridgeAccepted(XCTUnwrap(root))
    await assertSettlement(try scan.waitUntilStarted(timeoutMs: 2_000)) { XCTAssertNil($0.failure) }
    let retirement = try XCTUnwrap(root).invalidateForEngine()
    addTeardownBlock { await snapshot.release(); _ = await retirement.value }
    root = nil
    XCTAssertNil(weakRoot)
    let entered = await Task.detached { ScanWiringEvidence.waitForReceipt(snapshot.entered) }.value
    guard entered else { return XCTFail("Native retirement did not survive HybridObject release") }
    await snapshot.release()
    let receipt = await retirement.value
    XCTAssertTrue(receipt.closed)
    XCTAssertNil(receipt.failure)
    XCTAssertEqual(fixture.backend.disposeCount, 1)
    XCTAssertTrue(fixture.backend.resources().values.allSatisfy { $0 == 0 })
  }

  func testEngineInvalidationJoinsAnAlreadyRunningExplicitClose() async throws {
    let snapshot = ScanStartAdmissionGate()
    let fixture = ScanBridgeFixture(snapshotGate: snapshot)
    let root = HybridBlengCentral(ownerFactory: { try fixture.makeOwner() })
    addTeardownBlock { await snapshot.release(); _ = await root.invalidateForEngine().value }
    await assertInitializeSucceeded(try root.initialize())
    let closing = try root.close()
    let entered = await Task.detached { ScanWiringEvidence.waitForReceipt(snapshot.entered) }.value
    guard entered else { return XCTFail("Explicit close did not reach snapshot") }
    let retirement = root.invalidateForEngine()
    await snapshot.release()
    await assertSettlement(closing) { XCTAssertTrue($0.closed); XCTAssertNil($0.failure) }
    let receipt = await retirement.value
    XCTAssertTrue(receipt.closed)
    XCTAssertNil(receipt.failure)
    XCTAssertEqual(fixture.backend.disposeCount, 1)
    await assertInitializeFailed(try root.initialize())
    XCTAssertEqual(fixture.creationCount, 1)
  }

  func testEngineInvalidationPreservesCleanupFailureAndNeverRestartsTheOldOwner() async throws {
    let fixture = ScanBridgeFixture(backend: ScanWiringBackend(reportRetirementLeak: true))
    let root = HybridBlengCentral(ownerFactory: { try fixture.makeOwner() })
    addTeardownBlock { _ = await root.invalidateForEngine().value }
    await assertInitializeSucceeded(try root.initialize())
    for _ in 0..<2 {
      let receipt = await root.invalidateForEngine().value
      XCTAssertTrue(receipt.closed)
      XCTAssertEqual(receipt.failure?.code, "native_resources_not_zero")
      XCTAssertEqual(fixture.backend.disposeCount, 1)
    }
    await assertSettlement(try root.close()) {
      XCTAssertTrue($0.closed)
      XCTAssertEqual($0.failure?.code, "native_resources_not_zero")
    }
    await assertInitializeFailed(try root.initialize())
    XCTAssertEqual(fixture.creationCount, 1)
  }

  func testEngineInvalidationAfterExplicitCloseSealsReuseAndPreservesItsResult() async throws {
    for leak in [false, true] {
      let fixture = ScanBridgeFixture(backend: ScanWiringBackend(reportRetirementLeak: leak))
      let root = HybridBlengCentral(ownerFactory: { try fixture.makeOwner() })
      addTeardownBlock { _ = await root.invalidateForEngine().value }
      await assertInitializeSucceeded(try root.initialize())
      await assertSettlement(try root.close()) {
        XCTAssertTrue($0.closed)
        XCTAssertEqual($0.failure?.code, leak ? "native_resources_not_zero" : nil)
      }
      let receipt = await root.invalidateForEngine().value
      XCTAssertTrue(receipt.closed)
      XCTAssertEqual(receipt.failure?.code, leak ? "native_resources_not_zero" : nil)
      await assertInitializeFailed(try root.initialize())
      XCTAssertEqual(fixture.creationCount, 1)
      XCTAssertEqual(fixture.backend.disposeCount, 1)
    }
  }

  func testUnboundProductionHybridRejectsStartBeforeNativeInitialization() async throws {
    let owner = HybridBlengCentral()
    await assertInitializeFailed(try owner.initialize())
    let lifetime = BlengEngineLifetime()
    XCTAssertTrue(lifetime.bind(owner), "Rejected unbound startup must not poison engine binding")
    lifetime.invalidate()
    let results = await lifetime.waitForRetirement()
    XCTAssertEqual(results?.count, 1)
    XCTAssertTrue(results?.allSatisfy { $0.closed && $0.failure == nil } == true)
  }

  func testNativeNitroPromisesResolveAndRejectWithoutProbeSurface() async throws {
    let owner = HybridBlengCentral()
    let lifetime = BlengEngineLifetime()
    XCTAssertTrue(lifetime.bind(owner))
    addTeardownBlock {
      lifetime.invalidate()
      let results = await lifetime.waitForRetirement()
      XCTAssertTrue(results?.allSatisfy { $0.closed && $0.failure == nil } == true)
    }

    let emptyClose: Promise<BlengCloseResult> = try owner.close()
    await assertSettlement(emptyClose) {
      XCTAssertTrue($0.closed)
      XCTAssertNil($0.failure)
    }
    let start = try owner.initialize()
    await assertInitializeSucceeded(start) { self.assertStartupSnapshot($0) }
    await assertInitializeFailed(try owner.initialize())
    await assertSettlement(try owner.close())
    await assertSettlement(try owner.close())
    let restarted = try owner.initialize()
    await assertInitializeSucceeded(restarted) { self.assertStartupSnapshot($0) }
    await assertSettlement(try owner.close())
  }

  func testScanSubmissionWithoutStartedReceiptNeverConstructsOwner() async throws {
    let fixture = ScanBridgeFixture()
    let root = HybridBlengCentral(ownerFactory: { try fixture.makeOwner() })
    for _ in 0..<2 {
      guard case let .second(rejected) = try root.beginScan(options: scanBridgeOptions()) else {
        return XCTFail("An unstarted root must reject synchronously")
      }
      XCTAssertEqual(rejected.failure.domain.stringValue, "lifecycle")
      XCTAssertEqual(rejected.failure.code, "submissionUnavailable")
      XCTAssertEqual(rejected.failure.message, "No started session is available for scan submission.")
      XCTAssertNil(rejected.failure.platform)
    }
    await assertSettlement(try root.close())
    XCTAssertEqual(fixture.creationCount, 0)
    XCTAssertEqual(fixture.backend.commandCounts().initializations, 0)
  }

  func testStartAdmittedAfterCloseRejectsWithoutCreatingOwnerBackendOrReceipt() async throws {
    let fixture = ScanBridgeFixture()
    let admission = ScanStartAdmissionGate()
    let root = HybridBlengCentral(
      ownerFactory: { try fixture.makeOwner() },
      beforeStartAdmission: { await admission.wait() }
    )
    addTeardownBlock {
      await admission.release()
      await self.assertSettlement(try root.close())
    }
    let starting = try root.initialize()
    let waiting = await Task.detached {
      ScanWiringEvidence.waitForReceipt(admission.entered)
    }.value
    guard waiting else { return XCTFail("Start did not reach the controlled pre-actor gate") }
    XCTAssertEqual(fixture.creationCount, 0)
    await assertSettlement(try root.close())
    // The empty-owner close has finished before the original start may enter the actor.
    await admission.release()
    await assertInitializeFailed(starting)
    XCTAssertEqual(fixture.creationCount, 0)
    XCTAssertNil(fixture.currentOwner)
    XCTAssertEqual(fixture.backend.commandCounts().initializations, 0)
    XCTAssertEqual(fixture.backend.commandCounts().starts, 0)
    XCTAssertEqual(fixture.backend.commandCounts().stops, 0)
    XCTAssertTrue(fixture.evidence.snapshot().raw.isEmpty)
    guard case let .second(rejected) = try root.beginScan(options: scanBridgeOptions()) else {
      return XCTFail("A stale start must never publish a scan receipt")
    }
    XCTAssertEqual(rejected.failure.domain.stringValue, "lifecycle")
    XCTAssertEqual(rejected.failure.code, "submissionUnavailable")
    XCTAssertEqual(rejected.failure.message, "No started session is available for scan submission.")
    XCTAssertNil(rejected.failure.platform)
  }

  func testDuplicateStartRejectionPreservesTheActualStartedScanReceipt() async throws {
    let fixture = ScanBridgeFixture()
    let root = HybridBlengCentral(ownerFactory: { try fixture.makeOwner() })
    addTeardownBlock { await self.assertSettlement(try root.close()) }
    await assertInitializeSucceeded(try root.initialize())
    let owner = try XCTUnwrap(fixture.currentOwner)
    await assertInitializeFailed(try root.initialize())
    XCTAssertTrue(fixture.currentOwner === owner)
    XCTAssertEqual(fixture.creationCount, 1)
    XCTAssertEqual(fixture.backend.commandCounts().initializations, 1)
    let scan = try scanBridgeAccepted(root)
    await assertSettlement(try scan.waitUntilStarted(timeoutMs: 2_000)) { XCTAssertNil($0.failure) }
    await assertSettlement(try scan.nextEvent(timeoutMs: 2_000)) {
      guard case .first = $0 else { return XCTFail("Duplicate start must not revoke the real receipt") }
    }
    XCTAssertEqual(fixture.backend.commandCounts().starts, 1)
    XCTAssertEqual(fixture.backend.commandCounts().stops, 0)
  }

  func testPendingStartCannotPublishReceiptAfterBusyCloseAndOwnerIsRetainedForRetry() async throws {
    let fixture = ScanBridgeFixture(backend: ScanWiringBackend(holdInitialization: true))
    let root = HybridBlengCentral(ownerFactory: { try fixture.makeOwner() })
    addTeardownBlock {
      try? fixture.backend.completeHeldInitialization()
      await self.assertSettlement(try root.close())
    }
    let starting = try root.initialize()
    let submitted = await Task.detached {
      ScanWiringEvidence.waitForReceipt(fixture.backend.initializationSubmitted)
    }.value
    guard submitted else { return XCTFail("Fake initialize did not reach Rust's real owner") }
    guard case let .second(pending) = try root.beginScan(options: scanBridgeOptions()) else {
      return XCTFail("An owner under construction is not a started submission capability")
    }
    XCTAssertEqual(pending.failure.code, "submissionUnavailable")
    // Rust reports concurrent shutdown as nonterminal; the same owner remains retryable.
    let owner = try XCTUnwrap(fixture.currentOwner)
    for _ in 0..<2 {
      await assertSettlement(try root.close()) {
        XCTAssertFalse($0.closed)
        XCTAssertEqual($0.failure?.code, "supervisor_busy")
        XCTAssertEqual($0.failure?.message, "supervisor operation is already in progress")
      }
      XCTAssertTrue(fixture.currentOwner === owner)
      XCTAssertEqual(fixture.backend.disposeCount, 0)
    }
    try fixture.backend.completeHeldInitialization()
    await assertInitializeSucceeded(starting)
    guard case let .second(late) = try root.beginScan(options: scanBridgeOptions()) else {
      return XCTFail("A late start receipt must not reopen a gate revoked by close")
    }
    XCTAssertEqual(late.failure.code, "submissionUnavailable")
    XCTAssertEqual(fixture.creationCount, 1)
    XCTAssertEqual(fixture.backend.commandCounts().starts, 0)
    await assertSettlement(try root.close()) {
      XCTAssertTrue($0.closed)
      XCTAssertNil($0.failure)
    }
    XCTAssertEqual(fixture.creationCount, 1)
    XCTAssertEqual(fixture.backend.disposeCount, 1)
    XCTAssertTrue(fixture.backend.resources().values.allSatisfy { $0 == 0 })
  }

  func testRealInitializationFailureClosesWithFailureAndExplicitStartCreatesFreshOwner() async throws {
    let failed = ScanWiringBackend(rejectFirstInitialization: true)
    let fresh = ScanWiringBackend()
    let fixture = ScanBridgeFixture(backend: failed, replacementBackend: fresh)
    let root = HybridBlengCentral(ownerFactory: { try fixture.makeOwner() })
    addTeardownBlock { await self.assertSettlement(try root.close()) }

    await assertInitializeFailed(try root.initialize())
    let oldOwner = try XCTUnwrap(fixture.currentOwner)
    for _ in 0..<2 {
      await assertSettlement(try root.close()) {
        XCTAssertTrue($0.closed)
        XCTAssertEqual($0.failure?.code, "no_active_session")
        XCTAssertEqual($0.failure?.message, "supervisor has no active session")
      }
    }
    let native = await oldOwner.closeResult()
    XCTAssertTrue(native.ownerClosed)
    XCTAssertEqual(native.failure?.code, "no_active_session")
    XCTAssertEqual(native.failure?.message, "supervisor has no active session")
    XCTAssertEqual(failed.disposeCount, 1)
    XCTAssertEqual(fixture.creationCount, 1)
    guard case .second = try root.beginScan(options: scanBridgeOptions()) else {
      return XCTFail("Terminal failure must not leave a started receipt")
    }

    await assertInitializeSucceeded(try root.initialize())
    XCTAssertEqual(fixture.creationCount, 2)
    XCTAssertFalse(fixture.currentOwner === oldOwner)
    let scan = try scanBridgeAccepted(root)
    await assertSettlement(try scan.waitUntilStarted(timeoutMs: 2_000)) { XCTAssertNil($0.failure) }
    await assertSettlement(try root.close()) {
      XCTAssertTrue($0.closed)
      XCTAssertNil($0.failure)
    }
    XCTAssertEqual(fresh.disposeCount, 1)
  }

  func testRealCleanupFailureIsTerminalAndRepeatedClosePreservesFailureWithoutDisposingAgain() async throws {
    let backend = ScanWiringBackend(reportRetirementLeak: true)
    let fixture = ScanBridgeFixture(backend: backend, replacementBackend: ScanWiringBackend())
    let root = HybridBlengCentral(ownerFactory: { try fixture.makeOwner() })
    addTeardownBlock { await self.assertSettlement(try root.close()) }
    await assertInitializeSucceeded(try root.initialize())
    let oldOwner = try XCTUnwrap(fixture.currentOwner)

    for _ in 0..<3 {
      await assertSettlement(try root.close()) {
        XCTAssertTrue($0.closed)
        XCTAssertEqual($0.failure?.code, "native_resources_not_zero")
        XCTAssertEqual($0.failure?.message, "native resources remained after retirement")
      }
      XCTAssertEqual(backend.disposeCount, 1)
      XCTAssertEqual(fixture.creationCount, 1)
    }
    let native = await oldOwner.closeResult()
    XCTAssertTrue(native.ownerClosed)
    XCTAssertEqual(native.failure?.code, "native_resources_not_zero")
    XCTAssertEqual(native.failure?.message, "native resources remained after retirement")
    XCTAssertEqual(backend.disposeCount, 1)

    await assertInitializeSucceeded(try root.initialize())
    XCTAssertEqual(fixture.creationCount, 2)
    XCTAssertFalse(fixture.currentOwner === oldOwner)
    await assertSettlement(try root.close()) {
      XCTAssertTrue($0.closed)
      XCTAssertNil($0.failure)
    }
  }

  func testScanBridgeReturnsOpaquePeripheralAndOwnedBytesThroughSingleReader() async throws {
    let fixture = ScanBridgeFixture()
    let root = HybridBlengCentral(ownerFactory: { try fixture.makeOwner() })
    addTeardownBlock { await self.assertSettlement(try root.close()) }
    await assertInitializeSucceeded(try root.initialize())
    let scan = try scanBridgeAccepted(root)
    await assertSettlement(try scan.waitUntilStarted(timeoutMs: 2_000)) { XCTAssertNil($0.failure) }
    await assertSettlement(try scan.nextEvent(timeoutMs: 2_000)) { next in
      guard case let .first(event) = next else { return XCTFail("Expected native advertisement") }
      let advertisement = event.advertisement
      XCTAssertTrue(advertisement.peripheral is HybridBlengPeripheral)
      XCTAssertEqual(advertisement.localName, "scan wiring 🦷")
      XCTAssertEqual(advertisement.advertisedServices, [])
      XCTAssertEqual(advertisement.rssi, -61)
      XCTAssertEqual(advertisement.connectable, true)
      XCTAssertEqual(advertisement.manufacturerData.count, 1)
      let manufacturer = advertisement.manufacturerData[0]
      XCTAssertEqual(manufacturer.companyId, 0x0059)
      XCTAssertTrue(manufacturer.payload.isOwner)
      XCTAssertEqual(Data(bytes: manufacturer.payload.data, count: manufacturer.payload.size), Data([0, 0x80, 0xff]))
    }
    let nativeOwner = try XCTUnwrap(fixture.currentOwner)
    let start = try XCTUnwrap(fixture.evidence.snapshot().raw.compactMap { event -> ScanSessionId? in
      if case let .scanStarted(_, scanId) = event { return scanId }
      return nil
    }.first)
    try fixture.backend.emitOptionalAdvertisement(start)
    await assertSettlement(try scan.nextEvent(timeoutMs: 2_000)) { next in
      guard case let .first(event) = next else { return XCTFail("Expected optional native advertisement") }
      let advertisement = event.advertisement
      XCTAssertNil(advertisement.localName)
      XCTAssertNil(advertisement.rssi)
      XCTAssertNil(advertisement.connectable)
      XCTAssertEqual(advertisement.advertisedServices, [ScanWiringBackend.serviceUuid])
      XCTAssertEqual(advertisement.manufacturerData[0].payload.size, 0)
      XCTAssertTrue(advertisement.manufacturerData[0].payload.isOwner)
      XCTAssertEqual(advertisement.serviceData[0].serviceUuid, ScanWiringBackend.serviceUuid)
      let payload = advertisement.serviceData[0].payload
      XCTAssertTrue(payload.isOwner)
      XCTAssertEqual(Data(bytes: payload.data, count: payload.size), Data([0xff, 0, 0x80]))
    }
    XCTAssertNotNil(nativeOwner.factory.currentSessionForHost())
    XCTAssertEqual(fixture.backend.commandCounts().initializations, 1)
    XCTAssertEqual(fixture.backend.commandCounts().starts, 1)
  }

  func testScanBridgeLeavesCompanyAndTimeoutValidationToRustWithoutConsumingResults() async throws {
    let fixture = ScanBridgeFixture()
    let root = HybridBlengCentral(ownerFactory: { try fixture.makeOwner() })
    addTeardownBlock { await self.assertSettlement(try root.close()) }
    await assertInitializeSucceeded(try root.initialize())
    for company in [Double.nan, .infinity, -1, 1.5, 65_536] {
      guard case let .second(rejected) = try root.beginScan(options: scanBridgeOptions(companyId: company)) else {
        return XCTFail("Invalid company ID must return a Rust input failure")
      }
      XCTAssertEqual(rejected.failure.domain.stringValue, "input")
      XCTAssertEqual(rejected.failure.code, "invalid_company_id")
      XCTAssertEqual(rejected.failure.message, "company_id must be an integer between 0 and 65535")
    }
    XCTAssertEqual(fixture.backend.commandCounts().starts, 0)
    let scan = try scanBridgeAccepted(root)
    for timeout in [Double.nan, .infinity, -1, 1.5, 18_446_744_073_709_551_616, 0, 86_400_001] {
      await assertSettlement(try scan.waitUntilStarted(timeoutMs: timeout)) {
        XCTAssertEqual($0.failure?.domain.stringValue, "input")
        XCTAssertEqual($0.failure?.code, "invalid_timeout")
      }
      await assertSettlement(try scan.nextEvent(timeoutMs: timeout)) {
        guard case let .third(rejected) = $0 else { return XCTFail("Invalid pull timeout must fail") }
        XCTAssertEqual(rejected.failure.domain.stringValue, "input")
        XCTAssertEqual(rejected.failure.code, "invalid_timeout")
      }
    }
    await assertSettlement(try scan.waitUntilStarted(timeoutMs: 2_000)) { XCTAssertNil($0.failure) }
    await assertSettlement(try scan.waitUntilStarted(timeoutMs: 2_000)) {
      XCTAssertEqual($0.failure?.domain.stringValue, "operation")
      XCTAssertEqual($0.failure?.code, "bleng.operation_result_consumed")
    }
    await assertSettlement(try scan.cancelStart()) {
      XCTAssertEqual($0.failure?.domain.stringValue, "operation")
      XCTAssertEqual($0.failure?.code, "bleng.operation_already_completed")
    }
    await assertSettlement(try scan.nextEvent(timeoutMs: 2_000)) {
      guard case .first = $0 else { return XCTFail("Invalid/duplicate wait must not detach observations") }
    }
    XCTAssertEqual(fixture.backend.commandCounts().stops, 0)
  }

  func testScanBridgeStartTimeoutKeepsNativeObservationAndExplicitStopCapability() async throws {
    let fixture = ScanBridgeFixture(backend: ScanWiringBackend(holdScanCompletion: true))
    let root = HybridBlengCentral(ownerFactory: { try fixture.makeOwner() })
    addTeardownBlock {
      try? fixture.backend.completeHeldScanStart()
      await self.assertSettlement(try root.close())
    }
    await assertInitializeSucceeded(try root.initialize())
    let scan = try scanBridgeAccepted(root)
    let submitted = await Task.detached {
      ScanWiringEvidence.waitForReceipt(fixture.backend.scanSubmitted)
    }.value
    guard submitted else { return XCTFail("Fake startScan was not dispatched") }
    await assertSettlement(try scan.waitUntilStarted(timeoutMs: 1)) {
      XCTAssertEqual($0.failure?.domain.stringValue, "operation")
      XCTAssertEqual($0.failure?.code, "bleng.timeout")
    }
    XCTAssertEqual(fixture.backend.commandCounts().stops, 0)
    try fixture.backend.completeHeldScanStart()
    await assertSettlement(try scan.nextEvent(timeoutMs: 2_000)) {
      guard case .first = $0 else { return XCTFail("Timed-out start wait must not detach native observation") }
    }
    guard case let .first(stop) = try scan.beginStop() else {
      return XCTFail("Rust's observed scan identity must still support explicit stop")
    }
    await assertSettlement(try stop.stop.wait(timeoutMs: 2_000)) { XCTAssertNil($0.failure) }
    XCTAssertEqual(fixture.backend.commandCounts().stops, 1)
  }

  func testScanBridgeReturnsNativeStartFailureWithoutSynthesizingAnEndOrStop() async throws {
    let fixture = ScanBridgeFixture(backend: ScanWiringBackend(rejectFirstScan: true))
    let root = HybridBlengCentral(ownerFactory: { try fixture.makeOwner() })
    addTeardownBlock { await self.assertSettlement(try root.close()) }
    await assertInitializeSucceeded(try root.initialize())
    let scan = try scanBridgeAccepted(root)
    await assertSettlement(try scan.waitUntilStarted(timeoutMs: 2_000)) {
      XCTAssertEqual($0.failure?.domain.stringValue, "operation")
      XCTAssertEqual($0.failure?.code, "bleng.dispatch_rejected.invalid_state")
      XCTAssertEqual($0.failure?.message, ScanWiringBackend.rejectedScanDetail)
      XCTAssertNil($0.failure?.platform)
    }
    await assertSettlement(try scan.nextEvent(timeoutMs: 2_000)) {
      guard case let .third(rejected) = $0 else { return XCTFail("Start failure is not a native scan end") }
      XCTAssertEqual(rejected.failure.domain.stringValue, "delivery")
      XCTAssertEqual(rejected.failure.code, "delivery.start_failed")
    }
    XCTAssertEqual(fixture.backend.commandCounts().stops, 0)
  }

  func testScanBridgePullTimeoutTypedStopAndRetirementAreNotSynthesized() async throws {
    let fixture = ScanBridgeFixture(backend: ScanWiringBackend(emitOnStart: false))
    let root = HybridBlengCentral(ownerFactory: { try fixture.makeOwner() })
    addTeardownBlock { await self.assertSettlement(try root.close()) }
    await assertInitializeSucceeded(try root.initialize())
    let scan = try scanBridgeAccepted(root)
    await assertSettlement(try scan.waitUntilStarted(timeoutMs: 2_000)) { XCTAssertNil($0.failure) }
    await assertSettlement(try scan.nextEvent(timeoutMs: 1)) {
      guard case let .third(rejected) = $0 else { return XCTFail("Idle pull must time out, not end") }
      XCTAssertEqual(rejected.failure.domain.stringValue, "observation")
      XCTAssertEqual(rejected.failure.code, "bleng.timeout")
    }
    XCTAssertEqual(fixture.backend.commandCounts().stops, 0)
    guard case let .first(accepted) = try scan.beginStop() else { return XCTFail("Expected typed stop") }
    await assertSettlement(try accepted.stop.wait(timeoutMs: .nan)) {
      XCTAssertEqual($0.failure?.code, "invalid_timeout")
    }
    await assertSettlement(try accepted.stop.wait(timeoutMs: 2_000)) { XCTAssertNil($0.failure) }
    await assertSettlement(try accepted.stop.wait(timeoutMs: 2_000)) {
      XCTAssertEqual($0.failure?.code, "bleng.operation_result_consumed")
    }
    await assertSettlement(try accepted.stop.cancel()) {
      XCTAssertEqual($0.failure?.code, "bleng.operation_already_completed")
    }
    await assertSettlement(try scan.nextEvent(timeoutMs: 2_000)) {
      guard case let .second(event) = $0 else { return XCTFail("Expected actual requested end") }
      XCTAssertEqual(event.ended.reason.stringValue, "requested")
      XCTAssertNil(event.ended.failure)
    }
    XCTAssertEqual(fixture.backend.commandCounts().stops, 1)
    scan.dispose()
    let retiredScan = try scanBridgeAccepted(root)
    await assertSettlement(try retiredScan.waitUntilStarted(timeoutMs: 2_000)) { XCTAssertNil($0.failure) }
    let closing = try root.close()
    guard case let .second(rejected) = try root.beginScan(options: scanBridgeOptions()) else {
      return XCTFail("close must synchronously revoke the submission receipt")
    }
    XCTAssertEqual(rejected.failure.code, "submissionUnavailable")
    await assertSettlement(closing)
    await assertSettlement(try retiredScan.nextEvent(timeoutMs: 2_000)) {
      guard case let .second(event) = $0 else { return XCTFail("Expected Rust owner retirement") }
      XCTAssertEqual(event.ended.reason.stringValue, "ownerClosed")
      XCTAssertNil(event.ended.failure)
    }
    guard case let .second(stale) = try retiredScan.beginStop() else {
      return XCTFail("A retired scan cannot stop a new owner")
    }
    XCTAssertEqual(stale.failure.domain.stringValue, "lifecycle")
    XCTAssertNotEqual(stale.failure.code, "bridgeFailure")
  }

  func testScanBridgeDisposeDetachesButNeverStopsNativeScan() async throws {
    let fixture = ScanBridgeFixture()
    let root = HybridBlengCentral(ownerFactory: { try fixture.makeOwner() })
    addTeardownBlock { await self.assertSettlement(try root.close()) }
    await assertInitializeSucceeded(try root.initialize())
    let scan = try scanBridgeAccepted(root)
    await assertSettlement(try scan.waitUntilStarted(timeoutMs: 2_000)) { XCTAssertNil($0.failure) }
    scan.dispose()
    scan.dispose()
    await assertSettlement(try scan.nextEvent(timeoutMs: 2_000)) {
      guard case let .third(rejected) = $0 else { return XCTFail("Disposed observation must be detached") }
      XCTAssertEqual(rejected.failure.code, "delivery.detached")
    }
    XCTAssertEqual(fixture.backend.commandCounts().stops, 0)
    guard case let .first(stop) = try scan.beginStop() else {
      return XCTFail("Observation detach must not erase original stop capability")
    }
    stop.stop.dispose()
    await assertSettlement(try stop.stop.wait(timeoutMs: 2_000)) { XCTAssertNil($0.failure) }
    XCTAssertEqual(fixture.backend.commandCounts().stops, 1)
  }

  func testScanChildDoesNotRetainRootAndDestructionDoesNotStopNativeScan() async throws {
    let fixture = ScanBridgeFixture()
    var root: HybridBlengCentral? = HybridBlengCentral(ownerFactory: { try fixture.makeOwner() })
    weak var weakRoot = root
    await assertInitializeSucceeded(try XCTUnwrap(root).initialize())
    let nativeOwner = try XCTUnwrap(fixture.currentOwner)
    addTeardownBlock { _ = try await nativeOwner.close() }
    var scan: (any HybridBlengScanSpec)? = try scanBridgeAccepted(XCTUnwrap(root))
    weak var weakScan: AnyObject? = scan
    await assertSettlement(try XCTUnwrap(scan).waitUntilStarted(timeoutMs: 2_000)) { XCTAssertNil($0.failure) }
    root = nil
    XCTAssertNil(weakRoot)
    XCTAssertEqual(fixture.backend.commandCounts().stops, 0)
    scan = nil
    XCTAssertNil(weakScan)
    XCTAssertEqual(fixture.backend.commandCounts().stops, 0)
  }

  func testScanMappingRejectsAmbiguousOrEmptyDTOsAndPreservesRustFailures() async throws {
    let failure = ForeignOperationFailure(
      domain: .operation, code: "native.code", message: "native display 🦷",
      platformDetails: PlatformErrorDetails(platform: .apple, code: "42", name: "CBError", message: "native detail", status: nil, attCode: nil)
    )
    let projected = BlengScanMapping.failure(failure)
    XCTAssertEqual(projected.domain.stringValue, "operation")
    XCTAssertEqual(projected.code, failure.code)
    XCTAssertEqual(projected.message, failure.message)
    XCTAssertEqual(projected.platform?.platform, "apple")
    XCTAssertEqual(projected.platform?.code, "42")
    XCTAssertEqual(projected.platform?.name, "CBError")
    XCTAssertEqual(projected.platform?.message, "native detail")
    let platforms: [(Platform, String)] = [
      (.apple, "apple"), (.android, "android"), (.windows, "windows"), (.linux, "linux"), (.test, "test"),
    ]
    for (platform, expected) in platforms {
      let nativeFailure = ForeignOperationFailure(
        domain: failure.domain, code: failure.code, message: failure.message,
        platformDetails: PlatformErrorDetails(platform: platform, code: "42", name: "SDKError", message: "native detail", status: nil, attCode: nil)
      )
      let mapped = BlengScanMapping.failure(nativeFailure)
      XCTAssertEqual(mapped.domain.stringValue, "operation")
      XCTAssertEqual(mapped.code, failure.code)
      XCTAssertEqual(mapped.message, failure.message)
      XCTAssertEqual(mapped.platform?.platform, expected)
      XCTAssertEqual(mapped.platform?.code, "42")
      XCTAssertEqual(mapped.platform?.name, "SDKError")
      XCTAssertEqual(mapped.platform?.message, "native detail")
    }
    for observation in [
      ForeignScanObservation(advertisement: nil, ended: nil, failure: nil),
      ForeignScanObservation(advertisement: nil, ended: ForeignScanEnd(reason: .requested, failure: nil), failure: failure),
    ] {
      guard case let .third(rejected) = BlengScanMapping.observation(observation) else {
        return XCTFail("Malformed observation must fail closed")
      }
      XCTAssertEqual(rejected.failure.domain.stringValue, "delivery")
      XCTAssertEqual(rejected.failure.code, "bridgeFailure")
      XCTAssertEqual(rejected.failure.message, "Native scan bridge failed.")
      XCTAssertNil(rejected.failure.platform)
    }
    guard case let .second(empty) = BlengScanMapping.submission(ForeignScanBeginResult(scan: nil, failure: nil)) else {
      return XCTFail("Empty submission must fail closed")
    }
    XCTAssertEqual(empty.failure.code, "bridgeFailure")
    guard case let .second(emptyStop) = BlengScanMapping.stopSubmission(ForeignScanStopResult(operation: nil, failure: nil)) else {
      return XCTFail("Empty stop submission must fail closed")
    }
    XCTAssertEqual(emptyStop.failure.code, "bridgeFailure")

    let fixture = ScanBridgeFixture()
    let owner = try fixture.makeOwner()
    addTeardownBlock { _ = try await owner.close() }
    let started = try await owner.start()
    let result = owner.supervisor.beginScanView(context: started.context, options: ForeignScanOptions(
      serviceUuids: [], namePrefix: nil, exactName: nil, companyId: nil, minRssi: nil,
      manufacturerData: nil, android: nil, allowDuplicates: false
    ))
    let scan = try XCTUnwrap(result.scan)
    guard case let .second(ambiguous) = BlengScanMapping.submission(ForeignScanBeginResult(scan: scan, failure: failure)) else {
      return XCTFail("Ambiguous submission must not choose its first branch")
    }
    XCTAssertEqual(ambiguous.failure.code, "bridgeFailure")
    let startResult = await scan.startResult(timeoutMs: 2_000)
    XCTAssertNil(startResult.failure)
    let observation = await scan.nextObservation(timeoutMs: 2_000)
    let advertisement = try XCTUnwrap(observation.advertisement)
    guard case let .third(ambiguousAd) = BlengScanMapping.observation(ForeignScanObservation(
      advertisement: advertisement, ended: nil, failure: failure
    )) else { return XCTFail("Ambiguous advertisement must fail closed") }
    XCTAssertEqual(ambiguousAd.failure.code, "bridgeFailure")
    let stop = scan.beginStopView()
    let operation = try XCTUnwrap(stop.operation)
    guard case let .second(ambiguousStop) = BlengScanMapping.stopSubmission(ForeignScanStopResult(
      operation: operation, failure: failure
    )) else { return XCTFail("Ambiguous stop must fail closed") }
    XCTAssertEqual(ambiguousStop.failure.code, "bridgeFailure")
    let stopResult = await operation.waitResult(timeoutMs: 2_000)
    XCTAssertNil(stopResult.failure)
  }

  private func scanBridgeOptions(companyId: Double? = nil) -> BlengScanOptions {
    BlengScanOptions(
      serviceUuids: [],
      namePrefix: nil,
      exactName: nil,
      companyId: companyId,
      minRssi: nil,
      manufacturerData: nil,
      android: nil,
      allowDuplicates: false)
  }

  private func scanBridgeAccepted(_ root: HybridBlengCentral) throws -> any HybridBlengScanSpec {
    guard case let .first(accepted) = try root.beginScan(options: scanBridgeOptions()) else {
      throw NSError(domain: "ScanBridgeTest", code: 1, userInfo: [NSLocalizedDescriptionKey: "Expected accepted scan"])
    }
    return accepted.scan
  }

  private func assertStartupSnapshot(_ snapshot: BlengAdapterSnapshot) {
    XCTAssertTrue(
      ["unknown", "unavailable", "poweredOff", "poweredOn"].contains(snapshot.state.stringValue)
    )
    let authorizations = ["notRequired", "notDetermined", "denied", "restricted", "authorized"]
    XCTAssertTrue(authorizations.contains(snapshot.scanAuthorization.stringValue))
    XCTAssertTrue(authorizations.contains(snapshot.connectionAuthorization.stringValue))
  }

  private func assertSettlement<Value>(
    _ promise: Promise<Value>,
    rejects: Bool = false,
    onResolve: @escaping (Value) -> Void = { _ in },
    file: StaticString = #filePath,
    line: UInt = #line
  ) async {
    let settled = expectation(description: rejects ? "native Promise rejects" : "native Promise resolves")
    promise.then { value in
      if rejects {
        XCTFail("Native Promise unexpectedly resolved", file: file, line: line)
      } else {
        onResolve(value)
      }
      settled.fulfill()
    }.catch { error in
      if !rejects { XCTFail("Native Promise rejected: \(error)", file: file, line: line) }
      settled.fulfill()
    }
    await fulfillment(of: [settled], timeout: 15)
  }

  /// Await one successful `initialize()`. A native refusal resolves as `.second`, so a test that
  /// needs a live session must fail loudly instead of silently continuing.
  @discardableResult
  private func assertInitializeSucceeded(
    _ promise: Promise<BlengInitializeResult>,
    file: StaticString = #filePath,
    line: UInt = #line,
    onResolve: @escaping (BlengAdapterSnapshot) -> Void = { _ in }
  ) async -> BlengAdapterSnapshot? {
    var snapshot: BlengAdapterSnapshot?
    await assertSettlement(promise, onResolve: { result in
      switch result {
      case .first(let outcome):
        snapshot = outcome.snapshot
        onResolve(outcome.snapshot)
      case .second(let failure):
        XCTFail("initialize() failed: \(failure.failure.code)", file: file, line: line)
      }
    }, file: file, line: line)
    return snapshot
  }

  /// Await one refused `initialize()`; the failure is data (`.second`), not a rejected Promise.
  private func assertInitializeFailed(
    _ promise: Promise<BlengInitializeResult>,
    file: StaticString = #filePath,
    line: UInt = #line
  ) async {
    await assertSettlement(promise, onResolve: { result in
      guard case let .second(failure) = result else {
        return XCTFail("initialize() unexpectedly succeeded", file: file, line: line)
      }
      XCTAssertFalse(failure.failure.code.isEmpty, file: file, line: line)
    }, file: file, line: line)
  }
}

private actor BlengEventReadSample {
  private let reads: [Result<BleEvent?, Error>]
  private(set) var readCount = 0

  init(_ reads: [Result<BleEvent?, Error>]) {
    self.reads = reads
  }

  func next() throws -> BleEvent? {
    let index = readCount
    readCount += 1
    guard index < reads.count else { return nil }
    return try reads[index].get()
  }
}

private final class ScanWiringEvidence: @unchecked Sendable {
  private let lock = NSLock()
  private var raw: [BleEvent] = []
  private var scans: [ForeignScanEventEnvelope] = []

  // Synchronous boundary for Dispatch's noasync wait; callers run this in detached tasks.
  static func waitForReceipt(_ receipt: DispatchSemaphore) -> Bool {
    receipt.wait(timeout: .now() + 2) == .success
  }

  func record(_ event: BleEvent) { lock.withLock { raw.append(event) } }
  func record(_ envelope: ForeignScanEventEnvelope) { lock.withLock { scans.append(envelope) } }
  func snapshot() -> (raw: [BleEvent], scans: [ForeignScanEventEnvelope]) {
    lock.withLock { (raw, scans) }
  }
}

private actor ScanStartAdmissionGate {
  nonisolated let entered = DispatchSemaphore(value: 0)
  private var continuation: CheckedContinuation<Void, Never>?
  private var released = false

  func wait() async {
    await withCheckedContinuation { continuation in
      if released {
        continuation.resume()
      } else {
        precondition(self.continuation == nil, "Only one start is held by this fixture")
        self.continuation = continuation
        entered.signal()
      }
    }
  }

  func release() {
    released = true
    let continuation = self.continuation
    self.continuation = nil
    continuation?.resume()
  }
}

private final class ScanBridgeFixture: @unchecked Sendable {
  let backend: ScanWiringBackend
  let evidence = ScanWiringEvidence()
  private let lock = NSLock()
  private var creations = 0
  private var owner: AppleRustSupervisorOwner?
  private let replacementBackend: ScanWiringBackend?
  private let snapshotGate: ScanStartAdmissionGate?

  init(
    backend: ScanWiringBackend = ScanWiringBackend(),
    replacementBackend: ScanWiringBackend? = nil,
    snapshotGate: ScanStartAdmissionGate? = nil
  ) {
    self.backend = backend
    self.replacementBackend = replacementBackend
    self.snapshotGate = snapshotGate
  }

  var creationCount: Int { lock.withLock { creations } }
  var currentOwner: AppleRustSupervisorOwner? { lock.withLock { owner } }

  func makeOwner() throws -> AppleRustSupervisorOwner {
    let backend = lock.withLock { creations == 0 ? self.backend : replacementBackend ?? self.backend }
    let evidence = self.evidence
    let snapshotGate = self.snapshotGate
    let owner = try AppleRustSupervisorOwner(
      queueLabel: "BleHubScanBridgeNoRadio",
      eventSink: { evidence.record($0) },
      backendFactory: { _ in
        ApplePlatformBackendLease(
          backend: backend,
          resourceSnapshot: { await snapshotGate?.wait(); return backend.resources() },
          recreateCentral: { ["recreated": "false"] }
        )
      }
    )
    lock.withLock {
      creations += 1
      self.owner = owner
    }
    return owner
  }
}

/// No CoreBluetooth: only the real Rust owner and its single native reader are exercised.
private final class ScanWiringBackend: BleBackend, @unchecked Sendable {
  private let lock = NSLock()
  private var ingress: BackendEventIngressHandle?
  private var disposed = false
  private var disposals = 0
  private var rejectFirstInitialization: Bool
  private let reportRetirementLeak: Bool
  private var rejectFirstScan: Bool
  private let emitOnStart: Bool
  private let holdInitialization: Bool
  private let holdScanCompletion: Bool
  private var heldInitialization: InitializeCommand?
  private var heldScanStart: CommandId?
  let initializationSubmitted = DispatchSemaphore(value: 0)
  let scanSubmitted = DispatchSemaphore(value: 0)
  private var initializations = 0
  private var starts = 0
  private var stops = 0
  static let serviceUuid = "0000180f-0000-1000-8000-00805f9b34fb"
  static let rejectedScanDetail = "scan wiring fixture rejected first scan"

  func commandCounts() -> (initializations: Int, starts: Int, stops: Int) {
    lock.withLock { (initializations, starts, stops) }
  }

  var disposeCount: Int { lock.withLock { disposals } }

  init(
    rejectFirstInitialization: Bool = false,
    reportRetirementLeak: Bool = false,
    rejectFirstScan: Bool = false,
    emitOnStart: Bool = true,
    holdInitialization: Bool = false,
    holdScanCompletion: Bool = false
  ) {
    self.rejectFirstInitialization = rejectFirstInitialization
    self.reportRetirementLeak = reportRetirementLeak
    self.rejectFirstScan = rejectFirstScan
    self.emitOnStart = emitOnStart
    self.holdInitialization = holdInitialization
    self.holdScanCompletion = holdScanCompletion
  }

  func capabilities() throws -> BackendCapabilities {
    BackendCapabilities(
      platform: .test, contractVersion: blehubContractVersion(),
      supportsDuplicateScanResults: false, supportsConnectableFlag: true,
      supportsReadRssi: false, supportsRequiredNotification: false,
      supportsRequiredIndication: false, reportsActualNotificationMode: false
    )
  }

  func bindEventIngress(ingress: BackendEventIngressHandle) throws {
    lock.withLock { self.ingress = ingress }
  }

  private func push(_ event: BleEvent) throws {
    let root = lock.withLock { ingress }
    guard root?.push(event: event) == .delivered else {
      throw CommandDispatchError.InvalidState(detail: "scan wiring event was not delivered")
    }
  }

  func initialize(command: InitializeCommand) throws {
    let reject = lock.withLock {
      initializations += 1
      let reject = rejectFirstInitialization
      rejectFirstInitialization = false
      if holdInitialization { heldInitialization = command }
      return reject
    }
    initializationSubmitted.signal()
    if reject { throw CommandDispatchError.InvalidState(detail: "real startup rejection from fixture backend") }
    if !holdInitialization { try completeInitialization(command) }
  }

  func completeHeldInitialization() throws {
    let command = lock.withLock {
      let command = heldInitialization
      heldInitialization = nil
      return command
    }
    if let command { try completeInitialization(command) }
  }

  private func completeInitialization(_ command: InitializeCommand) throws {
    try push(.adapterSnapshotChanged(commandId: command.commandId, snapshot: AdapterSnapshot(
      state: .poweredOn, scanAuthorization: .authorized, connectionAuthorization: .authorized, name: nil
    )))
    try push(.commandCompleted(commandId: command.commandId, error: nil))
  }

  func advertisement(_ scan: ScanSessionId, sequence: UInt8 = 0) -> AdvertisementObservation {
    AdvertisementObservation(
      scanSessionId: scan, peripheralId: PeripheralId(value: 10 + UInt64(sequence)), localName: "scan wiring 🦷",
      advertisedServices: [], manufacturerData: [ManufacturerData(companyId: 0x0059, payload: Data([sequence, 0x80, 0xff]))],
      serviceData: [], rssi: -61, connectable: true
    )
  }

  func emitAdvertisement(_ scan: ScanSessionId, sequence: UInt8 = 0) throws {
    try push(.advertisementObserved(advertisement: advertisement(scan, sequence: sequence)))
  }

  func emitOptionalAdvertisement(_ scan: ScanSessionId) throws {
    try push(.advertisementObserved(advertisement: AdvertisementObservation(
      scanSessionId: scan, peripheralId: PeripheralId(value: 42), localName: nil,
      advertisedServices: [BluetoothUuid(value: Self.serviceUuid)],
      manufacturerData: [ManufacturerData(companyId: 0xffff, payload: Data())],
      serviceData: [ServiceData(serviceUuid: BluetoothUuid(value: Self.serviceUuid), payload: Data([0xff, 0, 0x80]))],
      rssi: nil, connectable: nil
    )))
  }

  func startScan(command: StartScanCommand) throws {
    let reject = lock.withLock {
      starts += 1
      let reject = rejectFirstScan
      rejectFirstScan = false
      return reject
    }
    if reject { throw CommandDispatchError.InvalidState(detail: Self.rejectedScanDetail) }
    try push(.scanStarted(commandId: command.commandId, scanId: command.scanSessionId))
    if emitOnStart { try emitAdvertisement(command.scanSessionId) }
    if holdScanCompletion {
      lock.withLock { heldScanStart = command.commandId }
    } else {
      try push(.commandCompleted(commandId: command.commandId, error: nil))
    }
    scanSubmitted.signal()
  }

  func completeHeldScanStart() throws {
    let commandId = lock.withLock {
      let commandId = heldScanStart
      heldScanStart = nil
      return commandId
    }
    if let commandId { try push(.commandCompleted(commandId: commandId, error: nil)) }
  }

  func stopScan(command: StopScanCommand) throws {
    lock.withLock { stops += 1 }
    try push(.scanStopped(scanId: command.scanSessionId, result: .ok(reason: .requested(commandId: command.commandId))))
    try push(.commandCompleted(commandId: command.commandId, error: nil))
  }

  func dispose() throws {
    lock.withLock {
      disposals += 1
      disposed = true
      ingress = nil
    }
  }

  func resources() -> [String: Int] {
    lock.withLock {
      Dictionary(uniqueKeysWithValues: ApplePlatformSession.requiredZeroResourceNames.map {
        ($0, ($0 == "backendLeases" && reportRetirementLeak) ||
          (!disposed && ($0 == "backendLeases" || $0 == "ingressRoots")) ? 1 : 0)
      })
    }
  }

  private func unexpected() -> CommandDispatchError {
    .InvalidState(detail: "unexpected non-scan command in scan wiring fixture")
  }

  func connect(command: ConnectCommand) throws { throw unexpected() }
  func disconnect(command: DisconnectCommand) throws { throw unexpected() }
  func discoverGatt(command: DiscoverGattCommand) throws { throw unexpected() }
  func readCharacteristic(command: ReadCharacteristicCommand) throws { throw unexpected() }
  func writeCharacteristic(command: WriteCharacteristicCommand) throws { throw unexpected() }
  func setNotifications(command: SetNotificationsCommand) throws { throw unexpected() }
  func readRssi(command: ReadRssiCommand) throws { throw unexpected() }
  func readDescriptor(command: ReadDescriptorCommand) throws { throw unexpected() }
  func writeDescriptor(command: WriteDescriptorCommand) throws { throw unexpected() }
  func mtu(command: MtuCommand) throws { throw unexpected() }
  func setConnectionPriority(command: SetConnectionPriorityCommand) throws { throw unexpected() }
  func createBond(command: CreateBondCommand) throws { throw unexpected() }
  func removeBond(command: RemoveBondCommand) throws { throw unexpected() }
  func setAdapterEnabled(command: SetAdapterEnabledCommand) throws { throw unexpected() }
  func queryDevices(command: QueryDevicesCommand) throws { throw unexpected() }
  func associateCompanionDevice(command: AssociateCompanionDeviceCommand) throws {
    throw unexpected()
  }
  func refreshGattCache(command: RefreshGattCacheCommand) throws { throw unexpected() }
  func cancelOperation(request: CancelOperationRequest) throws { throw unexpected() }
}
