import BleHubUniFfi
import Foundation

internal protocol ApplePeripheralObject: AnyObject {
    var identifier: UUID { get }
}

internal struct AppleConnectionCorrelation: Equatable {
    let generation: UInt64
    let connectCommandId: CommandId
    let connectionId: ConnectionId
    let peripheralId: PeripheralId
    let peripheral: any ApplePeripheralObject

    var peripheralIdentifier: UUID {
        peripheral.identifier
    }

    static func == (lhs: Self, rhs: Self) -> Bool {
        lhs.generation == rhs.generation &&
            lhs.connectCommandId == rhs.connectCommandId &&
            lhs.connectionId == rhs.connectionId &&
            lhs.peripheralId == rhs.peripheralId &&
            ObjectIdentifier(lhs.peripheral) == ObjectIdentifier(rhs.peripheral)
    }
}

internal enum AppleConnectionPhase: Equatable {
    case connecting
    case connected
    case disconnecting(commandId: CommandId, pendingConnect: Bool)
    case cancellingAfterBackendFailure
}

internal struct AppleConnectionRoute: Equatable {
    let correlation: AppleConnectionCorrelation
    var phase: AppleConnectionPhase
}

internal enum AppleDidConnectDisposition: Equatable {
    case connected(AppleConnectionCorrelation)
    case cancelAgain(AppleConnectionCorrelation)
}

internal final class AppleConnectionController {
    private var routesByGeneration: [UInt64: AppleConnectionRoute] = [:]
    private var generationsByConnectionId: [ConnectionId: UInt64] = [:]
    private var generationsByPeripheralId: [PeripheralId: UInt64] = [:]
    private var generationsByPeripheralIdentifier: [UUID: UInt64] = [:]
    private var nextGeneration: UInt64 = 1

    var activeCount: Int {
        routesByGeneration.count
    }

    func begin(
        command: ConnectCommand,
        peripheral: any ApplePeripheralObject
    ) throws -> AppleConnectionCorrelation {
        let peripheralIdentifier = peripheral.identifier
        guard generationsByPeripheralId[command.peripheralId] == nil,
              generationsByPeripheralIdentifier[peripheralIdentifier] == nil
        else {
            throw CommandDispatchError.InvalidState(
                detail: "the peripheral already has an active connection lifecycle"
            )
        }
        guard generationsByConnectionId[command.connectionId] == nil else {
            throw CommandDispatchError.InvalidRequest(
                detail: "the connection ID is already in use by this Apple backend"
            )
        }
        guard nextGeneration != 0 else {
            throw CommandDispatchError.BackendUnavailable(
                detail: "Apple connection generation space is exhausted",
                platformDetails: nil
            )
        }

        let correlation = AppleConnectionCorrelation(
            generation: nextGeneration,
            connectCommandId: command.commandId,
            connectionId: command.connectionId,
            peripheralId: command.peripheralId,
            peripheral: peripheral
        )
        nextGeneration = nextGeneration == UInt64.max ? 0 : nextGeneration + 1
        let route = AppleConnectionRoute(correlation: correlation, phase: .connecting)
        routesByGeneration[correlation.generation] = route
        generationsByConnectionId[correlation.connectionId] = correlation.generation
        generationsByPeripheralId[correlation.peripheralId] = correlation.generation
        generationsByPeripheralIdentifier[correlation.peripheralIdentifier] = correlation.generation
        return correlation
    }

    func peripheralId(for connectionId: ConnectionId) -> PeripheralId? {
        route(for: connectionId)?.correlation.peripheralId
    }

    func connectedCorrelation(
        for connectionId: ConnectionId
    ) -> AppleConnectionCorrelation? {
        guard let route = route(for: connectionId), route.phase == .connected else {
            return nil
        }
        return route.correlation
    }

    func connectedCorrelation(
        for peripheral: any ApplePeripheralObject
    ) -> AppleConnectionCorrelation? {
        guard let route = route(for: peripheral), route.phase == .connected else {
            return nil
        }
        return route.correlation
    }

    func beginFailClosedDisconnect(
        connection: AppleConnectionCorrelation
    ) -> Bool {
        guard var route = routesByGeneration[connection.generation],
              route.correlation == connection,
              route.phase == .connected
        else {
            return false
        }
        route.phase = .cancellingAfterBackendFailure
        routesByGeneration[connection.generation] = route
        return true
    }

    func beginDisconnect(command: DisconnectCommand) throws -> AppleConnectionCorrelation {
        guard let generation = generationsByConnectionId[command.connectionId],
              var route = routesByGeneration[generation]
        else {
            throw CommandDispatchError.InvalidState(
                detail: "connection lifecycle is not active"
            )
        }
        let pendingConnect: Bool
        switch route.phase {
        case .connecting:
            pendingConnect = true
        case .connected:
            pendingConnect = false
        case .disconnecting, .cancellingAfterBackendFailure:
            throw CommandDispatchError.InvalidState(
                detail: "connection lifecycle is not active"
            )
        }
        route.phase = .disconnecting(
            commandId: command.commandId,
            pendingConnect: pendingConnect
        )
        routesByGeneration[generation] = route
        return route.correlation
    }

    func didConnect(
        peripheral: any ApplePeripheralObject
    ) -> AppleDidConnectDisposition? {
        guard var route = route(for: peripheral) else {
            return nil
        }
        let generation = route.correlation.generation
        switch route.phase {
        case .connecting:
            route.phase = .connected
            routesByGeneration[generation] = route
            return .connected(route.correlation)
        case .disconnecting, .cancellingAfterBackendFailure:
            return .cancelAgain(route.correlation)
        case .connected:
            return nil
        }
    }

    func didFailToConnect(
        peripheral: any ApplePeripheralObject
    ) -> AppleConnectionRoute? {
        guard let route = route(for: peripheral) else {
            return nil
        }
        switch route.phase {
        case .connecting, .disconnecting(_, pendingConnect: true):
            remove(route)
            return route
        case .connected, .disconnecting(_, pendingConnect: false),
             .cancellingAfterBackendFailure:
            return nil
        }
    }

    func didDisconnect(
        peripheral: any ApplePeripheralObject
    ) -> AppleConnectionRoute? {
        guard let route = route(for: peripheral) else {
            return nil
        }
        remove(route)
        return route
    }

    func drain() -> [AppleConnectionRoute] {
        let routes = routesByGeneration.values.sorted {
            $0.correlation.generation < $1.correlation.generation
        }
        routesByGeneration.removeAll()
        generationsByConnectionId.removeAll()
        generationsByPeripheralId.removeAll()
        generationsByPeripheralIdentifier.removeAll()
        return routes
    }

    private func route(for connectionId: ConnectionId) -> AppleConnectionRoute? {
        guard let generation = generationsByConnectionId[connectionId] else {
            return nil
        }
        return routesByGeneration[generation]
    }

    private func route(
        for peripheral: any ApplePeripheralObject
    ) -> AppleConnectionRoute? {
        guard let generation = generationsByPeripheralIdentifier[peripheral.identifier],
              let route = routesByGeneration[generation],
              ObjectIdentifier(route.correlation.peripheral) == ObjectIdentifier(peripheral)
        else {
            return nil
        }
        return route
    }

    private func remove(_ route: AppleConnectionRoute) {
        let correlation = route.correlation
        routesByGeneration.removeValue(forKey: correlation.generation)
        generationsByConnectionId.removeValue(forKey: correlation.connectionId)
        generationsByPeripheralId.removeValue(forKey: correlation.peripheralId)
        generationsByPeripheralIdentifier.removeValue(forKey: correlation.peripheralIdentifier)
    }
}
