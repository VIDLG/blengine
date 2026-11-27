package com.margelo.nitro.blehubreactnativebleng

import com.margelo.nitro.core.Promise
import dev.bleng_supervisor.SupervisedAdapterObservation
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch

private val adapterBridgeScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

/** Holds only the Rust adapter-observation proxy; observation ends when this wrapper goes away. */
class HybridBlengAdapterObservation(
    private val observation: SupervisedAdapterObservation,
) : HybridBlengAdapterObservationSpec() {
    override fun nextEvent(timeoutMs: Double): Promise<BlengAdapterNext> {
        val promise = Promise<BlengAdapterNext>()
        adapterBridgeScope.launch {
            val result = try {
                BlengAdapterMapping.observation(observation.nextSnapshot(timeoutMs))
            } catch (_: Throwable) {
                BlengAdapterNext.Third(BlengFailure(BlengAdapterMapping.bridgeFailure()))
            }
            promise.resolve(result)
        }
        return promise
    }

    override fun dispose() {
        // Detach is an observation action, never a radio change or engine retirement.
        runCatching { observation.detach() }
        super.dispose()
    }
}
