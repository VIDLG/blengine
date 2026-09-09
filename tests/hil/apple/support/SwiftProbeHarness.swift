import BleHubUniFfi
import Foundation

struct SwiftProbeSnapshot: Sendable {
    let events: [BleEvent]
}

final class RecordingEventSink: BleEventSink, @unchecked Sendable {
    private let lock = NSLock()
    private var events: [BleEvent] = []

    func onEvent(event: BleEvent) throws {
        lock.lock()
        defer { lock.unlock() }
        events.append(event)
    }

    func snapshot() -> SwiftProbeSnapshot {
        lock.lock()
        defer { lock.unlock() }
        return SwiftProbeSnapshot(events: events)
    }
}

final class SwiftProbeHarness {
    private let sink: RecordingEventSink
    private let hub: BleHub

    init(backend: BleBackend) throws {
        let sink = RecordingEventSink()
        self.sink = sink
        hub = try BleHub(backend: backend, downstream: sink)
    }

    func snapshot() -> SwiftProbeSnapshot {
        sink.snapshot()
    }

    func initialize(command: InitializeCommand) throws {
        try hub.initialize(command: command)
    }

    func startScan(command: StartScanCommand) throws {
        try hub.startScan(command: command)
    }

    func stopScan(command: StopScanCommand) throws {
        try hub.stopScan(command: command)
    }

    func connect(command: ConnectCommand) throws {
        try hub.connect(command: command)
    }

    func disconnect(command: DisconnectCommand) throws {
        try hub.disconnect(command: command)
    }

    func discoverGatt(command: DiscoverGattCommand) throws {
        try hub.discoverGatt(command: command)
    }

    func readCharacteristic(command: ReadCharacteristicCommand) throws {
        try hub.readCharacteristic(command: command)
    }

    func writeCharacteristic(command: WriteCharacteristicCommand) throws {
        try hub.writeCharacteristic(command: command)
    }

    func setNotifications(command: SetNotificationsCommand) throws {
        try hub.setNotifications(command: command)
    }

    func readRssi(command: ReadRssiCommand) throws {
        try hub.readRssi(command: command)
    }

    func dispose() throws {
        try hub.dispose()
    }
}
