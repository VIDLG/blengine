package com.blehub.reactnative.bleng

import android.content.Context
import android.os.Handler
import android.os.HandlerThread
import dev.blehub.BleEvent
import dev.blehub.android.AndroidBleBackend
import dev.bleng.Bleng
import dev.bleng_supervisor.ForeignOwnerContext
import dev.bleng_supervisor.ForeignPlatformActionResult
import dev.bleng_supervisor.ForeignPlatformOwnerSession
import dev.bleng_supervisor.ForeignPlatformOwnerSessionFactory
import dev.bleng_supervisor.ForeignPlatformRecoveryAction
import dev.bleng_supervisor.ForeignRecoverySignal
import dev.bleng_supervisor.ForeignResourceCount
import dev.bleng_supervisor.ForeignScanEventEnvelope
import dev.bleng_supervisor.SessionEventProjection
import dev.bleng_supervisor.SupervisorFfiException

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Deferred
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.async
import kotlinx.coroutines.withContext

/**
 * Native Android session owned by the Rust Supervisor.
 *
 * Scan projection is synchronous, but retirement cannot retract an already dispatched envelope.
 * Consumers that queue envelopes must check the full sourceContext before applying them.
 */
class AndroidPlatformSession private constructor(
    private val sessionContext: ForeignOwnerContext,
    private val backendObject: AndroidBleBackend,
    private val backendThread: HandlerThread,
    private val eventSink: ((BleEvent) -> Unit)?,
    private val ownerSink: ((Bleng) -> Unit)?,
    private val scanEventSink: ((ForeignScanEventEnvelope) -> Unit)?,
) : ForeignPlatformOwnerSession {
    private val readerScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val readerLock = Any()
    private val ownedBackendThread = OwnedBackendThread(backendThread, { backendThread.quitSafely() })
    private var owner: Bleng? = null
    private var eventReader: Deferred<Result<Unit>>? = null

    override fun context(): ForeignOwnerContext = sessionContext

    override fun backend(): AndroidBleBackend = backendObject

    override fun disposeUnassembled() {
        var failure: Throwable? = null
        try {
            backendObject.dispose()
        } catch (error: Throwable) {
            failure = error
        }
        try {
            stopBackendThread()
        } catch (error: Throwable) {
            if (failure == null) failure = error else failure!!.addSuppressed(error)
        }
        failure?.let { throw SupervisorFfiException.Platform("Android session disposal failed: $it") }
    }

    override fun startEventReader(owner: Bleng, eventProjection: SessionEventProjection) {
        synchronized(readerLock) {
            check(eventReader == null) { "Android event reader already started" }
            this.owner = owner
            ownerSink?.invoke(owner)
            eventReader = readerScope.async {
                try {
                    drainBlengEvents(
                        nextEvent = { owner.nextEvent(300_000uL) },
                        onEvent = { event ->
                            eventProjection.project(event)?.let { scanEventSink?.invoke(it) }
                            eventSink?.invoke(event)
                        },
                    )
                    Result.success(Unit)
                } catch (error: Throwable) {
                    Result.failure(error)
                } finally {
                    eventProjection.destroy()
                }
            }
        }
    }

    override suspend fun waitEventReader() {
        traceClosePhase("platform.waitEventReader.enter")
        try {
            val reader = synchronized(readerLock) {
                val value = eventReader
                eventReader = null
                value
            }
            if (reader != null) {
                reader.await().getOrElse { throw SupervisorFfiException.Platform("Android event reader failed: $it") }
            }
            traceClosePhase("platform.waitEventReader.return")
        } catch (error: Throwable) {
            traceClosePhase("platform.waitEventReader.fail")
            throw error
        }
    }

    /** Terminal retirement barrier, called after Bleng shutdown and reader drain. */
    override suspend fun resourceSnapshot(): List<ForeignResourceCount> {
        traceClosePhase("platform.resourceSnapshot.enter")
        try {
            val snapshot = withContext(Dispatchers.IO) {
                // The backend snapshot posts to its Handler; read it before retiring that worker.
                val backendCounts = ownedBackendThread.snapshotAndStop {
                    backendObject.snapshotRfResources().map { (name, count) ->
                        check(count >= 0) { "Android resource count is negative for $name: $count" }
                        ForeignResourceCount(name, count.toULong())
                    }
                }
                (backendCounts + ForeignResourceCount("backendThreads", if (backendThread.isAlive) 1uL else 0uL))
                    .sortedBy { it.name }
            }
            traceClosePhase("platform.resourceSnapshot.return")
            return snapshot
        } catch (error: Throwable) {
            traceClosePhase("platform.resourceSnapshot.fail")
            throw error
        }
    }

    override suspend fun executeRecoveryAction(
        sequence: ULong,
        action: ForeignPlatformRecoveryAction,
    ): ForeignPlatformActionResult {
        throw SupervisorFfiException.Platform(
            "Android formal lifecycle session does not expose recovery action $action",
        )
    }

    private fun stopBackendThread() = ownedBackendThread.stop()

    companion object {
        fun createWithCallbacks(
            context: Context,
            requested: ForeignOwnerContext,
            queueLabel: String,
            eventSink: ((BleEvent) -> Unit)? = null,
            ownerSink: ((Bleng) -> Unit)? = null,
            scanEventSink: ((ForeignScanEventEnvelope) -> Unit)? = null,
        ): AndroidPlatformSession = create(
            context = context,
            requested = requested,
            queueLabel = queueLabel,
            eventSink = eventSink,
            ownerSink = ownerSink,
            scanEventSink = scanEventSink,
        )

        fun create(
            context: Context,
            requested: ForeignOwnerContext,
            queueLabel: String,
            eventSink: ((BleEvent) -> Unit)? = null,
            ownerSink: ((Bleng) -> Unit)? = null,
            scanEventSink: ((ForeignScanEventEnvelope) -> Unit)? = null,
        ): AndroidPlatformSession {
            val thread = HandlerThread("$queueLabel-g${requested.ownerGeneration}").also { it.start() }
            return try {
                AndroidPlatformSession(
                    sessionContext = requested,
                    backendObject = AndroidBleBackend(context, Handler(thread.looper)),
                    backendThread = thread,
                    eventSink = eventSink,
                    ownerSink = ownerSink,
                    scanEventSink = scanEventSink,
                )
            } catch (error: Throwable) {
                thread.quitSafely()
                thread.join(5_000L)
                throw SupervisorFfiException.Platform("Android backend creation failed: $error")
            }
        }
    }
}

/** Creates native Android sessions for Rust-supervised owner generations. */
class AndroidPlatformSessionFactory(
    private val context: Context,
    private val queueLabel: String,
    private val scanEventSink: ((ForeignScanEventEnvelope) -> Unit)? = null,
) : ForeignPlatformOwnerSessionFactory {
    override fun requiredZeroResourceNames(): List<String> = REQUIRED_ZERO_RESOURCE_NAMES

    override fun create(context: ForeignOwnerContext): ForeignPlatformOwnerSession =
        AndroidPlatformSession.create(
            context = this.context,
            requested = context,
            queueLabel = queueLabel,
            scanEventSink = scanEventSink,
        )

    companion object {
        /** Backend resources and the session-owned worker, checked after retirement. */
        val REQUIRED_ZERO_RESOURCE_NAMES: List<String> = listOf(
            "activeScans",
            "connections",
            "gattAttributes",
            "subscriptions",
            "pendingIo",
            "watchdogs",
            "callbackRoutes",
            "ingressRoots",
            "peerCacheEntries",
            "adapterDelegates",
            "adapterReceivers",
            "backendLeases",
            "backendThreads",
        )
    }
}
