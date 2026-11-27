package com.blehub.reactnative.bleng

/**
 * Host-scoped predecessor linkage for Android engines.
 *
 * The Apple host attaches the exact predecessor when its delegate creates a module. Android's
 * equivalent hook is the host's own `ReactPackage` instance, which survives engine reloads while
 * `getModule(name, context)` runs once per new engine. This class holds that immutable linkage.
 *
 * It is deliberately *not* a process-global current-engine registry: the instance belongs to one
 * host, it exposes no lookup, and callers must supply the exact successor engine.
 *
 * @param predecessorTimeoutMs bounded observation window handed to the successor's gate.
 */
class BlengEngineHostWiring(
    private val predecessorTimeoutMs: Long = DEFAULT_PREDECESSOR_TIMEOUT_MS,
) {
    companion object {
        /** Matches the Apple delegate's default retirement observation budget. */
        const val DEFAULT_PREDECESSOR_TIMEOUT_MS = 10_000L
    }

    private val lock = Any()
    private var previous: BlengEngineLifetime? = null

    /**
     * Link [successor] to the engine this host created most recently.
     *
     * @return true when the successor is the first engine, is already wired, or was linked to its
     *         predecessor. False means the host must seal the successor's admission: starting it
     *         without a working predecessor gate would bypass old-engine retirement.
     */
    fun attach(successor: BlengEngineLifetime): Boolean = synchronized(lock) {
        val predecessor = previous
        if (predecessor == null) {
            previous = successor
            return true
        }
        if (predecessor === successor) return true
        if (!successor.configurePredecessor(predecessor, predecessorTimeoutMs)) return false
        previous = successor
        true
    }

    /** The engine this host created most recently; observation only, never a start authority. */
    fun latest(): BlengEngineLifetime? = synchronized(lock) { previous }
}
