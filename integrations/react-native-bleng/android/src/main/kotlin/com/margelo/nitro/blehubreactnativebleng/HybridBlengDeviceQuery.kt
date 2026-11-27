package com.margelo.nitro.blehubreactnativebleng

import com.margelo.nitro.core.Promise
import dev.bleng_supervisor.SupervisedDeviceQuery
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch

private val deviceQueryBridgeScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

private fun <T> deviceQueryPromise(bridgeFailure: () -> T, action: suspend () -> T): Promise<T> {
    val promise = Promise<T>()
    deviceQueryBridgeScope.launch {
        val result = try {
            action()
        } catch (_: Throwable) {
            bridgeFailure()
        }
        promise.resolve(result)
    }
    return promise
}

/** One read-only inventory snapshot; no native handle or address is retained here. */
class HybridBlengDeviceQuery(
    private val query: SupervisedDeviceQuery,
) : HybridBlengDeviceQuerySpec() {
    override fun wait(timeoutMs: Double): Promise<BlengDeviceQueryResult> = deviceQueryPromise(
        { BlengDeviceQueryResult.Second(BlengFailure(BlengAdapterMapping.bridgeFailure())) },
    ) { BlengAdapterMapping.deviceList(query.waitResult(timeoutMs)) }

    override fun cancel(): Promise<BlengReceipt> = deviceQueryPromise(
        { BlengReceipt(BlengAdapterMapping.bridgeFailure()) },
    ) { BlengFailureMapping.command(query.cancelResult()) }
}
