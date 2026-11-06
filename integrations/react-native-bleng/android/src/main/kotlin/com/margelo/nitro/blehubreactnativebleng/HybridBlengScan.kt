package com.margelo.nitro.blehubreactnativebleng

import com.margelo.nitro.core.Promise
import dev.bleng_supervisor.SupervisedPeripheral
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
    override fun waitUntilStarted(timeoutMs: Double): Promise<BlengScanCommandResult> = scanPromise(
        { BlengScanCommandResult(BlengScanMapping.bridgeFailure()) },
    ) { BlengScanMapping.command(scan.startResult(timeoutMs)) }

    override fun cancelStart(): Promise<BlengScanCommandResult> = scanPromise(
        { BlengScanCommandResult(BlengScanMapping.bridgeFailure()) },
    ) { BlengScanMapping.command(scan.cancelStartResult()) }

    override fun nextEvent(timeoutMs: Double): Promise<BlengScanNext> = scanPromise(
        { BlengScanNext.Third(BlengScanFailure(BlengScanMapping.bridgeFailure())) },
    ) { BlengScanMapping.observation(scan.nextObservation(timeoutMs)) }

    override fun beginStop(): BlengScanStopSubmission = try {
        BlengScanMapping.stopSubmission(scan.beginStopView())
    } catch (_: Throwable) {
        BlengScanStopSubmission.Second(BlengScanFailure(BlengScanMapping.bridgeFailure()))
    }

    override fun dispose() {
        // Detach is an observation action, never a native stop or start cancellation.
        runCatching { scan.detach() }
        super.dispose()
    }
}

class HybridBlengScanStop(private val operation: SupervisedScanStop) : HybridBlengScanStopSpec() {
    override fun wait(timeoutMs: Double): Promise<BlengScanCommandResult> = scanPromise(
        { BlengScanCommandResult(BlengScanMapping.bridgeFailure()) },
    ) { BlengScanMapping.command(operation.waitResult(timeoutMs)) }

    override fun cancel(): Promise<BlengScanCommandResult> = scanPromise(
        { BlengScanCommandResult(BlengScanMapping.bridgeFailure()) },
    ) { BlengScanMapping.command(operation.cancelResult()) }
}

/** Native-created opaque capability. There is deliberately no JS identity or factory surface. */
class HybridBlengPeripheral(private val peripheral: SupervisedPeripheral) : HybridBlengPeripheralSpec()
