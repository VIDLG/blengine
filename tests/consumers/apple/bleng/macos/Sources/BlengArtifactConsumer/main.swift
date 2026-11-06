import Dispatch
import Darwin
import Foundation
import BleHubUniFfi

enum ConsumerFailure: Error {
    case invalid(String)
}

func require(_ condition: @autoclosure () -> Bool, _ message: String) throws {
    if !condition() { throw ConsumerFailure.invalid(message) }
}

func waitForSignal(_ signal: DispatchSemaphore, _ message: String) async throws {
    // Backend entry comes from a foreign callback; do not block a Swift executor.
    let entered = await withCheckedContinuation { continuation in
        DispatchQueue.global().async {
            continuation.resume(returning: signal.wait(timeout: .now() + .seconds(2)) == .success)
        }
    }
    try require(entered, message)
}

private final class ArtifactBackend: BleBackend, @unchecked Sendable {
    private let lock = NSLock()
    private var ingress: BackendEventIngressHandle?
    private var disposed = 0
    private var initializationCalls = 0
    private var initializationEvents = 0
    private let completeInitialization: Bool
    private let initializationSnapshot: AdapterSnapshot
    private let initializationEntered = DispatchSemaphore(value: 0)

    init(completeInitialization: Bool = true, snapshot: AdapterSnapshot? = nil) {
        self.completeInitialization = completeInitialization
        self.initializationSnapshot = snapshot ?? AdapterSnapshot(
            state: .poweredOn,
            scanAuthorization: .authorized,
            connectionAuthorization: .authorized,
            name: nil
        )
    }

    func capabilities() throws -> BackendCapabilities {
        BackendCapabilities(
            platform: .test,
            contractVersion: blehubContractVersion(),
            supportsDuplicateScanResults: false,
            supportsConnectableFlag: false,
            supportsReadRssi: false,
            supportsRequiredNotification: false,
            supportsRequiredIndication: false,
            reportsActualNotificationMode: false
        )
    }

    func bindEventIngress(ingress: BackendEventIngressHandle) throws {
        lock.withLock { self.ingress = ingress }
    }

    func retainedIngress() -> BackendEventIngressHandle {
        lock.withLock { ingress! }
    }

    func disposeCount() -> Int {
        lock.withLock { disposed }
    }

    func initializeCount() -> Int {
        lock.withLock { initializationCalls }
    }

    func initializationEventCount() -> Int {
        lock.withLock { initializationEvents }
    }

    func waitForInitializationEntry() async throws {
        try await waitForSignal(initializationEntered, "foreign initialize did not enter the backend")
    }

    func initialize(command: InitializeCommand) throws {
        lock.withLock { initializationCalls += 1 }
        initializationEntered.signal()
        if !completeInitialization { return }
        let root = retainedIngress()
        let snapshot = initializationSnapshot
        precondition(root.push(event: .adapterSnapshotChanged(
            commandId: command.commandId,
            snapshot: snapshot
        )) == .delivered)
        lock.withLock { initializationEvents += 1 }
        precondition(root.push(event: .commandCompleted(
            commandId: command.commandId,
            error: nil
        )) == .delivered)
        lock.withLock { initializationEvents += 1 }
    }

    func startScan(command: StartScanCommand) throws {
        let root = retainedIngress()
        precondition(root.push(event: .scanStarted(
            commandId: command.commandId,
            scanId: command.scanSessionId
        )) == .delivered)
        precondition(root.push(event: .commandCompleted(
            commandId: command.commandId,
            error: nil
        )) == .delivered)
    }

    func stopScan(command: StopScanCommand) throws { fatalError("unused") }
    func connect(command: ConnectCommand) throws { fatalError("unused") }
    func disconnect(command: DisconnectCommand) throws { fatalError("unused") }
    func discoverGatt(command: DiscoverGattCommand) throws { fatalError("unused") }
    func readCharacteristic(command: ReadCharacteristicCommand) throws { fatalError("unused") }
    func writeCharacteristic(command: WriteCharacteristicCommand) throws { fatalError("unused") }
    func setNotifications(command: SetNotificationsCommand) throws { fatalError("unused") }
    func readRssi(command: ReadRssiCommand) throws { fatalError("unused") }
    func cancelOperation(request: CancelOperationRequest) throws { fatalError("unused") }

    func dispose() throws {
        lock.withLock {
            disposed += 1
            ingress = nil
        }
    }
}

private func close(_ owner: Bleng) async {
    try? await owner.shutdown()
    try? await owner.shutdown()
}

private func settle<T>(_ task: Task<T, Error>) async {
    _ = try? await task.value
}

@main
private struct BlengArtifactConsumer {
    static func main() async {
        do {
            try await run()
        } catch {
            let message = "apple_bleng_artifact_consumer=failed: \(String(reflecting: error))\n"
            FileHandle.standardError.write(Data(message.utf8))
            exit(1)
        }
    }

    private static func run() async throws {
        let unavailableSnapshot = AdapterSnapshot(
            state: .poweredOff,
            scanAuthorization: .denied,
            connectionAuthorization: .denied,
            name: "fake-adapter"
        )
        let unavailableBackend = ArtifactBackend(snapshot: unavailableSnapshot)
        let unavailableOwner = try Bleng()
        do {
            try await unavailableOwner.assembleBackend(backend: unavailableBackend)
            for timeoutMs: UInt64 in [0, 86_400_001, UInt64.max] {
                do {
                    _ = try await unavailableOwner.initialize(timeoutMs: timeoutMs)
                    throw ConsumerFailure.invalid("invalid timeout \(timeoutMs) was accepted")
                } catch BlengError.InvalidTimeout {}
                try require(
                    unavailableBackend.initializeCount() == 0,
                    "invalid timeout \(timeoutMs) reached the backend"
                )
                try require(
                    unavailableBackend.initializationEventCount() == 0,
                    "invalid timeout \(timeoutMs) sent initialization ingress events"
                )
            }
            let returned = try await unavailableOwner.initialize(timeoutMs: 86_400_000)
            try require(
                unavailableBackend.initializeCount() == 1,
                "maximum timeout initialization was not dispatched exactly once"
            )
            try require(
                unavailableBackend.initializationEventCount() == 2,
                "maximum timeout initialization did not deliver its snapshot and terminal"
            )
            try require(returned.state == unavailableSnapshot.state, "powered-off snapshot state changed")
            try require(
                returned.scanAuthorization == unavailableSnapshot.scanAuthorization,
                "denied scan authorization changed"
            )
            try require(
                returned.connectionAuthorization == unavailableSnapshot.connectionAuthorization,
                "denied connection authorization changed"
            )
            try require(returned.name == unavailableSnapshot.name, "snapshot name changed")
            try await unavailableOwner.shutdown()
            try await unavailableOwner.shutdown()
            try require(unavailableBackend.disposeCount() == 1, "unavailable backend dispose was not exactly once")
        } catch {
            await close(unavailableOwner)
            throw error
        }

        let backend = ArtifactBackend()
        let owner = try Bleng()
        do {
            try await owner.assembleBackend(backend: backend)
            let lateIngress = backend.retainedIngress()
            let snapshot = try await owner.initialize(timeoutMs: 2_000)
            try require(snapshot.state == .poweredOn, "powered-on initialization failed")
            let scan = try await owner.startScan(
                options: ScanOptions(
                    serviceUuids: [], namePrefix: nil, companyId: nil, allowDuplicates: false
                ),
                timeoutMs: 2_000
            )
            try require(scan.value != 0, "BLeng returned an invalid scan ID")
            try await owner.shutdown()
            try await owner.shutdown()
            try require(backend.disposeCount() == 1, "backend dispose was not exactly once")
            try require(lateIngress.push(event: .commandCompleted(
                commandId: CommandId(value: 99), error: nil
            )) == .closed, "late ingress remained open")
            do {
                _ = try await owner.initialize(timeoutMs: 20)
                throw ConsumerFailure.invalid("closed BLeng package accepted an operation")
            } catch BlengError.Closed {}
        } catch {
            await close(owner)
            throw error
        }

        let eventBackend = ArtifactBackend()
        let eventOwner = try Bleng()
        try await eventOwner.assembleBackend(backend: eventBackend)
        do {
            _ = try await eventOwner.nextEvent(timeoutMs: 1)
            throw ConsumerFailure.invalid("empty event reader did not time out")
        } catch BlengError.Timeout {}
        let firstReaderStarted = DispatchSemaphore(value: 0)
        let secondReaderStarted = DispatchSemaphore(value: 0)
        let firstReader = Task<Optional<BleEvent>, Error> {
            firstReaderStarted.signal()
            return try await eventOwner.nextEvent(timeoutMs: 86_400_000)
        }
        let secondReader = Task<Optional<BleEvent>, Error> {
            secondReaderStarted.signal()
            return try await eventOwner.nextEvent(timeoutMs: 86_400_000)
        }
        do {
            try await waitForSignal(firstReaderStarted, "first nextEvent task did not start")
            try await waitForSignal(secondReaderStarted, "second nextEvent task did not start")
            try await eventOwner.shutdown()
            try await eventOwner.shutdown()
            let firstResult = try await firstReader.value
            let secondResult = try await secondReader.value
            if firstResult != nil || secondResult != nil {
                throw ConsumerFailure.invalid("blocked nextEvent call returned an event during shutdown")
            }
            try require(eventBackend.disposeCount() == 1, "event backend dispose was not exactly once")
            let afterClose = try await eventOwner.nextEvent(timeoutMs: 1)
            try require(afterClose == nil, "closed event reader did not return end-of-stream")
        } catch {
            await close(eventOwner)
            await settle(firstReader)
            await settle(secondReader)
            throw error
        }

        let pendingBackend = ArtifactBackend(completeInitialization: false)
        let pendingOwner = try Bleng()
        try await pendingOwner.assembleBackend(backend: pendingBackend)
        let pendingIngress = pendingBackend.retainedIngress()
        let pending = Task<AdapterSnapshot, Error> {
            try await pendingOwner.initialize(timeoutMs: 5_000)
        }
        do {
            try await pendingBackend.waitForInitializationEntry()
            try require(pendingBackend.initializeCount() == 1, "pending initialization was not dispatched once")
            // Swift cancellation alone does not cancel the Rust future/native command.
            pending.cancel()
            try await pendingOwner.shutdown()
            try await pendingOwner.shutdown()
            do {
                _ = try await pending.value
                throw ConsumerFailure.invalid("pending initialize succeeded after shutdown")
            } catch BlengError.Closed {}
            try require(pendingBackend.disposeCount() == 1, "pending backend was not disposed once")
            try require(pendingIngress.push(event: .commandCompleted(
                commandId: CommandId(value: 100), error: nil
            )) == .closed, "pending backend late ingress remained open")
        } catch {
            await close(pendingOwner)
            await settle(pending)
            throw error
        }
        try await runTypedOperationRegression()
        print("apple_bleng_artifact_consumer=passed")
    }
}
