import BleHubUniFfi
import Foundation

// The live delegate is the one production AppleBleBackend. Deterministic tests
// replace precisely this BleBackend boundary, never the native library/image.
final class SystemLossWitnessBackend: BleBackend, @unchecked Sendable {
    private let delegate: any BleBackend
    private let witness: SystemLossWitness

    init(delegate: any BleBackend, witness: SystemLossWitness) {
        self.delegate = delegate
        self.witness = witness
    }

    func counts() -> (bind: Int, initialize: Int, dispose: Int) { witness.counts() }
    func capabilities() throws -> BackendCapabilities { try delegate.capabilities() }
    func bindEventIngress(ingress: BackendEventIngressHandle) throws {
        try witness.willBind()
        try delegate.bindEventIngress(ingress: ingress)
    }
    func initialize(command: InitializeCommand) throws {
        try witness.willInitialize()
        try delegate.initialize(command: command)
        witness.returned(command)
    }
    func writeCharacteristic(command: WriteCharacteristicCommand) throws {
        try delegate.writeCharacteristic(command: command)
        witness.returned(command)
    }
    func setNotifications(command: SetNotificationsCommand) throws {
        try delegate.setNotifications(command: command)
        witness.returned(command)
    }
    func startScan(command: StartScanCommand) throws { try delegate.startScan(command: command) }
    func stopScan(command: StopScanCommand) throws { try delegate.stopScan(command: command) }
    func connect(command: ConnectCommand) throws { try delegate.connect(command: command) }
    func disconnect(command: DisconnectCommand) throws { try delegate.disconnect(command: command) }
    func discoverGatt(command: DiscoverGattCommand) throws { try delegate.discoverGatt(command: command) }
    func readCharacteristic(command: ReadCharacteristicCommand) throws { try delegate.readCharacteristic(command: command) }
    func readRssi(command: ReadRssiCommand) throws { try delegate.readRssi(command: command) }
    func cancelOperation(request: CancelOperationRequest) throws { try delegate.cancelOperation(request: request) }
    func dispose() throws {
        guard witness.willDispose() else { return }
        try delegate.dispose()
    }
}
