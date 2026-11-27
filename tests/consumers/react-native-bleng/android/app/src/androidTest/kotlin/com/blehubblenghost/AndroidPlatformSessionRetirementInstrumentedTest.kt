package com.blehubblenghost

import android.os.HandlerThread
import androidx.test.platform.app.InstrumentationRegistry
import com.blehub.reactnative.bleng.AndroidPlatformSession
import com.blehub.reactnative.bleng.AndroidPlatformSessionFactory
import dev.bleng_supervisor.ForeignOwnerContext
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotSame
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Production-session retirement without BLE initialization or RF commands.
 *
 * Only the native session/backend is constructed: no BLeng, supervisor, Hybrid owner,
 * event reader startup, or JS/Hermes runtime is created. Obtaining the Android service
 * reference in the backend constructor is not adapter initialization.
 */
class AndroidPlatformSessionRetirementInstrumentedTest {
    @Test
    fun transferredBackendRetirementStopsOwnedWorkerAndReportsZeroResources() {
        withSession(1uL) { session, worker ->
            assertTrue("The production session must own a live worker", worker.isAlive)
            retireTransferredBackend(session, worker)
        }
    }

    @Test
    fun independentSessionsRetireWithoutLeavingPreviousWorkersAlive() {
        val firstWorker = withSession(1uL) { session, worker ->
            assertTrue("The first session must own a live worker", worker.isAlive)
            retireTransferredBackend(session, worker)
            worker
        }

        withSession(2uL) { session, worker ->
            assertNotSame("A fresh session must own a different worker", firstWorker, worker)
            assertTrue("The replacement session must own a live worker", worker.isAlive)
            assertFalse("The previous session's worker must remain stopped", firstWorker.isAlive)
            retireTransferredBackend(session, worker)
            assertFalse("Retiring the replacement must not retain the old worker", firstWorker.isAlive)
        }
    }

    @Test
    fun disposeUnassembledStopsOwnedWorkerAndRepeatedDisposalLeavesItStopped() {
        withSession(1uL) { session, worker ->
            assertTrue("The unassembled session must own a live worker", worker.isAlive)
            session.disposeUnassembled()
            assertFalse("Unassembled disposal must join its owned worker", worker.isAlive)
            session.disposeUnassembled()
            assertFalse("Repeated unassembled disposal must leave the worker stopped", worker.isAlive)
        }
    }

    private fun retireTransferredBackend(session: AndroidPlatformSession, worker: HandlerThread) {
        // Model only the backend disposal normally performed by BLeng shutdown after
        // ownership transfer. Constructing BLeng would initialize BLE and is forbidden here.
        session.backend().dispose()
        assertTrue("Backend disposal alone must not stop a caller-owned worker", worker.isAlive)
        val snapshot = runBlocking {
            withTimeout(15_000L) {
                session.waitEventReader()
                session.resourceSnapshot()
            }
        }

        // Inspect before finally cleanup, so the fallback cannot turn a leak into a pass.
        assertFalse("Normal retirement must join this session's owned worker", worker.isAlive)
        val counts = snapshot.associate { it.name to it.count }
        assertEquals("Resource names must be unique", snapshot.size, counts.size)
        val required = AndroidPlatformSessionFactory(
            InstrumentationRegistry.getInstrumentation().targetContext,
            "AndroidPlatformSessionRetirement-required",
        ).requiredZeroResourceNames()
        assertTrue("The retirement gate must require backendThreads", "backendThreads" in required)
        assertEquals("Snapshot must cover the required resource vocabulary", required.toSet(), counts.keys)
        assertEquals("The session must report no owned backend threads", 0uL, counts["backendThreads"])
        for (name in required) {
            assertEquals("Retired resource $name must be zero", 0uL, counts.getValue(name))
        }
    }

    private fun <T> withSession(
        generation: ULong,
        block: (AndroidPlatformSession, HandlerThread) -> T,
    ): T {
        // Resolve the read-only diagnostic before allocating the session. Never enumerate
        // process threads or change private production state to make retirement succeed.
        val workerField = AndroidPlatformSession::class.java.getDeclaredField("backendThread").apply {
            isAccessible = true
        }
        val session = AndroidPlatformSession.create(
            context = InstrumentationRegistry.getInstrumentation().targetContext,
            requested = ForeignOwnerContext(
                "android-retirement-owner",
                "android-retirement-backend",
                generation,
                generation,
            ),
            queueLabel = "AndroidPlatformSessionRetirement",
        )
        var worker: HandlerThread? = null
        var failure: Throwable? = null
        try {
            val ownedWorker = workerField.get(session) as HandlerThread
            worker = ownedWorker
            return block(session, ownedWorker)
        } catch (error: Throwable) {
            failure = error
            throw error
        } finally {
            val primary = failure
            fun cleanup(action: () -> Unit) {
                try {
                    action()
                } catch (error: Throwable) {
                    if (failure == null) failure = error else failure!!.addSuppressed(error)
                }
            }

            // Dispose while the handler is available to release the production backend
            // lease even after a red assertion. Its synchronous wait is production-bounded.
            cleanup { session.backend().dispose() }
            val ownedWorker = worker
            if (ownedWorker == null) {
                cleanup { session.disposeUnassembled() }
            } else {
                cleanup { ownedWorker.quitSafely() }
                cleanup {
                    ownedWorker.join(5_000L)
                    check(!ownedWorker.isAlive) { "Test cleanup could not join the session's owned worker" }
                }
            }
            // Preserve the assertion/retirement failure and attach any cleanup failures.
            if (primary == null) failure?.let { throw it }
        }
    }
}
