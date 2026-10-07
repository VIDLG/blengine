import Foundation

final class ProbeBackend: BleBackend, @unchecked Sendable {
    private let lock = NSLock()
    private var observedCapabilities = 0
    private var observedDispose = 0
    private var ingress: BackendEventIngressHandle?
    private var initializationCount = 0
    private let completeInitialization: Bool

    init(completeInitialization: Bool = true) {
        self.completeInitialization = completeInitialization
    }

    func capabilities() throws -> BackendCapabilities {
        lock.withLock { observedCapabilities += 1 }
        precondition(!Thread.isMainThread)
        return BackendCapabilities(
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

    func callbackIngress() -> BackendEventIngressHandle {
        lock.withLock { ingress! }
    }

    func initialize(command: InitializeCommand) throws {
        lock.withLock { initializationCount += 1 }
        if !completeInitialization { return }
        let root = callbackIngress()
        let snapshot = AdapterSnapshot(
            state: .poweredOn,
            scanAuthorization: .authorized,
            connectionAuthorization: .authorized,
            name: nil
        )
        precondition(root.push(event: .adapterSnapshotChanged(
            commandId: command.commandId,
            snapshot: snapshot
        )) == .delivered)
        precondition(root.push(event: .commandCompleted(
            commandId: command.commandId,
            error: nil
        )) == .delivered)
    }

    func startScan(command: StartScanCommand) throws {
        let root = callbackIngress()
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

    func dispose() throws {
        lock.withLock {
            observedDispose += 1
            ingress = nil
        }
    }

    func counts() -> (Int, Int) {
        lock.withLock { (observedCapabilities, observedDispose) }
    }

    func initializationCalls() -> Int {
        lock.withLock { initializationCount }
    }
}

@main
struct Smoke {
    static func main() async throws {
        precondition(blehubContractVersion() == 5)
        let probe = try AsyncProbe()
        let echoed = try await probe.roundTrip(value: UInt64.max)
        precondition(echoed == UInt64.max)
        let backend = ProbeBackend()
        let backendVersion = try await probe.probeForeignBackend(backend: backend)
        precondition(backendVersion == 5)
        precondition(backend.counts() == (1, 1))

        let waiting = Task { try await probe.waitForRelease() }
        for _ in 0..<200 {
            if probe.isWaiting() { break }
            try await Task.sleep(nanoseconds: 10_000_000)
        }
        precondition(probe.isWaiting())
        waiting.cancel()
        try await Task.sleep(nanoseconds: 100_000_000)
        precondition(!probe.wasWaitingFutureDropped())
        probe.releaseWaiter()
        let released = try await waiting.value
        precondition(released == 17, "Swift Task.cancel alone does not cancel this Rust future")
        precondition(probe.wasWaitingFutureDropped())

        let realBackend = ProbeBackend()
        let owner = try BlengForeignProbe()
        try await owner.assembleBackend(backend: realBackend)
        let lateIngress = realBackend.callbackIngress()
        let snapshot = try await owner.initialize(deadlineMs: 2_000)
        precondition(snapshot.state == .poweredOn)
        let scan = try await owner.startScan(deadlineMs: 2_000)
        precondition(scan.value != 0)
        try await owner.shutdown()
        try await owner.shutdown()
        precondition(realBackend.counts() == (1, 1))
        precondition(lateIngress.push(event: .commandCompleted(
            commandId: CommandId(value: 99),
            error: nil
        )) == .closed)
        do {
            _ = try await owner.initialize(deadlineMs: 20)
            fatalError("closed BLeng owner unexpectedly initialized")
        } catch ForeignProbeError.Closed {}

        let pendingBackend = ProbeBackend(completeInitialization: false)
        let pendingOwner = try BlengForeignProbe()
        try await pendingOwner.assembleBackend(backend: pendingBackend)
        let pending = Task { try await pendingOwner.initialize(deadlineMs: 5_000) }
        for _ in 0..<200 {
            if pendingBackend.initializationCalls() == 1 { break }
            try await Task.sleep(nanoseconds: 10_000_000)
        }
        precondition(pendingBackend.initializationCalls() == 1)
        pending.cancel()
        try await Task.sleep(nanoseconds: 100_000_000)
        try await pendingOwner.shutdown()
        do {
            _ = try await pending.value
            fatalError("pending initialization succeeded after explicit close")
        } catch ForeignProbeError.Closed {}
        precondition(pendingBackend.counts() == (1, 1))

        probe.shutdown()
        do {
            _ = try await probe.roundTrip(value: 1)
            fatalError("closed probe unexpectedly accepted an operation")
        } catch ProbeError.Closed {
            print("bleng_uniffi_async_spike_swift=passed")
        }
    }
}
