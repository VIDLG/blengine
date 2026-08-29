package dev.blehub.uniffi

import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicLong
import java.util.concurrent.atomic.AtomicReference
import kotlin.test.Test
import kotlin.test.assertContentEquals
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertNotEquals
import kotlin.test.assertTrue
import uniffi.blehub_uniffi_lifecycle.BackendEventIngressHandle
import uniffi.blehub_uniffi_lifecycle.BleBackend
import uniffi.blehub_uniffi_lifecycle.BleEvent
import uniffi.blehub_uniffi_lifecycle.BleHubException
import uniffi.blehub_uniffi_lifecycle.CommandDispatchException
import uniffi.blehub_uniffi_lifecycle.EventIngressStatus
import uniffi.blehub_uniffi_lifecycle.Phase0Fixture
import uniffi.blehub_uniffi_lifecycle.StartScanCommand

class UniFfiLifecycleTest {
    @Test
    fun kotlinBackendRetainsDirectRustIngressHandle() {
        val backend = RecordingBackend(CallbackMode.SYNCHRONOUS)
        val fixture = Phase0Fixture(backend)

        fixture.startScan(StartScanCommand(commandId = 41u, scanSessionId = 7u))

        val event = fixture.events().single()
        assertEquals(41uL, event.commandId)
        assertContentEquals(byteArrayOf(1, 2, 3), event.payload)
        fixture.dispose()
        assertEquals(1, backend.disposeCount.get())
        assertEquals(
            EventIngressStatus.CLOSED,
            backend.push(BleEvent(sequence = 2u, commandId = 41u, payload = byteArrayOf(9))),
        )
        release(fixture, backend)
    }

    @Test
    fun kotlinWorkerThreadCanPushAfterBindReturns() {
        val callback = CountDownLatch(1)
        val backend = RecordingBackend(CallbackMode.KOTLIN_THREAD, callback)
        val fixture = Phase0Fixture(backend)

        fixture.startScan(StartScanCommand(commandId = 2u, scanSessionId = 1u))

        assertTrue(callback.await(5, TimeUnit.SECONDS))
        assertEquals(2uL, fixture.events().single().commandId)
        fixture.dispose()
        release(fixture, backend)
    }

    @Test
    fun rustCreatedThreadCanCallKotlinBackendAndRustIngress() {
        val callerThread = Thread.currentThread().id
        val backend = RecordingBackend(CallbackMode.SYNCHRONOUS)
        val fixture = Phase0Fixture(backend)

        fixture.startScanFromRustThread(StartScanCommand(commandId = 3u, scanSessionId = 1u))

        assertNotEquals(callerThread, backend.startScanThread.get())
        assertEquals(3uL, fixture.events().single().commandId)
        fixture.dispose()
        release(fixture, backend)
    }

    @Test
    fun declaredAndUnexpectedKotlinFailuresMapToTypedRustErrors() {
        val rejectingBackend = RecordingBackend(CallbackMode.REJECT)
        val rejectingFixture = Phase0Fixture(rejectingBackend)
        val rejected = assertFailsWith<CommandDispatchException.Rejected> {
            rejectingFixture.startScan(StartScanCommand(commandId = 4u, scanSessionId = 1u))
        }
        assertEquals("declared rejection", rejected.reason)
        rejectingFixture.dispose()
        release(rejectingFixture, rejectingBackend)

        val unexpectedBackend = RecordingBackend(CallbackMode.UNEXPECTED)
        val unexpectedFixture = Phase0Fixture(unexpectedBackend)
        val unexpected = assertFailsWith<CommandDispatchException.UnexpectedCallback> {
            unexpectedFixture.startScan(StartScanCommand(commandId = 5u, scanSessionId = 1u))
        }
        assertTrue(unexpected.reason.contains("undeclared backend failure"))
        unexpectedFixture.dispose()
        release(unexpectedFixture, unexpectedBackend)
    }

    @Test
    fun setupFailuresMapAcrossTheForeignTraitBoundary() {
        val declared = assertFailsWith<BleHubException.SetupRejected> {
            Phase0Fixture(RecordingBackend(CallbackMode.REJECT_BIND))
        }
        assertEquals("declared setup rejection", declared.reason)

        val unexpected = assertFailsWith<BleHubException.UnexpectedCallback> {
            Phase0Fixture(RecordingBackend(CallbackMode.UNEXPECTED_BIND))
        }
        assertTrue(unexpected.reason.contains("undeclared bind failure"))
    }

    @Test
    fun representativePayloadsSurviveRepeatedDirectPushes() {
        val backend = RecordingBackend(CallbackMode.NO_EVENT)
        val fixture = Phase0Fixture(backend)
        var expectedBytes = 0
        var sequence = 0uL

        for (size in listOf(20, 244, 1024)) {
            repeat(1_000) {
                expectedBytes += size
                assertEquals(
                    EventIngressStatus.DELIVERED,
                    backend.push(
                        BleEvent(
                            sequence = sequence++,
                            commandId = null,
                            payload = ByteArray(size) { (it and 0xff).toByte() },
                        ),
                    ),
                )
            }
        }

        val events = fixture.events()
        assertEquals(3_000, events.size)
        assertEquals(expectedBytes, events.sumOf { it.payload.size })
        fixture.dispose()
        release(fixture, backend)
    }

    @Test
    fun tenThousandDirectHandleLifecyclesComplete() {
        val disposeCount = AtomicInteger()

        repeat(10_000) { index ->
            val backend = RecordingBackend(CallbackMode.NO_EVENT, sharedDisposeCount = disposeCount)
            val fixture = Phase0Fixture(backend)
            fixture.dispose()
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
    }

    private fun release(fixture: Phase0Fixture, backend: RecordingBackend) {
        fixture.destroy()
        backend.releaseIngress()
    }
}

private enum class CallbackMode {
    SYNCHRONOUS,
    KOTLIN_THREAD,
    NO_EVENT,
    REJECT,
    UNEXPECTED,
    REJECT_BIND,
    UNEXPECTED_BIND,
}

private class RecordingBackend(
    private val mode: CallbackMode,
    private val callback: CountDownLatch? = null,
    private val sharedDisposeCount: AtomicInteger? = null,
) : BleBackend {
    private val ingress = AtomicReference<BackendEventIngressHandle>()
    val disposeCount = AtomicInteger()
    val startScanThread = AtomicLong(-1)

    override fun bindEventIngress(ingress: BackendEventIngressHandle) {
        when (mode) {
            CallbackMode.REJECT_BIND -> throw BleHubException.SetupRejected("declared setup rejection")
            CallbackMode.UNEXPECTED_BIND -> error("undeclared bind failure")
            else -> check(this.ingress.compareAndSet(null, ingress))
        }
    }

    override fun startScan(command: StartScanCommand) {
        startScanThread.set(Thread.currentThread().id)
        when (mode) {
            CallbackMode.SYNCHRONOUS -> push(eventFor(command))
            CallbackMode.KOTLIN_THREAD -> Thread {
                push(eventFor(command))
                callback?.countDown()
            }.start()
            CallbackMode.REJECT -> throw CommandDispatchException.Rejected("declared rejection")
            CallbackMode.UNEXPECTED -> error("undeclared backend failure")
            CallbackMode.NO_EVENT -> Unit
            CallbackMode.REJECT_BIND,
            CallbackMode.UNEXPECTED_BIND,
            -> error("fixture construction should have failed")
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

    private fun eventFor(command: StartScanCommand) = BleEvent(
        sequence = 1u,
        commandId = command.commandId,
        payload = byteArrayOf(1, 2, 3),
    )
}
