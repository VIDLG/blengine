import BleHubUniFfi

/// Resolves control on the backend queue using the existing operation owners.
internal enum AppleOperationCancellation {
    static func cancel(
        commandId: CommandId,
        connections: AppleConnectionController,
        gatt: AppleGattController,
        io: AppleGattIoController
    ) throws -> AppleConnectionCorrelation {
        switch io.cancel(commandId: commandId) {
        case .queued(let connection):
            return connection
        case .active:
            throw CommandDispatchError.OperationNotCancellable(targetCommandId: commandId)
        case .notFound:
            if connections.hasPendingConnect(commandId: commandId)
                || gatt.hasPendingDiscovery(commandId: commandId) {
                throw CommandDispatchError.OperationNotCancellable(targetCommandId: commandId)
            }
            throw CommandDispatchError.OperationNotFound(targetCommandId: commandId)
        }
    }
}
