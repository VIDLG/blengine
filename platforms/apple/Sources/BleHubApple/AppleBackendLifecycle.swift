import BleHubUniFfi

internal enum AppleBackendLifecycle: Equatable {
    case inert
    case initializing(CommandId)
    case ready
    case faulted
    case closed

    mutating func beginInitialization(commandId: CommandId) throws {
        guard self == .inert else {
            if self == .closed {
                throw CommandDispatchError.Closed
            }
            throw CommandDispatchError.InvalidState(
                detail: "Apple backend initialization has already started"
            )
        }
        self = .initializing(commandId)
    }

    mutating func markFaulted() {
        guard self != .closed else {
            return
        }
        self = .faulted
    }

    mutating func consumeInitialStateCallback() -> CommandId? {
        guard case .initializing(let commandId) = self else {
            return nil
        }
        self = .ready
        return commandId
    }

    func requireReady(operation: String) throws {
        switch self {
        case .ready:
            return
        case .closed:
            throw CommandDispatchError.Closed
        case .inert, .initializing, .faulted:
            throw CommandDispatchError.InvalidState(
                detail: "Apple backend must be ready before \(operation)"
            )
        }
    }
}
