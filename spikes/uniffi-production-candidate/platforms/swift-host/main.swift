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

final class CandidateBackend: BleBackend, @unchecked Sendable {
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
            throw BleHubError.BackendAssembly(detail: "declared capabilities failure")
        case .unexpectedCapabilitiesFailure:
            throw NativeFailure.undeclared("undeclared capabilities failure")
        case .invalidContractVersion:
            return BackendCapabilities(
                platform: .test,
                contractVersion: fixtureContractVersion() + 1,
                supportsDuplicateScanResults: false,
                supportsConnectableFlag: false,
                supportsReadRssi: false,
                supportsRequiredNotification: false,
                supportsRequiredIndication: false,
                reportsActualNotificationMode: false
            )
        default:
            return BackendCapabilities(
                platform: .test,
                contractVersion: fixtureContractVersion(),
                supportsDuplicateScanResults: false,
                supportsConnectableFlag: false,
                supportsReadRssi: false,
                supportsRequiredNotification: false,
                supportsRequiredIndication: false,
                reportsActualNotificationMode: false
            )
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
            throw BleHubError.BackendAssembly(detail: "retained before bind failure")
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

    func dispose() throws {
        let root = lock.withLock { () -> BackendEventIngressHandle? in
            disposeCount += 1
            return ingress.swap(with: nil)
        }
        if let sharedDisposeCount {
            sharedDisposeCount.set(sharedDisposeCount.get() + 1)
        }
        if mode == .pushDuringDispose, let root {
            let status = root.push(event: scanStarted(scanCommand(91)))
            lock.withLock { disposePushStatus = status }
        }
        switch mode {
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
        options: ScanOptions(
            serviceUuids: [],
            namePrefix: nil,
            companyId: nil,
            allowDuplicates: false
        )
    )
}

func scanStarted(_ command: StartScanCommand) -> BleEvent {
    .scanStarted(
        commandId: command.commandId,
        scanId: command.scanSessionId
    )
}

func release(_ fixture: inout ProductionCandidateFixture?, backend: CandidateBackend) throws {
    try fixture!.dispose()
    try fixture!.dispose()
    fixture = nil
    precondition(backend.disposeCount == 1)
    precondition(!backend.hasIngress())
}

func runCompleteEventCatalogTest() throws {
    let backend = CandidateBackend(mode: .noEvent)
    var fixture: ProductionCandidateFixture? = try ProductionCandidateFixture(backend: backend)
    let statuses = fixtureEventCatalog().map { backend.push($0) }

    precondition(statuses.allSatisfy { $0 == .delivered })
    precondition(fixtureEventCatalogMatches(events: fixture!.snapshot().events))
    try release(&fixture, backend: backend)
}

func runSynchronousAndThreadTests() throws {
    let backend = CandidateBackend(mode: .synchronous)
    var fixture: ProductionCandidateFixture? = try ProductionCandidateFixture(backend: backend)
    let command = scanCommand(41)

    try fixture!.startScan(command: command)
    precondition(
        fixture!.snapshot().events == [
            scanStarted(command),
            .commandCompleted(commandId: command.commandId, error: nil),
        ]
    )
    precondition(backend.pushStatuses == [.delivered, .delivered])
    precondition(fixture!.capabilities().platform == .test)
    try fixture!.startScanFromRustThread(command: scanCommand(42))
    precondition(fixture!.snapshot().events.count == 4)
    try release(&fixture, backend: backend)

    let callbackDone = DispatchSemaphore(value: 0)
    let threadedBackend = CandidateBackend(mode: .swiftThread, callbackDone: callbackDone)
    var threadedFixture: ProductionCandidateFixture? = try ProductionCandidateFixture(
        backend: threadedBackend
    )
    try threadedFixture!.startScan(command: scanCommand(43))
    precondition(callbackDone.wait(timeout: .now() + .seconds(5)) == .success)
    precondition(threadedFixture!.snapshot().events.count == 2)
    try release(&threadedFixture, backend: threadedBackend)
}

func runDispatchAndAssemblyFailureTests() throws {
    let rejectingBackend = CandidateBackend(mode: .rejectCommand)
    var rejectingFixture: ProductionCandidateFixture? = try ProductionCandidateFixture(
        backend: rejectingBackend
    )
    do {
        try rejectingFixture!.startScan(command: scanCommand(1))
        preconditionFailure("expected declared dispatch rejection")
    } catch CommandDispatchError.BackendUnavailable(let detail, _) {
        precondition(detail == "declared dispatch rejection")
    }
    precondition(rejectingFixture!.snapshot().events.isEmpty)
    try release(&rejectingFixture, backend: rejectingBackend)

    let unexpectedBackend = CandidateBackend(mode: .unexpectedCommand)
    var unexpectedFixture: ProductionCandidateFixture? = try ProductionCandidateFixture(
        backend: unexpectedBackend
    )
    do {
        try unexpectedFixture!.startScan(command: scanCommand(2))
        preconditionFailure("expected unexpected dispatch failure")
    } catch CommandDispatchError.FfiContract(let detail) {
        precondition(detail.contains("undeclared command failure"))
    }
    precondition(unexpectedFixture!.snapshot().events.isEmpty)
    try release(&unexpectedFixture, backend: unexpectedBackend)

    let before = fixtureLifecycleCounters()
    let cases: [(CandidateBackend, (BleHubError) -> Bool)] = [
        (
            CandidateBackend(mode: .typedCapabilitiesFailure),
            { if case .BackendAssembly = $0 { true } else { false } }
        ),
        (
            CandidateBackend(mode: .unexpectedCapabilitiesFailure),
            { if case .FfiContract(let detail) = $0 { detail.contains("undeclared capabilities failure") } else { false } }
        ),
        (
            CandidateBackend(mode: .invalidContractVersion),
            { if case .FfiContract = $0 { true } else { false } }
        ),
        (
            CandidateBackend(mode: .typedBindFailure),
            { if case .BackendAssembly = $0 { true } else { false } }
        ),
        (
            CandidateBackend(mode: .unexpectedBindFailure),
            { if case .FfiContract(let detail) = $0 { detail.contains("undeclared bind failure") } else { false } }
        ),
    ]
    for (backend, matches) in cases {
        do {
            _ = try ProductionCandidateFixture(backend: backend)
            preconditionFailure("expected assembly failure")
        } catch let error as BleHubError {
            precondition(matches(error))
        }
        precondition(backend.disposeCount == 1)
        precondition(!backend.hasIngress())
    }
    let after = fixtureLifecycleCounters()
    precondition(after.fixtureCreated == before.fixtureCreated)
    precondition(after.fixtureDropped == before.fixtureDropped)
    precondition(after.sinkCreated == before.sinkCreated + 5)
    precondition(after.sinkDropped == before.sinkDropped + 5)
}

func runBindRollbackTests() throws {
    let retained = CandidateBackend(mode: .retainBeforeBindFailure)
    do {
        _ = try ProductionCandidateFixture(backend: retained)
        preconditionFailure("expected retained bind failure")
    } catch BleHubError.BackendAssembly {}
    precondition(retained.disposeCount == 1)
    precondition(!retained.hasIngress())

    let pushing = CandidateBackend(mode: .pushDuringBind)
    do {
        _ = try ProductionCandidateFixture(backend: pushing)
        preconditionFailure("expected bind-time push failure")
    } catch BleHubError.FfiContract(let detail) {
        precondition(detail.contains("while binding ingress"))
    }
    precondition(pushing.bindPushStatus == .closed)
    precondition(pushing.disposeCount == 1)
    precondition(!pushing.hasIngress())
}

func runSinkBoundaryTests() throws {
    let backend = CandidateBackend(mode: .noEvent)
    var fixture: ProductionCandidateFixture? = try ProductionCandidateFixture(backend: backend)

    fixture!.setSinkBehavior(behavior: .reject)
    if case .downstreamFailed = backend.push(scanStarted(scanCommand(3))) {} else {
        preconditionFailure("expected sink rejection")
    }
    fixture!.setSinkBehavior(behavior: .panic)
    if case .downstreamFailed = backend.push(scanStarted(scanCommand(4))) {} else {
        preconditionFailure("expected contained sink panic")
    }
    precondition(fixture!.snapshot().events.isEmpty)
    try release(&fixture, backend: backend)
}

func runCloseDrainAndDisposeTests() throws {
    let backend = CandidateBackend(mode: .noEvent)
    var fixture: ProductionCandidateFixture? = try ProductionCandidateFixture(backend: backend)
    fixture!.setSinkBehavior(behavior: .block)
    let pushDone = DispatchSemaphore(value: 0)
    let disposeDone = DispatchSemaphore(value: 0)
    let pushStatus = LockedBox<EventIngressStatus?>(nil)
    let disposeError = LockedBox<Error?>(nil)
    let retainedFixture = fixture!

    DispatchQueue.global().async {
        pushStatus.set(backend.push(scanStarted(scanCommand(8))))
        pushDone.signal()
    }
    precondition(fixture!.waitForBlockedCallback(timeoutMillis: 5_000))
    DispatchQueue.global().async {
        do {
            try retainedFixture.dispose()
        } catch {
            disposeError.set(error)
        }
        disposeDone.signal()
    }
    precondition(disposeDone.wait(timeout: .now() + .milliseconds(50)) == .timedOut)
    fixture!.releaseBlockedCallback()
    precondition(pushDone.wait(timeout: .now() + .seconds(5)) == .success)
    precondition(disposeDone.wait(timeout: .now() + .seconds(5)) == .success)
    precondition(disposeError.get() == nil)
    precondition(pushStatus.get() == .delivered)
    precondition(fixture!.snapshot().events.count == 1)
    fixture = nil
    precondition(!backend.hasIngress())

    let disposingBackend = CandidateBackend(mode: .pushDuringDispose)
    var disposingFixture: ProductionCandidateFixture? = try ProductionCandidateFixture(
        backend: disposingBackend
    )
    try disposingFixture!.dispose()
    precondition(disposingBackend.disposePushStatus == .closed)
    disposingFixture = nil

    let typedBackend = CandidateBackend(mode: .typedDisposeFailure)
    var typedFixture: ProductionCandidateFixture? = try ProductionCandidateFixture(
        backend: typedBackend
    )
    do {
        try typedFixture!.dispose()
        preconditionFailure("expected typed cleanup failure")
    } catch BleHubError.BackendCleanup(let detail, _) {
        precondition(detail == "declared cleanup failure")
    }
    precondition(!typedBackend.hasIngress())
    try typedFixture!.dispose()
    typedFixture = nil

    let unexpectedBackend = CandidateBackend(mode: .unexpectedDisposeFailure)
    var unexpectedFixture: ProductionCandidateFixture? = try ProductionCandidateFixture(
        backend: unexpectedBackend
    )
    do {
        try unexpectedFixture!.dispose()
        preconditionFailure("expected unexpected cleanup failure")
    } catch BleHubError.FfiContract(let detail) {
        precondition(detail.contains("undeclared cleanup failure"))
    }
    precondition(!unexpectedBackend.hasIngress())
    try unexpectedFixture!.dispose()
    unexpectedFixture = nil
}

func runLifecycleCycle(sharedDisposeCount: LockedBox<Int>) throws {
    let backend = CandidateBackend(mode: .noEvent, sharedDisposeCount: sharedDisposeCount)
    var fixture: ProductionCandidateFixture? = try ProductionCandidateFixture(backend: backend)
    try fixture!.dispose()
    try fixture!.dispose()
    fixture = nil
    precondition(!backend.hasIngress())
}

func runLifecycleStressTest() throws {
    let before = fixtureLifecycleCounters()
    let disposeCount = LockedBox(0)
    for _ in 0..<10_000 {
        try runLifecycleCycle(sharedDisposeCount: disposeCount)
    }
    let after = fixtureLifecycleCounters()
    precondition(disposeCount.get() == 10_000)
    precondition(after.fixtureCreated == before.fixtureCreated + 10_000)
    precondition(after.fixtureDropped == before.fixtureDropped + 10_000)
    precondition(after.sinkCreated == before.sinkCreated + 10_000)
    precondition(after.sinkDropped == before.sinkDropped + 10_000)
}

try runCompleteEventCatalogTest()
try runSynchronousAndThreadTests()
try runDispatchAndAssemblyFailureTests()
try runBindRollbackTests()
try runSinkBoundaryTests()
try runCloseDrainAndDisposeTests()
try runLifecycleStressTest()
print("swift_uniffi_production_candidate=passed")
