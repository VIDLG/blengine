package dev.blehub.ffi.runtime

import dev.blehub.AdapterSnapshot
import dev.blehub.AdapterState
import dev.blehub.AdvertisementObservation
import dev.blehub.AuthorizationState
import dev.blehub.BackendCapabilities
import dev.blehub.BackendEventIngressHandle
import dev.blehub.BleBackend
import dev.blehub.BleErrorCategory
import dev.blehub.BleEvent
import dev.blehub.BleEventError
import dev.blehub.BleEventSink
import dev.blehub.BleHub
import dev.blehub.BleHubException
import dev.blehub.BluetoothUuid
import dev.blehub.CharacteristicProperties
import dev.blehub.CommandDispatchException
import dev.blehub.CommandId
import dev.blehub.ConnectCommand
import dev.blehub.ConnectionId
import dev.blehub.ConnectionState
import dev.blehub.ConnectionStateResult
import dev.blehub.DisconnectCommand
import dev.blehub.DiscoverGattCommand
import dev.blehub.EventIngressStatus
import dev.blehub.EventSinkException
import dev.blehub.GattCharacteristic
import dev.blehub.GattCharacteristicId
import dev.blehub.GattDatabase
import dev.blehub.GattDescriptor
import dev.blehub.GattDescriptorId
import dev.blehub.GattService
import dev.blehub.GattServiceId
import dev.blehub.GattSessionId
import dev.blehub.InitializeCommand
import dev.blehub.ManufacturerData
import dev.blehub.NotificationMode
import dev.blehub.NotificationState
import dev.blehub.PeripheralId
import dev.blehub.Platform
import dev.blehub.PlatformErrorDetails
import dev.blehub.ReadCharacteristicCommand
import dev.blehub.ReadRssiCommand
import dev.blehub.ScanOptions
import dev.blehub.ScanSessionId
import dev.blehub.ScanStopReason
import dev.blehub.ScanStopResult
import dev.blehub.ServiceData
import dev.blehub.SetNotificationsCommand
import dev.blehub.StartScanCommand
import dev.blehub.StopScanCommand
import dev.blehub.SubscriptionId
import dev.blehub.WriteCharacteristicCommand
import dev.blehub.blehubContractVersion
import java.util.Collections
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicLong
import java.util.concurrent.atomic.AtomicReference
import java.util.concurrent.locks.ReentrantLock
import kotlin.concurrent.withLock
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertFalse
import kotlin.test.assertIs
import kotlin.test.assertNotEquals
import kotlin.test.assertNull
import kotlin.test.assertTrue

abstract class ProductionUniFfiRuntimeContract {
    @Test
    fun synchronousEventsTraverseTheProductionWeakIngressGraph() {
        val backend = TestBackend(BackendMode.SYNCHRONOUS)
        val sink = RecordingSink()
        val hub = BleHub(backend, sink)
        val command = scanCommand(41uL)

        hub.startScan(command)

        val events = sink.snapshot().events
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
        assertEquals(Platform.TEST, hub.capabilities().platform)
        assertEquals(blehubContractVersion(), hub.capabilities().contractVersion)
        release(hub, backend, sink)
    }

    @Test
    fun completeEventCatalogCrossesTheProductionIngressUnchanged() {
        val backend = TestBackend(BackendMode.NO_EVENT)
        val sink = RecordingSink()
        val hub = BleHub(backend, sink)
        val expected = runtimeEventCatalog()
        try {
            val statuses = expected.map(backend::push)

            assertTrue(statuses.all { it == EventIngressStatus.Delivered })
            assertTrue(eventCatalogMatches(expected, sink.snapshot().events))
        } finally {
            release(hub, backend, sink)
        }
    }

    @Test
    fun kotlinWorkerThreadCompletesTheCallbackCycle() {
        val callerThread = Thread.currentThread().id
        val callbackDone = CountDownLatch(1)
        val backend = TestBackend(BackendMode.KOTLIN_THREAD, callbackDone)
        val sink = RecordingSink()
        val hub = BleHub(backend, sink)

        hub.startScan(scanCommand(2uL))

        assertTrue(callbackDone.await(5, TimeUnit.SECONDS))
        val snapshot = sink.snapshot()
        assertEquals(2, snapshot.events.size)
        val callbackThread = snapshot.callbackThreadIds.distinct().single()
        assertNotEquals(callerThread, callbackThread)
        assertEquals(backend.workerThreadId.get(), callbackThread)
        release(hub, backend, sink)
    }

    @Test
    fun dispatchRejectionAndUnexpectedExceptionProduceNoEvent() {
        val rejectingBackend = TestBackend(BackendMode.REJECT_COMMAND)
        val rejectingSink = RecordingSink()
        val rejectingHub = BleHub(rejectingBackend, rejectingSink)

        val rejected = assertFailsWith<CommandDispatchException.BackendUnavailable> {
            rejectingHub.startScan(scanCommand(4uL))
        }

        assertEquals("declared dispatch rejection", rejected.detail)
        assertTrue(rejectingSink.snapshot().events.isEmpty())
        release(rejectingHub, rejectingBackend, rejectingSink)

        val unexpectedBackend = TestBackend(BackendMode.UNEXPECTED_COMMAND)
        val unexpectedSink = RecordingSink()
        val unexpectedHub = BleHub(unexpectedBackend, unexpectedSink)

        val unexpected = assertFailsWith<CommandDispatchException.FfiContract> {
            unexpectedHub.startScan(scanCommand(5uL))
        }

        assertTrue(unexpected.detail.contains("undeclared command failure"))
        assertTrue(unexpectedSink.snapshot().events.isEmpty())
        release(unexpectedHub, unexpectedBackend, unexpectedSink)
    }

    @Test
    fun capabilitiesAndBindFailuresRollbackWithoutExposingAHalfHub() {
        val lifecycle = LocalSinkLifecycle()

        val typedCapabilities = TestBackend(BackendMode.TYPED_CAPABILITIES_FAILURE)
        val typedCapabilitiesSink = RecordingSink(lifecycle)
        assertFailsWith<BleHubException.BackendAssembly> {
            BleHub(typedCapabilities, typedCapabilitiesSink)
        }
        typedCapabilitiesSink.release()
        assertEquals(1, typedCapabilities.disposeCount.get())
        assertEquals(0, typedCapabilities.bindCount.get())

        val unexpectedCapabilities = TestBackend(BackendMode.UNEXPECTED_CAPABILITIES_FAILURE)
        val unexpectedCapabilitiesSink = RecordingSink(lifecycle)
        val unexpected = assertFailsWith<BleHubException.FfiContract> {
            BleHub(unexpectedCapabilities, unexpectedCapabilitiesSink)
        }
        unexpectedCapabilitiesSink.release()
        assertTrue(unexpected.detail.contains("undeclared capabilities failure"))
        assertEquals(1, unexpectedCapabilities.disposeCount.get())
        assertEquals(0, unexpectedCapabilities.bindCount.get())

        val invalidVersion = TestBackend(BackendMode.INVALID_CONTRACT_VERSION)
        val invalidVersionSink = RecordingSink(lifecycle)
        assertFailsWith<BleHubException.FfiContract> {
            BleHub(invalidVersion, invalidVersionSink)
        }
        invalidVersionSink.release()
        assertEquals(1, invalidVersion.disposeCount.get())
        assertEquals(0, invalidVersion.bindCount.get())

        val typedBind = TestBackend(BackendMode.TYPED_BIND_FAILURE)
        val typedBindSink = RecordingSink(lifecycle)
        assertFailsWith<BleHubException.BackendAssembly> {
            BleHub(typedBind, typedBindSink)
        }
        typedBindSink.release()
        assertEquals(1, typedBind.disposeCount.get())
        assertFalse(typedBind.hasIngress())

        val unexpectedBind = TestBackend(BackendMode.UNEXPECTED_BIND_FAILURE)
        val unexpectedBindSink = RecordingSink(lifecycle)
        val unexpectedBindError = assertFailsWith<BleHubException.FfiContract> {
            BleHub(unexpectedBind, unexpectedBindSink)
        }
        unexpectedBindSink.release()
        assertTrue(unexpectedBindError.detail.contains("undeclared bind failure"))
        assertEquals(1, unexpectedBind.disposeCount.get())
        assertFalse(unexpectedBind.hasIngress())

        assertEquals(LocalSinkLifecycleSnapshot(created = 5, released = 5), lifecycle.snapshot())
    }

    @Test
    fun retainedHandleAndPushDuringBindAreRolledBack() {
        val retained = TestBackend(BackendMode.RETAIN_BEFORE_BIND_FAILURE)
        val retainedSink = RecordingSink()

        assertFailsWith<BleHubException.BackendAssembly> {
            BleHub(retained, retainedSink)
        }
        retainedSink.release()

        assertEquals(1, retained.disposeCount.get())
        assertFalse(retained.hasIngress())

        val pushing = TestBackend(BackendMode.PUSH_DURING_BIND)
        val pushingSink = RecordingSink()
        val error = assertFailsWith<BleHubException.FfiContract> {
            BleHub(pushing, pushingSink)
        }
        pushingSink.release()

        assertTrue(error.detail.contains("while binding ingress"))
        assertEquals(EventIngressStatus.Closed, pushing.bindPushStatus.get())
        assertEquals(1, pushing.disposeCount.get())
        assertFalse(pushing.hasIngress())
    }

    @Test
    fun foreignSinkRejectionStaysInsideTheIngressBoundary() {
        val backend = TestBackend(BackendMode.NO_EVENT)
        val sink = RecordingSink()
        val hub = BleHub(backend, sink)

        sink.setBehavior(SinkBehavior.REJECT)
        val rejected = assertIs<EventIngressStatus.DownstreamFailed>(
            backend.push(scanStarted(scanCommand(6uL))),
        )
        assertTrue(rejected.message.contains("Kotlin test sink rejected the event"))
        assertTrue(sink.snapshot().events.isEmpty())

        sink.setBehavior(SinkBehavior.ACCEPT)
        assertEquals(
            EventIngressStatus.Delivered,
            backend.push(scanStarted(scanCommand(7uL))),
        )
        assertEquals(1, sink.snapshot().events.size)
        release(hub, backend, sink)
    }

    @Test
    fun foreignSinkBlockMakesCloseDrainAndDisposeSeesClosedIngress() {
        val backend = TestBackend(BackendMode.NO_EVENT)
        val sink = RecordingSink()
        val hub = BleHub(backend, sink)
        sink.setBehavior(SinkBehavior.BLOCK)
        val pushDone = CountDownLatch(1)
        val pushStatus = AtomicReference<EventIngressStatus>()
        Thread {
            pushStatus.set(backend.push(scanStarted(scanCommand(8uL))))
            pushDone.countDown()
        }.start()
        assertTrue(sink.waitForBlockedCallback(5_000))

        val disposeDone = CountDownLatch(1)
        val disposeFailure = AtomicReference<Throwable>()
        Thread {
            try {
                hub.dispose()
            } catch (error: Throwable) {
                disposeFailure.set(error)
            } finally {
                disposeDone.countDown()
            }
        }.start()
        assertFalse(disposeDone.await(50, TimeUnit.MILLISECONDS))

        sink.releaseBlockedCallback()
        assertTrue(pushDone.await(5, TimeUnit.SECONDS))
        assertTrue(disposeDone.await(5, TimeUnit.SECONDS))
        assertNull(disposeFailure.get())
        assertEquals(EventIngressStatus.Delivered, pushStatus.get())
        val snapshot = sink.snapshot()
        assertEquals(1, snapshot.events.size)
        assertEquals(0, snapshot.activeCallbacks)
        assertEquals(1, snapshot.maximumActiveCallbacks)
        hub.destroy()
        sink.release()
        assertFalse(backend.hasIngress())

        val disposingBackend = TestBackend(BackendMode.PUSH_DURING_DISPOSE)
        val disposingSink = RecordingSink()
        val disposingHub = BleHub(disposingBackend, disposingSink)
        disposingHub.dispose()
        assertEquals(EventIngressStatus.Closed, disposingBackend.disposePushStatus.get())
        disposingHub.destroy()
        disposingSink.release()
    }

    @Test
    fun typedAndUnexpectedDisposeFailuresReleaseTheRootBeforeReturning() {
        val typedBackend = TestBackend(BackendMode.TYPED_DISPOSE_FAILURE)
        val typedSink = RecordingSink()
        val typedHub = BleHub(typedBackend, typedSink)

        repeat(3) {
            val typed = assertFailsWith<BleHubException.BackendCleanup> {
                typedHub.dispose()
            }
            assertEquals("declared cleanup failure", typed.detail)
            assertFalse(typedBackend.hasIngress())
            assertEquals(1, typedBackend.disposeCount.get())
        }
        typedHub.destroy()
        typedSink.release()

        val unexpectedBackend = TestBackend(BackendMode.UNEXPECTED_DISPOSE_FAILURE)
        val unexpectedSink = RecordingSink()
        val unexpectedHub = BleHub(unexpectedBackend, unexpectedSink)

        repeat(3) {
            val unexpected = assertFailsWith<BleHubException.FfiContract> {
                unexpectedHub.dispose()
            }
            assertTrue(unexpected.detail.contains("undeclared cleanup failure"))
            assertFalse(unexpectedBackend.hasIngress())
            assertEquals(1, unexpectedBackend.disposeCount.get())
        }
        unexpectedHub.destroy()
        unexpectedSink.release()
    }

    @Test
    fun tenThousandProductionHubLifecyclesReturnLocalCountersToBaseline() {
        val lifecycle = LocalSinkLifecycle()
        val disposeCount = AtomicInteger()

        repeat(10_000) {
            val backend = TestBackend(
                mode = BackendMode.NO_EVENT,
                sharedDisposeCount = disposeCount,
            )
            val sink = RecordingSink(lifecycle)
            val hub = BleHub(backend, sink)
            hub.dispose()
            hub.dispose()
            hub.destroy()
            sink.release()
            assertFalse(backend.hasIngress())
        }

        assertEquals(10_000, disposeCount.get())
        assertEquals(
            LocalSinkLifecycleSnapshot(created = 10_000, released = 10_000),
            lifecycle.snapshot(),
        )
    }

    private fun release(hub: BleHub, backend: TestBackend, sink: RecordingSink) {
        hub.dispose()
        hub.dispose()
        hub.destroy()
        sink.release()
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

private class TestBackend(
    private val mode: BackendMode,
    private val callbackDone: CountDownLatch? = null,
    private val sharedDisposeCount: AtomicInteger? = null,
) : BleBackend {
    private val ingress = AtomicReference<BackendEventIngressHandle>()
    val bindCount = AtomicInteger()
    val disposeCount = AtomicInteger()
    val workerThreadId = AtomicLong(-1)
    val bindPushStatus = AtomicReference<EventIngressStatus>()
    val disposePushStatus = AtomicReference<EventIngressStatus>()
    val pushStatuses: MutableList<EventIngressStatus> = Collections.synchronizedList(mutableListOf())

    override fun capabilities(): BackendCapabilities {
        val contractVersion = when (mode) {
            BackendMode.TYPED_CAPABILITIES_FAILURE ->
                throw BleHubException.BackendAssembly("declared capabilities failure")

            BackendMode.UNEXPECTED_CAPABILITIES_FAILURE -> error("undeclared capabilities failure")
            BackendMode.INVALID_CONTRACT_VERSION -> blehubContractVersion() + 1u
            else -> blehubContractVersion()
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
        when (mode) {
            BackendMode.SYNCHRONOUS -> emitSuccessfulScan(command)
            BackendMode.KOTLIN_THREAD -> Thread {
                try {
                    workerThreadId.set(Thread.currentThread().id)
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

private enum class SinkBehavior {
    ACCEPT,
    REJECT,
    BLOCK,
}

private data class RecordingSinkSnapshot(
    val events: List<BleEvent>,
    val activeCallbacks: Int,
    val maximumActiveCallbacks: Int,
    val callbackThreadIds: List<Long>,
)

private data class LocalSinkLifecycleSnapshot(
    val created: Int,
    val released: Int,
)

private class LocalSinkLifecycle {
    private val created = AtomicInteger()
    private val released = AtomicInteger()

    fun recordCreated() {
        created.incrementAndGet()
    }

    fun recordReleased() {
        released.incrementAndGet()
    }

    fun snapshot() = LocalSinkLifecycleSnapshot(
        created = created.get(),
        released = released.get(),
    )
}

private class RecordingSink(
    private val lifecycle: LocalSinkLifecycle = LocalSinkLifecycle(),
) : BleEventSink {
    private val lock = ReentrantLock()
    private val events = mutableListOf<BleEvent>()
    private val callbackThreadIds = mutableListOf<Long>()
    private val released = AtomicBoolean()
    private var behavior = SinkBehavior.ACCEPT
    private var activeCallbacks = 0
    private var maximumActiveCallbacks = 0
    private var blockEntered = CountDownLatch(0)
    private var blockReleased = CountDownLatch(0)

    init {
        lifecycle.recordCreated()
    }

    @Throws(EventSinkException::class)
    override fun onEvent(event: BleEvent) {
        val selectedBehavior = lock.withLock {
            activeCallbacks += 1
            maximumActiveCallbacks = maxOf(maximumActiveCallbacks, activeCallbacks)
            callbackThreadIds += Thread.currentThread().id
            behavior
        }
        try {
            when (selectedBehavior) {
                SinkBehavior.ACCEPT -> lock.withLock { events += event }
                SinkBehavior.REJECT ->
                    throw declaredSinkFailure("Kotlin test sink rejected the event")

                SinkBehavior.BLOCK -> {
                    val (entered, release) = lock.withLock { blockEntered to blockReleased }
                    entered.countDown()
                    try {
                        release.await()
                    } catch (error: InterruptedException) {
                        Thread.currentThread().interrupt()
                        throw declaredSinkFailure("Kotlin test sink callback was interrupted")
                    }
                    lock.withLock { events += event }
                }
            }
        } finally {
            lock.withLock { activeCallbacks -= 1 }
        }
    }

    fun setBehavior(next: SinkBehavior) {
        lock.withLock {
            behavior = next
            if (next == SinkBehavior.BLOCK) {
                blockEntered = CountDownLatch(1)
                blockReleased = CountDownLatch(1)
            }
        }
    }

    fun waitForBlockedCallback(timeoutMillis: Long): Boolean {
        val entered = lock.withLock { blockEntered }
        return entered.await(timeoutMillis, TimeUnit.MILLISECONDS)
    }

    fun releaseBlockedCallback() {
        lock.withLock { blockReleased }.countDown()
    }

    fun snapshot() = lock.withLock {
        RecordingSinkSnapshot(
            events = events.toList(),
            activeCallbacks = activeCallbacks,
            maximumActiveCallbacks = maximumActiveCallbacks,
            callbackThreadIds = callbackThreadIds.toList(),
        )
    }

    fun release() {
        if (released.compareAndSet(false, true)) {
            check(lock.withLock { activeCallbacks == 0 }) { "cannot release a sink with active callbacks" }
            lifecycle.recordReleased()
        }
    }
}

private fun declaredSinkFailure(message: String): EventSinkException {
    val constructor = EventSinkException::class.java.declaredClasses
        .asSequence()
        .filter(EventSinkException::class.java::isAssignableFrom)
        .flatMap { it.declaredConstructors.asSequence() }
        .firstOrNull {
            it.parameterTypes.contentEquals(arrayOf(String::class.java))
        }
        ?: error("EventSinkException has no generated single-string variant")
    return constructor.newInstance(message) as EventSinkException
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

private fun runtimeEventCatalog(): List<BleEvent> {
    val serviceUuid = BluetoothUuid("0000180d-0000-1000-8000-00805f9b34fb")
    val characteristicUuid = BluetoothUuid("00002a37-0000-1000-8000-00805f9b34fb")
    val properties = CharacteristicProperties(
        readable = true,
        writableWithResponse = false,
        writableWithoutResponse = false,
        notifiable = true,
        indicatable = false,
    )
    val advertisement = AdvertisementObservation(
        scanSessionId = ScanSessionId(101uL),
        peripheralId = PeripheralId(201uL),
        localName = "BleHub Fixture",
        advertisedServices = listOf(serviceUuid),
        manufacturerData = listOf(ManufacturerData(0x1234u, byteArrayOf(0x01, 0x02))),
        serviceData = listOf(ServiceData(serviceUuid, byteArrayOf(0x03, 0x04))),
        rssi = -42,
        connectable = null,
    )
    val database = GattDatabase(
        connectionId = ConnectionId(301uL),
        gattSessionId = GattSessionId(401uL),
        services = listOf(
            GattService(
                id = GattServiceId(501uL),
                uuid = serviceUuid,
                primary = true,
                characteristics = listOf(
                    GattCharacteristic(
                        id = GattCharacteristicId(601uL),
                        uuid = characteristicUuid,
                        properties = properties,
                        descriptors = listOf(
                            GattDescriptor(
                                GattDescriptorId(701uL),
                                BluetoothUuid("00002902-0000-1000-8000-00805f9b34fb"),
                            ),
                        ),
                    ),
                ),
            ),
            GattService(
                id = GattServiceId(502uL),
                uuid = serviceUuid,
                primary = false,
                characteristics = listOf(
                    GattCharacteristic(
                        id = GattCharacteristicId(602uL),
                        uuid = characteristicUuid,
                        properties = properties,
                        descriptors = listOf(
                            GattDescriptor(
                                GattDescriptorId(702uL),
                                BluetoothUuid("00002902-0000-1000-8000-00805f9b34fb"),
                            ),
                        ),
                    ),
                ),
            ),
        ),
    )
    val commandId = CommandId(1uL)
    val connectionId = database.connectionId
    val gattSessionId = database.gattSessionId
    val characteristicId = database.services[0].characteristics[0].id
    val subscriptionId = SubscriptionId(801uL)
    val error = BleEventError(
        category = BleErrorCategory.GATT,
        message = "fixture failure",
        platformDetails = PlatformErrorDetails(
            platform = Platform.TEST,
            code = "fixture-code",
            name = "FixtureError",
            message = "fixture platform detail",
        ),
    )

    return listOf(
        BleEvent.AdapterSnapshotChanged(
            commandId,
            AdapterSnapshot(
                state = AdapterState.POWERED_ON,
                scanAuthorization = AuthorizationState.AUTHORIZED,
                connectionAuthorization = AuthorizationState.AUTHORIZED,
                name = "Fixture Adapter",
            ),
        ),
        BleEvent.ScanStarted(commandId, advertisement.scanSessionId),
        BleEvent.AdvertisementObserved(advertisement),
        BleEvent.ScanStopped(
            ScanSessionId(101uL),
            ScanStopResult.Ok(ScanStopReason.Requested(commandId)),
        ),
        BleEvent.ScanStopped(ScanSessionId(102uL), ScanStopResult.Err(error)),
        BleEvent.ConnectionStateChanged(
            commandId,
            connectionId,
            ConnectionStateResult.Ok(ConnectionState.CONNECTED),
        ),
        BleEvent.ConnectionStateChanged(null, connectionId, ConnectionStateResult.Err(error)),
        BleEvent.GattDiscovered(commandId, database),
        BleEvent.CharacteristicRead(
            commandId,
            connectionId,
            gattSessionId,
            characteristicId,
            byteArrayOf(0x10, 0x11),
        ),
        BleEvent.CharacteristicWritten(commandId, connectionId, gattSessionId, characteristicId),
        BleEvent.NotificationStateChanged(
            commandId,
            connectionId,
            gattSessionId,
            subscriptionId,
            characteristicId,
            NotificationState.Enabled(NotificationMode.NOTIFICATION),
        ),
        BleEvent.NotificationReceived(
            connectionId,
            gattSessionId,
            subscriptionId,
            characteristicId,
            byteArrayOf(0x20, 0x21),
        ),
        BleEvent.NotificationStateChanged(
            commandId,
            connectionId,
            gattSessionId,
            subscriptionId,
            characteristicId,
            NotificationState.Disabled,
        ),
        BleEvent.RssiRead(commandId, connectionId, -48),
        BleEvent.CommandCompleted(commandId, null),
        BleEvent.CommandCompleted(CommandId(2uL), error),
        BleEvent.BackendError(error),
    )
}

private fun eventCatalogMatches(expected: List<BleEvent>, actual: List<BleEvent>): Boolean =
    expected.size == actual.size && expected.zip(actual).all { (left, right) -> eventsMatch(left, right) }

private fun eventsMatch(expected: BleEvent, actual: BleEvent): Boolean = when {
    expected is BleEvent.AdvertisementObserved && actual is BleEvent.AdvertisementObserved -> {
        val left = expected.advertisement
        val right = actual.advertisement
        left.scanSessionId == right.scanSessionId &&
                left.peripheralId == right.peripheralId &&
                left.localName == right.localName &&
                left.advertisedServices == right.advertisedServices &&
                left.rssi == right.rssi &&
                left.connectable == right.connectable &&
                left.manufacturerData.size == right.manufacturerData.size &&
                left.manufacturerData.zip(right.manufacturerData).all { (a, b) ->
                    a.companyId == b.companyId && a.payload.contentEquals(b.payload)
                } &&
                left.serviceData.size == right.serviceData.size &&
                left.serviceData.zip(right.serviceData).all { (a, b) ->
                    a.serviceUuid == b.serviceUuid && a.payload.contentEquals(b.payload)
                }
    }

    expected is BleEvent.CharacteristicRead && actual is BleEvent.CharacteristicRead ->
        expected.commandId == actual.commandId &&
                expected.connectionId == actual.connectionId &&
                expected.gattSessionId == actual.gattSessionId &&
                expected.characteristicId == actual.characteristicId &&
                expected.value.contentEquals(actual.value)

    expected is BleEvent.NotificationReceived && actual is BleEvent.NotificationReceived ->
        expected.connectionId == actual.connectionId &&
                expected.gattSessionId == actual.gattSessionId &&
                expected.subscriptionId == actual.subscriptionId &&
                expected.characteristicId == actual.characteristicId &&
                expected.value.contentEquals(actual.value)

    else -> expected == actual
}
