package com.blehub.reactnative.bleng

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test

/** Exact-key lifetime lookup; no Android runtime, RN engine or device access. */
class BlengEngineLifetimeRegistryTest {
    private val first = Any()
    private val second = Any()

    @Test
    fun lookupRequiresTheExactKeyAndUnknownKeysResolveToNull() {
        val registry = BlengEngineLifetimeRegistry<Any>()
        val lifetime = BlengEngineLifetime()

        assertNull(registry.forKey(null))
        assertNull("An unregistered engine must not resolve to a default", registry.forKey(first))

        assertTrue(registry.register(first, lifetime))
        assertSame(lifetime, registry.forKey(first))
        assertNull("A different engine never shares the lifetime", registry.forKey(second))
    }

    @Test
    fun duplicateRegistrationForTheSameKeyFailsClosed() {
        val registry = BlengEngineLifetimeRegistry<Any>()
        val original = BlengEngineLifetime()

        assertTrue(registry.register(first, original))
        assertFalse(registry.register(first, BlengEngineLifetime()))
        assertSame(original, registry.forKey(first))
    }

    @Test
    fun unregistrationOnlyRemovesAnEntryItStillOwns() {
        val registry = BlengEngineLifetimeRegistry<Any>()
        val lifetime = BlengEngineLifetime()
        val foreign = BlengEngineLifetime()
        assertTrue(registry.register(first, lifetime))

        registry.unregister(first, foreign)
        assertSame("A foreign lifetime must not evict the registered one", lifetime, registry.forKey(first))

        registry.unregister(first, lifetime)
        assertNull(registry.forKey(first))
    }

    @Test
    fun distinctEnginesKeepIndependentLifetimes() {
        val registry = BlengEngineLifetimeRegistry<Any>()
        val one = BlengEngineLifetime()
        val two = BlengEngineLifetime()
        assertTrue(registry.register(first, one))
        assertTrue(registry.register(second, two))

        assertEquals(0, one.boundOwnerCount)
        assertTrue(one.bind(ProbeSession()))
        assertEquals(1, one.boundOwnerCount)
        assertEquals("Engines must not share native membership", 0, two.boundOwnerCount)
    }

    private class ProbeSession : EngineOwnedSession {
        override fun claimEngineBinding(permission: kotlinx.coroutines.Deferred<Unit>?): Boolean = true

        override fun invalidateForEngine(): kotlinx.coroutines.Deferred<
            com.margelo.nitro.blehubreactnativebleng.BlengCloseResult,
            > = kotlinx.coroutines.CompletableDeferred()
    }
}
