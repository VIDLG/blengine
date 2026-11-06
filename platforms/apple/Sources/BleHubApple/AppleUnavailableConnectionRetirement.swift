import BleHubUniFfi
import CoreBluetooth

/// Queue-confined retirement for an already sampled adapter/authorization update.
/// Native cleanup remains with the backend; no timers or callback barriers are reset here.
internal enum AppleUnavailableConnectionRetirement {
    static func retireIfUnavailable(
        state: CBManagerState,
        authorization: CBManagerAuthorization,
        connections: AppleConnectionController,
        gatt: AppleGattController,
        io: AppleGattIoController,
        watchdogs: AppleGattWatchdogRegistry,
        retireNative: (AppleConnectionCorrelation, AppleGattRetirement) -> Void
    ) -> [BleEvent] {
        guard let error = AppleEventMapper.connectionAvailabilityError(
            state: state,
            authorization: authorization
        ) else {
            return []
        }

        var events: [BleEvent] = []
        for route in connections.drain() {
            let connection = route.correlation
            watchdogs.cancel(connectionId: connection.connectionId)?.cancel()
            let retirement = io.retireAll(connection: connection, error: error)
            events.append(contentsOf: AppleBackendEventProjection.gattRetirementEvents(retirement))
            if let command = gatt.retire(connection: connection) {
                events.append(AppleBackendEventProjection.gattFailureEvent(command: command, error: error))
            }
            retireNative(connection, retirement)
            events.append(contentsOf: AppleConnectionEventComposer.unavailable(route: route, error: error))
        }
        _ = gatt.drain()
        return events
    }
}
