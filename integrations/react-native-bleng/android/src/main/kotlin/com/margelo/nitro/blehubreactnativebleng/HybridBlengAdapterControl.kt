package com.margelo.nitro.blehubreactnativebleng

import com.margelo.nitro.core.Promise
import dev.bleng_supervisor.SupervisedAdapterControl
import dev.bleng_supervisor.SupervisedCompanionAssociation
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch

private val adapterControlBridgeScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

private fun <T> adapterControlPromise(bridgeFailure: () -> T, action: suspend () -> T): Promise<T> {
    val promise = Promise<T>()
    adapterControlBridgeScope.launch {
        val result = try {
            action()
        } catch (_: Throwable) {
            bridgeFailure()
        }
        promise.resolve(result)
    }
    return promise
}

/** One bounded adapter-power receipt; no native handle or device identity is retained here. */
class HybridBlengAdapterControl(
    private val control: SupervisedAdapterControl,
) : HybridBlengAdapterControlSpec() {
    override fun wait(timeoutMs: Double): Promise<BlengReceipt> = adapterControlPromise(
        { BlengReceipt(BlengAdapterMapping.bridgeFailure()) },
    ) { BlengFailureMapping.command(control.waitResult(timeoutMs)) }

    override fun cancel(): Promise<BlengReceipt> = adapterControlPromise(
        { BlengReceipt(BlengAdapterMapping.bridgeFailure()) },
    ) { BlengFailureMapping.command(control.cancelResult()) }
}

/** One bounded companion-association receipt; no native handle or device identity is retained. */
class HybridBlengCompanionAssociation(
    private val association: SupervisedCompanionAssociation,
) : HybridBlengCompanionAssociationSpec() {
    override fun wait(timeoutMs: Double): Promise<BlengReceipt> = adapterControlPromise(
        { BlengReceipt(BlengAdapterMapping.bridgeFailure()) },
    ) { BlengFailureMapping.command(association.waitResult(timeoutMs)) }

    override fun cancel(): Promise<BlengReceipt> = adapterControlPromise(
        { BlengReceipt(BlengAdapterMapping.bridgeFailure()) },
    ) { BlengFailureMapping.command(association.cancelResult()) }
}
