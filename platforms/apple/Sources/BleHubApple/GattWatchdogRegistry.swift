import BleHubUniFfi
import Dispatch

internal enum AppleGattWatchdogKind: Equatable {
    case io
    case discovery
    case writeCapacity(CommandId)
}

internal struct AppleGattWatchdogToken: Equatable {
    let value: UInt64
    let kind: AppleGattWatchdogKind
}

/// Serial-queue-confined watchdog ownership for one native connection.
///
/// The registry only tracks identity and replacement. The backend remains
/// responsible for scheduling and cancelling DispatchWorkItems outside any
/// broader state transition that may call native CoreBluetooth.
internal final class AppleGattWatchdogRegistry {
    private struct Entry {
        let token: AppleGattWatchdogToken
        let workItem: DispatchWorkItem
    }

    private var nextValue: UInt64 = 1
    private var entries: [ConnectionId: Entry] = [:]

    // Registered work only; cancelled GCD submissions are not owned active watchdogs.
    var diagnosticCount: Int { entries.count }

    func arm(
        connectionId: ConnectionId,
        kind: AppleGattWatchdogKind,
        workItem: DispatchWorkItem
    ) -> (token: AppleGattWatchdogToken, replaced: DispatchWorkItem?) {
        let token = AppleGattWatchdogToken(value: nextValue, kind: kind)
        nextValue = nextValue == UInt64.max ? 1 : nextValue + 1
        let replaced = entries.updateValue(
            Entry(token: token, workItem: workItem),
            forKey: connectionId
        )?.workItem
        return (token, replaced)
    }

    func cancel(
        connectionId: ConnectionId,
        kind: AppleGattWatchdogKind? = nil
    ) -> DispatchWorkItem? {
        guard let entry = entries[connectionId], kind == nil || entry.token.kind == kind else {
            return nil
        }
        return entries.removeValue(forKey: connectionId)?.workItem
    }

    func takeIfCurrent(
        connectionId: ConnectionId,
        token: AppleGattWatchdogToken
    ) -> DispatchWorkItem? {
        guard let entry = entries[connectionId], entry.token == token else {
            return nil
        }
        entries.removeValue(forKey: connectionId)
        return entry.workItem
    }

    func cancelAll() -> [DispatchWorkItem] {
        let workItems = entries.values.map(\.workItem)
        entries.removeAll()
        return workItems
    }
}
