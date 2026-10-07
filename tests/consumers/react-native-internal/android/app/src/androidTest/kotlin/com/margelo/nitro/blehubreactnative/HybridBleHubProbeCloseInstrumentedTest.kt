package com.margelo.nitro.blehubreactnative

import com.margelo.nitro.JNIOnLoad
import com.margelo.nitro.core.Promise
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicReference
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

class HybridBleHubProbeCloseInstrumentedTest {
    @Before
    fun loadNitroBindings() {
        JNIOnLoad.initializeNativeNitro()
    }

    @Test
    fun closeBeforeInitializeSettlesOnceAndRejectsLaterDispatch() {
        val probe = HybridBleHubProbe()
        val first = probe.close()
        assertSame(first, probe.close())
        awaitClose(first)
        assertEquals(DispatchErrorCategory.CLOSED, probe.initialize("1")?.category)
        assertSame(first, probe.close())
    }

    @Test
    fun closePromiseRemainsPendingUntilBackendDisposeReturns() {
        val disposeGate = java.util.concurrent.CountDownLatch(1)
        val probe = HybridBleHubProbe.forDisposeWaitTest(disposeGate)
        val completion = CountDownLatch(1)
        val settled = CountDownLatch(1)
        val settleCount = AtomicInteger(0)

        try {
            probe.setCompletionListener { completion.countDown() }
            assertNull(probe.initialize("1"))
            assertTrue("deterministic completion did not arrive", completion.await(5, TimeUnit.SECONDS))

            val close = probe.close()
            close.then {
                settleCount.incrementAndGet()
                settled.countDown()
            }.catch {
                settleCount.incrementAndGet()
                settled.countDown()
            }
            assertSame(close, probe.close())
            assertFalse(
                "close Promise settled before backend dispose returned",
                settled.await(100, TimeUnit.MILLISECONDS),
            )
        } finally {
            disposeGate.countDown()
        }

        assertTrue("close Promise did not settle after backend dispose", settled.await(5, TimeUnit.SECONDS))
        assertEquals("close Promise settled more than once", 1, settleCount.get())
        awaitClose(probe.close())
    }

    private fun awaitClose(promise: Promise<Unit>) {
        val settled = CountDownLatch(1)
        val failure = AtomicReference<Throwable>()
        promise.then { settled.countDown() }.catch {
            failure.set(it)
            settled.countDown()
        }
        assertTrue("close did not settle", settled.await(5, TimeUnit.SECONDS))
        assertNull("close rejected", failure.get())
    }
}
