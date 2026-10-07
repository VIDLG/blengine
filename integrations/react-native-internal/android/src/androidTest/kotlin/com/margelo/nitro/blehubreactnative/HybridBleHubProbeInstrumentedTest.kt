package com.margelo.nitro.blehubreactnative

import android.os.Handler
import android.os.Looper
import com.margelo.nitro.JNIOnLoad
import com.margelo.nitro.core.ArrayBuffer
import com.margelo.nitro.core.Promise
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * Runs the production UniFFI AAR and Nitro-generated Kotlin surface on Android.
 * This deliberately uses the deterministic internal test backend, not Android BLE/RF.
 */
class HybridBleHubProbeInstrumentedTest {
    @Before
    fun loadNitroBindings() {
        // Standalone instrumentation has no RN runtime to load Nitro before Promise/ArrayBuffer.
        JNIOnLoad.initializeNativeNitro()
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

    @Test
    fun productionHubCompletesMaximumU64AndClosesExplicitly() {
        val probe = HybridBleHubProbe()
        val completion = AtomicReference<CompletionEvent>()
        val firstEvent = CountDownLatch(1)

        try {
            probe.setCompletionListener {
                completion.set(it)
                firstEvent.countDown()
            }

            assertNull(probe.initialize("18446744073709551615"))
            assertTrue(firstEvent.await(5, TimeUnit.SECONDS))
            assertEquals("18446744073709551615", completion.get().commandId)
            assertNull(completion.get().error)
        } finally {
            awaitClose(probe.close())
        }
    }

    @Test
    fun rfScanFailsClosedWithoutEmittingSyntheticEvents() {
        val probe = HybridBleHubProbe()
        val rfEvent = CountDownLatch(1)

        try {
            probe.setRfScanListener { rfEvent.countDown() }
            assertEquals(
                DispatchErrorCategory.BACKEND_UNAVAILABLE,
                probe.startRfScan("1", "2", "0000fff0-0000-1000-8000-00805f9b34fb")?.category,
            )
            assertNull(probe.initialize("3"))
            assertEquals(
                DispatchErrorCategory.BACKEND_UNAVAILABLE,
                probe.stopRfScan("4", "2")?.category,
            )
            assertFalse(rfEvent.await(100, TimeUnit.MILLISECONDS))
            probe.removeRfScanListener()
        } finally {
            awaitClose(probe.close())
        }

        assertEquals(
            DispatchErrorCategory.CLOSED,
            probe.startRfScan("5", "2", "0000fff0-0000-1000-8000-00805f9b34fb")?.category,
        )
        assertEquals(DispatchErrorCategory.CLOSED, probe.stopRfScan("6", "2")?.category)
    }

    @Test
    fun listenerRemovalBytesErrorsAndClosedDispatchAreStable() {
        val probe = HybridBleHubProbe()
        val eventCount = CountDownLatch(1)
        val removedEvent = CountDownLatch(1)
        val error = EventError(EventErrorCategory.GATT, "synthetic transport error", null)
        val input = ArrayBuffer.allocate(4)
        input.getBuffer(false).put(byteArrayOf(0x01, 0x02, 0x7f, 0x00))

        try {
            probe.setCompletionListener { eventCount.countDown() }
            assertNull(probe.initialize("1"))
            assertTrue(eventCount.await(5, TimeUnit.SECONDS))

            val roundTrip = probe.roundTrip(TransportSample("bytes", input, error))
            assertEquals("bytes", roundTrip.id)
            assertArrayEquals(byteArrayOf(0x01, 0x02, 0x7f, 0x00), roundTrip.bytes.toByteArray())
            assertEquals(error, roundTrip.error)

            probe.setCompletionListener { removedEvent.countDown() }
            probe.removeCompletionListener()
            assertNull(probe.initialize("2"))
            // The backend posts completion on main; drain that queue before checking silence.
            val drained = CountDownLatch(1)
            assertTrue(Handler(Looper.getMainLooper()).post { drained.countDown() })
            assertTrue(drained.await(5, TimeUnit.SECONDS))
            assertFalse(removedEvent.await(100, TimeUnit.MILLISECONDS))
        } finally {
            awaitClose(probe.close())
        }

        val closedError = probe.initialize("3")
        assertNotNull(closedError)
        assertEquals(DispatchErrorCategory.CLOSED, closedError?.category)
    }

    @Test
    fun closeWithoutInitializeSettlesAndRejectsLaterDispatch() {
        val probe = HybridBleHubProbe()
        val first = probe.close()
        assertSame(first, probe.close())
        awaitClose(first)
        assertEquals(DispatchErrorCategory.CLOSED, probe.initialize("1")?.category)
    }

    @Test
    fun duplicateCloseSharesCompletionAfterInitializedHubCleanup() {
        val probe = HybridBleHubProbe()
        val completion = CountDownLatch(1)
        probe.setCompletionListener { completion.countDown() }
        try {
            assertNull(probe.initialize("1"))
            assertTrue(completion.await(5, TimeUnit.SECONDS))
        } finally {
            val first = probe.close()
            assertSame(first, probe.close())
            awaitClose(first)
            assertSame(first, probe.close())
        }
    }
}
