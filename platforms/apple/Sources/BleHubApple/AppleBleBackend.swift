import BleHubUniFfi
import CoreBluetooth
import Dispatch
import Foundation
import OSLog

extension CBPeripheral: ApplePeripheralObject {}

private enum AppleBackendLease {
    private static let lock = NSLock()
    private static var leased = false

    static func acquire() -> Bool {
        lock.withLock {
            guard !leased else {
                return false
            }
            leased = true
            return true
        }
    }

    static func release() {
        lock.withLock {
            precondition(leased, "Apple backend process lease released more than once")
            leased = false
        }
    }
}

public final class AppleBleBackend: NSObject, BleBackend, @unchecked Sendable {
    public static let contractVersion: UInt32 = 5

    private static let logger = Logger(subsystem: "dev.blehub", category: "AppleBleBackend")

    private let queue: DispatchQueue
    private let queueKey = DispatchSpecificKey<UInt8>()
    private let queueIdentity: UInt8 = 1

    private var lifecycle = AppleBackendLifecycle.inert
    private var ingress: BackendEventIngressHandle?
    private var centralManager: CBCentralManager?
    private let scanController = AppleScanController()
    private let connectionController = AppleConnectionController()
    private let gattController = AppleGattController()
    private let gattIoController = AppleGattIoController()
    private let rssiController = AppleRssiController()
    private var ownsLease = true

    /// All CoreBluetooth objects, callbacks, and mutable backend state use this private serial queue.
    public init(queueLabel: String = "dev.blehub.apple.backend") throws {
        queue = DispatchQueue(label: queueLabel)
        guard AppleBackendLease.acquire() else {
            throw BleHubError.ResourceBusy(
                detail: "another Apple backend owns the process CoreBluetooth controller lease"
            )
        }
        super.init()
        queue.setSpecific(key: queueKey, value: queueIdentity)
    }

    deinit {
        try? dispose()
    }

    public func capabilities() throws -> BackendCapabilities {
        BackendCapabilities(
            platform: .apple,
            contractVersion: Self.contractVersion,
            supportsDuplicateScanResults: true,
            supportsConnectableFlag: true,
            supportsReadRssi: true,
            supportsRequiredNotification: true,
            supportsRequiredIndication: true,
            reportsActualNotificationMode: true
        )
    }

    @_spi(AppleDiagnostics) public func diagnosticPeripheralId(
        for nativeIdentifier: UUID
    ) -> PeripheralId? {
        onQueue {
            scanController.existingPeripheralId(for: nativeIdentifier)
        }
    }

    public func bindEventIngress(ingress: BackendEventIngressHandle) throws {
        try onQueue {
            guard lifecycle == .inert, self.ingress == nil else {
                throw BleHubError.BackendAssembly(
                    detail: "Apple backend event ingress may only be bound once while inert"
                )
            }
            self.ingress = ingress
        }
    }

    public func initialize(command: InitializeCommand) throws {
        try onQueue {
            if lifecycle == .closed {
                throw CommandDispatchError.Closed
            }
            guard ingress != nil else {
                throw CommandDispatchError.InvalidState(
                    detail: "Apple backend event ingress is not bound"
                )
            }
            try lifecycle.beginInitialization(commandId: command.commandId)
            queue.async { [weak self] in
                self?.createCentralManagerIfInitializing()
            }
        }
    }

    public func startScan(command: StartScanCommand) throws {
        try onQueue {
            try lifecycle.requireReady(operation: "start scan")
            guard let centralManager else {
                throw CommandDispatchError.BackendUnavailable(
                    detail: "Apple CoreBluetooth manager is unavailable",
                    platformDetails: nil
                )
            }
            if let error = AppleEventMapper.scanAvailabilityError(
                state: centralManager.state,
                authorization: CBManager.authorization
            ) {
                push(.commandCompleted(commandId: command.commandId, error: error))
                return
            }

            let services = try scanController.begin(command: command)
            centralManager.scanForPeripherals(
                withServices: services,
                options: [
                    CBCentralManagerScanOptionAllowDuplicatesKey: command.options.allowDuplicates,
                ]
            )
            push(
                .scanStarted(commandId: command.commandId, scanId: command.scanSessionId),
                .commandCompleted(commandId: command.commandId, error: nil)
            )
        }
    }

    public func stopScan(command: StopScanCommand) throws {
        try onQueue {
            try lifecycle.requireReady(operation: "stop scan")
            guard let centralManager else {
                throw CommandDispatchError.BackendUnavailable(
                    detail: "Apple CoreBluetooth manager is unavailable",
                    platformDetails: nil
                )
            }
            switch try scanController.stop(command: command) {
            case .stopped(let scanSessionId):
                centralManager.stopScan()
                push(
                    .scanStopped(
                        scanId: scanSessionId,
                        result: .ok(reason: .requested(commandId: command.commandId))
                    ),
                    .commandCompleted(commandId: command.commandId, error: nil)
                )
            case .alreadyStopped:
                push(.commandCompleted(commandId: command.commandId, error: nil))
            }
        }
    }

    public func connect(command: ConnectCommand) throws {
        try onQueue {
            try lifecycle.requireReady(operation: "connect")
            guard let centralManager else {
                throw CommandDispatchError.BackendUnavailable(
                    detail: "Apple CoreBluetooth manager is unavailable",
                    platformDetails: nil
                )
            }
            guard let peripheral = scanController.peripheral(for: command.peripheralId) else {
                throw CommandDispatchError.InvalidRequest(
                    detail: "the peripheral ID was not discovered by this Apple backend"
                )
            }
            if let error = AppleEventMapper.connectionAvailabilityError(
                state: centralManager.state,
                authorization: CBManager.authorization
            ) {
                push(.commandCompleted(commandId: command.commandId, error: error))
                return
            }

            _ = try connectionController.begin(
                command: command,
                peripheral: peripheral
            )
            centralManager.connect(peripheral, options: nil)
            push(
                .connectionStateChanged(
                    commandId: command.commandId,
                    connectionId: command.connectionId,
                    result: .ok(state: .connecting)
                )
            )
        }
    }

    public func disconnect(command: DisconnectCommand) throws {
        try onQueue {
            try lifecycle.requireReady(operation: "disconnect")
            guard let centralManager else {
                throw CommandDispatchError.BackendUnavailable(
                    detail: "Apple CoreBluetooth manager is unavailable",
                    platformDetails: nil
                )
            }
            let correlation = try connectionController.beginDisconnect(command: command)
            guard let peripheral = correlation.peripheral as? CBPeripheral else {
                throw CommandDispatchError.BackendUnavailable(
                    detail: "Apple peripheral ownership disappeared during disconnect dispatch",
                    platformDetails: nil
                )
            }
            let retirementError = AppleEventMapper.connectionError(
                nil,
                context: "the connection was retired during CoreBluetooth GATT activity"
            )
            let gattRetirement = gattIoController.retireAll(
                connection: correlation,
                error: retirementError
            )
            let rssiCompletion = rssiController.retire(
                connection: correlation,
                error: retirementError
            )
            let pendingGattCommand = gattController.retire(connection: correlation)
            bestEffortUnsubscribe(gattRetirement, peripheral: peripheral)
            peripheral.delegate = nil
            centralManager.cancelPeripheralConnection(peripheral)
            var events = gattRetirementEvents(gattRetirement)
            if let rssiCompletion {
                events.append(contentsOf: rssiEvents(rssiCompletion))
            }
            if let pendingGattCommand {
                events.append(gattFailureEvent(command: pendingGattCommand, error: retirementError))
            }
            push(events)
            _ = handleGattSafetyFaultIfNeeded()
        }
    }

    public func discoverGatt(command: DiscoverGattCommand) throws {
        try onQueue {
            try lifecycle.requireReady(operation: "discover GATT")
            guard let centralManager else {
                throw CommandDispatchError.BackendUnavailable(
                    detail: "Apple CoreBluetooth manager is unavailable",
                    platformDetails: nil
                )
            }
            guard let correlation = connectionController.connectedCorrelation(
                for: command.connectionId
            ) else {
                throw CommandDispatchError.InvalidState(
                    detail: "connection lifecycle is not connected"
                )
            }
            guard let peripheral = correlation.peripheral as? CBPeripheral else {
                throw CommandDispatchError.BackendUnavailable(
                    detail: "Apple peripheral ownership disappeared during GATT dispatch",
                    platformDetails: nil
                )
            }
            if let error = AppleEventMapper.connectionAvailabilityError(
                state: centralManager.state,
                authorization: CBManager.authorization
            ) {
                push(.commandCompleted(commandId: command.commandId, error: error))
                return
            }
            guard !gattIoController.hasPending(connection: correlation) else {
                throw CommandDispatchError.InvalidState(
                    detail: "Apple GATT discovery cannot replace a database while I/O is pending"
                )
            }

            _ = try gattController.begin(command: command, connection: correlation)
            peripheral.delegate = self
            switch gattIoController.prepareSubscriptionsForRediscovery(connection: correlation) {
            case .ready:
                startPendingGattDiscoveryIfReady(
                    connection: correlation,
                    peripheral: peripheral
                )
            case .unsubscribeQueued:
                driveGattIo(connection: correlation, peripheral: peripheral)
            case .unsafe(let error):
                if !handleGattSafetyFaultIfNeeded() {
                    failCurrentGattDiscovery(connection: correlation, error: error)
                }
            }
        }
    }

    public func readCharacteristic(command: ReadCharacteristicCommand) throws {
        try onQueue {
            guard let (connection, peripheral, target) = try resolveGattIoTarget(
                connectionId: command.connectionId,
                gattSessionId: command.gattSessionId,
                characteristicId: command.characteristicId,
                commandId: command.commandId,
                operation: "read characteristic"
            ) else {
                return
            }
            try gattIoController.enqueueRead(
                command: command,
                connection: connection,
                target: target
            )
            driveGattIo(connection: connection, peripheral: peripheral)
        }
    }

    public func writeCharacteristic(command: WriteCharacteristicCommand) throws {
        try onQueue {
            guard let (connection, peripheral, target) = try resolveGattIoTarget(
                connectionId: command.connectionId,
                gattSessionId: command.gattSessionId,
                characteristicId: command.characteristicId,
                commandId: command.commandId,
                operation: "write characteristic"
            ) else {
                return
            }
            try gattIoController.enqueueWrite(
                command: command,
                connection: connection,
                target: target
            )
            driveGattIo(connection: connection, peripheral: peripheral)
        }
    }

    public func setNotifications(command: SetNotificationsCommand) throws {
        try onQueue {
            guard let (connection, peripheral, target) = try resolveGattIoTarget(
                connectionId: command.connectionId,
                gattSessionId: command.gattSessionId,
                characteristicId: command.characteristicId,
                commandId: command.commandId,
                operation: "set notifications"
            ) else {
                return
            }
            try gattIoController.enqueueSetNotifications(
                command: command,
                connection: connection,
                target: target
            )
            driveGattIo(connection: connection, peripheral: peripheral)
        }
    }

    public func readRssi(command: ReadRssiCommand) throws {
        try onQueue {
            try lifecycle.requireReady(operation: "read RSSI")
            guard let centralManager else {
                throw CommandDispatchError.BackendUnavailable(
                    detail: "Apple CoreBluetooth manager is unavailable",
                    platformDetails: nil
                )
            }
            guard let connection = connectionController.connectedCorrelation(
                for: command.connectionId
            ) else {
                throw CommandDispatchError.InvalidState(
                    detail: "connection lifecycle is not connected"
                )
            }
            guard let peripheral = connection.peripheral as? CBPeripheral else {
                throw CommandDispatchError.BackendUnavailable(
                    detail: "Apple peripheral ownership disappeared during RSSI dispatch",
                    platformDetails: nil
                )
            }
            if let error = AppleEventMapper.connectionAvailabilityError(
                state: centralManager.state,
                authorization: CBManager.authorization
            ) {
                push(.commandCompleted(commandId: command.commandId, error: error))
                return
            }
            try rssiController.begin(command: command, connection: connection)
            peripheral.delegate = self
            peripheral.readRSSI()
        }
    }

    public func dispose() throws {
        let releaseLease = onQueue {
            guard lifecycle != .closed else {
                return false
            }

            lifecycle = .closed
            ingress = nil
            let wasScanning = scanController.activeSessionId != nil
            let connectionRoutes = connectionController.drain()
            _ = gattController.drain()
            let connectedPeripherals = connectionRoutes.compactMap {
                route -> (CBPeripheral, AppleGattRetirement)? in
                guard let peripheral = route.correlation.peripheral as? CBPeripheral else {
                    return nil
                }
                _ = rssiController.retire(
                    connection: route.correlation,
                    error: AppleEventMapper.connectionError(
                        nil,
                        context: "the Apple backend was disposed during an RSSI read"
                    )
                )
                let retirement = gattIoController.retireAll(
                    connection: route.correlation,
                    error: AppleEventMapper.connectionError(
                        nil,
                        context: "the Apple backend was disposed during GATT activity"
                    )
                )
                return (peripheral, retirement)
            }
            rssiController.drain()
            gattIoController.drain()
            scanController.dispose()
            if let centralManager {
                if wasScanning {
                    centralManager.stopScan()
                }
                for (peripheral, retirement) in connectedPeripherals {
                    if centralManager.state == .poweredOn {
                        bestEffortUnsubscribe(retirement, peripheral: peripheral)
                    }
                    peripheral.delegate = nil
                    if centralManager.state == .poweredOn {
                        centralManager.cancelPeripheralConnection(peripheral)
                    }
                }
                centralManager.delegate = nil
            } else {
                for (peripheral, _) in connectedPeripherals {
                    peripheral.delegate = nil
                }
            }
            centralManager = nil

            let releaseLease = ownsLease
            ownsLease = false
            return releaseLease
        }
        if releaseLease {
            AppleBackendLease.release()
        }
    }

    private func createCentralManagerIfInitializing() {
        guard case .initializing = lifecycle else {
            return
        }
        centralManager = CBCentralManager(
            delegate: self,
            queue: queue,
            options: [CBCentralManagerOptionShowPowerAlertKey: false]
        )
    }


    private func gattFailureEvent(
        command: DiscoverGattCommand,
        error: BleEventError
    ) -> BleEvent {
        .commandCompleted(commandId: command.commandId, error: error)
    }

    private func resolveGattIoTarget(
        connectionId: ConnectionId,
        gattSessionId: GattSessionId,
        characteristicId: GattCharacteristicId,
        commandId: CommandId,
        operation: String
    ) throws -> (AppleConnectionCorrelation, CBPeripheral, AppleGattCharacteristicTarget)? {
        try lifecycle.requireReady(operation: operation)
        guard let centralManager else {
            throw CommandDispatchError.BackendUnavailable(
                detail: "Apple CoreBluetooth manager is unavailable",
                platformDetails: nil
            )
        }
        guard let connection = connectionController.connectedCorrelation(for: connectionId) else {
            throw CommandDispatchError.InvalidState(
                detail: "connection lifecycle is not connected"
            )
        }
        guard let peripheral = connection.peripheral as? CBPeripheral else {
            throw CommandDispatchError.BackendUnavailable(
                detail: "Apple peripheral ownership disappeared during GATT I/O dispatch",
                platformDetails: nil
            )
        }
        if let error = AppleEventMapper.connectionAvailabilityError(
            state: centralManager.state,
            authorization: CBManager.authorization
        ) {
            push(.commandCompleted(commandId: commandId, error: error))
            return nil
        }
        let target = try gattController.resolveCharacteristic(
            connection: connection,
            gattSessionId: gattSessionId,
            characteristicId: characteristicId
        )
        guard target.characteristic is CBCharacteristic else {
            throw CommandDispatchError.BackendUnavailable(
                detail: "Apple GATT characteristic routing lost its CoreBluetooth object",
                platformDetails: nil
            )
        }
        peripheral.delegate = self
        return (connection, peripheral, target)
    }

    private func driveGattIo(
        connection: AppleConnectionCorrelation,
        peripheral: CBPeripheral
    ) {
        while true {
            let ioAction: AppleGattIoAction?
            do {
                ioAction = try gattIoController.nextAction(
                    connection: connection,
                    canSendWriteWithoutResponse: peripheral.canSendWriteWithoutResponse,
                    maximumWriteWithResponse: peripheral.maximumWriteValueLength(for: .withResponse),
                    maximumWriteWithoutResponse: peripheral.maximumWriteValueLength(for: .withoutResponse)
                )
            } catch {
                failGattIoQueue(
                    connection: connection,
                    context: "Apple GATT I/O state transition failed"
                )
                return
            }

            if let ioAction {
                switch ioAction {
                case .terminal(let completion):
                    push(gattIoEvents(completion))
                case .read(_, let actionConnection, let characteristicObject):
                    guard let characteristic = characteristicObject as? CBCharacteristic else {
                        failGattIoQueue(
                            connection: actionConnection,
                            context: "Apple GATT read routing lost its CoreBluetooth characteristic"
                        )
                        return
                    }
                    peripheral.readValue(for: characteristic)
                    return
                case .writeWithResponse(
                    let command,
                    let actionConnection,
                    let characteristicObject
                ):
                    guard let characteristic = characteristicObject as? CBCharacteristic else {
                        failGattIoQueue(
                            connection: actionConnection,
                            context: "Apple GATT write routing lost its CoreBluetooth characteristic"
                        )
                        return
                    }
                    peripheral.writeValue(
                        Data(command.value),
                        for: characteristic,
                        type: .withResponse
                    )
                    return
                case .writeWithoutResponse(
                    let command,
                    let actionConnection,
                    let characteristicObject
                ):
                    guard let characteristic = characteristicObject as? CBCharacteristic else {
                        failGattIoQueue(
                            connection: actionConnection,
                            context: "Apple GATT write routing lost its CoreBluetooth characteristic"
                        )
                        return
                    }
                    peripheral.writeValue(
                        Data(command.value),
                        for: characteristic,
                        type: .withoutResponse
                    )
                    if let completion = gattIoController.didSubmitWriteWithoutResponse(
                        connection: actionConnection,
                        peripheral: peripheral,
                        characteristic: characteristic
                    ) {
                        push(gattIoEvents(completion))
                    }
                }
                continue
            }

            guard let notificationAction = gattIoController.nextNotificationAction(
                connection: connection
            ) else {
                return
            }
            switch notificationAction {
            case .terminal(let completion):
                push(gattNotificationEvents(completion))
            case .setNotifyValue(
                _,
                let actionConnection,
                let characteristicObject,
                let enabled
            ):
                guard let characteristic = characteristicObject as? CBCharacteristic else {
                    failGattIoQueue(
                        connection: actionConnection,
                        context: "Apple notification routing lost its CoreBluetooth characteristic"
                    )
                    return
                }
                peripheral.setNotifyValue(enabled, for: characteristic)
                return
            case .retireForRediscovery(let actionConnection, let characteristicObject):
                guard let characteristic = characteristicObject as? CBCharacteristic else {
                    failGattIoQueue(
                        connection: actionConnection,
                        context: "Apple rediscovery notification teardown lost its CoreBluetooth characteristic"
                    )
                    return
                }
                peripheral.setNotifyValue(false, for: characteristic)
                return
            }
        }
    }

    private func failGattIoQueue(
        connection: AppleConnectionCorrelation,
        context: String
    ) {
        let error = AppleEventMapper.internalError(context)
        let retirement = gattIoController.retireAll(
            connection: connection,
            error: error
        )
        var events = gattRetirementEvents(retirement)
        if let command = gattController.failCurrent(connection: connection) {
            events.append(gattFailureEvent(command: command, error: error))
        }
        push(events)
        _ = handleGattSafetyFaultIfNeeded()
    }

    private func startPendingGattDiscoveryIfReady(
        connection: AppleConnectionCorrelation,
        peripheral: CBPeripheral
    ) {
        guard !gattIoController.hasPending(connection: connection),
              gattController.hasPendingDiscovery(connection: connection)
        else {
            return
        }
        peripheral.discoverServices(nil)
    }

    private func rssiEvents(_ completion: AppleRssiCompletion) -> [BleEvent] {
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

    private func gattIoEvents(_ completion: AppleGattIoCompletion) -> [BleEvent] {
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

    private func gattNotificationEvents(
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

    private func gattRetirementEvents(_ retirement: AppleGattRetirement) -> [BleEvent] {
        retirement.completions.flatMap { completion in
            switch completion {
            case .io(let ioCompletion):
                return gattIoEvents(ioCompletion)
            case .notification(let notificationCompletion):
                return gattNotificationEvents(notificationCompletion)
            }
        }
    }

    private func performGattAction(
        _ action: AppleGattDiscoveryAction,
        peripheral: CBPeripheral,
        connection: AppleConnectionCorrelation
    ) {
        switch action {
        case .discoverCharacteristics(let serviceObject):
            guard let service = serviceObject as? CBService else {
                failCurrentGattDiscovery(
                    connection: connection,
                    error: AppleEventMapper.internalError(
                        "Apple GATT service routing lost its CoreBluetooth object"
                    )
                )
                return
            }
            peripheral.discoverCharacteristics(nil, for: service)
        case .discoverDescriptors(let characteristicObject):
            guard let characteristic = characteristicObject as? CBCharacteristic else {
                failCurrentGattDiscovery(
                    connection: connection,
                    error: AppleEventMapper.internalError(
                        "Apple GATT characteristic routing lost its CoreBluetooth object"
                    )
                )
                return
            }
            peripheral.discoverDescriptors(for: characteristic)
        case .completed(let completion):
            push(
                .gattDiscovered(
                    commandId: completion.correlation.command.commandId,
                    database: completion.database
                ),
                .commandCompleted(
                    commandId: completion.correlation.command.commandId,
                    error: nil
                )
            )
        }
    }

    private func failCurrentGattDiscovery(
        connection: AppleConnectionCorrelation,
        error: BleEventError
    ) {
        guard let command = gattController.failCurrent(connection: connection) else {
            return
        }
        push(gattFailureEvent(command: command, error: error))
    }

    private func handleGattControllerFailure(
        _ error: Error,
        connection: AppleConnectionCorrelation
    ) {
        let mapped: BleEventError
        switch error as? AppleGattControllerError {
        case .attributeIdSpaceExhausted:
            mapped = AppleEventMapper.internalError(
                "Apple GATT attribute ID space is exhausted"
            )
        case .discoveryLimitExceeded(let collection, let maximum, let actual):
            mapped = AppleEventMapper.gattError(
                nil,
                context: "Apple GATT discovery returned \(actual) \(collection.rawValue); the FFI allocation hard limit is \(maximum)"
            )
        case nil:
            mapped = AppleEventMapper.internalError(
                "Apple GATT discovery state transition failed"
            )
        }
        failCurrentGattDiscovery(connection: connection, error: mapped)
    }


    private func handleRssiCallback(
        peripheral: CBPeripheral,
        rssi: NSNumber?,
        error: Error?
    ) {
        if rssiController.consumeRetiredCallback(peripheral: peripheral) {
            return
        }
        guard let connection = connectionController.connectedCorrelation(for: peripheral) else {
            return
        }
        let mappedError = error.map {
            AppleEventMapper.connectionError(
                $0,
                context: "CoreBluetooth RSSI read failed"
            )
        }
        guard let completion = rssiController.didReadRssi(
            connection: connection,
            peripheral: peripheral,
            rssi: rssi,
            error: mappedError
        ) else {
            return
        }
        push(rssiEvents(completion))
    }

    private func bestEffortUnsubscribe(
        _ retirement: AppleGattRetirement,
        peripheral: CBPeripheral
    ) {
        for characteristicObject in retirement.activeNotificationCharacteristicsToUnsubscribe {
            guard let characteristic = characteristicObject as? CBCharacteristic else {
                Self.logger.fault("notification cleanup lost its CoreBluetooth characteristic")
                continue
            }
            peripheral.setNotifyValue(false, for: characteristic)
        }
    }

    @discardableResult
    private func handleGattSafetyFaultIfNeeded() -> Bool {
        guard let error = gattIoController.takeSafetyFault() else {
            return false
        }
        lifecycle.markFaulted()
        var events: [BleEvent] = []
        if let scanSessionId = scanController.stopForUnavailableAdapter() {
            centralManager?.stopScan()
            events.append(.scanStopped(scanId: scanSessionId, result: .err(error: error)))
        }
        for route in connectionController.drain() {
            let connection = route.correlation
            let retirement = gattIoController.retireAll(connection: connection, error: error)
            events.append(contentsOf: gattRetirementEvents(retirement))
            if let rssiCompletion = rssiController.retire(connection: connection, error: error) {
                events.append(contentsOf: rssiEvents(rssiCompletion))
            }
            if let command = gattController.retire(connection: connection) {
                events.append(gattFailureEvent(command: command, error: error))
            }
            if let peripheral = connection.peripheral as? CBPeripheral {
                if centralManager?.state == .poweredOn {
                    bestEffortUnsubscribe(retirement, peripheral: peripheral)
                }
                peripheral.delegate = nil
                if centralManager?.state == .poweredOn {
                    centralManager?.cancelPeripheralConnection(peripheral)
                }
            }
            events.append(
                contentsOf: AppleConnectionEventComposer.unavailable(route: route, error: error)
            )
        }
        _ = gattController.drain()
        events.append(.backendError(error: error))
        push(events)
        return true
    }

    private func push(_ events: BleEvent...) {
        push(events)
    }

    private func push(_ events: [BleEvent]) {
        guard let ingress else {
            return
        }
        for event in events {
            switch ingress.push(event: event) {
            case .delivered:
                continue
            case .closed:
                return
            case .invalidEvent(let message):
                Self.logger.error(
                    "event ingress rejected an invalid Apple event: \(message, privacy: .private)"
                )
            case .downstreamFailed(let message):
                Self.logger.error(
                    "Apple downstream event sink failed: \(message, privacy: .private)"
                )
            }
        }
    }

    private func onQueue<T>(_ action: () throws -> T) rethrows -> T {
        if DispatchQueue.getSpecific(key: queueKey) == queueIdentity {
            return try action()
        }
        return try queue.sync(execute: action)
    }
}

extension AppleBleBackend: CBCentralManagerDelegate {
    public func centralManagerDidUpdateState(_ central: CBCentralManager) {
        guard central === centralManager else {
            return
        }

        let commandId: CommandId?
        switch lifecycle {
        case .initializing:
            commandId = lifecycle.consumeInitialStateCallback()
        case .ready:
            commandId = nil
        case .inert, .faulted, .closed:
            return
        }

        let authorization = CBManager.authorization
        let snapshot = AppleEventMapper.adapterSnapshot(
            state: central.state,
            authorization: authorization
        )
        var events: [BleEvent] = [
            .adapterSnapshotChanged(commandId: commandId, snapshot: snapshot),
        ]
        if let commandId {
            events.append(.commandCompleted(commandId: commandId, error: nil))
        }
        if let error = AppleEventMapper.scanAvailabilityError(
            state: central.state,
            authorization: authorization
        ), let scanSessionId = scanController.stopForUnavailableAdapter() {
            central.stopScan()
            events.append(.scanStopped(scanId: scanSessionId, result: .err(error: error)))
        }
        if let error = AppleEventMapper.connectionAvailabilityError(
            state: central.state,
            authorization: authorization
        ) {
            for route in connectionController.drain() {
                let retirement = gattIoController.retireAll(
                    connection: route.correlation,
                    error: error
                )
                events.append(contentsOf: gattRetirementEvents(retirement))
                if let rssiCompletion = rssiController.retire(
                    connection: route.correlation,
                    error: error
                ) {
                    events.append(contentsOf: rssiEvents(rssiCompletion))
                }
                if let command = gattController.retire(connection: route.correlation) {
                    events.append(gattFailureEvent(command: command, error: error))
                }
                if let peripheral = route.correlation.peripheral as? CBPeripheral {
                    if central.state == .poweredOn {
                        bestEffortUnsubscribe(retirement, peripheral: peripheral)
                    }
                    peripheral.delegate = nil
                    if central.state == .poweredOn {
                        central.cancelPeripheralConnection(peripheral)
                    }
                }
                events.append(
                    contentsOf: AppleConnectionEventComposer.unavailable(
                        route: route,
                        error: error
                    )
                )
            }
            _ = gattController.drain()
        }
        push(events)
        _ = handleGattSafetyFaultIfNeeded()
    }

    public func centralManager(_ central: CBCentralManager, didConnect peripheral: CBPeripheral) {
        guard central === centralManager,
              let disposition = connectionController.didConnect(
                  peripheral: peripheral
              )
        else {
            return
        }
        switch disposition {
        case .connected(let correlation):
            push(
                .connectionStateChanged(
                    commandId: correlation.connectCommandId,
                    connectionId: correlation.connectionId,
                    result: .ok(state: .connected)
                ),
                .commandCompleted(commandId: correlation.connectCommandId, error: nil)
            )
        case .cancelAgain:
            central.cancelPeripheralConnection(peripheral)
        }
    }

    public func centralManager(
        _ central: CBCentralManager,
        didFailToConnect peripheral: CBPeripheral,
        error: Error?
    ) {
        guard central === centralManager,
              let route = connectionController.didFailToConnect(
                  peripheral: peripheral
              )
        else {
            return
        }
        let error = AppleEventMapper.connectionError(
            error,
            context: "CoreBluetooth failed to connect to the peripheral"
        )
        push(AppleConnectionEventComposer.connectFailure(route: route, error: error))
    }

    public func centralManager(
        _ central: CBCentralManager,
        didDisconnectPeripheral peripheral: CBPeripheral,
        error: Error?
    ) {
        guard central === centralManager,
              let route = connectionController.didDisconnect(
                  peripheral: peripheral
              )
        else {
            return
        }
        let correlation = route.correlation
        let connectionEndedError = AppleEventMapper.connectionError(
            error,
            context: "the CoreBluetooth connection ended during native activity"
        )
        let retirement = gattIoController.retireAll(
            connection: correlation,
            error: connectionEndedError
        )
        var events = gattRetirementEvents(retirement)
        if let rssiCompletion = rssiController.retire(
            connection: correlation,
            error: connectionEndedError
        ) {
            events.append(contentsOf: rssiEvents(rssiCompletion))
        }
        if let command = gattController.retire(connection: correlation) {
            events.append(
                gattFailureEvent(
                    command: command,
                    error: AppleEventMapper.connectionError(
                        error,
                        context: "the CoreBluetooth connection ended during GATT discovery"
                    )
                )
            )
        }
        peripheral.delegate = nil
        switch route.phase {
        case .connecting:
            let error = AppleEventMapper.connectionError(
                error,
                context: "CoreBluetooth disconnected before the connection completed"
            )
            events.append(
                contentsOf: AppleConnectionEventComposer.connectFailure(
                    route: route,
                    error: error
                )
            )
        case .connected, .cancellingAfterBackendFailure:
            if let error {
                events.append(
                    .connectionStateChanged(
                        commandId: nil,
                        connectionId: correlation.connectionId,
                        result: .err(
                            error: AppleEventMapper.connectionError(
                                error,
                                context: "CoreBluetooth connection ended unexpectedly"
                            )
                        )
                    )
                )
            } else {
                events.append(
                    .connectionStateChanged(
                        commandId: nil,
                        connectionId: correlation.connectionId,
                        result: .ok(state: .disconnected)
                    )
                )
            }
        case .disconnecting(let commandId, let pendingConnect):
            if pendingConnect {
                events.append(
                    .commandCompleted(
                        commandId: correlation.connectCommandId,
                        error: BleEventError(
                            category: .cancelled,
                            message: "the pending CoreBluetooth connect was cancelled by disconnect",
                            platformDetails: nil
                        )
                    )
                )
            }
            events.append(
                .connectionStateChanged(
                    commandId: commandId,
                    connectionId: correlation.connectionId,
                    result: .ok(state: .disconnected)
                )
            )
            events.append(.commandCompleted(commandId: commandId, error: nil))
        }
        push(events)
        _ = handleGattSafetyFaultIfNeeded()
    }

    public func centralManager(
        _ central: CBCentralManager,
        didDiscover peripheral: CBPeripheral,
        advertisementData: [String: Any],
        rssi RSSI: NSNumber
    ) {
        guard central === centralManager,
              let observation = scanController.observation(
                  peripheral: peripheral,
                  advertisementData: advertisementData,
                  rssi: RSSI
              )
        else {
            return
        }
        push(.advertisementObserved(advertisement: observation))
    }
}

extension AppleBleBackend: CBPeripheralDelegate {
    public func peripheral(_ peripheral: CBPeripheral, didDiscoverServices error: Error?) {
        guard let connection = connectionController.connectedCorrelation(
            for: peripheral
        ) else {
            return
        }
        if let error {
            guard let command = gattController.failServices(connection: connection) else {
                return
            }
            push(
                gattFailureEvent(
                    command: command,
                    error: AppleEventMapper.gattError(
                        error,
                        context: "CoreBluetooth service discovery failed"
                    )
                )
            )
            return
        }
        guard let services = peripheral.services else {
            guard let command = gattController.failServices(connection: connection) else {
                return
            }
            push(
                gattFailureEvent(
                    command: command,
                    error: AppleEventMapper.gattError(
                        nil,
                        context: "CoreBluetooth service discovery returned no service collection"
                    )
                )
            )
            return
        }

        do {
            if let action = try gattController.didDiscoverServices(
                connection: connection,
                services: services.map { $0 as any AppleGattServiceObject }
            ) {
                performGattAction(action, peripheral: peripheral, connection: connection)
            }
        } catch {
            handleGattControllerFailure(error, connection: connection)
        }
    }

    public func peripheral(
        _ peripheral: CBPeripheral,
        didDiscoverCharacteristicsFor service: CBService,
        error: Error?
    ) {
        guard let connection = connectionController.connectedCorrelation(
            for: peripheral
        ) else {
            return
        }
        if let error {
            guard let command = gattController.failCharacteristics(
                connection: connection,
                service: service
            ) else {
                return
            }
            push(
                gattFailureEvent(
                    command: command,
                    error: AppleEventMapper.gattError(
                        error,
                        context: "CoreBluetooth characteristic discovery failed"
                    )
                )
            )
            return
        }
        guard let characteristics = service.characteristics else {
            guard let command = gattController.failCharacteristics(
                connection: connection,
                service: service
            ) else {
                return
            }
            push(
                gattFailureEvent(
                    command: command,
                    error: AppleEventMapper.gattError(
                        nil,
                        context: "CoreBluetooth characteristic discovery returned no collection"
                    )
                )
            )
            return
        }

        do {
            if let action = try gattController.didDiscoverCharacteristics(
                connection: connection,
                service: service,
                characteristics: characteristics.map {
                    $0 as any AppleGattCharacteristicObject
                }
            ) {
                performGattAction(action, peripheral: peripheral, connection: connection)
            }
        } catch {
            handleGattControllerFailure(error, connection: connection)
        }
    }

    public func peripheral(
        _ peripheral: CBPeripheral,
        didDiscoverDescriptorsFor characteristic: CBCharacteristic,
        error: Error?
    ) {
        guard let connection = connectionController.connectedCorrelation(
            for: peripheral
        ) else {
            return
        }
        if let error {
            guard let command = gattController.failDescriptors(
                connection: connection,
                characteristic: characteristic
            ) else {
                return
            }
            push(
                gattFailureEvent(
                    command: command,
                    error: AppleEventMapper.gattError(
                        error,
                        context: "CoreBluetooth descriptor discovery failed"
                    )
                )
            )
            return
        }
        guard let descriptors = characteristic.descriptors else {
            guard let command = gattController.failDescriptors(
                connection: connection,
                characteristic: characteristic
            ) else {
                return
            }
            push(
                gattFailureEvent(
                    command: command,
                    error: AppleEventMapper.gattError(
                        nil,
                        context: "CoreBluetooth descriptor discovery returned no collection"
                    )
                )
            )
            return
        }

        do {
            if let action = try gattController.didDiscoverDescriptors(
                connection: connection,
                characteristic: characteristic,
                descriptors: descriptors.map { $0 as any AppleGattDescriptorObject }
            ) {
                performGattAction(action, peripheral: peripheral, connection: connection)
            }
        } catch {
            handleGattControllerFailure(error, connection: connection)
        }
    }

    public func peripheral(
        _ peripheral: CBPeripheral,
        didUpdateValueFor characteristic: CBCharacteristic,
        error: Error?
    ) {
        if gattIoController.consumeRetiredReadCallback(
            peripheral: peripheral,
            characteristic: characteristic
        ) {
            return
        }
        guard let connection = connectionController.connectedCorrelation(for: peripheral) else {
            return
        }
        let mappedError = error.map {
            AppleEventMapper.gattError(
                $0,
                context: "CoreBluetooth characteristic value update failed"
            )
        }
        let disposition = gattIoController.didUpdateValue(
            connection: connection,
            peripheral: peripheral,
            characteristic: characteristic,
            value: characteristic.value.map { [UInt8]($0) },
            error: mappedError
        )
        switch disposition {
        case .read(let completion):
            push(gattIoEvents(completion))
            driveGattIo(connection: connection, peripheral: peripheral)
        case .notification(let notification):
            push(
                .notificationReceived(
                    connectionId: notification.connectionId,
                    gattSessionId: notification.gattSessionId,
                    subscriptionId: notification.subscriptionId,
                    characteristicId: notification.characteristicId,
                    value: Data(notification.value)
                )
            )
        case .ignored:
            return
        }
    }

    public func peripheral(
        _ peripheral: CBPeripheral,
        didUpdateNotificationStateFor characteristic: CBCharacteristic,
        error: Error?
    ) {
        if gattIoController.consumeRetiredNotificationStateCallback(
            peripheral: peripheral,
            characteristic: characteristic
        ) {
            return
        }
        guard let connection = connectionController.connectedCorrelation(for: peripheral) else {
            return
        }
        let mappedError = error.map {
            AppleEventMapper.gattError(
                $0,
                context: "CoreBluetooth notification state update failed"
            )
        }
        guard let completion = gattIoController.didUpdateNotificationState(
            connection: connection,
            peripheral: peripheral,
            characteristic: characteristic,
            isNotifying: characteristic.isNotifying,
            error: mappedError
        ) else {
            return
        }
        switch completion {
        case .rediscoveryRetired:
            if handleGattSafetyFaultIfNeeded() {
                return
            }
            driveGattIo(connection: connection, peripheral: peripheral)
            startPendingGattDiscoveryIfReady(
                connection: connection,
                peripheral: peripheral
            )
        case .rediscoveryFailed(let error):
            _ = connectionController.beginFailClosedDisconnect(connection: connection)
            let retirement = gattIoController.retireAll(
                connection: connection,
                error: error
            )
            let pendingCommand = gattController.retire(connection: connection)
            bestEffortUnsubscribe(retirement, peripheral: peripheral)
            centralManager?.cancelPeripheralConnection(peripheral)
            var events = gattRetirementEvents(retirement)
            if let pendingCommand {
                events.append(gattFailureEvent(command: pendingCommand, error: error))
            }
            push(events)
            _ = handleGattSafetyFaultIfNeeded()
        case .enabled, .disabled, .failed:
            push(gattNotificationEvents(completion))
            if handleGattSafetyFaultIfNeeded() {
                return
            }
            driveGattIo(connection: connection, peripheral: peripheral)
        }
    }

    public func peripheral(
        _ peripheral: CBPeripheral,
        didWriteValueFor characteristic: CBCharacteristic,
        error: Error?
    ) {
        if gattIoController.consumeRetiredWriteCallback(
            peripheral: peripheral,
            characteristic: characteristic
        ) {
            return
        }
        guard let connection = connectionController.connectedCorrelation(for: peripheral) else {
            return
        }
        let mappedError = error.map {
            AppleEventMapper.gattError(
                $0,
                context: "CoreBluetooth characteristic write with response failed"
            )
        }
        guard let completion = gattIoController.didWriteValue(
            connection: connection,
            peripheral: peripheral,
            characteristic: characteristic,
            error: mappedError
        ) else {
            return
        }
        push(gattIoEvents(completion))
        driveGattIo(connection: connection, peripheral: peripheral)
    }

    public func peripheralIsReady(toSendWriteWithoutResponse peripheral: CBPeripheral) {
        guard let connection = connectionController.connectedCorrelation(for: peripheral),
              gattIoController.didBecomeReadyToSendWriteWithoutResponse(
                  connection: connection,
                  peripheral: peripheral
              )
        else {
            return
        }
        driveGattIo(connection: connection, peripheral: peripheral)
    }

    public func peripheral(
        _ peripheral: CBPeripheral,
        didReadRSSI RSSI: NSNumber,
        error: Error?
    ) {
        handleRssiCallback(peripheral: peripheral, rssi: RSSI, error: error)
    }

    #if os(macOS)
    @available(macOS, introduced: 10.7, deprecated: 10.13)
    public func peripheralDidUpdateRSSI(_ peripheral: CBPeripheral, error: Error?) {
        handleRssiCallback(peripheral: peripheral, rssi: peripheral.rssi, error: error)
    }
    #endif

    public func peripheral(
        _ peripheral: CBPeripheral,
        didModifyServices invalidatedServices: [CBService]
    ) {
        guard !invalidatedServices.isEmpty,
              let connection = connectionController.connectedCorrelation(
                  for: peripheral
              )
        else {
            return
        }
        let invalidationError = AppleEventMapper.gattError(
            nil,
            context: "CoreBluetooth invalidated services during GATT activity"
        )
        let retirement = gattIoController.retireAll(
            connection: connection,
            error: invalidationError
        )
        bestEffortUnsubscribe(retirement, peripheral: peripheral)
        var events = gattRetirementEvents(retirement)
        if let command = gattController.invalidateServices(connection: connection) {
            events.append(gattFailureEvent(command: command, error: invalidationError))
        }
        push(events)
        _ = handleGattSafetyFaultIfNeeded()
    }
}
