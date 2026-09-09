import BleHubUniFfi

internal enum AppleConnectionEventComposer {
    static func connectFailure(
        route: AppleConnectionRoute,
        error: BleEventError
    ) -> [BleEvent] {
        var events: [BleEvent] = [
            .connectionStateChanged(
                commandId: route.correlation.connectCommandId,
                connectionId: route.correlation.connectionId,
                result: .err(error: error)
            ),
            .commandCompleted(
                commandId: route.correlation.connectCommandId,
                error: error
            ),
        ]
        if case .disconnecting(let commandId, pendingConnect: true) = route.phase {
            events.append(
                .connectionStateChanged(
                    commandId: commandId,
                    connectionId: route.correlation.connectionId,
                    result: .ok(state: .disconnected)
                )
            )
            events.append(.commandCompleted(commandId: commandId, error: nil))
        }
        return events
    }

    static func unavailable(
        route: AppleConnectionRoute,
        error: BleEventError
    ) -> [BleEvent] {
        switch route.phase {
        case .connecting:
            return connectFailure(route: route, error: error)
        case .connected, .cancellingAfterBackendFailure:
            return [
                .connectionStateChanged(
                    commandId: nil,
                    connectionId: route.correlation.connectionId,
                    result: .err(error: error)
                ),
            ]
        case .disconnecting(let commandId, let pendingConnect):
            if pendingConnect {
                return connectFailure(route: route, error: error)
            }
            return [
                .connectionStateChanged(
                    commandId: commandId,
                    connectionId: route.correlation.connectionId,
                    result: .ok(state: .disconnected)
                ),
                .commandCompleted(commandId: commandId, error: nil),
            ]
        }
    }
}
