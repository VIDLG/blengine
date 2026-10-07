import BleHubUniFfi

import XCTest
@testable import BleHubApple

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
