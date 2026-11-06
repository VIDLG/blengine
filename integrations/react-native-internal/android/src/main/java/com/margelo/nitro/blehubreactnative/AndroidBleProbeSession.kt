package com.margelo.nitro.blehubreactnative

import android.os.HandlerThread
import dev.blehub.BleEvent
import kotlinx.coroutines.Deferred

/**
 * Production Android session resource owner used by the private RN integration.
 *
 * The Rust `Bleng` owner is the only command/runtime owner. This class only coordinates its
 * shutdown with the Android backend/thread and handoff barriers; probe-specific evidence remains
 * injected by the owner.
 */
internal class AndroidBleProbeSession(
    private val eventReader: Deferred<Result<Unit>>?,
    private val backendThread: HandlerThread?,
    private val deliveryThread: HandlerThread?,
    private val handoff: AndroidEventHandoff<BleEvent>?,
    private val awaitAssembly: suspend () -> Unit,
    private val assemblyStarted: () -> Boolean,
    private val shutdownOwner: suspend () -> Unit,
    private val disposeUnboundBackend: () -> Unit,
    private val snapshotBackend: () -> Map<String, Int>,
    private val lateRetirementCheck: () -> Unit,
) {
    private var retired = false

    suspend fun retire(): Map<String, Int> {
        check(!retired) { "Android BLE session was retired twice" }
        retired = true
        handoff?.close()

        var failure: Throwable? = null
        var native: Map<String, Int>? = null

        fun cleanup(action: () -> Unit) {
            try {
                action()
            } catch (error: Throwable) {
                if (failure == null) failure = error
                else if (failure !== error) failure!!.addSuppressed(error)
            }
        }

        try {
            awaitAssembly()
        } catch (error: Throwable) {
            failure = error
        }
        val wasAssemblyStarted = assemblyStarted()
        try {
            shutdownOwner()
        } catch (error: Throwable) {
            if (failure == null) failure = error
            else if (failure !== error) failure!!.addSuppressed(error)
        }
        if (!wasAssemblyStarted) {
            cleanup(disposeUnboundBackend)
        }
        try {
            eventReader?.await()?.getOrThrow()
        } catch (error: Throwable) {
            if (failure == null) failure = error
            else if (failure !== error) failure!!.addSuppressed(error)
        }
        cleanup {
            native = snapshotBackend()
        }
        cleanup(lateRetirementCheck)
        cleanup { backendThread?.quitSafely() }
        cleanup { deliveryThread?.quitSafely() }
        cleanup {
            backendThread?.join(5_000L)
            check(backendThread?.isAlive != true) { "Android backend thread did not stop" }
        }
        cleanup {
            deliveryThread?.join(5_000L)
            check(deliveryThread?.isAlive != true) { "Android delivery thread did not stop" }
        }
        cleanup {
            check(handoff?.pendingCount() == null || handoff.pendingCount() == 0) {
                "closed Android handoff still owns accepted delivery jobs"
            }
        }

        val counts = native ?: emptyMap()
        if (failure == null) {
            check(counts.values.all { it == 0 }) {
                "disposed Android backend still owns resources: $counts"
            }
        }
        failure?.let { throw it }
        return counts
    }
}
