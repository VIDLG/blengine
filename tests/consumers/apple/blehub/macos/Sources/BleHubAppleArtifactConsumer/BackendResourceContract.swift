import Foundation
@_spi(AppleDiagnostics) import BleHubApple
import BleHubUniFfi

private enum BackendResourceContractFailure: Error {
    case invalid(String)
}

private struct BackendResourceCleanupFailure: Error, CustomStringConvertible {
    let primary: Error?
    let cleanup: [Error]

    var description: String {
        let primaryDescription = primary.map { String(describing: $0) } ?? "none"
        return "backend resources primary=\(primaryDescription); cleanup=\(cleanup)"
    }
}

private final class BackendResourceSink: BleEventSink {
    func onEvent(event: BleEvent) throws {}
}

// Only the consumer caller waits. The backend callback lane just publishes and signals.
private final class BackendResourceSnapshot: @unchecked Sendable {
    private let lock = NSLock()
    private let ready = DispatchSemaphore(value: 0)
    private var counts: [String: Int]?

    func complete(_ counts: [String: Int]) {
        lock.lock()
        self.counts = counts
        lock.unlock()
        ready.signal()
    }

    func wait() throws -> [String: Int] {
        guard ready.wait(timeout: .now() + 5) == .success else {
            throw BackendResourceContractFailure.invalid("backend resource snapshot timed out after 5s")
        }
        lock.lock()
        defer { lock.unlock() }
        guard let counts else {
            throw BackendResourceContractFailure.invalid("backend resource snapshot was not published")
        }
        return counts
    }
}

private func snapshotBackendResources(_ backend: AppleBleBackend) throws -> [String: Int] {
    let snapshot = BackendResourceSnapshot()
    backend.diagnosticSnapshotRfResources { counts in
        snapshot.complete(counts)
    }
    return try snapshot.wait()
}

// No live initialize, scan or connection: this observes packaged native ownership, not RF.
func verifyBackendResourceContract() throws {
    let backend = try AppleBleBackend(queueLabel: "dev.blehub.artifact.resources")
    var hub: BleHub?
    var replacement: AppleBleBackend?
    let outcome = Result<Void, Error> {
        let assembled = try BleHub(backend: backend, downstream: BackendResourceSink())
        hub = assembled
        let expectedBound = [
            "activeScans": 0,
            "connections": 0,
            "gattAttributes": 0,
            "subscriptions": 0,
            "pendingIo": 0,
            "watchdogs": 0,
            "callbackRoutes": 0,
            "ingressRoots": 1,
            "adapterDelegates": 0,
            "peerCacheEntries": 0,
            "backendLeases": 1,
        ]
        let bound = try snapshotBackendResources(backend)
        guard bound == expectedBound else {
            throw BackendResourceContractFailure.invalid("bound backend resources mismatch: \(bound)")
        }

        try assembled.dispose()
        try backend.dispose()
        let disposed = try snapshotBackendResources(backend)
        // Dictionary equality checks all eleven keys; an empty map is never resource-zero.
        guard disposed == expectedBound.mapValues({ _ in 0 }) else {
            throw BackendResourceContractFailure.invalid("disposed backend resources mismatch: \(disposed)")
        }
        do {
            try backend.initialize(command: InitializeCommand(commandId: CommandId(value: 99)))
            throw BackendResourceContractFailure.invalid("disposed backend accepted initialize")
        } catch CommandDispatchError.Closed {
            // The same strongly retained backend must reject before creating an OS manager.
        }

        let next = try AppleBleBackend(queueLabel: "dev.blehub.artifact.resources.replacement")
        replacement = next
        var expectedReplacement = expectedBound
        expectedReplacement["ingressRoots"] = 0
        let reused = try snapshotBackendResources(next)
        guard reused == expectedReplacement else {
            throw BackendResourceContractFailure.invalid("replacement backend resources mismatch: \(reused)")
        }
        try next.dispose()
    }

    // Attempt every cleanup even after a failed assertion, assembly or earlier disposal.
    var cleanup: [Error] = []
    do { try hub?.dispose() } catch { cleanup.append(error) }
    do { try backend.dispose() } catch { cleanup.append(error) }
    do { try replacement?.dispose() } catch { cleanup.append(error) }
    if !cleanup.isEmpty {
        let primary: Error?
        switch outcome {
        case .success: primary = nil
        case .failure(let error): primary = error
        }
        throw BackendResourceCleanupFailure(primary: primary, cleanup: cleanup)
    }
    try outcome.get()
}
