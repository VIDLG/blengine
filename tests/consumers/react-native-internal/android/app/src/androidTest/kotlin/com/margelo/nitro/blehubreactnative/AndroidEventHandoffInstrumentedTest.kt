package com.margelo.nitro.blehubreactnative

import android.os.Handler
import android.os.HandlerThread
import java.util.Collections
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class AndroidEventHandoffInstrumentedTest {
    @Test
    fun closeDiscardsAcceptedEventBeforeDeliveryAndRejectsLateSubmission() {
        val thread = HandlerThread("BleHubTest-handoff-close").also { it.start() }
        val handler = Handler(thread.looper)
        val blockerEntered = CountDownLatch(1)
        val releaseBlocker = CountDownLatch(1)
        val delivered = Collections.synchronizedList(mutableListOf<Int>())
        try {
            assertTrue(handler.post {
                blockerEntered.countDown()
                releaseBlocker.await(5, TimeUnit.SECONDS)
            })
            assertTrue(blockerEntered.await(5, TimeUnit.SECONDS))

            val handoff = AndroidEventHandoff<Int>(handler, capacity = 1, delivered::add) {}
            assertEquals(AndroidEventHandoff.SubmitResult.ACCEPTED, handoff.submit(1))
            handoff.close()
            handoff.close()
            assertEquals(AndroidEventHandoff.SubmitResult.CLOSED, handoff.submit(2))

            releaseBlocker.countDown()
            assertTrue(handler.awaitIdle())
            assertTrue(delivered.isEmpty())
            assertEquals(AndroidEventHandoff.SubmitResult.CLOSED, handoff.submit(3))
        } finally {
            releaseBlocker.countDown()
            thread.quitSafely()
            thread.join(5_000)
        }
    }

    @Test
    fun capacityOverflowRejectsPermanentlyWithoutDroppingAcceptedEvent() {
        val thread = HandlerThread("BleHubTest-handoff-capacity").also { it.start() }
        val handler = Handler(thread.looper)
        val blockerEntered = CountDownLatch(1)
        val releaseBlocker = CountDownLatch(1)
        val delivered = Collections.synchronizedList(mutableListOf<Int>())
        val failures = CountDownLatch(1)
        try {
            assertTrue(handler.post {
                blockerEntered.countDown()
                releaseBlocker.await(5, TimeUnit.SECONDS)
            })
            assertTrue(blockerEntered.await(5, TimeUnit.SECONDS))

            val handoff = AndroidEventHandoff(
                handler = handler,
                capacity = 1,
                deliver = delivered::add,
                onFailure = { failures.countDown() },
            )
            assertEquals(AndroidEventHandoff.SubmitResult.ACCEPTED, handoff.submit(1))
            assertEquals(AndroidEventHandoff.SubmitResult.OVERFLOW, handoff.submit(2))
            assertTrue(failures.await(5, TimeUnit.SECONDS))

            releaseBlocker.countDown()
            assertTrue(handler.awaitIdle())
            assertEquals(listOf(1), delivered)
            assertEquals(AndroidEventHandoff.SubmitResult.CLOSED, handoff.submit(3))
        } finally {
            releaseBlocker.countDown()
            thread.quitSafely()
            thread.join(5_000)
        }
    }

    private fun Handler.awaitIdle(): Boolean {
        val idle = CountDownLatch(1)
        if (!post { idle.countDown() }) return false
        return idle.await(5, TimeUnit.SECONDS)
    }
}
