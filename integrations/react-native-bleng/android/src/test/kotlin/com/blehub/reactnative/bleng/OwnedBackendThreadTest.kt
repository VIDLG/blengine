package com.blehub.reactnative.bleng

import java.io.Closeable
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicReference
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertSame
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test

/** Real JVM workers; no Android runtime, native image, Bluetooth or device access. */
class OwnedBackendThreadTest {
    @Test
    fun snapshotRunsBeforeQuitAndRepeatedStopDoesNotRepeatTheRequest() {
        Worker().use { worker ->
            val events = mutableListOf<String>()
            val retirement = OwnedBackendThread(worker.thread, {
                events += "quit"
                worker.release.countDown()
            })
            val snapshot = retirement.snapshotAndStop {
                assertTrue(worker.thread.isAlive)
                events += "snapshot"
                mapOf("ingressRoots" to 0)
            }
            assertEquals(mapOf("ingressRoots" to 0), snapshot)
            assertEquals(listOf("snapshot", "quit"), events)
            assertFalse(worker.thread.isAlive)
            retirement.stop()
            assertEquals(listOf("snapshot", "quit"), events)
        }
    }

    @Test
    fun snapshotFailureStillStopsTheOwnedWorkerAndKeepsThePrimary() {
        Worker().use { worker ->
            val primary = IllegalStateException("snapshot failed")
            val retirement = OwnedBackendThread(worker.thread, { worker.release.countDown() })
            val failure = runCatching { retirement.snapshotAndStop<Unit> { throw primary } }.exceptionOrNull()
            assertSame(primary, failure)
            assertFalse(worker.thread.isAlive)
            assertTrue(primary.suppressed.isEmpty())
        }
    }

    @Test
    fun snapshotAndQuitFailureKeepBothErrorsWithoutRetry() {
        Worker().use { worker ->
            val primary = IllegalStateException("snapshot failed")
            val quitFailure = IllegalArgumentException("quit failed")
            val requests = AtomicInteger()
            val retirement = OwnedBackendThread(worker.thread, {
                requests.incrementAndGet()
                throw quitFailure
            })
            val failure = runCatching { retirement.snapshotAndStop<Unit> { throw primary } }.exceptionOrNull()
            assertSame(primary, failure)
            assertEquals(1, primary.suppressed.size)
            assertSame(quitFailure, primary.suppressed.single())
            assertSame(quitFailure, runCatching { retirement.stop() }.exceptionOrNull())
            assertEquals(1, requests.get())
        }
    }

    @Test
    fun quitFailureCannotBecomeASuccessfulSnapshot() {
        Worker().use { worker ->
            val quitFailure = IllegalStateException("quit failed")
            val retirement = OwnedBackendThread(worker.thread, { throw quitFailure })
            assertSame(
                quitFailure,
                runCatching { retirement.snapshotAndStop { emptyMap<String, Int>() } }.exceptionOrNull(),
            )
            assertTrue(worker.thread.isAlive)
        }
    }

    @Test
    fun joinTimeoutRemainsFailedEvenIfTheWorkerLaterExits() {
        Worker().use { worker ->
            val requests = AtomicInteger()
            val retirement = OwnedBackendThread(worker.thread, { requests.incrementAndGet() }, 20L)
            val failure = assertThrows(IllegalStateException::class.java) { retirement.stop() }
            assertEquals("Android backend thread did not stop", failure.message)
            assertTrue(worker.thread.isAlive)
            worker.close()
            assertFalse(worker.thread.isAlive)
            assertSame(failure, runCatching { retirement.stop() }.exceptionOrNull())
            assertEquals(1, requests.get())
        }
    }

    @Test
    fun snapshotFailureKeepsAJoinTimeoutAsIndependentCleanupEvidence() {
        Worker().use { worker ->
            val primary = IllegalStateException("snapshot failed")
            val retirement = OwnedBackendThread(worker.thread, {}, 20L)
            val failure = runCatching { retirement.snapshotAndStop<Unit> { throw primary } }.exceptionOrNull()
            assertSame(primary, failure)
            assertTrue(worker.thread.isAlive)
            assertEquals(1, primary.suppressed.size)
            val stopFailure = primary.suppressed.single()
            assertEquals("Android backend thread did not stop", stopFailure.message)
            assertSame(stopFailure, runCatching { retirement.stop() }.exceptionOrNull())
        }
    }

    @Test
    fun interruptedJoinPreservesTheCallerInterruptAndFailureWithoutRetry() {
        Worker().use { worker ->
            val requests = AtomicInteger()
            val retirement = OwnedBackendThread(worker.thread, { requests.incrementAndGet() })
            val failure = AtomicReference<Throwable?>()
            val interrupted = AtomicBoolean()
            val caller = Thread {
                Thread.currentThread().interrupt()
                failure.set(runCatching { retirement.stop() }.exceptionOrNull())
                interrupted.set(Thread.currentThread().isInterrupted)
            }.also { it.start() }
            caller.join(5_000L)
            assertFalse("Stop caller must finish without joining an unbounded worker", caller.isAlive)
            assertTrue(failure.get() is InterruptedException)
            assertTrue(interrupted.get())
            assertSame(failure.get(), runCatching { retirement.stop() }.exceptionOrNull())
            assertFalse("A cached failure must not interrupt a different caller", Thread.currentThread().isInterrupted)
            assertEquals(1, requests.get())
        }
    }

    @Test
    fun joiningTheCurrentWorkerIsRejectedBeforeRequestingQuit() {
        val requests = AtomicInteger()
        val failure = AtomicReference<Throwable?>()
        lateinit var retirement: OwnedBackendThread
        val thread = Thread {
            failure.set(runCatching { retirement.stop() }.exceptionOrNull())
        }
        retirement = OwnedBackendThread(thread, { requests.incrementAndGet() })
        thread.start()
        thread.join(5_000L)
        assertFalse(thread.isAlive)
        assertTrue(failure.get() is IllegalStateException)
        assertEquals(0, requests.get())
    }

    @Test
    fun aZeroOrNegativeJoinBudgetIsRejectedBeforeAnyWork() {
        for (budget in listOf(0L, -1L)) {
            val requests = AtomicInteger()
            assertThrows(IllegalArgumentException::class.java) {
                OwnedBackendThread(Thread(), { requests.incrementAndGet() }, budget)
            }
            assertEquals(0, requests.get())
        }
    }

    private class Worker : Closeable {
        val release = CountDownLatch(1)
        private val entered = CountDownLatch(1)
        val thread = Thread {
            entered.countDown()
            check(release.await(10, TimeUnit.SECONDS)) { "Test worker release was not signalled" }
        }.also { it.start() }

        init {
            check(entered.await(5, TimeUnit.SECONDS)) { "Test worker did not start" }
        }

        override fun close() {
            release.countDown()
            thread.join(5_000L)
            check(!thread.isAlive) { "Test-owned worker was not retired" }
        }
    }
}
