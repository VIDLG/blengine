package com.margelo.nitro.blehubreactnative

import androidx.test.platform.app.InstrumentationRegistry
import com.facebook.react.bridge.ReactApplicationContext
import com.margelo.nitro.JNIOnLoad
import com.margelo.nitro.core.Promise
import com.facebook.react.bridge.BridgeReactContext
import java.util.UUID
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

class BleHubProbeLifecycleOwnerInstrumentedTest {
    @Before
    fun loadNitro() {
        JNIOnLoad.initializeNativeNitro()
    }

    @Test
    fun retirementIsContextScopedAndBarrierWaitsForEveryOldOwner() {
        val session = PendingReadReloadSession(UUID.randomUUID().toString())
        val oldContext = context()
        BleHubProbeLifecycleOwners.configureEngineForTesting(oldContext, session.capture())
        val firstGate = CountDownLatch(1)
        val secondGate = CountDownLatch(1)
        val first = acceptedProbe(oldContext, firstGate)
        val second = acceptedProbe(oldContext, secondGate)
        val unrelatedContext = context()
        val unrelated = HybridBleHubProbe.forEngineDisposeTest(CountDownLatch(0), unrelatedContext)
        var fresh: HybridBleHubProbe? = null
        try {
            assertEquals(2, BleHubProbeLifecycleOwners.ownerCount(oldContext))
            await(first.awaitPendingReloadReady(session.runId, 1.0)).assertSucceeded()
            assertFalse(session.requestReload(UUID.randomUUID().toString()))
            assertTrue(session.requestReload(session.runId))
            assertFalse(session.requestReload(session.runId))
            BleHubProbeLifecycleOwners.retireEngine(oldContext)
            // Hook returns while the old native dispose gates remain blocked.
            assertEquals(DispatchErrorCategory.CLOSED, first.readRfRssi("9", "12004")?.category)
            assertEquals(1, BleHubProbeLifecycleOwners.ownerCount(unrelatedContext))
            assertEquals(DispatchErrorCategory.INVALID_STATE, unrelated.readRfRssi("9", "12004")?.category)
            val newContext = context()
            BleHubProbeLifecycleOwners.configureEngineForTesting(newContext, session.capture())
            fresh = HybridBleHubProbe.forEngineDisposeTest(CountDownLatch(0), newContext)
            val barrier = Settlement(fresh.awaitPendingReloadReady(session.runId, 2.0))
            assertFalse(barrier.done.await(100, TimeUnit.MILLISECONDS))
            firstGate.countDown()
            await(first.close()).assertSucceeded()
            assertSame(first.close(), first.close())
            assertFalse("barrier skipped the second owner", barrier.done.await(100, TimeUnit.MILLISECONDS))
            secondGate.countDown()
            barrier.await().assertSucceeded()
            BleHubProbeLifecycleOwners.retireEngine(oldContext) // duplicate hook is idempotent
            assertEquals(0, BleHubProbeLifecycleOwners.ownerCount(oldContext))
            assertEquals(1, BleHubProbeLifecycleOwners.ownerCount(newContext))
            await(fresh.awaitPendingReloadReady(session.runId, 1.0)).assertFailed()
            await(fresh.awaitPendingReloadReady(UUID.randomUUID().toString(), 2.0)).assertFailed()
            await(fresh.awaitPendingReloadReady(session.runId, 2.5)).assertFailed()
        } finally {
            firstGate.countDown()
            secondGate.countDown()
            await(first.close())
            await(second.close())
            await(unrelated.close()).assertSucceeded()
            fresh?.let { await(it.close()).assertSucceeded() }
        }
    }

    @Test
    fun savedOldHandoffReceivesExactSyntheticRouteAndRejectsAfterClose() {
        val session = PendingReadReloadSession(UUID.randomUUID().toString())
        val oldContext = context()
        BleHubProbeLifecycleOwners.configureEngineForTesting(oldContext, session.capture())
        val captured = AtomicReference<List<String>>()
        val probe = acceptedProbe(oldContext, CountDownLatch(0)) { route, result ->
            captured.set(route)
            assertEquals(AndroidEventHandoff.SubmitResult.CLOSED, result)
            result
        }
        assertTrue(session.requestReload(session.runId))
        BleHubProbeLifecycleOwners.retireEngine(oldContext)
        await(probe.close()).assertSucceeded()
        val read = captured.get()
        assertNotNull(read)
        assertEquals("12008", read[0])
        assertEquals("12004", read[1])
        assertEquals("12006", read[2])
        assertEquals("18446744073709551615", read[3])
    }

    @Test
    fun oldHandoffVerificationFailureRejectsNewEngineBarrier() {
        val session = PendingReadReloadSession(UUID.randomUUID().toString())
        val oldContext = context()
        BleHubProbeLifecycleOwners.configureEngineForTesting(oldContext, session.capture())
        val probe = acceptedProbe(oldContext, CountDownLatch(0)) { _, _ -> AndroidEventHandoff.SubmitResult.ACCEPTED }
        assertTrue(session.requestReload(session.runId))
        BleHubProbeLifecycleOwners.retireEngine(oldContext)
        val newContext = context()
        BleHubProbeLifecycleOwners.configureEngineForTesting(newContext, session.capture())
        val fresh = HybridBleHubProbe.forEngineDisposeTest(CountDownLatch(0), newContext)
        try {
            await(probe.close()).assertFailed()
            await(fresh.awaitPendingReloadReady(session.runId, 2.0)).assertFailed()
            assertEquals(0, BleHubProbeLifecycleOwners.ownerCount(oldContext))
        } finally {
            await(fresh.close()).assertSucceeded()
        }
    }

    @Test
    fun effectCloseCannotBeRelabeledAsBeforeDestroyClose() {
        val session = PendingReadReloadSession(UUID.randomUUID().toString())
        val oldContext = context()
        BleHubProbeLifecycleOwners.configureEngineForTesting(oldContext, session.capture())
        val gate = CountDownLatch(1)
        val probe = acceptedProbe(oldContext, gate)
        val effectClose = probe.close()
        assertTrue(session.requestReload(session.runId))
        BleHubProbeLifecycleOwners.retireEngine(oldContext)
        val newContext = context()
        BleHubProbeLifecycleOwners.configureEngineForTesting(newContext, session.capture())
        val fresh = HybridBleHubProbe.forEngineDisposeTest(CountDownLatch(0), newContext)
        try {
            assertSame(effectClose, probe.close())
            gate.countDown()
            await(effectClose).assertFailed()
            await(fresh.awaitPendingReloadReady(session.runId, 2.0)).assertFailed()
        } finally {
            gate.countDown()
            await(fresh.close()).assertSucceeded()
        }
    }

    @Test
    fun noAcceptedReadOrUnconfiguredEngineCannotPassPendingBarrier() {
        val session = PendingReadReloadSession(UUID.randomUUID().toString())
        val oldContext = context()
        BleHubProbeLifecycleOwners.configureEngineForTesting(oldContext, session.capture())
        val probe = HybridBleHubProbe.forEngineDisposeTest(CountDownLatch(0), oldContext)
        assertEquals(
            DispatchErrorCategory.INVALID_STATE,
            probe.readRfCharacteristic("12008", "12004", "12006", "1")?.category
        )
        assertTrue(session.requestReload(session.runId))
        BleHubProbeLifecycleOwners.retireEngine(oldContext)
        await(probe.close()).assertFailed()
        val unconfigured = HybridBleHubProbe.forDisposeWaitTest(CountDownLatch(0))
        try {
            await(unconfigured.awaitPendingReloadReady(session.runId, 1.0)).assertFailed()
        } finally {
            await(unconfigured.close()).assertSucceeded()
        }
    }

    @Test
    fun backendDisposeFailureRejectsCloseAndNewEngineBarrier() {
        val session = PendingReadReloadSession(UUID.randomUUID().toString())
        val oldContext = context()
        BleHubProbeLifecycleOwners.configureEngineForTesting(oldContext, session.capture())
        val gate = CountDownLatch(1)
        val probe = acceptedProbe(oldContext, gate)
        assertTrue(session.requestReload(session.runId))
        BleHubProbeLifecycleOwners.retireEngine(oldContext)
        val newContext = context()
        BleHubProbeLifecycleOwners.configureEngineForTesting(newContext, session.capture())
        val fresh = HybridBleHubProbe.forEngineDisposeTest(CountDownLatch(0), newContext)
        try {
            // Intentionally let the deterministic backend dispose deadline expire.
            await(probe.close()).assertFailed()
            await(fresh.awaitPendingReloadReady(session.runId, 2.0)).assertFailed()
            assertEquals(0, BleHubProbeLifecycleOwners.ownerCount(oldContext))
        } finally {
            gate.countDown()
            await(fresh.close()).assertSucceeded()
        }
    }

    @Test
    fun threadJoinTimeoutFailsBarrierAndRetainsOwnerUntilCallbackActuallyStops() {
        val session = PendingReadReloadSession(UUID.randomUUID().toString())
        val oldContext = context()
        BleHubProbeLifecycleOwners.configureEngineForTesting(oldContext, session.capture())
        val entered = CountDownLatch(1)
        val release = CountDownLatch(1)
        val callbackReturned = CountDownLatch(1)
        val probe = HybridBleHubProbe.forEngineDisposeTest(CountDownLatch(0), oldContext)
        var fresh: HybridBleHubProbe? = null
        try {
            probe.setCompletionListener {
                entered.countDown()
                try { release.await() } finally { callbackReturned.countDown() }
            }
            assertNull(probe.initialize("12001"))
            assertTrue(entered.await(5, TimeUnit.SECONDS))
            assertNull(probe.readRfCharacteristic("12008", "12004", "12006", "1"))
            assertTrue(session.requestReload(session.runId))
            BleHubProbeLifecycleOwners.retireEngine(oldContext)
            val newContext = context()
            BleHubProbeLifecycleOwners.configureEngineForTesting(newContext, session.capture())
            fresh = HybridBleHubProbe.forEngineDisposeTest(CountDownLatch(0), newContext)
            await(probe.close()).assertFailed()
            await(fresh.awaitPendingReloadReady(session.runId, 2.0)).assertFailed()
            assertEquals("live old callback lost its native context owner", 1,
                BleHubProbeLifecycleOwners.ownerCount(oldContext))
        } finally {
            release.countDown()
            assertTrue(callbackReturned.await(5, TimeUnit.SECONDS))
            fresh?.let { await(it.close()).assertSucceeded() }
            await(probe.close())
        }
        val deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(5)
        while (BleHubProbeLifecycleOwners.ownerCount(oldContext) != 0 && System.nanoTime() < deadline) {
            Thread.sleep(10)
        }
        assertEquals(0, BleHubProbeLifecycleOwners.ownerCount(oldContext))
    }

    private fun acceptedProbe(
        context: ReactApplicationContext,
        gate: CountDownLatch,
        lateSubmit: ((List<String>, AndroidEventHandoff.SubmitResult) -> AndroidEventHandoff.SubmitResult)? = null,
    ): HybridBleHubProbe {
        val probe = HybridBleHubProbe.forEngineDisposeTest(gate, context, lateSubmit)
        assertNull(probe.initialize("12001"))
        assertNull(probe.readRfCharacteristic("12008", "12004", "12006", "18446744073709551615"))
        return probe
    }

    @Suppress("DEPRECATION")
    private fun context(): ReactApplicationContext =
        BridgeReactContext(InstrumentationRegistry.getInstrumentation().targetContext)

    private fun await(promise: Promise<Unit>): Settlement = Settlement(promise).await()

    private class Settlement(promise: Promise<Unit>) {
        val done = CountDownLatch(1)
        private val failure = AtomicReference<Throwable>()

        init {
            promise.then { done.countDown() }.catch { failure.set(it); done.countDown() }
        }

        fun await(): Settlement {
            assertTrue("native Promise did not settle", done.await(10, TimeUnit.SECONDS))
            return this
        }

        fun assertSucceeded() = assertNull("native cleanup rejected", failure.get())
        fun assertFailed() = assertNotNull("native failure resolved as success", failure.get())
    }
}
