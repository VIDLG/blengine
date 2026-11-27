package dev.blehub.android

import android.bluetooth.BluetoothGatt
import dev.blehub.BleEvent
import dev.blehub.BleEventError
import dev.blehub.CommandDispatchException
import dev.blehub.CommandId
import dev.blehub.ConnectCommand
import dev.blehub.ConnectionState
import dev.blehub.ConnectionStateResult

/**
 * The backend's connection indexes and disconnect retirement, not a second route inventory.
 * Admission and index access use the caller's stateLock. disconnected() also owns the
 * lock boundary so native close and event delivery happen only after atomic retirement.
 */
internal class AndroidConnectionLifecycle(
    private val stateLock: Any,
    private val removeWatchdog: (Runnable) -> Unit,
    private val closeGatt: (ActiveConnection) -> Unit,
    private val publish: (BleEvent) -> Unit,
) {
    private var nextGeneration = 1L
    private val byId = mutableMapOf<ULong, ActiveConnection>()
    private val byPeripheral = mutableMapOf<ULong, ActiveConnection>()

    val activeRoutes: Collection<ActiveConnection>
        get() = byId.values

    val ownedRoutes: List<ActiveConnection>
        get() = byId.values + byPeripheral.values

    fun find(connectionId: ULong): ActiveConnection? = byId[connectionId]

    fun isActive(route: ActiveConnection): Boolean = byId[route.command.connectionId.value] === route

    fun reserve(command: ConnectCommand, address: String): ActiveConnection {
        if (byPeripheral.containsKey(command.peripheralId.value)) {
            throw CommandDispatchException.InvalidState(
                "the peripheral already has an active connection lifecycle",
            )
        }
        if (byId.containsKey(command.connectionId.value)) {
            throw CommandDispatchException.InvalidRequest(
                "the connection ID is already in use by this Android backend",
            )
        }
        return ActiveConnection(
            generation = nextGeneration++,
            command = command,
            address = address,
        ).also { route ->
            byId[command.connectionId.value] = route
            byPeripheral[command.peripheralId.value] = route
        }
    }

    fun remove(route: ActiveConnection): Boolean {
        if (!isActive(route)) return false
        byId.remove(route.command.connectionId.value)
        if (byPeripheral[route.command.peripheralId.value] === route) {
            byPeripheral.remove(route.command.peripheralId.value)
        }
        return true
    }

    fun drain(): List<ActiveConnection> = byId.values.toList().also {
        byId.clear()
        byPeripheral.clear()
    }

    /** Called under the backend stateLock; only queued I/O can be removed. */
    fun cancelOperation(commandId: CommandId) {
        val route = byId.values.firstOrNull { it.pendingIo.contains(commandId) } ?: run {
            if (byId.values.any {
                    (!it.connectCompleted && it.command.commandId == commandId) ||
                            it.pendingDiscovery?.command?.commandId == commandId
                }) {
                throw CommandDispatchException.OperationNotCancellable(commandId)
            }
            throw CommandDispatchException.OperationNotFound(commandId)
        }
        if (route.pendingIo.isActive(commandId)) {
            throw CommandDispatchException.OperationNotCancellable(commandId)
        }
        route.pendingIo.removeQueued(commandId)
            ?: throw CommandDispatchException.OperationNotFound(commandId)
    }

    fun retireGattState(
        route: ActiveConnection,
        error: BleEventError,
        activeOperationError: BleEventError = error,
        queuedOperationError: BleEventError = error,
    ): List<BleEvent> {
        val events = buildList {
            route.pendingDiscovery?.let {
                add(BleEvent.CommandCompleted(it.command.commandId, error))
            }
            addAll(retirePendingIo(route.pendingIo, activeOperationError, queuedOperationError))
        }
        route.activeWatchdog.clear()?.let(removeWatchdog)
        route.pendingDiscovery = null
        route.gattSessionId = null
        route.characteristicsById = emptyMap()
        route.descriptorsById = emptyMap()
        route.subscriptionsById.clear()
        return events
    }

    fun disconnected(route: ActiveConnection, status: Int) {
        val disconnectCommandId: CommandId?
        val connectWasCompleted: Boolean
        val pendingEvents: List<BleEvent>
        val activityError = if (status == BluetoothGatt.GATT_SUCCESS) {
            connectionError("Android GATT connection ended during native activity")
        } else {
            connectionError("Android GATT connection ended during native activity", status)
        }
        synchronized(stateLock) {
            if (!remove(route)) return
            pendingEvents = retireGattState(route, activityError)
            disconnectCommandId = route.disconnectCommandId
            connectWasCompleted = route.connectCompleted
        }
        closeGatt(route)
        pendingEvents.forEach(publish)
        val error = if (status == BluetoothGatt.GATT_SUCCESS) {
            null
        } else {
            connectionError("Android GATT connection ended with status $status", status)
        }
        if (!connectWasCompleted) {
            val connectError = error ?: connectionError(
                "Android GATT connection ended before reaching the connected state",
            )
            publish(
                BleEvent.ConnectionStateChanged(
                    commandId = route.command.commandId,
                    connectionId = route.command.connectionId,
                    result = ConnectionStateResult.Err(connectError),
                ),
            )
            publish(BleEvent.CommandCompleted(route.command.commandId, connectError))
        }
        if (disconnectCommandId != null) {
            publish(
                BleEvent.ConnectionStateChanged(
                    commandId = disconnectCommandId,
                    connectionId = route.command.connectionId,
                    result = if (error == null) ConnectionStateResult.Ok(ConnectionState.DISCONNECTED)
                    else ConnectionStateResult.Err(error),
                ),
            )
            publish(BleEvent.CommandCompleted(disconnectCommandId, error))
        } else if (connectWasCompleted) {
            publish(
                BleEvent.ConnectionStateChanged(
                    commandId = null,
                    connectionId = route.command.connectionId,
                    result = if (error == null) ConnectionStateResult.Ok(ConnectionState.DISCONNECTED)
                    else ConnectionStateResult.Err(error),
                ),
            )
        }
    }
}
