import BleHubUniFfi
import CoreBluetooth
import Foundation

internal enum AppleScanStopDisposition: Equatable {
    case stopped(ScanSessionId)
    case alreadyStopped
}

internal final class AppleScanController {
    private struct Session {
        let id: ScanSessionId
        let options: ScanOptions
    }

    private var session: Session?
    private var lastStoppedSessionId: ScanSessionId?
    private var peripheralIdsByIdentifier: [UUID: PeripheralId] = [:]
    private var peripheralsById: [PeripheralId: CBPeripheral] = [:]
    private var nextPeripheralId: UInt64?

    init(initialPeripheralId: UInt64 = 0) {
        nextPeripheralId = initialPeripheralId
    }

    var activeSessionId: ScanSessionId? {
        session?.id
    }

    func begin(command: StartScanCommand) throws -> [CBUUID]? {
        guard session == nil else {
            throw CommandDispatchError.InvalidState(
                detail: "an Apple scan is already active"
            )
        }
        session = Session(id: command.scanSessionId, options: command.options)
        let services = command.options.serviceUuids.map { CBUUID(string: $0.value) }
        return services.isEmpty ? nil : services
    }

    func stop(command: StopScanCommand) throws -> AppleScanStopDisposition {
        guard let session else {
            if lastStoppedSessionId == command.scanSessionId {
                return .alreadyStopped
            }
            throw CommandDispatchError.InvalidState(
                detail: "the requested Apple scan session is not active"
            )
        }
        guard session.id == command.scanSessionId else {
            throw CommandDispatchError.InvalidState(
                detail: "the requested Apple scan session is not active"
            )
        }
        self.session = nil
        lastStoppedSessionId = session.id
        return .stopped(session.id)
    }

    func stopForUnavailableAdapter() -> ScanSessionId? {
        guard let session else {
            return nil
        }
        self.session = nil
        lastStoppedSessionId = session.id
        return session.id
    }

    func observation(
        peripheral: CBPeripheral,
        advertisementData: [String: Any],
        rssi: NSNumber
    ) -> AdvertisementObservation? {
        observation(
            peripheralIdentifier: peripheral.identifier,
            advertisementData: advertisementData,
            rssi: rssi
        ) { [self] peripheralId in
            peripheralsById[peripheralId] = peripheral
        }
    }

    func observation(
        peripheralIdentifier: UUID,
        advertisementData: [String: Any],
        rssi: NSNumber,
        retainPeripheral: (PeripheralId) -> Void
    ) -> AdvertisementObservation? {
        AppleAdvertisementDiagnostics.record(
            peripheralIdentifier: peripheralIdentifier,
            advertisementData: advertisementData,
            rssi: rssi
        )
        guard let session,
              session.options.matchesIdentityFreeFields(advertisementData)
        else {
            return nil
        }
        guard let peripheralId = peripheralId(for: peripheralIdentifier) else {
            return nil
        }
        let observation = AppleAdvertisementMapper.observation(
            scanSessionId: session.id,
            peripheralId: peripheralId,
            advertisementData: advertisementData,
            rssi: rssi
        )
        // CoreBluetooth only guarantees that a callback passed the native
        // service filter; it does not identify which filter matched. Keep
        // that evidence separate from the advertisement payload we expose.
        guard session.options.matches(
            observation,
            nativeServiceFilterMatched: !session.options.serviceUuids.isEmpty
        ) else {
            return nil
        }
        retainPeripheral(peripheralId)
        return observation
    }

    func peripheral(for id: PeripheralId) -> CBPeripheral? {
        peripheralsById[id]
    }

    /// Retains OS-restored peripherals under this backend's identity index.
    ///
    /// A restored peer is already connected, so CoreBluetooth will not advertise it again; retaining
    /// the restored `CBPeripheral` lets an explicit adoption re-use that native link instead of
    /// waiting for an advertisement that can never arrive. This seeds identity only: it does not
    /// create a JS capability, and it does not claim a connection the engine has not adopted.
    func adoptRestored(_ peripherals: [CBPeripheral]) -> Int {
        var adopted = 0
        for peripheral in peripherals {
            guard let id = peripheralId(for: peripheral.identifier) else { continue }
            peripheralsById[id] = peripheral
            adopted += 1
        }
        return adopted
    }

    /// Read-only inventory of system-connected peripherals advertising `services`.
    ///
    /// Keys come from the same identity index as scan observations, so the same peer's key matches.
    /// The native peer is retained under that key so an explicit `adoptPeripheral(key)` can connect
    /// it; the inventory record itself is still data, not a capability.
    func connectedPeripheralRecords(
        _ central: CBCentralManager,
        services: [BluetoothUuid]
    ) -> [DeviceRecord] {
        let filters = services.map { CBUUID(string: $0.value) }
        return central.retrieveConnectedPeripherals(withServices: filters).compactMap { peripheral in
            guard let id = peripheralId(for: peripheral.identifier) else { return nil }
            peripheralsById[id] = peripheral
            return DeviceRecord(
                key: String(id.value),
                localName: peripheral.name,
                connected: true,
                bonded: false
            )
        }
    }

    func existingPeripheralId(for identifier: UUID) -> PeripheralId? {
        peripheralIdsByIdentifier[identifier]
    }

    func peripheralId(for identifier: UUID) -> PeripheralId? {
        if let existing = existingPeripheralId(for: identifier) {
            return existing
        }
        guard let nextPeripheralId else {
            return nil
        }
        let allocated = PeripheralId(value: nextPeripheralId)
        peripheralIdsByIdentifier[identifier] = allocated
        self.nextPeripheralId = nextPeripheralId == UInt64.max ? nil : nextPeripheralId + 1
        return allocated
    }

    // Count the union of identity allocations and retained native peers, not two indexes.
    var diagnosticPeerCacheCount: Int {
        Set(peripheralIdsByIdentifier.values).union(peripheralsById.keys).count
    }

    func dispose() {
        session = nil
        lastStoppedSessionId = nil
        peripheralIdsByIdentifier.removeAll()
        peripheralsById.removeAll()
    }
}

@_spi(AppleDiagnostics) public enum AppleAdvertisementDiagnostics {
    private struct Summary {
        let fields: String
        let firstMilliseconds: Int
        var lastMilliseconds: Int
        var count: Int
        var minimumRssi: Int
        var maximumRssi: Int

        var rendered: String {
            "\(fields) firstMs=\(firstMilliseconds) lastMs=\(lastMilliseconds) " +
                "count=\(count) rssiMin=\(minimumRssi) rssiMax=\(maximumRssi)"
        }
    }

    private static let lock = NSLock()
    private static var captureStartedAt = ProcessInfo.processInfo.systemUptime
    private static var summaryKeys: [String] = []
    private static var summaries: [String: Summary] = [:]
    private static let limit = 32

    static var enabled: Bool {
        CommandLine.arguments.contains("--rf-dump-advertisements")
    }

    static func record(
        peripheralIdentifier: UUID,
        advertisementData: [String: Any],
        rssi: NSNumber
    ) {
        guard enabled else { return }
        let services = uuids(advertisementData[CBAdvertisementDataServiceUUIDsKey])
        let overflow = uuids(advertisementData[CBAdvertisementDataOverflowServiceUUIDsKey])
        let localName = advertisementData[CBAdvertisementDataLocalNameKey] as? String ?? "absent"
        let manufacturer = advertisementData[CBAdvertisementDataManufacturerDataKey] as? Data
        let serviceData = advertisementData[CBAdvertisementDataServiceDataKey] as? [CBUUID: Data]
        let fields = [
            "keys=\(advertisementData.keys.sorted().joined(separator: ","))",
            "services=\(services.joined(separator: ","))",
            "overflow=\(overflow.joined(separator: ","))",
            "localName=\(localName)",
            "manufacturer=\(dataSummary(manufacturer))",
            "serviceData=\(serviceDataSummary(serviceData))",
            "connectable=\((advertisementData[CBAdvertisementDataIsConnectable] as? NSNumber)?.stringValue ?? "absent")",
        ].joined(separator: " ")
        let identifier = peripheralIdentifier.uuidString.lowercased()
        let deduplicationKey = "identifier=\(identifier) \(fields)"
        let rssiValue = rssi.intValue
        lock.lock()
        defer { lock.unlock() }
        let elapsedMilliseconds = Int(
            (ProcessInfo.processInfo.systemUptime - captureStartedAt) * 1_000
        )
        if var existing = summaries[deduplicationKey] {
            existing.lastMilliseconds = elapsedMilliseconds
            existing.count += 1
            existing.minimumRssi = min(existing.minimumRssi, rssiValue)
            existing.maximumRssi = max(existing.maximumRssi, rssiValue)
            summaries[deduplicationKey] = existing
            return
        }
        guard summaryKeys.count < limit else { return }
        summaryKeys.append(deduplicationKey)
        summaries[deduplicationKey] = Summary(
            fields: deduplicationKey,
            firstMilliseconds: elapsedMilliseconds,
            lastMilliseconds: elapsedMilliseconds,
            count: 1,
            minimumRssi: rssiValue,
            maximumRssi: rssiValue
        )
    }

    public static func reset() {
        lock.lock()
        defer { lock.unlock() }
        captureStartedAt = ProcessInfo.processInfo.systemUptime
        summaryKeys.removeAll()
        summaries.removeAll()
    }

    public static func snapshot() -> [String] {
        lock.lock()
        defer { lock.unlock() }
        return summaryKeys.compactMap { summaries[$0]?.rendered }
    }

    private static func uuids(_ value: Any?) -> [String] {
        (value as? [CBUUID] ?? []).map { $0.uuidString.lowercased() }
    }

    private static func dataSummary(_ data: Data?) -> String {
        guard let data else { return "absent" }
        return "length=\(data.count) hex=\(data.prefix(16).map { String(format: "%02x", $0) }.joined())"
    }

    private static func serviceDataSummary(_ data: [CBUUID: Data]?) -> String {
        guard let data, !data.isEmpty else { return "absent" }
        return data
            .sorted { $0.key.uuidString < $1.key.uuidString }
            .map { "\($0.key.uuidString.lowercased()):\(dataSummary($0.value))" }
            .joined(separator: ",")
    }
}

internal enum AppleAdvertisementMapper {
    static func observation(
        scanSessionId: ScanSessionId,
        peripheralId: PeripheralId,
        advertisementData: [String: Any],
        rssi: NSNumber
    ) -> AdvertisementObservation {
        let normalServices = advertisementData[CBAdvertisementDataServiceUUIDsKey]
            as? [CBUUID] ?? []
        let overflowServices = advertisementData[CBAdvertisementDataOverflowServiceUUIDsKey]
            as? [CBUUID] ?? []
        let advertisedServices = canonicalUuids(normalServices + overflowServices)

        let manufacturerData = manufacturerData(advertisementData)

        let serviceData = (
            advertisementData[CBAdvertisementDataServiceDataKey] as? [CBUUID: Data] ?? [:]
        )
        .map {
            ServiceData(serviceUuid: canonicalUuid($0.key), payload: $0.value)
        }
        .sorted { $0.serviceUuid.value < $1.serviceUuid.value }

        let rssiValue = Int16(exactly: rssi.intValue)
        let connectable = (
            advertisementData[CBAdvertisementDataIsConnectable] as? NSNumber
        )?.boolValue

        return AdvertisementObservation(
            scanSessionId: scanSessionId,
            peripheralId: peripheralId,
            localName: advertisementData[CBAdvertisementDataLocalNameKey] as? String,
            advertisedServices: advertisedServices,
            manufacturerData: manufacturerData,
            serviceData: serviceData,
            rssi: rssiValue,
            connectable: connectable
        )
    }

    static func manufacturerData(
        _ advertisementData: [String: Any]
    ) -> [ManufacturerData] {
        guard let raw = advertisementData[CBAdvertisementDataManufacturerDataKey] as? Data,
              raw.count >= 2
        else {
            return []
        }
        let bytes = [UInt8](raw)
        return [
            ManufacturerData(
                companyId: UInt16(bytes[0]) | UInt16(bytes[1]) << 8,
                payload: Data(bytes.dropFirst(2))
            ),
        ]
    }

    static func canonicalUuid(_ uuid: CBUUID) -> BluetoothUuid {
        let bytes = [UInt8](uuid.data)
        let value: String
        switch bytes.count {
        case 2:
            value = String(
                format: "0000%02x%02x-0000-1000-8000-00805f9b34fb",
                bytes[0], bytes[1]
            )
        case 4:
            value = String(
                format: "%02x%02x%02x%02x-0000-1000-8000-00805f9b34fb",
                bytes[0], bytes[1], bytes[2], bytes[3]
            )
        case 16:
            let hex = bytes.map { String(format: "%02x", $0) }.joined()
            value = "\(hex.prefix(8))-\(hex.dropFirst(8).prefix(4))-" +
                "\(hex.dropFirst(12).prefix(4))-\(hex.dropFirst(16).prefix(4))-" +
                "\(hex.dropFirst(20))"
        default:
            value = uuid.uuidString.lowercased()
        }
        return BluetoothUuid(value: value)
    }

    private static func canonicalUuids(_ uuids: [CBUUID]) -> [BluetoothUuid] {
        var seen: Set<String> = []
        return uuids.compactMap {
            let mapped = canonicalUuid($0)
            return seen.insert(mapped.value).inserted ? mapped : nil
        }
    }
}

internal extension ScanOptions {
    func matchesIdentityFreeFields(_ advertisementData: [String: Any]) -> Bool {
        if let namePrefix,
           (advertisementData[CBAdvertisementDataLocalNameKey] as? String)?
           .hasPrefix(namePrefix) != true
        {
            return false
        }
        if let exactName,
           (advertisementData[CBAdvertisementDataLocalNameKey] as? String) != exactName
        {
            return false
        }
        if let companyId,
           !AppleAdvertisementMapper.manufacturerData(advertisementData)
           .contains(where: { $0.companyId == companyId })
        {
            return false
        }
        if let filter = manufacturerData,
           !Self.matchesManufacturer(filter, AppleAdvertisementMapper.manufacturerData(advertisementData))
        {
            return false
        }
        return true
    }

    /// One manufacturer predicate over parsed advertisement entries: a matching company id whose
    /// payload starts with `filter.data`, or matches it byte-for-byte under `filter.mask`.
    static func matchesManufacturer(
        _ filter: ManufacturerDataFilter,
        _ entries: [ManufacturerData]
    ) -> Bool {
        let expected = [UInt8](filter.data)
        return entries.contains { entry in
            guard entry.companyId == filter.companyId else { return false }
            let payload = [UInt8](entry.payload)
            guard payload.count >= expected.count else { return false }
            if let mask = filter.mask {
                let maskBytes = [UInt8](mask)
                guard maskBytes.count == expected.count else { return false }
                return (0..<expected.count).allSatisfy { index in
                    (payload[index] & maskBytes[index]) == (expected[index] & maskBytes[index])
                }
            }
            return payload.starts(with: expected)
        }
    }

    func matches(
        _ observation: AdvertisementObservation,
        nativeServiceFilterMatched: Bool = false
    ) -> Bool {
        if !serviceUuids.isEmpty,
           !nativeServiceFilterMatched,
           !serviceUuids.contains(where: observation.advertisedServices.contains)
        {
            return false
        }
        if let namePrefix,
           observation.localName?.hasPrefix(namePrefix) != true
        {
            return false
        }
        if let exactName,
           observation.localName != exactName
        {
            return false
        }
        if let companyId,
           !observation.manufacturerData.contains(where: { $0.companyId == companyId })
        {
            return false
        }
        if let filter = self.manufacturerData,
           !Self.matchesManufacturer(filter, observation.manufacturerData)
        {
            return false
        }
        if let minRssi = self.minRssi,
           observation.rssi.map({ $0 >= minRssi }) != true
        {
            return false
        }
        return true
    }
}
