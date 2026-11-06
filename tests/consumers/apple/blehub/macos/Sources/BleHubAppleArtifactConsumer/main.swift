import BleHubUniFfi

final class ArtifactBackend: BleBackend {
    private(set) var ingress: BackendEventIngressHandle?
    private(set) var statuses: [EventIngressStatus] = []
    private(set) var disposeCount = 0
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
        guard let ingress else { throw ArtifactConsumerError.ingressNotBound }
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
    func cancelOperation(request: CancelOperationRequest) throws {}
    func dispose() throws {
        disposeCount += 1
        ingress = nil
    }
}

enum ArtifactConsumerError: Error {
    case ingressNotBound
}

final class RecordingSink: BleEventSink {
    private(set) var events: [BleEvent] = []

    func onEvent(event: BleEvent) throws {
        events.append(event)
    }
}

do {
    let backend = ArtifactBackend()
    let sink = RecordingSink()
    let hub = try BleHub(backend: backend, downstream: sink)
    precondition(hub.capabilities().platform == .test)

    let command = StartScanCommand(
        commandId: CommandId(value: 17),
        scanSessionId: ScanSessionId(value: 23),
        options: ScanOptions(serviceUuids: [], namePrefix: nil, companyId: nil, allowDuplicates: false)
    )
    try hub.startScan(command: command)
    precondition(backend.statuses == [.delivered, .delivered])
    precondition(sink.events == [
        .scanStarted(commandId: command.commandId, scanId: command.scanSessionId),
        .commandCompleted(commandId: command.commandId, error: nil),
    ])

    guard let retainedIngress = backend.ingress else {
        throw ArtifactConsumerError.ingressNotBound
    }
    try hub.dispose()
    precondition(backend.disposeCount == 1)
    precondition(retainedIngress.push(event: .scanStarted(
        commandId: command.commandId,
        scanId: command.scanSessionId
    )) == .closed)
    precondition(sink.events.count == 2)
    try verifyBackendResourceContract()
    print("apple_artifact_backend_resources=passed")
    print("apple_artifact_backend_resources_scope=macos-native-non-rf-not-iphone")
    print("apple_artifact_consumer=passed")
} catch {
    fatalError("artifact-only Apple consumer failed: \(error)")
}
