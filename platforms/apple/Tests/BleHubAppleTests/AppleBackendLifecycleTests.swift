import BleHubUniFfi

import XCTest
@_spi(AppleDiagnostics) @testable import BleHubApple

final class AppleBackendLifecycleTests: XCTestCase {
    func testInitializationTransitionsExactlyOnceOnFirstStateCallback() throws {
        let commandId = CommandId(value: 42)
        var lifecycle = AppleBackendLifecycle.inert

        try lifecycle.beginInitialization(commandId: commandId)
        XCTAssertEqual(lifecycle, .initializing(commandId))
        XCTAssertEqual(lifecycle.consumeInitialStateCallback(), commandId)
        XCTAssertEqual(lifecycle, .ready)
        XCTAssertNil(lifecycle.consumeInitialStateCallback())
    }

    func testRepeatedInitializationIsRejectedBeforeAcceptance() throws {
        var lifecycle = AppleBackendLifecycle.inert
        try lifecycle.beginInitialization(commandId: CommandId(value: 1))

        XCTAssertThrowsError(
            try lifecycle.beginInitialization(commandId: CommandId(value: 2))
        ) { error in
            guard case CommandDispatchError.InvalidState = error else {
                return XCTFail("unexpected error: \(error)")
            }
        }
    }

    func testFaultedLifecycleRejectsOperationsAndCannotLeaveClosedState() {
        var lifecycle = AppleBackendLifecycle.ready
        lifecycle.markFaulted()
        XCTAssertEqual(lifecycle, .faulted)
        XCTAssertThrowsError(try lifecycle.requireReady(operation: "test operation")) { error in
            guard case CommandDispatchError.InvalidState = error else {
                return XCTFail("unexpected error: \(error)")
            }
        }
        XCTAssertThrowsError(
            try lifecycle.beginInitialization(commandId: CommandId(value: 7))
        ) { error in
            guard case CommandDispatchError.InvalidState = error else {
                return XCTFail("unexpected error: \(error)")
            }
        }

        lifecycle = .closed
        lifecycle.markFaulted()
        XCTAssertEqual(lifecycle, .closed)
    }

    func testProcessLeaseRejectsContentionAndIsReleasedByDispose() throws {
        let owner = try AppleBleBackend(queueLabel: "dev.blehub.apple.tests.lease-owner")
        defer { try? owner.dispose() }

        XCTAssertThrowsError(
            try AppleBleBackend(queueLabel: "dev.blehub.apple.tests.lease-contender")
        ) { error in
            guard case BleHubError.ResourceBusy = error else {
                return XCTFail("unexpected error: \(error)")
            }
        }

        try owner.dispose()
        let replacement = try AppleBleBackend(
            queueLabel: "dev.blehub.apple.tests.lease-replacement"
        )
        try replacement.dispose()
    }

    func testResourceSnapshotObservesBoundIngressAndLeaseThenDisposedBackend() throws {
        let backend = try AppleBleBackend(queueLabel: "dev.blehub.apple.tests.resources")
        defer { try? backend.dispose() }
        let hub = try BleHub(backend: backend, downstream: ResourceTestSink())
        let bound = expectation(description: "bound backend ownership")
        backend.diagnosticSnapshotRfResources { counts in
            XCTAssertEqual(counts.count, 11)
            XCTAssertEqual(counts["ingressRoots"], 1)
            XCTAssertEqual(counts["backendLeases"], 1)
            XCTAssertEqual(counts["adapterDelegates"], 0, "no manager before initialization")
            bound.fulfill()
        }
        wait(for: [bound], timeout: 5)

        try hub.dispose()
        try backend.dispose()
        let disposed = expectation(description: "observed zero on retained disposed backend")
        backend.diagnosticSnapshotRfResources { counts in
            XCTAssertEqual(counts.count, 11)
            XCTAssertTrue(counts.values.allSatisfy { $0 == 0 }, "disposed backend: \(counts)")
            disposed.fulfill()
        }
        wait(for: [disposed], timeout: 5)
        // The backend remains strongly held throughout both observations.
        XCTAssertThrowsError(try backend.initialize(command: InitializeCommand(commandId: CommandId(value: 99))))
    }

    func testFifoSnapshotMissingAndDisposedConnectionIsNilOnBackendQueue() throws {
        let backend = try AppleBleBackend(queueLabel: "dev.blehub.apple.tests.fifo")
        defer { try? backend.dispose() }
        let missing = expectation(description: "missing connection observed on backend queue")
        backend.diagnosticSnapshotGattIo(connectionId: ConnectionId(value: 10)) { snapshot in
            dispatchPrecondition(condition: .notOnQueue(.main))
            XCTAssertNil(snapshot)
            // Reentrant backend work must not deadlock; the second observation is asynchronous.
            do {
                try backend.dispose()
            } catch {
                XCTFail("reentrant dispose failed: \(error)")
            }
            backend.diagnosticSnapshotGattIo(connectionId: ConnectionId(value: 10)) { disposed in
                XCTAssertNil(disposed)
                missing.fulfill()
            }
        }
        wait(for: [missing], timeout: 5)
    }

    func testBackendCapabilitiesAndClosedDispatchBoundary() throws {
        let backend = try AppleBleBackend(queueLabel: "dev.blehub.apple.tests.backend")
        let capabilities = try backend.capabilities()

        XCTAssertEqual(capabilities.platform, .apple)
        XCTAssertEqual(capabilities.contractVersion, AppleBleBackend.contractVersion)

        XCTAssertTrue(capabilities.supportsDuplicateScanResults)
        XCTAssertTrue(capabilities.supportsConnectableFlag)
        XCTAssertTrue(capabilities.supportsReadRssi)
        XCTAssertTrue(capabilities.supportsRequiredNotification)
        XCTAssertTrue(capabilities.supportsRequiredIndication)
        XCTAssertTrue(capabilities.reportsActualNotificationMode)

        XCTAssertThrowsError(
            try backend.initialize(
                command: InitializeCommand(commandId: CommandId(value: 3))
            )
        ) { error in
            guard case CommandDispatchError.InvalidState = error else {
                return XCTFail("unexpected error: \(error)")
            }
        }

        try backend.dispose()
        try backend.dispose()

        XCTAssertThrowsError(
            try backend.initialize(
                command: InitializeCommand(commandId: CommandId(value: 4))
            )
        ) { error in
            guard case CommandDispatchError.Closed = error else {
                return XCTFail("unexpected error: \(error)")
            }
        }
    }
}

private final class ResourceTestSink: BleEventSink {
    func onEvent(event: BleEvent) throws {}
}
