package com.margelo.nitro.blehubreactnativebleng

import com.blehub.reactnative.bleng.AndroidRustSupervisorOwner
import com.blehub.reactnative.bleng.traceClosePhase
import com.margelo.nitro.NitroModules
import com.margelo.nitro.core.Promise
import dev.blehub.AdapterSnapshot
import dev.blehub.AdapterState
import dev.blehub.AuthorizationState
import dev.bleng_supervisor.ForeignOwnerContext
import dev.bleng_supervisor.ForeignScanOptions
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock

/** Lifecycle and synchronous scan submission bridge for the formal BLeng package line. */
class HybridBlengSessionOwner : HybridBlengSessionOwnerSpec() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val lifecycleMutex = Mutex()
    private val applicationContext = requireNotNull(NitroModules.applicationContext) {
        "Nitro application context is unavailable"
    }
    private val ownerFactory: () -> AndroidRustSupervisorOwner = {
        AndroidRustSupervisorOwner(
            context = applicationContext,
            queueLabel = "BleHubReactNativeBleng",
        )
    }
    private var owner: AndroidRustSupervisorOwner? = null
    private var terminalClose: BlengCloseResult? = null
    private data class StartupReceipt(
        val owner: AndroidRustSupervisorOwner,
        val context: ForeignOwnerContext,
    )
    private val receiptLock = Any()
    private var pendingLifecycleRequests = 0
    private var closeEpoch = 0L
    @Volatile private var startupReceipt: StartupReceipt? = null

    override fun start(): Promise<BlengAdapterSnapshot> {
        traceClosePhase("probe.start.enter")
        val ticket = synchronized(receiptLock) {
            pendingLifecycleRequests += 1
            closeEpoch
        }
        return launch(phase = "probe.start") {
            lifecycleMutex.withLock {
                try {
                    val current = synchronized(receiptLock) {
                        check(ticket == closeEpoch) { "Session start was superseded by close." }
                        owner ?: ownerFactory().also {
                            owner = it
                            terminalClose = null
                        }
                    }
                    val started = current.start()
                    val snapshot = started.adapterSnapshot.toBlengAdapterSnapshot()
                    synchronized(receiptLock) {
                        if (ticket == closeEpoch) {
                            startupReceipt = StartupReceipt(current, started.context.copy())
                        }
                    }
                    snapshot
                } finally {
                    finishLifecycleRequest()
                }
            }
        }.also { traceClosePhase("probe.start.returnPromise") }
    }

    override fun beginScan(options: BlengScanOptions): BlengScanSubmission {
        val receipt = synchronized(receiptLock) {
            if (pendingLifecycleRequests == 0) startupReceipt else null
        } ?: return BlengScanSubmission.Second(
            BlengScanFailure(BlengScanMapping.submissionUnavailable()),
        )
        return try {
            BlengScanMapping.submission(receipt.owner.beginScanView(
                receipt.context,
                ForeignScanOptions(
                    serviceUuids = options.serviceUuids.toList(),
                    namePrefix = options.namePrefix,
                    companyId = options.companyId,
                    allowDuplicates = options.allowDuplicates,
                ),
            ))
        } catch (_: Throwable) {
            BlengScanSubmission.Second(BlengScanFailure(BlengScanMapping.bridgeFailure()))
        }
    }

    override fun close(): Promise<BlengCloseResult> {
        traceClosePhase("hybrid.close.enter")
        synchronized(receiptLock) {
            closeEpoch += 1
            startupReceipt = null
            pendingLifecycleRequests += 1
        }
        return launch(phase = "hybrid.close") {
            lifecycleMutex.withLock {
                try {
                    // A newer start can acquire the mutex before this queued close.
                    synchronized(receiptLock) { startupReceipt = null }
                    terminalClose ?: run {
                        val current = owner
                        val receipt = current?.closeResult()
                        val result = if (receipt == null) {
                            BlengCloseResult(ownerClosed = true, failure = null)
                        } else {
                            BlengCloseResult(
                                ownerClosed = receipt.ownerClosed,
                                failure = receipt.failure?.let { BlengCloseFailure(it.code, it.message) },
                            )
                        }
                        if (result.ownerClosed && owner === current) {
                            owner = null
                            terminalClose = result
                        }
                        result
                    }
                } finally {
                    finishLifecycleRequest()
                }
            }
        }.also { traceClosePhase("hybrid.close.returnPromise") }
    }

    private fun finishLifecycleRequest() = synchronized(receiptLock) {
        // Pending requests hide the receipt; only successful start and close change its facts.
        pendingLifecycleRequests -= 1
    }

    private fun <T> launch(phase: String? = null, action: suspend () -> T): Promise<T> {
        val promise = Promise<T>()
        scope.launch {
            if (phase != null) traceClosePhase("$phase.action.enter")
            runCatching {
                action().also {
                    if (phase != null) traceClosePhase("$phase.action.return")
                }
            }
                .onSuccess {
                    if (phase != null) traceClosePhase("$phase.promise.resolve.enter")
                    promise.resolve(it)
                    if (phase != null) traceClosePhase("$phase.promise.resolve.return")
                }
                .onFailure {
                    if (phase != null) traceClosePhase("$phase.promise.reject.enter")
                    promise.reject(it)
                    if (phase != null) traceClosePhase("$phase.promise.reject.return")
                }
        }
        return promise
    }
}

fun AdapterSnapshot.toBlengAdapterSnapshot(): BlengAdapterSnapshot = BlengAdapterSnapshot(
    state = when (state) {
        AdapterState.UNKNOWN -> BlengAdapterState.UNKNOWN
        AdapterState.UNAVAILABLE -> BlengAdapterState.UNAVAILABLE
        AdapterState.POWERED_OFF -> BlengAdapterState.POWEREDOFF
        AdapterState.POWERED_ON -> BlengAdapterState.POWEREDON
    },
    scanAuthorization = scanAuthorization.toBlengAuthorizationState(),
    connectionAuthorization = connectionAuthorization.toBlengAuthorizationState(),
    name = name,
)

private fun AuthorizationState.toBlengAuthorizationState(): BlengAuthorizationState = when (this) {
    AuthorizationState.NOT_REQUIRED -> BlengAuthorizationState.NOTREQUIRED
    AuthorizationState.NOT_DETERMINED -> BlengAuthorizationState.NOTDETERMINED
    AuthorizationState.DENIED -> BlengAuthorizationState.DENIED
    AuthorizationState.RESTRICTED -> BlengAuthorizationState.RESTRICTED
    AuthorizationState.AUTHORIZED -> BlengAuthorizationState.AUTHORIZED
}
