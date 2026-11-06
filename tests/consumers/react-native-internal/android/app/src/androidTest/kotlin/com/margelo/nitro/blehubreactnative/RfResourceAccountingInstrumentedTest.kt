package com.margelo.nitro.blehubreactnative

import android.os.Handler
import android.os.HandlerThread
import androidx.test.platform.app.InstrumentationRegistry
import com.facebook.react.bridge.BridgeReactContext
import com.facebook.react.bridge.ReactApplicationContext
import com.facebook.soloader.nativeloader.NativeLoader
import com.facebook.soloader.nativeloader.SystemDelegate
import com.margelo.nitro.JNIOnLoad
import com.margelo.nitro.core.Promise
import dev.blehub.BleHubException
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/** Native ownership tests only; not RF evidence. */
class RfResourceAccountingInstrumentedTest {
    @Before
    fun loadNitroBindings() {
        // Standalone library instrumentation has no RN Application bootstrap for fbjni.
        NativeLoader.initIfUninitialized(SystemDelegate())
        JNIOnLoad.initializeNativeNitro()
    }

    @Test
    fun tenFakeCyclesReturnOwnerHandoffAndWorkersToExactZeroWithoutRecreatingHub() {
        val context = testContext()
        val originalOwners = BleHubProbeLifecycleOwners.ownerCount(context)
        repeat(10) {
            val probe = HybridBleHubProbe.forResourceAccountingTest(context)
            try {
                val untouched = await(probe.snapshotRfResources())
                assertEquals(1.0, untouched.lifecycleOwners, 0.0)
                assertEquals(0.0, untouched.hubRoots, 0.0)
                assertEquals(0.0, untouched.workerThreads, 0.0)
                assertEquals(originalOwners + 1, BleHubProbeLifecycleOwners.ownerCount(context))

                val completed = CountDownLatch(1)
                probe.setCompletionListener { completed.countDown() }
                probe.setRfScanListener { }
                assertNull(probe.initialize("1"))
                assertTrue(completed.await(5, TimeUnit.SECONDS))
                val warm = await(probe.snapshotRfResources())
                assertEquals(1.0, warm.ingressRoots, 0.0)
                assertEquals(1.0, warm.hubRoots, 0.0)
                assertEquals(1.0, warm.handoffRoots, 0.0)
                assertEquals(2.0, warm.listeners, 0.0)
                assertEquals(2.0, warm.workerThreads, 0.0)
                assertEquals(1.0, warm.lifecycleOwners, 0.0)
                assertEquals(0.0, warm.backendLeases, 0.0) // The fake never acquires Android's lease.
                assertTransientZero(warm)
            } finally {
                await(probe.close())
            }
            val closed = await(probe.snapshotRfResources())
            assertAllZero(closed)
            assertEquals(closed, await(probe.snapshotRfResources()))
            assertEquals(DispatchErrorCategory.CLOSED, probe.initialize("2")?.category)
            assertAllZero(await(probe.snapshotRfResources()))
            assertEquals(originalOwners, BleHubProbeLifecycleOwners.ownerCount(context))
        }
    }

    @Test
    fun unfinishedAndFailedCloseCannotPublishAnAllZeroSnapshot() {
        val context = testContext()
        val gate = CountDownLatch(1)
        val pending = HybridBleHubProbe.forEngineDisposeTest(gate, context)
        try {
            assertNull(pending.initialize("1"))
            assertEquals(1.0, await(pending.snapshotRfResources()).ingressRoots, 0.0)
            val closing = pending.close()
            assertNotNull(awaitFailure(pending.snapshotRfResources()))
            gate.countDown()
            await(closing)
            assertAllZero(await(pending.snapshotRfResources()))
        } finally {
            gate.countDown()
            await(pending.close())
        }

        val failed = HybridBleHubProbe.forResourceAccountingTest(
            context, BleHubException.BackendCleanup("deliberate fake dispose failure", null),
        )
        assertNull(failed.initialize("1"))
        assertEquals(1.0, await(failed.snapshotRfResources()).hubRoots, 0.0)
        assertNotNull(awaitFailure(failed.close()))
        assertNotNull(awaitFailure(failed.snapshotRfResources()))
        assertNotNull(awaitFailure(failed.snapshotRfResources()))
        assertEquals(DispatchErrorCategory.CLOSED, failed.initialize("2")?.category)
        assertEquals(0, BleHubProbeLifecycleOwners.ownerCount(context))
    }

    @Test
    fun tenProductionProbeCyclesObserveWarmNativeRootsThenCacheAllSeventeenZeros() {
        val context = testContext()
        val originalOwners = BleHubProbeLifecycleOwners.ownerCount(context)
        repeat(10) {
            val probe = HybridBleHubProbe.forAndroidResourceAccountingTest(context)
            try {
                val untouched = await(probe.snapshotRfResources())
                assertEquals(0.0, untouched.hubRoots, 0.0)
                assertEquals(0.0, untouched.backendLeases, 0.0)
                val completed = CountDownLatch(1)
                probe.setCompletionListener { completed.countDown() }
                assertNull(probe.initialize("1"))
                assertTrue("production initialization did not complete", completed.await(5, TimeUnit.SECONDS))
                val warm = await(probe.snapshotRfResources())
                assertTransientZero(warm)
                assertEquals(1.0, warm.ingressRoots, 0.0)
                assertEquals(1.0, warm.adapterDelegates, 0.0)
                assertEquals(1.0, warm.backendLeases, 0.0)
                assertEquals(1.0, warm.hubRoots, 0.0)
                assertEquals(1.0, warm.handoffRoots, 0.0)
                assertEquals(1.0, warm.listeners, 0.0)
                assertEquals(2.0, warm.workerThreads, 0.0)
                assertEquals(1.0, warm.lifecycleOwners, 0.0)
                assertEquals(originalOwners + 1, BleHubProbeLifecycleOwners.ownerCount(context))
            } finally {
                await(probe.close())
            }
            val closed = await(probe.snapshotRfResources())
            assertAllZero(closed)
            assertEquals(closed, await(probe.snapshotRfResources()))
            assertEquals(DispatchErrorCategory.CLOSED, probe.initialize("2")?.category)
            assertAllZero(await(probe.snapshotRfResources()))
            assertEquals(originalOwners, BleHubProbeLifecycleOwners.ownerCount(context))
        } // The next production construction also exercises real lease reacquisition.
    }

    @Test
    fun snapshotRequestedInsideExecutingListenerWaitsForNativeDeliveryReturn() {
        val backendRead = CountDownLatch(1)
        val requested = CountDownLatch(1)
        val releaseDelivery = CountDownLatch(1)
        val settled = CountDownLatch(1)
        val snapshot = AtomicReference<Promise<RfResourceSnapshot>>()
        val probe = HybridBleHubProbe.forAndroidResourceAccountingTest(testContext()) {
            backendRead.countDown()
        }
        try {
            probe.setCompletionListener {
                // Re-enter exactly while the handoff still accounts for this executing job.
                snapshot.set(probe.snapshotRfResources().then { settled.countDown() }.catch { settled.countDown() })
                requested.countDown()
                check(releaseDelivery.await(5, TimeUnit.SECONDS)) { "delivery gate was not released" }
            }
            assertNull(probe.initialize("1"))
            assertTrue("snapshot request blocked delivery", requested.await(5, TimeUnit.SECONDS))
            assertTrue("backend inspection did not run", backendRead.await(5, TimeUnit.SECONDS))
            assertEquals("snapshot settled before delivery returned", 1L, settled.count)
            releaseDelivery.countDown()
            val warm = await(snapshot.get())
            assertEquals(0.0, warm.handoffPending, 0.0)
            assertEquals(1.0, warm.handoffRoots, 0.0)
            assertEquals(1.0, warm.backendLeases, 0.0)
            assertTransientZero(warm)
        } finally {
            releaseDelivery.countDown()
            await(probe.close())
        }
        assertAllZero(await(probe.snapshotRfResources()))
    }

    @Test
    fun snapshotSettlementCanSynchronouslyWaitForCloseWithoutBlockingEitherOwner() {
        val completed = CountDownLatch(1)
        val releaseDelivery = CountDownLatch(1)
        val backendRead = CountDownLatch(1)
        val reentered = CountDownLatch(1)
        val failure = AtomicReference<Throwable>()
        val probe = HybridBleHubProbe.forAndroidResourceAccountingTest(testContext()) {
            backendRead.countDown()
        }
        try {
            probe.setCompletionListener {
                completed.countDown()
                check(releaseDelivery.await(5, TimeUnit.SECONDS)) { "delivery gate was not released" }
            }
            assertNull(probe.initialize("1"))
            assertTrue(completed.await(5, TimeUnit.SECONDS))
            val snapshot = probe.snapshotRfResources()
            assertTrue(backendRead.await(5, TimeUnit.SECONDS))
            // Register before the barrier can run so this callback executes on the resolver.
            snapshot.then { warm ->
                try {
                    assertEquals(0.0, warm.handoffPending, 0.0)
                    assertNull("snapshot resolved on an owner Looper", android.os.Looper.myLooper())
                    await(probe.close())
                    assertAllZero(await(probe.snapshotRfResources()))
                } catch (error: Throwable) {
                    failure.set(error)
                } finally {
                    reentered.countDown()
                }
            }.catch {
                failure.set(it)
                reentered.countDown()
            }
            releaseDelivery.countDown()
            assertTrue("snapshot listener deadlocked native close", reentered.await(15, TimeUnit.SECONDS))
            assertNull("snapshot listener failed", failure.get())
        } finally {
            releaseDelivery.countDown()
            await(probe.close())
        }
    }

    @Test
    fun closeBeforeDeliveryBarrierRejectsInspectionUntilRealCloseCompletes() {
        val completed = CountDownLatch(1)
        val releaseDelivery = CountDownLatch(1)
        val backendRead = CountDownLatch(1)
        val probe = HybridBleHubProbe.forAndroidResourceAccountingTest(testContext()) {
            backendRead.countDown()
        }
        try {
            probe.setCompletionListener {
                completed.countDown()
                check(releaseDelivery.await(5, TimeUnit.SECONDS)) { "delivery gate was not released" }
            }
            assertNull(probe.initialize("1"))
            assertTrue(completed.await(5, TimeUnit.SECONDS))
            val pendingSnapshot = probe.snapshotRfResources()
            assertTrue(backendRead.await(5, TimeUnit.SECONDS))
            val closing = probe.close()
            // Delivery still owns an executing job, so closed zeros must not be available.
            assertNotNull(awaitFailure(probe.snapshotRfResources()))
            releaseDelivery.countDown()
            assertNotNull(awaitFailure(pendingSnapshot))
            await(closing)
            assertAllZero(await(probe.snapshotRfResources()))
        } finally {
            releaseDelivery.countDown()
            await(probe.close())
        }
    }

    @Test
    fun closedHandoffCountsAcceptedJobsUntilTheyActuallyDrain() {
        val worker = HandlerThread("BleHubTest-accounted-handoff").also { it.start() }
        val handler = Handler(worker.looper)
        val entered = CountDownLatch(1)
        val release = CountDownLatch(1)
        val delivered = CountDownLatch(1)
        val handoff = AndroidEventHandoff<Int>(handler, 2, { delivered.countDown() }, {})
        try {
            assertTrue(handler.post {
                entered.countDown()
                release.await(5, TimeUnit.SECONDS)
            })
            assertTrue(entered.await(5, TimeUnit.SECONDS))
            assertEquals(AndroidEventHandoff.SubmitResult.ACCEPTED, handoff.submit(1))
            assertEquals(AndroidEventHandoff.SubmitResult.ACCEPTED, handoff.submit(2))
            assertEquals(2, handoff.pendingCount())
            handoff.close()
            assertEquals(2, handoff.pendingCount())
            assertEquals(AndroidEventHandoff.SubmitResult.CLOSED, handoff.submit(3))
            release.countDown()
            worker.quitSafely()
            worker.join(5_000)
            assertFalse(worker.isAlive)
            assertEquals(0, handoff.pendingCount())
            assertEquals(1L, delivered.count)
        } finally {
            handoff.close()
            release.countDown()
            worker.quitSafely()
            worker.join(5_000)
        }
    }

    // Identity/membership fixture only; no React engine or JS runtime is started.
    @Suppress("DEPRECATION")
    private fun testContext(): ReactApplicationContext =
        BridgeReactContext(InstrumentationRegistry.getInstrumentation().targetContext)

    private fun assertTransientZero(snapshot: RfResourceSnapshot) {
        assertEquals(
            listOf(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
            listOf(
                snapshot.activeScans, snapshot.connections, snapshot.gattAttributes,
                snapshot.subscriptions, snapshot.pendingIo, snapshot.watchdogs, snapshot.callbackRoutes,
                snapshot.handoffPending,
            ),
        )
    }

    private fun assertAllZero(snapshot: RfResourceSnapshot) {
        val counts = listOf(
            snapshot.activeScans, snapshot.connections, snapshot.gattAttributes,
            snapshot.subscriptions, snapshot.pendingIo, snapshot.watchdogs, snapshot.callbackRoutes,
            snapshot.ingressRoots, snapshot.adapterDelegates, snapshot.peerCacheEntries,
            snapshot.backendLeases, snapshot.hubRoots, snapshot.handoffRoots, snapshot.handoffPending,
            snapshot.listeners, snapshot.workerThreads, snapshot.lifecycleOwners,
        )
        assertTrue("closed counts: $counts", counts.all { it == 0.0 })
    }

    private fun <T> await(promise: Promise<T>): T {
        val settled = CountDownLatch(1)
        val value = AtomicReference<T>()
        val failure = AtomicReference<Throwable>()
        promise.then {
            value.set(it)
            settled.countDown()
        }.catch {
            failure.set(it)
            settled.countDown()
        }
        assertTrue("Promise did not settle", settled.await(10, TimeUnit.SECONDS))
        assertNull("Promise rejected", failure.get())
        return value.get()
    }

    private fun <T> awaitFailure(promise: Promise<T>): Throwable? {
        val settled = CountDownLatch(1)
        val failure = AtomicReference<Throwable>()
        promise.then { settled.countDown() }.catch {
            failure.set(it)
            settled.countDown()
        }
        assertTrue("Promise did not settle", settled.await(10, TimeUnit.SECONDS))
        assertNotNull("Promise incorrectly succeeded", failure.get())
        return failure.get()
    }
}
