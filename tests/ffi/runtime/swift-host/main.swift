import Dispatch
import Foundation

enum NativeFailure: LocalizedError {
    case undeclared(String)

    var errorDescription: String? {
        switch self {
        case .undeclared(let detail): detail
        }
    }
}

enum BackendMode {
    case synchronous
    case swiftThread
    case noEvent
    case rejectCommand
    case unexpectedCommand
    case typedCapabilitiesFailure
    case unexpectedCapabilitiesFailure
    case invalidContractVersion
    case typedBindFailure
    case unexpectedBindFailure
    case retainBeforeBindFailure
    case pushDuringBind
    case pushDuringDispose
    case typedDisposeFailure
    case unexpectedDisposeFailure
}

enum SinkBehavior {
    case accept
    case reject
    case block
}

final class LockedBox<Value>: @unchecked Sendable {
    private let lock = NSLock()
    private var value: Value

    init(_ value: Value) {
        self.value = value
    }

    func get() -> Value {
        lock.withLock { value }
    }

    func set(_ value: Value) {
        lock.withLock { self.value = value }
    }
}

final class RecordingSink: BleEventSink, @unchecked Sendable {
    private let lock = NSLock()
    private let blockedCallbackEntered = DispatchSemaphore(value: 0)
    private let blockedCallbackRelease = DispatchSemaphore(value: 0)
    private var behavior: SinkBehavior
    private var events: [BleEvent] = []

    init(behavior: SinkBehavior = .accept) {
        self.behavior = behavior
    }

    func onEvent(event: BleEvent) throws {
        switch lock.withLock({ behavior }) {
        case .accept:
            lock.withLock { events.append(event) }
        case .reject:
            throw EventSinkError.Rejected(detail: "declared sink rejection")
        case .block:
            blockedCallbackEntered.signal()
            blockedCallbackRelease.wait()
            lock.withLock { events.append(event) }
        }
    }

    func setBehavior(_ behavior: SinkBehavior) {
        lock.withLock { self.behavior = behavior }
    }

    func snapshot() -> [BleEvent] {
        lock.withLock { events }
    }

    func waitForBlockedCallback(timeoutMillis: Int) -> Bool {
        blockedCallbackEntered.wait(
            timeout: .now() + .milliseconds(timeoutMillis)
        ) == .success
    }

    func releaseBlockedCallback() {
        blockedCallbackRelease.signal()
    }
}

final class TestBackend: BleBackend, @unchecked Sendable {
    private let lock = NSLock()
    private let mode: BackendMode
    private let callbackDone: DispatchSemaphore?
    private let sharedDisposeCount: LockedBox<Int>?
    private var ingress: BackendEventIngressHandle?
    private(set) var capabilitiesCount = 0
    private(set) var bindCount = 0
    private(set) var disposeCount = 0
    private(set) var bindPushStatus: EventIngressStatus?
    private(set) var disposePushStatus: EventIngressStatus?
    private(set) var pushStatuses: [EventIngressStatus] = []

    init(
        mode: BackendMode,
        callbackDone: DispatchSemaphore? = nil,
        sharedDisposeCount: LockedBox<Int>? = nil
    ) {
        self.mode = mode
        self.callbackDone = callbackDone
        self.sharedDisposeCount = sharedDisposeCount
    }

    func capabilities() throws -> BackendCapabilities {
        lock.withLock { capabilitiesCount += 1 }
        switch mode {
        case .typedCapabilitiesFailure:
            throw BleHubError.BackendAssembly(
                detail: "declared capabilities failure"
            )
        case .unexpectedCapabilitiesFailure:
            throw NativeFailure.undeclared("undeclared capabilities failure")
        case .invalidContractVersion:
            return capabilities(contractVersion: blehubContractVersion() + 1)
        default:
            return capabilities(contractVersion: blehubContractVersion())
        }
    }

    func bindEventIngress(ingress: BackendEventIngressHandle) throws {
        lock.withLock { bindCount += 1 }
        switch mode {
        case .typedBindFailure:
            throw BleHubError.BackendAssembly(detail: "declared bind failure")
        case .unexpectedBindFailure:
            throw NativeFailure.undeclared("undeclared bind failure")
        case .retainBeforeBindFailure:
            retain(ingress)
            throw BleHubError.BackendAssembly(
                detail: "retained before bind failure"
            )
        case .pushDuringBind:
            retain(ingress)
            let status = ingress.push(event: scanStarted(scanCommand(90)))
            lock.withLock { bindPushStatus = status }
        default:
            retain(ingress)
        }
    }

    func initialize(command: InitializeCommand) throws {}

    func startScan(command: StartScanCommand) throws {
        switch mode {
        case .synchronous:
            emitSuccessfulScan(command)
        case .swiftThread:
            DispatchQueue.global().async { [self] in
                emitSuccessfulScan(command)
                callbackDone?.signal()
            }
        case .rejectCommand:
            throw CommandDispatchError.BackendUnavailable(
                detail: "declared dispatch rejection",
                platformDetails: nil
            )
        case .unexpectedCommand:
            throw NativeFailure.undeclared("undeclared command failure")
        default:
            break
        }
    }

    func stopScan(command: StopScanCommand) throws {}

    func connect(command: ConnectCommand) throws {}

    func disconnect(command: DisconnectCommand) throws {}

    func discoverGatt(command: DiscoverGattCommand) throws {}

    func readCharacteristic(command: ReadCharacteristicCommand) throws {}

    func writeCharacteristic(command: WriteCharacteristicCommand) throws {}

    func setNotifications(command: SetNotificationsCommand) throws {}

    func readRssi(command: ReadRssiCommand) throws {}

    func setAdapterEnabled(command: SetAdapterEnabledCommand) throws {}

    func queryDevices(command: QueryDevicesCommand) throws {}

    func associateCompanionDevice(command: AssociateCompanionDeviceCommand) throws {}

    func readDescriptor(command: ReadDescriptorCommand) throws {}

    func writeDescriptor(command: WriteDescriptorCommand) throws {}

    func mtu(command: MtuCommand) throws {}

    func setConnectionPriority(command: SetConnectionPriorityCommand) throws {}

    func createBond(command: CreateBondCommand) throws {}

    func removeBond(command: RemoveBondCommand) throws {}

    func refreshGattCache(command: RefreshGattCacheCommand) throws {}

    func cancelOperation(request: CancelOperationRequest) throws {}

    func dispose() throws {
        let root = lock.withLock { () -> BackendEventIngressHandle? in
            disposeCount += 1
            return ingress.swap(with: nil)
        }
        if let sharedDisposeCount {
            sharedDisposeCount.set(sharedDisposeCount.get() + 1)
        }
        switch mode {
        case .pushDuringDispose:
            if let root {
                let status = root.push(event: scanStarted(scanCommand(91)))
                lock.withLock { disposePushStatus = status }
            }
        case .typedDisposeFailure:
            throw BleHubError.BackendCleanup(
                detail: "declared cleanup failure",
                platformDetails: nil
            )
        case .unexpectedDisposeFailure:
            throw NativeFailure.undeclared("undeclared cleanup failure")
        default:
            break
        }
    }

    func push(_ event: BleEvent) -> EventIngressStatus {
        let root = lock.withLock { ingress }
        precondition(root != nil, "event ingress was not bound")
        return root!.push(event: event)
    }

    func hasIngress() -> Bool {
        lock.withLock { ingress != nil }
    }

    private func capabilities(contractVersion: UInt32) -> BackendCapabilities {
        BackendCapabilities(
            platform: .test,
            contractVersion: contractVersion,
            supportsDuplicateScanResults: false,
            supportsConnectableFlag: false,
            supportsReadRssi: false,
            supportsRequiredNotification: false,
            supportsRequiredIndication: false,
            reportsActualNotificationMode: false
        )
    }

    private func retain(_ ingress: BackendEventIngressHandle) {
        lock.withLock {
            precondition(self.ingress == nil)
            self.ingress = ingress
        }
    }

    private func emitSuccessfulScan(_ command: StartScanCommand) {
        let statuses = [
            push(scanStarted(command)),
            push(.commandCompleted(commandId: command.commandId, error: nil)),
        ]
        lock.withLock { pushStatuses.append(contentsOf: statuses) }
    }
}

extension Optional {
    mutating func swap(with replacement: Wrapped?) -> Wrapped? {
        let previous = self
        self = replacement
        return previous
    }
}

func scanCommand(_ value: UInt64) -> StartScanCommand {
    StartScanCommand(
        commandId: CommandId(value: value),
        scanSessionId: ScanSessionId(value: value + 100),
        options: ScanOptions(serviceUuids: [], namePrefix: nil, exactName: nil, companyId: nil, minRssi: nil, manufacturerData: nil, android: nil, allowDuplicates: false)
    )
}

func scanStarted(_ command: StartScanCommand) -> BleEvent {
    .scanStarted(
        commandId: command.commandId,
        scanId: command.scanSessionId
    )
}

func uuid(_ value: String) -> BluetoothUuid {
    BluetoothUuid(value: value)
}

func eventCatalog() -> [BleEvent] {
    let serviceUuid = uuid("0000180d-0000-1000-8000-00805f9b34fb")
    let characteristicUuid = uuid("00002a37-0000-1000-8000-00805f9b34fb")
    let descriptorUuid = uuid("00002902-0000-1000-8000-00805f9b34fb")
    let properties = CharacteristicProperties(
        readable: true,
        writableWithResponse: false,
        writableWithoutResponse: false,
        notifiable: true,
        indicatable: false
    )
    let commandId = CommandId(value: 1)
    let connectionId = ConnectionId(value: 301)
    let gattSessionId = GattSessionId(value: 401)
    let characteristicId = GattCharacteristicId(value: 601)
    let subscriptionId = SubscriptionId(value: 801)
    let error = BleEventError(
        category: .gatt,
        message: "fixture failure",
        platformDetails: PlatformErrorDetails(
            platform: .test,
            code: "fixture-code",
            name: "FixtureError",
            message: "fixture platform detail"
        )
    )
    let advertisement = AdvertisementObservation(
        scanSessionId: ScanSessionId(value: 101),
        peripheralId: PeripheralId(value: 201),
        localName: "BleHub Fixture",
        advertisedServices: [serviceUuid],
        manufacturerData: [
            ManufacturerData(companyId: 0x1234, payload: Data([0x01, 0x02]))
        ],
        serviceData: [
            ServiceData(serviceUuid: serviceUuid, payload: Data([0x03, 0x04]))
        ],
        rssi: -42,
        connectable: nil
    )
    let database = GattDatabase(
        connectionId: connectionId,
        gattSessionId: gattSessionId,
        services: [
            GattService(
                id: GattServiceId(value: 501),
                uuid: serviceUuid,
                primary: true,
                characteristics: [
                    GattCharacteristic(
                        id: characteristicId,
                        uuid: characteristicUuid,
                        properties: properties,
                        descriptors: [
                            GattDescriptor(
                                id: GattDescriptorId(value: 701),
                                uuid: descriptorUuid
                            )
                        ]
                    )
                ]
            ),
            GattService(
                id: GattServiceId(value: 502),
                uuid: serviceUuid,
                primary: false,
                characteristics: [
                    GattCharacteristic(
                        id: GattCharacteristicId(value: 602),
                        uuid: characteristicUuid,
                        properties: properties,
                        descriptors: [
                            GattDescriptor(
                                id: GattDescriptorId(value: 702),
                                uuid: descriptorUuid
                            )
                        ]
                    )
                ]
            ),
        ]
    )

    return [
        .adapterSnapshotChanged(
            commandId: commandId,
            snapshot: AdapterSnapshot(
                state: .poweredOn,
                scanAuthorization: .authorized,
                connectionAuthorization: .authorized,
                name: "Fixture Adapter"
            )
        ),
        .scanStarted(commandId: commandId, scanId: advertisement.scanSessionId),
        .advertisementObserved(advertisement: advertisement),
        .scanStopped(
            scanId: ScanSessionId(value: 101),
            result: .ok(reason: .requested(commandId: commandId))
        ),
        .scanStopped(
            scanId: ScanSessionId(value: 102),
            result: .err(error: error)
        ),
        .connectionStateChanged(
            commandId: commandId,
            connectionId: connectionId,
            result: .ok(state: .connected)
        ),
        .connectionStateChanged(
            commandId: nil,
            connectionId: connectionId,
            result: .err(error: error)
        ),
        .gattDiscovered(commandId: commandId, database: database),
        .characteristicRead(
            commandId: commandId,
            connectionId: connectionId,
            gattSessionId: gattSessionId,
            characteristicId: characteristicId,
            value: Data([0x10, 0x11])
        ),
        .characteristicWritten(
            commandId: commandId,
            connectionId: connectionId,
            gattSessionId: gattSessionId,
            characteristicId: characteristicId
        ),
        .notificationStateChanged(
            commandId: commandId,
            connectionId: connectionId,
            gattSessionId: gattSessionId,
            subscriptionId: subscriptionId,
            characteristicId: characteristicId,
            state: .enabled(actualMode: .notification)
        ),
        .notificationReceived(
            connectionId: connectionId,
            gattSessionId: gattSessionId,
            subscriptionId: subscriptionId,
            characteristicId: characteristicId,
            value: Data([0x20, 0x21])
        ),
        .notificationStateChanged(
            commandId: commandId,
            connectionId: connectionId,
            gattSessionId: gattSessionId,
            subscriptionId: subscriptionId,
            characteristicId: characteristicId,
            state: .disabled
        ),
        .rssiRead(commandId: commandId, connectionId: connectionId, rssi: -48),
        .commandCompleted(commandId: commandId, error: nil),
        .commandCompleted(commandId: CommandId(value: 2), error: error),
        .backendError(error: error),
    ]
}

func release(_ hub: inout BleHub?, backend: TestBackend) throws {
    try hub!.dispose()
    try hub!.dispose()
    hub = nil
    precondition(backend.disposeCount == 1)
    precondition(!backend.hasIngress())
}

func runCompleteEventCatalogTest() throws {
    let backend = TestBackend(mode: .noEvent)
    let sink = RecordingSink()
    var hub: BleHub? = try BleHub(backend: backend, downstream: sink)
    let catalog = eventCatalog()
    let statuses = catalog.map { backend.push($0) }

    precondition(statuses.allSatisfy { $0 == .delivered })
    precondition(sink.snapshot() == catalog)
    try release(&hub, backend: backend)
}

func runSynchronousAndThreadTests() throws {
    let backend = TestBackend(mode: .synchronous)
    let sink = RecordingSink()
    var hub: BleHub? = try BleHub(backend: backend, downstream: sink)
    let command = scanCommand(41)

    try hub!.startScan(command: command)
    precondition(
        sink.snapshot() == [
            scanStarted(command),
            .commandCompleted(commandId: command.commandId, error: nil),
        ]
    )
    precondition(backend.pushStatuses == [.delivered, .delivered])
    precondition(hub!.capabilities().platform == .test)
    try release(&hub, backend: backend)

    let callbackDone = DispatchSemaphore(value: 0)
    let threadedBackend = TestBackend(
        mode: .swiftThread,
        callbackDone: callbackDone
    )
    let threadedSink = RecordingSink()
    var threadedHub: BleHub? = try BleHub(
        backend: threadedBackend,
        downstream: threadedSink
    )
    try threadedHub!.startScan(command: scanCommand(43))
    precondition(callbackDone.wait(timeout: .now() + .seconds(5)) == .success)
    precondition(threadedSink.snapshot().count == 2)
    try release(&threadedHub, backend: threadedBackend)
}

func runDispatchAndAssemblyFailureTests() throws {
    let rejectingBackend = TestBackend(mode: .rejectCommand)
    let rejectingSink = RecordingSink()
    var rejectingHub: BleHub? = try BleHub(
        backend: rejectingBackend,
        downstream: rejectingSink
    )
    do {
        try rejectingHub!.startScan(command: scanCommand(1))
        preconditionFailure("expected declared dispatch rejection")
    } catch CommandDispatchError.BackendUnavailable(let detail, _) {
        precondition(detail == "declared dispatch rejection")
    }
    precondition(rejectingSink.snapshot().isEmpty)
    try release(&rejectingHub, backend: rejectingBackend)

    let unexpectedBackend = TestBackend(mode: .unexpectedCommand)
    let unexpectedSink = RecordingSink()
    var unexpectedHub: BleHub? = try BleHub(
        backend: unexpectedBackend,
        downstream: unexpectedSink
    )
    do {
        try unexpectedHub!.startScan(command: scanCommand(2))
        preconditionFailure("expected unexpected dispatch failure")
    } catch CommandDispatchError.FfiContract(let detail) {
        precondition(detail.contains("undeclared command failure"))
    }
    precondition(unexpectedSink.snapshot().isEmpty)
    try release(&unexpectedHub, backend: unexpectedBackend)

    let cases: [(TestBackend, (BleHubError) -> Bool)] = [
        (
            TestBackend(mode: .typedCapabilitiesFailure),
            { if case .BackendAssembly = $0 { true } else { false } }
        ),
        (
            TestBackend(mode: .unexpectedCapabilitiesFailure),
            {
                if case .FfiContract(let detail) = $0 {
                    detail.contains("undeclared capabilities failure")
                } else {
                    false
                }
            }
        ),
        (
            TestBackend(mode: .invalidContractVersion),
            { if case .FfiContract = $0 { true } else { false } }
        ),
        (
            TestBackend(mode: .typedBindFailure),
            { if case .BackendAssembly = $0 { true } else { false } }
        ),
        (
            TestBackend(mode: .unexpectedBindFailure),
            {
                if case .FfiContract(let detail) = $0 {
                    detail.contains("undeclared bind failure")
                } else {
                    false
                }
            }
        ),
    ]
    for (backend, matches) in cases {
        do {
            _ = try BleHub(backend: backend, downstream: RecordingSink())
            preconditionFailure("expected assembly failure")
        } catch let error as BleHubError {
            precondition(matches(error))
        }
        precondition(backend.disposeCount == 1)
        precondition(!backend.hasIngress())
    }
}

func runBindRollbackTests() throws {
    let retained = TestBackend(mode: .retainBeforeBindFailure)
    do {
        _ = try BleHub(backend: retained, downstream: RecordingSink())
        preconditionFailure("expected retained bind failure")
    } catch BleHubError.BackendAssembly {}
    precondition(retained.disposeCount == 1)
    precondition(!retained.hasIngress())

    let pushing = TestBackend(mode: .pushDuringBind)
    do {
        _ = try BleHub(backend: pushing, downstream: RecordingSink())
        preconditionFailure("expected bind-time push failure")
    } catch BleHubError.FfiContract(let detail) {
        precondition(detail.contains("while binding ingress"))
    }
    precondition(pushing.bindPushStatus == .closed)
    precondition(pushing.disposeCount == 1)
    precondition(!pushing.hasIngress())
}

func runSinkBoundaryTest() throws {
    let backend = TestBackend(mode: .noEvent)
    let sink = RecordingSink(behavior: .reject)
    var hub: BleHub? = try BleHub(backend: backend, downstream: sink)

    if case .downstreamFailed(let message) = backend.push(
        scanStarted(scanCommand(3))
    ) {
        precondition(message.contains("declared sink rejection"))
    } else {
        preconditionFailure("expected sink rejection")
    }
    precondition(sink.snapshot().isEmpty)
    try release(&hub, backend: backend)
}

func runCloseDrainAndDisposeTests() throws {
    let backend = TestBackend(mode: .noEvent)
    let sink = RecordingSink(behavior: .block)
    var hub: BleHub? = try BleHub(backend: backend, downstream: sink)
    let pushDone = DispatchSemaphore(value: 0)
    let disposeDone = DispatchSemaphore(value: 0)
    let pushStatus = LockedBox<EventIngressStatus?>(nil)
    let disposeError = LockedBox<Error?>(nil)
    let retainedHub = hub!

    DispatchQueue.global().async {
        pushStatus.set(backend.push(scanStarted(scanCommand(8))))
        pushDone.signal()
    }
    precondition(sink.waitForBlockedCallback(timeoutMillis: 5_000))
    DispatchQueue.global().async {
        do {
            try retainedHub.dispose()
        } catch {
            disposeError.set(error)
        }
        disposeDone.signal()
    }
    precondition(disposeDone.wait(timeout: .now() + .milliseconds(50)) == .timedOut)
    precondition(backend.disposeCount == 0)
    sink.releaseBlockedCallback()
    precondition(pushDone.wait(timeout: .now() + .seconds(5)) == .success)
    precondition(disposeDone.wait(timeout: .now() + .seconds(5)) == .success)
    precondition(disposeError.get() == nil)
    precondition(pushStatus.get() == .delivered)
    precondition(sink.snapshot().count == 1)
    hub = nil
    precondition(backend.disposeCount == 1)
    precondition(!backend.hasIngress())

    let disposingBackend = TestBackend(mode: .pushDuringDispose)
    var disposingHub: BleHub? = try BleHub(
        backend: disposingBackend,
        downstream: RecordingSink()
    )
    try disposingHub!.dispose()
    precondition(disposingBackend.disposePushStatus == .closed)
    disposingHub = nil

    let typedBackend = TestBackend(mode: .typedDisposeFailure)
    var typedHub: BleHub? = try BleHub(
        backend: typedBackend,
        downstream: RecordingSink()
    )
    for _ in 0..<3 {
        do {
            try typedHub!.dispose()
            preconditionFailure("expected cached typed cleanup failure")
        } catch BleHubError.BackendCleanup(let detail, _) {
            precondition(detail == "declared cleanup failure")
        }
        precondition(!typedBackend.hasIngress())
        precondition(typedBackend.disposeCount == 1)
    }
    typedHub = nil

    let unexpectedBackend = TestBackend(mode: .unexpectedDisposeFailure)
    var unexpectedHub: BleHub? = try BleHub(
        backend: unexpectedBackend,
        downstream: RecordingSink()
    )
    for _ in 0..<3 {
        do {
            try unexpectedHub!.dispose()
            preconditionFailure("expected cached unexpected cleanup failure")
        } catch BleHubError.FfiContract(let detail) {
            precondition(detail.contains("undeclared cleanup failure"))
        }
        precondition(!unexpectedBackend.hasIngress())
        precondition(unexpectedBackend.disposeCount == 1)
    }
    unexpectedHub = nil
}

func runLifecycleCycle(sharedDisposeCount: LockedBox<Int>) throws {
    let backend = TestBackend(
        mode: .noEvent,
        sharedDisposeCount: sharedDisposeCount
    )
    var hub: BleHub? = try BleHub(
        backend: backend,
        downstream: RecordingSink()
    )
    try hub!.dispose()
    try hub!.dispose()
    hub = nil
    precondition(!backend.hasIngress())
}

func runLifecycleStressTest() throws {
    let disposeCount = LockedBox(0)
    for _ in 0..<10_000 {
        try runLifecycleCycle(sharedDisposeCount: disposeCount)
    }
    precondition(disposeCount.get() == 10_000)
}

try runCompleteEventCatalogTest()
try runSynchronousAndThreadTests()
try runDispatchAndAssemblyFailureTests()
try runBindRollbackTests()
try runSinkBoundaryTest()
try runCloseDrainAndDisposeTests()
try runLifecycleStressTest()
print("swift_uniffi_production_runtime=passed")
