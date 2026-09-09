import BleHubUniFfi
import Foundation

internal enum AppleRssiCompletion {
    case read(command: ReadRssiCommand, rssi: Int16)
    case failed(command: ReadRssiCommand, error: BleEventError)
}

internal final class AppleRssiController {
    private struct PendingRead {
        let command: ReadRssiCommand
        let connection: AppleConnectionCorrelation
    }

    private var pendingByConnectionId: [ConnectionId: PendingRead] = [:]
    private var retiredPeripheralBarriers: [ObjectIdentifier: any ApplePeripheralObject] = [:]

    var pendingCount: Int {
        pendingByConnectionId.count
    }

    func hasPending(connection: AppleConnectionCorrelation) -> Bool {
        guard let pending = pendingByConnectionId[connection.connectionId] else {
            return false
        }
        return connectionsMatch(pending.connection, connection)
    }

    func begin(
        command: ReadRssiCommand,
        connection: AppleConnectionCorrelation
    ) throws {
        guard command.connectionId == connection.connectionId else {
            throw CommandDispatchError.InvalidState(
                detail: "the RSSI command does not match the active Apple connection"
            )
        }

        let peripheralIdentity = ObjectIdentifier(connection.peripheral)
        guard retiredPeripheralBarriers[peripheralIdentity] == nil else {
            throw CommandDispatchError.InvalidState(
                detail: "a retired CoreBluetooth RSSI callback route has not reached a safe reuse boundary"
            )
        }
        guard pendingByConnectionId[connection.connectionId] == nil else {
            throw CommandDispatchError.InvalidState(
                detail: "an RSSI read is already pending for the Apple connection"
            )
        }

        pendingByConnectionId[connection.connectionId] = PendingRead(
            command: command,
            connection: connection
        )
    }

    func didReadRssi(
        connection: AppleConnectionCorrelation,
        peripheral: any ApplePeripheralObject,
        rssi: NSNumber?,
        error: BleEventError?
    ) -> AppleRssiCompletion? {
        if consumeRetiredCallback(peripheral: peripheral) {
            return nil
        }
        guard let pending = matchingPending(
            connection: connection,
            callbackPeripheral: peripheral
        ) else {
            return nil
        }

        pendingByConnectionId.removeValue(forKey: connection.connectionId)
        if let error {
            return .failed(command: pending.command, error: error)
        }
        guard let rssi else {
            return .failed(
                command: pending.command,
                error: invalidValueError(
                    "CoreBluetooth RSSI read completed without a value"
                )
            )
        }
        guard let value = exactInt16(rssi) else {
            return .failed(
                command: pending.command,
                error: invalidValueError(
                    "CoreBluetooth RSSI read returned a value that is not representable as Int16"
                )
            )
        }
        return .read(command: pending.command, rssi: value)
    }

    func retire(
        connection: AppleConnectionCorrelation,
        error: BleEventError
    ) -> AppleRssiCompletion? {
        guard let pending = matchingPending(connection: connection) else {
            return nil
        }

        pendingByConnectionId.removeValue(forKey: connection.connectionId)
        let peripheral = pending.connection.peripheral
        retiredPeripheralBarriers[ObjectIdentifier(peripheral)] = peripheral
        return .failed(command: pending.command, error: error)
    }

    func consumeRetiredCallback(peripheral: any ApplePeripheralObject) -> Bool {
        retiredPeripheralBarriers.removeValue(
            forKey: ObjectIdentifier(peripheral)
        ) != nil
    }

    func drain() {
        pendingByConnectionId.removeAll(keepingCapacity: false)
        retiredPeripheralBarriers.removeAll(keepingCapacity: false)
    }

    private func matchingPending(
        connection: AppleConnectionCorrelation,
        callbackPeripheral: (any ApplePeripheralObject)? = nil
    ) -> PendingRead? {
        guard let pending = pendingByConnectionId[connection.connectionId],
              connectionsMatch(pending.connection, connection)
        else {
            return nil
        }
        if let callbackPeripheral,
           ObjectIdentifier(pending.connection.peripheral) != ObjectIdentifier(callbackPeripheral)
        {
            return nil
        }
        return pending
    }

    private func connectionsMatch(
        _ lhs: AppleConnectionCorrelation,
        _ rhs: AppleConnectionCorrelation
    ) -> Bool {
        lhs.generation == rhs.generation &&
            lhs.connectionId == rhs.connectionId &&
            ObjectIdentifier(lhs.peripheral) == ObjectIdentifier(rhs.peripheral)
    }

    private func exactInt16(_ value: NSNumber) -> Int16? {
        let numericValue = value.doubleValue
        guard numericValue.isFinite else {
            return nil
        }
        return Int16(exactly: numericValue)
    }

    private func invalidValueError(_ message: String) -> BleEventError {
        BleEventError(
            category: .connection,
            message: message,
            platformDetails: nil
        )
    }
}
