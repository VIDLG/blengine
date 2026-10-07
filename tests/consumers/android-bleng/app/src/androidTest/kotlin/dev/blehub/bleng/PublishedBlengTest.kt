package dev.blehub.bleng

import androidx.test.ext.junit.runners.AndroidJUnit4
import dev.blehub.*
import dev.bleng.Bleng
import dev.bleng.BlengBusyReason
import dev.bleng.BlengException
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.atomic.AtomicInteger
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertIs
import kotlin.test.assertNull
import kotlin.test.assertTrue
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeout
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

private const val DEADLINE_MS = 5_000L
private const val OPERATION_TIMEOUT_MS = 3_000uL
private const val EVENT_READER_TIMEOUT_MS = 86_400_000uL
private val SCAN_OPTIONS = ScanOptions(emptyList(), null, null, false)

@RunWith(AndroidJUnit4::class)
class PublishedBlengTest {
    @Before
    fun requireArt() {
        val vm = System.getProperty("java.vm.name").orEmpty()
        assertTrue(vm.contains("dalvik", true) || vm.contains("art", true), "expected ART, got $vm")
    }

    @Test
    fun invalidTimeoutIsTypedAndNeverDispatches() = blengTest(BlengTestBackend()) { owner, backend, _ ->
        for (invalid in listOf(0uL, 86_400_001uL, ULong.MAX_VALUE)) {
            assertFailsWith<BlengException.InvalidTimeout> { owner.initialize(invalid) }
            assertFailsWith<BlengException.InvalidTimeout> { owner.startScan(SCAN_OPTIONS, invalid) }
        }
        assertEquals(0, backend.initializeCount.get())
        assertEquals(0, backend.scanCount.get())
        assertEquals(backend.snapshot, owner.initialize(OPERATION_TIMEOUT_MS))
        assertEquals(1, backend.initializeCount.get())
    }

    @Test
    fun poweredOffAndDeniedInitializeSnapshotsCrossBothComponentsUnchanged() {
        for (snapshot in listOf(
            AdapterSnapshot(
                AdapterState.POWERED_OFF,
                AuthorizationState.AUTHORIZED,
                AuthorizationState.AUTHORIZED,
                null
            ),
            AdapterSnapshot(AdapterState.POWERED_ON, AuthorizationState.DENIED, AuthorizationState.DENIED, null),
        )) {
            blengTest(BlengTestBackend(snapshot)) { owner, backend, _ ->
                assertEquals(snapshot, owner.initialize(OPERATION_TIMEOUT_MS))
                val command = backend.initializing.await()
                assertEquals(
                    BleEvent.AdapterSnapshotChanged(command.commandId, snapshot),
                    owner.nextEvent(EVENT_READER_TIMEOUT_MS)
                )
                assertEquals(
                    BleEvent.CommandCompleted(command.commandId, null),
                    owner.nextEvent(EVENT_READER_TIMEOUT_MS)
                )
                assertFailsWith<BlengException.Timeout> { owner.nextEvent(1uL) }
                assertEquals(1, backend.initializeCount.get())
            }
        }
    }

    @Test
    fun scanEventsAreExactlyOnceAndShutdownClosesRetainedIngress() =
        blengTest(BlengTestBackend()) { owner, backend, late ->
            owner.initialize(OPERATION_TIMEOUT_MS)
            val initialization = backend.initializing.await()
            assertEquals(
                BleEvent.AdapterSnapshotChanged(initialization.commandId, backend.snapshot),
                owner.nextEvent(EVENT_READER_TIMEOUT_MS)
            )
            assertEquals(
                BleEvent.CommandCompleted(initialization.commandId, null),
                owner.nextEvent(EVENT_READER_TIMEOUT_MS)
            )
            val session = owner.startScan(SCAN_OPTIONS, OPERATION_TIMEOUT_MS)
            val command = backend.scanning.await()
            assertEquals(command.scanSessionId, session)
            assertEquals(
                listOf(
                    BleEvent.ScanStarted(command.commandId, session),
                    BleEvent.CommandCompleted(command.commandId, null),
                ), listOf(owner.nextEvent(EVENT_READER_TIMEOUT_MS), owner.nextEvent(EVENT_READER_TIMEOUT_MS))
            )
            assertFailsWith<BlengException.Timeout> { owner.nextEvent(1uL) }
            assertEquals(1, backend.scanCount.get())
            owner.shutdown()
            owner.shutdown()
            assertEquals(1, backend.disposeCount.get())
            assertNull(backend.ingress)
            assertEquals(EventIngressStatus.Closed, backend.disposeIngressStatus)
            assertEquals(EventIngressStatus.Closed, late.push(BleEvent.ScanStarted(command.commandId, session)))
            assertNull(owner.nextEvent(1uL))
            assertFailsWith<BlengException.Closed> { owner.startScan(SCAN_OPTIONS, OPERATION_TIMEOUT_MS) }
            assertEquals(1, backend.scanCount.get())
        }

    @Test
    fun shutdownRetiresPendingInitializationAndBlockedEventReader() =
        blengTest(BlengTestBackend(completeInitialization = false)) { owner, backend, late ->
            val pending = async { runCatching { owner.initialize(30_000uL) } }
            val command = backend.initializing.await()
            val reader = async(start = CoroutineStart.UNDISPATCHED) {
                owner.nextEvent(EVENT_READER_TIMEOUT_MS)
            }
            listOf(async { owner.shutdown() }, async { owner.shutdown() }).awaitAll()
            assertIs<BlengException.Closed>(pending.await().exceptionOrNull())
            assertNull(reader.await())
            assertEquals(1, backend.initializeCount.get())
            assertEquals(1, backend.disposeCount.get())
            assertEquals(
                EventIngressStatus.Closed,
                late.push(BleEvent.AdapterSnapshotChanged(command.commandId, backend.snapshot))
            )
            assertFailsWith<BlengException.Closed> { owner.initialize(OPERATION_TIMEOUT_MS) }
        }

    @Test
    fun coroutineCancellationRetiresTheWaiterNotTheNativeCommand() =
        blengTest(BlengTestBackend(completeInitialization = false)) { owner, backend, _ ->
            val pending = async { owner.initialize(30_000uL) }
            val command = backend.initializing.await()
            pending.cancelAndJoin()
            assertTrue(pending.isCancelled)
            val busy = assertFailsWith<BlengException.Busy> { owner.initialize(OPERATION_TIMEOUT_MS) }
            assertEquals(BlengBusyReason.OPERATION, busy.reason)
            assertEquals(1, backend.initializeCount.get())
            assertEquals(0, backend.disposeCount.get())
            // Native completion may still arrive after Kotlin frees its Rust future.
            backend.completeInitialize(command)
            assertEquals(
                BleEvent.AdapterSnapshotChanged(command.commandId, backend.snapshot),
                owner.nextEvent(EVENT_READER_TIMEOUT_MS)
            )
            assertEquals(
                BleEvent.CommandCompleted(command.commandId, null),
                owner.nextEvent(EVENT_READER_TIMEOUT_MS)
            )
            val session = owner.startScan(SCAN_OPTIONS, OPERATION_TIMEOUT_MS)
            assertEquals(backend.scanning.await().scanSessionId, session)
            owner.shutdown()
            assertEquals(1, backend.disposeCount.get())
            assertNull(owner.nextEvent(1uL))
        }
}

private fun blengTest(
    backend: BlengTestBackend,
    block: suspend CoroutineScope.(Bleng, BlengTestBackend, BackendEventIngressHandle) -> Unit,
) = runBlocking {
    withTimeout(10_000) {
        val owner = Bleng()
        var late: BackendEventIngressHandle? = null
        try {
            owner.assembleBackend(backend)
            late = checkNotNull(backend.ingress)
            block(owner, backend, late)
        } finally {
            withContext(NonCancellable) {
                try {
                    withTimeout(DEADLINE_MS) {
                        owner.shutdown()
                        owner.shutdown()
                    }
                } finally {
                    owner.destroy()
                    late?.destroy()
                }
            }
            assertEquals(1, backend.disposeCount.get())
            assertNull(backend.ingress)
            assertTrue(backend.deliveryStatuses.all { it == EventIngressStatus.Delivered })
        }
    }
}

private class BlengTestBackend(
    val snapshot: AdapterSnapshot = AdapterSnapshot(
        AdapterState.POWERED_ON, AuthorizationState.AUTHORIZED, AuthorizationState.AUTHORIZED, "ART synthetic backend",
    ),
    private val completeInitialization: Boolean = true,
) : BleBackend {
    @Volatile
    var ingress: BackendEventIngressHandle? = null

    @Volatile
    var disposeIngressStatus: EventIngressStatus? = null
    val initializeCount = AtomicInteger()
    val scanCount = AtomicInteger()
    val disposeCount = AtomicInteger()
    val initializing = CompletableDeferred<InitializeCommand>()
    val scanning = CompletableDeferred<StartScanCommand>()
    val deliveryStatuses = CopyOnWriteArrayList<EventIngressStatus>()

    override fun capabilities() = BackendCapabilities(
        Platform.ANDROID,
        blehubContractVersion(),
        false,
        false,
        false,
        false,
        false,
        false,
    )

    override fun bindEventIngress(ingress: BackendEventIngressHandle) {
        this.ingress = ingress
    }

    override fun initialize(command: InitializeCommand) {
        initializeCount.incrementAndGet()
        if (completeInitialization) completeInitialize(command)
        initializing.complete(command)
    }

    fun completeInitialize(command: InitializeCommand) {
        push(BleEvent.AdapterSnapshotChanged(command.commandId, snapshot))
        push(BleEvent.CommandCompleted(command.commandId, null))
    }

    override fun startScan(command: StartScanCommand) {
        scanCount.incrementAndGet()
        push(BleEvent.ScanStarted(command.commandId, command.scanSessionId))
        push(BleEvent.CommandCompleted(command.commandId, null))
        scanning.complete(command)
    }

    private fun push(event: BleEvent) {
        deliveryStatuses.add(checkNotNull(ingress).push(event))
    }

    override fun dispose() {
        disposeCount.incrementAndGet()
        val retained = ingress
        ingress = null
        disposeIngressStatus = retained?.push(BleEvent.AdapterSnapshotChanged(null, snapshot))
    }

    override fun stopScan(command: StopScanCommand) = unexpected()
    override fun connect(command: ConnectCommand) = unexpected()
    override fun disconnect(command: DisconnectCommand) = unexpected()
    override fun discoverGatt(command: DiscoverGattCommand) = unexpected()
    override fun readCharacteristic(command: ReadCharacteristicCommand) = unexpected()
    override fun writeCharacteristic(command: WriteCharacteristicCommand) = unexpected()
    override fun setNotifications(command: SetNotificationsCommand) = unexpected()
    override fun readRssi(command: ReadRssiCommand) = unexpected()
    private fun unexpected(): Nothing = error("unexpected command in artifact-only BLeng fixture")
}
