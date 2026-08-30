import Dispatch
import Foundation

final class WeakBox<Value: AnyObject> {
    weak var value: Value?

    init(_ value: Value) {
        self.value = value
    }
}

final class TestBackend: BleBackend {
    private(set) var token: BackendEventIngressToken?
    private(set) var capabilitiesCount = 0
    private(set) var bindCount = 0
    private(set) var disposeCount = 0

    func capabilities() throws -> BackendCapabilities {
        capabilitiesCount += 1
        return BackendCapabilities(platform: .test, contractVersion: fixtureContractVersion)
    }

    func bindEventIngress(token: BackendEventIngressToken) throws {
        bindCount += 1
        precondition(self.token == nil)
        self.token = token
    }

    func startScan(command: StartScanCommand) throws {
        let token = self.token!
        let event = BleEvent(
            payload: .scanStarted(
                commandId: command.commandId,
                scanId: command.scanSessionId
            )
        )
        precondition(pushBackendEvent(token: token, event: event) == .delivered)
    }

    func connect(command: ConnectCommand) throws {}

    func dispose() throws {
        disposeCount += 1
    }
}

let backend = TestBackend()
var fixture: Phase0Fixture? = try Phase0Fixture(backend: backend)
precondition(fixtureContractVersion == 2)
precondition(backend.capabilitiesCount == 1)
precondition(backend.bindCount == 1)
precondition(fixture!.capabilities().platform == .test)

let command = StartScanCommand(
    commandId: CommandId(value: 1),
    scanSessionId: ScanSessionId(value: 2)
)
try fixture!.startScan(command: command)
try fixture!.startScanFromRustThread(command: command)

let token = backend.token!
DispatchQueue.concurrentPerform(iterations: 64) { _ in
    let status = pushBackendEvent(
        token: token,
        event: BleEvent(
            payload: .scanStarted(
                commandId: command.commandId,
                scanId: command.scanSessionId
            )
        )
    )
    precondition(status == .delivered)
}

let snapshot = fixture!.snapshot()
precondition(snapshot.eventCount == 66)
precondition(snapshot.maximumActiveCallbacks == 1)
precondition(fixture!.events().count == 66)

try fixture!.dispose()
try fixture!.dispose()
precondition(backend.disposeCount == 1)
precondition(
    pushBackendEvent(
        token: token,
        event: BleEvent(
            payload: .scanStarted(
                commandId: command.commandId,
                scanId: command.scanSessionId
            )
        )
    ) == .closed
)

fixture = nil

var lifecycleTokens = Set<UInt64>()
for _ in 0..<10_000 {
    var lifecycleBackend: TestBackend? = TestBackend()
    let releasedBackend = WeakBox(lifecycleBackend!)
    var lifecycleFixture: Phase0Fixture? = try Phase0Fixture(backend: lifecycleBackend!)
    let lifecycleToken = lifecycleBackend!.token!
    precondition(lifecycleTokens.insert(lifecycleToken.value).inserted)

    lifecycleBackend = nil
    try lifecycleFixture!.dispose()
    lifecycleFixture = nil
    precondition(releasedBackend.value == nil)
    precondition(
        pushBackendEvent(
            token: lifecycleToken,
            event: BleEvent(
                payload: .scanStarted(
                    commandId: command.commandId,
                    scanId: command.scanSessionId
                )
            )
        ) == .closed
    )
}
precondition(lifecycleTokens.count == 10_000)

print("swift_windows_token_ingress_smoke=passed")
