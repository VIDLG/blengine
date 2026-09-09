import BleHubUniFfi

final class NoopBackend: BleBackend {
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

    func bindEventIngress(ingress: BackendEventIngressHandle) throws {}
    func initialize(command: InitializeCommand) throws {}
    func startScan(command: StartScanCommand) throws {}
    func stopScan(command: StopScanCommand) throws {}
    func connect(command: ConnectCommand) throws {}
    func disconnect(command: DisconnectCommand) throws {}
    func discoverGatt(command: DiscoverGattCommand) throws {}
    func readCharacteristic(command: ReadCharacteristicCommand) throws {}
    func writeCharacteristic(command: WriteCharacteristicCommand) throws {}
    func setNotifications(command: SetNotificationsCommand) throws {}
    func readRssi(command: ReadRssiCommand) throws {}
    func dispose() throws {}
}

final class NoopSink: BleEventSink {
    func onEvent(event: BleEvent) throws {}
}

do {
    let hub = try BleHub(backend: NoopBackend(), downstream: NoopSink())
    _ = hub.capabilities()
    try hub.dispose()
    print("apple_artifact_consumer=passed")
} catch {
    fatalError("artifact-only Apple consumer failed: \(error)")
}
