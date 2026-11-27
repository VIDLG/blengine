package com.margelo.nitro.blehubreactnativebleng

import com.margelo.nitro.core.Promise
import dev.bleng_supervisor.ForeignRefreshBeginResult
import dev.bleng_supervisor.SupervisedBond
import dev.bleng_supervisor.SupervisedConnect
import dev.bleng_supervisor.SupervisedConnection
import dev.bleng_supervisor.SupervisedConnectionPriority
import dev.bleng_supervisor.SupervisedDisconnect
import dev.bleng_supervisor.SupervisedPeripheral
import dev.bleng_supervisor.SupervisedRefreshGattCache
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch

private val connectionBridgeScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

private fun <T> connectionPromise(bridgeFailure: () -> T, action: suspend () -> T): Promise<T> {
    val promise = Promise<T>()
    connectionBridgeScope.launch {
        val result = try {
            action()
        } catch (_: Throwable) {
            bridgeFailure()
        }
        promise.resolve(result)
    }
    return promise
}

/** Mechanical projection only: Rust owns validation, failure classification and lifecycle facts. */
internal object BlengConnectionMapping {
    fun bridgeFailure() = BlengFailureMapping.bridgeFailure(
        BlengFailureDomain.DELIVERY,
        "Native connection bridge failed.",
    )

    fun connectSubmission(value: dev.bleng_supervisor.ForeignConnectBeginResult): BlengConnectSubmission {
        val connect = value.connect
        val error = value.failure
        return when {
            connect != null && error == null ->
                BlengConnectSubmission.First(BlengConnectAccepted(HybridBlengConnect(connect)))

            connect == null && error != null ->
                BlengConnectSubmission.Second(BlengFailure(BlengFailureMapping.failure(error)))

            else -> BlengConnectSubmission.Second(BlengFailure(bridgeFailure()))
        }
    }

    fun connectResult(value: dev.bleng_supervisor.ForeignConnectResult): BlengConnectResult {
        val connection = value.connection
        val error = value.failure
        return when {
            connection != null && error == null -> BlengConnectResult.First(
                BlengConnectOutcome(HybridBlengConnection(connection)),
            )

            connection == null && error != null ->
                BlengConnectResult.Second(BlengFailure(BlengFailureMapping.failure(error)))

            else -> BlengConnectResult.Second(BlengFailure(bridgeFailure()))
        }
    }

    fun refreshSubmission(value: ForeignRefreshBeginResult): BlengGattCacheRefreshSubmission {
        val refresh = value.refresh
        val error = value.failure
        return when {
            refresh != null && error == null -> BlengGattCacheRefreshSubmission.First(
                BlengGattCacheRefreshAccepted(HybridBlengGattCacheRefresh(refresh)),
            )

            refresh == null && error != null ->
                BlengGattCacheRefreshSubmission.Second(BlengFailure(BlengFailureMapping.failure(error)))

            else -> BlengGattCacheRefreshSubmission.Second(BlengFailure(bridgeFailure()))
        }
    }

    fun disconnectSubmission(
        value: dev.bleng_supervisor.ForeignDisconnectBeginResult,
    ): BlengDisconnectSubmission {
        val disconnect = value.disconnect
        val error = value.failure
        return when {
            disconnect != null && error == null -> BlengDisconnectSubmission.First(
                BlengDisconnectAccepted(HybridBlengDisconnect(disconnect)),
            )

            disconnect == null && error != null ->
                BlengDisconnectSubmission.Second(BlengFailure(BlengFailureMapping.failure(error)))

            else -> BlengDisconnectSubmission.Second(BlengFailure(bridgeFailure()))
        }
    }

    fun priority(value: BlengConnectionPriority): dev.blehub.ConnectionPriority = when (value) {
        BlengConnectionPriority.BALANCED -> dev.blehub.ConnectionPriority.BALANCED
        BlengConnectionPriority.HIGH -> dev.blehub.ConnectionPriority.HIGH
        BlengConnectionPriority.LOWPOWER -> dev.blehub.ConnectionPriority.LOW_POWER
    }

    fun prioritySubmission(
        value: dev.bleng_supervisor.ForeignPriorityBeginResult,
    ): BlengPrioritySubmission {
        val priority = value.priority
        val error = value.failure
        return when {
            priority != null && error == null -> BlengPrioritySubmission.First(
                BlengPriorityAccepted(HybridBlengPriority(priority)),
            )

            priority == null && error != null ->
                BlengPrioritySubmission.Second(BlengFailure(BlengFailureMapping.failure(error)))

            else -> BlengPrioritySubmission.Second(BlengFailure(bridgeFailure()))
        }
    }

    fun bondState(value: dev.blehub.BondState): BlengBondState = when (value) {
        dev.blehub.BondState.NONE -> BlengBondState.NONE
        dev.blehub.BondState.BONDING -> BlengBondState.BONDING
        dev.blehub.BondState.BONDED -> BlengBondState.BONDED
    }

    fun bondSubmission(
        value: dev.bleng_supervisor.ForeignBondBeginResult,
    ): BlengBondSubmission {
        val bond = value.bond
        val error = value.failure
        return when {
            bond != null && error == null -> BlengBondSubmission.First(
                BlengBondAccepted(HybridBlengBond(bond)),
            )

            bond == null && error != null ->
                BlengBondSubmission.Second(BlengFailure(BlengFailureMapping.failure(error)))

            else -> BlengBondSubmission.Second(BlengFailure(bridgeFailure()))
        }
    }

    fun bondResult(
        value: dev.bleng_supervisor.ForeignBondValueResult,
    ): BlengBondResult {
        val state = value.state
        val error = value.failure
        return when {
            state != null && error == null ->
                BlengBondResult.First(BlengBondOutcome(bondState(state)))

            state == null && error != null ->
                BlengBondResult.Second(BlengFailure(BlengFailureMapping.failure(error)))

            else -> BlengBondResult.Second(BlengFailure(bridgeFailure()))
        }
    }

    fun bondProgress(
        value: dev.bleng_supervisor.ForeignBondProgress,
    ): BlengBondProgressResult {
        val state = value.state
        val error = value.failure
        return when {
            error != null ->
                BlengBondProgressResult.Second(BlengFailure(BlengFailureMapping.failure(error)))

            else -> BlengBondProgressResult.First(
                BlengBondProgress(
                    // The retained end has no state; the terminal receipt stays on `wait`.
                    state = state?.let(::bondState),
                    ended = value.ended,
                ),
            )
        }
    }

    /** One end observation: the retained terminal, or why it could not be observed this time. */
    fun endObservation(
        value: dev.bleng_supervisor.ForeignConnectionEndObservation,
    ): BlengConnectionNext {
        val ended = value.ended
        val error = value.failure
        return when {
            ended != null && error == null -> BlengConnectionNext.First(end(ended))
            ended == null && error != null ->
                BlengConnectionNext.Second(BlengFailure(BlengFailureMapping.failure(error)))

            else -> BlengConnectionNext.Second(BlengFailure(bridgeFailure()))
        }
    }

    private fun end(value: dev.bleng_supervisor.ForeignConnectionEnd): BlengConnectionEndedEvent =
        BlengConnectionEndedEvent(
            BlengEnd(
                reason = when (value.by) {
                    dev.bleng_supervisor.ForeignConnectionEndBy.OPERATOR ->
                        BlengEndReason.REQUESTED

                    dev.bleng_supervisor.ForeignConnectionEndBy.UNREQUESTED ->
                        BlengEndReason.UNREQUESTED

                    dev.bleng_supervisor.ForeignConnectionEndBy.OWNER ->
                        BlengEndReason.OWNERCLOSED
                },
                failure = value.failure?.let { BlengFailureMapping.failure(it) },
            ),
        )
}

/** Holds only the original Rust proxy; no BLE identity or owner reference is retained here. */
class HybridBlengConnect(private val connect: SupervisedConnect) : HybridBlengConnectSpec() {
    override fun wait(timeoutMs: Double): Promise<BlengConnectResult> = connectionPromise(
        { BlengConnectResult.Second(BlengFailure(BlengConnectionMapping.bridgeFailure())) },
    ) { BlengConnectionMapping.connectResult(connect.waitResult(timeoutMs)) }

    override fun cancel(): Promise<BlengReceipt> = connectionPromise(
        { BlengReceipt(BlengConnectionMapping.bridgeFailure()) },
    ) { BlengFailureMapping.command(connect.cancelResult()) }
}

/** Owner-scoped capability; the exact Rust proxy decides which owner may disconnect it. */
class HybridBlengConnection(
    private val connection: SupervisedConnection,
) : HybridBlengConnectionSpec() {
    override fun beginDisconnect(): BlengDisconnectSubmission = try {
        BlengConnectionMapping.disconnectSubmission(connection.beginDisconnect())
    } catch (_: Throwable) {
        BlengDisconnectSubmission.Second(BlengFailure(BlengConnectionMapping.bridgeFailure()))
    }

    override fun beginDiscoverGatt(): BlengGattDiscoverySubmission = try {
        BlengGattMapping.discoverySubmission(connection.beginDiscoverGatt())
    } catch (_: Throwable) {
        BlengGattDiscoverySubmission.Second(BlengFailure(BlengGattMapping.bridgeFailure()))
    }

    override fun beginReadRssi(): BlengRssiSubmission = try {
        BlengGattMapping.rssiSubmission(connection.beginReadRssi())
    } catch (_: Throwable) {
        BlengRssiSubmission.Second(BlengFailure(BlengGattMapping.bridgeFailure()))
    }

    override fun beginRequestMtu(mtu: Double): BlengMtuSubmission = try {
        if (!mtu.isFinite() || mtu % 1.0 != 0.0 || mtu < 23.0 || mtu > 517.0) {
            BlengMtuSubmission.Second(
                BlengFailure(BlengGattMapping.invalidMtu("mtu must be an integer between 23 and 517")),
            )
        } else {
            BlengGattMapping.mtuSubmission(connection.beginRequestMtu(mtu.toInt().toUShort()))
        }
    } catch (_: Throwable) {
        BlengMtuSubmission.Second(BlengFailure(BlengGattMapping.bridgeFailure()))
    }

    override fun beginQueryMtu(): BlengMtuSubmission = try {
        BlengGattMapping.mtuSubmission(connection.beginQueryMtu())
    } catch (_: Throwable) {
        BlengMtuSubmission.Second(BlengFailure(BlengGattMapping.bridgeFailure()))
    }

    override fun beginSetConnectionPriority(
        priority: BlengConnectionPriority,
    ): BlengPrioritySubmission = try {
        BlengConnectionMapping.prioritySubmission(
            connection.beginSetConnectionPriority(BlengConnectionMapping.priority(priority)),
        )
    } catch (_: Throwable) {
        BlengPrioritySubmission.Second(BlengFailure(BlengConnectionMapping.bridgeFailure()))
    }

    override fun beginBond(pin: String?): BlengBondSubmission = try {
        BlengConnectionMapping.bondSubmission(connection.beginCreateBond(pin))
    } catch (_: Throwable) {
        BlengBondSubmission.Second(BlengFailure(BlengConnectionMapping.bridgeFailure()))
    }

    override fun beginRemoveBond(): BlengBondSubmission = try {
        BlengConnectionMapping.bondSubmission(connection.beginRemoveBond())
    } catch (_: Throwable) {
        BlengBondSubmission.Second(BlengFailure(BlengConnectionMapping.bridgeFailure()))
    }

    override fun beginGattCacheRefresh(): BlengGattCacheRefreshSubmission = try {
        BlengConnectionMapping.refreshSubmission(connection.beginRefreshGattCache())
    } catch (_: Throwable) {
        BlengGattCacheRefreshSubmission.Second(BlengFailure(BlengConnectionMapping.bridgeFailure()))
    }

    override fun isConnected(): Boolean = runCatching { connection.isConnected() }.getOrDefault(false)

    override fun localName(): String? = runCatching { connection.localName() }.getOrNull()

    override fun awaitEnd(timeoutMs: Double): Promise<BlengConnectionNext> = connectionPromise(
        { BlengConnectionNext.Second(BlengFailure(BlengConnectionMapping.bridgeFailure())) },
    ) { BlengConnectionMapping.endObservation(connection.awaitEnd(timeoutMs)) }
}

class HybridBlengPriority(
    private val priority: SupervisedConnectionPriority,
) : HybridBlengPrioritySpec() {
    override fun wait(timeoutMs: Double): Promise<BlengReceipt> = connectionPromise(
        { BlengReceipt(BlengConnectionMapping.bridgeFailure()) },
    ) { BlengFailureMapping.command(priority.waitResult(timeoutMs)) }

    override fun cancel(): Promise<BlengReceipt> = connectionPromise(
        { BlengReceipt(BlengConnectionMapping.bridgeFailure()) },
    ) { BlengFailureMapping.command(priority.cancelResult()) }
}

class HybridBlengBond(
    private val bond: SupervisedBond,
) : HybridBlengBondSpec() {
    override fun wait(timeoutMs: Double): Promise<BlengBondResult> = connectionPromise(
        { BlengBondResult.Second(BlengFailure(BlengConnectionMapping.bridgeFailure())) },
    ) { BlengConnectionMapping.bondResult(bond.waitResult(timeoutMs)) }

    override fun cancel(): Promise<BlengReceipt> = connectionPromise(
        { BlengReceipt(BlengConnectionMapping.bridgeFailure()) },
    ) { BlengFailureMapping.command(bond.cancelResult()) }

    override fun nextState(timeoutMs: Double): Promise<BlengBondProgressResult> = connectionPromise(
        { BlengBondProgressResult.Second(BlengFailure(BlengConnectionMapping.bridgeFailure())) },
    ) { BlengConnectionMapping.bondProgress(bond.nextState(timeoutMs)) }
}

class HybridBlengDisconnect(
    private val disconnect: SupervisedDisconnect,
) : HybridBlengDisconnectSpec() {
    override fun wait(timeoutMs: Double): Promise<BlengReceipt> = connectionPromise(
        { BlengReceipt(BlengConnectionMapping.bridgeFailure()) },
    ) { BlengFailureMapping.command(disconnect.waitResult(timeoutMs)) }

    override fun cancel(): Promise<BlengReceipt> = connectionPromise(
        { BlengReceipt(BlengConnectionMapping.bridgeFailure()) },
    ) { BlengFailureMapping.command(disconnect.cancelResult()) }
}

/**
 * Per-advertisement capability. Connecting is admitted only through this observation's own Rust
 * proxy, which carries the exact owner context that produced it.
 */
class HybridBlengPeripheral(
    private val peripheral: SupervisedPeripheral,
) : HybridBlengPeripheralSpec() {
    override fun key(): String = peripheral.key()

    override fun localName(): String? = peripheral.localName()

    override fun beginConnect(): BlengConnectSubmission = try {
        BlengConnectionMapping.connectSubmission(peripheral.beginConnect(false))
    } catch (_: Throwable) {
        BlengConnectSubmission.Second(BlengFailure(BlengConnectionMapping.bridgeFailure()))
    }

    override fun beginAutoConnect(): BlengConnectSubmission = try {
        BlengConnectionMapping.connectSubmission(peripheral.beginConnect(true))
    } catch (_: Throwable) {
        BlengConnectSubmission.Second(BlengFailure(BlengConnectionMapping.bridgeFailure()))
    }
}

/** One bounded GATT-cache refresh receipt; no native handle is retained here. */
class HybridBlengGattCacheRefresh(
    private val refresh: SupervisedRefreshGattCache,
) : HybridBlengGattCacheRefreshSpec() {
    override fun wait(timeoutMs: Double): Promise<BlengReceipt> = connectionPromise(
        { BlengReceipt(BlengConnectionMapping.bridgeFailure()) },
    ) { BlengFailureMapping.command(refresh.waitResult(timeoutMs)) }

    override fun cancel(): Promise<BlengReceipt> = connectionPromise(
        { BlengReceipt(BlengConnectionMapping.bridgeFailure()) },
    ) { BlengFailureMapping.command(refresh.cancelResult()) }
}
