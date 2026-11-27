package com.blehub.reactnative.bleng

import com.margelo.nitro.blehubreactnativebleng.BlengFailureDomain
import com.margelo.nitro.blehubreactnativebleng.BlengFailureInfo
import com.margelo.nitro.blehubreactnativebleng.BlengCloseResult
import java.util.concurrent.atomic.AtomicInteger
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Deferred
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test

/** Real coroutines; no Android runtime, RN engine, native image, Bluetooth or device access. */
class BlengEngineLifetimeTest {
    private class FakeSession : EngineOwnedSession {
        val bindCalls = AtomicInteger()
        val invalidateCalls = AtomicInteger()
        var permission: Deferred<Unit>? = null
        var refuseBinding = false
        var receipt = BlengCloseResult(closed = true, failure = null)
        private val completion = CompletableDeferred<BlengCloseResult>()

        override fun claimEngineBinding(permission: Deferred<Unit>?): Boolean {
            bindCalls.incrementAndGet()
            if (refuseBinding) return false
            this.permission = permission
            return true
        }

        override fun invalidateForEngine(): Deferred<BlengCloseResult> {
            invalidateCalls.incrementAndGet()
            completion.complete(receipt)
            return completion
        }
    }

    @Test
    fun invalidationRetiresEveryBoundOwnerOnceInBindingOrder() {
        val lifetime = BlengEngineLifetime()
        val first = FakeSession()
        val second = FakeSession()
        second.receipt = BlengCloseResult(
            closed = true,
            failure = BlengFailureInfo(
                domain = BlengFailureDomain.LIFECYCLE,
                code = "native_resources_not_zero",
                message = "leak",
                platform = null,
            ),
        )
        assertTrue(lifetime.bind(first))
        assertTrue(lifetime.bind(second))
        assertEquals(2, lifetime.boundOwnerCount)

        lifetime.invalidate()
        val results = runBlocking { lifetime.waitForRetirement(5_000) }

        assertEquals(2, results.size)
        assertNull(results[0].failure)
        assertEquals("native_resources_not_zero", results[1].failure?.code)
        assertEquals(1, first.invalidateCalls.get())
        assertEquals(1, second.invalidateCalls.get())
        assertEquals(0, lifetime.boundOwnerCount)
        assertTrue(lifetime.isInvalidated)
    }

    @Test
    fun repeatedInvalidationSharesOneRetirementAndSealsAdmission() {
        val lifetime = BlengEngineLifetime()
        val session = FakeSession()
        assertTrue(lifetime.bind(session))

        lifetime.invalidate()
        lifetime.invalidate()
        val outcome = lifetime.retirementOutcome()
        assertTrue(outcome != null)
        runBlocking { outcome!!.await() }

        assertEquals(1, session.invalidateCalls.get())
        assertFalse("Admission must stay sealed after invalidation", lifetime.bind(FakeSession()))
    }

    @Test
    fun rejectedAdmissionOrRefusedBindingCannotStart() {
        val rejected = BlengEngineLifetime()
        rejected.rejectHostAdmission()
        assertFalse(rejected.bind(FakeSession()))

        val lifetime = BlengEngineLifetime()
        val refusing = FakeSession().apply { refuseBinding = true }
        assertFalse(lifetime.bind(refusing))
        assertEquals(0, lifetime.boundOwnerCount)
        assertEquals(1, refusing.bindCalls.get())
    }

    @Test
    fun bindingCapacityIsBounded() {
        val lifetime = BlengEngineLifetime()
        repeat(BlengEngineLifetime.MAXIMUM_OWNERS) {
            assertTrue("owner $it must bind", lifetime.bind(FakeSession()))
        }
        assertFalse(lifetime.bind(FakeSession()))
        assertEquals(BlengEngineLifetime.MAXIMUM_OWNERS, lifetime.boundOwnerCount)
    }

    @Test
    fun predecessorLinkageRejectsSelfDuplicateAndLateConfiguration() {
        val predecessor = BlengEngineLifetime()
        val successor = BlengEngineLifetime()
        assertFalse("self linkage is invalid", successor.configurePredecessor(successor, 100))
        assertFalse("deadline must be bounded", successor.configurePredecessor(predecessor, 0))
        assertFalse(
            "deadline must be bounded above",
            successor.configurePredecessor(predecessor, BlengEngineLifetime.MAX_TIMEOUT_MS + 1),
        )

        assertTrue(successor.configurePredecessor(predecessor, 5_000))
        assertFalse("predecessor supports one successor", BlengEngineLifetime().configurePredecessor(predecessor, 5_000))
        assertFalse("successor cannot be relinked", successor.configurePredecessor(BlengEngineLifetime(), 5_000))

        val bound = BlengEngineLifetime()
        assertTrue(bound.bind(FakeSession()))
        assertFalse(
            "linking after an owner bound is refused",
            bound.configurePredecessor(BlengEngineLifetime(), 5_000),
        )
    }

    @Test
    fun predecessorRetirementSucceedsOnlyWhenItClosedCleanly() {
        val predecessor = BlengEngineLifetime()
        val successor = BlengEngineLifetime()
        assertTrue(predecessor.bind(FakeSession()))
        assertTrue(successor.configurePredecessor(predecessor, 5_000))

        predecessor.invalidate()
        // The successor's gate awaits exactly this call on the predecessor.
        runBlocking { predecessor.requireSuccessfulRetirement(5_000) }
    }

    @Test
    fun predecessorRetirementFailsWhenAnyOwnerLeakedResources() {
        val predecessor = BlengEngineLifetime()
        val successor = BlengEngineLifetime()
        val leaky = FakeSession().apply {
            receipt = BlengCloseResult(
                closed = true,
                failure = BlengFailureInfo(
                    domain = BlengFailureDomain.LIFECYCLE,
                    code = "native_resources_not_zero",
                    message = "leak",
                    platform = null,
                ),
            )
        }
        assertTrue(predecessor.bind(leaky))
        assertTrue(successor.configurePredecessor(predecessor, 5_000))
        predecessor.invalidate()

        val failure = assertThrows(BlengEngineRetirementException::class.java) {
            runBlocking { predecessor.requireSuccessfulRetirement(5_000) }
        }
        assertEquals(BlengEngineRetirementFailure.PredecessorFailed, failure.failure)
    }

    @Test
    fun retirementThatIsNeverObservedTimesOut() {
        val predecessor = BlengEngineLifetime()
        val successor = BlengEngineLifetime()
        assertTrue(successor.configurePredecessor(predecessor, 50))

        val failure = assertThrows(BlengEngineRetirementException::class.java) {
            runBlocking { predecessor.requireSuccessfulRetirement(200) }
        }
        assertEquals(BlengEngineRetirementFailure.RetirementTimedOut, failure.failure)
    }

    @Test
    fun invalidDeadlineIsRejectedBeforeWaiting() {
        val lifetime = BlengEngineLifetime()
        for (timeout in listOf(0L, -1L, BlengEngineLifetime.MAX_TIMEOUT_MS + 1)) {
            val failure = assertThrows(BlengEngineRetirementException::class.java) {
                runBlocking { lifetime.waitForRetirement(timeout) }
            }
            assertEquals(BlengEngineRetirementFailure.InvalidDeadline, failure.failure)
        }
    }

    @Test
    fun anEngineCancelledMidGateCannotReportACleanRetirement() {
        val root = BlengEngineLifetime()
        val middle = BlengEngineLifetime()
        val last = BlengEngineLifetime()
        assertTrue(middle.configurePredecessor(root, 5_000))
        assertTrue(last.configurePredecessor(middle, 5_000))

        // The middle engine is retired while its own predecessor gate is still pending, so its
        // recorded startupFailed must block the final engine even though the root later clears.
        middle.invalidate()
        root.invalidate()

        val failure = assertThrows(BlengEngineRetirementException::class.java) {
            runBlocking { middle.requireSuccessfulRetirement(5_000) }
        }
        assertEquals(BlengEngineRetirementFailure.PredecessorFailed, failure.failure)
    }
}
