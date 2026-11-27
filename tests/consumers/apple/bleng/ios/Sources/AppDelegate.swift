import BleHubUniFfi
import Dispatch
import Foundation
import UIKit

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

    func retainedIngress() throws -> BackendEventIngressHandle {
        guard let ingress = lock.withLock({ self.ingress }) else {
            throw ConsumerFailure.invalid("backend ingress not bound")
        }
        return ingress
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
        let root = try retainedIngress()
        let snapshot = initializationSnapshot
        try require(root.push(event: .adapterSnapshotChanged(
            commandId: command.commandId, snapshot: snapshot
        )) == .delivered, "initialization snapshot ingress failed")
        lock.withLock { initializationEvents += 1 }
        try require(root.push(event: .commandCompleted(
            commandId: command.commandId, error: nil
        )) == .delivered, "initialization terminal ingress failed")
        lock.withLock { initializationEvents += 1 }
    }

    func startScan(command: StartScanCommand) throws {
        let root = try retainedIngress()
        try require(root.push(event: .scanStarted(
            commandId: command.commandId, scanId: command.scanSessionId
        )) == .delivered, "scan domain ingress failed")
        try require(root.push(event: .commandCompleted(
            commandId: command.commandId, error: nil
        )) == .delivered, "scan terminal ingress failed")
    }

    func stopScan(command: StopScanCommand) throws { fatalError("unused") }
    func connect(command: ConnectCommand) throws { fatalError("unused") }
    func disconnect(command: DisconnectCommand) throws { fatalError("unused") }
    func discoverGatt(command: DiscoverGattCommand) throws { fatalError("unused") }
    func readCharacteristic(command: ReadCharacteristicCommand) throws { fatalError("unused") }
    func writeCharacteristic(command: WriteCharacteristicCommand) throws { fatalError("unused") }
    func readDescriptor(command: ReadDescriptorCommand) throws { fatalError("unused") }
    func writeDescriptor(command: WriteDescriptorCommand) throws { fatalError("unused") }
    func setNotifications(command: SetNotificationsCommand) throws { fatalError("unused") }
    func readRssi(command: ReadRssiCommand) throws { fatalError("unused") }
    func setAdapterEnabled(command: SetAdapterEnabledCommand) throws { fatalError("unused") }
    func queryDevices(command: QueryDevicesCommand) throws { fatalError("unused") }
    func associateCompanionDevice(command: AssociateCompanionDeviceCommand) throws { fatalError("unused") }
    func mtu(command: MtuCommand) throws { fatalError("unused") }
    func setConnectionPriority(command: SetConnectionPriorityCommand) throws { fatalError("unused") }
    func createBond(command: CreateBondCommand) throws { fatalError("unused") }
    func removeBond(command: RemoveBondCommand) throws { fatalError("unused") }
    func refreshGattCache(command: RefreshGattCacheCommand) throws { fatalError("unused") }
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

private func verifyTypedOperations() async throws {
    try await runTypedOperationRegression()
}

private func verifyBlengArtifact() async throws {
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
        let lateIngress = try backend.retainedIngress()
        let snapshot = try await owner.initialize(timeoutMs: 2_000)
        try require(snapshot.state == .poweredOn, "powered-on initialization failed")
        let scan = try await owner.startScan(
            options: ScanOptions(serviceUuids: [], namePrefix: nil, exactName: nil, companyId: nil, minRssi: nil, manufacturerData: nil, android: nil, allowDuplicates: false),
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
    let pendingIngress = try pendingBackend.retainedIngress()
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
            throw ConsumerFailure.invalid("pending initialization succeeded after shutdown")
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
}

@main
final class AppDelegate: UIResponder, UIApplicationDelegate {
    var window: UIWindow?

    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil
    ) -> Bool {
        let args = ProcessInfo.processInfo.arguments
        guard let index = args.firstIndex(of: "--run-id"), args.indices.contains(index + 1) else {
            return false
        }
        let runId = args[index + 1]
        guard UUID(uuidString: runId) != nil else { return false }

        let status = UILabel()
        status.textAlignment = .center
        status.numberOfLines = 0
        status.text = "BLeng artifact: running"
        let controller = UIViewController()
        controller.view.backgroundColor = .systemBackground
        controller.view.addSubview(status)
        status.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            status.centerXAnchor.constraint(equalTo: controller.view.centerXAnchor),
            status.centerYAnchor.constraint(equalTo: controller.view.centerYAnchor),
        ])
        let window = UIWindow(frame: UIScreen.main.bounds)
        window.rootViewController = controller
        window.makeKeyAndVisible()
        self.window = window

        Task {
            let result: String
            do {
                try await verifyTypedOperations()
                try await verifyBlengArtifact()
                status.text = "BLeng artifact: passed\ntyped_operations=passed"
                result = "typed_operations=passed\napple_bleng_ios_artifact_consumer=passed\n"
            } catch {
                status.text = "BLeng artifact: failed"
                result = "apple_bleng_ios_artifact_consumer=failed\nerror=\(error)\n"
            }
            let documents = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            let output = documents.appendingPathComponent("bleng-ios-artifact-result.txt")
            do {
                try ("apple_bleng_ios_run_id=\(runId)\n" + result)
                    .write(to: output, atomically: true, encoding: .utf8)
            } catch {
                NSLog("cannot write BLeng iOS artifact result: %@", String(describing: error))
            }
        }
        return true
    }
}
