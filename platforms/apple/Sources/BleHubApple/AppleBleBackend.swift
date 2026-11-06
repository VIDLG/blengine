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
    public static let contractVersion: UInt32 = blehubContractVersion()

    private static let logger = Logger(subsystem: "dev.blehub", category: "AppleBleBackend")
    private let notificationDiagnostics = AppleNotificationDiagnostics()

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
    private var gattWatchdogs = AppleGattWatchdogRegistry()
    private var ownsLease = true
    private let gattIoWatchdogDelay: DispatchTimeInterval
    private var lastLossDiagnostic: [String: String] = [
        "last_callback": "none",
        "adapter_state": "unknown",
        "retired_operation_count": "0",
        "emitted_event_count": "0",
        "ingress_status": "not-attempted",
    ]
    private var lastIngressStatus = "not-attempted"
    private var diagnosticCentralRecreateCompletion: (@Sendable ([String: String]) -> Void)?

    /// All CoreBluetooth objects, callbacks, and mutable backend state use this private serial queue.
    public init(
        queueLabel: String = "dev.blehub.apple.backend",
        gattIoWatchdogMilliseconds: UInt64 = 10_000
    ) throws {
        guard gattIoWatchdogMilliseconds > 0,
              gattIoWatchdogMilliseconds <= UInt64(Int.max)
        else {
            throw BleHubError.InvalidConfiguration(
                field: "gattIoWatchdogMilliseconds",
                detail: "watchdog duration must fit a positive platform timer interval"
            )
        }
        queue = DispatchQueue(label: queueLabel)
        gattIoWatchdogDelay = .milliseconds(Int(gattIoWatchdogMilliseconds))
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

    /// Read-only private RF diagnostics, ordered with commands and delegates on the
    /// backend queue. Counts adapter-owned logical roots, not OS heap or CB threads.
    /// The ingress count is the retained handle whose Rust target is weak.
    @_spi(AppleDiagnostics) public func diagnosticSnapshotRfResources(
        completion: @escaping @Sendable ([String: Int]) -> Void
    ) {
        queue.async { [self] in
            let attributes = gattController.diagnosticAttributeIdentities
                .union(gattIoController.diagnosticAttributeIdentities)
            completion([
                "activeScans": scanController.activeSessionId == nil ? 0 : 1,
                "connections": connectionController.activeCount,
                "gattAttributes": attributes.count,
                "subscriptions": gattIoController.diagnosticSubscriptionCount,
                "pendingIo": gattController.pendingCount + gattIoController.pendingCount,
                "watchdogs": gattWatchdogs.diagnosticCount,
                "callbackRoutes": connectionController.activeCount
                    + gattController.diagnosticCallbackRouteCount
                    + gattIoController.diagnosticCallbackRouteCount,
                "ingressRoots": ingress == nil ? 0 : 1,
                "adapterDelegates": centralManager?.delegate == nil ? 0 : 1,
                "peerCacheEntries": scanController.diagnosticPeerCacheCount,
                "backendLeases": ownsLease ? 1 : 0,
            ])
        }
    }

    /// Returns the last adapter/connection-loss callback observation without driving the backend.
    /// This is private HIL evidence only; it is not part of the Stable UniFFI ABI.
    @_spi(AppleDiagnostics) public func diagnosticSnapshotLoss(
        completion: @escaping @Sendable ([String: String]) -> Void
    ) {
        queue.async { [self] in
            completion(lastLossDiagnostic)
        }
    }

    /// Observes the logical GATT I/O lane on the backend serial queue without driving it.
    /// Nil means the requested connection is not currently connected; a live connection
    /// with no I/O queue is idle. Callback ownership is not proof of remote delivery.
    /// Recreate the native Central manager for an explicitly authorized recovery spike.
    /// This is not Stable UniFFI API and refuses to run while native resources are active.
    @_spi(AppleDiagnostics) public func diagnosticRecreateCentral(
        completion: @escaping @Sendable ([String: String]) -> Void
    ) {
        queue.async { [self] in
            guard lifecycle == .ready else {
                completion(["recreated": "false", "reason": "backend-not-ready"])
                return
            }
            guard scanController.activeSessionId == nil,
                  connectionController.activeCount == 0,
                  gattController.pendingCount == 0,
                  gattIoController.pendingCount == 0 else {
                completion(["recreated": "false", "reason": "resources-active"])
                return
            }
            let previousState = centralManager.map { String(describing: $0.state) } ?? "missing"
            centralManager?.delegate = nil
            centralManager = nil
            diagnosticCentralRecreateCompletion = completion
            centralManager = CBCentralManager(
                delegate: self,
                queue: queue,
                options: [CBCentralManagerOptionShowPowerAlertKey: false]
            )
            lastLossDiagnostic["central_recreated"] = "true"
            lastLossDiagnostic["central_previous_state"] = previousState
        }
    }

    @_spi(AppleDiagnostics) public func diagnosticSnapshotGattIo(
        connectionId: ConnectionId,
        completion: @escaping @Sendable (AppleGattIoSnapshot?) -> Void
    ) {
        queue.async { [self] in
            guard connectionController.connectedCorrelation(for: connectionId) != nil else {
                completion(nil)
                return
            }
            completion(gattIoController.diagnosticSnapshotGattIo(connectionId: connectionId)
                ?? AppleGattIoSnapshot(
                    connectionId: connectionId,
                    headKind: nil,
                    headCommandId: nil,
                    headState: .idle,
                    queuedCommandIds: []
                ))
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
            cancelGattWatchdog(connection: correlation)
            let gattRetirement = gattIoController.retireAll(
                connection: correlation,
                error: retirementError
            )
            let pendingGattCommand = gattController.retire(connection: correlation)
            bestEffortUnsubscribe(gattRetirement, peripheral: peripheral)
            peripheral.delegate = nil
            centralManager.cancelPeripheralConnection(peripheral)
            var events = gattRetirementEvents(gattRetirement)
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
            try requireNoPendingGattDiscovery(connection)
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
            try gattIoController.enqueueRssi(
                command: command,
                connection: connection
            )
            peripheral.delegate = self
            driveGattIo(connection: connection, peripheral: peripheral)
        }
    }

    public func cancelOperation(request: CancelOperationRequest) throws {
        try onQueue {
            try lifecycle.requireReady(operation: "cancel operation")
            let connection = try AppleOperationCancellation.cancel(
                commandId: request.targetCommandId,
                connections: connectionController,
                gatt: gattController,
                io: gattIoController
            )
            gattWatchdogs.cancel(
                connectionId: connection.connectionId,
                kind: .writeCapacity(request.targetCommandId)
            )?.cancel()
            push(
                .commandCompleted(
                    commandId: request.targetCommandId,
                    error: BleEventError(
                        category: .cancelled,
                        message: "Apple GATT operation was cancelled before native dispatch",
                        platformDetails: nil
                    )
                )
            )
            if let peripheral = connection.peripheral as? CBPeripheral {
                driveGattIo(connection: connection, peripheral: peripheral)
            }
        }
    }

    public func dispose() throws {
        let releaseLease = onQueue {
            guard lifecycle != .closed else {
                return false
            }

            lifecycle = .closed
            ingress = nil
            gattWatchdogs.cancelAll().forEach { $0.cancel() }
            let wasScanning = scanController.activeSessionId != nil
            let connectionRoutes = connectionController.drain()
            _ = gattController.drain()
            var connectedPeripherals: [(CBPeripheral, AppleGattRetirement)] = []
            for route in connectionRoutes {
                guard let peripheral = route.correlation.peripheral as? CBPeripheral else {
                    continue
                }
                let retirement = gattIoController.retireAll(
                    connection: route.correlation,
                    error: AppleEventMapper.connectionError(
                        nil,
                        context: "the Apple backend was disposed during GATT activity"
                    )
                )
                connectedPeripherals.append((peripheral, retirement))
            }
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
            diagnosticCentralRecreateCompletion = nil

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
        AppleBackendEventProjection.gattFailureEvent(command: command, error: error)
    }


    private func gattIoEvents(_ completion: AppleGattIoCompletion) -> [BleEvent] {
        AppleBackendEventProjection.gattIoEvents(completion)
    }

    private func gattNotificationEvents(
        _ completion: AppleGattNotificationCompletion
    ) -> [BleEvent] {
        AppleBackendEventProjection.gattNotificationEvents(completion)
    }

    private func gattRetirementEvents(_ retirement: AppleGattRetirement) -> [BleEvent] {
        AppleBackendEventProjection.gattRetirementEvents(retirement)
    }

    private func armGattWatchdog(
        connection: AppleConnectionCorrelation,
        kind: AppleGattWatchdogKind,
        context: String
    ) {
        AppleGattWatchdogOrchestration.arm(
            connection: connection,
            kind: kind,
            watchdogs: gattWatchdogs,
            delay: gattIoWatchdogDelay,
            schedule: { deadline, workItem in
                queue.asyncAfter(deadline: deadline, execute: workItem)
            },
            expire: { [weak self] token in
                self?.handleGattWatchdog(connection: connection, token: token, context: context)
            }
        )
    }

    private func cancelGattWatchdog(connection: AppleConnectionCorrelation) {
        gattWatchdogs.cancel(connectionId: connection.connectionId)?.cancel()
    }

    private func handleGattWatchdog(
        connection: AppleConnectionCorrelation,
        token: AppleGattWatchdogToken,
        context: String
    ) {
        guard let events = AppleGattWatchdogOrchestration.expire(
            connection: connection,
            token: token,
            context: context,
            watchdogs: gattWatchdogs,
            connections: connectionController,
            gatt: gattController,
            io: gattIoController,
            resolveNative: { connection.peripheral as? CBPeripheral },
            retireNative: { peripheral, retirement in
                bestEffortUnsubscribe(retirement, peripheral: peripheral)
                peripheral.delegate = nil
                centralManager?.cancelPeripheralConnection(peripheral)
            }
        ) else {
            return
        }
        push(events)
        _ = handleGattSafetyFaultIfNeeded()
    }

    private func requireNoPendingGattDiscovery(
        _ connection: AppleConnectionCorrelation
    ) throws {
        guard !gattController.hasPendingDiscovery(connection: connection) else {
            throw CommandDispatchError.InvalidState(
                detail: "GATT discovery is pending on this Apple connection"
            )
        }
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
        try requireNoPendingGattDiscovery(connection)
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
                ioAction = try AppleGattWatchdogOrchestration.nextAction(
                    connection: connection,
                    io: gattIoController,
                    canSendWriteWithoutResponse: peripheral.canSendWriteWithoutResponse,
                    maximumWriteWithResponse: peripheral.maximumWriteValueLength(for: .withResponse),
                    maximumWriteWithoutResponse: peripheral.maximumWriteValueLength(for: .withoutResponse)
                ) { owner, commandId in
                    armGattWatchdog(
                        connection: owner,
                        kind: .writeCapacity(commandId),
                        context: "write without response capacity"
                    )
                }
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
                    cancelGattWatchdog(connection: connection)
                    push(gattIoEvents(completion))
                case .read(_, let actionConnection, let characteristicObject):
                    guard let characteristic = characteristicObject as? CBCharacteristic else {
                        failGattIoQueue(
                            connection: actionConnection,
                            context: "Apple GATT read routing lost its CoreBluetooth characteristic"
                        )
                        return
                    }
                    armGattWatchdog(
                        connection: actionConnection,
                        kind: .io,
                        context: "characteristic read"
                    )
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
                    armGattWatchdog(
                        connection: actionConnection,
                        kind: .io,
                        context: "characteristic write with response"
                    )
                    peripheral.writeValue(
                        Data(command.value),
                        for: characteristic,
                        type: .withResponse
                    )
                    return
                case .waitForWriteCapacity:
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
                    if let completion = AppleGattWatchdogOrchestration.submitWithoutResponse(
                        command: command,
                        connection: actionConnection,
                        peripheral: peripheral,
                        characteristic: characteristic,
                        io: gattIoController,
                        watchdogs: gattWatchdogs,
                        submit: {
                            peripheral.writeValue(
                                Data(command.value),
                                for: characteristic,
                                type: .withoutResponse
                            )
                        }
                    ) {
                        push(gattIoEvents(completion))
                    }
                case .rssi(_, let actionConnection):
                    armGattWatchdog(
                        connection: actionConnection,
                        kind: .io,
                        context: "RSSI read"
                    )
                    peripheral.readRSSI()
                    return
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
                cancelGattWatchdog(connection: connection)
                push(gattNotificationEvents(completion))
            case .setNotifyValue(
                let command,
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
                armGattWatchdog(
                    connection: actionConnection,
                    kind: .io,
                    context: "notification state transition"
                )
                notificationDiagnostics.record("setNotify.before", fields: notificationDiagnosticFields(
                    peripheral: peripheral, characteristic: characteristic,
                    extra: ["commandId": String(command.commandId.value), "action": enabled ? "enable" : "disable", "origin": "command"]
                ))
                peripheral.setNotifyValue(enabled, for: characteristic)
                notificationDiagnostics.record("setNotify.after", fields: notificationDiagnosticFields(
                    peripheral: peripheral, characteristic: characteristic,
                    extra: ["commandId": String(command.commandId.value), "action": enabled ? "enable" : "disable", "origin": "command"]
                ))
                return
            case .retireForRediscovery(let actionConnection, let characteristicObject):
                guard let characteristic = characteristicObject as? CBCharacteristic else {
                    failGattIoQueue(
                        connection: actionConnection,
                        context: "Apple rediscovery notification teardown lost its CoreBluetooth characteristic"
                    )
                    return
                }
                armGattWatchdog(
                    connection: actionConnection,
                    kind: .io,
                    context: "notification unsubscribe during GATT rediscovery"
                )
                notificationDiagnostics.record("setNotify.before", fields: notificationDiagnosticFields(
                    peripheral: peripheral, characteristic: characteristic,
                    extra: ["action": "disable", "origin": "rediscovery"]
                ))
                peripheral.setNotifyValue(false, for: characteristic)
                notificationDiagnostics.record("setNotify.after", fields: notificationDiagnosticFields(
                    peripheral: peripheral, characteristic: characteristic,
                    extra: ["action": "disable", "origin": "rediscovery"]
                ))
                return
            }
        }
    }

    private func failGattIoQueue(
        connection: AppleConnectionCorrelation,
        context: String
    ) {
        let error = AppleEventMapper.internalError(context)
        cancelGattWatchdog(connection: connection)
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
        armGattWatchdog(
            connection: connection,
            kind: .discovery,
            context: "service discovery"
        )
        peripheral.discoverServices(nil)
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
            armGattWatchdog(
                connection: connection,
                kind: .discovery,
                context: "characteristic discovery"
            )
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
            armGattWatchdog(
                connection: connection,
                kind: .discovery,
                context: "descriptor discovery"
            )
            peripheral.discoverDescriptors(for: characteristic)
        case .completed(let completion):
            cancelGattWatchdog(connection: connection)
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
        cancelGattWatchdog(connection: connection)
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
        if gattIoController.consumeRetiredRssiCallback(peripheral: peripheral) {
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
        guard let completion = gattIoController.didReadRssi(
            connection: connection,
            peripheral: peripheral,
            rssi: rssi,
            error: mappedError
        ) else {
            return
        }
        cancelGattWatchdog(connection: connection)
        push(gattIoEvents(completion))
        driveGattIo(connection: connection, peripheral: peripheral)
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
            notificationDiagnostics.record("setNotify.before", fields: notificationDiagnosticFields(
                peripheral: peripheral, characteristic: characteristic,
                extra: ["action": "disable", "origin": "cleanup"]
            ))
            peripheral.setNotifyValue(false, for: characteristic)
            notificationDiagnostics.record("setNotify.after", fields: notificationDiagnosticFields(
                peripheral: peripheral, characteristic: characteristic,
                extra: ["action": "disable", "origin": "cleanup"]
            ))
        }
    }

    @discardableResult
    private func handleGattSafetyFaultIfNeeded() -> Bool {
        guard let error = gattIoController.takeSafetyFault() else {
            return false
        }
        lifecycle.markFaulted()
        gattWatchdogs.cancelAll().forEach { $0.cancel() }
        var events: [BleEvent] = []
        if let scanSessionId = scanController.stopForUnavailableAdapter() {
            centralManager?.stopScan()
            events.append(.scanStopped(scanId: scanSessionId, result: .err(error: error)))
        }
        for route in connectionController.drain() {
            let connection = route.correlation
            let retirement = gattIoController.retireAll(connection: connection, error: error)
            events.append(contentsOf: gattRetirementEvents(retirement))
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

    private func notificationDiagnosticFields(
        peripheral: CBPeripheral,
        characteristic: CBCharacteristic,
        error: Error? = nil,
        extra: [String: Any] = [:]
    ) -> [String: Any] {
        var fields = gattIoController.diagnosticNotificationRoute(
            connection: connectionController.connectedCorrelation(for: peripheral),
            peripheral: peripheral, characteristic: characteristic
        )
        fields["isNotifying"] = characteristic.isNotifying
        fields["hasError"] = error != nil
        if let error = error as NSError? {
            fields["errorDomain"] = error.domain
            fields["errorCode"] = String(error.code)
        }
        fields["valueNil"] = characteristic.value == nil
        if let value = characteristic.value {
            fields["valueLength"] = value.count
            fields["valueHex"] = value.prefix(16).map { String(format: "%02x", $0) }.joined()
            fields["valueTruncated"] = value.count > 16
        }
        fields.merge(extra) { _, new in new }
        if extra["disposition"] as? String == "ignored" {
            if error != nil { fields["reason"] = "callback-error" }
            else if characteristic.value == nil { fields["reason"] = "nil-value" }
            else if fields["routeMatches"] as? Bool == true { fields["reason"] = "subscription-phase-rejects-value" }
            else { fields["reason"] = "no-matching-subscription-or-read" }
        }
        return fields
    }

    private func notificationIngressDiagnostic(_ event: BleEvent, result: String) {
        guard notificationDiagnostics.isEnabled,
              case let .notificationReceived(connection, session, subscription, characteristic, value) = event else { return }
        notificationDiagnostics.record("notification.ingress", fields: [
            "connectionId": String(connection.value), "gattSessionId": String(session.value),
            "subscriptionId": String(subscription.value), "characteristicId": String(characteristic.value),
            "valueLength": value.count, "valueHex": value.prefix(16).map { String(format: "%02x", $0) }.joined(),
            "valueTruncated": value.count > 16, "result": result,
        ])
    }

    private func push(_ events: [BleEvent]) {
        guard let ingress else {
            lastIngressStatus = "no-ingress"
            if notificationDiagnostics.isEnabled {
                for event in events { notificationIngressDiagnostic(event, result: "no-ingress") }
            }
            return
        }
        var delivered = 0
        var closed = 0
        var invalid = 0
        var downstreamFailed = 0
        for event in events {
            switch ingress.push(event: event) {
            case .delivered:
                delivered += 1
                notificationIngressDiagnostic(event, result: "delivered")
            case .closed:
                closed += 1
                notificationIngressDiagnostic(event, result: "closed")
            case .invalidEvent(let message):
                invalid += 1
                notificationIngressDiagnostic(event, result: "invalid-event")
                Self.logger.error(
                    "event ingress rejected an invalid Apple event: \(message, privacy: .private)"
                )
            case .downstreamFailed(let message):
                downstreamFailed += 1
                notificationIngressDiagnostic(event, result: "downstream-failed")
                Self.logger.error(
                    "Apple downstream event sink failed: \(message, privacy: .private)"
                )
            }
        }
        lastIngressStatus = "delivered=\(delivered),closed=\(closed),invalid=\(invalid),downstreamFailed=\(downstreamFailed)"
    }

    private func recordLossDiagnostic(
        callback: String,
        adapterState: String,
        retiredOperationCount: Int,
        emittedEventCount: Int
    ) {
        lastLossDiagnostic = [
            "last_callback": callback,
            "adapter_state": adapterState,
            "retired_operation_count": String(retiredOperationCount),
            "emitted_event_count": String(emittedEventCount),
            "ingress_status": lastIngressStatus,
            "active_connections": String(connectionController.activeCount),
            "pending_io": String(gattIoController.pendingCount),
            "watchdogs": String(gattWatchdogs.diagnosticCount),
        ]
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
        let wasReady: Bool
        switch lifecycle {
        case .initializing:
            wasReady = false
            commandId = lifecycle.consumeInitialStateCallback()
        case .ready:
            wasReady = true
            commandId = nil
        case .inert, .faulted, .closed:
            return
        }

        let state = central.state
        let authorization = CBManager.authorization
        if let completion = diagnosticCentralRecreateCompletion {
            diagnosticCentralRecreateCompletion = nil
            completion([
                "recreated": "true",
                "state": String(describing: state),
                "authorization": String(describing: authorization),
            ])
        }
        let pendingIoBefore = gattIoController.pendingCount
        let snapshot = AppleEventMapper.adapterSnapshot(
            state: state,
            authorization: authorization
        )
        var events: [BleEvent] = [
            .adapterSnapshotChanged(commandId: commandId, snapshot: snapshot),
        ]
        if let commandId {
            events.append(.commandCompleted(commandId: commandId, error: nil))
        }
        if let error = AppleEventMapper.scanAvailabilityError(
            state: state,
            authorization: authorization
        ), let scanSessionId = scanController.stopForUnavailableAdapter() {
            central.stopScan()
            events.append(.scanStopped(scanId: scanSessionId, result: .err(error: error)))
        }
        events.append(contentsOf: AppleUnavailableConnectionRetirement.retireIfUnavailable(
            state: state,
            authorization: authorization,
            connections: connectionController,
            gatt: gattController,
            io: gattIoController,
            watchdogs: gattWatchdogs
        ) { connection, retirement in
            if let peripheral = connection.peripheral as? CBPeripheral {
                if state == .poweredOn {
                    bestEffortUnsubscribe(retirement, peripheral: peripheral)
                }
                peripheral.delegate = nil
                if state == .poweredOn {
                    central.cancelPeripheralConnection(peripheral)
                }
            }
        })
        // CoreBluetooth's resetting state invalidates the manager's native peripheral
        // lifecycles. Its later callbacks carry no operation token, so this backend
        // must not reuse retained CBPeripheral objects after the reset. The caller
        // must dispose and assemble a fresh backend instead. Powered-off and
        // authorization transitions remain recoverable within the current backend.
        if wasReady && state == .resetting {
            lifecycle.markFaulted()
            scanController.dispose()
        }
        push(events)
        _ = handleGattSafetyFaultIfNeeded()
        recordLossDiagnostic(
            callback: "centralManagerDidUpdateState",
            adapterState: String(describing: state),
            retiredOperationCount: max(0, pendingIoBefore - gattIoController.pendingCount),
            emittedEventCount: events.count
        )
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
        cancelGattWatchdog(connection: correlation)
        let retirement = gattIoController.retireAll(
            connection: correlation,
            error: connectionEndedError
        )
        var events = gattRetirementEvents(retirement)
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
        recordLossDiagnostic(
            callback: "didDisconnectPeripheral",
            adapterState: centralManager.map { String(describing: $0.state) } ?? "missing",
            retiredOperationCount: retirement.completions.count,
            emittedEventCount: events.count
        )
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
        notificationDiagnostics.record("value.entry", fields: notificationDiagnosticFields(
            peripheral: peripheral, characteristic: characteristic, error: error
        ))
        if gattIoController.consumeRetiredReadCallback(
            peripheral: peripheral,
            characteristic: characteristic
        ) {
            notificationDiagnostics.record("value.drop", fields: ["reason": "retired-read-barrier"])
            return
        }
        guard let connection = connectionController.connectedCorrelation(for: peripheral) else {
            notificationDiagnostics.record("value.drop", fields: ["reason": "no-connected-correlation"])
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
        let diagnosticDisposition: String
        switch disposition {
        case .read: diagnosticDisposition = "read"
        case .notification: diagnosticDisposition = "notification"
        case .ignored: diagnosticDisposition = "ignored"
        }
        notificationDiagnostics.record("value.disposition", fields: notificationDiagnosticFields(
            peripheral: peripheral, characteristic: characteristic, error: error,
            extra: ["disposition": diagnosticDisposition]
        ))
        switch disposition {
        case .read(let completion):
            cancelGattWatchdog(connection: connection)
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
        notificationDiagnostics.record("notification-state.entry", fields: notificationDiagnosticFields(
            peripheral: peripheral, characteristic: characteristic, error: error
        ))
        if gattIoController.consumeRetiredNotificationStateCallback(
            peripheral: peripheral,
            characteristic: characteristic
        ) {
            notificationDiagnostics.record("notification-state.drop", fields: ["reason": "retired-notification-state-barrier"])
            return
        }
        guard let connection = connectionController.connectedCorrelation(for: peripheral) else {
            notificationDiagnostics.record("notification-state.drop", fields: ["reason": "no-connected-correlation"])
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
            notificationDiagnostics.record("notification-state.drop", fields: notificationDiagnosticFields(
                peripheral: peripheral, characteristic: characteristic, error: error,
                extra: ["reason": "controller-no-completion"]
            ))
            return
        }
        let diagnosticCompletion: String
        switch completion {
        case .enabled: diagnosticCompletion = "enabled"
        case .disabled: diagnosticCompletion = "disabled"
        case .failed: diagnosticCompletion = "failed"
        case .rediscoveryRetired: diagnosticCompletion = "rediscovery-retired"
        case .rediscoveryFailed: diagnosticCompletion = "rediscovery-failed"
        }
        notificationDiagnostics.record("notification-state.result", fields: notificationDiagnosticFields(
            peripheral: peripheral, characteristic: characteristic, error: error,
            extra: ["completion": diagnosticCompletion]
        ))
        cancelGattWatchdog(connection: connection)
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
        cancelGattWatchdog(connection: connection)
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
