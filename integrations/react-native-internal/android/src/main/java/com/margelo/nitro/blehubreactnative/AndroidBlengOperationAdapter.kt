package com.margelo.nitro.blehubreactnative

import dev.blehub.CommandId
import dev.blehub.ConnectionId
import dev.blehub.GattCharacteristicId
import dev.blehub.GattSessionId
import dev.blehub.NotificationModeRequest
import dev.blehub.PeripheralId
import dev.blehub.ScanOptions
import dev.blehub.ScanSessionId
import dev.blehub.SubscriptionId
import dev.blehub.WriteType
import dev.bleng.Bleng
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.launch
import java.util.concurrent.ConcurrentHashMap

/**
 * Private adapter from the legacy diagnostic probe calls to BLeng typed operations.
 *
 * The probe still accepts caller labels because its test ledger is not the public BLeng API. Those
 * labels are only a projection key: native command identity comes from the typed operation's
 * `commandId()`. Completion events are therefore fenced through the actual Rust ID before the
 * adapter consumes the typed result. This class owns no BLE state and makes no recovery decision.
 */
internal class AndroidBlengOperationAdapter(
    owner: Bleng?,
    private val scope: CoroutineScope,
    private val onObservationFailure: (Throwable) -> Unit,
) {
    private data class TrackedOperation(
        val externalId: ULong,
        val commandId: ULong,
        val waitForTerminal: suspend () -> Any?,
        val cancel: suspend () -> Unit,
    )

    private val stateLock = Any()
    private var ownerHandle: Bleng? = owner
    private var pendingInitializationExternalId: ULong? = null
    private var initializationCommandId: ULong? = null
    private var initializationTerminalObserved = false
    private val operations = ConcurrentHashMap<ULong, TrackedOperation>()
    private val actualByExternalId = ConcurrentHashMap<ULong, ULong>()
    private val reservedExternalIds = ConcurrentHashMap.newKeySet<ULong>()

    fun markInitializationPending(externalId: ULong) = synchronized(stateLock) {
        pendingInitializationExternalId = externalId
    }

    fun completeInitialization(commandId: CommandId) = synchronized(stateLock) {
        initializationCommandId = commandId.value
        if (initializationTerminalObserved) {
            initializationCommandId = null
            pendingInitializationExternalId = null
        }
    }

    fun attach(owner: Bleng) = synchronized(stateLock) {
        check(ownerHandle == null) { "BLeng operation adapter owner was already attached" }
        ownerHandle = owner
    }

    fun initialize(externalId: ULong): DispatchError? = submit(externalId) {
        val operation = owner().beginInitialize()
        tracked(operation.commandId(), { operation.wait(OBSERVATION_TIMEOUT_MS) }) { operation.cancel() }
    }

    fun startScan(externalId: ULong, options: ScanOptions): DispatchError? = submit(externalId) {
        val operation = owner().beginStartScan(options)
        tracked(operation.commandId(), { operation.wait(OBSERVATION_TIMEOUT_MS) }) { operation.cancel() }
    }

    fun stopScan(externalId: ULong, session: ScanSessionId): DispatchError? = submit(externalId) {
        val operation = owner().beginStopScan(session)
        tracked(operation.commandId(), { operation.wait(OBSERVATION_TIMEOUT_MS) }) { operation.cancel() }
    }

    fun connect(externalId: ULong, peripheral: PeripheralId): DispatchError? = submit(externalId) {
        val operation = owner().beginConnect(peripheral)
        tracked(operation.commandId(), { operation.wait(OBSERVATION_TIMEOUT_MS) }) { operation.cancel() }
    }

    fun disconnect(externalId: ULong, connection: ConnectionId): DispatchError? = submit(externalId) {
        val operation = owner().beginDisconnect(connection)
        tracked(operation.commandId(), { operation.wait(OBSERVATION_TIMEOUT_MS) }) { operation.cancel() }
    }

    fun discoverGatt(externalId: ULong, connection: ConnectionId): DispatchError? = submit(externalId) {
        val operation = owner().beginDiscoverGatt(connection)
        tracked(operation.commandId(), { operation.wait(OBSERVATION_TIMEOUT_MS) }) { operation.cancel() }
    }

    fun readRssi(externalId: ULong, connection: ConnectionId): DispatchError? = submit(externalId) {
        val operation = owner().beginReadRssi(connection)
        tracked(operation.commandId(), { operation.wait(OBSERVATION_TIMEOUT_MS) }) { operation.cancel() }
    }

    fun readCharacteristic(
        externalId: ULong,
        connection: ConnectionId,
        session: GattSessionId,
        characteristic: GattCharacteristicId,
    ): DispatchError? = submit(externalId) {
        val operation = owner().beginReadCharacteristic(connection, session, characteristic)
        tracked(operation.commandId(), { operation.wait(OBSERVATION_TIMEOUT_MS) }) { operation.cancel() }
    }

    fun writeCharacteristic(
        externalId: ULong,
        connection: ConnectionId,
        session: GattSessionId,
        characteristic: GattCharacteristicId,
        value: ByteArray,
        writeType: WriteType,
    ): DispatchError? = submit(externalId) {
        val operation = owner().beginWriteCharacteristic(
            connection,
            session,
            characteristic,
            value,
            writeType,
        )
        tracked(operation.commandId(), { operation.wait(OBSERVATION_TIMEOUT_MS) }) { operation.cancel() }
    }

    fun setNotifications(
        externalId: ULong,
        connection: ConnectionId,
        session: GattSessionId,
        subscription: SubscriptionId,
        characteristic: GattCharacteristicId,
        enabled: Boolean,
        mode: NotificationModeRequest,
    ): DispatchError? {
        if (enabled) {
            return submit(externalId) {
                val operation = owner().beginEnableNotifications(connection, session, characteristic, mode)
                tracked(operation.commandId(), { operation.wait(OBSERVATION_TIMEOUT_MS) }) {
                    operation.cancel()
                }
            }
        }
        return submit(externalId) {
            val operation = owner().beginDisableNotifications(connection, session, subscription)
            tracked(operation.commandId(), { operation.wait(OBSERVATION_TIMEOUT_MS) }) { operation.cancel() }
        }
    }

    /** Cancels by the diagnostic projection ID, while BLeng receives the real command ID. */
    fun cancel(externalId: ULong): DispatchError? {
        val tracked = synchronized(stateLock) {
            val operation = actualByExternalId[externalId]
                ?: return DispatchError(
                    DispatchErrorCategory.OPERATION_NOT_FOUND,
                    "operation $externalId is not active",
                    null,
                )
            operations[operation]
                ?: return DispatchError(
                    DispatchErrorCategory.OPERATION_NOT_FOUND,
                    "operation $externalId is not active",
                    null,
                )
        }
        scope.launch {
            try {
                tracked.cancel()
            } catch (error: Throwable) {
                onObservationFailure(error)
            }
        }
        return null
    }

    /** Returns the probe-facing ID for a Rust event command, if it is still tracked. */
    fun externalId(commandId: CommandId?): String? = synchronized(stateLock) {
        commandId?.value?.let {
                    operations[it]?.externalId?.toString()
                        ?: if (initializationCommandId == it || pendingInitializationExternalId != null) {
                            (pendingInitializationExternalId ?: initializationCommandId)?.toString()
                        } else {
                            null
                        }
                }
    }

    /**
     * Retires the typed waiter after the actor has emitted its terminal event. The event remains
     * the probe's delivery path; this wait is the typed-operation consistency check.
     */
    fun complete(commandId: CommandId) {
        val tracked = synchronized(stateLock) {
            val tracked = operations.remove(commandId.value)
            if (tracked == null) {
                if (initializationCommandId == commandId.value || pendingInitializationExternalId != null) {
                    initializationTerminalObserved = true
                    if (initializationCommandId == commandId.value) {
                        initializationCommandId = null
                        pendingInitializationExternalId = null
                    }
                }
                return
            }
            actualByExternalId.remove(tracked.externalId, tracked.commandId)
            tracked
        }
        scope.launch {
            try {
                tracked.waitForTerminal()
            } catch (error: Throwable) {
                onObservationFailure(error)
            }
        }
    }

    fun close() = synchronized(stateLock) {
        operations.clear()
        actualByExternalId.clear()
        reservedExternalIds.clear()
        pendingInitializationExternalId = null
        initializationCommandId = null
        initializationTerminalObserved = false
    }

    private fun owner(): Bleng = synchronized(stateLock) {
        ownerHandle ?: error("BLeng operation adapter owner is not attached")
    }

    private fun submit(
        externalId: ULong,
        create: () -> TrackedOperation,
    ): DispatchError? {
        return synchronized(stateLock) {
            if (!reservedExternalIds.add(externalId) || actualByExternalId.containsKey(externalId)) {
                reservedExternalIds.remove(externalId)
                return@synchronized DispatchError(
                    DispatchErrorCategory.INVALID_REQUEST,
                    "operation $externalId is already active",
                    null,
                )
            }
            try {
                submitTracked(externalId, create())
            } catch (error: Throwable) {
                toDispatchError(error)
            } finally {
                reservedExternalIds.remove(externalId)
            }
        }
    }

    private fun submitTracked(
        externalId: ULong,
        operation: TrackedOperation,
    ): DispatchError? {
        if (actualByExternalId.putIfAbsent(externalId, operation.commandId) != null) {
            return DispatchError(
                DispatchErrorCategory.INVALID_REQUEST,
                "operation $externalId is already active",
                null,
            )
        }
        operations[operation.commandId] = operation.copy(externalId = externalId)
        return null
    }

    private fun <T : Any?> tracked(
        commandId: CommandId,
        wait: suspend () -> T,
        cancel: suspend () -> Unit,
    ): TrackedOperation = TrackedOperation(
        externalId = 0u,
        commandId = commandId.value,
        waitForTerminal = { wait() },
        cancel = cancel,
    )

    private fun toDispatchError(error: Throwable): DispatchError = DispatchError(
        DispatchErrorCategory.FFI_CONTRACT,
        error.message ?: error::class.java.simpleName,
        null,
    )

    private companion object {
        // This is an observation bound after a Rust terminal event, not the caller's BLE timeout.
        const val OBSERVATION_TIMEOUT_MS: ULong = 86_400_000uL
    }
}
