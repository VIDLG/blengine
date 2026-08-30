import Dispatch
import Foundation

final class WeakBox<Value: AnyObject> {
    weak var value: Value?

    init(_ value: Value) {
        self.value = value
    }
}

enum NativeFailure: Error {
    case undeclared
}

enum BackendMode: Equatable {
    case synchronous
    case noEvent
    case reject
    case unexpected
    case unexpectedBind
}

final class TestBackend: BleBackend, @unchecked Sendable {
    private let lock = NSLock()
    private let mode: BackendMode
    private var ingressToken: BackendEventIngressToken?
    private var capabilitiesCalls = 0
    private var bindCalls = 0
    private var disposeCalls = 0

    init(mode: BackendMode) {
        self.mode = mode
    }

    var capabilitiesCount: Int {
        lock.withLock { capabilitiesCalls }
    }

    var bindCount: Int {
        lock.withLock { bindCalls }
    }

    var disposeCount: Int {
        lock.withLock { disposeCalls }
    }

    func capabilities() throws -> BackendCapabilities {
        lock.withLock {
            capabilitiesCalls += 1
        }
        return BackendCapabilities(platform: .test, contractVersion: fixtureContractVersion)
    }

    func bindEventIngress(token: BackendEventIngressToken) throws {
        lock.withLock {
            bindCalls += 1
        }
        if mode == .unexpectedBind {
            throw NativeFailure.undeclared
        }
        lock.withLock {
            precondition(ingressToken == nil)
            ingressToken = token
        }
    }

    func startScan(command: StartScanCommand) throws {
        switch mode {
        case .synchronous:
            precondition(push(scanStarted(command)) == .delivered)
        case .noEvent:
            break
        case .reject:
            throw CommandDispatchError.backendUnavailable(
                detail: "declared rejection",
                platformDetails: nil
            )
        case .unexpected:
            throw NativeFailure.undeclared
        case .unexpectedBind:
            preconditionFailure("fixture construction should have failed")
        }
    }

    func connect(command: ConnectCommand) throws {}

    func dispose() throws {
        lock.withLock {
            disposeCalls += 1
        }
    }

    func push(_ event: BleEvent) -> EventIngressStatus {
        pushBackendEvent(token: retainedToken(), event: event)
    }

    func retainedToken() -> BackendEventIngressToken {
        lock.withLock {
            precondition(ingressToken != nil)
            return ingressToken!
        }
    }
}

func scanStarted(_ command: StartScanCommand) -> BleEvent {
    BleEvent(
        payload: .scanStarted(
            commandId: command.commandId,
            scanId: command.scanSessionId
        )
    )
}

func runBasicTests() throws {
    let backend = TestBackend(mode: .synchronous)
    var fixture: Phase0Fixture? = try Phase0Fixture(backend: backend)
    let command = StartScanCommand(
        commandId: CommandId(value: 1),
        scanSessionId: ScanSessionId(value: 2)
    )

    precondition(fixtureContractVersion == 2)
    precondition(backend.capabilitiesCount == 1)
    precondition(backend.bindCount == 1)
    precondition(fixture!.capabilities().platform == .test)

    try fixture!.startScan(command: command)
    try fixture!.startScanFromRustThread(command: command)
    DispatchQueue.concurrentPerform(iterations: 64) { _ in
        precondition(backend.push(scanStarted(command)) == .delivered)
    }

    let snapshot = fixture!.snapshot()
    precondition(snapshot.eventCount == 66)
    precondition(snapshot.maximumActiveCallbacks == 1)
    precondition(fixture!.events().count == 66)

    try fixture!.dispose()
    try fixture!.dispose()
    precondition(backend.disposeCount == 1)
    precondition(backend.push(scanStarted(command)) == .closed)
    fixture = nil
}

func runInFlightDisposeTest() throws {
    let backend = TestBackend(mode: .noEvent)
    let fixture = try Phase0Fixture(backend: backend)
    let command = StartScanCommand(
        commandId: CommandId(value: 3),
        scanSessionId: ScanSessionId(value: 4)
    )
    fixture.setSinkBehavior(behavior: .block)
    let pushFinished = DispatchSemaphore(value: 0)
    let disposeFinished = DispatchSemaphore(value: 0)

    DispatchQueue.global().async {
        precondition(backend.push(scanStarted(command)) == .delivered)
        pushFinished.signal()
    }
    precondition(fixture.waitForBlockedCallback(timeoutMillis: 5_000))

    DispatchQueue.global().async {
        try! fixture.dispose()
        disposeFinished.signal()
    }
    precondition(disposeFinished.wait(timeout: .now() + .milliseconds(100)) == .timedOut)

    fixture.releaseBlockedCallback()
    precondition(pushFinished.wait(timeout: .now() + .seconds(5)) == .success)
    precondition(disposeFinished.wait(timeout: .now() + .seconds(5)) == .success)
    precondition(backend.disposeCount == 1)
    precondition(fixture.snapshot().activeCallbacks == 0)
    precondition(backend.push(scanStarted(command)) == .closed)
}

func runErrorTests() throws {
    let command = StartScanCommand(
        commandId: CommandId(value: 5),
        scanSessionId: ScanSessionId(value: 6)
    )

    let rejectingBackend = TestBackend(mode: .reject)
    var rejectingFixture: Phase0Fixture? = try Phase0Fixture(backend: rejectingBackend)
    do {
        try rejectingFixture!.startScan(command: command)
        preconditionFailure("expected declared rejection")
    } catch let CommandDispatchError.backendUnavailable(detail, platform) {
        precondition(detail == "declared rejection")
        precondition(platform == nil)
    }
    try rejectingFixture!.dispose()
    rejectingFixture = nil

    let unexpectedBackend = TestBackend(mode: .unexpected)
    var unexpectedFixture: Phase0Fixture? = try Phase0Fixture(backend: unexpectedBackend)
    do {
        try unexpectedFixture!.startScan(command: command)
        preconditionFailure("expected unexpected callback error")
    } catch let CommandDispatchError.ffiContract(detail) {
        precondition(detail.contains("undeclared"))
    }
    try unexpectedFixture!.dispose()
    unexpectedFixture = nil

    let unexpectedBindBackend = TestBackend(mode: .unexpectedBind)
    do {
        _ = try Phase0Fixture(backend: unexpectedBindBackend)
        preconditionFailure("expected unexpected bind error")
    } catch let BleHubError.ffiContract(detail) {
        precondition(detail.contains("undeclared"))
    }
    precondition(unexpectedBindBackend.disposeCount == 1)
}

func runLifecycleCycle(_ command: StartScanCommand, tokens: inout Set<UInt64>) throws {
    var backend: TestBackend? = TestBackend(mode: .noEvent)
    let releasedBackend = WeakBox(backend!)
    var fixture: Phase0Fixture? = try Phase0Fixture(backend: backend!)
    let token = backend!.retainedToken()
    precondition(tokens.insert(token.value).inserted)

    backend = nil
    try fixture!.dispose()
    fixture = nil
    precondition(releasedBackend.value == nil)
    precondition(pushBackendEvent(token: token, event: scanStarted(command)) == .closed)
}

try runBasicTests()
try runInFlightDisposeTest()
try runErrorTests()

let lifecycleCommand = StartScanCommand(
    commandId: CommandId(value: 7),
    scanSessionId: ScanSessionId(value: 8)
)
var lifecycleTokens = Set<UInt64>()
for _ in 0..<10_000 {
    try runLifecycleCycle(lifecycleCommand, tokens: &lifecycleTokens)
}
precondition(lifecycleTokens.count == 10_000)

print("swift_macos_token_ingress_smoke=passed")
