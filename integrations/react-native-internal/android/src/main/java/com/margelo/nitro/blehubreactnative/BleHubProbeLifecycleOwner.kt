package com.margelo.nitro.blehubreactnative

import com.facebook.react.ReactHost
import com.facebook.react.bridge.ReactApplicationContext
import java.util.IdentityHashMap
import java.util.UUID
import java.util.WeakHashMap
import java.util.concurrent.CompletableFuture

/** Private host diagnostic configuration, captured separately for each React engine. */
data class PendingReadEngineConfig(val session: PendingReadReloadSession, val generation: Int)

/** One host/run, one reload. This future belongs to native code, not the retired JS runtime. */
class PendingReadReloadSession(val runId: String) {
    private val lock = Any()
    private var generation = 1
    private var reloadRequested = false
    private var retirement: CompletableFuture<Unit>? = null

    init {
        require(isCanonicalRunId(runId)) { "rfPendingReadRunId must be a canonical UUID" }
    }

    fun capture(): PendingReadEngineConfig = synchronized(lock) {
        PendingReadEngineConfig(this, generation)
    }

    fun requestReload(candidateRunId: String): Boolean = synchronized(lock) {
        if (candidateRunId != runId || generation != 1 || reloadRequested) return false
        reloadRequested = true
        true
    }

    internal fun retire(config: PendingReadEngineConfig, cleanup: CompletableFuture<Unit>) {
        synchronized(lock) {
            if (config.generation != 1 || retirement != null) return
            retirement = if (reloadRequested) cleanup else CompletableFuture<Unit>().also {
                it.completeExceptionally(IllegalStateException("engine retired without matching reload intent"))
            }
            generation = 2
        }
    }

    internal fun awaitReady(
        config: PendingReadEngineConfig,
        candidateRunId: String,
        candidateGeneration: Double
    ): CompletableFuture<Unit> =
        synchronized(lock) {
            if (config.session !== this || candidateRunId != runId ||
                candidateGeneration != config.generation.toDouble()
            ) {
                return failedFuture("pending reload runId/generation does not match this probe's engine")
            }
            when (config.generation) {
                1 -> CompletableFuture.completedFuture(Unit)
                2 -> retirement ?: failedFuture("old engine retirement was not registered")
                else -> failedFuture("unsupported pending reload generation")
            }
        }

    companion object {
        fun isCanonicalRunId(value: String): Boolean =
            runCatching { UUID.fromString(value).toString() == value }.getOrDefault(false)

        private fun failedFuture(detail: String): CompletableFuture<Unit> =
            CompletableFuture<Unit>().also { it.completeExceptionally(IllegalArgumentException(detail)) }
    }
}

/**
 * Strong native ownership until close completes. This is engine lifecycle bookkeeping only;
 * no BLE handles, events or routes are looked up through this collection.
 */
class BleHubProbeLifecycleOwner internal constructor(
    internal var context: ReactApplicationContext?,
    val pendingConfig: PendingReadEngineConfig?,
) {
    private val lock = Any()
    private var closeFromEngine: (() -> Unit)? = null
    private var retirementRequested = false
    internal val completion = CompletableFuture<Unit>()

    internal fun activate(close: () -> Unit) {
        val retiring = synchronized(lock) {
            closeFromEngine = close
            retirementRequested
        }
        if (retiring) close()
    }

    internal fun retire() {
        val close = synchronized(lock) {
            retirementRequested = true
            closeFromEngine
        }
        close?.invoke()
    }

    internal fun complete(failure: Throwable?, releaseContext: Boolean = true) {
        if (releaseContext) BleHubProbeLifecycleOwners.unregister(this)
        if (failure == null) completion.complete(Unit) else completion.completeExceptionally(failure)
    }
}

/** Identity-scoped native owners; snapshots are closed only after releasing the registry lock. */
object BleHubProbeLifecycleOwners {
    private class Engine(val config: PendingReadEngineConfig?) {
        val owners = LinkedHashSet<BleHubProbeLifecycleOwner>()
    }

    private val lock = Any()
    private val engines = IdentityHashMap<ReactApplicationContext, Engine>()
    private val retired = WeakHashMap<ReactApplicationContext, Boolean>()
    private val pendingHosts = IdentityHashMap<ReactHost, PendingReadReloadSession>()

    fun configurePendingHost(host: ReactHost, session: PendingReadReloadSession) {
        synchronized(lock) {
            check(pendingHosts[host] == null || pendingHosts[host] === session) {
                "pending read host already has a different run"
            }
            pendingHosts[host] = session
        }
    }

    internal fun register(
        context: ReactApplicationContext?,
    ): BleHubProbeLifecycleOwner = synchronized(lock) {
        if (context == null) return BleHubProbeLifecycleOwner(null, null)
        check(retired[context] != true) { "cannot create a probe in a retired React engine" }
        val engine = engines.getOrPut(context) {
            // Match the actual context, never a process-global 'current generation'. This also
            // works if the JS bundle constructs the probe before RN's instance-created listeners.
            val session = pendingHosts.entries.singleOrNull {
                it.key.currentReactContext === context
            }?.value
            Engine(session?.capture())
        }
        BleHubProbeLifecycleOwner(context, engine.config).also { engine.owners.add(it) }
    }

    fun retireEngine(context: ReactApplicationContext) {
        val snapshot: List<BleHubProbeLifecycleOwner>
        val config: PendingReadEngineConfig?
        val cleanup = CompletableFuture<Unit>()
        synchronized(lock) {
            if (retired.put(context, true) == true) return
            val engine = engines[context]
            snapshot = engine?.owners?.toList().orEmpty()
            config = engine?.config ?: pendingHosts.entries.singleOrNull {
                it.key.currentReactContext === context
            }?.value?.capture()
            config?.session?.retire(config, cleanup)
        }
        // Never call probe.close while holding the registry lock (close unregisters owners).
        snapshot.forEach { owner ->
            try {
                owner.retire()
            } catch (failure: Throwable) {
                owner.complete(failure)
            }
        }
        val futures = snapshot.map { it.completion }.toTypedArray()
        CompletableFuture.allOf(*futures).whenComplete { _, failure ->
            if (failure != null) cleanup.completeExceptionally(failure)
            else if (config?.generation == 1 && snapshot.isEmpty()) {
                cleanup.completeExceptionally(IllegalStateException("pending old engine had no live native owner"))
            } else cleanup.complete(Unit)
        }
    }

    internal fun unregister(owner: BleHubProbeLifecycleOwner) {
        synchronized(lock) {
            owner.context?.let { context ->
                engines[context]?.let { engine ->
                    engine.owners.remove(owner)
                    if (engine.owners.isEmpty()) engines.remove(context)
                }
            }
            owner.context = null
        }
    }

    /** This owner's actual context membership, not the process-wide number of probes. */
    internal fun membershipCount(owner: BleHubProbeLifecycleOwner): Int = synchronized(lock) {
        owner.context?.let { context ->
            if (engines[context]?.owners?.contains(owner) == true) 1 else 0
        } ?: 0
    }

    @androidx.annotation.VisibleForTesting
    fun configureEngineForTesting(context: ReactApplicationContext, config: PendingReadEngineConfig) {
        synchronized(lock) {
            check(engines[context] == null && retired[context] != true)
            engines[context] = Engine(config)
        }
    }

    @androidx.annotation.VisibleForTesting
    fun ownerCount(context: ReactApplicationContext): Int = synchronized(lock) {
        engines[context]?.owners?.size ?: 0
    }
}
