package com.blehub.reactnative.bleng

import android.content.Context
import android.os.Handler
import android.os.HandlerThread
import dev.blehub.BleEvent
import dev.blehub.android.AndroidBleBackend
import dev.blehub.android.AndroidBleResourceSnapshot
import dev.blehub.android.AndroidBleSession
import dev.blehub.android.AndroidBleSessionIdentity
import dev.blehub.android.currentAndroidBootId
import dev.blehub.android.currentAndroidProcessId
import dev.bleng.Bleng
import java.util.UUID
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.launch

/**
 * Formal production adapter for one Android BLeng owner/backend lifetime.
 *
 * This module depends only on published `dev.blehub:bleng` and `dev.blehub:blehub-android`
 * artifacts. It is intentionally below Nitro: a future RN bridge owns the operation projection,
 * while this session owns Bleng, Android backend, event reading and native retirement.
 */
class AndroidBlengSession private constructor(
    override val identity: AndroidBleSessionIdentity,
    val owner: Bleng,
    private val backend: AndroidBleBackend,
    private val backendThread: HandlerThread,
    private val eventReader: Job,
) : AndroidBleSession {
    override suspend fun retire(): AndroidBleResourceSnapshot {
        var failure: Throwable? = null
        eventReader.cancelAndJoin()

        try {
            owner.shutdown()
        } catch (error: Throwable) {
            failure = error
        }

        val resources = try {
            backend.snapshotRfResources()
        } catch (error: Throwable) {
            if (failure == null) failure = error else failure!!.addSuppressed(error)
            emptyMap()
        }

        try {
            owner.destroy()
        } catch (error: Throwable) {
            if (failure == null) failure = error else failure!!.addSuppressed(error)
        }

        backendThread.quitSafely()
        backendThread.join(5_000L)
        if (backendThread.isAlive) {
            val error = IllegalStateException("Android BLeng backend thread did not stop")
            if (failure == null) failure = error else failure!!.addSuppressed(error)
        }
        if (resources.values.any { it != 0 }) {
            val error = IllegalStateException(
                "Android BLeng backend retained resources after shutdown: $resources",
            )
            if (failure == null) failure = error else failure!!.addSuppressed(error)
        }
        failure?.let { throw it }
        return resources
    }

    companion object {
        suspend fun create(
            context: Context,
            queueLabel: String,
            eventSink: ((BleEvent) -> Unit)? = null,
        ): AndroidBlengSession {
            val backendThread = HandlerThread(queueLabel).also { it.start() }
            val backend = AndroidBleBackend(context, Handler(backendThread.looper))
            val owner = Bleng()
            try {
                owner.assembleBackend(backend)
                val eventReader = CoroutineScope(SupervisorJob() + Dispatchers.IO).launch {
                    while (true) {
                        val event = owner.nextEvent(300_000uL) ?: break
                        eventSink?.invoke(event)
                    }
                }
                return AndroidBlengSession(
                    identity = AndroidBleSessionIdentity(
                        pid = currentAndroidProcessId(),
                        bootId = currentAndroidBootId(),
                        ownerId = UUID.randomUUID().toString(),
                        backendId = UUID.randomUUID().toString(),
                    ),
                    owner = owner,
                    backend = backend,
                    backendThread = backendThread,
                    eventReader = eventReader,
                )
            } catch (failure: Throwable) {
                runCatching { owner.shutdown() }
                runCatching { owner.destroy() }
                runCatching { backend.dispose() }
                backendThread.quitSafely()
                backendThread.join(5_000L)
                throw failure
            }
        }
    }
}
