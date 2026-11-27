package com.margelo.nitro.blehubreactnativebleng

import com.blehub.reactnative.bleng.AndroidRustSupervisorOwner
import com.blehub.reactnative.bleng.BlengEngineLifetime
import com.blehub.reactnative.bleng.BlengEngineRetirementException
import com.blehub.reactnative.bleng.BlengEngineRetirementFailure
import com.blehub.reactnative.bleng.EngineOwnedSession
import com.blehub.reactnative.bleng.traceClosePhase
import com.margelo.nitro.NitroModules
import com.margelo.nitro.core.Promise
import dev.blehub.AdapterSnapshot
import dev.blehub.AdapterState
import dev.blehub.AndroidScanMode
import dev.blehub.AndroidScanPhy
import dev.blehub.AuthorizationState
import dev.bleng_supervisor.ForeignAndroidScanOptions
import dev.bleng_supervisor.ForeignManufacturerDataFilter
import dev.bleng_supervisor.ForeignOwnerContext
import dev.bleng_supervisor.ForeignScanOptions
import dev.bleng_supervisor.SupervisorFfiException
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Deferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock

/** Lifecycle and synchronous scan submission bridge for the formal BLeng package line. */
class HybridBlengCentral : HybridBlengCentralSpec(), EngineOwnedSession {
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

    @Volatile
    private var startupReceipt: StartupReceipt? = null

    /** Engine membership is a native concern; JS can never reach these members. */
    private val engineLock = Any()
    private var engineRetirement: Deferred<BlengCloseResult>? = null
    private var engineBound = false
    private var startupPermission: Deferred<Unit>? = null

    init {
        // Bind to the exact engine that created this owner. There is no process-global fallback:
        // an unknown context leaves the owner unbound and therefore unable to start.
        BleHubBlengEngineLifecycleModule.lifetimeFor(applicationContext)?.bind(this)
    }

    override fun claimEngineBinding(permission: Deferred<Unit>?): Boolean =
        synchronized(engineLock) {
            if (engineBound || engineRetirement != null) return false
            startupPermission = permission
            engineBound = true
            true
        }

    /**
     * Terminal engine retirement. Idempotent; shares one receipt with repeated invalidations and
     * with any close that observes it. Native cleanup never runs under the engine lock.
     */
    override fun invalidateForEngine(): Deferred<BlengCloseResult> {
        val deferred: CompletableDeferred<BlengCloseResult>
        synchronized(engineLock) {
            engineRetirement?.let { return it }
            deferred = CompletableDeferred()
            engineRetirement = deferred
        }
        // Revoke scan admission and supersede in-flight lifecycle tickets before awaiting cleanup.
        synchronized(receiptLock) {
            closeEpoch += 1
            startupReceipt = null
            pendingLifecycleRequests += 1
        }
        scope.launch {
            lifecycleMutex.withLock {
                try {
                    deferred.complete(closeOwnerLocked())
                } catch (error: Throwable) {
                    // Never report a bridge failure as a clean closure; a successor must refuse.
                    deferred.complete(
                        BlengCloseResult(
                            closed = false,
                            failure = BlengFailureInfo(
                                domain = BlengFailureDomain.LIFECYCLE,
                                code = "engine_retirement_failed",
                                message = error.message ?: "Android engine retirement failed",
                                platform = null,
                            ),
                        )
                    )
                } finally {
                    finishLifecycleRequest()
                }
            }
        }
        return deferred
    }

    override fun initialize(): Promise<BlengInitializeResult> {
        traceClosePhase("probe.start.enter")
        val ticket = synchronized(receiptLock) {
            pendingLifecycleRequests += 1
            closeEpoch
        }
        val (bound, permission) = synchronized(engineLock) {
            engineBound to startupPermission
        }
        return launch(phase = "probe.start") {
            try {
                if (!bound) {
                    throw SessionStartFailure(
                        "engine_unbound",
                        "BLeng session owner is not bound to an RN engine; the engine lifecycle " +
                                "module must be registered for this ReactApplicationContext",
                    )
                }
                // Engine invalidation is permanent: a retired engine's runtime must never
                // resurrect native work, even if JS calls start() again before teardown.
                if (synchronized(engineLock) { engineRetirement } != null) {
                    throw SessionStartFailure(
                        "engine_invalidated",
                        "Session owner belongs to an engine that has already been retired.",
                    )
                }
                // A predecessor gate is awaited outside the lifecycle mutex so it cannot deadlock
                // against that predecessor's own retirement.
                permission?.await()
                lifecycleMutex.withLock {
                    val current = synchronized(receiptLock) {
                        if (ticket != closeEpoch) {
                            throw SessionStartFailure(
                                "superseded_by_close",
                                "Session start was superseded by close.",
                            )
                        }
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
                    BlengInitializeResult.First(BlengInitializeOutcome(snapshot))
                }
            } catch (error: Throwable) {
                // A transition failure is data here, exactly like close() reports one. Native work
                // may still be running, so this classifies the failure rather than proving a stop.
                BlengInitializeResult.Second(BlengFailure(initializeFailure(error)))
            } finally {
                finishLifecycleRequest()
            }
        }.also { traceClosePhase("probe.start.returnPromise") }
    }

    override fun beginScan(options: BlengScanOptions): BlengScanSubmission {
        val receipt = synchronized(receiptLock) {
            if (pendingLifecycleRequests == 0) startupReceipt else null
        } ?: return BlengScanSubmission.Second(
            BlengFailure(BlengScanMapping.submissionUnavailable()),
        )
        return try {
            BlengScanMapping.submission(
                receipt.owner.beginScanView(
                    receipt.context,
                    ForeignScanOptions(
                        serviceUuids = options.serviceUuids.toList(),
                        namePrefix = options.namePrefix,
                        exactName = options.exactName,
                        companyId = options.companyId,
                        minRssi = options.minRssi,
                        manufacturerData = options.manufacturerData?.let { filter ->
                            ForeignManufacturerDataFilter(
                                companyId = filter.companyId,
                                data = filter.data.toByteArray(),
                                mask = filter.mask?.toByteArray(),
                            )
                        },
                        android = options.android?.let { android ->
                            ForeignAndroidScanOptions(
                                scanMode = android.scanMode?.let(::androidScanMode),
                                phy = android.phy?.let(::androidScanPhy),
                                reportDelayMs = android.reportDelayMs,
                                legacy = android.legacy,
                            )
                        },
                        allowDuplicates = options.allowDuplicates,
                    ),
                )
            )
        } catch (_: Throwable) {
            BlengScanSubmission.Second(BlengFailure(BlengScanMapping.bridgeFailure()))
        }
    }

    override fun beginAdapterControl(enabled: Boolean): BlengAdapterControlSubmission {
        val receipt = synchronized(receiptLock) {
            if (pendingLifecycleRequests == 0) startupReceipt else null
        } ?: return BlengAdapterControlSubmission.Second(
            BlengFailure(BlengAdapterMapping.submissionUnavailable()),
        )
        return try {
            BlengAdapterMapping.controlSubmission(
                receipt.owner.beginSetAdapterEnabledView(receipt.context, enabled),
            )
        } catch (_: Throwable) {
            BlengAdapterControlSubmission.Second(
                BlengFailure(BlengAdapterMapping.bridgeFailure()),
            )
        }
    }

    override fun beginCompanionAssociation(
        serviceUuids: Array<String>,
        displayName: String?,
        deviceProfile: String?,
        notifyOnDeviceNearby: Boolean?,
    ): BlengCompanionAssociationSubmission {
        val receipt = synchronized(receiptLock) {
            if (pendingLifecycleRequests == 0) startupReceipt else null
        } ?: return BlengCompanionAssociationSubmission.Second(
            BlengFailure(BlengAdapterMapping.submissionUnavailable()),
        )
        return try {
            BlengAdapterMapping.companionAssociationSubmission(
                receipt.owner.beginAssociateCompanionView(
                    receipt.context,
                    serviceUuids.toList(),
                    displayName,
                    deviceProfile,
                    notifyOnDeviceNearby ?: false,
                ),
            )
        } catch (_: Throwable) {
            BlengCompanionAssociationSubmission.Second(
                BlengFailure(BlengAdapterMapping.bridgeFailure()),
            )
        }
    }

    override fun beginDeviceQuery(
        kind: BlengDeviceQueryKind,
        serviceUuids: Array<String>,
    ): BlengDeviceQuerySubmission {
        val receipt = synchronized(receiptLock) {
            if (pendingLifecycleRequests == 0) startupReceipt else null
        } ?: return BlengDeviceQuerySubmission.Second(
            BlengFailure(BlengAdapterMapping.submissionUnavailable()),
        )
        return try {
            BlengAdapterMapping.querySubmission(
                receipt.owner.beginQueryDevicesView(
                    receipt.context,
                    BlengAdapterMapping.deviceQueryKind(kind),
                    serviceUuids.toList(),
                ),
            )
        } catch (_: Throwable) {
            BlengDeviceQuerySubmission.Second(
                BlengFailure(BlengAdapterMapping.bridgeFailure()),
            )
        }
    }

    override fun adoptPeripheral(key: String): BlengPeripheralAdoptSubmission {
        val receipt = synchronized(receiptLock) {
            if (pendingLifecycleRequests == 0) startupReceipt else null
        } ?: return BlengPeripheralAdoptSubmission.Second(
            BlengFailure(BlengAdapterMapping.submissionUnavailable()),
        )
        return try {
            BlengAdapterMapping.adoptSubmission(
                receipt.owner.adoptPeripheral(receipt.context, key),
            )
        } catch (_: Throwable) {
            BlengPeripheralAdoptSubmission.Second(
                BlengFailure(BlengAdapterMapping.bridgeFailure()),
            )
        }
    }

    override fun beginAdapterObservation(): BlengAdapterObservationSubmission {
        val receipt = synchronized(receiptLock) {
            if (pendingLifecycleRequests == 0) startupReceipt else null
        } ?: return BlengAdapterObservationSubmission.Second(
            BlengFailure(BlengAdapterMapping.submissionUnavailable()),
        )
        return try {
            BlengAdapterMapping.submission(receipt.owner.beginObserveAdapterView(receipt.context))
        } catch (_: Throwable) {
            BlengAdapterObservationSubmission.Second(BlengFailure(BlengAdapterMapping.bridgeFailure()))
        }
    }

    override fun beginCompanionPresence(): BlengCompanionPresenceSubmission {
        val receipt = synchronized(receiptLock) {
            if (pendingLifecycleRequests == 0) startupReceipt else null
        } ?: return BlengCompanionPresenceSubmission.Second(
            BlengFailure(BlengAdapterMapping.submissionUnavailable()),
        )
        return try {
            BlengAdapterMapping.companionPresenceSubmission(
                receipt.owner.beginObserveCompanionPresenceView(receipt.context),
            )
        } catch (_: Throwable) {
            BlengCompanionPresenceSubmission.Second(
                BlengFailure(BlengAdapterMapping.bridgeFailure()),
            )
        }
    }

    private fun androidScanMode(value: BlengAndroidScanMode): AndroidScanMode = when (value) {
        BlengAndroidScanMode.LOWPOWER -> AndroidScanMode.LOW_POWER
        BlengAndroidScanMode.BALANCED -> AndroidScanMode.BALANCED
        BlengAndroidScanMode.LOWLATENCY -> AndroidScanMode.LOW_LATENCY
        BlengAndroidScanMode.OPPORTUNISTIC -> AndroidScanMode.OPPORTUNISTIC
    }

    private fun androidScanPhy(value: BlengAndroidScanPhy): AndroidScanPhy = when (value) {
        BlengAndroidScanPhy.LE1M -> AndroidScanPhy.LE1M
        BlengAndroidScanPhy.LE2M -> AndroidScanPhy.LE2M
        BlengAndroidScanPhy.LECODED -> AndroidScanPhy.LE_CODED
    }

    override fun close(): Promise<BlengCloseResult> {
        traceClosePhase("hybrid.close.enter")
        synchronized(receiptLock) {
            closeEpoch += 1
            startupReceipt = null
            pendingLifecycleRequests += 1
        }
        return launch(phase = "hybrid.close") {
            val retirement = synchronized(engineLock) { engineRetirement }
            try {
                if (retirement != null) {
                    // Shared engine receipt; awaiting outside the mutex avoids deadlock.
                    retirement.await()
                } else {
                    lifecycleMutex.withLock { closeOwnerLocked() }
                }
            } finally {
                finishLifecycleRequest()
            }
        }.also { traceClosePhase("hybrid.close.returnPromise") }
    }

    /** Caller must hold [lifecycleMutex]. */
    private suspend fun closeOwnerLocked(): BlengCloseResult {
        synchronized(receiptLock) { startupReceipt = null }
        // A newer start can acquire the mutex before this queued close.
        terminalClose?.let { return it }
        val current = owner
        if (current == null) {
            val result = BlengCloseResult(closed = true, failure = null)
            terminalClose = result
            return result
        }
        val receipt = current.closeResult()
        val result = BlengCloseResult(
            closed = receipt.ownerClosed,
            failure = receipt.failure?.let {
                BlengFailureInfo(
                    domain = BlengFailureDomain.LIFECYCLE,
                    code = it.code,
                    message = it.message,
                    platform = null,
                )
            },
        )
        if (result.closed && owner === current) {
            owner = null
            terminalClose = result
        }
        return result
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

/** A synchronous admission refusal thrown by [HybridBlengCentral.initialize]. */
private class SessionStartFailure(
    val code: String,
    override val message: String,
) : IllegalStateException(message)

/** Classifies only the owner-assembly errors `initialize()` can raise, as data. */
private fun initializeFailure(error: Throwable): BlengFailureInfo = when (error) {
    is SupervisorFfiException -> BlengFailureMapping.supervisorFailure(error)
    is SessionStartFailure -> lifecycleFailure(error.code, error.message)
    is BlengEngineRetirementException -> lifecycleFailure(
        when (error.failure) {
            BlengEngineRetirementFailure.InvalidDeadline -> "invalid_deadline"
            BlengEngineRetirementFailure.RetirementTimedOut -> "retirement_timed_out"
            BlengEngineRetirementFailure.PredecessorFailed -> "predecessor_failed"
        },
        error.message ?: error.toString(),
    )

    is CancellationException -> lifecycleFailure("cancelled", "Session start was cancelled.")
    else -> BlengFailureMapping.bridgeFailure(
        BlengFailureDomain.LIFECYCLE,
        error.message ?: error.toString(),
    )
}

private fun lifecycleFailure(code: String, message: String) = BlengFailureInfo(
    domain = BlengFailureDomain.LIFECYCLE,
    code = code,
    message = message,
    platform = null,
)

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
