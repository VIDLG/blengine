package com.margelo.nitro.blehubreactnativebleng

import com.margelo.nitro.core.Promise
import dev.bleng_supervisor.SupervisedScan
import dev.bleng_supervisor.SupervisedScanStop
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch

private val scanBridgeScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

private fun <T> scanPromise(bridgeFailure: () -> T, action: suspend () -> T): Promise<T> {
    val promise = Promise<T>()
    scanBridgeScope.launch {
        val result = try {
            action()
        } catch (_: Throwable) {
            bridgeFailure()
        }
        promise.resolve(result)
    }
    return promise
}

/** Holds only the original Rust proxy; no root owner or BLE identity is retained here. */
class HybridBlengScan(private val scan: SupervisedScan) : HybridBlengScanSpec() {
    override fun waitUntilStarted(timeoutMs: Double): Promise<BlengReceipt> = scanPromise(
        { BlengReceipt(BlengScanMapping.bridgeFailure()) },
    ) { BlengFailureMapping.command(scan.startResult(timeoutMs)) }

    override fun cancelStart(): Promise<BlengReceipt> = scanPromise(
        { BlengReceipt(BlengScanMapping.bridgeFailure()) },
    ) { BlengFailureMapping.command(scan.cancelStartResult()) }

    override fun nextEvent(timeoutMs: Double): Promise<BlengScanNext> = scanPromise(
        { BlengScanNext.Third(BlengFailure(BlengScanMapping.bridgeFailure())) },
    ) { BlengScanMapping.observation(scan.nextObservation(timeoutMs)) }

    override fun beginStop(): BlengScanStopSubmission = try {
        BlengScanMapping.stopSubmission(scan.beginStopView())
    } catch (_: Throwable) {
        BlengScanStopSubmission.Second(BlengFailure(BlengScanMapping.bridgeFailure()))
    }

    override fun isScanning(): Boolean = runCatching { scan.isScanning() }.getOrDefault(false)

    override fun dispose() {
        // Detach is an observation action, never a native stop or start cancellation.
        runCatching { scan.detach() }
        super.dispose()
    }
}

class HybridBlengScanStop(private val operation: SupervisedScanStop) : HybridBlengScanStopSpec() {
    override fun wait(timeoutMs: Double): Promise<BlengReceipt> = scanPromise(
        { BlengReceipt(BlengScanMapping.bridgeFailure()) },
    ) { BlengFailureMapping.command(operation.waitResult(timeoutMs)) }

    override fun cancel(): Promise<BlengReceipt> = scanPromise(
        { BlengReceipt(BlengScanMapping.bridgeFailure()) },
    ) { BlengFailureMapping.command(operation.cancelResult()) }
}
