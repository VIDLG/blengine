package com.blehub.reactnative.bleng

import com.margelo.nitro.blehubreactnativebleng.BlengCloseResult
import java.util.concurrent.atomic.AtomicBoolean
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Deferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull

/**
 * Native-only view of one session owned by exactly one RN engine.
 *
 * Implementations must be idempotent for [invalidateForEngine]; this boundary never exposes a
 * JavaScript Promise, a UniFFI handle or a native platform object.
 */
interface EngineOwnedSession {
    /**
     * Claim engine membership before any native work starts.
     *
     * @param permission predecessor retirement gate; `null` when no predecessor was configured.
     * @return false to refuse binding, which permanently prevents this session from starting.
     */
    fun claimEngineBinding(permission: Deferred<Unit>?): Boolean

    /** Terminal, idempotent engine retirement of this session's native resources. */
    fun invalidateForEngine(): Deferred<BlengCloseResult>
}

/** Typed reasons a replacement engine may not start yet. */
enum class BlengEngineRetirementFailure {
    InvalidDeadline,
    RetirementTimedOut,
    PredecessorFailed,
}

class BlengEngineRetirementException(
    val failure: BlengEngineRetirementFailure,
) : Exception(failure.name)

/**
 * Retirement outcome of one engine.
 *
 * @param results per-owner close receipts, in binding order.
 * @param startupFailed whether the predecessor permission itself failed (including cancellation).
 */
data class BlengEngineRetirementRecord(
    val results: List<BlengCloseResult>,
    val startupFailed: Boolean,
) {
    val isClean: Boolean
        get() = !startupFailed && results.all { it.closed && it.failure == null }
}

/**
 * One RN module instance owns one engine's native membership; there is no global current engine.
 *
 * Semantics mirror the Apple lifetime so both platforms age the same way:
 * - admission is sealed permanently at first [invalidate];
 * - retirement retires every bound owner and aggregates their exact close receipts;
 * - a successor may be linked at most once and only before any owner binds;
 * - a successor is gated on the predecessor's successful retirement, never on a timer alone.
 *
 * This class is pure Kotlin so its concurrency contract is verified by JVM tests. The RN
 * engine boundary that calls [invalidate] lives in the Nitro package, not here.
 */
class BlengEngineLifetime(
    private val scope: CoroutineScope = CoroutineScope(SupervisorJob() + Dispatchers.IO),
) {
    companion object {
        // These are cross-platform lifecycle invariants shared with the Apple
        // `BlengEngineLifetime`, not tunables: both platforms must age engines identically, so
        // changing one side requires changing the other in the same review.
        const val MAXIMUM_OWNERS = 64
        const val MIN_TIMEOUT_MS = 1L
        const val MAX_TIMEOUT_MS = 30_000L

        /** Serializes immutable predecessor/successor linking only; holds no engine registry. */
        private val linkageLock = Any()
    }

    private val lock = Any()
    private val owners = ArrayList<EngineOwnedSession>()
    private val completion = CompletableDeferred<BlengEngineRetirementRecord>()
    private var retirementStarted = false
    private var retirementDeferred: CompletableDeferred<List<BlengCloseResult>>? = null
    private var startupPermission: CompletableDeferred<Unit>? = null
    private var permissionJob: Job? = null
    private val permissionFailed = AtomicBoolean(false)
    private var predecessorConfigured = false
    private var successorClaimed = false
    private var hostAdmissionRejected = false

    val boundOwnerCount: Int get() = synchronized(lock) { owners.size }

    val isInvalidated: Boolean get() = synchronized(lock) { retirementStarted }

    /** Seal module-creation failure without impersonating RN's later invalidation callback. */
    fun rejectHostAdmission() = synchronized(lock) { hostAdmissionRejected = true }

    /** Bind one owner; refusal is permanent for that owner and must prevent its start. */
    fun bind(session: EngineOwnedSession): Boolean = synchronized(lock) {
        if (hostAdmissionRejected || retirementStarted || owners.size >= MAXIMUM_OWNERS) {
            return false
        }
        if (!session.claimEngineBinding(startupPermission)) return false
        owners.add(session)
        return true
    }

    /**
     * Link the exact predecessor engine before any owner binds.
     *
     * @param timeoutMs bounded observation window for the predecessor's retirement.
     * @return false for self/duplicate/double-successor/after-owner/already-retired linkups.
     */
    fun configurePredecessor(predecessor: BlengEngineLifetime, timeoutMs: Long): Boolean {
        if (predecessor === this || timeoutMs !in MIN_TIMEOUT_MS..MAX_TIMEOUT_MS) return false
        synchronized(linkageLock) {
            synchronized(lock) {
                if (owners.isNotEmpty() || retirementStarted || predecessorConfigured || successorClaimed) {
                    return false
                }
                val claimed = synchronized(predecessor.lock) {
                    if (predecessor.successorClaimed) {
                        false
                    } else {
                        predecessor.successorClaimed = true
                        true
                    }
                }
                if (!claimed) return false
                predecessorConfigured = true
                val permission = CompletableDeferred<Unit>()
                startupPermission = permission
                val job = scope.launch {
                    predecessor.requireSuccessfulRetirement(timeoutMs)
                    permission.complete(Unit)
                }
                // Observe the job outcome instead of the body: a job cancelled before its first
                // dispatch never runs, yet it must still count as a failed startup permission.
                job.invokeOnCompletion { cause ->
                    if (cause != null) {
                        permissionFailed.set(true)
                        permission.completeExceptionally(cause)
                    }
                }
                permissionJob = job
                return true
            }
        }
    }

    /**
     * Terminal engine retirement. Only seals admission and starts tasks; native cleanup never
     * runs under these locks.
     */
    fun invalidate() {
        val pending: List<Deferred<BlengCloseResult>>
        val deferred: CompletableDeferred<List<BlengCloseResult>>
        val permission: Job?
        synchronized(lock) {
            if (retirementStarted) return
            retirementStarted = true
            pending = owners.map { it.invalidateForEngine() }
            owners.clear()
            deferred = CompletableDeferred()
            retirementDeferred = deferred
            permission = permissionJob
        }
        // Cancelling may resume the recorded retirement task, so it happens outside the lock.
        permission?.cancel()
        scope.launch {
            var startupFailed = false
            if (permission != null) {
                permission.join()
                startupFailed = permissionFailed.get()
            }
            val results = pending.map { it.await() }
            val record = BlengEngineRetirementRecord(results, startupFailed)
            completion.complete(record)
            deferred.complete(results)
        }
    }

    /** Observe-only: null when this engine was never invalidated. Never starts retirement. */
    fun retirementOutcome(): Deferred<List<BlengCloseResult>>? =
        synchronized(lock) { retirementDeferred }

    /** Bounded observation of a future real invalidation callback. */
    suspend fun waitForRetirement(timeoutMs: Long): List<BlengCloseResult> =
        awaitRecord(timeoutMs).results

    /**
     * Gate a successor engine on this engine's retirement.
     *
     * @throws BlengEngineRetirementException when the deadline is invalid, retirement is not
     * observed in time, or any owner closed uncleanly.
     */
    suspend fun requireSuccessfulRetirement(timeoutMs: Long) {
        val record = awaitRecord(timeoutMs)
        currentCoroutineContext().ensureActive()
        if (!record.isClean) {
            throw BlengEngineRetirementException(BlengEngineRetirementFailure.PredecessorFailed)
        }
    }

    private suspend fun awaitRecord(timeoutMs: Long): BlengEngineRetirementRecord {
        if (timeoutMs !in MIN_TIMEOUT_MS..MAX_TIMEOUT_MS) {
            throw BlengEngineRetirementException(BlengEngineRetirementFailure.InvalidDeadline)
        }
        return withTimeoutOrNull(timeoutMs) { completion.await() }
            ?: throw BlengEngineRetirementException(
                BlengEngineRetirementFailure.RetirementTimedOut,
            )
    }
}
