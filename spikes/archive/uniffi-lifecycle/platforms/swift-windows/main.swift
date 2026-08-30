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

enum BackendMode {
    case synchronous
    case noEvent
    case reject
    case unexpected
    case unexpectedBind
}

final class TestBackend: BleBackend, @unchecked Sendable {
    private let lock = NSLock()
    private let mode: BackendMode
    private var ingress: BackendEventIngressHandle?
    private(set) var disposeCount = 0

    init(mode: BackendMode) {
        self.mode = mode
    }

    func bindEventIngress(ingress: BackendEventIngressHandle) throws {
        if mode == .unexpectedBind {
            throw NativeFailure.undeclared
        }
        lock.withLock {
            precondition(self.ingress == nil)
            self.ingress = ingress
        }
    }

    func startScan(command: StartScanCommand) throws {
        switch mode {
        case .synchronous:
            precondition(
                push(
                    BleEvent(
                        sequence: 1,
                        commandId: command.commandId,
                        payload: Data([1, 2, 3])
                    )
                ) == .delivered
            )
        case .noEvent:
            break
        case .reject:
            throw CommandDispatchError.Rejected(reason: "declared rejection")
        case .unexpected:
            throw NativeFailure.undeclared
        case .unexpectedBind:
            preconditionFailure("fixture construction should have failed")
        }
    }

    func dispose() throws {
        lock.withLock {
            disposeCount += 1
        }
    }

    func push(_ event: BleEvent) -> EventIngressStatus {
        lock.withLock {
            precondition(ingress != nil)
            return ingress!.push(event: event)
        }
    }

    func retainedIngress() -> BackendEventIngressHandle {
        lock.withLock {
            precondition(ingress != nil)
            return ingress!
        }
    }
}

func runBasicTests() throws {
    let backend = TestBackend(mode: .synchronous)
    var fixture: Phase0Fixture? = try Phase0Fixture(backend: backend)
    let command = StartScanCommand(commandId: 41, scanSessionId: 7)

    try fixture!.startScan(command: command)
    try fixture!.startScanFromRustThread(command: command)
    DispatchQueue.concurrentPerform(iterations: 64) { sequence in
        precondition(
            backend.push(
                BleEvent(
                    sequence: UInt64(sequence + 10),
                    commandId: nil,
                    payload: Data([UInt8(sequence & 0xff)])
                )
            ) == .delivered
        )
    }

    precondition(fixture!.events().count == 66)
    try fixture!.dispose()
    try fixture!.dispose()
    precondition(backend.disposeCount == 1)
    precondition(
        backend.push(BleEvent(sequence: 100, commandId: nil, payload: Data())) == .closed
    )
    fixture = nil
}

func runInFlightDisposeTest() throws {
    let backend = TestBackend(mode: .noEvent)
    let fixture = try Phase0Fixture(backend: backend)
    fixture.setCallbackBlocking(blocked: true)
    let pushFinished = DispatchSemaphore(value: 0)
    let disposeFinished = DispatchSemaphore(value: 0)

    DispatchQueue.global().async {
        precondition(
            backend.push(BleEvent(sequence: 200, commandId: nil, payload: Data())) == .delivered
        )
        pushFinished.signal()
    }
    precondition(fixture.waitForBlockedCallback(timeoutMillis: 5_000))

    DispatchQueue.global().async {
        try! fixture.dispose()
        disposeFinished.signal()
    }
    precondition(disposeFinished.wait(timeout: .now() + .milliseconds(100)) == .timedOut)

    fixture.releaseBlockedCallbacks()
    precondition(pushFinished.wait(timeout: .now() + .seconds(5)) == .success)
    precondition(disposeFinished.wait(timeout: .now() + .seconds(5)) == .success)
    precondition(backend.disposeCount == 1)
    precondition(fixture.events().count == 1)
    precondition(
        backend.push(BleEvent(sequence: 201, commandId: nil, payload: Data())) == .closed
    )
}

func runErrorTests() throws {
    let rejectingBackend = TestBackend(mode: .reject)
    var rejectingFixture: Phase0Fixture? = try Phase0Fixture(backend: rejectingBackend)
    do {
        try rejectingFixture!.startScan(
            command: StartScanCommand(commandId: 1, scanSessionId: 1)
        )
        preconditionFailure("expected declared rejection")
    } catch CommandDispatchError.Rejected(let reason) {
        precondition(reason == "declared rejection")
    }
    try rejectingFixture!.dispose()
    rejectingFixture = nil

    let unexpectedBackend = TestBackend(mode: .unexpected)
    var unexpectedFixture: Phase0Fixture? = try Phase0Fixture(backend: unexpectedBackend)
    do {
        try unexpectedFixture!.startScan(
            command: StartScanCommand(commandId: 2, scanSessionId: 2)
        )
        preconditionFailure("expected unexpected callback error")
    } catch CommandDispatchError.UnexpectedCallback(let reason) {
        precondition(reason.contains("undeclared"))
    }
    try unexpectedFixture!.dispose()
    unexpectedFixture = nil

    do {
        _ = try Phase0Fixture(backend: TestBackend(mode: .unexpectedBind))
        preconditionFailure("expected unexpected bind error")
    } catch BleHubError.UnexpectedCallback(let reason) {
        precondition(reason.contains("undeclared"))
    }
}

func runLifecycleCycle(_ sequence: UInt64) throws {
    var backend: TestBackend? = TestBackend(mode: .noEvent)
    let releasedBackend = WeakBox(backend!)
    var fixture: Phase0Fixture? = try Phase0Fixture(backend: backend!)
    let ingress = backend!.retainedIngress()

    backend = nil
    try fixture!.dispose()
    fixture = nil
    precondition(releasedBackend.value == nil)
    precondition(
        ingress.push(event: BleEvent(sequence: sequence, commandId: nil, payload: Data())) == .closed
    )
}

try runBasicTests()
try runInFlightDisposeTest()
try runErrorTests()

let baseline = lifecycleCounters()
for sequence in 0..<10_000 {
    try runLifecycleCycle(UInt64(sequence))
}
let finalCounters = lifecycleCounters()
precondition(finalCounters.ingressCreated == baseline.ingressCreated + 10_000)
precondition(finalCounters.ingressDropped == baseline.ingressDropped + 10_000)

print("swift_windows_uniffi_direct_ingress_smoke=passed")
