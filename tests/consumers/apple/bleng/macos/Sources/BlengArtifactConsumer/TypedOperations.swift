import Dispatch
import Foundation
import BleHubUniFfi

// This foreign fixture exercises the installed ABI, not CoreBluetooth or RF.
// Each scenario admits only its finite command sequence and correlated identities.
private final class TypedOperationBackend: BleBackend, @unchecked Sendable {
    enum Scenario {
        case fullFlow
        case scanProxy
        case acceptCancellation
        case rejectCancellation
    }

    private let lock = NSLock()
    private let scenario: Scenario
    private var ingress: BackendEventIngressHandle?
    private var calls: [String] = []
    private var commandIds: Set<CommandId> = []
    private var scan: ScanSessionId?
    private var scanStartCommand: StartScanCommand?
    private var connection: ConnectionId?
    private var session: GattSessionId?
    private var subscription: SubscriptionId?
    private var readCommand: ReadCharacteristicCommand?
    private var pendingConnect: ConnectCommand?
    private var cancellations: [CommandId] = []
    private var disposals = 0
    private let connectEntered = DispatchSemaphore(value: 0)

    let peripheral = PeripheralId(value: 10)
    let characteristic = GattCharacteristicId(value: 30)
    let readValue = Data([0x00, 0x7f, 0x80, 0xff])
    let writeValue = Data([0xff, 0x00, 0x42])
    let notificationValue = Data([0x81, 0x00, 0xfe])
    let snapshot = AdapterSnapshot(
        state: .poweredOn, scanAuthorization: .authorized,
        connectionAuthorization: .authorized, name: "typed-artifact-fixture"
    )

    init(_ scenario: Scenario) {
        self.scenario = scenario
    }

    private var expectedCalls: [String] {
        switch scenario {
        case .fullFlow:
            return [
                "initialize", "startScan", "stopScan", "connect", "discoverGatt",
                "read", "write", "enable", "disable", "rssi", "disconnect",
            ]
        case .scanProxy:
            return ["initialize", "startScan", "stopScan"]
        case .acceptCancellation, .rejectCancellation:
            return ["initialize", "connect"]
        }
    }

    private func check(_ condition: Bool, _ message: String) throws {
        if !condition { throw CommandDispatchError.InvalidRequest(detail: message) }
    }

    private func record(_ name: String, _ id: CommandId) throws {
        try lock.withLock {
            try check(disposals == 0, "command after fixture disposal")
            try check(calls.count < expectedCalls.count, "unexpected extra command: \(name)")
            try check(expectedCalls[calls.count] == name, "out-of-order command: \(name)")
            try check(commandIds.insert(id).inserted, "reused command ID")
            calls.append(name)
        }
    }

    func assertCommandIds(_ expected: [CommandId]) throws {
        try require(expected.count == Set(expected).count, "operation command IDs were reused")
        try require(lock.withLock { commandIds == Set(expected) },
                    "operation IDs did not match actual backend commands")
    }

    private func push(_ event: BleEvent) throws {
        let root = lock.withLock { ingress }
        try check(root?.push(event: event) == .delivered, "fixture event was not delivered: \(event)")
    }

    private func complete(_ id: CommandId) throws {
        try push(.commandCompleted(commandId: id, error: nil))
    }

    func capabilities() throws -> BackendCapabilities {
        BackendCapabilities(
            platform: .test, contractVersion: blehubContractVersion(),
            supportsDuplicateScanResults: false, supportsConnectableFlag: false,
            supportsReadRssi: true, supportsRequiredNotification: true,
            supportsRequiredIndication: true, reportsActualNotificationMode: true
        )
    }

    func bindEventIngress(ingress: BackendEventIngressHandle) throws {
        lock.withLock { self.ingress = ingress }
    }

    func initialize(command: InitializeCommand) throws {
        try record("initialize", command.commandId)
        try push(.adapterSnapshotChanged(commandId: command.commandId, snapshot: snapshot))
        try complete(command.commandId)
    }

    func startScan(command: StartScanCommand) throws {
        try check(command.options == ScanOptions(serviceUuids: [], namePrefix: "typed-fixture", exactName: nil, companyId: nil, minRssi: nil, manufacturerData: nil, android: nil, allowDuplicates: false), "scan options changed across the ABI")
        try record("startScan", command.commandId)
        lock.withLock {
            scan = command.scanSessionId
            scanStartCommand = command
        }
        if case .scanProxy = scenario {
            // All callbacks enter Rust before this foreign dispatch returns, including
            // advertisements preceding Started; only the existing public reader projects them.
            for advertisement in scanAdvertisements(for: command.scanSessionId) {
                try push(.advertisementObserved(advertisement: advertisement))
            }
        }
        try push(.scanStarted(commandId: command.commandId, scanId: command.scanSessionId))
        try complete(command.commandId)
    }

    func completedScanStartCommand() throws -> StartScanCommand {
        guard let command = lock.withLock({ scanStartCommand }) else {
            throw ConsumerFailure.invalid("scan start was not dispatched")
        }
        return command
    }

    func scanAdvertisements(for scan: ScanSessionId) -> [AdvertisementObservation] {
        let service = BluetoothUuid(value: "0000180d-0000-1000-8000-00805f9b34fb")
        return [
            AdvertisementObservation(
                scanSessionId: scan, peripheralId: peripheral, localName: "é🦷\0typed-fixture",
                advertisedServices: [service, service],
                manufacturerData: [ManufacturerData(companyId: 0x0059, payload: readValue)],
                serviceData: [ServiceData(serviceUuid: service, payload: notificationValue)],
                rssi: -61, connectable: true
            ),
            AdvertisementObservation(
                scanSessionId: scan, peripheralId: peripheral, localName: nil,
                advertisedServices: [], manufacturerData: [], serviceData: [],
                rssi: nil, connectable: nil
            ),
        ]
    }

    func injectScanAdvertisement(_ advertisement: AdvertisementObservation) throws {
        guard case .scanProxy = scenario else {
            throw ConsumerFailure.invalid("advertisement injection outside scan proxy scenario")
        }
        try check(lock.withLock { scan == advertisement.scanSessionId }, "injected scan identity changed")
        try push(.advertisementObserved(advertisement: advertisement))
    }

    func stopScan(command: StopScanCommand) throws {
        try check(lock.withLock { scan == command.scanSessionId }, "stop targeted a different scan")
        try record("stopScan", command.commandId)
        try push(.scanStopped(
            scanId: command.scanSessionId,
            result: .ok(reason: .requested(commandId: command.commandId))
        ))
        try complete(command.commandId)
    }

    func connect(command: ConnectCommand) throws {
        try check(command.peripheralId == peripheral, "connect targeted a different peripheral")
        try record("connect", command.commandId)
        lock.withLock {
            connection = command.connectionId
            pendingConnect = command
        }
        connectEntered.signal()
        if case .fullFlow = scenario { try finishConnect() }
    }

    func waitForConnectEntry() async throws {
        try await waitForSignal(connectEntered, "typed connect did not enter the foreign backend")
    }

    func finishConnect() throws {
        let command = lock.withLock { () -> ConnectCommand? in
            defer { pendingConnect = nil }
            return pendingConnect
        }
        guard let command else {
            throw CommandDispatchError.InvalidState(detail: "no pending fixture connect")
        }
        try push(.connectionStateChanged(
            commandId: command.commandId, connectionId: command.connectionId,
            result: .ok(state: .connected)
        ))
        try complete(command.commandId)
    }

    func database() -> GattDatabase {
        let ids = lock.withLock { (connection!, session!) }
        return GattDatabase(connectionId: ids.0, gattSessionId: ids.1, services: [
            GattService(
                id: GattServiceId(value: 20),
                uuid: BluetoothUuid(value: "0000180d-0000-1000-8000-00805f9b34fb"),
                primary: true,
                characteristics: [GattCharacteristic(
                    id: characteristic,
                    uuid: BluetoothUuid(value: "00002a37-0000-1000-8000-00805f9b34fb"),
                    properties: CharacteristicProperties(
                        readable: true, writableWithResponse: true, writableWithoutResponse: false,
                        notifiable: true, indicatable: true
                    ),
                    descriptors: []
                )]
            ),
        ])
    }

    func discoverGatt(command: DiscoverGattCommand) throws {
        try check(lock.withLock { connection == command.connectionId }, "discovery connection changed")
        try record("discoverGatt", command.commandId)
        lock.withLock { session = command.gattSessionId }
        try push(.gattDiscovered(commandId: command.commandId, database: database()))
        try complete(command.commandId)
    }

    private func checkGatt(_ connection: ConnectionId, _ session: GattSessionId) throws {
        try check(lock.withLock { self.connection == connection && self.session == session },
                  "GATT connection/session changed across the ABI")
    }

    func readCharacteristic(command: ReadCharacteristicCommand) throws {
        try checkGatt(command.connectionId, command.gattSessionId)
        try check(command.characteristicId == characteristic, "read characteristic changed")
        try record("read", command.commandId)
        lock.withLock { readCommand = command }
        try push(.characteristicRead(
            commandId: command.commandId, connectionId: command.connectionId,
            gattSessionId: command.gattSessionId, characteristicId: command.characteristicId,
            value: readValue
        ))
        try complete(command.commandId)
    }

    func completedReadCommand() throws -> ReadCharacteristicCommand {
        guard let command = lock.withLock({ readCommand }) else {
            throw ConsumerFailure.invalid("read was not dispatched")
        }
        return command
    }

    func writeCharacteristic(command: WriteCharacteristicCommand) throws {
        try checkGatt(command.connectionId, command.gattSessionId)
        try check(command.characteristicId == characteristic, "write characteristic changed")
        try check(command.value == writeValue && command.writeType == .withResponse,
                  "write bytes/type changed across the ABI")
        try record("write", command.commandId)
        try push(.characteristicWritten(
            commandId: command.commandId, connectionId: command.connectionId,
            gattSessionId: command.gattSessionId, characteristicId: command.characteristicId
        ))
        try complete(command.commandId)
    }

    func setNotifications(command: SetNotificationsCommand) throws {
        try checkGatt(command.connectionId, command.gattSessionId)
        try check(command.characteristicId == characteristic, "notification characteristic changed")
        switch command.action {
        case .enable(let mode):
            try check(mode == .platformDefault, "notification request mode changed")
            try record("enable", command.commandId)
            lock.withLock { subscription = command.subscriptionId }
            // Actual mode remains event evidence; the handle returns only SubscriptionId.
            try push(.notificationStateChanged(
                commandId: command.commandId, connectionId: command.connectionId,
                gattSessionId: command.gattSessionId, subscriptionId: command.subscriptionId,
                characteristicId: command.characteristicId, state: .enabled(actualMode: .indication)
            ))
            try complete(command.commandId)
            try push(.notificationReceived(
                connectionId: command.connectionId, gattSessionId: command.gattSessionId,
                subscriptionId: command.subscriptionId, characteristicId: command.characteristicId,
                value: notificationValue
            ))
        case .disable:
            try check(lock.withLock { subscription == command.subscriptionId },
                      "disable targeted a different subscription")
            try record("disable", command.commandId)
            try push(.notificationStateChanged(
                commandId: command.commandId, connectionId: command.connectionId,
                gattSessionId: command.gattSessionId, subscriptionId: command.subscriptionId,
                characteristicId: command.characteristicId, state: .disabled
            ))
            try complete(command.commandId)
        }
    }

    func readRssi(command: ReadRssiCommand) throws {
        try check(lock.withLock { connection == command.connectionId }, "RSSI connection changed")
        try record("rssi", command.commandId)
        try push(.rssiRead(commandId: command.commandId, connectionId: command.connectionId, rssi: -61))
        try complete(command.commandId)
    }

    func disconnect(command: DisconnectCommand) throws {
        try check(lock.withLock { connection == command.connectionId }, "disconnect connection changed")
        try record("disconnect", command.commandId)
        try push(.connectionStateChanged(
            commandId: command.commandId, connectionId: command.connectionId,
            result: .ok(state: .disconnected)
        ))
        try complete(command.commandId)
    }

    func cancelOperation(request: CancelOperationRequest) throws {
        try lock.withLock {
            try check(calls == ["initialize", "connect"], "cancellation outside pending-connect scenario")
            try check(pendingConnect?.commandId == request.targetCommandId, "cancellation target changed")
            try check(cancellations.isEmpty, "duplicate native cancellation")
            cancellations.append(request.targetCommandId)
        }
        switch scenario {
        case .acceptCancellation:
            // Acceptance is a control receipt, not a fabricated native terminal/rollback.
            return
        case .rejectCancellation:
            throw CommandDispatchError.OperationNotCancellable(targetCommandId: request.targetCommandId)
        case .fullFlow:
            throw CommandDispatchError.InvalidState(detail: "unexpected full-flow cancellation")
        case .scanProxy:
            throw CommandDispatchError.InvalidState(detail: "unexpected scan-proxy cancellation")
        }
    }

    func assertCancellation(_ commandId: CommandId? = nil) throws {
        try require(lock.withLock { cancellations.count == 1 }, "cancel did not reach backend exactly once")
        if let commandId {
            try require(lock.withLock { cancellations == [commandId] }, "cancel receipt lost command identity")
        }
    }

    // The typed scenarios above are the only commands this fixture drives; every capability
    // outside them refuses by name instead of pretending the shape is unused.
    func setAdapterEnabled(command: SetAdapterEnabledCommand) throws {
        try check(false, "unexpected command: setAdapterEnabled")
    }

    func queryDevices(command: QueryDevicesCommand) throws {
        try check(false, "unexpected command: queryDevices")
    }

    func associateCompanionDevice(command: AssociateCompanionDeviceCommand) throws {
        try check(false, "unexpected command: associateCompanionDevice")
    }

    func readDescriptor(command: ReadDescriptorCommand) throws {
        try check(false, "unexpected command: readDescriptor")
    }

    func writeDescriptor(command: WriteDescriptorCommand) throws {
        try check(false, "unexpected command: writeDescriptor")
    }

    func mtu(command: MtuCommand) throws {
        try check(false, "unexpected command: mtu")
    }

    func setConnectionPriority(command: SetConnectionPriorityCommand) throws {
        try check(false, "unexpected command: setConnectionPriority")
    }

    func createBond(command: CreateBondCommand) throws {
        try check(false, "unexpected command: createBond")
    }

    func removeBond(command: RemoveBondCommand) throws {
        try check(false, "unexpected command: removeBond")
    }

    func refreshGattCache(command: RefreshGattCacheCommand) throws {
        try check(false, "unexpected command: refreshGattCache")
    }

    func expectedConnection() -> ConnectionId? {
        lock.withLock { connection }
    }

    func dispose() throws {
        lock.withLock {
            disposals += 1
            ingress = nil
        }
    }

    func resourceSnapshot() throws -> [ForeignResourceCount] {
        try lock.withLock {
            guard disposals <= 1 else {
                throw SupervisorFfiError.Platform(detail: "typed fixture disposed more than once: \(disposals)")
            }
            return [
                ForeignResourceCount(name: "backendLeases", count: disposals == 0 ? 1 : 0),
                ForeignResourceCount(name: "ingressRoots", count: ingress == nil ? 0 : 1),
            ]
        }
    }

    func assertFinished() throws {
        try require(lock.withLock { calls == expectedCalls }, "typed fixture command sequence incomplete")
        try require(lock.withLock { disposals == 1 }, "typed fixture was not disposed exactly once")
    }
}

private final class SupervisorTypedSession: ForeignPlatformOwnerSession, @unchecked Sendable {
    static let resourceNames = ["backendLeases", "eventReaders", "ingressRoots"]

    private let sessionContext: ForeignOwnerContext
    private let fixture: TypedOperationBackend
    private let lock = NSLock()
    private var reader: Task<Void, Error>?
    private var readerDrained = false
    private var events: [BleEvent] = []
    private var scanEvents: [ForeignScanEventEnvelope] = []
    private var retainedOwner: Bleng?
    private var eventProjection: SessionEventProjection?
    private let scanStopped = DispatchSemaphore(value: 0)
    private let scanEventObserved = DispatchSemaphore(value: 0)
    private var scanReceiptIndex = 0
    private let operationTerminalObserved = DispatchSemaphore(value: 0)

    init(context: ForeignOwnerContext, backend: TypedOperationBackend) {
        sessionContext = context
        fixture = backend
    }

    func context() -> ForeignOwnerContext { sessionContext }
    func backend() -> any BleBackend { fixture }
    func disposeUnassembled() throws { try fixture.dispose() }

    func startEventReader(owner: Bleng, eventProjection: SessionEventProjection) throws {
        try lock.withLock {
            guard reader == nil else {
                throw SupervisorFfiError.Platform(detail: "typed Supervisor reader started twice")
            }
            // Retain the Rust roots to verify retirement even while native references survive.
            retainedOwner = owner
            self.eventProjection = eventProjection
            reader = Task {
                for _ in 0..<64 {
                    guard let event = try await owner.nextEvent(timeoutMs: 10_000) else { return }
                    let envelope = eventProjection.project(event: event)
                    self.lock.withLock {
                        self.events.append(event)
                        if let envelope { self.scanEvents.append(envelope) }
                    }
                    if let envelope {
                        self.scanEventObserved.signal()
                        if case .stopped = envelope.event { self.scanStopped.signal() }
                    }
                    if case .commandCompleted = event { self.operationTerminalObserved.signal() }
                }
                throw ConsumerFailure.invalid("typed Supervisor event budget exceeded")
            }
        }
    }

    func waitEventReader() async throws {
        guard let task = lock.withLock({ reader }) else { return }
        do {
            try await task.value
            lock.withLock { readerDrained = true }
        } catch {
            throw SupervisorFfiError.Platform(detail: "typed Supervisor reader failed: \(error)")
        }
    }

    func resourceSnapshot() async throws -> [ForeignResourceCount] {
        let activeReader: UInt64 = lock.withLock { reader != nil && !readerDrained ? 1 : 0 }
        let backendResources = try fixture.resourceSnapshot()
        return (backendResources + [
            ForeignResourceCount(name: "eventReaders", count: activeReader),
        ]).sorted { $0.name < $1.name }
    }

    func executeRecoveryAction(
        sequence: UInt64, action: ForeignPlatformRecoveryAction
    ) async throws -> ForeignPlatformActionResult {
        throw SupervisorFfiError.Platform(detail: "unexpected recovery in typed operation flow: \(sequence) \(action)")
    }

    func waitForScanEvents() async throws {
        try await waitForSignal(scanStopped, "single reader did not project scan stop")
    }

    func waitForScanEvent(_ expected: ScanEvent) async throws {
        try await waitForSignal(scanEventObserved, "single reader did not project scan event: \(expected)")
        let envelope = try lock.withLock {
            try require(scanReceiptIndex < scanEvents.count, "scan reader receipt has no recorded event")
            defer { scanReceiptIndex += 1 }
            return scanEvents[scanReceiptIndex]
        }
        try require(envelope == ForeignScanEventEnvelope(sourceContext: sessionContext, event: expected),
                    "single reader scan receipt changed context/order/payload: \(envelope)")
    }

    func waitForOperationTerminals(_ expected: [CommandId]) async throws {
        // Typed waits acknowledge actor settlement, not consumption of the public FIFO.
        // Observe the real reader before shutdown can make nextEvent return nil.
        let complete = await withCheckedContinuation { continuation in
            DispatchQueue.global().async {
                let deadline = DispatchTime.now() + .seconds(2)
                let complete = expected.allSatisfy { _ in
                    self.operationTerminalObserved.wait(timeout: deadline) == .success
                }
                continuation.resume(returning: complete)
            }
        }
        let observed = lock.withLock {
            events.compactMap { event -> CommandId? in
                if case .commandCompleted(let id, _) = event { return id }
                return nil
            }
        }
        try require(complete && observed == expected,
                    "Supervisor reader terminal barrier failed: withinDeadline=\(complete) " +
                    "observed=\(observed.map(\.value)) expected=\(expected.map(\.value))")
    }

    func scanEndpoint() throws -> (owner: Bleng, projection: SessionEventProjection) {
        try lock.withLock {
            guard let retainedOwner, let eventProjection else {
                throw ConsumerFailure.invalid("Rust did not supply the owner-bound scan endpoint")
            }
            return (retainedOwner, eventProjection)
        }
    }

    func drainedScanEvents() throws -> [ForeignScanEventEnvelope] {
        try lock.withLock {
            try require(readerDrained, "scan evidence read before reader drain")
            return scanEvents
        }
    }

    func drainedEvents() throws -> [BleEvent] {
        try lock.withLock {
            try require(readerDrained, "typed Supervisor evidence read before reader drain")
            return events
        }
    }
}

private final class SupervisorTypedFactory: ForeignPlatformOwnerSessionFactory, @unchecked Sendable {
    private let fixture: TypedOperationBackend
    private let lock = NSLock()
    private var session: SupervisorTypedSession?

    init(backend: TypedOperationBackend) { fixture = backend }

    func requiredZeroResourceNames() -> [String] { SupervisorTypedSession.resourceNames }

    func create(context: ForeignOwnerContext) throws -> any ForeignPlatformOwnerSession {
        try lock.withLock {
            guard session == nil else {
                throw SupervisorFfiError.Platform(detail: "typed Supervisor unexpectedly created another backend session")
            }
            let created = SupervisorTypedSession(context: context, backend: fixture)
            session = created
            return created
        }
    }

    func createdSession() -> SupervisorTypedSession? { lock.withLock { session } }
}

private struct RetiredScanEndpoint {
    let owner: Bleng
    let projection: SessionEventProjection
    let envelope: ForeignScanEventEnvelope

    func assertRevoked() throws {
        guard case .started(let commandId, let scanId) = envelope.event else {
            throw ConsumerFailure.invalid("retired endpoint sample is not scanStarted")
        }
        try require(projection.project(event: .scanStarted(commandId: commandId, scanId: scanId)) == nil,
                    "retired scan endpoint projected through an outstanding native root")
        do {
            _ = try owner.beginStartScan(options: ScanOptions(serviceUuids: [], namePrefix: "typed-fixture", exactName: nil, companyId: nil, minRssi: nil, manufacturerData: nil, android: nil, allowDuplicates: false))
            throw ConsumerFailure.invalid("outstanding owner root reactivated after shutdown")
        } catch BlengError.Closed {}
    }
}

private func runSupervisorTypedOperationRegression(
    context: ForeignOwnerContext, previous: RetiredScanEndpoint? = nil
) async throws -> RetiredScanEndpoint {
    let backend = TypedOperationBackend(.fullFlow)
    let factory = SupervisorTypedFactory(backend: backend)
    let supervisor = try Supervisor(factory: factory, config: ForeignSupervisorConfig(
        recovery: ForeignRecoveryPolicyConfig(
            initialContext: context, oldLink: nil, expectedOldOperationIds: [],
            allowCentralRecreation: false, allowFreshOwner: false
        ),
        recoveryTarget: nil
    ))
    var shutdownAttempted = false
    let retained: RetiredScanEndpoint
    do {
        let initialized: ForeignOwnerStart = try await supervisor.start()
        try require(initialized.context == context && initialized.adapterSnapshot == backend.snapshot,
                    "Supervisor Rust startup changed context/adapter snapshot")
        guard let platform = factory.createdSession() else {
            throw ConsumerFailure.invalid("Supervisor did not create the foreign session")
        }
        let liveResources = try await platform.resourceSnapshot()
        try require(liveResources == SupervisorTypedSession.resourceNames.map {
            ForeignResourceCount(name: $0, count: 1)
        }, "typed Supervisor live resources were not measured")
        var operationIds = [CommandId(value: initialized.initializationCommandId)]
        let options = ScanOptions(serviceUuids: [], namePrefix: "typed-fixture", exactName: nil, companyId: nil, minRssi: nil, manufacturerData: nil, android: nil, allowDuplicates: false)
        var staleContexts = [ForeignOwnerContext]()
        var stale = context
        stale.ownerId += "-stale"
        staleContexts.append(stale)
        stale = context
        stale.backendId += "-stale"
        staleContexts.append(stale)
        stale = context
        stale.ownerGeneration += 1
        staleContexts.append(stale)
        stale = context
        stale.recoveryAttempt += 1
        staleContexts.append(stale)
        for staleContext in staleContexts {
            do {
                _ = try supervisor.beginStartScan(context: staleContext, options: options)
                throw ConsumerFailure.invalid("Supervisor admitted stale context: \(staleContext)")
            } catch SupervisorOperationError.StaleContext {}
        }

        let start: StartScanOperation = try supervisor.beginStartScan(context: context, options: options)
        operationIds.append(start.commandId())
        let scan: ScanSessionId = try await start.wait(timeoutMs: 2_000)
        let stop: StopScanOperation = try supervisor.beginStopScan(context: context, session: scan)
        operationIds.append(stop.commandId())
        let _: Void = try await stop.wait(timeoutMs: 2_000)
        try await platform.waitForScanEvents()
        let endpoint = try platform.scanEndpoint()
        let started = BleEvent.scanStarted(commandId: start.commandId(), scanId: scan)
        let stopped = ScanStopResult.ok(reason: .requested(commandId: stop.commandId()))
        let service = BluetoothUuid(value: "0000180d-0000-1000-8000-00805f9b34fb")
        // Exercise the canonical advertisement ABI without changing the raw fixture's 12/5 pairs.
        let advertisement = AdvertisementObservation(
            scanSessionId: scan, peripheralId: backend.peripheral, localName: "typed-fixture 🦷",
            advertisedServices: [service],
            manufacturerData: [ManufacturerData(companyId: 0x0059, payload: backend.readValue)],
            serviceData: [ServiceData(serviceUuid: service, payload: backend.notificationValue)],
            rssi: -61, connectable: true
        )
        let expectedScanEvents: [ScanEvent] = [
            .started(commandId: start.commandId(), scanId: scan),
            .advertisement(advertisement: advertisement),
            .stopped(scanId: scan, result: stopped),
        ]
        let rawScanEvents: [BleEvent] = [
            started, .advertisementObserved(advertisement: advertisement), .scanStopped(scanId: scan, result: stopped),
        ]
        let projectedScanEvents = try zip(rawScanEvents, expectedScanEvents).map { raw, expected in
            guard let envelope = endpoint.projection.project(event: raw) else {
                throw ConsumerFailure.invalid("live scan endpoint did not project \(expected)")
            }
            try require(envelope == ForeignScanEventEnvelope(sourceContext: context, event: expected),
                        "scan endpoint changed full source context or canonical payload: \(expected)")
            return envelope
        }
        try require(endpoint.projection.project(event: .commandCompleted(commandId: start.commandId(), error: nil)) == nil,
                    "scan endpoint projected an operation terminal")
        retained = RetiredScanEndpoint(
            owner: endpoint.owner, projection: endpoint.projection,
            envelope: projectedScanEvents[0]
        )
        if let previous {
            try previous.assertRevoked()
            try require(previous.envelope.event == retained.envelope.event,
                        "fresh owner did not exercise reused command/scan IDs")
            try require(previous.envelope.sourceContext != context,
                        "fresh owner reused the previous full context")
            try require(previous.projection.project(event: started) == nil,
                        "fresh owner reactivated an old scan endpoint")
        }
        let connect: ConnectOperation = try supervisor.beginConnect(context: context, peripheral: backend.peripheral)
        operationIds.append(connect.commandId())
        let connection: ConnectionId = try await connect.wait(timeoutMs: 2_000)
        try require(connection == backend.expectedConnection(), "Supervisor typed connection identity changed")
        let discover: DiscoverGattOperation = try supervisor.beginDiscoverGatt(context: context, connection: connection)
        operationIds.append(discover.commandId())
        let database: GattDatabase = try await discover.wait(timeoutMs: 2_000)
        try require(database == backend.database(), "Supervisor typed GATT database changed")
        let session = database.gattSessionId
        let characteristic = database.services[0].characteristics[0].id
        let read: ReadOperation = try supervisor.beginReadCharacteristic(
            context: context, connection: connection, session: session, characteristic: characteristic
        )
        operationIds.append(read.commandId())
        let bytes: Data = try await read.wait(timeoutMs: 2_000)
        try require(bytes == backend.readValue, "Supervisor typed read bytes changed")
        do {
            _ = try await read.wait(timeoutMs: 2_000)
            throw ConsumerFailure.invalid("Supervisor typed read result was delivered twice")
        } catch BlengError.OperationResultConsumed {}
        do {
            try await read.cancel()
            throw ConsumerFailure.invalid("Supervisor completed read cancellation was accepted")
        } catch BlengError.OperationAlreadyCompleted(let id) {
            try require(id == read.commandId(), "Supervisor completed-read cancel error lost command ID")
        }
        let write: WriteOperation = try supervisor.beginWriteCharacteristic(
            context: context, connection: connection, session: session, characteristic: characteristic,
            value: backend.writeValue, writeType: .withResponse
        )
        operationIds.append(write.commandId())
        let _: Void = try await write.wait(timeoutMs: 2_000)
        let enable: EnableNotificationsOperation = try supervisor.beginEnableNotifications(
            context: context, connection: connection, session: session, characteristic: characteristic,
            mode: .platformDefault
        )
        operationIds.append(enable.commandId())
        let receipt: NotificationSubscription = try await enable.wait(timeoutMs: 2_000)
        // The actual mode is part of the enable receipt, not a later event lookup.
        try require(receipt.actualMode == .indication, "Supervisor actual notification mode changed")
        let subscription: SubscriptionId = receipt.subscription
        let disable: DisableNotificationsOperation = try supervisor.beginDisableNotifications(
            context: context, connection: connection, session: session, subscription: subscription
        )
        operationIds.append(disable.commandId())
        let _: Void = try await disable.wait(timeoutMs: 2_000)
        let rssi: ReadRssiOperation = try supervisor.beginReadRssi(context: context, connection: connection)
        operationIds.append(rssi.commandId())
        let signal: Int16 = try await rssi.wait(timeoutMs: 2_000)
        try require(signal == -61, "Supervisor typed signed RSSI changed")
        let disconnect: DisconnectOperation = try supervisor.beginDisconnect(context: context, connection: connection)
        operationIds.append(disconnect.commandId())
        let _: Void = try await disconnect.wait(timeoutMs: 2_000)
        try await platform.waitForOperationTerminals(operationIds)

        shutdownAttempted = true
        let retired = try await supervisor.shutdown()
        try require(retired == SupervisorTypedSession.resourceNames.map {
            ForeignResourceCount(name: $0, count: 0)
        }, "Supervisor did not retire measured backend/ingress/reader resources")
        try backend.assertFinished()
        try backend.assertCommandIds(operationIds)
        try require(operationIds.map(\.value) == Array(UInt64(1)...UInt64(11)),
                    "Supervisor stale rejection consumed IDs or typed command sequence changed")
        try require(operationIds == [
            CommandId(value: initialized.initializationCommandId), start.commandId(), stop.commandId(),
            connect.commandId(), discover.commandId(), read.commandId(), write.commandId(),
            enable.commandId(), disable.commandId(), rssi.commandId(), disconnect.commandId(),
        ], "Supervisor typed handle identities changed after shutdown")

        try retained.assertRevoked()
        let scanEnvelopes = try platform.drainedScanEvents()
        try require(scanEnvelopes == [
            ForeignScanEventEnvelope(sourceContext: context, event: expectedScanEvents[0]),
            ForeignScanEventEnvelope(sourceContext: context, event: expectedScanEvents[2]),
        ], "single reader lost scan events or relabeled their full source context")
        if let previous {
            try previous.assertRevoked()
            try require(previous.envelope.sourceContext != retained.envelope.sourceContext,
                        "retirement relabeled an already delivered envelope")
        }
        let events = try platform.drainedEvents()
        var terminals: [CommandId] = []
        var sawInitialization = false
        var sawRead = false
        var sawActualMode = false
        var sawValue = false
        for event in events {
            switch event {
            case .adapterSnapshotChanged(let id, let snapshot):
                try require(id == operationIds[0] && snapshot == initialized.adapterSnapshot,
                            "Supervisor startup receipt did not match Rust initialization event")
                sawInitialization = true
            case .commandCompleted(let id, let error):
                try require(error == nil, "Supervisor typed command terminal failed: \(String(describing: error))")
                terminals.append(id)
            case .characteristicRead(let id, let eventConnection, let eventSession, let eventCharacteristic, let value):
                try require(id == read.commandId() && eventConnection == connection && eventSession == session,
                            "Supervisor read event correlation changed")
                try require(eventCharacteristic == characteristic && value == backend.readValue,
                            "Supervisor read event characteristic/bytes changed")
                sawRead = true
            case .notificationStateChanged(let id, let eventConnection, let eventSession, let eventSubscription,
                                           let eventCharacteristic, let state) where id == enable.commandId():
                try require(eventConnection == connection && eventSession == session && eventSubscription == subscription,
                            "Supervisor enable event identities changed")
                try require(eventCharacteristic == characteristic && state == .enabled(actualMode: .indication),
                            "Supervisor actual notification mode changed")
                sawActualMode = true
            case .notificationReceived(let eventConnection, let eventSession, let eventSubscription,
                                       let eventCharacteristic, let value):
                try require(sawActualMode, "Supervisor notification preceded enable evidence")
                try require(eventConnection == connection && eventSession == session && eventSubscription == subscription,
                            "Supervisor notification identities changed")
                try require(eventCharacteristic == characteristic && value == backend.notificationValue,
                            "Supervisor notification bytes changed")
                sawValue = true
            default:
                break
            }
        }
        try require(sawInitialization && sawRead && sawActualMode && sawValue && terminals == operationIds,
                    "Supervisor reader did not drain complete initialization/typed operation evidence: " +
                    "initialization=\(sawInitialization) read=\(sawRead) actualMode=\(sawActualMode) value=\(sawValue) " +
                    "terminals=\(terminals.map(\.value)) expected=\(operationIds.map(\.value)) events=\(events.count)")
        do {
            _ = try supervisor.beginStartScan(context: context, options: options)
            throw ConsumerFailure.invalid("Supervisor accepted submission after shutdown")
        } catch SupervisorOperationError.NoActiveSession {}
        do {
            _ = try await read.wait(timeoutMs: 2_000)
            throw ConsumerFailure.invalid("Supervisor retained read result was delivered twice after shutdown")
        } catch BlengError.OperationResultConsumed {}
    } catch {
        let primary = error
        var cleanupFailures: [String] = []
        if !shutdownAttempted {
            do { _ = try await supervisor.shutdown() }
            catch { cleanupFailures.append("Supervisor shutdown: \(error)") }
        }
        // Even a failed Rust retirement must not hide reader/snapshot cleanup evidence.
        if let platform = factory.createdSession() {
            do { try await platform.waitEventReader() }
            catch { cleanupFailures.append("reader drain: \(error)") }
            do {
                let resources = try await platform.resourceSnapshot()
                try require(resources == SupervisorTypedSession.resourceNames.map {
                    ForeignResourceCount(name: $0, count: 0)
                }, "typed Supervisor cleanup resources remain: \(resources)")
            } catch { cleanupFailures.append("resource cleanup: \(error)") }
        }
        if cleanupFailures.isEmpty { throw primary }
        throw ConsumerFailure.invalid("Supervisor typed operations primary: \(primary); cleanup: \(cleanupFailures.joined(separator: "; "))")
    }
    print("supervisor_typed_operations=passed")
    return retained
}

private func requireEmptyScanDelivery(_ scan: SupervisedScan) throws {
    let snapshot: ForeignScanDeliverySnapshot = scan.observationSnapshot()
    try require(snapshot == ForeignScanDeliverySnapshot(
        queuedAdvertisements: 0, queuedBytes: 0, queuedStopBytes: 0, pullPending: false
    ), "scan proxy retained queued data or a pending pull: \(snapshot)")
}

private func runSupervisorScanProxyRegression() async throws {
    let context = ForeignOwnerContext(
        ownerId: "typed-scan-proxy-owner", backendId: "typed-scan-proxy-backend",
        ownerGeneration: 1, recoveryAttempt: 1
    )
    let backend = TypedOperationBackend(.scanProxy)
    let factory = SupervisorTypedFactory(backend: backend)
    let supervisor = try Supervisor(factory: factory, config: ForeignSupervisorConfig(
        recovery: ForeignRecoveryPolicyConfig(
            initialContext: context, oldLink: nil, expectedOldOperationIds: [],
            allowCentralRecreation: false, allowFreshOwner: false
        ),
        recoveryTarget: nil
    ))
    var shutdownAttempted = false
    do {
        let initialized = try await supervisor.start()
        try require(initialized.context == context && initialized.adapterSnapshot == backend.snapshot,
                    "scan proxy Supervisor startup changed context/adapter snapshot")
        guard let platform = factory.createdSession() else {
            throw ConsumerFailure.invalid("scan proxy Supervisor did not create the foreign session")
        }
        let liveResources = try await platform.resourceSnapshot()
        try require(liveResources == SupervisorTypedSession.resourceNames.map {
            ForeignResourceCount(name: $0, count: 1)
        }, "scan proxy live backend/ingress/single-reader resources were not measured")
        let scan: SupervisedScan = try supervisor.beginScan(context: context, options: ScanOptions(serviceUuids: [], namePrefix: "typed-fixture", exactName: nil, companyId: nil, minRssi: nil, manufacturerData: nil, android: nil, allowDuplicates: false))
        for timeout: UInt64 in [0, UInt64.max] {
            do {
                try await scan.waitUntilStarted(timeoutMs: timeout)
                throw ConsumerFailure.invalid("scan proxy accepted invalid start timeout: \(timeout)")
            } catch ScanSessionError.Operation(let error) {
                try require(error == .InvalidTimeout, "invalid start timeout lost its typed operation cause")
            }
        }
        let _: Void = try await scan.waitUntilStarted(timeoutMs: 2_000)
        let startCommand = try backend.completedScanStartCommand()
        let advertisements = backend.scanAdvertisements(for: startCommand.scanSessionId)
        let started = ScanEvent.started(commandId: startCommand.commandId, scanId: startCommand.scanSessionId)
        // Operation settlement is not a public FIFO flush. These receipts follow the
        // sole reader's real nextEvent + project calls, including callbacks before Started.
        for advertisement in advertisements {
            try await platform.waitForScanEvent(.advertisement(advertisement: advertisement))
        }
        try await platform.waitForScanEvent(started)
        let queued: ForeignScanDeliverySnapshot = scan.observationSnapshot()
        try require(queued.queuedAdvertisements == 2 && queued.queuedBytes > 0 &&
                    queued.queuedStopBytes == 0 && !queued.pullPending,
                    "scan proxy did not queue both binary/optional callback-before-return advertisements")
        do {
            try await scan.waitUntilStarted(timeoutMs: 2_000)
            throw ConsumerFailure.invalid("scan proxy delivered start result twice")
        } catch ScanSessionError.Operation(let error) {
            try require(error == .OperationResultConsumed, "duplicate start wait lost its typed operation cause")
        }
        do {
            try await scan.cancelStart()
            throw ConsumerFailure.invalid("scan proxy accepted completed-start cancellation")
        } catch ScanSessionError.Operation(let error) {
            try require(error == .OperationAlreadyCompleted(commandId: startCommand.commandId),
                        "scan proxy completed-start cancellation lost its typed command identity")
        }
        for timeout: UInt64 in [0, UInt64.max] {
            do {
                _ = try await scan.nextEvent(timeoutMs: timeout)
                throw ConsumerFailure.invalid("scan proxy accepted invalid observation timeout: \(timeout)")
            } catch ScanSessionError.Observation(let error) {
                try require(error == .InvalidTimeout, "invalid pull timeout lost its typed observation cause")
            }
        }
        try require(scan.observationSnapshot() == queued,
                    "invalid/duplicate waits or completed cancellation damaged scan delivery")
        for advertisement in advertisements {
            let event: ScanEvent = try await scan.nextEvent(timeoutMs: 2_000)
            try require(event == .advertisement(advertisement: advertisement),
                        "scan proxy changed binary/optional advertisement payload or order: \(event)")
        }
        try requireEmptyScanDelivery(scan)
        do {
            _ = try await scan.nextEvent(timeoutMs: 1)
            throw ConsumerFailure.invalid("empty scan proxy pull did not time out")
        } catch ScanSessionError.Observation(let error) {
            // Exercise the actual Rust-owned observation timer, not an OS/platform guess.
            try require(error == .Timeout, "empty scan proxy pull did not return observation Timeout")
        }
        try requireEmptyScanDelivery(scan)
        var lateAdvertisement = advertisements[0]
        lateAdvertisement.localName = "after-observation-timeout"
        lateAdvertisement.manufacturerData = [ManufacturerData(companyId: 0x0059, payload: backend.writeValue)]
        lateAdvertisement.connectable = false
        try backend.injectScanAdvertisement(lateAdvertisement)
        try await platform.waitForScanEvent(.advertisement(advertisement: lateAdvertisement))
        let resumed = scan.observationSnapshot()
        try require(resumed.queuedAdvertisements == 1 && resumed.queuedBytes > 0 &&
                    resumed.queuedStopBytes == 0 && !resumed.pullPending,
                    "observation timeout did not release its pull for later advertisement delivery")
        let resumedEvent: ScanEvent = try await scan.nextEvent(timeoutMs: 2_000)
        try require(resumedEvent == .advertisement(advertisement: lateAdvertisement),
                    "scan proxy failed to pull new binary advertisement after observation timeout")
        try requireEmptyScanDelivery(scan)

        let stop: StopScanOperation = try scan.beginStop()
        let _: Void = try await stop.wait(timeoutMs: 2_000)
        do {
            try await stop.wait(timeoutMs: 2_000)
            throw ConsumerFailure.invalid("scan proxy original stop result was delivered twice")
        } catch BlengError.OperationResultConsumed {}
        do {
            try await stop.cancel()
            throw ConsumerFailure.invalid("scan proxy original completed stop cancellation was accepted")
        } catch BlengError.OperationAlreadyCompleted(let id) {
            try require(id == stop.commandId(), "scan proxy original stop cancel error lost command ID")
        }
        let stopped = ScanEvent.stopped(
            scanId: startCommand.scanSessionId, result: .ok(reason: .requested(commandId: stop.commandId()))
        )
        try await platform.waitForScanEvent(stopped)
        let operationIds = [
            CommandId(value: initialized.initializationCommandId), startCommand.commandId, stop.commandId(),
        ]
        try await platform.waitForOperationTerminals(operationIds)
        let stopEvent: ScanEvent = try await scan.nextEvent(timeoutMs: 2_000)
        try require(stopEvent == stopped, "scan proxy stop observation changed original stop result/identity")
        try requireEmptyScanDelivery(scan)

        shutdownAttempted = true
        let retired = try await supervisor.shutdown()
        try require(retired == SupervisorTypedSession.resourceNames.map {
            ForeignResourceCount(name: $0, count: 0)
        }, "scan proxy did not retire measured backend/ingress/single-reader resources")
        try backend.assertFinished()
        try backend.assertCommandIds(operationIds)
        try require(operationIds.map(\.value) == [1, 2, 3], "scan proxy controls consumed unexpected command IDs")
        let scanEvents = advertisements.map { ScanEvent.advertisement(advertisement: $0) } + [
            started, .advertisement(advertisement: lateAdvertisement), stopped,
        ]
        let envelopes = try platform.drainedScanEvents()
        try require(envelopes == scanEvents.map { ForeignScanEventEnvelope(sourceContext: context, event: $0) },
                    "scan proxy single reader lost or relabeled scan evidence")
        let events = try platform.drainedEvents()
        try require(events == [
            .adapterSnapshotChanged(commandId: operationIds[0], snapshot: initialized.adapterSnapshot),
            .commandCompleted(commandId: operationIds[0], error: nil),
            .advertisementObserved(advertisement: advertisements[0]),
            .advertisementObserved(advertisement: advertisements[1]),
            .scanStarted(commandId: startCommand.commandId, scanId: startCommand.scanSessionId),
            .commandCompleted(commandId: startCommand.commandId, error: nil),
            .advertisementObserved(advertisement: lateAdvertisement),
            .scanStopped(scanId: startCommand.scanSessionId, result: .ok(reason: .requested(commandId: stop.commandId()))),
            .commandCompleted(commandId: stop.commandId(), error: nil),
        ], "scan proxy reader did not drain the exact domain/terminal sequence")
        do {
            _ = try await scan.nextEvent(timeoutMs: 2_000)
            throw ConsumerFailure.invalid("retained scan proxy delivered after owner shutdown")
        } catch ScanSessionError.Delivery(let error) {
            try require(error == .OwnerRetired, "retired scan proxy lost its typed delivery cause")
        }
        scan.detach()
        try requireEmptyScanDelivery(scan)
        do {
            _ = try await scan.nextEvent(timeoutMs: 2_000)
            throw ConsumerFailure.invalid("detach reactivated retired scan delivery")
        } catch ScanSessionError.Delivery(let error) {
            try require(error == .OwnerRetired, "detach concealed the earlier OwnerRetired failure")
        }
    } catch {
        let primary = error
        var cleanupFailures: [String] = []
        if !shutdownAttempted {
            do { _ = try await supervisor.shutdown() }
            catch { cleanupFailures.append("Supervisor shutdown: \(error)") }
        }
        if let platform = factory.createdSession() {
            do { try await platform.waitEventReader() }
            catch { cleanupFailures.append("reader drain: \(error)") }
            do {
                let resources = try await platform.resourceSnapshot()
                try require(resources == SupervisorTypedSession.resourceNames.map {
                    ForeignResourceCount(name: $0, count: 0)
                }, "scan proxy cleanup resources remain: \(resources)")
            } catch { cleanupFailures.append("resource cleanup: \(error)") }
        }
        if cleanupFailures.isEmpty { throw primary }
        throw ConsumerFailure.invalid("Supervisor scan proxy primary: \(primary); cleanup: \(cleanupFailures.joined(separator: "; "))")
    }
    print("supervisor_scan_proxy=passed")
}

private func nextTypedEvent(_ owner: Bleng) async throws -> BleEvent {
    guard let event = try await owner.nextEvent(timeoutMs: 2_000) else {
        throw ConsumerFailure.invalid("typed flow event stream closed early")
    }
    return event
}

func runTypedOperationRegression() async throws {
    try await runSupervisorScanProxyRegression()
    let firstContext = ForeignOwnerContext(
        ownerId: "typed-supervisor-owner", backendId: "typed-supervisor-backend",
        ownerGeneration: 1, recoveryAttempt: 1
    )
    let retired = try await runSupervisorTypedOperationRegression(context: firstContext)
    _ = try await runSupervisorTypedOperationRegression(context: ForeignOwnerContext(
        ownerId: "typed-supervisor-owner-fresh", backendId: "typed-supervisor-backend-fresh",
        ownerGeneration: 2, recoveryAttempt: 2
    ), previous: retired)

    let backend = TypedOperationBackend(.fullFlow)
    let owner = try Bleng()
    do {
        try await owner.assembleBackend(backend: backend)
        var operationIds: [CommandId] = []
        let initialize: InitializeOperation = try owner.beginInitialize()
        operationIds.append(initialize.commandId())
        let snapshot: AdapterSnapshot = try await initialize.wait(timeoutMs: 2_000)
        try require(snapshot == backend.snapshot, "typed initialize snapshot changed")
        do {
            _ = try await initialize.wait(timeoutMs: 2_000)
            throw ConsumerFailure.invalid("initialize result was delivered twice")
        } catch BlengError.OperationResultConsumed {}

        let start: StartScanOperation = try owner.beginStartScan(
            options: ScanOptions(serviceUuids: [], namePrefix: "typed-fixture", exactName: nil, companyId: nil, minRssi: nil, manufacturerData: nil, android: nil, allowDuplicates: false)
        )
        operationIds.append(start.commandId())
        let scan: ScanSessionId = try await start.wait(timeoutMs: 2_000)
        let stop: StopScanOperation = try owner.beginStopScan(session: scan)
        operationIds.append(stop.commandId())
        let _: Void = try await stop.wait(timeoutMs: 2_000)
        do {
            _ = try await start.wait(timeoutMs: 2_000)
            throw ConsumerFailure.invalid("scan result was delivered twice")
        } catch BlengError.OperationResultConsumed {}
        do {
            try await stop.wait(timeoutMs: 2_000)
            throw ConsumerFailure.invalid("stop result was delivered twice")
        } catch BlengError.OperationResultConsumed {}

        let connect: ConnectOperation = try owner.beginConnect(
            peripheral: backend.peripheral
        )
        operationIds.append(connect.commandId())
        let connection: ConnectionId = try await connect.wait(timeoutMs: 2_000)
        try require(connection == backend.expectedConnection(), "typed connect identity changed")
        do {
            _ = try await connect.wait(timeoutMs: 2_000)
            throw ConsumerFailure.invalid("connect result was delivered twice")
        } catch BlengError.OperationResultConsumed {}
        let discover: DiscoverGattOperation = try owner.beginDiscoverGatt(
            connection: connection
        )
        operationIds.append(discover.commandId())
        let database: GattDatabase = try await discover.wait(timeoutMs: 2_000)
        try require(database == backend.database(), "typed GATT database changed")
        do {
            _ = try await discover.wait(timeoutMs: 2_000)
            throw ConsumerFailure.invalid("discovery result was delivered twice")
        } catch BlengError.OperationResultConsumed {}
        let characteristic = database.services[0].characteristics[0].id
        let session = database.gattSessionId
        let read: ReadOperation = try owner.beginReadCharacteristic(
            connection: connection, session: session, characteristic: characteristic
        )
        operationIds.append(read.commandId())
        // Observe both pieces of read evidence without claiming the typed result.
        // The six commands so far each publish exactly domain + terminal events.
        var readEvidence: ReadCharacteristicCommand?
        var readCompleted = false
        for _ in 0..<12 {
            let event = try await nextTypedEvent(owner)
            if case .characteristicRead(let id, let eventConnection, let eventSession, let idOfCharacteristic, let bytes) = event {
                let command = try backend.completedReadCommand()
                try require(id == read.commandId() && id == command.commandId && eventConnection == connection && eventSession == session,
                            "read event correlation changed")
                try require(idOfCharacteristic == characteristic && bytes == backend.readValue,
                            "read event bytes/characteristic changed")
                readEvidence = command
            }
            if case .commandCompleted(let id, let error) = event, let command = readEvidence,
               id == command.commandId {
                try require(error == nil, "read terminal failed")
                readCompleted = true
                break
            }
        }
        guard readCompleted, let completedRead = readEvidence else {
            throw ConsumerFailure.invalid("completed read evidence was missing")
        }
        do {
            try await read.cancel()
            throw ConsumerFailure.invalid("completed read cancellation was accepted")
        } catch BlengError.OperationAlreadyCompleted(let id) {
            try require(id == read.commandId() && id == completedRead.commandId, "completed read cancellation identity changed")
        }

        let write: WriteOperation = try owner.beginWriteCharacteristic(
            connection: connection, session: session, characteristic: characteristic,
            value: backend.writeValue, writeType: .withResponse
        )
        operationIds.append(write.commandId())
        let _: Void = try await write.wait(timeoutMs: 2_000)
        do {
            try await write.wait(timeoutMs: 2_000)
            throw ConsumerFailure.invalid("write result was delivered twice")
        } catch BlengError.OperationResultConsumed {}
        let enable: EnableNotificationsOperation = try owner.beginEnableNotifications(
            connection: connection, session: session, characteristic: characteristic,
            mode: .platformDefault
        )
        operationIds.append(enable.commandId())
        let receipt: NotificationSubscription = try await enable.wait(timeoutMs: 2_000)
        let subscription: SubscriptionId = receipt.subscription
        do {
            _ = try await enable.wait(timeoutMs: 2_000)
            throw ConsumerFailure.invalid("enable result was delivered twice")
        } catch BlengError.OperationResultConsumed {}
        // Write's pair is still queued, then enable state + terminal + value.
        var sawActualMode = false
        var sawValue = false
        for _ in 0..<5 {
            switch try await nextTypedEvent(owner) {
            case .notificationStateChanged(_, let eventConnection, let eventSession, let eventSubscription,
                                           let eventCharacteristic, let state):
                try require(eventConnection == connection && eventSession == session && eventSubscription == subscription,
                            "enabled notification identity changed")
                try require(eventCharacteristic == characteristic && state == .enabled(actualMode: .indication),
                            "actual notification mode was not preserved in BleEvent")
                sawActualMode = true
            case .notificationReceived(let eventConnection, let eventSession, let eventSubscription,
                                       let eventCharacteristic, let bytes):
                try require(sawActualMode, "notification value preceded enable evidence")
                try require(eventConnection == connection && eventSession == session && eventSubscription == subscription,
                            "notification value identity changed")
                try require(eventCharacteristic == characteristic && bytes == backend.notificationValue,
                            "notification payload changed")
                sawValue = true
            default:
                break
            }
        }
        try require(sawActualMode && sawValue, "typed notification enable/value evidence missing")
        let disable: DisableNotificationsOperation = try owner.beginDisableNotifications(
            connection: connection, session: session, subscription: subscription
        )
        operationIds.append(disable.commandId())
        let _: Void = try await disable.wait(timeoutMs: 2_000)
        do {
            try await disable.wait(timeoutMs: 2_000)
            throw ConsumerFailure.invalid("disable result was delivered twice")
        } catch BlengError.OperationResultConsumed {}
        let rssi: ReadRssiOperation = try owner.beginReadRssi(connection: connection)
        operationIds.append(rssi.commandId())
        let signal: Int16 = try await rssi.wait(timeoutMs: 2_000)
        try require(signal == -61, "typed signed RSSI changed")
        do {
            _ = try await rssi.wait(timeoutMs: 2_000)
            throw ConsumerFailure.invalid("RSSI result was delivered twice")
        } catch BlengError.OperationResultConsumed {}
        let disconnect: DisconnectOperation = try owner.beginDisconnect(connection: connection)
        operationIds.append(disconnect.commandId())
        let _: Void = try await disconnect.wait(timeoutMs: 2_000)
        do {
            try await disconnect.wait(timeoutMs: 2_000)
            throw ConsumerFailure.invalid("disconnect result was delivered twice")
        } catch BlengError.OperationResultConsumed {}
        try await owner.shutdown()
        try await owner.shutdown()
        try backend.assertFinished()
        try backend.assertCommandIds(operationIds)
        try require(operationIds == [
            initialize.commandId(), start.commandId(), stop.commandId(), connect.commandId(),
            discover.commandId(), read.commandId(), write.commandId(), enable.commandId(),
            disable.commandId(), rssi.commandId(), disconnect.commandId(),
        ], "operation command IDs changed after wait/shutdown")
        do {
            _ = try owner.beginInitialize()
            throw ConsumerFailure.invalid("closed owner accepted synchronous submission")
        } catch BlengError.Closed {}
        let bytes: Data = try await read.wait(timeoutMs: 2_000)
        try require(bytes == backend.readValue, "completed unclaimed read did not survive disconnect/shutdown")
        do {
            _ = try await read.wait(timeoutMs: 2_000)
            throw ConsumerFailure.invalid("retained read result was delivered twice")
        } catch BlengError.OperationResultConsumed {}
    } catch {
        try? await owner.shutdown()
        throw error
    }

    for acceptsCancellation in [true, false] {
        let backend = TypedOperationBackend(acceptsCancellation ? .acceptCancellation : .rejectCancellation)
        let owner = try Bleng()
        var waiter: Task<ConnectionId, Error>?
        do {
            try await owner.assembleBackend(backend: backend)
            let initialize: InitializeOperation = try owner.beginInitialize()
            let _: AdapterSnapshot = try await initialize.wait(timeoutMs: 2_000)
            let connect: ConnectOperation = try owner.beginConnect(
                peripheral: backend.peripheral
            )
            let connectId = connect.commandId()
            try await backend.waitForConnectEntry()
            do {
                _ = try await connect.wait(timeoutMs: 0)
                throw ConsumerFailure.invalid("invalid wait timeout was accepted")
            } catch BlengError.InvalidTimeout {}
            let waiterStarted = DispatchSemaphore(value: 0)
            let waiting = Task<ConnectionId, Error> {
                waiterStarted.signal()
                return try await connect.wait(timeoutMs: 2_000)
            }
            waiter = waiting
            try await waitForSignal(waiterStarted, "typed connect waiter did not start")
            if acceptsCancellation {
                let _: Void = try await connect.cancel()
                try backend.assertCancellation(connectId)
                do {
                    _ = try await waiting.value
                    throw ConsumerFailure.invalid("accepted connect cancellation did not cancel its waiter")
                } catch BlengError.Cancelled {}
            } else {
                do {
                    try await connect.cancel()
                    throw ConsumerFailure.invalid("unsupported native cancellation was reported as accepted")
                } catch BlengError.OperationNotCancellable(let id) {
                    try require(id == connectId, "cancel error did not match operation identity")
                    try backend.assertCancellation(id)
                }
                // Rejection must leave the sole typed result available to the concurrent waiter.
                try backend.finishConnect()
                let connection: ConnectionId = try await waiting.value
                try require(connection == backend.expectedConnection(), "rejected cancellation consumed connect result")
            }
            do {
                _ = try await connect.wait(timeoutMs: 2_000)
                throw ConsumerFailure.invalid("concurrent connect result was delivered twice")
            } catch BlengError.OperationResultConsumed {}
            try await owner.shutdown()
            try await owner.shutdown()
            try require(connect.commandId() == connectId, "cancelled operation identity changed after shutdown")
            try backend.assertFinished()
        } catch {
            try? await owner.shutdown()
            if let waiter { _ = try? await waiter.value }
            throw error
        }
    }
}
