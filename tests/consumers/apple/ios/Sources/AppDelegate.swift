import BleHubUniFfi
import Foundation
import UIKit

private enum ConsumerFailure: Error {
    case invalid(String)
}

private func require(_ condition: @autoclosure () -> Bool, _ message: String) throws {
    if !condition() { throw ConsumerFailure.invalid(message) }
}

private final class RecordingSink: BleEventSink {
    private(set) var events: [BleEvent] = []

    func onEvent(event: BleEvent) throws {
        events.append(event)
    }
}

private final class FakeBackend: BleBackend {
    private var ingress: BackendEventIngressHandle?
    private(set) var disposeCount = 0
    private(set) var statuses: [EventIngressStatus] = []
    var boundIngress: BackendEventIngressHandle? { ingress }

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
        self.ingress = ingress
    }

    func initialize(command: InitializeCommand) throws {}

    func startScan(command: StartScanCommand) throws {
        guard let ingress else { throw ConsumerFailure.invalid("ingress not bound") }
        statuses = [
            ingress.push(event: .scanStarted(commandId: command.commandId, scanId: command.scanSessionId)),
            ingress.push(event: .commandCompleted(commandId: command.commandId, error: nil)),
        ]
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
        disposeCount += 1
        ingress = nil
    }
}

private func verifyArtifact() throws {
    let backend = FakeBackend()
    let sink = RecordingSink()
    let hub = try BleHub(backend: backend, downstream: sink)
    try require(hub.capabilities().platform == .test, "assembly capabilities mismatch")

    let command = StartScanCommand(
        commandId: CommandId(value: 17),
        scanSessionId: ScanSessionId(value: 23),
        options: ScanOptions(serviceUuids: [], namePrefix: nil, companyId: nil, allowDuplicates: false)
    )
    try hub.startScan(command: command)
    try require(backend.statuses == [.delivered, .delivered], "ingress did not deliver both callbacks")
    try require(sink.events == [
        .scanStarted(commandId: command.commandId, scanId: command.scanSessionId),
        .commandCompleted(commandId: command.commandId, error: nil),
    ], "domain and terminal event order or payload mismatch")

    guard let retainedIngress = backend.boundIngress else {
        throw ConsumerFailure.invalid("backend lost ingress before dispose")
    }
    try hub.dispose()
    try require(backend.disposeCount == 1, "backend was not explicitly disposed once")
    try require(
        retainedIngress.push(event: .scanStarted(commandId: command.commandId, scanId: command.scanSessionId)) == .closed,
        "ingress remained open after dispose"
    )
    try require(sink.events.count == 2, "sink received event after dispose")
}

@main
final class AppDelegate: UIResponder, UIApplicationDelegate {
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
        let documents = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        let result = documents.appendingPathComponent("blehub-ios-artifact-result.txt")
        let text: String
        do {
            try verifyArtifact()
            text = "apple_ios_artifact_run_id=\(runId)\napple_ios_artifact_consumer=passed\n"
        } catch {
            text = "apple_ios_artifact_run_id=\(runId)\napple_ios_artifact_consumer=failed\nerror=\(error)\n"
        }
        do {
            try text.write(to: result, atomically: true, encoding: .utf8)
        } catch {
            NSLog("cannot write iOS artifact consumer result: %@", String(describing: error))
        }
        return true
    }
}
