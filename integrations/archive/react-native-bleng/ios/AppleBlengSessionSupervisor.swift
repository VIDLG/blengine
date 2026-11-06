@_spi(AppleDiagnostics) import BleHubApple
import BleHubUniFfi
import Foundation

struct AppleBlengSessionIdentity: Equatable {
    let generation: Int
    let ownerId: String
    let backendId: String
}

enum AppleBlengSessionSupervisorPhase: Equatable {
    case idle
    case active(AppleBlengSessionIdentity)
    case waitingForRadioOn(AppleBlengSessionIdentity)
    case waitingForAuthorization(AppleBlengSessionIdentity)
    case retiring(AppleBlengSessionIdentity)
    case creating(Int)
    case platformTimeout(String)
    case manualReconnectRequired(String)
    case failed(String)
}

/// A backend plus the native resource snapshot used by the supervisor's
/// retirement barrier. The production owner may inject a backend factory for
/// deterministic lifecycle tests without moving ownership outside the supervisor.
struct AppleBlengBackendLease {
    let backend: any BleBackend
    let snapshot: () async -> [String: Int]
}

enum AppleBlengSessionSupervisorFailure: Error, CustomStringConvertible {
    case alreadyActive
    case noActiveSession
    case oldSessionDidNotRetire
    case invalidAdapterState(String)
    case identityDidNotChange

    var description: String {
        switch self {
        case .alreadyActive:
            return "BLE session supervisor already has an active session"
        case .noActiveSession:
            return "BLE session supervisor has no active session"
        case .oldSessionDidNotRetire:
            return "old BLE session did not retire cleanly"
        case .invalidAdapterState(let state):
            return "new BLE session initialized with invalid adapter state: \(state)"
        case .identityDidNotChange:
            return "fresh BLE session reused the old session identity"
        }
    }
}

private final class BleSessionObservation<Value>: @unchecked Sendable {
    private let lock = NSLock()
    private var value: Value?

    func set(_ value: Value) {
        lock.lock()
        defer { lock.unlock() }
        self.value = value
    }

    func get() -> Value? {
        lock.lock()
        defer { lock.unlock() }
        return value
    }
}

/// Owns one complete Apple `Bleng` + `AppleBleBackend` lifetime.
/// It deliberately lives above Bleng: Bleng itself has no policy for replacing an
/// assembled backend or creating a second owner after radio loss.
///
/// Recovery policy remains outside this class. `replace()` only implements the
/// native fresh-owner barrier; callers decide when the operation is justified.
final class AppleBlengSessionSupervisor: @unchecked Sendable {
    private struct Session {
        let owner: Bleng
        let backend: any BleBackend
        let snapshot: () async -> [String: Int]
        let identity: AppleBlengSessionIdentity
        let eventReader: Task<Void, Never>
    }

    private let queueLabel: String
    private let eventSink: (@Sendable (BleEvent) -> Void)?
    private let eventReaderErrorSink: (@Sendable (String) -> Void)?
    private let backendFactory: (String) throws -> AppleBlengBackendLease
    private var session: Session?
    private var generationCounter = 0
    private(set) var phase: AppleBlengSessionSupervisorPhase = .idle
    private(set) var lastRetiredResources: [String: Int] = [:]
    private(set) var initializationCommandId: CommandId?

    init(
        queueLabel: String,
        eventSink: (@Sendable (BleEvent) -> Void)? = nil,
        eventReaderErrorSink: (@Sendable (String) -> Void)? = nil,
        backendFactory: ((String) throws -> AppleBlengBackendLease)? = nil
    ) {
        self.queueLabel = queueLabel
        self.eventSink = eventSink
        self.eventReaderErrorSink = eventReaderErrorSink
        self.backendFactory = backendFactory ?? { queueLabel in
            let backend = try AppleBleBackend(queueLabel: queueLabel)
            return AppleBlengBackendLease(
                backend: backend,
                snapshot: { await Self.snapshot(backend) }
            )
        }
        self.session = nil
    }

    var identity: AppleBlengSessionIdentity? { session?.identity }

    func activeOwner() throws -> Bleng {
        guard let session else { throw AppleBlengSessionSupervisorFailure.noActiveSession }
        return session.owner
    }


    func start() async throws -> AdapterSnapshot {
        guard session == nil else { throw AppleBlengSessionSupervisorFailure.alreadyActive }
        generationCounter += 1
        let generation = generationCounter
        phase = .creating(generation)
        let lease: AppleBlengBackendLease
        do {
            lease = try backendFactory("\(queueLabel)-g\(generation)")
        } catch {
            phase = .failed(String(describing: error))
            throw error
        }
        let backend = lease.backend
        let owner: Bleng
        do {
            owner = try Bleng()
        } catch {
            try? backend.dispose()
            phase = .failed(String(describing: error))
            throw error
        }

        let reader = Task { [owner, eventSink] in
            do {
                while let event = try await owner.nextEvent(timeoutMs: 300_000) {
                    eventSink?(event)
                }
            } catch {
                // Shutdown and native retirement are the terminal barriers. A reader
                // error is reported to the owning scenario and is never retried here.
                eventReaderErrorSink?(String(describing: error))
            }
        }

        do {
            try await owner.assembleBackend(backend: backend)
            let initialize = try owner.beginInitialize()
            let initializeCommandId = initialize.commandId()
            let snapshot = try await initialize.wait(timeoutMs: 30_000)
            let identity = AppleBlengSessionIdentity(
                generation: generation,
                ownerId: UUID().uuidString.lowercased(),
                backendId: UUID().uuidString.lowercased()
            )
            session = Session(
                owner: owner,
                backend: backend,
                snapshot: lease.snapshot,
                identity: identity,
                eventReader: reader
            )
            initializationCommandId = initializeCommandId
            if snapshot.scanAuthorization != .authorized {
                phase = .waitingForAuthorization(identity)
            } else if snapshot.state != .poweredOn {
                phase = .waitingForRadioOn(identity)
            } else {
                phase = .active(identity)
            }
            return snapshot
        } catch {
            try? await owner.shutdown()
            reader.cancel()
            _ = await reader.value
            try? backend.dispose()
            phase = .failed(String(describing: error))
            throw error
        }
    }

    /// Retires the current owner completely. A new owner must not be created until
    /// this method returns, because the backend process lease and event reader are
    /// part of the old session's cleanup barrier.
    func retire() async throws -> [String: Int] {
        guard let current = session else {
            throw AppleBlengSessionSupervisorFailure.noActiveSession
        }
        phase = .retiring(current.identity)
        session = nil

        var cleanupError: Error?
        do {
            try await current.owner.shutdown()
        } catch {
            cleanupError = error
        }
        _ = await current.eventReader.value

        do {
            try current.backend.dispose()
        } catch {
            cleanupError = cleanupError ?? error
        }

        let resources = await current.snapshot()
        lastRetiredResources = resources
        let requiredResourcesAreZero =
            resources["activeScans"] == 0 &&
            resources["connections"] == 0 &&
            resources["gattAttributes"] == 0 &&
            resources["subscriptions"] == 0 &&
            resources["pendingIo"] == 0 &&
            resources["watchdogs"] == 0 &&
            resources["callbackRoutes"] == 0 &&
            resources["ingressRoots"] == 0 &&
            resources["adapterDelegates"] == 0 &&
            resources["backendLeases"] == 0

        if let cleanupError {
            phase = .failed(String(describing: cleanupError))
            throw cleanupError
        }
        guard requiredResourcesAreZero else {
            let failure = AppleBlengSessionSupervisorFailure.oldSessionDidNotRetire
            phase = .failed("retired BLE session still owns native resources: \(resources)")
            throw failure
        }
        phase = .idle
        return resources
    }

    /// Creates a new logical owner only after the old owner and native backend
    /// have crossed their complete retirement barrier.
    func replace() async throws -> AdapterSnapshot {
        let oldIdentity = identity
        _ = try await retire()
        let snapshot = try await start()
        guard let newIdentity = identity, newIdentity != oldIdentity else {
            phase = .failed(AppleBlengSessionSupervisorFailure.identityDidNotChange.description)
            throw AppleBlengSessionSupervisorFailure.identityDidNotChange
        }
        return snapshot
    }

    func close() async throws {
        guard session != nil else {
            phase = .idle
            return
        }
        _ = try await retire()
    }

    /// Records a bounded platform failure without pretending that radio restoration
    /// or an RF observation completed the BLE recovery contract.
    func markPlatformTimeout(_ reason: String) {
        phase = .platformTimeout(reason)
    }

    /// Records that the caller must explicitly request another fresh-session attempt.
    func markManualReconnectRequired(_ reason: String) {
        phase = .manualReconnectRequired(reason)
    }

    static func snapshot(_ backend: AppleBleBackend) async -> [String: Int] {
        let observation = BleSessionObservation<[String: Int]>()
        backend.diagnosticSnapshotRfResources { observation.set($0) }
        for _ in 0..<20 {
            if let value = observation.get() { return value }
            try? await Task.sleep(nanoseconds: 50_000_000)
        }
        return [:]
    }
}
