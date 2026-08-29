package dev.blehub.phase0

import android.os.Handler
import android.os.Looper
import android.util.Log
import androidx.test.ext.junit.runners.AndroidJUnit4
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicReference
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class TokenIngressAndroidTest {
    @Test
    fun tokenIngressRunsOnArtMainLooperRustThreadAndWorkers() {
        val vmName = System.getProperty("java.vm.name").orEmpty()
        assertTrue(
            "expected Android runtime, got $vmName",
            vmName.contains("dalvik", ignoreCase = true) ||
                vmName.contains("art", ignoreCase = true),
        )

        val statuses = mutableListOf<EventIngressStatus>()
        val statusesLock = Any()
        val backend = TestBackend { token, command ->
            val status = pushBackendEvent(token, scanStarted(command))
            synchronized(statusesLock) { statuses += status }
        }
        val fixture = Phase0Fixture(backend)
        val testThread = Thread.currentThread()
        val mainThread = AtomicReference<Thread>()
        val mainFailure = AtomicReference<Throwable>()
        val mainDone = CountDownLatch(1)

        Handler(Looper.getMainLooper()).post {
            try {
                mainThread.set(Thread.currentThread())
                fixture.startScan(scanCommand(1uL))
            } catch (error: Throwable) {
                mainFailure.set(error)
            } finally {
                mainDone.countDown()
            }
        }

        assertTrue(mainDone.await(10, TimeUnit.SECONDS))
        mainFailure.get()?.let { throw it }
        assertNotEquals(testThread, mainThread.get())

        fixture.startScanFromRustThread(scanCommand(2uL))

        val token = checkNotNull(backend.token.get())
        val executor = Executors.newFixedThreadPool(8)
        val workerStatuses = (0 until 32).map { sequence ->
            executor.submit<EventIngressStatus> {
                pushBackendEvent(token, scanStarted(scanCommand(sequence.toULong() + 10uL)))
            }
        }.map { it.get(10, TimeUnit.SECONDS) }
        executor.shutdown()
        assertTrue(executor.awaitTermination(10, TimeUnit.SECONDS))

        assertTrue(workerStatuses.all { it == EventIngressStatus.Delivered })
        synchronized(statusesLock) {
            assertEquals(listOf(EventIngressStatus.Delivered, EventIngressStatus.Delivered), statuses)
        }
        assertEquals(34uL, fixture.snapshot().eventCount)
        assertEquals(1uL, fixture.snapshot().maximumActiveCallbacks)
        assertEquals(1, backend.capabilitiesCount.get())
        assertEquals(1, backend.bindCount.get())

        fixture.dispose()
        fixture.dispose()
        assertEquals(1, backend.disposeCount.get())
        assertEquals(
            EventIngressStatus.Closed,
            pushBackendEvent(token, scanStarted(scanCommand(100uL))),
        )
        fixture.close()
    }

    @Test
    fun explicitAckContainsSwallowedExceptionsOnArt() {
        val unitFixture = ForeignResultExceptionProbeFixture(
            object : Probe() {
                override fun unitResult() = error("unchecked ART unit-result failure")
            },
        )
        unitFixture.callUnitResult()
        unitFixture.close()

        val failingFixture = ForeignResultExceptionProbeFixture(
            object : Probe() {
                override fun acknowledgedUnitResult(token: ForeignCallAckToken): Unit =
                    error("unchecked ART explicit-ACK failure")
            },
        )
        repeat(10_000) {
            val error = assertThrows(CommandDispatchError.FfiContract::class.java) {
                failingFixture.callExplicitlyAcknowledgedUnitResult()
            }
            assertEquals(
                "foreign callback returned without the required explicit acknowledgement",
                error.detail,
            )
        }
        failingFixture.close()

        val successfulFixture = ForeignResultExceptionProbeFixture(Probe())
        successfulFixture.callExplicitlyAcknowledgedUnitResult()
        successfulFixture.close()
    }

    @Test
    fun recordsRepresentativeDirectJniMechanismCost() {
        val backend = TestBackend { _, _ -> Unit }
        val fixture = Phase0Fixture(backend)
        val command = scanCommand(1uL)
        val event = scanStarted(command)
        val token = checkNotNull(backend.token.get())

        repeat(100) {
            pushBackendEvent(token, event)
            fixture.startScan(command)
        }

        val pushIterations = 20_000
        val pushStarted = System.nanoTime()
        repeat(pushIterations) { pushBackendEvent(token, event) }
        val pushElapsed = System.nanoTime() - pushStarted

        val commandIterations = 10_000
        val commandStarted = System.nanoTime()
        repeat(commandIterations) { fixture.startScan(command) }
        val commandElapsed = System.nanoTime() - commandStarted

        Log.i(
            "BleHubFfiBenchmark",
            "transport=boltffi_jni operation=push iterations=$pushIterations " +
                "elapsed_ns=$pushElapsed ns_per_op=${pushElapsed / pushIterations}",
        )
        Log.i(
            "BleHubFfiBenchmark",
            "transport=boltffi_jni operation=command iterations=$commandIterations " +
                "elapsed_ns=$commandElapsed ns_per_op=${commandElapsed / commandIterations}",
        )
        assertEquals(20_100uL, fixture.snapshot().eventCount)
        fixture.dispose()
        fixture.close()
    }

    private open class Probe : ForeignResultExceptionProbe {
        override fun unitResult() = Unit

        override fun successSentinelResult() = true

        override fun recordAckResult() = ExceptionProbeAck(true)

        override fun acknowledgedUnitResult(token: ForeignCallAckToken) {
            check(acknowledgeForeignCall(token))
        }
    }

    private class TestBackend(
        private val onStartScan: (BackendEventIngressToken, StartScanCommand) -> Unit,
    ) : BleBackend {
        val token = AtomicReference<BackendEventIngressToken>()
        val capabilitiesCount = AtomicInteger()
        val bindCount = AtomicInteger()
        val disposeCount = AtomicInteger()

        override fun capabilities(): BackendCapabilities {
            capabilitiesCount.incrementAndGet()
            return BackendCapabilities(Platform.TEST, fixtureContractVersion)
        }

        override fun bindEventIngress(token: BackendEventIngressToken) {
            bindCount.incrementAndGet()
            check(this.token.compareAndSet(null, token))
        }

        override fun startScan(command: StartScanCommand) {
            onStartScan(checkNotNull(token.get()), command)
        }

        override fun connect(command: ConnectCommand) = Unit

        override fun dispose() {
            disposeCount.incrementAndGet()
        }
    }

    companion object {
        private fun scanCommand(id: ULong) = StartScanCommand(
            commandId = CommandId(id),
            scanSessionId = ScanSessionId(id + 100uL),
        )

        private fun scanStarted(command: StartScanCommand) = BleEvent(
            BleEventPayload.ScanStarted(command.commandId, command.scanSessionId),
        )
    }
}
