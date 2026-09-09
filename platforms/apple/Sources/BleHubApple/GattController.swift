import BleHubUniFfi
import CoreBluetooth
import Foundation

internal protocol AppleGattServiceObject: AnyObject {
    var appleGattUuid: BluetoothUuid { get }
    var appleGattIsPrimary: Bool { get }
}

internal protocol AppleGattCharacteristicObject: AnyObject {
    var appleGattUuid: BluetoothUuid { get }
    var appleGattProperties: CharacteristicProperties { get }
}

internal protocol AppleGattDescriptorObject: AnyObject {
    var appleGattUuid: BluetoothUuid { get }
}

extension CBService: AppleGattServiceObject {
    internal var appleGattUuid: BluetoothUuid {
        AppleAdvertisementMapper.canonicalUuid(uuid)
    }

    internal var appleGattIsPrimary: Bool {
        isPrimary
    }
}

extension CBCharacteristic: AppleGattCharacteristicObject {
    internal var appleGattUuid: BluetoothUuid {
        AppleAdvertisementMapper.canonicalUuid(uuid)
    }

    internal var appleGattProperties: CharacteristicProperties {
        AppleGattMapper.properties(properties)
    }
}

extension CBDescriptor: AppleGattDescriptorObject {
    internal var appleGattUuid: BluetoothUuid {
        AppleAdvertisementMapper.canonicalUuid(uuid)
    }
}

internal enum AppleGattMapper {
    static func properties(_ properties: CBCharacteristicProperties) -> CharacteristicProperties {
        CharacteristicProperties(
            readable: properties.contains(.read),
            writableWithResponse: properties.contains(.write),
            writableWithoutResponse: properties.contains(.writeWithoutResponse),
            notifiable: properties.contains(.notify),
            indicatable: properties.contains(.indicate)
        )
    }
}

internal struct AppleGattDiscoveryCorrelation: Equatable {
    let connection: AppleConnectionCorrelation
    let generation: UInt64
    let command: DiscoverGattCommand
}

internal struct AppleGattNativeMap {
    let connectionGeneration: UInt64
    let gattSessionId: GattSessionId
    let services: [GattServiceId: any AppleGattServiceObject]
    let characteristics: [GattCharacteristicId: any AppleGattCharacteristicObject]
    let descriptors: [GattDescriptorId: any AppleGattDescriptorObject]
}

internal struct AppleGattDiscoveryCompletion {
    let correlation: AppleGattDiscoveryCorrelation
    let database: GattDatabase
    let nativeMap: AppleGattNativeMap
}

internal struct AppleGattCharacteristicTarget {
    let connectionGeneration: UInt64
    let gattSessionId: GattSessionId
    let characteristicId: GattCharacteristicId
    let characteristic: any AppleGattCharacteristicObject
}

internal enum AppleGattDiscoveryAction {
    case discoverCharacteristics(any AppleGattServiceObject)
    case discoverDescriptors(any AppleGattCharacteristicObject)
    case completed(AppleGattDiscoveryCompletion)
}

internal enum AppleGattDiscoveryCollection: String, Equatable {
    case services
    case characteristics
    case descriptors
}

internal enum AppleGattControllerError: Error, Equatable {
    case attributeIdSpaceExhausted
    case discoveryLimitExceeded(
        collection: AppleGattDiscoveryCollection,
        maximum: Int,
        actual: Int
    )
}

internal final class AppleGattController {
    // FFI/allocation guards, not ATT handle, MTU, or platform discovery limits.
    internal static let maximumDiscoveredServices = 128
    internal static let maximumDiscoveredCharacteristics = 1_024
    internal static let maximumDiscoveredDescriptors = 2_048

    private enum DiscoveryStage: Equatable {
        case services
        case characteristics(serviceIndex: Int, serviceIdentity: ObjectIdentifier)
        case descriptors(
            serviceIndex: Int,
            characteristicIndex: Int,
            characteristicIdentity: ObjectIdentifier
        )
    }

    private struct DescriptorDraft {
        let id: GattDescriptorId
        let object: any AppleGattDescriptorObject
    }

    private struct CharacteristicDraft {
        let id: GattCharacteristicId
        let object: any AppleGattCharacteristicObject
        var descriptors: [DescriptorDraft]
    }

    private struct ServiceDraft {
        let id: GattServiceId
        let object: any AppleGattServiceObject
        var characteristics: [CharacteristicDraft]
    }

    private struct PendingDiscovery {
        let correlation: AppleGattDiscoveryCorrelation
        var stage: DiscoveryStage
        var services: [ServiceDraft]
        var characteristicCount: Int
        var descriptorCount: Int
    }

    private struct ActiveSession {
        let connectionGeneration: UInt64
        let database: GattDatabase
        let nativeMap: AppleGattNativeMap
    }

    private var pendingByConnectionId: [ConnectionId: PendingDiscovery] = [:]
    private var activeByConnectionId: [ConnectionId: ActiveSession] = [:]
    private var quarantinedGenerationByConnectionId: [ConnectionId: UInt64] = [:]
    private var nextDiscoveryGeneration: UInt64? = 1
    private var nextAttributeId: UInt64? = 1

    var pendingCount: Int {
        pendingByConnectionId.count
    }

    var activeCount: Int {
        activeByConnectionId.count
    }

    func begin(
        command: DiscoverGattCommand,
        connection: AppleConnectionCorrelation
    ) throws -> AppleGattDiscoveryCorrelation {
        guard command.connectionId == connection.connectionId else {
            throw CommandDispatchError.InvalidState(
                detail: "the Apple GATT command does not match the active connection"
            )
        }
        guard pendingByConnectionId[command.connectionId] == nil else {
            throw CommandDispatchError.InvalidState(
                detail: "an Apple GATT discovery is already active for this connection"
            )
        }
        guard quarantinedGenerationByConnectionId[command.connectionId] != connection.generation else {
            throw CommandDispatchError.InvalidState(
                detail: "Apple GATT discovery is unavailable until this connection is re-established"
            )
        }
        if let active = activeByConnectionId[command.connectionId],
           active.connectionGeneration == connection.generation,
           active.database.gattSessionId == command.gattSessionId
        {
            throw CommandDispatchError.InvalidRequest(
                detail: "the Apple GATT session ID is already active for this connection"
            )
        }
        guard let generation = allocateDiscoveryGeneration() else {
            throw CommandDispatchError.BackendUnavailable(
                detail: "Apple GATT discovery generation space is exhausted",
                platformDetails: nil
            )
        }

        let correlation = AppleGattDiscoveryCorrelation(
            connection: connection,
            generation: generation,
            command: command
        )
        pendingByConnectionId[command.connectionId] = PendingDiscovery(
            correlation: correlation,
            stage: .services,
            services: [],
            characteristicCount: 0,
            descriptorCount: 0
        )
        return correlation
    }

    func didDiscoverServices(
        connection: AppleConnectionCorrelation,
        services: [any AppleGattServiceObject]
    ) throws -> AppleGattDiscoveryAction? {
        guard var pending = currentPending(connection: connection),
              pending.stage == .services
        else {
            return nil
        }

        try enforceDiscoveryLimit(
            collection: .services,
            actual: services.count,
            maximum: Self.maximumDiscoveredServices
        )
        pending.services = try services.map {
            ServiceDraft(
                id: GattServiceId(value: try allocateAttributeId()),
                object: $0,
                characteristics: []
            )
        }
        pendingByConnectionId[connection.connectionId] = pending
        return advanceToFirstServiceOrComplete(connectionId: connection.connectionId)
    }

    func didDiscoverCharacteristics(
        connection: AppleConnectionCorrelation,
        service: any AppleGattServiceObject,
        characteristics: [any AppleGattCharacteristicObject]
    ) throws -> AppleGattDiscoveryAction? {
        guard var pending = currentPending(connection: connection),
              case .characteristics(let serviceIndex, let expectedIdentity) = pending.stage,
              expectedIdentity == ObjectIdentifier(service),
              pending.services.indices.contains(serviceIndex)
        else {
            return nil
        }

        let characteristicCount = try checkedAggregateCount(
            current: pending.characteristicCount,
            incoming: characteristics.count,
            collection: .characteristics,
            maximum: Self.maximumDiscoveredCharacteristics
        )
        pending.services[serviceIndex].characteristics = try characteristics.map {
            CharacteristicDraft(
                id: GattCharacteristicId(value: try allocateAttributeId()),
                object: $0,
                descriptors: []
            )
        }
        pending.characteristicCount = characteristicCount
        pendingByConnectionId[connection.connectionId] = pending
        return advanceAfterCharacteristics(
            connectionId: connection.connectionId,
            serviceIndex: serviceIndex
        )
    }

    func didDiscoverDescriptors(
        connection: AppleConnectionCorrelation,
        characteristic: any AppleGattCharacteristicObject,
        descriptors: [any AppleGattDescriptorObject]
    ) throws -> AppleGattDiscoveryAction? {
        guard var pending = currentPending(connection: connection),
              case .descriptors(
                  let serviceIndex,
                  let characteristicIndex,
                  let expectedIdentity
              ) = pending.stage,
              expectedIdentity == ObjectIdentifier(characteristic),
              pending.services.indices.contains(serviceIndex),
              pending.services[serviceIndex].characteristics.indices.contains(characteristicIndex)
        else {
            return nil
        }

        let descriptorCount = try checkedAggregateCount(
            current: pending.descriptorCount,
            incoming: descriptors.count,
            collection: .descriptors,
            maximum: Self.maximumDiscoveredDescriptors
        )
        pending.services[serviceIndex].characteristics[characteristicIndex].descriptors =
            try descriptors.map {
                DescriptorDraft(
                    id: GattDescriptorId(value: try allocateAttributeId()),
                    object: $0
                )
            }
        pending.descriptorCount = descriptorCount
        pendingByConnectionId[connection.connectionId] = pending
        return advanceAfterDescriptors(
            connectionId: connection.connectionId,
            serviceIndex: serviceIndex,
            characteristicIndex: characteristicIndex
        )
    }

    func failServices(connection: AppleConnectionCorrelation) -> DiscoverGattCommand? {
        fail(connection: connection, matching: { $0 == .services })
    }

    func failCharacteristics(
        connection: AppleConnectionCorrelation,
        service: any AppleGattServiceObject
    ) -> DiscoverGattCommand? {
        fail(connection: connection) { stage in
            guard case .characteristics(_, let expectedIdentity) = stage else {
                return false
            }
            return expectedIdentity == ObjectIdentifier(service)
        }
    }

    func failDescriptors(
        connection: AppleConnectionCorrelation,
        characteristic: any AppleGattCharacteristicObject
    ) -> DiscoverGattCommand? {
        fail(connection: connection) { stage in
            guard case .descriptors(_, _, let expectedIdentity) = stage else {
                return false
            }
            return expectedIdentity == ObjectIdentifier(characteristic)
        }
    }

    func failCurrent(connection: AppleConnectionCorrelation) -> DiscoverGattCommand? {
        guard let pending = currentPending(connection: connection) else {
            return nil
        }
        pendingByConnectionId.removeValue(forKey: connection.connectionId)
        return pending.correlation.command
    }

    func invalidateServices(connection: AppleConnectionCorrelation) -> DiscoverGattCommand? {
        let command = failCurrent(connection: connection)
        if command != nil {
            quarantinedGenerationByConnectionId[connection.connectionId] = connection.generation
        }
        removeActive(connection: connection)
        return command
    }

    func retire(connection: AppleConnectionCorrelation) -> DiscoverGattCommand? {
        let command = failCurrent(connection: connection)
        removeActive(connection: connection)
        if quarantinedGenerationByConnectionId[connection.connectionId] == connection.generation {
            quarantinedGenerationByConnectionId.removeValue(forKey: connection.connectionId)
        }
        return command
    }

    func drain() -> [DiscoverGattCommand] {
        let commands = pendingByConnectionId.values
            .sorted { $0.correlation.generation < $1.correlation.generation }
            .map(\.correlation.command)
        pendingByConnectionId.removeAll()
        activeByConnectionId.removeAll()
        quarantinedGenerationByConnectionId.removeAll()
        return commands
    }

    func activeDatabase(
        connection: AppleConnectionCorrelation
    ) -> GattDatabase? {
        guard let active = activeByConnectionId[connection.connectionId],
              active.connectionGeneration == connection.generation
        else {
            return nil
        }
        return active.database
    }

    func hasPendingDiscovery(connection: AppleConnectionCorrelation) -> Bool {
        currentPending(connection: connection) != nil
    }

    func resolveCharacteristic(
        connection: AppleConnectionCorrelation,
        gattSessionId: GattSessionId,
        characteristicId: GattCharacteristicId
    ) throws -> AppleGattCharacteristicTarget {
        guard !hasPendingDiscovery(connection: connection) else {
            throw CommandDispatchError.InvalidState(
                detail: "GATT discovery is pending for this Apple connection"
            )
        }
        guard let active = activeByConnectionId[connection.connectionId],
              active.connectionGeneration == connection.generation
        else {
            throw CommandDispatchError.InvalidState(
                detail: "no discovered GATT database is active for this Apple connection"
            )
        }
        guard active.database.gattSessionId == gattSessionId else {
            throw CommandDispatchError.InvalidState(
                detail: "the Apple GATT session ID is stale or does not match the active database"
            )
        }
        guard active.nativeMap.connectionGeneration == active.connectionGeneration,
              active.nativeMap.gattSessionId == active.database.gattSessionId
        else {
            throw CommandDispatchError.BackendUnavailable(
                detail: "Apple GATT database and native routing map are inconsistent",
                platformDetails: nil
            )
        }

        let databaseContainsCharacteristic = active.database.services.contains { service in
            service.characteristics.contains { $0.id == characteristicId }
        }
        let characteristic = active.nativeMap.characteristics[characteristicId]
        guard databaseContainsCharacteristic == (characteristic != nil) else {
            throw CommandDispatchError.BackendUnavailable(
                detail: "Apple GATT characteristic database and native routing map are inconsistent",
                platformDetails: nil
            )
        }
        guard let characteristic else {
            throw CommandDispatchError.InvalidRequest(
                detail: "the GATT characteristic ID is not present in the active Apple database"
            )
        }
        return AppleGattCharacteristicTarget(
            connectionGeneration: active.connectionGeneration,
            gattSessionId: active.database.gattSessionId,
            characteristicId: characteristicId,
            characteristic: characteristic
        )
    }

    private func enforceDiscoveryLimit(
        collection: AppleGattDiscoveryCollection,
        actual: Int,
        maximum: Int
    ) throws {
        guard actual <= maximum else {
            throw AppleGattControllerError.discoveryLimitExceeded(
                collection: collection,
                maximum: maximum,
                actual: actual
            )
        }
    }

    private func checkedAggregateCount(
        current: Int,
        incoming: Int,
        collection: AppleGattDiscoveryCollection,
        maximum: Int
    ) throws -> Int {
        guard current <= maximum, incoming <= maximum - current else {
            let (actual, overflow) = current.addingReportingOverflow(incoming)
            throw AppleGattControllerError.discoveryLimitExceeded(
                collection: collection,
                maximum: maximum,
                actual: overflow ? Int.max : actual
            )
        }
        return current + incoming
    }

    private func currentPending(
        connection: AppleConnectionCorrelation
    ) -> PendingDiscovery? {
        guard let pending = pendingByConnectionId[connection.connectionId],
              pending.correlation.connection.generation == connection.generation,
              ObjectIdentifier(pending.correlation.connection.peripheral)
                  == ObjectIdentifier(connection.peripheral)
        else {
            return nil
        }
        return pending
    }

    private func removeActive(connection: AppleConnectionCorrelation) {
        if let active = activeByConnectionId[connection.connectionId],
           active.connectionGeneration == connection.generation
        {
            activeByConnectionId.removeValue(forKey: connection.connectionId)
        }
    }

    private func fail(
        connection: AppleConnectionCorrelation,
        matching predicate: (DiscoveryStage) -> Bool
    ) -> DiscoverGattCommand? {
        guard let pending = currentPending(connection: connection),
              predicate(pending.stage)
        else {
            return nil
        }
        pendingByConnectionId.removeValue(forKey: connection.connectionId)
        return pending.correlation.command
    }

    private func advanceToFirstServiceOrComplete(
        connectionId: ConnectionId
    ) -> AppleGattDiscoveryAction? {
        guard var pending = pendingByConnectionId[connectionId] else {
            return nil
        }
        guard let service = pending.services.first else {
            return complete(connectionId: connectionId)
        }
        pending.stage = .characteristics(
            serviceIndex: 0,
            serviceIdentity: ObjectIdentifier(service.object)
        )
        pendingByConnectionId[connectionId] = pending
        return .discoverCharacteristics(service.object)
    }

    private func advanceAfterCharacteristics(
        connectionId: ConnectionId,
        serviceIndex: Int
    ) -> AppleGattDiscoveryAction? {
        guard var pending = pendingByConnectionId[connectionId] else {
            return nil
        }
        if let characteristic = pending.services[serviceIndex].characteristics.first {
            pending.stage = .descriptors(
                serviceIndex: serviceIndex,
                characteristicIndex: 0,
                characteristicIdentity: ObjectIdentifier(characteristic.object)
            )
            pendingByConnectionId[connectionId] = pending
            return .discoverDescriptors(characteristic.object)
        }
        return advanceToNextServiceOrComplete(
            pending: pending,
            completedServiceIndex: serviceIndex
        )
    }

    private func advanceAfterDescriptors(
        connectionId: ConnectionId,
        serviceIndex: Int,
        characteristicIndex: Int
    ) -> AppleGattDiscoveryAction? {
        guard var pending = pendingByConnectionId[connectionId] else {
            return nil
        }
        let nextCharacteristicIndex = characteristicIndex + 1
        let characteristics = pending.services[serviceIndex].characteristics
        if characteristics.indices.contains(nextCharacteristicIndex) {
            let characteristic = characteristics[nextCharacteristicIndex]
            pending.stage = .descriptors(
                serviceIndex: serviceIndex,
                characteristicIndex: nextCharacteristicIndex,
                characteristicIdentity: ObjectIdentifier(characteristic.object)
            )
            pendingByConnectionId[connectionId] = pending
            return .discoverDescriptors(characteristic.object)
        }
        return advanceToNextServiceOrComplete(
            pending: pending,
            completedServiceIndex: serviceIndex
        )
    }

    private func advanceToNextServiceOrComplete(
        pending: PendingDiscovery,
        completedServiceIndex: Int
    ) -> AppleGattDiscoveryAction? {
        var pending = pending
        let nextServiceIndex = completedServiceIndex + 1
        guard pending.services.indices.contains(nextServiceIndex) else {
            return complete(connectionId: pending.correlation.command.connectionId)
        }
        let service = pending.services[nextServiceIndex]
        pending.stage = .characteristics(
            serviceIndex: nextServiceIndex,
            serviceIdentity: ObjectIdentifier(service.object)
        )
        pendingByConnectionId[pending.correlation.command.connectionId] = pending
        return .discoverCharacteristics(service.object)
    }

    private func complete(connectionId: ConnectionId) -> AppleGattDiscoveryAction? {
        guard let pending = pendingByConnectionId.removeValue(forKey: connectionId) else {
            return nil
        }

        let database = GattDatabase(
            connectionId: pending.correlation.command.connectionId,
            gattSessionId: pending.correlation.command.gattSessionId,
            services: pending.services.map { service in
                GattService(
                    id: service.id,
                    uuid: service.object.appleGattUuid,
                    primary: service.object.appleGattIsPrimary,
                    characteristics: service.characteristics.map { characteristic in
                        GattCharacteristic(
                            id: characteristic.id,
                            uuid: characteristic.object.appleGattUuid,
                            properties: characteristic.object.appleGattProperties,
                            descriptors: characteristic.descriptors.map { descriptor in
                                GattDescriptor(
                                    id: descriptor.id,
                                    uuid: descriptor.object.appleGattUuid
                                )
                            }
                        )
                    }
                )
            }
        )
        let nativeMap = AppleGattNativeMap(
            connectionGeneration: pending.correlation.connection.generation,
            gattSessionId: pending.correlation.command.gattSessionId,
            services: Dictionary(
                uniqueKeysWithValues: pending.services.map { ($0.id, $0.object) }
            ),
            characteristics: Dictionary(
                uniqueKeysWithValues: pending.services.flatMap { service in
                    service.characteristics.map { ($0.id, $0.object) }
                }
            ),
            descriptors: Dictionary(
                uniqueKeysWithValues: pending.services.flatMap { service in
                    service.characteristics.flatMap { characteristic in
                        characteristic.descriptors.map { ($0.id, $0.object) }
                    }
                }
            )
        )
        let active = ActiveSession(
            connectionGeneration: pending.correlation.connection.generation,
            database: database,
            nativeMap: nativeMap
        )
        activeByConnectionId[connectionId] = active
        return .completed(
            AppleGattDiscoveryCompletion(
                correlation: pending.correlation,
                database: database,
                nativeMap: nativeMap
            )
        )
    }

    private func allocateDiscoveryGeneration() -> UInt64? {
        guard let value = nextDiscoveryGeneration else {
            return nil
        }
        nextDiscoveryGeneration = value == UInt64.max ? nil : value + 1
        return value
    }

    private func allocateAttributeId() throws -> UInt64 {
        guard let value = nextAttributeId else {
            throw AppleGattControllerError.attributeIdSpaceExhausted
        }
        nextAttributeId = value == UInt64.max ? nil : value + 1
        return value
    }
}
