package com.blehub.reactnative.bleng

import java.util.WeakHashMap

/**
 * Associates one engine lifetime with the exact object that represents that engine.
 *
 * This is deliberately *not* a "current engine" global: lookup requires the caller to supply the
 * concrete engine key, entries are held weakly, and an unknown key resolves to `null` so callers
 * can fail closed. Android's engine key is the RN `ReactApplicationContext`; tests use plain
 * objects. The class stays generic so this contract is verifiable on the JVM.
 */
class BlengEngineLifetimeRegistry<Key : Any> {
    private val lock = Any()
    private val lifetimes = WeakHashMap<Key, BlengEngineLifetime>()

    /** @return false when this exact key already has a different registered lifetime. */
    fun register(key: Key, lifetime: BlengEngineLifetime): Boolean = synchronized(lock) {
        val existing = lifetimes[key]
        if (existing != null && existing !== lifetime) return false
        lifetimes[key] = lifetime
        return true
    }

    /** Removes the entry only when it still belongs to the same lifetime. */
    fun unregister(key: Key, lifetime: BlengEngineLifetime) = synchronized(lock) {
        if (lifetimes[key] === lifetime) lifetimes.remove(key)
    }

    fun forKey(key: Key?): BlengEngineLifetime? = synchronized(lock) {
        if (key == null) null else lifetimes[key]
    }
}
