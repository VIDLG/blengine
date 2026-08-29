package dev.blehub.uniffi

import android.os.Handler
import android.os.Looper
import android.util.Log
import androidx.test.ext.junit.runners.AndroidJUnit4
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicReference
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class UniFfiAndroidTest {
    @Test
    fun directIngressRunsOnArtMainLooperRustThreadAndWorkers() {
        assertArtRuntime()
        val backend = RecordingBackend(CallbackMode.SYNCHRONOUS)
        val fixture = Phase0Fixture(backend)
        val testThread = Thread.currentThread()
        val mainThread = AtomicReference<Thread>()
        val mainFailure = AtomicReference<Throwable>()
        val mainDone = CountDownLatch(1)

        Handler(Looper.getMainLooper()).post {
            try {
                mainThread.set(Thread.currentThread())
                fixture.startScan(StartScanCommand(commandId = 1u, scanSessionId = 101u))
            } catch (error: Throwable) {
                mainFailure.set(error)
            } finally {
                mainDone.countDown()
            }
        }

        assertTrue(mainDone.await(10, TimeUnit.SECONDS))
        mainFailure.get()?.let { throw it }
        assertNotEquals(testThread, mainThread.get())

        fixture.startScanFromRustThread(
            StartScanCommand(commandId = 2u, scanSessionId = 102u),
        )
        assertNotEquals(testThread, backend.startScanThread.get())

        val executor = Executors.newFixedThreadPool(8)
        val statuses = (0 until 32).map { sequence ->
            executor.submit<EventIngressStatus> {
                backend.push(
                    BleEvent(
                        sequence = sequence.toULong() + 10u,
                        commandId = null,
                        payload = byteArrayOf(sequence.toByte()),
                    ),
                )
            }
        }.map { it.get(10, TimeUnit.SECONDS) }
        executor.shutdown()
        assertTrue(executor.awaitTermination(10, TimeUnit.SECONDS))

        assertTrue(statuses.all { it == EventIngressStatus.DELIVERED })
        val events = fixture.events()
        assertEquals(34, events.size)
        assertEquals(1uL, events[0].commandId)
        assertArrayEquals(byteArrayOf(1, 2, 3), events[0].payload)

        fixture.dispose()
        fixture.dispose()
        assertEquals(1, backend.disposeCount.get())
        assertEquals(
            EventIngressStatus.CLOSED,
            backend.push(BleEvent(sequence = 100u, commandId = null, payload = byteArrayOf())),
        )
        release(fixture, backend)
    }

    @Test
    fun declaredAndUncheckedKotlinFailuresRemainTypedOnArt() {
        assertArtRuntime()
        val rejectingBackend = RecordingBackend(CallbackMode.REJECT)
        val rejectingFixture = Phase0Fixture(rejectingBackend)
        val rejected = assertThrows(CommandDispatchException.Rejected::class.java) {
            rejectingFixture.startScan(StartScanCommand(commandId = 3u, scanSessionId = 103u))
        }
        assertEquals("declared rejection", rejected.reason)
        rejectingFixture.dispose()
        release(rejectingFixture, rejectingBackend)

        val unexpectedBackend = RecordingBackend(CallbackMode.UNEXPECTED)
        val unexpectedFixture = Phase0Fixture(unexpectedBackend)
        repeat(10_000) {
            val error = assertThrows(CommandDispatchException.UnexpectedCallback::class.java) {
                unexpectedFixture.startScan(
                    StartScanCommand(commandId = 4u, scanSessionId = 104u),
                )
            }
            assertTrue(error.reason.contains("undeclared backend failure"))
        }
        unexpectedFixture.dispose()
        release(unexpectedFixture, unexpectedBackend)

        val setupError = assertThrows(BleHubException.UnexpectedCallback::class.java) {
            Phase0Fixture(RecordingBackend(CallbackMode.UNEXPECTED_BIND))
        }
        assertTrue(setupError.reason.contains("undeclared bind failure"))
    }

    @Test
    fun disposeWaitsForAnAdmittedDirectIngressCallback() {
        assertArtRuntime()
        val backend = RecordingBackend(CallbackMode.NO_EVENT)
        val fixture = Phase0Fixture(backend)
        fixture.setCallbackBlocking(true)
        val executor = Executors.newFixedThreadPool(2)
        val push = executor.submit<EventIngressStatus> {
            backend.push(BleEvent(sequence = 200u, commandId = null, payload = byteArrayOf()))
        }
        assertTrue(fixture.waitForBlockedCallback(5_000uL))

        val dispose = executor.submit<Unit> { fixture.dispose() }
        Thread.sleep(100)
        assertFalse(dispose.isDone)

        fixture.releaseBlockedCallbacks()
        assertEquals(EventIngressStatus.DELIVERED, push.get(5, TimeUnit.SECONDS))
        dispose.get(5, TimeUnit.SECONDS)
        assertEquals(1, backend.disposeCount.get())
        assertEquals(1, fixture.events().size)
        assertEquals(
            EventIngressStatus.CLOSED,
            backend.push(BleEvent(sequence = 201u, commandId = null, payload = byteArrayOf())),
        )
        executor.shutdown()
        assertTrue(executor.awaitTermination(5, TimeUnit.SECONDS))
        release(fixture, backend)
    }

    @Test
    fun tenThousandDirectHandleLifecyclesReturnToBaseline() {
        assertArtRuntime()
        assertEquals(0, FfiConverterTypeBleBackend.handleMap.size)
        val baseline = lifecycleCounters()
        val disposeCount = AtomicInteger()

        repeat(10_000) { index ->
            val backend = RecordingBackend(
                mode = CallbackMode.NO_EVENT,
                sharedDisposeCount = disposeCount,
            )
            val fixture = Phase0Fixture(backend)
            fixture.dispose()
            assertEquals(
                EventIngressStatus.CLOSED,
                backend.push(
                    BleEvent(
                        sequence = index.toULong(),
                        commandId = null,
                        payload = byteArrayOf(),
                    ),
                ),
            )
            release(fixture, backend)
        }

        assertEquals(10_000, disposeCount.get())
        assertEquals(0, FfiConverterTypeBleBackend.handleMap.size)
        val finalCounters = lifecycleCounters()
        assertEquals(baseline.ingressCreated + 10_000uL, finalCounters.ingressCreated)
        assertEquals(baseline.ingressDropped + 10_000uL, finalCounters.ingressDropped)
    }

    @Test
    fun representativePayloadsCrossArtWithoutCorruption() {
        assertArtRuntime()
        val backend = RecordingBackend(CallbackMode.NO_EVENT)
        val fixture = Phase0Fixture(backend)
        var sequence = 0uL
        var expectedBytes = 0

        for (size in listOf(20, 244, 1024)) {
            repeat(1_000) {
                val payload = ByteArray(size) { index -> (index and 0xff).toByte() }
                expectedBytes += payload.size
                assertEquals(
                    EventIngressStatus.DELIVERED,
                    backend.push(
                        BleEvent(
                            sequence = sequence++,
                            commandId = null,
                            payload = payload,
                        ),
                    ),
                )
            }
        }

        val events = fixture.events()
        assertEquals(3_000, events.size)
        assertEquals(expectedBytes, events.sumOf { it.payload.size })
        assertArrayEquals(ByteArray(1024) { (it and 0xff).toByte() }, events.last().payload)
        fixture.dispose()
        release(fixture, backend)
    }

    @Test
    fun recordsRepresentativeArtMechanismCost() {
        assertArtRuntime()
        val backend = RecordingBackend(CallbackMode.NO_EVENT)
        val fixture = Phase0Fixture(backend)
        val event = BleEvent(sequence = 1u, commandId = null, payload = byteArrayOf())
        val command = StartScanCommand(commandId = 1u, scanSessionId = 1u)

        repeat(100) {
            backend.push(event)
            fixture.startScan(command)
        }

        val pushIterations = 20_000
        val pushStarted = System.nanoTime()
        repeat(pushIterations) { backend.push(event) }
        val pushElapsed = System.nanoTime() - pushStarted

        val commandIterations = 10_000
        val commandStarted = System.nanoTime()
        repeat(commandIterations) { fixture.startScan(command) }
        val commandElapsed = System.nanoTime() - commandStarted

        Log.i(
            BENCHMARK_TAG,
            "transport=uniffi_jna operation=push iterations=$pushIterations " +
                "elapsed_ns=$pushElapsed ns_per_op=${pushElapsed / pushIterations}",
        )
        Log.i(
            BENCHMARK_TAG,
            "transport=uniffi_jna operation=command iterations=$commandIterations " +
                "elapsed_ns=$commandElapsed ns_per_op=${commandElapsed / commandIterations}",
        )
        assertEquals(20_100, fixture.events().size)
        fixture.dispose()
        release(fixture, backend)
    }

    private fun assertArtRuntime() {
        val vmName = System.getProperty("java.vm.name").orEmpty()
        assertTrue(
            "expected Android runtime, got $vmName",
            vmName.contains("dalvik", ignoreCase = true) ||
                vmName.contains("art", ignoreCase = true),
        )
    }

    private fun release(fixture: Phase0Fixture, backend: RecordingBackend) {
        fixture.destroy()
        backend.releaseIngress()
    }

    companion object {
        private const val BENCHMARK_TAG = "BleHubFfiBenchmark"
    }
}

private enum class CallbackMode {
    SYNCHRONOUS,
    NO_EVENT,
    REJECT,
    UNEXPECTED,
    UNEXPECTED_BIND,
}

private class RecordingBackend(
    private val mode: CallbackMode,
    private val sharedDisposeCount: AtomicInteger? = null,
) : BleBackend {
    private val ingress = AtomicReference<BackendEventIngressHandle>()
    val disposeCount = AtomicInteger()
    val startScanThread = AtomicReference<Thread>()

    override fun bindEventIngress(ingress: BackendEventIngressHandle) {
        if (mode == CallbackMode.UNEXPECTED_BIND) {
            error("undeclared bind failure")
        }
        check(this.ingress.compareAndSet(null, ingress))
    }

    override fun startScan(command: StartScanCommand) {
        startScanThread.set(Thread.currentThread())
        when (mode) {
            CallbackMode.SYNCHRONOUS -> push(
                BleEvent(
                    sequence = 1u,
                    commandId = command.commandId,
                    payload = byteArrayOf(1, 2, 3),
                ),
            )
            CallbackMode.NO_EVENT -> Unit
            CallbackMode.REJECT -> throw CommandDispatchException.Rejected("declared rejection")
            CallbackMode.UNEXPECTED -> error("undeclared backend failure")
            CallbackMode.UNEXPECTED_BIND -> error("fixture construction should have failed")
        }
    }

    override fun dispose() {
        disposeCount.incrementAndGet()
        sharedDisposeCount?.incrementAndGet()
    }

    fun push(event: BleEvent): EventIngressStatus =
        checkNotNull(ingress.get()) { "event ingress was not bound" }.push(event)

    fun releaseIngress() {
        ingress.getAndSet(null)?.destroy()
    }
}
