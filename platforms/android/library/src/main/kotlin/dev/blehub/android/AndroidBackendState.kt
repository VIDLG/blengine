package dev.blehub.android

import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.le.BluetoothLeScanner
import android.bluetooth.le.ScanCallback
import dev.blehub.BleEvent
import dev.blehub.BleEventError
import dev.blehub.CommandId
import dev.blehub.ConnectCommand
import dev.blehub.ConnectionId
import dev.blehub.DiscoverGattCommand
import dev.blehub.GattCharacteristicId
import dev.blehub.GattService
import dev.blehub.GattSessionId
import dev.blehub.NotificationMode
import dev.blehub.ScanOptions
import dev.blehub.ScanSessionId
import dev.blehub.SetNotificationsCommand
import dev.blehub.SubscriptionId
import dev.blehub.WriteCharacteristicCommand

/** Internal lifecycle state shared by the Android backend's serialized handlers. */
internal enum class Lifecycle {
    NEW,
    INITIALIZING,
    READY,
    CLOSING,
    CLOSED,
}

internal enum class ConnectionPhase {
    CONNECTING,
    CONNECTED,
    DISCONNECTING,
}

internal data class ActiveConnection(
    val generation: Long,
    val command: ConnectCommand,
    val address: String,
    var phase: ConnectionPhase = ConnectionPhase.CONNECTING,
    var gatt: BluetoothGatt? = null,
    var disconnectCommandId: CommandId? = null,
    var connectCompleted: Boolean = false,
    var pendingDiscovery: PendingDiscovery? = null,
    var gattSessionId: GattSessionId? = null,
    var characteristicsById: Map<ULong, BluetoothGattCharacteristic> = emptyMap(),
    var descriptorsById: Map<ULong, BluetoothGattDescriptor> = emptyMap(),
    // Platform-reported ATT MTU; 23 is the Bluetooth LE default before negotiation.
    var mtu: Int = 23,
    val pendingIo: PendingIoQueue = PendingIoQueue(),
    // Only the active native operation owns a watchdog; queued work has not entered Android yet.
    val activeWatchdog: ActiveWatchdogSlot = ActiveWatchdogSlot(),
    val subscriptionsById: MutableMap<ULong, ActiveSubscription<*>> = LinkedHashMap(),
)

/** Read-only aggregation; caller serializes route mutation with its state lock. */
internal fun connectionRfResourceCounts(ownedRoutes: Collection<ActiveConnection>): Map<String, Int> {
    // One route may appear in the ID index, peer index and pending-dispose ownership.
    val routes = java.util.Collections.newSetFromMap(
        java.util.IdentityHashMap<ActiveConnection, Boolean>(),
    ).also { it.addAll(ownedRoutes) }
    val subscriptions = routes.sumOf { it.subscriptionsById.size }
    return linkedMapOf(
        "connections" to routes.size,
        "gattAttributes" to routes.sumOf { it.characteristicsById.size + it.descriptorsById.size },
        "subscriptions" to subscriptions,
        "pendingIo" to routes.sumOf { it.pendingIo.size + if (it.pendingDiscovery != null) 1 else 0 },
        "watchdogs" to routes.count { it.activeWatchdog.isArmed },
        "callbackRoutes" to (routes.size + subscriptions +
                routes.count { it.pendingDiscovery != null } +
                routes.count { it.pendingIo.active() != null }),
    )
}

internal data class ActiveSubscription<C : Any>(
    val subscriptionId: SubscriptionId,
    val characteristicId: GattCharacteristicId,
    val characteristic: C,
    val mode: NotificationMode,
)

/** Which direction a pending bond request is pursuing; the terminal state differs per direction. */
internal enum class BondRequestKind { CREATE, REMOVE }

/** One bond request correlated to the platform broadcast that will report its terminal. */
internal data class PendingBond(
    val commandId: CommandId,
    val connectionId: ConnectionId,
    val kind: BondRequestKind,
    /** Numeric PIN pairing input only; cleared as soon as the platform consumes it. Never logged. */
    val pin: String? = null,
)

/**
 * The Android GATT API serializes operations per BluetoothGatt instance.
 * Different ActiveConnection instances still own independent queues and may
 * make progress concurrently on the same backend Handler.
 *
 * The first entry is the only native operation in flight. Entries after it
 * have been admitted but must not call BluetoothGatt until the first entry is
 * completed or retired. The queue also prevents cancellation from removing
 * the active native operation accidentally.
 */
internal class PendingIoQueue(private val capacity: Int = DEFAULT_CAPACITY) {
    private val entries = ArrayDeque<PendingIo>()
    private var active = false

    init {
        require(capacity > 0) { "pending I/O queue capacity must be positive" }
    }

    val isEmpty: Boolean
        get() = entries.isEmpty()

    /** Includes the native-active head and all admitted queued operations. */
    val size: Int
        get() = entries.size

    fun contains(commandId: CommandId): Boolean =
        entries.any { it.commandId == commandId }

    fun any(predicate: (PendingIo) -> Boolean): Boolean = entries.any(predicate)

    fun enqueue(operation: PendingIo): Boolean {
        if (entries.size >= capacity || contains(operation.commandId)) return false
        entries.addLast(operation)
        return true
    }

    /** Claims the next operation for one native dispatch, if the lane is idle. */
    fun begin(): PendingIo? {
        if (active) return null
        return entries.firstOrNull()?.also { active = true }
    }

    fun active(): PendingIo? = if (active) entries.firstOrNull() else null

    fun isActive(commandId: CommandId): Boolean =
        active && entries.firstOrNull()?.commandId == commandId

    fun isActive(operation: PendingIo): Boolean = active && entries.firstOrNull() === operation

    /** Completes exactly the active head; late or mismatched callbacks are ignored. */
    fun complete(operation: PendingIo): Boolean {
        if (!isActive(operation)) return false
        entries.removeFirst()
        active = false
        return true
    }

    /** Removes a queued operation without touching the active native head. */
    fun removeQueued(commandId: CommandId): PendingIo? {
        val index = entries.indexOfFirst { it.commandId == commandId }
        if (index < 0 || (active && index == 0)) return null
        return entries.removeAt(index)
    }

    /** Retires the whole lane for disconnect/dispose and returns all owners. */
    fun retire(): List<PendingIo> {
        val retired = entries.toList()
        entries.clear()
        active = false
        return retired
    }

    private companion object {
        const val DEFAULT_CAPACITY = 1024
    }
}

/** Retires native ownership even when a command terminal was claimed at local submission. */
internal fun retirePendingIo(
    queue: PendingIoQueue,
    activeOperationError: BleEventError,
    queuedOperationError: BleEventError,
): List<BleEvent> {
    val activeOperation = queue.active()
    return queue.retire().mapNotNull { pending ->
        if (!pending.claimCommandTerminal()) return@mapNotNull null
        val error = if (pending === activeOperation) activeOperationError else queuedOperationError
        BleEvent.CommandCompleted(pending.commandId, error)
    }
}

internal sealed class PendingIo {
    abstract val commandId: CommandId

    private var commandTerminalClaimed = false

    /** Called under stateLock; claiming publication does not release the native queue slot. */
    fun claimCommandTerminal(): Boolean {
        if (commandTerminalClaimed) return false
        commandTerminalClaimed = true
        return true
    }

    data class Read(
        val command: dev.blehub.ReadCharacteristicCommand,
        val characteristic: BluetoothGattCharacteristic,
    ) : PendingIo() {
        override val commandId: CommandId
            get() = command.commandId
    }

    // Lifecycle code needs only native identity; the Framework adapter retains the concrete type.
    data class Write<C : Any>(
        val command: WriteCharacteristicCommand,
        val characteristic: C,
    ) : PendingIo() {
        override val commandId: CommandId
            get() = command.commandId
    }

    enum class NotificationStage { QUEUED, REGISTERING_LOCAL, WRITING_DESCRIPTOR, COMPLETING }

    data class Notification<C : Any, D : Any>(
        val command: SetNotificationsCommand,
        val characteristic: C,
        val descriptor: D,
        val mode: NotificationMode,
        val enabling: Boolean,
    ) : PendingIo() {
        // Mutated under stateLock, including across reentrant native hooks/callbacks.
        var stage = NotificationStage.QUEUED

        // Only enable can compensate, after positive local registration.
        var localCompensationPending = false

        override val commandId: CommandId
            get() = command.commandId
    }

    data class Rssi(
        val command: dev.blehub.ReadRssiCommand,
    ) : PendingIo() {
        override val commandId: CommandId
            get() = command.commandId
    }

    data class Mtu(
        val command: dev.blehub.MtuCommand,
    ) : PendingIo() {
        override val commandId: CommandId
            get() = command.commandId
    }

    data class Priority(
        val command: dev.blehub.SetConnectionPriorityCommand,
    ) : PendingIo() {
        override val commandId: CommandId
            get() = command.commandId
    }

    data class DescriptorRead(
        val command: dev.blehub.ReadDescriptorCommand,
        val descriptor: BluetoothGattDescriptor,
    ) : PendingIo() {
        override val commandId: CommandId
            get() = command.commandId
    }

    data class DescriptorWrite(
        val command: dev.blehub.WriteDescriptorCommand,
        val descriptor: BluetoothGattDescriptor,
    ) : PendingIo() {
        override val commandId: CommandId
            get() = command.commandId
    }
}

internal data class PendingDiscovery(
    val command: DiscoverGattCommand,
)

internal sealed class ActiveNativeOperation {
    data class Io(val operation: PendingIo) : ActiveNativeOperation()
    data class Discovery(val pending: PendingDiscovery) : ActiveNativeOperation()
}

internal data class GattDraft(
    val services: List<GattService>,
    val characteristics: Map<ULong, BluetoothGattCharacteristic>,
    val descriptors: Map<ULong, BluetoothGattDescriptor>,
)

internal data class ActiveScan(
    val session: ScanSessionId,
    val options: ScanOptions,
    var scanner: BluetoothLeScanner? = null,
    var callback: ScanCallback? = null,
    val reportedAddresses: MutableSet<String> = LinkedHashSet(),
)

internal object BackendLease {
    private val held = java.util.concurrent.atomic.AtomicBoolean(false)

    fun acquire(): Boolean = held.compareAndSet(false, true)

    fun release() {
        check(held.compareAndSet(true, false)) { "Android backend lease released twice" }
    }
}
