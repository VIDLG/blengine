import BleHubUniFfi
import NitroModules
import XCTest
@testable import BleHubReactNativeBleng

final class BleHubBlengHostNativeTests: XCTestCase {
  func testFormalRustSupervisorOwnerStartsAndClosesWithoutProbeSurface() async throws {
    let owner = try AppleRustSupervisorOwner(queueLabel: "BleHubFormalConsumerNativeContract")
    addTeardownBlock { _ = try await owner.close() }

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
      options: ScanOptions(serviceUuids: [], namePrefix: nil, companyId: nil, allowDuplicates: false)
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
    let options = ScanOptions(serviceUuids: [], namePrefix: nil, companyId: nil, allowDuplicates: false)
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
    let options = ScanOptions(serviceUuids: [], namePrefix: nil, companyId: nil, allowDuplicates: false)
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

  func testNativeNitroPromisesResolveAndRejectWithoutProbeSurface() async throws {
    let owner = HybridBlengSessionOwner()
    addTeardownBlock { await self.assertSettlement(try owner.close()) }

    let emptyClose: Promise<BlengCloseResult> = try owner.close()
    await assertSettlement(emptyClose) {
      XCTAssertTrue($0.ownerClosed)
      XCTAssertNil($0.failure)
    }
    let start: Promise<BlengAdapterSnapshot> = try owner.start()
    await assertSettlement(start, onResolve: assertStartupSnapshot)
    await assertSettlement(try owner.start(), rejects: true)
    await assertSettlement(try owner.close())
    await assertSettlement(try owner.close())
    let restarted: Promise<BlengAdapterSnapshot> = try owner.start()
    await assertSettlement(restarted, onResolve: assertStartupSnapshot)
    await assertSettlement(try owner.close())
  }

  func testScanSubmissionWithoutStartedReceiptNeverConstructsOwner() async throws {
    let fixture = ScanBridgeFixture()
    let root = HybridBlengSessionOwner(ownerFactory: { try fixture.makeOwner() })
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
    let root = HybridBlengSessionOwner(
      ownerFactory: { try fixture.makeOwner() },
      beforeStartAdmission: { await admission.wait() }
    )
    addTeardownBlock {
      await admission.release()
      await self.assertSettlement(try root.close())
    }
    let starting = try root.start()
    let waiting = await Task.detached {
      ScanWiringEvidence.waitForReceipt(admission.entered)
    }.value
    guard waiting else { return XCTFail("Start did not reach the controlled pre-actor gate") }
    XCTAssertEqual(fixture.creationCount, 0)
    await assertSettlement(try root.close())
    // The empty-owner close has finished before the original start may enter the actor.
    await admission.release()
    await assertSettlement(starting, rejects: true)
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
    let root = HybridBlengSessionOwner(ownerFactory: { try fixture.makeOwner() })
    addTeardownBlock { await self.assertSettlement(try root.close()) }
    await assertSettlement(try root.start())
    let owner = try XCTUnwrap(fixture.currentOwner)
    await assertSettlement(try root.start(), rejects: true)
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
    let root = HybridBlengSessionOwner(ownerFactory: { try fixture.makeOwner() })
    addTeardownBlock {
      try? fixture.backend.completeHeldInitialization()
      await self.assertSettlement(try root.close())
    }
    let starting = try root.start()
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
        XCTAssertFalse($0.ownerClosed)
        XCTAssertEqual($0.failure?.code, "supervisor_busy")
        XCTAssertEqual($0.failure?.message, "supervisor operation is already in progress")
      }
      XCTAssertTrue(fixture.currentOwner === owner)
      XCTAssertEqual(fixture.backend.disposeCount, 0)
    }
    try fixture.backend.completeHeldInitialization()
    await assertSettlement(starting)
    guard case let .second(late) = try root.beginScan(options: scanBridgeOptions()) else {
      return XCTFail("A late start receipt must not reopen a gate revoked by close")
    }
    XCTAssertEqual(late.failure.code, "submissionUnavailable")
    XCTAssertEqual(fixture.creationCount, 1)
    XCTAssertEqual(fixture.backend.commandCounts().starts, 0)
    await assertSettlement(try root.close()) {
      XCTAssertTrue($0.ownerClosed)
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
    let root = HybridBlengSessionOwner(ownerFactory: { try fixture.makeOwner() })
    addTeardownBlock { await self.assertSettlement(try root.close()) }

    await assertSettlement(try root.start(), rejects: true)
    let oldOwner = try XCTUnwrap(fixture.currentOwner)
    for _ in 0..<2 {
      await assertSettlement(try root.close()) {
        XCTAssertTrue($0.ownerClosed)
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

    await assertSettlement(try root.start())
    XCTAssertEqual(fixture.creationCount, 2)
    XCTAssertFalse(fixture.currentOwner === oldOwner)
    let scan = try scanBridgeAccepted(root)
    await assertSettlement(try scan.waitUntilStarted(timeoutMs: 2_000)) { XCTAssertNil($0.failure) }
    await assertSettlement(try root.close()) {
      XCTAssertTrue($0.ownerClosed)
      XCTAssertNil($0.failure)
    }
    XCTAssertEqual(fresh.disposeCount, 1)
  }

  func testRealCleanupFailureIsTerminalAndRepeatedClosePreservesFailureWithoutDisposingAgain() async throws {
    let backend = ScanWiringBackend(reportRetirementLeak: true)
    let fixture = ScanBridgeFixture(backend: backend, replacementBackend: ScanWiringBackend())
    let root = HybridBlengSessionOwner(ownerFactory: { try fixture.makeOwner() })
    addTeardownBlock { await self.assertSettlement(try root.close()) }
    await assertSettlement(try root.start())
    let oldOwner = try XCTUnwrap(fixture.currentOwner)

    for _ in 0..<3 {
      await assertSettlement(try root.close()) {
        XCTAssertTrue($0.ownerClosed)
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

    await assertSettlement(try root.start())
    XCTAssertEqual(fixture.creationCount, 2)
    XCTAssertFalse(fixture.currentOwner === oldOwner)
    await assertSettlement(try root.close()) {
      XCTAssertTrue($0.ownerClosed)
      XCTAssertNil($0.failure)
    }
  }

  func testScanBridgeReturnsOpaquePeripheralAndOwnedBytesThroughSingleReader() async throws {
    let fixture = ScanBridgeFixture()
    let root = HybridBlengSessionOwner(ownerFactory: { try fixture.makeOwner() })
    addTeardownBlock { await self.assertSettlement(try root.close()) }
    await assertSettlement(try root.start())
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
    let root = HybridBlengSessionOwner(ownerFactory: { try fixture.makeOwner() })
    addTeardownBlock { await self.assertSettlement(try root.close()) }
    await assertSettlement(try root.start())
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
    let root = HybridBlengSessionOwner(ownerFactory: { try fixture.makeOwner() })
    addTeardownBlock {
      try? fixture.backend.completeHeldScanStart()
      await self.assertSettlement(try root.close())
    }
    await assertSettlement(try root.start())
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
    await assertSettlement(try stop.operation.wait(timeoutMs: 2_000)) { XCTAssertNil($0.failure) }
    XCTAssertEqual(fixture.backend.commandCounts().stops, 1)
  }

  func testScanBridgeReturnsNativeStartFailureWithoutSynthesizingAnEndOrStop() async throws {
    let fixture = ScanBridgeFixture(backend: ScanWiringBackend(rejectFirstScan: true))
    let root = HybridBlengSessionOwner(ownerFactory: { try fixture.makeOwner() })
    addTeardownBlock { await self.assertSettlement(try root.close()) }
    await assertSettlement(try root.start())
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
    let root = HybridBlengSessionOwner(ownerFactory: { try fixture.makeOwner() })
    addTeardownBlock { await self.assertSettlement(try root.close()) }
    await assertSettlement(try root.start())
    let scan = try scanBridgeAccepted(root)
    await assertSettlement(try scan.waitUntilStarted(timeoutMs: 2_000)) { XCTAssertNil($0.failure) }
    await assertSettlement(try scan.nextEvent(timeoutMs: 1)) {
      guard case let .third(rejected) = $0 else { return XCTFail("Idle pull must time out, not end") }
      XCTAssertEqual(rejected.failure.domain.stringValue, "observation")
      XCTAssertEqual(rejected.failure.code, "bleng.timeout")
    }
    XCTAssertEqual(fixture.backend.commandCounts().stops, 0)
    guard case let .first(accepted) = try scan.beginStop() else { return XCTFail("Expected typed stop") }
    await assertSettlement(try accepted.operation.wait(timeoutMs: .nan)) {
      XCTAssertEqual($0.failure?.code, "invalid_timeout")
    }
    await assertSettlement(try accepted.operation.wait(timeoutMs: 2_000)) { XCTAssertNil($0.failure) }
    await assertSettlement(try accepted.operation.wait(timeoutMs: 2_000)) {
      XCTAssertEqual($0.failure?.code, "bleng.operation_result_consumed")
    }
    await assertSettlement(try accepted.operation.cancel()) {
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
    let root = HybridBlengSessionOwner(ownerFactory: { try fixture.makeOwner() })
    addTeardownBlock { await self.assertSettlement(try root.close()) }
    await assertSettlement(try root.start())
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
    stop.operation.dispose()
    await assertSettlement(try stop.operation.wait(timeoutMs: 2_000)) { XCTAssertNil($0.failure) }
    XCTAssertEqual(fixture.backend.commandCounts().stops, 1)
  }

  func testScanChildDoesNotRetainRootAndDestructionDoesNotStopNativeScan() async throws {
    let fixture = ScanBridgeFixture()
    var root: HybridBlengSessionOwner? = HybridBlengSessionOwner(ownerFactory: { try fixture.makeOwner() })
    weak var weakRoot = root
    await assertSettlement(try XCTUnwrap(root).start())
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
    let failure = ForeignScanFailure(
      domain: .operation, code: "native.code", message: "native display 🦷",
      platformDetails: PlatformErrorDetails(platform: .apple, code: "42", name: "CBError", message: "native detail")
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
      let nativeFailure = ForeignScanFailure(
        domain: failure.domain, code: failure.code, message: failure.message,
        platformDetails: PlatformErrorDetails(platform: platform, code: "42", name: "SDKError", message: "native detail")
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
      serviceUuids: [], namePrefix: nil, companyId: nil, allowDuplicates: false
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
    BlengScanOptions(serviceUuids: [], namePrefix: nil, companyId: companyId, allowDuplicates: false)
  }

  private func scanBridgeAccepted(_ root: HybridBlengSessionOwner) throws -> any HybridBlengScanSpec {
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

  init(backend: ScanWiringBackend = ScanWiringBackend(), replacementBackend: ScanWiringBackend? = nil) {
    self.backend = backend
    self.replacementBackend = replacementBackend
  }

  var creationCount: Int { lock.withLock { creations } }
  var currentOwner: AppleRustSupervisorOwner? { lock.withLock { owner } }

  func makeOwner() throws -> AppleRustSupervisorOwner {
    let backend = lock.withLock { creations == 0 ? self.backend : replacementBackend ?? self.backend }
    let evidence = self.evidence
    let owner = try AppleRustSupervisorOwner(
      queueLabel: "BleHubScanBridgeNoRadio",
      eventSink: { evidence.record($0) },
      backendFactory: { _ in
        ApplePlatformBackendLease(
          backend: backend, resourceSnapshot: { backend.resources() },
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
  func cancelOperation(request: CancelOperationRequest) throws { throw unexpected() }
}
