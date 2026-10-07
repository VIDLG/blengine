import BleHubUniFfi
import Foundation

/// Converts backend-owned operation completions into the public BleEvent stream.
/// This is deliberately pure: queue ownership and native callback ordering remain
/// in AppleBleBackend.
internal enum AppleBackendEventProjection {
    static func gattFailureEvent(
        command: DiscoverGattCommand,
        error: BleEventError
    ) -> BleEvent {
        .commandCompleted(commandId: command.commandId, error: error)
    }

    static func rssiEvents(_ completion: AppleRssiCompletion) -> [BleEvent] {
        switch completion {
        case .read(let command, let rssi):
            return [
                .rssiRead(
                    commandId: command.commandId,
                    connectionId: command.connectionId,
                    rssi: rssi
                ),
                .commandCompleted(commandId: command.commandId, error: nil),
            ]
        case .failed(let command, let error):
            return [.commandCompleted(commandId: command.commandId, error: error)]
        }
    }

    static func gattIoEvents(_ completion: AppleGattIoCompletion) -> [BleEvent] {
        switch completion {
        case .read(let command, let value):
            return [
                .characteristicRead(
                    commandId: command.commandId,
                    connectionId: command.connectionId,
                    gattSessionId: command.gattSessionId,
                    characteristicId: command.characteristicId,
                    value: Data(value)
                ),
                .commandCompleted(commandId: command.commandId, error: nil),
            ]
        case .write(let command):
            return [
                .characteristicWritten(
                    commandId: command.commandId,
                    connectionId: command.connectionId,
                    gattSessionId: command.gattSessionId,
                    characteristicId: command.characteristicId
                ),
                .commandCompleted(commandId: command.commandId, error: nil),
            ]
        case .failed(let command, let error):
            let commandId: CommandId
            switch command {
            case .read(let read):
                commandId = read.commandId
            case .write(let write):
                commandId = write.commandId
            }
            return [.commandCompleted(commandId: commandId, error: error)]
        }
    }

    static func gattNotificationEvents(
        _ completion: AppleGattNotificationCompletion
    ) -> [BleEvent] {
        switch completion {
        case .enabled(let command, let actualMode):
            return [
                .notificationStateChanged(
                    commandId: command.commandId,
                    connectionId: command.connectionId,
                    gattSessionId: command.gattSessionId,
                    subscriptionId: command.subscriptionId,
                    characteristicId: command.characteristicId,
                    state: .enabled(actualMode: actualMode)
                ),
                .commandCompleted(commandId: command.commandId, error: nil),
            ]
        case .disabled(let command):
            return [
                .notificationStateChanged(
                    commandId: command.commandId,
                    connectionId: command.connectionId,
                    gattSessionId: command.gattSessionId,
                    subscriptionId: command.subscriptionId,
                    characteristicId: command.characteristicId,
                    state: .disabled
                ),
                .commandCompleted(commandId: command.commandId, error: nil),
            ]
        case .failed(let command, let error):
            return [.commandCompleted(commandId: command.commandId, error: error)]
        case .rediscoveryRetired, .rediscoveryFailed:
            return []
        }
    }

    static func gattRetirementEvents(_ retirement: AppleGattRetirement) -> [BleEvent] {
        retirement.completions.flatMap { completion in
            switch completion {
            case .io(let ioCompletion):
                return gattIoEvents(ioCompletion)
            case .notification(let notificationCompletion):
                return gattNotificationEvents(notificationCompletion)
            }
        }
    }
}
