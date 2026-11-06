import BleHubUniFfi
import Dispatch

/// Serial-queue-confined wiring over the existing controller and watchdog owners.
/// Scheduling and native effects stay ports of the backend, not another driver.
internal enum AppleGattWatchdogOrchestration {
    static func nextAction(
        connection: AppleConnectionCorrelation,
        io: AppleGattIoController,
        canSendWriteWithoutResponse: Bool,
        maximumWriteWithResponse: Int,
        maximumWriteWithoutResponse: Int,
        armCapacity: (AppleConnectionCorrelation, CommandId) -> Void
    ) throws -> AppleGattIoAction? {
        let action = try io.nextAction(
            connection: connection,
            canSendWriteWithoutResponse: canSendWriteWithoutResponse,
            maximumWriteWithResponse: maximumWriteWithResponse,
            maximumWriteWithoutResponse: maximumWriteWithoutResponse
        )
        if case .waitForWriteCapacity(let commandId, let owner) = action {
            armCapacity(owner, commandId)
        }
        return action
    }

    /// The caller has already validated the native characteristic before entering.
    static func submitWithoutResponse(
        command: WriteCharacteristicCommand,
        connection: AppleConnectionCorrelation,
        peripheral: any ApplePeripheralObject,
        characteristic: any AppleGattCharacteristicObject,
        io: AppleGattIoController,
        watchdogs: AppleGattWatchdogRegistry,
        submit: () -> Void
    ) -> AppleGattIoCompletion? {
        watchdogs.cancel(
            connectionId: connection.connectionId,
            kind: .writeCapacity(command.commandId)
        )?.cancel()
        submit()
        return io.didSubmitWriteWithoutResponse(
            connection: connection,
            peripheral: peripheral,
            characteristic: characteristic
        )
    }

    @discardableResult
    static func arm(
        connection: AppleConnectionCorrelation,
        kind: AppleGattWatchdogKind,
        watchdogs: AppleGattWatchdogRegistry,
        delay: DispatchTimeInterval,
        schedule: (DispatchTime, DispatchWorkItem) -> Void,
        expire: @escaping (AppleGattWatchdogToken) -> Void
    ) -> AppleGattWatchdogToken {
        var token: AppleGattWatchdogToken?
        let workItem = DispatchWorkItem {
            guard let token else { return }
            expire(token)
        }
        let armed = watchdogs.arm(
            connectionId: connection.connectionId,
            kind: kind,
            workItem: workItem
        )
        armed.replaced?.cancel()
        token = armed.token
        schedule(.now() + delay, workItem)
        return armed.token
    }

    /// Consumes the same token gate for scheduled expiry and a late delivery.
    /// Native availability is checked before fail-closed mutation, as in the backend.
    static func expire<Native: AnyObject>(
        connection: AppleConnectionCorrelation,
        token: AppleGattWatchdogToken,
        context: String,
        watchdogs: AppleGattWatchdogRegistry,
        connections: AppleConnectionController,
        gatt: AppleGattController,
        io: AppleGattIoController,
        resolveNative: () -> Native?,
        retireNative: (Native, AppleGattRetirement) -> Void
    ) -> [BleEvent]? {
        guard let watchdog = watchdogs.takeIfCurrent(
            connectionId: connection.connectionId,
            token: token
        ), !watchdog.isCancelled else {
            return nil
        }
        let kind = token.kind
        if case .writeCapacity(let commandId) = kind,
           !io.isWaitingForWriteCapacity(connection: connection, commandId: commandId) {
            return nil
        }
        guard connections.connectedCorrelation(for: connection.connectionId) == connection,
              let native = resolveNative(),
              connections.beginFailClosedDisconnect(connection: connection) else {
            return nil
        }
        let timeout = AppleEventMapper.operationTimeout(
            "Apple GATT operation watchdog expired: \(context)"
        )
        let aborted = AppleEventMapper.operationAborted(
            "Apple GATT connection recovery retired a pending operation"
        )
        let retirement = io.retireAll(
            connection: connection,
            error: aborted,
            activeOperationError: kind == .discovery ? aborted : timeout,
            queuedOperationError: aborted
        )
        let pendingDiscovery = gatt.retire(connection: connection)
        retireNative(native, retirement)
        var events = AppleBackendEventProjection.gattRetirementEvents(retirement)
        if let pendingDiscovery {
            events.append(AppleBackendEventProjection.gattFailureEvent(
                command: pendingDiscovery,
                error: kind == .discovery ? timeout : aborted
            ))
        }
        return events
    }
}
