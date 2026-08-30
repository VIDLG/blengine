package dev.blehub.candidate

import dev.blehub.candidate.blehub.BackendCapabilities
import dev.blehub.candidate.blehub.BackendEventIngressHandle
import dev.blehub.candidate.blehub.BleBackend
import dev.blehub.candidate.blehub.BleEvent
import dev.blehub.candidate.blehub.BleHubException
import dev.blehub.candidate.blehub.CommandDispatchException
import dev.blehub.candidate.blehub.CommandId
import dev.blehub.candidate.blehub.ConnectCommand
import dev.blehub.candidate.blehub.ConnectionId
import dev.blehub.candidate.blehub.DisconnectCommand
import dev.blehub.candidate.blehub.DiscoverGattCommand
import dev.blehub.candidate.blehub.EventIngressStatus
import dev.blehub.candidate.blehub.InitializeCommand
import dev.blehub.candidate.blehub.PeripheralId
import dev.blehub.candidate.blehub.Platform
import dev.blehub.candidate.blehub.ReadCharacteristicCommand
import dev.blehub.candidate.blehub.ReadRssiCommand
import dev.blehub.candidate.blehub.ScanOptions
import dev.blehub.candidate.blehub.ScanSessionId
import dev.blehub.candidate.blehub.SetNotificationsCommand
import dev.blehub.candidate.blehub.StartScanCommand
import dev.blehub.candidate.blehub.StopScanCommand
import dev.blehub.candidate.blehub.WriteCharacteristicCommand
import dev.blehub.candidate.fixture.FixtureSinkBehavior
import dev.blehub.candidate.fixture.ProductionCandidateFixture
import dev.blehub.candidate.fixture.fixtureContractVersion
import dev.blehub.candidate.fixture.fixtureEventCatalog
import dev.blehub.candidate.fixture.fixtureEventCatalogMatches
import dev.blehub.candidate.fixture.fixtureLifecycleCounters
import java.util.Collections
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicLong
import java.util.concurrent.atomic.AtomicReference
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertFalse
import kotlin.test.assertIs
import kotlin.test.assertNotEquals
import kotlin.test.assertNull
import kotlin.test.assertTrue

class UniFfiProductionCandidateTest {
    @Test
    fun synchronousEventsTraverseTheProductionWeakIngressGraph() {
        val backend = CandidateBackend(BackendMode.SYNCHRONOUS)
        val fixture = ProductionCandidateFixture(backend)
        val command = scanCommand(41uL)

        fixture.startScan(command)

        val events = fixture.snapshot().events
        assertEquals(2, events.size)
        assertEquals(
            BleEvent.ScanStarted(command.commandId, command.scanSessionId),
            events[0],
        )
        assertEquals(
            BleEvent.CommandCompleted(command.commandId, null),
            events[1],
        )
        assertEquals(
            listOf<EventIngressStatus>(EventIngressStatus.Delivered, EventIngressStatus.Delivered),
            backend.pushStatuses,
        )
        assertEquals(Platform.TEST, fixture.capabilities().platform)
        release(fixture, backend)
    }

    @Test
    fun completeEventCatalogCrossesTheProductionIngressUnchanged() {
        val backend = CandidateBackend(BackendMode.NO_EVENT)
        val fixture = ProductionCandidateFixture(backend)
        try {
            val statuses = fixtureEventCatalog().map(backend::push)

            assertTrue(statuses.all { it == EventIngressStatus.Delivered })
            assertTrue(fixtureEventCatalogMatches(fixture.snapshot().events))
        } finally {
            release(fixture, backend)
        }
    }

    @Test
    fun kotlinAndRustCreatedThreadsCanCompleteTheCallbackCycle() {
        val callbackDone = CountDownLatch(1)
        val kotlinBackend = CandidateBackend(BackendMode.KOTLIN_THREAD, callbackDone)
        val kotlinFixture = ProductionCandidateFixture(kotlinBackend)

        kotlinFixture.startScan(scanCommand(2uL))

        assertTrue(callbackDone.await(5, TimeUnit.SECONDS))
        assertEquals(2, kotlinFixture.snapshot().events.size)
        release(kotlinFixture, kotlinBackend)

        val callerThread = Thread.currentThread().id
        val rustBackend = CandidateBackend(BackendMode.SYNCHRONOUS)
        val rustFixture = ProductionCandidateFixture(rustBackend)

        rustFixture.startScanFromRustThread(scanCommand(3uL))

        assertNotEquals(callerThread, rustBackend.startScanThread.get())
        assertEquals(2, rustFixture.snapshot().events.size)
        release(rustFixture, rustBackend)
    }

    @Test
    fun dispatchRejectionAndUnexpectedExceptionProduceNoEvent() {
        val rejectingBackend = CandidateBackend(BackendMode.REJECT_COMMAND)
        val rejectingFixture = ProductionCandidateFixture(rejectingBackend)

        val rejected = assertFailsWith<CommandDispatchException.BackendUnavailable> {
            rejectingFixture.startScan(scanCommand(4uL))
        }

        assertEquals("declared dispatch rejection", rejected.detail)
        assertTrue(rejectingFixture.snapshot().events.isEmpty())
        release(rejectingFixture, rejectingBackend)

        val unexpectedBackend = CandidateBackend(BackendMode.UNEXPECTED_COMMAND)
        val unexpectedFixture = ProductionCandidateFixture(unexpectedBackend)

        val unexpected = assertFailsWith<CommandDispatchException.FfiContract> {
            unexpectedFixture.startScan(scanCommand(5uL))
        }

        assertTrue(unexpected.detail.contains("undeclared command failure"))
        assertTrue(unexpectedFixture.snapshot().events.isEmpty())
        release(unexpectedFixture, unexpectedBackend)
    }

    @Test
    fun capabilitiesAndBindFailuresRollbackWithoutExposingAHalfHub() {
        val before = fixtureLifecycleCounters()

        val typedCapabilities = CandidateBackend(BackendMode.TYPED_CAPABILITIES_FAILURE)
        assertFailsWith<BleHubException.BackendAssembly> {
            ProductionCandidateFixture(typedCapabilities)
        }
        assertEquals(1, typedCapabilities.disposeCount.get())
        assertEquals(0, typedCapabilities.bindCount.get())

        val unexpectedCapabilities = CandidateBackend(BackendMode.UNEXPECTED_CAPABILITIES_FAILURE)
        val unexpected = assertFailsWith<BleHubException.FfiContract> {
            ProductionCandidateFixture(unexpectedCapabilities)
        }
        assertTrue(unexpected.detail.contains("undeclared capabilities failure"))
        assertEquals(1, unexpectedCapabilities.disposeCount.get())
        assertEquals(0, unexpectedCapabilities.bindCount.get())

        val invalidVersion = CandidateBackend(BackendMode.INVALID_CONTRACT_VERSION)
        assertFailsWith<BleHubException.FfiContract> {
            ProductionCandidateFixture(invalidVersion)
        }
        assertEquals(1, invalidVersion.disposeCount.get())
        assertEquals(0, invalidVersion.bindCount.get())

        val typedBind = CandidateBackend(BackendMode.TYPED_BIND_FAILURE)
        assertFailsWith<BleHubException.BackendAssembly> {
            ProductionCandidateFixture(typedBind)
        }
        assertEquals(1, typedBind.disposeCount.get())
        assertFalse(typedBind.hasIngress())

        val unexpectedBind = CandidateBackend(BackendMode.UNEXPECTED_BIND_FAILURE)
        val unexpectedBindError = assertFailsWith<BleHubException.FfiContract> {
            ProductionCandidateFixture(unexpectedBind)
        }
        assertTrue(unexpectedBindError.detail.contains("undeclared bind failure"))
        assertEquals(1, unexpectedBind.disposeCount.get())
        assertFalse(unexpectedBind.hasIngress())

        val after = fixtureLifecycleCounters()
        assertEquals(before.fixtureCreated, after.fixtureCreated)
        assertEquals(before.fixtureDropped, after.fixtureDropped)
        assertEquals(5uL, after.sinkCreated - before.sinkCreated)
        assertEquals(5uL, after.sinkDropped - before.sinkDropped)
    }

    @Test
    fun retainedHandleAndPushDuringBindAreRolledBack() {
        val retained = CandidateBackend(BackendMode.RETAIN_BEFORE_BIND_FAILURE)

        assertFailsWith<BleHubException.BackendAssembly> {
            ProductionCandidateFixture(retained)
        }

        assertEquals(1, retained.disposeCount.get())
        assertFalse(retained.hasIngress())

        val pushing = CandidateBackend(BackendMode.PUSH_DURING_BIND)
        val error = assertFailsWith<BleHubException.FfiContract> {
            ProductionCandidateFixture(pushing)
        }

        assertTrue(error.detail.contains("while binding ingress"))
        assertEquals(EventIngressStatus.Closed, pushing.bindPushStatus.get())
        assertEquals(1, pushing.disposeCount.get())
        assertFalse(pushing.hasIngress())
    }

    @Test
    fun sinkFailureAndPanicStayInsideTheIngressBoundary() {
        val backend = CandidateBackend(BackendMode.NO_EVENT)
        val fixture = ProductionCandidateFixture(backend)

        fixture.setSinkBehavior(FixtureSinkBehavior.REJECT)
        assertIs<EventIngressStatus.DownstreamFailed>(backend.push(scanStarted(scanCommand(6uL))))
        assertTrue(fixture.snapshot().events.isEmpty())

        fixture.setSinkBehavior(FixtureSinkBehavior.PANIC)
        assertIs<EventIngressStatus.DownstreamFailed>(backend.push(scanStarted(scanCommand(7uL))))
        assertTrue(fixture.snapshot().events.isEmpty())
        release(fixture, backend)
    }

    @Test
    fun closeDrainsInFlightCallbackAndDisposeSeesClosedIngress() {
        val backend = CandidateBackend(BackendMode.NO_EVENT)
        val fixture = ProductionCandidateFixture(backend)
        fixture.setSinkBehavior(FixtureSinkBehavior.BLOCK)
        val pushDone = CountDownLatch(1)
        val pushStatus = AtomicReference<EventIngressStatus>()
        val callback = Thread {
            pushStatus.set(backend.push(scanStarted(scanCommand(8uL))))
            pushDone.countDown()
        }
        callback.start()
        assertTrue(fixture.waitForBlockedCallback(5_000uL))

        val disposeDone = CountDownLatch(1)
        val disposeFailure = AtomicReference<Throwable>()
        Thread {
            try {
                fixture.dispose()
            } catch (error: Throwable) {
                disposeFailure.set(error)
            } finally {
                disposeDone.countDown()
            }
        }.start()
        assertFalse(disposeDone.await(50, TimeUnit.MILLISECONDS))

        fixture.releaseBlockedCallback()
        assertTrue(pushDone.await(5, TimeUnit.SECONDS))
        assertTrue(disposeDone.await(5, TimeUnit.SECONDS))
        assertNull(disposeFailure.get())
        assertEquals(EventIngressStatus.Delivered, pushStatus.get())
        assertEquals(1, fixture.snapshot().events.size)
        fixture.destroy()
        assertFalse(backend.hasIngress())

        val disposingBackend = CandidateBackend(BackendMode.PUSH_DURING_DISPOSE)
        val disposingFixture = ProductionCandidateFixture(disposingBackend)
        disposingFixture.dispose()
        assertEquals(EventIngressStatus.Closed, disposingBackend.disposePushStatus.get())
        disposingFixture.destroy()
    }

    @Test
    fun typedAndUnexpectedDisposeFailuresReleaseTheRootBeforeReturning() {
        val typedBackend = CandidateBackend(BackendMode.TYPED_DISPOSE_FAILURE)
        val typedFixture = ProductionCandidateFixture(typedBackend)

        val typed = assertFailsWith<BleHubException.BackendCleanup> {
            typedFixture.dispose()
        }

        assertEquals("declared cleanup failure", typed.detail)
        assertFalse(typedBackend.hasIngress())
        typedFixture.dispose()
        typedFixture.destroy()

        val unexpectedBackend = CandidateBackend(BackendMode.UNEXPECTED_DISPOSE_FAILURE)
        val unexpectedFixture = ProductionCandidateFixture(unexpectedBackend)

        val unexpected = assertFailsWith<BleHubException.FfiContract> {
            unexpectedFixture.dispose()
        }

        assertTrue(unexpected.detail.contains("undeclared cleanup failure"))
        assertFalse(unexpectedBackend.hasIngress())
        unexpectedFixture.dispose()
        unexpectedFixture.destroy()
    }

    @Test
    fun tenThousandProductionObjectLifecyclesReturnToBaseline() {
        val before = fixtureLifecycleCounters()
        val disposeCount = AtomicInteger()

        repeat(10_000) {
            val backend = CandidateBackend(
                mode = BackendMode.NO_EVENT,
                sharedDisposeCount = disposeCount,
            )
            val fixture = ProductionCandidateFixture(backend)
            fixture.dispose()
            fixture.dispose()
            fixture.destroy()
            assertFalse(backend.hasIngress())
        }

        val after = fixtureLifecycleCounters()
        assertEquals(10_000, disposeCount.get())
        assertEquals(10_000uL, after.fixtureCreated - before.fixtureCreated)
        assertEquals(10_000uL, after.fixtureDropped - before.fixtureDropped)
        assertEquals(10_000uL, after.sinkCreated - before.sinkCreated)
        assertEquals(10_000uL, after.sinkDropped - before.sinkDropped)
    }

    private fun release(fixture: ProductionCandidateFixture, backend: CandidateBackend) {
        fixture.dispose()
        fixture.dispose()
        fixture.destroy()
        assertEquals(1, backend.disposeCount.get())
        assertFalse(backend.hasIngress())
    }
}

private enum class BackendMode {
    SYNCHRONOUS,
    KOTLIN_THREAD,
    NO_EVENT,
    REJECT_COMMAND,
    UNEXPECTED_COMMAND,
    TYPED_CAPABILITIES_FAILURE,
    UNEXPECTED_CAPABILITIES_FAILURE,
    INVALID_CONTRACT_VERSION,
    TYPED_BIND_FAILURE,
    UNEXPECTED_BIND_FAILURE,
    RETAIN_BEFORE_BIND_FAILURE,
    PUSH_DURING_BIND,
    PUSH_DURING_DISPOSE,
    TYPED_DISPOSE_FAILURE,
    UNEXPECTED_DISPOSE_FAILURE,
}

private class CandidateBackend(
    private val mode: BackendMode,
    private val callbackDone: CountDownLatch? = null,
    private val sharedDisposeCount: AtomicInteger? = null,
) : BleBackend {
    private val ingress = AtomicReference<BackendEventIngressHandle>()
    val capabilitiesCount = AtomicInteger()
    val bindCount = AtomicInteger()
    val disposeCount = AtomicInteger()
    val startScanThread = AtomicLong(-1)
    val bindPushStatus = AtomicReference<EventIngressStatus>()
    val disposePushStatus = AtomicReference<EventIngressStatus>()
    val pushStatuses: MutableList<EventIngressStatus> = Collections.synchronizedList(mutableListOf())

    override fun capabilities(): BackendCapabilities {
        capabilitiesCount.incrementAndGet()
        val contractVersion = when (mode) {
            BackendMode.TYPED_CAPABILITIES_FAILURE ->
                throw BleHubException.BackendAssembly("declared capabilities failure")
            BackendMode.UNEXPECTED_CAPABILITIES_FAILURE -> error("undeclared capabilities failure")
            BackendMode.INVALID_CONTRACT_VERSION -> fixtureContractVersion() + 1u
            else -> fixtureContractVersion()
        }
        return BackendCapabilities(
            platform = Platform.TEST,
            contractVersion = contractVersion,
            supportsDuplicateScanResults = false,
            supportsConnectableFlag = false,
            supportsReadRssi = false,
            supportsRequiredNotification = false,
            supportsRequiredIndication = false,
            reportsActualNotificationMode = false,
        )
    }

    override fun bindEventIngress(ingress: BackendEventIngressHandle) {
        bindCount.incrementAndGet()
        when (mode) {
            BackendMode.TYPED_BIND_FAILURE ->
                throw BleHubException.BackendAssembly("declared bind failure")
            BackendMode.UNEXPECTED_BIND_FAILURE -> error("undeclared bind failure")
            BackendMode.RETAIN_BEFORE_BIND_FAILURE -> {
                check(this.ingress.compareAndSet(null, ingress))
                throw BleHubException.BackendAssembly("retained before bind failure")
            }
            BackendMode.PUSH_DURING_BIND -> {
                check(this.ingress.compareAndSet(null, ingress))
                bindPushStatus.set(ingress.push(scanStarted(scanCommand(90uL))))
            }
            else -> check(this.ingress.compareAndSet(null, ingress))
        }
    }

    override fun initialize(command: InitializeCommand) = Unit

    override fun startScan(command: StartScanCommand) {
        startScanThread.set(Thread.currentThread().id)
        when (mode) {
            BackendMode.SYNCHRONOUS -> emitSuccessfulScan(command)
            BackendMode.KOTLIN_THREAD -> Thread {
                try {
                    emitSuccessfulScan(command)
                } finally {
                    callbackDone?.countDown()
                }
            }.start()
            BackendMode.REJECT_COMMAND -> throw CommandDispatchException.BackendUnavailable(
                "declared dispatch rejection",
                null,
            )
            BackendMode.UNEXPECTED_COMMAND -> error("undeclared command failure")
            else -> Unit
        }
    }

    override fun stopScan(command: StopScanCommand) = Unit

    override fun connect(command: ConnectCommand) = Unit

    override fun disconnect(command: DisconnectCommand) = Unit

    override fun discoverGatt(command: DiscoverGattCommand) = Unit

    override fun readCharacteristic(command: ReadCharacteristicCommand) = Unit

    override fun writeCharacteristic(command: WriteCharacteristicCommand) = Unit

    override fun setNotifications(command: SetNotificationsCommand) = Unit

    override fun readRssi(command: ReadRssiCommand) = Unit

    override fun dispose() {
        disposeCount.incrementAndGet()
        sharedDisposeCount?.incrementAndGet()
        val root = ingress.getAndSet(null)
        if (mode == BackendMode.PUSH_DURING_DISPOSE && root != null) {
            disposePushStatus.set(root.push(scanStarted(scanCommand(91uL))))
        }
        root?.destroy()
        when (mode) {
            BackendMode.TYPED_DISPOSE_FAILURE ->
                throw BleHubException.BackendCleanup("declared cleanup failure", null)
            BackendMode.UNEXPECTED_DISPOSE_FAILURE -> error("undeclared cleanup failure")
            else -> Unit
        }
    }

    fun push(event: BleEvent): EventIngressStatus =
        checkNotNull(ingress.get()) { "event ingress was not bound" }.push(event)

    fun hasIngress(): Boolean = ingress.get() != null

    private fun emitSuccessfulScan(command: StartScanCommand) {
        pushStatuses += push(scanStarted(command))
        pushStatuses += push(BleEvent.CommandCompleted(command.commandId, null))
    }
}

private fun scanCommand(id: ULong) = StartScanCommand(
    commandId = CommandId(id),
    scanSessionId = ScanSessionId(id + 100uL),
    options = ScanOptions(
        serviceUuids = emptyList(),
        namePrefix = null,
        companyId = null,
        allowDuplicates = false,
    ),
)

private fun scanStarted(command: StartScanCommand) =
    BleEvent.ScanStarted(command.commandId, command.scanSessionId)
