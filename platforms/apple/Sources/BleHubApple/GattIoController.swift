import BleHubUniFfi
import Foundation

internal enum AppleGattIoCommand {
    case read(ReadCharacteristicCommand)
    case write(WriteCharacteristicCommand)
}

internal enum AppleGattIoCompletion {
    case read(command: ReadCharacteristicCommand, value: [UInt8])
    case write(command: WriteCharacteristicCommand)
    case failed(command: AppleGattIoCommand, error: BleEventError)
}

internal enum AppleGattIoAction {
    case read(
        command: ReadCharacteristicCommand,
        connection: AppleConnectionCorrelation,
        characteristic: any AppleGattCharacteristicObject
    )
    case writeWithResponse(
        command: WriteCharacteristicCommand,
        connection: AppleConnectionCorrelation,
        characteristic: any AppleGattCharacteristicObject
    )
    case writeWithoutResponse(
        command: WriteCharacteristicCommand,
        connection: AppleConnectionCorrelation,
        characteristic: any AppleGattCharacteristicObject
    )
    case terminal(AppleGattIoCompletion)
}

internal enum AppleNotificationModeSelection: Equatable {
    case supported(NotificationMode)
    case strictUnsupported(NotificationMode)
    case noSupportedMode
}

internal enum AppleGattRediscoveryPreparation {
    case ready
    case unsubscribeQueued
    case unsafe(BleEventError)
}

internal enum AppleGattNotificationCompletion {
    case enabled(command: SetNotificationsCommand, actualMode: NotificationMode)
    case disabled(command: SetNotificationsCommand)
    case failed(command: SetNotificationsCommand, error: BleEventError)
    case rediscoveryRetired
    case rediscoveryFailed(BleEventError)
}

internal enum AppleGattNotificationAction {
    case setNotifyValue(
        command: SetNotificationsCommand,
        connection: AppleConnectionCorrelation,
        characteristic: any AppleGattCharacteristicObject,
        enabled: Bool
    )
    case retireForRediscovery(
        connection: AppleConnectionCorrelation,
        characteristic: any AppleGattCharacteristicObject
    )
    case terminal(AppleGattNotificationCompletion)
}

internal struct AppleGattNotificationValue {
    let connectionId: ConnectionId
    let gattSessionId: GattSessionId
    let subscriptionId: SubscriptionId
    let characteristicId: GattCharacteristicId
    let value: [UInt8]
}

internal enum AppleGattValueDisposition {
    case read(AppleGattIoCompletion)
    case notification(AppleGattNotificationValue)
    case ignored
}

internal enum AppleGattRetiredCompletion {
    case io(AppleGattIoCompletion)
    case notification(AppleGattNotificationCompletion)
}

internal struct AppleGattRetirement {
    let completions: [AppleGattRetiredCompletion]
    let activeNotificationCharacteristicsToUnsubscribe: [any AppleGattCharacteristicObject]

    var ioCompletions: [AppleGattIoCompletion] {
        completions.compactMap {
            guard case .io(let completion) = $0 else {
                return nil
            }
            return completion
        }
    }

    var notificationCompletions: [AppleGattNotificationCompletion] {
        completions.compactMap {
            guard case .notification(let completion) = $0 else {
                return nil
            }
            return completion
        }
    }
}

internal final class AppleGattIoController {
    private static let maximumWriteValueBytes = 512
    internal static let maximumPendingOperationsPerConnection = 1_024
    private static let maximumSafeBarrierRoutes = 128
    private static let maximumRetainedBarrierRoutes = maximumSafeBarrierRoutes + 1

    private enum CallbackKind: Hashable {
        case read
        case writeWithResponse
        case notificationState
    }

    private struct CallbackKey: Hashable {
        let peripheralIdentity: ObjectIdentifier
        let characteristicIdentity: ObjectIdentifier
        let kind: CallbackKind
    }

    private struct CallbackBarrier {
        let peripheral: any ApplePeripheralObject
        let characteristic: any AppleGattCharacteristicObject
    }

    private struct ValueRouteKey: Hashable {
        let peripheralIdentity: ObjectIdentifier
        let characteristicIdentity: ObjectIdentifier
    }

    private struct ValueRouteBarrier {
        let correlation: SubscriptionCorrelation
        let characteristic: any AppleGattCharacteristicObject
    }

    private struct SubscriptionCorrelation {
        let connection: AppleConnectionCorrelation
        let generation: UInt64
        let gattSessionId: GattSessionId
        let subscriptionId: SubscriptionId
        let characteristicId: GattCharacteristicId
    }

    private enum SubscriptionPhase {
        case queuedEnable(commandId: CommandId)
        case enabling(commandId: CommandId)
        case active
        case queuedDisable(commandId: CommandId)
        case disabling(commandId: CommandId)
        case rediscoveryQueued
        case rediscoveryDisabling
    }

    private struct SubscriptionRoute {
        let correlation: SubscriptionCorrelation
        var phase: SubscriptionPhase
        let actualMode: NotificationMode
        let characteristic: any AppleGattCharacteristicObject
    }

    private enum NotificationTransition {
        case enable(correlation: SubscriptionCorrelation, actualMode: NotificationMode)
        case disable(correlation: SubscriptionCorrelation)
        case unsupported(requestedMode: NotificationMode)
    }

    private enum Operation {
        case read(
            command: ReadCharacteristicCommand,
            characteristic: any AppleGattCharacteristicObject
        )
        case write(
            command: WriteCharacteristicCommand,
            characteristic: any AppleGattCharacteristicObject
        )
        case setNotifications(
            command: SetNotificationsCommand,
            characteristic: any AppleGattCharacteristicObject,
            transition: NotificationTransition
        )
        case retireSubscription(
            correlation: SubscriptionCorrelation,
            characteristic: any AppleGattCharacteristicObject
        )

        var ioCommand: AppleGattIoCommand? {
            switch self {
            case .read(let command, _):
                return .read(command)
            case .write(let command, _):
                return .write(command)
            case .setNotifications, .retireSubscription:
                return nil
            }
        }

        var notificationCommand: SetNotificationsCommand? {
            guard case .setNotifications(let command, _, _) = self else {
                return nil
            }
            return command
        }

        var characteristic: any AppleGattCharacteristicObject {
            switch self {
            case .read(_, let characteristic),
                 .write(_, let characteristic),
                 .setNotifications(_, let characteristic, _),
                 .retireSubscription(_, let characteristic):
                return characteristic
            }
        }

        var callbackKind: CallbackKind? {
            switch self {
            case .read:
                return .read
            case .write(let command, _):
                return command.writeType == .withResponse ? .writeWithResponse : nil
            case .setNotifications(_, _, let transition):
                switch transition {
                case .enable, .disable:
                    return .notificationState
                case .unsupported:
                    return nil
                }
            case .retireSubscription:
                return .notificationState
            }
        }
    }

    private enum ActiveStage {
        case callback(CallbackKey)
        case notificationState(SubscriptionCorrelation)
        case submittingWriteWithoutResponse
    }

    private struct OperationQueue {
        private var storage: [Operation] = []
        private var headIndex = 0

        var count: Int {
            storage.count - headIndex
        }

        var isEmpty: Bool {
            count == 0
        }

        var first: Operation? {
            guard headIndex < storage.count else {
                return nil
            }
            return storage[headIndex]
        }

        mutating func append(_ operation: Operation) {
            storage.append(operation)
        }

        func contains(where predicate: (Operation) throws -> Bool) rethrows -> Bool {
            try storage[headIndex...].contains(where: predicate)
        }

        func compactMap<Element>(
            _ transform: (Operation) throws -> Element?
        ) rethrows -> [Element] {
            try storage[headIndex...].compactMap(transform)
        }

        mutating func removeFirst() {
            precondition(!isEmpty)
            headIndex += 1
            if headIndex == storage.count {
                storage.removeAll(keepingCapacity: true)
                headIndex = 0
            } else if headIndex >= 64 && headIndex >= storage.count / 2 {
                storage = Array(storage[headIndex...])
                headIndex = 0
            }
        }
    }

    private struct ConnectionQueue {
        let connection: AppleConnectionCorrelation
        var operations: OperationQueue
        var activeStage: ActiveStage?
        var subscriptions: [SubscriptionId: SubscriptionRoute]
        var subscriptionByCharacteristic: [GattCharacteristicId: SubscriptionId]
    }

    private var queuesByConnectionId: [ConnectionId: ConnectionQueue] = [:]
    private var staleCallbackBarriers: [CallbackKey: CallbackBarrier] = [:]
    private var valueRouteBarriers: [ValueRouteKey: ValueRouteBarrier] = [:]
    private var safetyFaultLatched = false
    private var pendingSafetyFault: BleEventError?
    private var nextSubscriptionGeneration: UInt64 = 1

    static func selectNotificationMode(
        request: NotificationModeRequest,
        properties: CharacteristicProperties
    ) -> AppleNotificationModeSelection {
        switch request {
        case .platformDefault where properties.notifiable:
            return .supported(.notification)
        case .platformDefault where properties.indicatable:
            return .supported(.indication)
        case .platformDefault:
            return .noSupportedMode
        case .requireNotification where properties.notifiable:
            return .supported(.notification)
        case .requireNotification:
            return .strictUnsupported(.notification)
        case .requireIndication where properties.indicatable && !properties.notifiable:
            return .supported(.indication)
        case .requireIndication:
            return .strictUnsupported(.indication)
        }
    }

    var pendingCount: Int {
        queuesByConnectionId.values.reduce(0) { $0 + $1.operations.count }
    }

    func hasPending(connection: AppleConnectionCorrelation) -> Bool {
        guard let queue = matchingQueue(connection: connection) else {
            return false
        }
        return !queue.operations.isEmpty
    }

    func prepareSubscriptionsForRediscovery(
        connection: AppleConnectionCorrelation
    ) -> AppleGattRediscoveryPreparation {
        guard !safetyFaultLatched else {
            return .unsafe(
                BleEventError(
                    category: .internal,
                    message: "Apple GATT rediscovery encountered a previously latched safety fault",
                    platformDetails: nil
                )
            )
        }
        guard var queue = matchingQueue(connection: connection) else {
            return .ready
        }
        guard queue.operations.isEmpty else {
            return rediscoverySafetyFault(
                "Apple GATT rediscovery encountered pending I/O after admission"
            )
        }
        guard !queue.subscriptions.isEmpty else {
            return .ready
        }

        let routes = queue.subscriptions.values.sorted {
            $0.correlation.generation < $1.correlation.generation
        }
        guard routes.count <= Self.maximumPendingOperationsPerConnection else {
            return rediscoverySafetyFault(
                "Apple GATT rediscovery cannot safely retire more than \(Self.maximumPendingOperationsPerConnection) active subscriptions"
            )
        }
        for route in routes {
            guard case .active = route.phase else {
                return rediscoverySafetyFault(
                    "Apple GATT rediscovery encountered a subscription outside the active phase"
                )
            }
        }
        for var route in routes {
            route.phase = .rediscoveryQueued
            queue.subscriptions[route.correlation.subscriptionId] = route
            queue.operations.append(
                .retireSubscription(
                    correlation: route.correlation,
                    characteristic: route.characteristic
                )
            )
        }
        queuesByConnectionId[connection.connectionId] = queue
        return .unsubscribeQueued
    }


    func enqueueRead(
        command: ReadCharacteristicCommand,
        connection: AppleConnectionCorrelation,
        target: AppleGattCharacteristicTarget
    ) throws {
        try rejectSafetyFault()
        try validateTarget(
            connectionId: command.connectionId,
            gattSessionId: command.gattSessionId,
            characteristicId: command.characteristicId,
            connection: connection,
            target: target
        )
        guard target.characteristic.appleGattProperties.readable else {
            throw CommandDispatchError.InvalidRequest(
                detail: "characteristic does not advertise the Read property"
            )
        }
        try rejectValueRouteBarrier(
            connection: connection,
            characteristic: target.characteristic
        )
        if let queue = matchingQueue(connection: connection),
           queue.subscriptionByCharacteristic[command.characteristicId] != nil ||
           queue.operations.contains(where: {
               isNotificationOperation($0, characteristic: target.characteristic)
           })
        {
            throw CommandDispatchError.InvalidState(
                detail: "the characteristic has an enabling, active, or disabling subscription route"
            )
        }

        let operation = Operation.read(
            command: command,
            characteristic: target.characteristic
        )
        try enqueue(operation, connection: connection)
    }

    func enqueueWrite(
        command: WriteCharacteristicCommand,
        connection: AppleConnectionCorrelation,
        target: AppleGattCharacteristicTarget
    ) throws {
        try rejectSafetyFault()
        try validateTarget(
            connectionId: command.connectionId,
            gattSessionId: command.gattSessionId,
            characteristicId: command.characteristicId,
            connection: connection,
            target: target
        )
        guard command.value.count <= Self.maximumWriteValueBytes else {
            throw CommandDispatchError.InvalidRequest(
                detail: "value contains \(command.value.count) bytes; maximum is \(Self.maximumWriteValueBytes)"
            )
        }

        switch command.writeType {
        case .withResponse:
            guard target.characteristic.appleGattProperties.writableWithResponse else {
                throw CommandDispatchError.InvalidRequest(
                    detail: "characteristic does not advertise the Write property"
                )
            }
        case .withoutResponse:
            guard target.characteristic.appleGattProperties.writableWithoutResponse else {
                throw CommandDispatchError.InvalidRequest(
                    detail: "characteristic does not advertise the WriteWithoutResponse property"
                )
            }
        }

        let operation = Operation.write(
            command: command,
            characteristic: target.characteristic
        )
        try enqueue(operation, connection: connection)
    }

    func enqueueSetNotifications(
        command: SetNotificationsCommand,
        connection: AppleConnectionCorrelation,
        target: AppleGattCharacteristicTarget
    ) throws {
        try rejectSafetyFault()
        try validateTarget(
            connectionId: command.connectionId,
            gattSessionId: command.gattSessionId,
            characteristicId: command.characteristicId,
            connection: connection,
            target: target
        )

        switch command.action {
        case .enable(let mode):
            try enqueueNotificationEnable(
                command: command,
                request: mode,
                connection: connection,
                target: target
            )
        case .disable:
            try enqueueNotificationDisable(
                command: command,
                connection: connection,
                target: target
            )
        }
    }

    func nextAction(
        connection: AppleConnectionCorrelation,
        canSendWriteWithoutResponse: Bool,
        maximumWriteWithResponse: Int,
        maximumWriteWithoutResponse: Int
    ) throws -> AppleGattIoAction? {
        guard var queue = matchingQueue(connection: connection),
              queue.activeStage == nil,
              let operation = queue.operations.first
        else {
            return nil
        }

        switch operation {
        case .read(let command, let characteristic):
            let key = callbackKey(
                connection: connection,
                characteristic: characteristic,
                kind: .read
            )
            try rejectStaleCallbackBarrier(key)
            queue.activeStage = .callback(key)
            queuesByConnectionId[connection.connectionId] = queue
            return .read(
                command: command,
                connection: connection,
                characteristic: characteristic
            )

        case .write(let command, let characteristic):
            let maximumLength: Int
            switch command.writeType {
            case .withResponse:
                maximumLength = maximumWriteWithResponse
            case .withoutResponse:
                maximumLength = maximumWriteWithoutResponse
            }
            if command.value.count > maximumLength {
                removeFirstOperation(from: &queue)
                store(queue)
                return .terminal(
                    .failed(
                        command: .write(command),
                        error: platformWriteLengthError(
                            actual: command.value.count,
                            maximum: maximumLength,
                            writeType: command.writeType
                        )
                    )
                )
            }

            switch command.writeType {
            case .withResponse:
                let key = callbackKey(
                    connection: connection,
                    characteristic: characteristic,
                    kind: .writeWithResponse
                )
                try rejectStaleCallbackBarrier(key)
                queue.activeStage = .callback(key)
                queuesByConnectionId[connection.connectionId] = queue
                return .writeWithResponse(
                    command: command,
                    connection: connection,
                    characteristic: characteristic
                )

            case .withoutResponse:
                guard canSendWriteWithoutResponse else {
                    return nil
                }
                queue.activeStage = .submittingWriteWithoutResponse
                queuesByConnectionId[connection.connectionId] = queue
                return .writeWithoutResponse(
                    command: command,
                    connection: connection,
                    characteristic: characteristic
                )
            }

        case .setNotifications, .retireSubscription:
            return nil
        }
    }

    func nextNotificationAction(
        connection: AppleConnectionCorrelation
    ) -> AppleGattNotificationAction? {
        guard var queue = matchingQueue(connection: connection),
              queue.activeStage == nil,
              let operation = queue.operations.first
        else {
            return nil
        }

        switch operation {
        case .read, .write:
            return nil

        case .retireSubscription(let correlation, let characteristic):
            guard var route = queue.subscriptions[correlation.subscriptionId],
                  subscriptionCorrelationsMatch(route.correlation, correlation),
                  case .rediscoveryQueued = route.phase
            else {
                return nil
            }
            route.phase = .rediscoveryDisabling
            queue.subscriptions[correlation.subscriptionId] = route
            queue.activeStage = .notificationState(correlation)
            queuesByConnectionId[connection.connectionId] = queue
            return .retireForRediscovery(
                connection: connection,
                characteristic: characteristic
            )

        case .setNotifications(let command, let characteristic, let transition):
            switch transition {
            case .unsupported(let requestedMode):
                removeFirstOperation(from: &queue)
                store(queue)
                return .terminal(
                    .failed(
                        command: command,
                        error: BleEventError(
                            category: .notSupported,
                            message: "CoreBluetooth cannot guarantee strictly requested \(requestedMode) mode for this characteristic",
                            platformDetails: nil
                        )
                    )
                )

            case .enable(let correlation, _):
                guard var route = queue.subscriptions[correlation.subscriptionId],
                      subscriptionCorrelationsMatch(route.correlation, correlation),
                      case .queuedEnable(let commandId) = route.phase,
                      commandId == command.commandId
                else {
                    return nil
                }
                route.phase = .enabling(commandId: command.commandId)
                queue.subscriptions[correlation.subscriptionId] = route
                queue.activeStage = .notificationState(correlation)
                queuesByConnectionId[connection.connectionId] = queue
                return .setNotifyValue(
                    command: command,
                    connection: connection,
                    characteristic: characteristic,
                    enabled: true
                )

            case .disable(let correlation):
                guard var route = queue.subscriptions[correlation.subscriptionId],
                      subscriptionCorrelationsMatch(route.correlation, correlation),
                      case .queuedDisable(let commandId) = route.phase,
                      commandId == command.commandId
                else {
                    return nil
                }
                route.phase = .disabling(commandId: command.commandId)
                queue.subscriptions[correlation.subscriptionId] = route
                queue.activeStage = .notificationState(correlation)
                queuesByConnectionId[connection.connectionId] = queue
                return .setNotifyValue(
                    command: command,
                    connection: connection,
                    characteristic: characteristic,
                    enabled: false
                )
            }
        }
    }

    func didReadValue(
        connection: AppleConnectionCorrelation,
        peripheral: any ApplePeripheralObject,
        characteristic: any AppleGattCharacteristicObject,
        value: [UInt8]?,
        error: BleEventError?
    ) -> AppleGattIoCompletion? {
        let key = CallbackKey(
            peripheralIdentity: ObjectIdentifier(peripheral),
            characteristicIdentity: ObjectIdentifier(characteristic),
            kind: .read
        )
        if valueRouteIsBlocked(peripheral: peripheral, characteristic: characteristic) ||
            matchingSubscriptionRoute(
                connection: connection,
                peripheral: peripheral,
                characteristic: characteristic
            ) != nil
        {
            return nil
        }
        if consumeStaleCallbackBarrier(key) {
            return nil
        }
        guard var queue = matchingQueue(
            connection: connection,
            callbackPeripheral: peripheral
        ),
        case .callback(let expectedKey) = queue.activeStage,
        expectedKey == key,
        case .read(let command, let expectedCharacteristic) = queue.operations.first,
        ObjectIdentifier(expectedCharacteristic) == ObjectIdentifier(characteristic)
        else {
            return nil
        }

        removeFirstOperation(from: &queue)
        store(queue)
        if let error {
            return .failed(command: .read(command), error: error)
        }
        guard let value else {
            return .failed(
                command: .read(command),
                error: BleEventError(
                    category: .gatt,
                    message: "CoreBluetooth characteristic read completed without a value",
                    platformDetails: nil
                )
            )
        }
        return .read(command: command, value: value)
    }

    func didUpdateValue(
        connection: AppleConnectionCorrelation,
        peripheral: any ApplePeripheralObject,
        characteristic: any AppleGattCharacteristicObject,
        value: [UInt8]?,
        error: BleEventError?
    ) -> AppleGattValueDisposition {
        if let barrier = valueRouteBarrier(
            peripheral: peripheral,
            characteristic: characteristic
        ) {
            guard error == nil, let value else {
                return .ignored
            }
            return .notification(
                AppleGattNotificationValue(
                    connectionId: barrier.correlation.connection.connectionId,
                    gattSessionId: barrier.correlation.gattSessionId,
                    subscriptionId: barrier.correlation.subscriptionId,
                    characteristicId: barrier.correlation.characteristicId,
                    value: value
                )
            )
        }
        if let route = matchingSubscriptionRoute(
            connection: connection,
            peripheral: peripheral,
            characteristic: characteristic
        ) {
            guard subscriptionAcceptsValue(route.phase), error == nil, let value else {
                return .ignored
            }
            return .notification(
                AppleGattNotificationValue(
                    connectionId: route.correlation.connection.connectionId,
                    gattSessionId: route.correlation.gattSessionId,
                    subscriptionId: route.correlation.subscriptionId,
                    characteristicId: route.correlation.characteristicId,
                    value: value
                )
            )
        }
        guard let completion = didReadValue(
            connection: connection,
            peripheral: peripheral,
            characteristic: characteristic,
            value: value,
            error: error
        ) else {
            return .ignored
        }
        return .read(completion)
    }

    func didUpdateNotificationState(
        connection: AppleConnectionCorrelation,
        peripheral: any ApplePeripheralObject,
        characteristic: any AppleGattCharacteristicObject,
        isNotifying: Bool? = nil,
        error: BleEventError?
    ) -> AppleGattNotificationCompletion? {
        let callbackKey = CallbackKey(
            peripheralIdentity: ObjectIdentifier(peripheral),
            characteristicIdentity: ObjectIdentifier(characteristic),
            kind: .notificationState
        )
        if consumeStaleCallbackBarrier(callbackKey) {
            return nil
        }
        guard var queue = matchingQueue(
            connection: connection,
            callbackPeripheral: peripheral
        ),
        case .notificationState(let expectedCorrelation) = queue.activeStage,
        let operation = queue.operations.first,
        ObjectIdentifier(operation.characteristic) == ObjectIdentifier(characteristic)
        else {
            return nil
        }

        switch operation {
        case .read, .write:
            return nil

        case .retireSubscription(let correlation, _):
            guard subscriptionCorrelationsMatch(expectedCorrelation, correlation),
                  var route = queue.subscriptions[correlation.subscriptionId],
                  subscriptionCorrelationsMatch(route.correlation, correlation),
                  case .rediscoveryDisabling = route.phase
            else {
                return nil
            }
            removeFirstOperation(from: &queue)
            if let error = notificationStateError(
                callbackError: error,
                expectedIsNotifying: false,
                observedIsNotifying: isNotifying
            ) {
                route.phase = .active
                queue.subscriptions[correlation.subscriptionId] = route
                store(queue)
                return .rediscoveryFailed(error)
            }
            removeSubscription(correlation: correlation, from: &queue)
            addValueRouteBarrier(
                correlation: correlation,
                characteristic: characteristic
            )
            store(queue)
            return .rediscoveryRetired

        case .setNotifications(let command, _, let transition):
            switch transition {
            case .unsupported:
                return nil

            case .enable(let correlation, let actualMode):
                guard subscriptionCorrelationsMatch(expectedCorrelation, correlation),
                      var route = queue.subscriptions[correlation.subscriptionId],
                      subscriptionCorrelationsMatch(route.correlation, correlation),
                      case .enabling(let commandId) = route.phase,
                      commandId == command.commandId
                else {
                    return nil
                }
                removeFirstOperation(from: &queue)
                if let error = notificationStateError(
                    callbackError: error,
                    expectedIsNotifying: true,
                    observedIsNotifying: isNotifying
                ) {
                    removeSubscription(correlation: correlation, from: &queue)
                    addValueRouteBarrier(
                        correlation: correlation,
                        characteristic: characteristic
                    )
                    store(queue)
                    return .failed(command: command, error: error)
                }
                route.phase = .active
                removeValueRouteBarrier(
                    connection: correlation.connection,
                    characteristic: characteristic
                )
                queue.subscriptions[correlation.subscriptionId] = route
                store(queue)
                return .enabled(command: command, actualMode: actualMode)

            case .disable(let correlation):
                guard subscriptionCorrelationsMatch(expectedCorrelation, correlation),
                      var route = queue.subscriptions[correlation.subscriptionId],
                      subscriptionCorrelationsMatch(route.correlation, correlation),
                      case .disabling(let commandId) = route.phase,
                      commandId == command.commandId
                else {
                    return nil
                }
                removeFirstOperation(from: &queue)
                if let error = notificationStateError(
                    callbackError: error,
                    expectedIsNotifying: false,
                    observedIsNotifying: isNotifying
                ) {
                    route.phase = .active
                    queue.subscriptions[correlation.subscriptionId] = route
                    store(queue)
                    return .failed(command: command, error: error)
                }
                removeSubscription(correlation: correlation, from: &queue)
                addValueRouteBarrier(
                    correlation: correlation,
                    characteristic: characteristic
                )
                store(queue)
                return .disabled(command: command)
            }
        }
    }

    func didWriteValue(
        connection: AppleConnectionCorrelation,
        peripheral: any ApplePeripheralObject,
        characteristic: any AppleGattCharacteristicObject,
        error: BleEventError?
    ) -> AppleGattIoCompletion? {
        let key = CallbackKey(
            peripheralIdentity: ObjectIdentifier(peripheral),
            characteristicIdentity: ObjectIdentifier(characteristic),
            kind: .writeWithResponse
        )
        if consumeStaleCallbackBarrier(key) {
            return nil
        }
        guard var queue = matchingQueue(
            connection: connection,
            callbackPeripheral: peripheral
        ),
        case .callback(let expectedKey) = queue.activeStage,
        expectedKey == key,
        case .write(let command, let expectedCharacteristic) = queue.operations.first,
        command.writeType == .withResponse,
        ObjectIdentifier(expectedCharacteristic) == ObjectIdentifier(characteristic)
        else {
            return nil
        }

        removeFirstOperation(from: &queue)
        store(queue)
        if let error {
            return .failed(command: .write(command), error: error)
        }
        return .write(command: command)
    }

    func didSubmitWriteWithoutResponse(
        connection: AppleConnectionCorrelation,
        peripheral: any ApplePeripheralObject,
        characteristic: any AppleGattCharacteristicObject
    ) -> AppleGattIoCompletion? {
        guard var queue = matchingQueue(
            connection: connection,
            callbackPeripheral: peripheral
        ),
        case .submittingWriteWithoutResponse = queue.activeStage,
        case .write(let command, let expectedCharacteristic) = queue.operations.first,
        command.writeType == .withoutResponse,
        ObjectIdentifier(expectedCharacteristic) == ObjectIdentifier(characteristic)
        else {
            return nil
        }

        removeFirstOperation(from: &queue)
        store(queue)
        return .write(command: command)
    }

    func didBecomeReadyToSendWriteWithoutResponse(
        connection: AppleConnectionCorrelation,
        peripheral: any ApplePeripheralObject
    ) -> Bool {
        guard let queue = matchingQueue(
            connection: connection,
            callbackPeripheral: peripheral
        ),
        queue.activeStage == nil,
        case .write(let command, _) = queue.operations.first
        else {
            return false
        }
        return command.writeType == .withoutResponse
    }

    func retire(
        connection: AppleConnectionCorrelation,
        error: BleEventError
    ) -> [AppleGattIoCompletion] {
        retireAll(connection: connection, error: error).ioCompletions
    }

    func retireAll(
        connection: AppleConnectionCorrelation,
        error: BleEventError
    ) -> AppleGattRetirement {
        guard let queue = matchingQueue(connection: connection) else {
            return AppleGattRetirement(
                completions: [],
                activeNotificationCharacteristicsToUnsubscribe: []
            )
        }
        if let operation = queue.operations.first {
            switch queue.activeStage {
            case .callback(let key):
                addStaleCallbackBarrier(
                    key,
                    peripheral: queue.connection.peripheral,
                    characteristic: operation.characteristic
                )
            case .notificationState:
                addStaleCallbackBarrier(
                    callbackKey(
                        connection: queue.connection,
                        characteristic: operation.characteristic,
                        kind: .notificationState
                    ),
                    peripheral: queue.connection.peripheral,
                    characteristic: operation.characteristic
                )
            case .submittingWriteWithoutResponse, .none:
                break
            }
        }
        let routes = queue.subscriptions.values.sorted {
            $0.correlation.generation < $1.correlation.generation
        }
        var seenNotificationCharacteristics: Set<ObjectIdentifier> = []
        var activeNotificationCharacteristicsToUnsubscribe: [any AppleGattCharacteristicObject] = []
        for route in routes {
            addValueRouteBarrier(
                correlation: route.correlation,
                characteristic: route.characteristic
            )
            let identity = ObjectIdentifier(route.characteristic)
            if notificationRouteMayBeActiveNatively(route.phase),
               seenNotificationCharacteristics.insert(identity).inserted
            {
                activeNotificationCharacteristicsToUnsubscribe.append(route.characteristic)
            }
        }
        queuesByConnectionId.removeValue(forKey: connection.connectionId)
        return AppleGattRetirement(
            completions: queue.operations.compactMap { operation in
                if let command = operation.ioCommand {
                    return .io(.failed(command: command, error: error))
                }
                if let command = operation.notificationCommand {
                    return .notification(.failed(command: command, error: error))
                }
                return nil
            },
            activeNotificationCharacteristicsToUnsubscribe: activeNotificationCharacteristicsToUnsubscribe
        )
    }

    func consumeRetiredReadCallback(
        peripheral: any ApplePeripheralObject,
        characteristic: any AppleGattCharacteristicObject
    ) -> Bool {
        consumeStaleCallbackBarrier(
            CallbackKey(
                peripheralIdentity: ObjectIdentifier(peripheral),
                characteristicIdentity: ObjectIdentifier(characteristic),
                kind: .read
            )
        )
    }

    func consumeRetiredWriteCallback(
        peripheral: any ApplePeripheralObject,
        characteristic: any AppleGattCharacteristicObject
    ) -> Bool {
        consumeStaleCallbackBarrier(
            CallbackKey(
                peripheralIdentity: ObjectIdentifier(peripheral),
                characteristicIdentity: ObjectIdentifier(characteristic),
                kind: .writeWithResponse
            )
        )
    }

    func consumeRetiredNotificationStateCallback(
        peripheral: any ApplePeripheralObject,
        characteristic: any AppleGattCharacteristicObject
    ) -> Bool {
        consumeStaleCallbackBarrier(
            CallbackKey(
                peripheralIdentity: ObjectIdentifier(peripheral),
                characteristicIdentity: ObjectIdentifier(characteristic),
                kind: .notificationState
            )
        )
    }

    func takeSafetyFault() -> BleEventError? {
        defer {
            pendingSafetyFault = nil
        }
        return pendingSafetyFault
    }

    func drain() {
        queuesByConnectionId.removeAll(keepingCapacity: false)
        staleCallbackBarriers.removeAll(keepingCapacity: false)
        valueRouteBarriers.removeAll(keepingCapacity: false)
        safetyFaultLatched = false
        pendingSafetyFault = nil
        nextSubscriptionGeneration = 1
    }

    private func enqueueNotificationEnable(
        command: SetNotificationsCommand,
        request: NotificationModeRequest,
        connection: AppleConnectionCorrelation,
        target: AppleGattCharacteristicTarget
    ) throws {
        try rejectStaleCallbackBarrier(
            callbackKey(
                connection: connection,
                characteristic: target.characteristic,
                kind: .read
            )
        )
        var queue = try queueForMutation(connection: connection)
        try rejectFullOperationQueue(queue)
        guard queue.subscriptions[command.subscriptionId] == nil,
              !queue.operations.contains(where: {
                  $0.notificationCommand?.subscriptionId == command.subscriptionId
              })
        else {
            throw CommandDispatchError.InvalidRequest(
                detail: "the subscription ID is currently active or transitioning for this connection"
            )
        }
        guard queue.subscriptionByCharacteristic[command.characteristicId] == nil,
              !queue.operations.contains(where: {
                  isNotificationOperation($0, characteristic: target.characteristic)
              })
        else {
            throw CommandDispatchError.InvalidState(
                detail: "the characteristic already has a notification subscription route"
            )
        }
        guard !queue.operations.contains(where: {
            isReadOperation($0, characteristic: target.characteristic)
        }) else {
            throw CommandDispatchError.InvalidState(
                detail: "a characteristic read is active or queued for the notification target"
            )
        }

        let selection = Self.selectNotificationMode(
            request: request,
            properties: target.characteristic.appleGattProperties
        )
        switch selection {
        case .noSupportedMode:
            throw CommandDispatchError.InvalidRequest(
                detail: "characteristic advertises neither Notify nor Indicate"
            )

        case .strictUnsupported(let requestedMode):
            queue.operations.append(
                .setNotifications(
                    command: command,
                    characteristic: target.characteristic,
                    transition: .unsupported(requestedMode: requestedMode)
                )
            )
            queuesByConnectionId[connection.connectionId] = queue

        case .supported(let actualMode):
            try rejectStaleCallbackBarrier(
                callbackKey(
                    connection: connection,
                    characteristic: target.characteristic,
                    kind: .notificationState
                )
            )
            let correlation = SubscriptionCorrelation(
                connection: connection,
                generation: try allocateSubscriptionGeneration(),
                gattSessionId: command.gattSessionId,
                subscriptionId: command.subscriptionId,
                characteristicId: command.characteristicId
            )
            queue.subscriptions[command.subscriptionId] = SubscriptionRoute(
                correlation: correlation,
                phase: .queuedEnable(commandId: command.commandId),
                actualMode: actualMode,
                characteristic: target.characteristic
            )
            queue.subscriptionByCharacteristic[command.characteristicId] = command.subscriptionId
            queue.operations.append(
                .setNotifications(
                    command: command,
                    characteristic: target.characteristic,
                    transition: .enable(correlation: correlation, actualMode: actualMode)
                )
            )
            queuesByConnectionId[connection.connectionId] = queue
        }
    }

    private func enqueueNotificationDisable(
        command: SetNotificationsCommand,
        connection: AppleConnectionCorrelation,
        target: AppleGattCharacteristicTarget
    ) throws {
        var queue = try queueForMutation(connection: connection)
        try rejectFullOperationQueue(queue)
        guard var route = queue.subscriptions[command.subscriptionId] else {
            throw CommandDispatchError.InvalidState(
                detail: "the subscription ID is unknown or no longer active"
            )
        }
        guard subscriptionCorrelationMatchesCommand(route.correlation, command: command),
              ObjectIdentifier(route.characteristic) == ObjectIdentifier(target.characteristic)
        else {
            throw CommandDispatchError.InvalidRequest(
                detail: "the subscription scope does not match the active route"
            )
        }
        guard case .active = route.phase else {
            throw CommandDispatchError.InvalidState(
                detail: "the subscription is not active or is already transitioning"
            )
        }
        try rejectStaleCallbackBarrier(
            callbackKey(
                connection: connection,
                characteristic: target.characteristic,
                kind: .notificationState
            )
        )
        route.phase = .queuedDisable(commandId: command.commandId)
        queue.subscriptions[command.subscriptionId] = route
        queue.operations.append(
            .setNotifications(
                command: command,
                characteristic: target.characteristic,
                transition: .disable(correlation: route.correlation)
            )
        )
        queuesByConnectionId[connection.connectionId] = queue
    }

    private func validateTarget(
        connectionId: ConnectionId,
        gattSessionId: GattSessionId,
        characteristicId: GattCharacteristicId,
        connection: AppleConnectionCorrelation,
        target: AppleGattCharacteristicTarget
    ) throws {
        guard connectionId == connection.connectionId else {
            throw CommandDispatchError.InvalidState(
                detail: "the Apple GATT I/O command does not match the active connection"
            )
        }
        guard target.connectionGeneration == connection.generation,
              target.gattSessionId == gattSessionId,
              target.characteristicId == characteristicId
        else {
            throw CommandDispatchError.InvalidState(
                detail: "the Apple GATT characteristic target is stale or does not match the command"
            )
        }
    }

    private func queueForMutation(
        connection: AppleConnectionCorrelation
    ) throws -> ConnectionQueue {
        if let queue = queuesByConnectionId[connection.connectionId] {
            guard connectionsMatch(queue.connection, connection) else {
                throw CommandDispatchError.InvalidState(
                    detail: "the Apple GATT I/O queue belongs to a stale connection lifecycle"
                )
            }
            return queue
        }
        return ConnectionQueue(
            connection: connection,
            operations: OperationQueue(),
            activeStage: nil,
            subscriptions: [:],
            subscriptionByCharacteristic: [:]
        )
    }

    private func enqueue(
        _ operation: Operation,
        connection: AppleConnectionCorrelation
    ) throws {
        if let callbackKind = operation.callbackKind {
            try rejectStaleCallbackBarrier(
                callbackKey(
                    connection: connection,
                    characteristic: operation.characteristic,
                    kind: callbackKind
                )
            )
        }

        var queue = try queueForMutation(connection: connection)
        try rejectFullOperationQueue(queue)
        queue.operations.append(operation)
        queuesByConnectionId[connection.connectionId] = queue
    }

    private func matchingQueue(
        connection: AppleConnectionCorrelation,
        callbackPeripheral: (any ApplePeripheralObject)? = nil
    ) -> ConnectionQueue? {
        guard let queue = queuesByConnectionId[connection.connectionId],
              connectionsMatch(queue.connection, connection)
        else {
            return nil
        }
        if let callbackPeripheral,
           ObjectIdentifier(connection.peripheral) != ObjectIdentifier(callbackPeripheral)
        {
            return nil
        }
        return queue
    }

    private func connectionsMatch(
        _ lhs: AppleConnectionCorrelation,
        _ rhs: AppleConnectionCorrelation
    ) -> Bool {
        lhs.generation == rhs.generation &&
            lhs.connectionId == rhs.connectionId &&
            ObjectIdentifier(lhs.peripheral) == ObjectIdentifier(rhs.peripheral)
    }

    private func subscriptionCorrelationsMatch(
        _ lhs: SubscriptionCorrelation,
        _ rhs: SubscriptionCorrelation
    ) -> Bool {
        connectionsMatch(lhs.connection, rhs.connection) &&
            lhs.generation == rhs.generation &&
            lhs.gattSessionId == rhs.gattSessionId &&
            lhs.subscriptionId == rhs.subscriptionId &&
            lhs.characteristicId == rhs.characteristicId
    }

    private func subscriptionCorrelationMatchesCommand(
        _ correlation: SubscriptionCorrelation,
        command: SetNotificationsCommand
    ) -> Bool {
        correlation.connection.connectionId == command.connectionId &&
            correlation.gattSessionId == command.gattSessionId &&
            correlation.subscriptionId == command.subscriptionId &&
            correlation.characteristicId == command.characteristicId
    }

    private func isReadOperation(
        _ operation: Operation,
        characteristic: any AppleGattCharacteristicObject
    ) -> Bool {
        guard case .read(_, let candidate) = operation else {
            return false
        }
        return ObjectIdentifier(candidate) == ObjectIdentifier(characteristic)
    }

    private func isNotificationOperation(
        _ operation: Operation,
        characteristic: any AppleGattCharacteristicObject
    ) -> Bool {
        guard case .setNotifications(_, let candidate, _) = operation else {
            return false
        }
        return ObjectIdentifier(candidate) == ObjectIdentifier(characteristic)
    }

    private func matchingSubscriptionRoute(
        connection: AppleConnectionCorrelation,
        peripheral: any ApplePeripheralObject,
        characteristic: any AppleGattCharacteristicObject
    ) -> SubscriptionRoute? {
        guard ObjectIdentifier(connection.peripheral) == ObjectIdentifier(peripheral),
              let queue = matchingQueue(
                  connection: connection,
                  callbackPeripheral: peripheral
              )
        else {
            return nil
        }
        return queue.subscriptions.values.first { route in
            subscriptionCorrelationsMatch(
                route.correlation,
                SubscriptionCorrelation(
                    connection: connection,
                    generation: route.correlation.generation,
                    gattSessionId: route.correlation.gattSessionId,
                    subscriptionId: route.correlation.subscriptionId,
                    characteristicId: route.correlation.characteristicId
                )
            ) &&
                ObjectIdentifier(route.characteristic) == ObjectIdentifier(characteristic)
        }
    }

    private func subscriptionAcceptsValue(_ phase: SubscriptionPhase) -> Bool {
        switch phase {
        case .enabling, .active, .queuedDisable, .disabling,
             .rediscoveryQueued, .rediscoveryDisabling:
            return true
        case .queuedEnable:
            return false
        }
    }

    private func notificationRouteMayBeActiveNatively(_ phase: SubscriptionPhase) -> Bool {
        switch phase {
        case .enabling, .active, .queuedDisable, .disabling,
             .rediscoveryQueued, .rediscoveryDisabling:
            return true
        case .queuedEnable:
            return false
        }
    }

    private func notificationStateError(
        callbackError: BleEventError?,
        expectedIsNotifying: Bool,
        observedIsNotifying: Bool?
    ) -> BleEventError? {
        if let callbackError {
            return callbackError
        }
        guard let observedIsNotifying, observedIsNotifying != expectedIsNotifying else {
            return nil
        }
        return BleEventError(
            category: .gatt,
            message: "CoreBluetooth notification state callback reported an unexpected isNotifying value",
            platformDetails: nil
        )
    }

    private func removeSubscription(
        correlation: SubscriptionCorrelation,
        from queue: inout ConnectionQueue
    ) {
        guard let route = queue.subscriptions[correlation.subscriptionId],
              subscriptionCorrelationsMatch(route.correlation, correlation)
        else {
            return
        }
        queue.subscriptions.removeValue(forKey: correlation.subscriptionId)
        if queue.subscriptionByCharacteristic[correlation.characteristicId] == correlation.subscriptionId {
            queue.subscriptionByCharacteristic.removeValue(forKey: correlation.characteristicId)
        }
    }

    private func allocateSubscriptionGeneration() throws -> UInt64 {
        guard nextSubscriptionGeneration < UInt64.max else {
            throw CommandDispatchError.BackendUnavailable(
                detail: "Apple GATT subscription generation space is exhausted",
                platformDetails: nil
            )
        }
        let generation = nextSubscriptionGeneration
        nextSubscriptionGeneration += 1
        return generation
    }

    private func callbackKey(
        connection: AppleConnectionCorrelation,
        characteristic: any AppleGattCharacteristicObject,
        kind: CallbackKind
    ) -> CallbackKey {
        CallbackKey(
            peripheralIdentity: ObjectIdentifier(connection.peripheral),
            characteristicIdentity: ObjectIdentifier(characteristic),
            kind: kind
        )
    }

    private func valueRouteKey(
        connection: AppleConnectionCorrelation,
        characteristic: any AppleGattCharacteristicObject
    ) -> ValueRouteKey {
        ValueRouteKey(
            peripheralIdentity: ObjectIdentifier(connection.peripheral),
            characteristicIdentity: ObjectIdentifier(characteristic)
        )
    }

    private func valueRouteIsBlocked(
        peripheral: any ApplePeripheralObject,
        characteristic: any AppleGattCharacteristicObject
    ) -> Bool {
        valueRouteBarriers[
            ValueRouteKey(
                peripheralIdentity: ObjectIdentifier(peripheral),
                characteristicIdentity: ObjectIdentifier(characteristic)
            )
        ] != nil
    }

    private func rejectValueRouteBarrier(
        connection: AppleConnectionCorrelation,
        characteristic: any AppleGattCharacteristicObject
    ) throws {
        guard valueRouteBarriers[valueRouteKey(
            connection: connection,
            characteristic: characteristic
        )] == nil
        else {
            throw CommandDispatchError.InvalidState(
                detail: "a retired CoreBluetooth notification value route has no safe reuse boundary"
            )
        }
    }

    private func valueRouteBarrier(
        peripheral: any ApplePeripheralObject,
        characteristic: any AppleGattCharacteristicObject
    ) -> ValueRouteBarrier? {
        valueRouteBarriers[
            ValueRouteKey(
                peripheralIdentity: ObjectIdentifier(peripheral),
                characteristicIdentity: ObjectIdentifier(characteristic)
            )
        ]
    }

    private func addValueRouteBarrier(
        correlation: SubscriptionCorrelation,
        characteristic: any AppleGattCharacteristicObject
    ) {
        let key = valueRouteKey(
            connection: correlation.connection,
            characteristic: characteristic
        )
        guard !safetyFaultLatched else {
            return
        }
        if valueRouteBarriers[key] != nil {
            valueRouteBarriers[key] = ValueRouteBarrier(
                correlation: correlation,
                characteristic: characteristic
            )
            return
        }
        guard valueRouteBarriers.count < Self.maximumRetainedBarrierRoutes else {
            return
        }
        valueRouteBarriers[key] = ValueRouteBarrier(
            correlation: correlation,
            characteristic: characteristic
        )
        if valueRouteBarriers.count > Self.maximumSafeBarrierRoutes {
            latchSafetyFault(
                "Apple GATT retired notification value routes exceeded safe bounded capacity"
            )
        }
    }

    private func removeValueRouteBarrier(
        connection: AppleConnectionCorrelation,
        characteristic: any AppleGattCharacteristicObject
    ) {
        valueRouteBarriers.removeValue(
            forKey: valueRouteKey(connection: connection, characteristic: characteristic)
        )
    }

    private func rejectStaleCallbackBarrier(_ key: CallbackKey) throws {
        guard staleCallbackBarriers[key] == nil
        else {
            throw CommandDispatchError.InvalidState(
                detail: "a retired CoreBluetooth callback route has not reached a safe reuse boundary"
            )
        }
    }

    private func addStaleCallbackBarrier(
        _ key: CallbackKey,
        peripheral: any ApplePeripheralObject,
        characteristic: any AppleGattCharacteristicObject
    ) {
        guard !safetyFaultLatched,
              staleCallbackBarriers[key] == nil,
              staleCallbackBarriers.count < Self.maximumRetainedBarrierRoutes
        else {
            return
        }
        staleCallbackBarriers[key] = CallbackBarrier(
            peripheral: peripheral,
            characteristic: characteristic
        )
        if staleCallbackBarriers.count > Self.maximumSafeBarrierRoutes {
            latchSafetyFault(
                "Apple GATT retired callback routes exceeded safe bounded capacity"
            )
        }
    }

    private func consumeStaleCallbackBarrier(_ key: CallbackKey) -> Bool {
        staleCallbackBarriers.removeValue(forKey: key) != nil
    }

    private func rediscoverySafetyFault(
        _ message: String
    ) -> AppleGattRediscoveryPreparation {
        latchSafetyFault(message)
        return .unsafe(
            BleEventError(
                category: .internal,
                message: message,
                platformDetails: nil
            )
        )
    }

    private func rejectSafetyFault() throws {
        guard !safetyFaultLatched else {
            throw CommandDispatchError.InvalidState(
                detail: "Apple GATT I/O is unavailable after a callback-route safety fault"
            )
        }
    }

    private func rejectFullOperationQueue(_ queue: ConnectionQueue) throws {
        guard queue.operations.count < Self.maximumPendingOperationsPerConnection else {
            throw CommandDispatchError.InvalidState(
                detail: "Apple GATT I/O queue reached the per-connection limit of \(Self.maximumPendingOperationsPerConnection) operations"
            )
        }
    }

    private func latchSafetyFault(_ message: String) {
        guard !safetyFaultLatched else {
            return
        }
        safetyFaultLatched = true
        pendingSafetyFault = BleEventError(
            category: .internal,
            message: message,
            platformDetails: nil
        )
    }

    private func removeFirstOperation(from queue: inout ConnectionQueue) {
        queue.operations.removeFirst()
        queue.activeStage = nil
    }

    private func store(_ queue: ConnectionQueue) {
        if queue.operations.isEmpty && queue.subscriptions.isEmpty {
            queuesByConnectionId.removeValue(forKey: queue.connection.connectionId)
        } else {
            queuesByConnectionId[queue.connection.connectionId] = queue
        }
    }

    private func platformWriteLengthError(
        actual: Int,
        maximum: Int,
        writeType: WriteType
    ) -> BleEventError {
        let kind = writeType == .withResponse ? "with-response" : "without-response"
        return BleEventError(
            category: .gatt,
            message: "value contains \(actual) bytes; CoreBluetooth \(kind) maximum is \(maximum)",
            platformDetails: nil
        )
    }
}
