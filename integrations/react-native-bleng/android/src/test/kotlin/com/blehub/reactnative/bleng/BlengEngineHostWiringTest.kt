package com.blehub.reactnative.bleng

import com.margelo.nitro.blehubreactnativebleng.BlengFailureDomain
import com.margelo.nitro.blehubreactnativebleng.BlengFailureInfo
import com.margelo.nitro.blehubreactnativebleng.BlengCloseResult
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Deferred
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test

/** Host-scoped engine linkage; no Android runtime, RN engine, native image or device access. */
class BlengEngineHostWiringTest {
    private class FakeSession : EngineOwnedSession {
        var permission: Deferred<Unit>? = null
        private val completion = CompletableDeferred<BlengCloseResult>()

        override fun claimEngineBinding(permission: Deferred<Unit>?): Boolean {
            this.permission = permission
            return true
        }

        override fun invalidateForEngine(): Deferred<BlengCloseResult> {
            completion.complete(BlengCloseResult(closed = true, failure = null))
            return completion
        }
    }

    @Test
    fun theFirstEngineHasNoPredecessorGate() {
        val wiring = BlengEngineHostWiring()
        val first = BlengEngineLifetime()

        assertTrue(wiring.attach(first))
        assertSame(first, wiring.latest())
        val session = FakeSession()
        assertTrue(first.bind(session))
        assertNull("A first engine must not wait on anything", session.permission)
    }

    @Test
    fun aReplacementEngineLinksToTheExactPredecessor() {
        val wiring = BlengEngineHostWiring()
        val first = BlengEngineLifetime()
        val second = BlengEngineLifetime()

        assertTrue(wiring.attach(first))
        assertTrue(wiring.attach(second))
        assertSame(second, wiring.latest())

        val session = FakeSession()
        assertTrue(second.bind(session))
        assertTrue("The successor must carry a predecessor gate", session.permission != null)
    }

    @Test
    fun reattachingTheSameEngineIsIdempotent() {
        val wiring = BlengEngineHostWiring()
        val first = BlengEngineLifetime()

        assertTrue(wiring.attach(first))
        assertTrue("Re-entry for the same engine is not a new link", wiring.attach(first))
        assertSame(first, wiring.latest())
    }

    @Test
    fun aRefusedLinkIsReportedSoTheHostCanSealAdmission() {
        val wiring = BlengEngineHostWiring()
        val first = BlengEngineLifetime()
        assertTrue(wiring.attach(first))

        // A successor whose owner already bound cannot accept a predecessor gate afterwards;
        // silently accepting it would start native work without old-engine retirement.
        val successor = BlengEngineLifetime()
        assertTrue(successor.bind(FakeSession()))
        assertFalse(wiring.attach(successor))
        assertSame("A refused link must not advance the host", first, wiring.latest())
    }

    @Test
    fun theSuccessorGateReleasesOnlyAfterThePredecessorRetires() {
        val wiring = BlengEngineHostWiring()
        val first = BlengEngineLifetime()
        val second = BlengEngineLifetime()
        assertTrue(wiring.attach(first))
        assertTrue(first.bind(FakeSession()))
        assertTrue(wiring.attach(second))

        val successorSession = FakeSession()
        assertTrue(second.bind(successorSession))
        val gate = successorSession.permission ?: error("successor must be gated")

        assertFalse("The gate must stay closed while the old engine lives", gate.isCompleted)
        first.invalidate()
        runBlocking { withTimeout(5_000) { gate.await() } }
        assertTrue(gate.isCompleted)
    }

    @Test
    fun theSuccessorGateFailsWhenThePredecessorLeakedResources() {
        val wiring = BlengEngineHostWiring()
        val first = BlengEngineLifetime()
        val second = BlengEngineLifetime()
        val leaky = object : EngineOwnedSession {
            private val completion = CompletableDeferred(
                BlengCloseResult(
                    closed = true,
                    failure = BlengFailureInfo(
                        domain = BlengFailureDomain.LIFECYCLE,
                        code = "native_resources_not_zero",
                        message = "leak",
                        platform = null,
                    ),
                ),
            )

            override fun claimEngineBinding(permission: Deferred<Unit>?): Boolean = true

            override fun invalidateForEngine(): Deferred<BlengCloseResult> = completion
        }
        assertTrue(wiring.attach(first))
        assertTrue(first.bind(leaky))
        assertTrue(wiring.attach(second))
        val successorSession = FakeSession()
        assertTrue(second.bind(successorSession))
        val gate = successorSession.permission ?: error("successor must be gated")

        first.invalidate()
        assertThrows(Throwable::class.java) {
            runBlocking { withTimeout(5_000) { gate.await() } }
        }
    }

    @Test
    fun predecessorTimeoutIsPassedToTheGate() {
        val wiring = BlengEngineHostWiring(predecessorTimeoutMs = 250)
        val first = BlengEngineLifetime()
        val second = BlengEngineLifetime()
        assertTrue(wiring.attach(first))
        assertTrue(wiring.attach(second))
        val session = FakeSession()
        assertTrue(second.bind(session))
        val gate = session.permission ?: error("successor must be gated")

        // The predecessor never retires, so the bounded gate must fail instead of hanging.
        val failure = assertThrows(BlengEngineRetirementException::class.java) {
            runBlocking { withTimeout(5_000) { gate.await() } }
        }
        assertEquals(BlengEngineRetirementFailure.RetirementTimedOut, failure.failure)
    }
}
