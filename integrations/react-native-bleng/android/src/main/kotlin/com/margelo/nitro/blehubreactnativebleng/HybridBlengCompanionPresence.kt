package com.margelo.nitro.blehubreactnativebleng

import com.margelo.nitro.core.Promise
import dev.bleng_supervisor.SupervisedCompanionPresenceObservation
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch

private val companionPresenceBridgeScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

/** Holds only the Rust presence-observation proxy; observation ends when this wrapper goes away. */
class HybridBlengCompanionPresence(
    private val observation: SupervisedCompanionPresenceObservation,
) : HybridBlengCompanionPresenceSpec() {
    override fun nextEvent(timeoutMs: Double): Promise<BlengCompanionPresenceNext> {
        val promise = Promise<BlengCompanionPresenceNext>()
        companionPresenceBridgeScope.launch {
            val result = try {
                BlengAdapterMapping.companionPresenceNext(observation.nextEvent(timeoutMs))
            } catch (_: Throwable) {
                BlengCompanionPresenceNext.Third(
                    BlengFailure(BlengAdapterMapping.bridgeFailure()),
                )
            }
            promise.resolve(result)
        }
        return promise
    }

    override fun dispose() {
        // Detach is an observation action, never an association change or engine retirement.
        runCatching { observation.detach() }
        super.dispose()
    }
}
