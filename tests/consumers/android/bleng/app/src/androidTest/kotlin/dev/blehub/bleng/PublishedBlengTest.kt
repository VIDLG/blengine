package dev.blehub.bleng

import androidx.test.ext.junit.runners.AndroidJUnit4
import dev.blehub.*
import dev.bleng.Bleng
import dev.bleng.BlengBusyReason
import dev.bleng.BlengException
import dev.bleng.ConnectOperation
import dev.bleng.DisableNotificationsOperation
import dev.bleng.DisconnectOperation
import dev.bleng.DiscoverGattOperation
import dev.bleng.EnableNotificationsOperation
import dev.bleng.InitializeOperation
import dev.bleng.NotificationSubscription
import dev.bleng.ReadOperation
import dev.bleng.ReadRssiOperation
import dev.bleng.StartScanOperation
import dev.bleng.StopScanOperation
import dev.bleng.WriteOperation
import dev.bleng_supervisor.ForeignPlatformOwnerSession
import dev.bleng_supervisor.ForeignPlatformOwnerSessionFactory
import dev.bleng_supervisor.ForeignOwnerContext
import dev.bleng_supervisor.ForeignOwnerStart
import dev.bleng_supervisor.ForeignPlatformActionResult
import dev.bleng_supervisor.ForeignPlatformRecoveryAction
import dev.bleng_supervisor.ForeignOperationCommandResult
import dev.bleng_supervisor.ForeignOperationFailureDomain
import dev.bleng_supervisor.ForeignRecoveryPolicyConfig
import dev.bleng_supervisor.ForeignResourceCount
import dev.bleng_supervisor.ForeignScanAdvertisement
import dev.bleng_supervisor.ForeignScanDeliverySnapshot
import dev.bleng_supervisor.ForeignScanEnd
import dev.bleng_supervisor.ForeignScanEndReason
import dev.bleng_supervisor.ForeignScanEventEnvelope
import dev.bleng_supervisor.ForeignScanObservation
import dev.bleng_supervisor.ForeignScanOptions
import dev.bleng_supervisor.ScanEvent
import dev.bleng_supervisor.SessionEventProjection
import dev.bleng_supervisor.SupervisedScan
import dev.bleng_supervisor.SupervisedScanStop
import dev.bleng_supervisor.ForeignSupervisorConfig
import dev.bleng_supervisor.Supervisor
import dev.bleng_supervisor.SupervisorFfiException
import dev.bleng_supervisor.SupervisorOperationException
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.atomic.AtomicInteger
import kotlin.test.assertContentEquals
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertFailsWith
import kotlin.test.assertIs
import kotlin.test.assertNull
import kotlin.test.assertTrue
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.Deferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.TimeoutCancellationException
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.cancel
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeout
import kotlinx.coroutines.yield
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

private const val DEADLINE_MS = 5_000L
private const val OPERATION_TIMEOUT_MS = 3_000uL
private const val EVENT_READER_TIMEOUT_MS = 86_400_000uL
private val SCAN_OPTIONS = ScanOptions(
    serviceUuids = emptyList(),
    namePrefix = null,
    exactName = null,
    companyId = null,
    minRssi = null,
    manufacturerData = null,
    android = null,
    allowDuplicates = false,
)

// The exported scan view takes foreign-representable options; it canonicalizes to the same
// `ScanOptions` the typed BLeng surface receives, so `SCAN_OPTIONS` stays the expected command.
private val FOREIGN_SCAN_OPTIONS = ForeignScanOptions(
    serviceUuids = emptyList(),
    namePrefix = null,
    exactName = null,
    companyId = null,
    minRssi = null,
    manufacturerData = null,
    android = null,
    allowDuplicates = false,
)
private val PERIPHERAL = PeripheralId(201uL)
private val CHARACTERISTIC = GattCharacteristicId(601uL)
private val READ_VALUE = byteArrayOf(0, 0x7f, 0x80.toByte(), 0xff.toByte())

@RunWith(AndroidJUnit4::class)
class PublishedBlengTest {
    @Before
    fun requireArt() {
        val vm = System.getProperty("java.vm.name").orEmpty()
        assertTrue(vm.contains("dalvik", true) || vm.contains("art", true), "expected ART, got $vm")
    }

    @Test
    fun supervisorFfiSurfaceIsPublished() {
        assertEquals("dev.bleng_supervisor.Supervisor", Supervisor::class.java.name)
        assertEquals(
            "dev.bleng_supervisor.ForeignPlatformOwnerSession",
            ForeignPlatformOwnerSession::class.java.name,
        )
        assertEquals(
            "dev.bleng_supervisor.ForeignPlatformOwnerSessionFactory",
            ForeignPlatformOwnerSessionFactory::class.java.name,
        )
        assertEquals(
            "dev.bleng_supervisor.ForeignOwnerContext",
            ForeignOwnerContext::class.java.name,
        )
        assertEquals(
            "dev.bleng_supervisor.ForeignResourceCount",
            ForeignResourceCount::class.java.name,
        )
    }

    @Test
    fun supervisorReturnsAllTenTypedOperationsThroughThePublishedArtifact() =
        supervisorTest(BlengTestBackend()) { supervisor, platform, initialized, releases ->
            val backend = platform.backend()
            val context = initialized.context
            val operationIds = mutableListOf(CommandId(initialized.initializationCommandId))
            val options = ScanOptions(
                serviceUuids = listOf(BluetoothUuid("0000180d-0000-1000-8000-00805f9b34fb")),
                namePrefix = "supervisor-fixture",
                exactName = null,
                companyId = 0x1234u.toUShort(),
                minRssi = null,
                manufacturerData = null,
                android = null,
                allowDuplicates = true,
            )
            for (stale in listOf(
                context.copy(ownerId = context.ownerId + "-stale"),
                context.copy(backendId = context.backendId + "-stale"),
                context.copy(ownerGeneration = context.ownerGeneration + 1uL),
                context.copy(recoveryAttempt = context.recoveryAttempt + 1uL),
            )) {
                assertFailsWith<SupervisorOperationException.StaleContext> {
                    supervisor.beginStartScan(stale, options).also { releases.add(it::destroy) }
                }
            }
            assertFailsWith<SupervisorOperationException.InvalidContext> {
                supervisor.beginStartScan(context.copy(ownerGeneration = 0uL), options)
                    .also { releases.add(it::destroy) }
            }
            assertEquals(0, backend.scanCount.get())

            val start: StartScanOperation = supervisor.beginStartScan(context, options)
                .also { releases.add(it::destroy); operationIds.add(it.commandId()) }
            val scan: ScanSessionId = start.wait(OPERATION_TIMEOUT_MS)
            assertEquals(StartScanCommand(start.commandId(), scan, options), backend.scanning.await())
            assertFailsWith<BlengException.OperationResultConsumed> { start.wait(OPERATION_TIMEOUT_MS) }
            val advertisement = AdvertisementObservation(
                scanSessionId = scan,
                peripheralId = PERIPHERAL,
                localName = "supervisor-fixture-心率",
                advertisedServices = options.serviceUuids,
                manufacturerData = listOf(ManufacturerData(0x1234u.toUShort(), READ_VALUE)),
                serviceData = listOf(ServiceData(options.serviceUuids.single(), byteArrayOf(0xff.toByte(), 0, 0x42))),
                rssi = (-73).toShort(),
                connectable = true,
            )
            assertEquals(
                EventIngressStatus.Delivered,
                checkNotNull(backend.ingress).push(BleEvent.AdvertisementObserved(advertisement)),
            )
            val stop: StopScanOperation = supervisor.beginStopScan(context, scan)
                .also { releases.add(it::destroy); operationIds.add(it.commandId()) }
            val stopped: Unit = stop.wait(OPERATION_TIMEOUT_MS)
            assertEquals(Unit, stopped)
            assertEquals(StopScanCommand(stop.commandId(), scan), backend.stopping.await())
            assertFailsWith<BlengException.OperationResultConsumed> { stop.wait(OPERATION_TIMEOUT_MS) }
            // Fence the existing reader before shutdown revokes further projection.
            platform.awaitEvent(
                BleEvent.ScanStopped(
                    scan,
                    ScanStopResult.Ok(ScanStopReason.Requested(stop.commandId()))
                )
            )

            val connect: ConnectOperation = supervisor.beginConnect(context, PERIPHERAL)
                .also { releases.add(it::destroy); operationIds.add(it.commandId()) }
            val connection: ConnectionId = connect.wait(OPERATION_TIMEOUT_MS)
            assertEquals(ConnectCommand(connect.commandId(), connection, PERIPHERAL, false), backend.connecting.await())
            assertFailsWith<BlengException.OperationResultConsumed> { connect.wait(OPERATION_TIMEOUT_MS) }
            val discover: DiscoverGattOperation = supervisor.beginDiscoverGatt(context, connection)
                .also { releases.add(it::destroy); operationIds.add(it.commandId()) }
            val database: GattDatabase = discover.wait(OPERATION_TIMEOUT_MS)
            val session = database.gattSessionId
            assertEquals(backend.database, database)
            assertEquals(connection, database.connectionId)
            assertEquals(CHARACTERISTIC, database.services.single().characteristics.single().id)
            assertEquals(DiscoverGattCommand(discover.commandId(), connection, session), backend.discovering.await())
            assertFailsWith<BlengException.OperationResultConsumed> { discover.wait(OPERATION_TIMEOUT_MS) }

            val read: ReadOperation = supervisor.beginReadCharacteristic(context, connection, session, CHARACTERISTIC)
                .also { releases.add(it::destroy); operationIds.add(it.commandId()) }
            val bytes: ByteArray = read.wait(OPERATION_TIMEOUT_MS)
            assertContentEquals(READ_VALUE, bytes)
            assertEquals(
                ReadCharacteristicCommand(read.commandId(), connection, session, CHARACTERISTIC),
                backend.reading.receive(),
            )
            assertFailsWith<BlengException.OperationResultConsumed> { read.wait(OPERATION_TIMEOUT_MS) }
            val completed = assertFailsWith<BlengException.OperationAlreadyCompleted> { read.cancel() }
            assertEquals(read.commandId(), completed.commandId)

            val payload = byteArrayOf(0xff.toByte(), 0, 0x80.toByte(), 0x42)
            val write: WriteOperation = supervisor.beginWriteCharacteristic(
                context, connection, session, CHARACTERISTIC, payload, WriteType.WITH_RESPONSE,
            ).also { releases.add(it::destroy); operationIds.add(it.commandId()) }
            val written: Unit = write.wait(OPERATION_TIMEOUT_MS)
            assertEquals(Unit, written)
            val writeCommand = backend.writing.await()
            assertEquals(write.commandId(), writeCommand.commandId)
            assertEquals(connection, writeCommand.connectionId)
            assertEquals(session, writeCommand.gattSessionId)
            assertEquals(CHARACTERISTIC, writeCommand.characteristicId)
            assertContentEquals(payload, writeCommand.value)
            assertEquals(WriteType.WITH_RESPONSE, writeCommand.writeType)
            assertFailsWith<BlengException.OperationResultConsumed> { write.wait(OPERATION_TIMEOUT_MS) }

            val enable: EnableNotificationsOperation = supervisor.beginEnableNotifications(
                context, connection, session, CHARACTERISTIC, NotificationModeRequest.PLATFORM_DEFAULT,
            ).also { releases.add(it::destroy); operationIds.add(it.commandId()) }
            val receipt: NotificationSubscription = enable.wait(OPERATION_TIMEOUT_MS)
            // The actual mode is part of the enable receipt, not a later event lookup.
            assertEquals(NotificationMode.INDICATION, receipt.actualMode)
            val subscription: SubscriptionId = receipt.subscription
            assertEquals(
                SetNotificationsCommand(
                    enable.commandId(), connection, session, subscription, CHARACTERISTIC,
                    NotificationAction.Enable(NotificationModeRequest.PLATFORM_DEFAULT),
                ),
                backend.notifications.receive(),
            )
            assertFailsWith<BlengException.OperationResultConsumed> { enable.wait(OPERATION_TIMEOUT_MS) }
            val disable: DisableNotificationsOperation = supervisor.beginDisableNotifications(
                context, connection, session, subscription,
            ).also { releases.add(it::destroy); operationIds.add(it.commandId()) }
            val disabled: Unit = disable.wait(OPERATION_TIMEOUT_MS)
            assertEquals(Unit, disabled)
            assertEquals(
                SetNotificationsCommand(
                    disable.commandId(), connection, session, subscription, CHARACTERISTIC, NotificationAction.Disable,
                ),
                backend.notifications.receive(),
            )
            assertFailsWith<BlengException.OperationResultConsumed> { disable.wait(OPERATION_TIMEOUT_MS) }

            val readRssi: ReadRssiOperation = supervisor.beginReadRssi(context, connection)
                .also { releases.add(it::destroy); operationIds.add(it.commandId()) }
            val rssi: Short = readRssi.wait(OPERATION_TIMEOUT_MS)
            assertEquals((-61).toShort(), rssi)
            assertEquals(ReadRssiCommand(readRssi.commandId(), connection), backend.readingRssi.await())
            assertFailsWith<BlengException.OperationResultConsumed> { readRssi.wait(OPERATION_TIMEOUT_MS) }
            val disconnect: DisconnectOperation = supervisor.beginDisconnect(context, connection)
                .also { releases.add(it::destroy); operationIds.add(it.commandId()) }
            val disconnected: Unit = disconnect.wait(OPERATION_TIMEOUT_MS)
            assertEquals(Unit, disconnected)
            assertEquals(DisconnectCommand(disconnect.commandId(), connection), backend.disconnecting.await())
            assertFailsWith<BlengException.OperationResultConsumed> { disconnect.wait(OPERATION_TIMEOUT_MS) }

            assertEquals((1uL..11uL).toList(), operationIds.map { it.value }, "rejections must not consume command IDs")
            // Handle completion does not prove the public reader consumed its terminal.
            // Observe the final terminal before shutdown, which closes rather than flushes the FIFO.
            platform.awaitEvent(BleEvent.CommandCompleted(disconnect.commandId(), null))
            assertEquals(
                SupervisorTestSession.resourceNames.map { ForeignResourceCount(it, 0uL) },
                supervisor.shutdown()
            )
            assertNull(supervisor.activeContext())
            assertFailsWith<SupervisorOperationException.NoActiveSession> {
                supervisor.beginStartScan(context, options).also { releases.add(it::destroy) }
            }
            assertFailsWith<BlengException.OperationResultConsumed> { read.wait(OPERATION_TIMEOUT_MS) }
            assertEquals(
                operationIds, listOf(
                    CommandId(initialized.initializationCommandId),
                    start.commandId(),
                    stop.commandId(),
                    connect.commandId(),
                    discover.commandId(),
                    read.commandId(),
                    write.commandId(),
                    enable.commandId(),
                    disable.commandId(),
                    readRssi.commandId(),
                    disconnect.commandId(),
                )
            )
            val events = platform.drainedEvents()
            assertEquals(23, events.size)
            assertEquals(1, platform.readerStartCount.get())
            val envelopes = platform.drainedScanEnvelopes()
            assertEquals(listOf(context, context, context), envelopes.map { it.sourceContext })
            val rawStarted = events.filterIsInstance<BleEvent.ScanStarted>().single()
            assertEquals(ScanEvent.Started(rawStarted.commandId, rawStarted.scanId), envelopes[0].event)
            val rawAdvertisement = events.filterIsInstance<BleEvent.AdvertisementObserved>().single().advertisement
            val projectedAdvertisement = assertIs<ScanEvent.Advertisement>(envelopes[1].event).advertisement
            for (actual in listOf(rawAdvertisement, projectedAdvertisement)) {
                // Generated data-class equality does not compare nested ByteArray payloads by content.
                assertEquals(
                    advertisement.copy(manufacturerData = emptyList(), serviceData = emptyList()),
                    actual.copy(manufacturerData = emptyList(), serviceData = emptyList()),
                )
                assertEquals(
                    advertisement.manufacturerData.map { it.companyId },
                    actual.manufacturerData.map { it.companyId })
                advertisement.manufacturerData.zip(actual.manufacturerData).forEach { (expected, observed) ->
                    assertContentEquals(expected.payload, observed.payload)
                }
                assertEquals(
                    advertisement.serviceData.map { it.serviceUuid },
                    actual.serviceData.map { it.serviceUuid })
                advertisement.serviceData.zip(actual.serviceData).forEach { (expected, observed) ->
                    assertContentEquals(expected.payload, observed.payload)
                }
            }
            val rawStopped = events.filterIsInstance<BleEvent.ScanStopped>().single()
            assertEquals(ScanEvent.Stopped(rawStopped.scanId, rawStopped.result), envelopes[2].event)
            // Already dispatched envelopes remain evidence; the retained endpoint cannot publish again.
            platform.assertScanProjectionRevoked()
            assertEquals(
                operationIds.map { BleEvent.CommandCompleted(it, null) },
                events.filterIsInstance<BleEvent.CommandCompleted>(),
            )
            val domains = events.filterNot { it is BleEvent.CommandCompleted || it is BleEvent.AdvertisementObserved }
            assertEquals(BleEvent.AdapterSnapshotChanged(operationIds.first(), backend.snapshot), domains[0])
            assertEquals(BleEvent.ScanStarted(start.commandId(), scan), domains[1])
            assertEquals(
                BleEvent.ScanStopped(scan, ScanStopResult.Ok(ScanStopReason.Requested(stop.commandId()))), domains[2],
            )
            assertEquals(
                BleEvent.ConnectionStateChanged(
                    connect.commandId(), connection, ConnectionStateResult.Ok(ConnectionState.CONNECTED),
                ),
                domains[3],
            )
            assertEquals(BleEvent.GattDiscovered(discover.commandId(), database), domains[4])
            val readEvent = assertIs<BleEvent.CharacteristicRead>(domains[5])
            assertEquals(read.commandId(), readEvent.commandId)
            assertEquals(connection, readEvent.connectionId)
            assertEquals(session, readEvent.gattSessionId)
            assertEquals(CHARACTERISTIC, readEvent.characteristicId)
            assertContentEquals(READ_VALUE, readEvent.value)
            assertEquals(
                BleEvent.CharacteristicWritten(write.commandId(), connection, session, CHARACTERISTIC),
                domains[6]
            )
            assertEquals(
                BleEvent.NotificationStateChanged(
                    enable.commandId(), connection, session, subscription, CHARACTERISTIC,
                    NotificationState.Enabled(NotificationMode.INDICATION),
                ),
                domains[7],
            )
            assertEquals(
                BleEvent.NotificationStateChanged(
                    disable.commandId(), connection, session, subscription, CHARACTERISTIC, NotificationState.Disabled,
                ),
                domains[8],
            )
            assertEquals(BleEvent.RssiRead(readRssi.commandId(), connection, -61), domains[9])
            assertEquals(
                BleEvent.ConnectionStateChanged(
                    disconnect.commandId(), connection, ConnectionStateResult.Ok(ConnectionState.DISCONNECTED),
                ),
                domains[10],
            )
            assertEquals(0, backend.cancelCount.get())
        }

    @Test
    fun supervisorScanProxyPreservesNativePayloadAndDeliveryAcrossWaitAndPullErrors() =
        supervisorTest(BlengTestBackend(advertiseBeforeScanStart = true)) { supervisor, platform, initialized, releases ->
            val backend = platform.backend()
            val context = initialized.context
            val beginScan = supervisor.beginScanView(context, FOREIGN_SCAN_OPTIONS)
            assertNull(beginScan.failure)
            val scan: SupervisedScan = requireNotNull(beginScan.scan).also { releases.add(it::destroy) }
            val command = backend.scanning.await()
            assertEquals(SCAN_OPTIONS, command.options)
            // This is the sole reader's receipt, not a start-wait-as-FIFO-flush assumption.
            platform.awaitEvent(BleEvent.CommandCompleted(command.commandId, null))
            val advertisements = supervisedScanAdvertisements(command.scanSessionId)
            val queued: ForeignScanDeliverySnapshot = scan.observationSnapshot()
            assertEquals(2uL, queued.queuedAdvertisements)
            assertTrue(queued.queuedBytes > 0uL)
            assertEquals(0uL, queued.queuedStopBytes)
            assertFalse(queued.pullPending)
            for (invalid in listOf(0.0, 86_400_001.0, ULong.MAX_VALUE.toDouble())) {
                val result: ForeignOperationCommandResult = scan.startResult(invalid)
                assertEquals(ForeignOperationFailureDomain.INPUT, result.failure?.domain)
                assertEquals("invalid_timeout", result.failure?.code)
                assertEquals(queued, scan.observationSnapshot())
            }
            assertNull(scan.startResult(OPERATION_TIMEOUT_MS.toDouble()).failure)
            val duplicate = requireNotNull(scan.startResult(OPERATION_TIMEOUT_MS.toDouble()).failure)
            assertEquals("bleng.operation_result_consumed", duplicate.code)
            val cancellation = requireNotNull(scan.cancelStartResult().failure)
            assertEquals("bleng.operation_already_completed", cancellation.code)
            val busyBegin = supervisor.beginScanView(context, FOREIGN_SCAN_OPTIONS)
            assertNull(busyBegin.scan)
            assertEquals("delivery.busy", busyBegin.failure?.code)
            for (invalid in listOf(0.0, ULong.MAX_VALUE.toDouble())) {
                val observation = scan.nextObservation(invalid)
                assertEquals(ForeignOperationFailureDomain.INPUT, observation.failure?.domain)
                assertEquals("invalid_timeout", observation.failure?.code)
                assertNull(observation.advertisement)
                assertNull(observation.ended)
            }
            assertEquals(queued, scan.observationSnapshot(), "invalid/duplicate waits must not detach delivery")
            for (advertisement in advertisements) {
                assertScanAdvertisement(advertisement, nextScanAdvertisement(scan))
            }
            assertEmptyScanDelivery(scan)
            val timeout = scan.nextObservation(1.0)
            assertEquals("bleng.timeout", timeout.failure?.code)
            assertNull(timeout.advertisement)
            assertNull(timeout.ended)
            assertEmptyScanDelivery(scan)
            assertEquals(1, backend.scanCount.get())
            assertEquals(0, backend.cancelCount.get())
            assertFalse(backend.stopping.isCompleted, "observation timeout must not dispatch BLE teardown")

            val late = advertisements.first().copy(localName = "", rssi = 0.toShort(), connectable = false)
            assertEquals(
                EventIngressStatus.Delivered,
                checkNotNull(backend.ingress).push(BleEvent.AdvertisementObserved(late)),
            )
            assertScanAdvertisement(late, nextScanAdvertisement(scan))
            assertEmptyScanDelivery(scan)

            // A live scan with an empty queue is the only state where a pull can park; the retained
            // terminal below is deliberately readable more than once.
            val pending = async(start = CoroutineStart.UNDISPATCHED) {
                runCatching { scan.nextObservation(OPERATION_TIMEOUT_MS.toDouble()) }
            }
            // Kotlin future creation alone does not prove Rust installed the pending pull.
            withTimeout(DEADLINE_MS) {
                while (!scan.observationSnapshot().pullPending) {
                    assertFalse(pending.isCompleted, "pull ended before pending-state receipt")
                    yield()
                }
            }
            val busy = scan.nextObservation(OPERATION_TIMEOUT_MS.toDouble())
            assertEquals(ForeignOperationFailureDomain.DELIVERY, busy.failure?.domain)
            assertEquals("delivery.pull_busy", busy.failure?.code)
            assertNull(busy.advertisement)
            assertNull(busy.ended)
            assertTrue(scan.observationSnapshot().pullPending)

            val stopBegin = scan.beginStopView()
            assertNull(stopBegin.failure)
            val stop: SupervisedScanStop = requireNotNull(stopBegin.operation).also { releases.add(it::destroy) }
            assertNull(stop.waitResult(OPERATION_TIMEOUT_MS.toDouble()).failure)
            val stopCommand = backend.stopping.await()
            assertEquals(command.scanSessionId, stopCommand.scanSessionId)
            assertEquals(
                "bleng.operation_result_consumed",
                stop.waitResult(OPERATION_TIMEOUT_MS.toDouble()).failure?.code,
            )
            assertEquals("bleng.operation_already_completed", stop.cancelResult().failure?.code)
            val stopResult = ScanStopResult.Ok(ScanStopReason.Requested(stopCommand.commandId))
            platform.awaitEvent(BleEvent.CommandCompleted(stopCommand.commandId, null))
            // The real terminal wakes the parked pull; the busy refusal above did not steal it.
            val terminal = pending.await().getOrThrow()
            assertNull(terminal.failure)
            assertNull(terminal.advertisement)
            assertEquals(ForeignScanEnd(ForeignScanEndReason.REQUESTED, null), terminal.ended)
            assertEmptyScanDelivery(scan)
            // A scan's end is retained, not consumed: a consumer polling on its own schedule still
            // learns it, and a second read does not invent a different answer.
            assertEquals(terminal, scan.nextObservation(OPERATION_TIMEOUT_MS.toDouble()))
            assertEmptyScanDelivery(scan)

            assertEquals(
                SupervisorTestSession.resourceNames.map { ForeignResourceCount(it, 0uL) },
                supervisor.shutdown()
            )
            assertEmptyScanDelivery(scan)
            assertNull(supervisor.activeContext())
            for (detach in listOf(false, true)) {
                if (detach) scan.detach()
                val observation = scan.nextObservation(OPERATION_TIMEOUT_MS.toDouble())
                assertEquals(ForeignScanEndReason.OWNER_CLOSED, observation.ended?.reason)
                assertNull(observation.failure)
                assertNull(observation.advertisement)
                assertEmptyScanDelivery(scan)
            }
            val submission = supervisor.beginScanView(context, FOREIGN_SCAN_OPTIONS)
            assertNull(submission.scan)
            assertEquals(ForeignOperationFailureDomain.LIFECYCLE, submission.failure?.domain)
            assertEquals("supervisor.no_active_session", submission.failure?.code)

            val operationIds =
                listOf(CommandId(initialized.initializationCommandId), command.commandId, stopCommand.commandId)
            assertEquals(listOf(1uL, 2uL, 3uL), operationIds.map { it.value })
            val events = platform.drainedEvents()
            assertEquals(9, events.size)
            assertEquals(
                operationIds.map { BleEvent.CommandCompleted(it, null) },
                events.filterIsInstance<BleEvent.CommandCompleted>()
            )
            val rawAdvertisements = events.filterIsInstance<BleEvent.AdvertisementObserved>()
            assertEquals(3, rawAdvertisements.size)
            (advertisements + late).zip(rawAdvertisements).forEach { (expected, actual) ->
                assertScanAdvertisement(expected, actual.advertisement)
            }
            assertEquals(
                listOf(
                    BleEvent.AdapterSnapshotChanged(operationIds[0], initialized.adapterSnapshot),
                    BleEvent.CommandCompleted(operationIds[0], null),
                    BleEvent.ScanStarted(command.commandId, command.scanSessionId),
                    BleEvent.CommandCompleted(command.commandId, null),
                    BleEvent.ScanStopped(command.scanSessionId, stopResult),
                    BleEvent.CommandCompleted(stopCommand.commandId, null),
                ),
                events.filterNot { it is BleEvent.AdvertisementObserved },
            )
            val envelopes = platform.drainedScanEnvelopes()
            assertEquals(List(5) { context }, envelopes.map { it.sourceContext })
            advertisements.zip(envelopes.take(2)).forEach { (expected, envelope) ->
                assertScanAdvertisement(expected, assertIs<ScanEvent.Advertisement>(envelope.event).advertisement)
            }
            assertEquals(ScanEvent.Started(command.commandId, command.scanSessionId), envelopes[2].event)
            assertScanAdvertisement(late, assertIs<ScanEvent.Advertisement>(envelopes[3].event).advertisement)
            assertEquals(ScanEvent.Stopped(command.scanSessionId, stopResult), envelopes[4].event)
            assertEquals(1, platform.readerStartCount.get())
            platform.assertScanProjectionRevoked()
            assertEquals(0, backend.cancelCount.get())
        }

    @Test
    fun supervisorScanProxyKeepsOriginalStartAndStopWaitsIndependentFromCancelReceipts() =
        supervisorTest(
            BlengTestBackend(
                completeScanStart = false, completeScanStop = false, gateCancellation = true, rejectCancellation = true,
            )
        ) { supervisor, platform, initialized, releases ->
            val backend = platform.backend()
            val beginScan = supervisor.beginScanView(initialized.context, FOREIGN_SCAN_OPTIONS)
            assertNull(beginScan.failure)
            val scan: SupervisedScan = requireNotNull(beginScan.scan).also { releases.add(it::destroy) }
            val startCommand = backend.scanning.await()
            val earlyStop = scan.beginStopView()
            assertNull(earlyStop.operation)
            assertEquals("scan.start_not_observed", earlyStop.failure?.code)
            val starting = async(start = CoroutineStart.UNDISPATCHED) {
                runCatching { scan.startResult(OPERATION_TIMEOUT_MS.toDouble()) }
            }
            val cancelling = async(start = CoroutineStart.UNDISPATCHED) {
                runCatching { scan.cancelStartResult() }
            }
            assertEquals(startCommand.commandId, backend.cancelling.await().targetCommandId)
            assertFalse(starting.isCompleted)
            assertFalse(cancelling.isCompleted, "cancelStart must await the native cancellation receipt")
            backend.releaseCancellation.complete(Unit)
            val rejected = requireNotNull(cancelling.await().getOrThrow().failure)
            assertEquals("bleng.operation_not_cancellable", rejected.code)
            assertFalse(starting.isCompleted, "rejected cancellation must not consume or settle start wait")
            backend.completeStartScan(startCommand)
            assertNull(starting.await().getOrThrow().failure)
            platform.awaitEvent(BleEvent.CommandCompleted(startCommand.commandId, null))
            assertEmptyScanDelivery(scan)

            val stopBegin = scan.beginStopView()
            assertNull(stopBegin.failure)
            val stop: SupervisedScanStop = requireNotNull(stopBegin.operation).also { releases.add(it::destroy) }
            val stopCommand = backend.stopping.await()
            assertEquals(startCommand.scanSessionId, stopCommand.scanSessionId)
            val zero = stop.waitResult(0.0)
            assertEquals(ForeignOperationFailureDomain.INPUT, zero.failure?.domain)
            assertEquals("invalid_timeout", zero.failure?.code)
            val stopping = async(start = CoroutineStart.UNDISPATCHED) {
                runCatching { stop.waitResult(OPERATION_TIMEOUT_MS.toDouble()) }
            }
            // Installed teardown is not cancellable; this original typed result must not be scan-error wrapped.
            assertEquals("bleng.operation_not_cancellable", stop.cancelResult().failure?.code)
            assertFalse(stopping.isCompleted, "cancel rejection must leave the stop result pending")
            assertEquals(1, backend.cancelCount.get(), "stop cancellation must not dispatch to the backend")
            backend.completeStopScan(stopCommand)
            assertNull(stopping.await().getOrThrow().failure)
            assertEquals(
                "bleng.operation_result_consumed",
                stop.waitResult(OPERATION_TIMEOUT_MS.toDouble()).failure?.code,
            )
            assertEquals("bleng.operation_already_completed", stop.cancelResult().failure?.code)
            platform.awaitEvent(BleEvent.CommandCompleted(stopCommand.commandId, null))
            val stopResult = ScanStopResult.Ok(ScanStopReason.Requested(stopCommand.commandId))
            val terminal = scan.nextObservation(OPERATION_TIMEOUT_MS.toDouble())
            assertNull(terminal.failure)
            assertNull(terminal.advertisement)
            assertEquals(ForeignScanEnd(ForeignScanEndReason.REQUESTED, null), terminal.ended)
            assertEmptyScanDelivery(scan)
            assertEquals(
                SupervisorTestSession.resourceNames.map { ForeignResourceCount(it, 0uL) },
                supervisor.shutdown()
            )
            val events = platform.drainedEvents()
            assertEquals(
                listOf(CommandId(initialized.initializationCommandId), startCommand.commandId, stopCommand.commandId)
                    .map { BleEvent.CommandCompleted(it, null) },
                events.filterIsInstance<BleEvent.CommandCompleted>(),
            )
            assertEquals(6, events.size)
            assertEquals(
                1,
                events.count { it == BleEvent.ScanStarted(startCommand.commandId, startCommand.scanSessionId) })
            assertEquals(1, events.count { it == BleEvent.ScanStopped(startCommand.scanSessionId, stopResult) })
            assertEquals(
                listOf(
                    ForeignScanEventEnvelope(
                        initialized.context,
                        ScanEvent.Started(startCommand.commandId, startCommand.scanSessionId)
                    ),
                    ForeignScanEventEnvelope(
                        initialized.context,
                        ScanEvent.Stopped(startCommand.scanSessionId, stopResult)
                    ),
                ),
                platform.drainedScanEnvelopes(),
            )
            assertEquals(1, platform.readerStartCount.get())
            assertEquals(1, backend.scanCount.get())
            platform.assertScanProjectionRevoked()
        }

    @Test
    fun supervisorTypedConnectKeepsWaitAndCancelReceiptsIndependent() {
        for (reject in listOf(false, true)) {
            supervisorTest(
                BlengTestBackend(completeConnection = false, gateCancellation = true, rejectCancellation = reject),
            ) { supervisor, platform, initialized, releases ->
                val backend = platform.backend()
                val connect: ConnectOperation = supervisor.beginConnect(initialized.context, PERIPHERAL)
                    .also { releases.add(it::destroy) }
                val command = backend.connecting.await()
                assertEquals(connect.commandId(), command.commandId)
                assertEquals(PERIPHERAL, command.peripheralId)
                val waiting = async(start = CoroutineStart.UNDISPATCHED) {
                    runCatching { connect.wait(OPERATION_TIMEOUT_MS) }
                }
                val cancelling = async(start = CoroutineStart.UNDISPATCHED) { runCatching { connect.cancel() } }
                assertEquals(connect.commandId(), backend.cancelling.await().targetCommandId)
                assertFalse(waiting.isCompleted)
                assertFalse(cancelling.isCompleted, "cancel must await the backend receipt")
                backend.releaseCancellation.complete(Unit)
                val receipt = cancelling.await()
                if (reject) {
                    val error = assertIs<BlengException.OperationNotCancellable>(receipt.exceptionOrNull())
                    assertEquals(connect.commandId(), error.commandId)
                    assertFalse(waiting.isCompleted, "rejected cancel must not consume or settle wait")
                    backend.completeConnect(command)
                    val connection: ConnectionId = waiting.await().getOrThrow()
                    assertEquals(command.connectionId, connection)
                } else {
                    val accepted: Unit = receipt.getOrThrow()
                    assertEquals(Unit, accepted)
                    assertIs<BlengException.Cancelled>(waiting.await().exceptionOrNull())
                    backend.completeConnect(command)
                }
                assertFailsWith<BlengException.OperationResultConsumed> { connect.wait(OPERATION_TIMEOUT_MS) }
                assertEquals(1, backend.cancelCount.get())
                platform.awaitEvent(BleEvent.CommandCompleted(command.commandId, null))
                supervisor.shutdown()
                val events = platform.drainedEvents()
                assertEquals(
                    if (reject) 1 else 0,
                    events.count {
                        it == BleEvent.ConnectionStateChanged(
                            command.commandId,
                            command.connectionId,
                            ConnectionStateResult.Ok(ConnectionState.CONNECTED),
                        )
                    },
                )
                assertEquals(1, events.count { it == BleEvent.CommandCompleted(command.commandId, null) })
            }
        }
    }

    @Test
    fun supervisorShutdownClosesPendingTypedConnectWithoutDispatchingCancel() =
        supervisorTest(BlengTestBackend(completeConnection = false)) { supervisor, platform, initialized, releases ->
            val backend = platform.backend()
            val connect: ConnectOperation = supervisor.beginConnect(initialized.context, PERIPHERAL)
                .also { releases.add(it::destroy) }
            val command = backend.connecting.await()
            val waiting = async(start = CoroutineStart.UNDISPATCHED) {
                runCatching { connect.wait(OPERATION_TIMEOUT_MS) }
            }
            assertFalse(waiting.isCompleted)
            assertEquals(
                SupervisorTestSession.resourceNames.map { ForeignResourceCount(it, 0uL) },
                supervisor.shutdown()
            )
            assertIs<BlengException.Closed>(waiting.await().exceptionOrNull())
            assertFailsWith<BlengException.OperationResultConsumed> { connect.wait(OPERATION_TIMEOUT_MS) }
            assertFailsWith<BlengException.Closed> { connect.cancel() }
            assertEquals(command.commandId, connect.commandId())
            assertEquals(0, backend.cancelCount.get())
            assertNull(supervisor.activeContext())
            assertFailsWith<SupervisorOperationException.NoActiveSession> {
                supervisor.beginConnect(initialized.context, PERIPHERAL).also { releases.add(it::destroy) }
            }
            assertTrue(platform.drainedEvents().none {
                it is BleEvent.ConnectionStateChanged && it.connectionId == command.connectionId
            })
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
            assertFailsWith<BlengException.Closed> { owner.beginInitialize() }
            assertFailsWith<BlengException.Closed> { owner.initialize(OPERATION_TIMEOUT_MS) }
        }

    @Test
    fun allElevenGeneratedHandlesReturnTheirTypedOutputs() =
        blengTest(BlengTestBackend()) { owner, backend, _ ->
            val releases = mutableListOf<() -> Unit>()
            val operationIds = mutableListOf<CommandId>()
            try {
                val initialize: InitializeOperation = owner.beginInitialize()
                    .also { releases.add(it::destroy); operationIds.add(it.commandId()) }
                val snapshot: AdapterSnapshot = initialize.wait(OPERATION_TIMEOUT_MS)
                assertEquals(backend.snapshot, snapshot)

                val start: StartScanOperation = owner.beginStartScan(SCAN_OPTIONS)
                    .also { releases.add(it::destroy); operationIds.add(it.commandId()) }
                val scan: ScanSessionId = start.wait(OPERATION_TIMEOUT_MS)
                assertEquals(backend.scanning.await().scanSessionId, scan)
                val stop: StopScanOperation = owner.beginStopScan(scan)
                    .also { releases.add(it::destroy); operationIds.add(it.commandId()) }
                val stopped: Unit = stop.wait(OPERATION_TIMEOUT_MS)
                assertEquals(Unit, stopped)
                assertEquals(scan, backend.stopping.await().scanSessionId)

                val connect: ConnectOperation = owner.beginConnect(PERIPHERAL)
                    .also { releases.add(it::destroy); operationIds.add(it.commandId()) }
                val connection: ConnectionId = connect.wait(OPERATION_TIMEOUT_MS)
                assertEquals(backend.connecting.await().connectionId, connection)
                assertEquals(PERIPHERAL, backend.connecting.await().peripheralId)
                val discover: DiscoverGattOperation = owner.beginDiscoverGatt(connection)
                    .also { releases.add(it::destroy); operationIds.add(it.commandId()) }
                val database: GattDatabase = discover.wait(OPERATION_TIMEOUT_MS)
                assertEquals(backend.database, database)
                assertEquals(connection, database.connectionId)
                val session: GattSessionId = database.gattSessionId
                assertEquals(CHARACTERISTIC, database.services.single().characteristics.single().id)

                val read: ReadOperation = owner.beginReadCharacteristic(
                    connection, session, CHARACTERISTIC,
                ).also { releases.add(it::destroy); operationIds.add(it.commandId()) }
                val bytes: ByteArray = read.wait(OPERATION_TIMEOUT_MS)
                assertContentEquals(READ_VALUE, bytes)
                assertFailsWith<BlengException.OperationResultConsumed> { read.wait(OPERATION_TIMEOUT_MS) }

                val payload = byteArrayOf(0xff.toByte(), 0, 0x80.toByte(), 0x42)
                val write: WriteOperation = owner.beginWriteCharacteristic(
                    connection, session, CHARACTERISTIC, payload, WriteType.WITH_RESPONSE,
                ).also { releases.add(it::destroy); operationIds.add(it.commandId()) }
                val written: Unit = write.wait(OPERATION_TIMEOUT_MS)
                assertEquals(Unit, written)
                val writeCommand = backend.writing.await()
                assertContentEquals(payload, writeCommand.value)
                assertEquals(WriteType.WITH_RESPONSE, writeCommand.writeType)
                assertEquals(connection, writeCommand.connectionId)
                assertEquals(session, writeCommand.gattSessionId)
                assertEquals(CHARACTERISTIC, writeCommand.characteristicId)

                val enable: EnableNotificationsOperation = owner.beginEnableNotifications(
                    connection, session, CHARACTERISTIC, NotificationModeRequest.PLATFORM_DEFAULT,
                ).also { releases.add(it::destroy); operationIds.add(it.commandId()) }
                val receipt: NotificationSubscription = enable.wait(OPERATION_TIMEOUT_MS)
                // The actual mode is part of the enable receipt, not a later event lookup.
                assertEquals(NotificationMode.INDICATION, receipt.actualMode)
                val subscription: SubscriptionId = receipt.subscription
                val enableCommand = backend.notifications.receive()
                assertEquals(enableCommand.subscriptionId, subscription)
                assertEquals(NotificationAction.Enable(NotificationModeRequest.PLATFORM_DEFAULT), enableCommand.action)
                // The receipt carries the actual mode; the event remains the canonical evidence.
                owner.awaitEvent(
                    BleEvent.NotificationStateChanged(
                        enableCommand.commandId, connection, session, subscription, CHARACTERISTIC,
                        NotificationState.Enabled(NotificationMode.INDICATION),
                    )
                )
                val disable: DisableNotificationsOperation = owner.beginDisableNotifications(
                    connection, session, subscription,
                ).also { releases.add(it::destroy); operationIds.add(it.commandId()) }
                val disabled: Unit = disable.wait(OPERATION_TIMEOUT_MS)
                assertEquals(Unit, disabled)
                val disableCommand = backend.notifications.receive()
                assertEquals(subscription, disableCommand.subscriptionId)
                assertEquals(NotificationAction.Disable, disableCommand.action)
                owner.awaitEvent(
                    BleEvent.NotificationStateChanged(
                        disableCommand.commandId, connection, session, subscription, CHARACTERISTIC,
                        NotificationState.Disabled,
                    )
                )

                val readRssi: ReadRssiOperation = owner.beginReadRssi(connection)
                    .also { releases.add(it::destroy); operationIds.add(it.commandId()) }
                val rssi: Short = readRssi.wait(OPERATION_TIMEOUT_MS)
                assertEquals((-61).toShort(), rssi)
                val disconnect: DisconnectOperation = owner.beginDisconnect(connection)
                    .also { releases.add(it::destroy); operationIds.add(it.commandId()) }
                val disconnected: Unit = disconnect.wait(OPERATION_TIMEOUT_MS)
                assertEquals(Unit, disconnected)
                assertEquals(connection, backend.disconnecting.await().connectionId)
                assertEquals(11, operationIds.toSet().size)
                assertEquals(backend.initializing.await().commandId, initialize.commandId())
                assertEquals(backend.scanning.await().commandId, start.commandId())
                assertEquals(backend.stopping.await().commandId, stop.commandId())
                assertEquals(backend.connecting.await().commandId, connect.commandId())
                assertEquals(backend.reading.receive().commandId, read.commandId())
                assertEquals(writeCommand.commandId, write.commandId())
                assertEquals(enableCommand.commandId, enable.commandId())
                assertEquals(disableCommand.commandId, disable.commandId())
                assertEquals(backend.disconnecting.await().commandId, disconnect.commandId())
                owner.shutdown()
                assertEquals(
                    operationIds, listOf(
                        initialize.commandId(), start.commandId(), stop.commandId(), connect.commandId(),
                        discover.commandId(), read.commandId(), write.commandId(), enable.commandId(),
                        disable.commandId(), readRssi.commandId(), disconnect.commandId(),
                    )
                )
                assertEquals(0, backend.cancelCount.get())
            } finally {
                releases.asReversed().forEach { it() }
            }
        }

    @Test
    fun generatedHandleAllowsOnlyOneConcurrentResultConsumer() =
        blengTest(BlengTestBackend(completeInitialization = false)) { owner, backend, _ ->
            val initialize: InitializeOperation = owner.beginInitialize()
            try {
                val command = backend.initializing.await()
                assertFailsWith<BlengException.InvalidTimeout> { initialize.wait(0uL) }
                val first = async(start = CoroutineStart.UNDISPATCHED) {
                    runCatching { initialize.wait(OPERATION_TIMEOUT_MS) }
                }
                val second = async(start = CoroutineStart.UNDISPATCHED) {
                    runCatching { initialize.wait(OPERATION_TIMEOUT_MS) }
                }
                backend.completeInitialize(command)
                val results = listOf(first, second).awaitAll()
                assertEquals(backend.snapshot, results.single { it.isSuccess }.getOrThrow())
                assertIs<BlengException.OperationResultConsumed>(results.single { it.isFailure }.exceptionOrNull())
                assertFailsWith<BlengException.OperationResultConsumed> { initialize.wait(OPERATION_TIMEOUT_MS) }
            } finally {
                initialize.destroy()
            }
        }

    @Test
    fun shutdownRetainsCompletedUnclaimedReadButClosesPendingRead() =
        blengTest(BlengTestBackend(completeReads = false)) { owner, backend, _ ->
            owner.initialize(OPERATION_TIMEOUT_MS)
            val connection = owner.connect(PERIPHERAL, OPERATION_TIMEOUT_MS)
            val database = owner.discoverGatt(connection, OPERATION_TIMEOUT_MS)
            val completed: ReadOperation = owner.beginReadCharacteristic(
                connection, database.gattSessionId, CHARACTERISTIC,
            )
            try {
                val command = backend.reading.receive()
                backend.completeRead(command)
                owner.awaitEvent(BleEvent.CommandCompleted(command.commandId, null))
                // Publication alone does not prove settlement. The following actor-linearized
                // cancel must observe completion without consuming the retained read result.
                val alreadyCompleted = assertFailsWith<BlengException.OperationAlreadyCompleted> { completed.cancel() }
                assertEquals(completed.commandId(), command.commandId)
                assertEquals(completed.commandId(), alreadyCompleted.commandId)
                val pending: ReadOperation = owner.beginReadCharacteristic(
                    connection, database.gattSessionId, CHARACTERISTIC,
                )
                try {
                    backend.reading.receive()
                    owner.shutdown()
                    val bytes: ByteArray = completed.wait(OPERATION_TIMEOUT_MS)
                    assertContentEquals(READ_VALUE, bytes)
                    assertFailsWith<BlengException.OperationResultConsumed> { completed.wait(OPERATION_TIMEOUT_MS) }
                    assertFailsWith<BlengException.Closed> { pending.wait(OPERATION_TIMEOUT_MS) }
                    assertFailsWith<BlengException.OperationResultConsumed> { pending.wait(OPERATION_TIMEOUT_MS) }
                    assertFailsWith<BlengException.Closed> { pending.cancel() }
                    assertEquals(0, backend.cancelCount.get())
                } finally {
                    pending.destroy()
                }
            } finally {
                completed.destroy()
            }
        }

    @Test
    fun connectWaitAndCancelHaveIndependentAcceptedAndRejectedReceipts() {
        for (reject in listOf(false, true)) {
            blengTest(
                BlengTestBackend(
                    completeConnection = false,
                    gateCancellation = true,
                    rejectCancellation = reject
                )
            ) { owner, backend, _ ->
                val scenario = if (reject) "connect cancel rejected" else "connect cancel accepted"
                atStage("$scenario: initialize") { owner.initialize(OPERATION_TIMEOUT_MS) }
                val connect: ConnectOperation = atStage("$scenario: begin connect") {
                    owner.beginConnect(PERIPHERAL)
                }
                try {
                    val command = atStage("$scenario: backend connect dispatch") { backend.connecting.await() }
                    val connected = BleEvent.ConnectionStateChanged(
                        command.commandId,
                        command.connectionId,
                        ConnectionStateResult.Ok(ConnectionState.CONNECTED),
                    )
                    val terminal = BleEvent.CommandCompleted(command.commandId, null)
                    val waiting = async(start = CoroutineStart.UNDISPATCHED) {
                        runCatching {
                            atStage("$scenario: operation wait for ${command.commandId}") {
                                connect.wait(OPERATION_TIMEOUT_MS)
                            }
                        }
                    }
                    val cancelling = async(start = CoroutineStart.UNDISPATCHED) {
                        runCatching {
                            atStage("$scenario: cancel receipt for ${command.commandId}") { connect.cancel() }
                        }
                    }
                    val request = atStage("$scenario: backend cancellation dispatch") { backend.cancelling.await() }
                    assertEquals(connect.commandId(), command.commandId)
                    assertEquals(connect.commandId(), request.targetCommandId)
                    assertFalse(cancelling.isCompleted, "cancel must await the backend receipt")
                    assertFalse(waiting.isCompleted, "a pending receipt cannot settle wait")
                    backend.releaseCancellation.complete(Unit)
                    val receipt = atStage("$scenario: await cancel receipt") { cancelling.await() }
                    if (reject) {
                        val error = assertIs<BlengException.OperationNotCancellable>(
                            receipt.exceptionOrNull(), "$scenario: expected rejected receipt",
                        )
                        assertEquals(command.commandId, error.commandId)
                        assertFalse(waiting.isCompleted, "rejected cancel must leave the result available")
                        backend.completeConnect(command)
                        val connection: ConnectionId = atStage("$scenario: await original connect result") {
                            waiting.await().getOrThrow()
                        }
                        assertEquals(command.connectionId, connection)
                        owner.awaitEvent(connected, stage = "$scenario: original Connected")
                        owner.awaitEvent(terminal, stage = "$scenario: original terminal after Connected")
                    } else {
                        val accepted: Unit = receipt.getOrThrow()
                        assertEquals(Unit, accepted)
                        val result = atStage("$scenario: await Cancelled result") { waiting.await() }
                        assertIs<BlengException.Cancelled>(
                            result.exceptionOrNull(),
                            "$scenario: expected Cancelled result"
                        )
                        // Native callbacks may still arrive, but accepted cancellation suppresses target domain success.
                        backend.completeConnect(command)
                        owner.awaitEvent(
                            terminal,
                            stage = "$scenario: original terminal without target Connected",
                            forbidden = connected,
                        )
                    }
                    assertFailsWith<BlengException.OperationResultConsumed> { connect.wait(OPERATION_TIMEOUT_MS) }
                    assertEquals(1, backend.cancelCount.get())
                } finally {
                    backend.releaseCancellation.complete(Unit)
                    connect.destroy()
                }
            }
        }
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

private fun supervisedScanAdvertisements(scan: ScanSessionId): List<AdvertisementObservation> {
    val service = BluetoothUuid("0000180d-0000-1000-8000-00805f9b34fb")
    return listOf(
        AdvertisementObservation(
            scanSessionId = scan,
            peripheralId = PERIPHERAL,
            localName = "proxy-é🦀\u0000心率",
            advertisedServices = listOf(service, service),
            manufacturerData = listOf(
                ManufacturerData(0x1234u.toUShort(), READ_VALUE.copyOf()),
                ManufacturerData(0xffffu.toUShort(), byteArrayOf()),
            ),
            serviceData = listOf(ServiceData(service, byteArrayOf(0xff.toByte(), 0, 0x80.toByte()))),
            rssi = (-127).toShort(),
            connectable = true,
        ),
        AdvertisementObservation(scan, PeripheralId(202uL), null, emptyList(), emptyList(), emptyList(), null, null),
    )
}

private suspend fun nextScanAdvertisement(scan: SupervisedScan): ForeignScanAdvertisement {
    val observation: ForeignScanObservation = scan.nextObservation(OPERATION_TIMEOUT_MS.toDouble())
    assertNull(observation.failure)
    assertNull(observation.ended)
    return requireNotNull(observation.advertisement)
}

private fun assertScanAdvertisement(expected: AdvertisementObservation, actual: ForeignScanAdvertisement) {
    // The view carries no scan identity by design; the opaque peripheral key is the peer identity.
    assertEquals(expected.peripheralId.value.toString(), actual.peripheral.key())
    assertEquals(expected.localName, actual.localName)
    assertEquals(expected.localName, actual.peripheral.localName())
    assertEquals(expected.advertisedServices.map { it.value }, actual.advertisedServices)
    assertEquals(expected.rssi, actual.rssi)
    assertEquals(expected.connectable, actual.connectable)
    assertScanPayloads(
        expected.manufacturerData, actual.manufacturerData, expected.serviceData, actual.serviceData,
    )
}

private fun assertScanAdvertisement(expected: AdvertisementObservation, actual: AdvertisementObservation) {
    assertEquals(expected.scanSessionId, actual.scanSessionId)
    assertEquals(expected.peripheralId, actual.peripheralId)
    assertEquals(expected.localName, actual.localName)
    assertEquals(expected.advertisedServices, actual.advertisedServices)
    assertEquals(expected.rssi, actual.rssi)
    assertEquals(expected.connectable, actual.connectable)
    assertScanPayloads(
        expected.manufacturerData, actual.manufacturerData, expected.serviceData, actual.serviceData,
    )
}

private fun assertScanPayloads(
    expectedManufacturerData: List<ManufacturerData>,
    actualManufacturerData: List<ManufacturerData>,
    expectedServiceData: List<ServiceData>,
    actualServiceData: List<ServiceData>,
) {
    assertEquals(expectedManufacturerData.size, actualManufacturerData.size)
    expectedManufacturerData.zip(actualManufacturerData).forEach { (expectedData, actualData) ->
        assertEquals(expectedData.companyId, actualData.companyId)
        assertContentEquals(expectedData.payload, actualData.payload)
    }
    assertEquals(expectedServiceData.size, actualServiceData.size)
    expectedServiceData.zip(actualServiceData).forEach { (expectedData, actualData) ->
        assertEquals(expectedData.serviceUuid, actualData.serviceUuid)
        assertContentEquals(expectedData.payload, actualData.payload)
    }
}

private fun assertEmptyScanDelivery(scan: SupervisedScan) {
    assertEquals(ForeignScanDeliverySnapshot(0uL, 0uL, 0uL, false), scan.observationSnapshot())
}

private suspend fun <T> atStage(stage: String, block: suspend () -> T): T = try {
    block()
} catch (error: BlengException.Timeout) {
    throw AssertionError("$stage: BLeng timeout", error)
} catch (error: TimeoutCancellationException) {
    throw AssertionError("$stage: coroutine deadline expired", error)
}

private suspend fun Bleng.awaitEvent(
    expected: BleEvent,
    stage: String = "public event",
    forbidden: BleEvent? = null,
) = atStage("$stage: awaiting $expected; forbidden=$forbidden") {
    withTimeout(DEADLINE_MS) {
        while (true) {
            val event = nextEvent(OPERATION_TIMEOUT_MS)
            checkNotNull(event) { "$stage: event stream closed before $expected" }
            assertFalse(
                forbidden != null && event == forbidden,
                "$stage: suppressed target event leaked before $expected: $event",
            )
            if (event == expected) break
        }
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
                backend.releaseCancellation.complete(Unit)
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

private class SupervisorTestSession(
    private val sessionContext: ForeignOwnerContext,
    private val backendObject: BlengTestBackend,
) : ForeignPlatformOwnerSession {
    private val readerScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val readerLock = Any()
    private var reader: Deferred<Result<Unit>>? = null
    private var readerDrained = false
    private var retainedIngress: BackendEventIngressHandle? = null
    private var eventProjection: SessionEventProjection? = null
    private val scanEnvelopes = CopyOnWriteArrayList<ForeignScanEventEnvelope>()
    private val events = CopyOnWriteArrayList<BleEvent>()
    private val observed = Channel<BleEvent>(64)
    val readerStartCount = AtomicInteger()

    override fun context(): ForeignOwnerContext = sessionContext.copy()

    override fun backend(): BlengTestBackend = backendObject

    override fun disposeUnassembled() = backendObject.dispose()

    override fun startEventReader(owner: Bleng, eventProjection: SessionEventProjection) {
        synchronized(readerLock) {
            check(readerStartCount.incrementAndGet() == 1) { "Supervisor started more than one reader" }
            retainedIngress = checkNotNull(backendObject.ingress)
            this.eventProjection = eventProjection
            // Retain the endpoint only for fixture revocation assertions; it is never exposed.
            // Rust owns assembly/initialization. The owner wrapper belongs to the single reader.
            reader = readerScope.async {
                try {
                    while (true) {
                        val event = owner.nextEvent(EVENT_READER_TIMEOUT_MS) ?: break
                        check(events.size < 64) { "Supervisor fixture event budget exceeded" }
                        eventProjection.project(event)?.let { scanEnvelopes.add(it) }
                        events.add(event)
                        check(observed.trySend(event).isSuccess)
                    }
                    Result.success(Unit)
                } catch (error: Throwable) {
                    Result.failure(error)
                } finally {
                    withContext(NonCancellable) { owner.destroy() }
                }
            }
        }
    }

    override suspend fun waitEventReader() {
        val task = synchronized(readerLock) { reader } ?: return
        // Rust must revoke before asking the existing reader to drain, not just after it exits.
        assertNull(checkNotNull(eventProjection).project(BleEvent.ScanStarted(CommandId(999uL), ScanSessionId(999uL))))
        task.await().getOrThrow()
        synchronized(readerLock) { readerDrained = true }
    }

    override suspend fun resourceSnapshot(): List<ForeignResourceCount> {
        val disposals = backendObject.disposeCount.get()
        check(disposals in 0..1) { "Supervisor backend disposed $disposals times" }
        val readerCount = synchronized(readerLock) {
            check(disposals == 0 || reader == null || readerDrained) { "snapshot preceded reader drain" }
            if (reader != null && !readerDrained) 1uL else 0uL
        }
        return listOf(
            ForeignResourceCount("backendLeases", if (disposals == 0) 1uL else 0uL),
            ForeignResourceCount("eventReaders", readerCount),
            ForeignResourceCount("ingressRoots", if (backendObject.ingress != null) 1uL else 0uL),
        )
    }

    override suspend fun executeRecoveryAction(
        sequence: ULong,
        action: ForeignPlatformRecoveryAction,
    ): ForeignPlatformActionResult = throw SupervisorFfiException.Platform(
        "unexpected recovery in typed operation fixture: $sequence $action",
    )

    suspend fun awaitEvent(expected: BleEvent) = atStage("Supervisor reader: $expected") {
        withTimeout(DEADLINE_MS) {
            while (true) {
                if (observed.receive() == expected) break
            }
        }
    }

    fun drainedEvents(): List<BleEvent> {
        check(synchronized(readerLock) { readerDrained }) { "event evidence requested before reader drain" }
        return events.toList()
    }

    fun drainedScanEnvelopes(): List<ForeignScanEventEnvelope> {
        check(synchronized(readerLock) { readerDrained }) { "scan evidence requested before reader drain" }
        return scanEnvelopes.toList()
    }

    fun assertScanProjectionRevoked() {
        val projection = checkNotNull(eventProjection)
        for (event in drainedEvents()) assertNull(projection.project(event))
    }

    suspend fun release() {
        readerScope.cancel()
        try {
            synchronized(readerLock) { reader }?.join()
        } finally {
            eventProjection?.destroy()
            retainedIngress?.destroy()
            observed.close()
        }
    }

    companion object {
        val resourceNames = listOf("backendLeases", "eventReaders", "ingressRoots")
    }
}

private class SupervisorTestFactory(private val backendObject: BlengTestBackend) : ForeignPlatformOwnerSessionFactory {
    @Volatile
    var session: SupervisorTestSession? = null
        private set

    override fun requiredZeroResourceNames(): List<String> = SupervisorTestSession.resourceNames

    @Synchronized
    override fun create(context: ForeignOwnerContext): ForeignPlatformOwnerSession {
        check(session == null) { "Supervisor unexpectedly created a second session" }
        return SupervisorTestSession(context.copy(), backendObject).also { session = it }
    }
}

private fun supervisorTest(
    backend: BlengTestBackend,
    block: suspend CoroutineScope.(
        Supervisor, SupervisorTestSession, ForeignOwnerStart, MutableList<() -> Unit>,
    ) -> Unit,
) = runBlocking {
    withTimeout(10_000) {
        val context = ForeignOwnerContext("art-supervisor-owner", "art-supervisor-backend", 1uL, 1uL)
        val factory = SupervisorTestFactory(backend)
        val supervisor = Supervisor(
            factory,
            ForeignSupervisorConfig(ForeignRecoveryPolicyConfig(context, null, emptyList(), false, false), null),
        )
        val releases = mutableListOf<() -> Unit>()
        var primary: Throwable? = null
        try {
            assertNull(supervisor.activeContext())
            val initialized: ForeignOwnerStart = supervisor.start()
            val platform = checkNotNull(factory.session)
            assertEquals(context, initialized.context)
            assertEquals(backend.snapshot, initialized.adapterSnapshot)
            assertEquals(backend.initializing.await().commandId.value, initialized.initializationCommandId)
            assertEquals(context, supervisor.activeContext())
            assertEquals(initialized, supervisor.activeOwnerStart())
            assertEquals(1, backend.initializeCount.get())
            assertEquals(1, platform.readerStartCount.get())
            assertEquals(
                SupervisorTestSession.resourceNames.map { ForeignResourceCount(it, 1uL) }, platform.resourceSnapshot(),
            )
            block(supervisor, platform, initialized, releases)
        } catch (error: Throwable) {
            primary = error
            throw error
        } finally {
            withContext(NonCancellable) {
                val cleanupErrors = mutableListOf<Throwable>()
                suspend fun attempt(action: suspend () -> Unit) {
                    try {
                        withTimeout(DEADLINE_MS) { action() }
                    } catch (error: Throwable) {
                        cleanupErrors.add(error)
                    }
                }
                backend.releaseCancellation.complete(Unit)
                // Teardown uses the idempotent close view: a body that already closed the owner must
                // not turn cleanup into a false failure. `shutdown()` itself stays strict, and every
                // body that calls it asserts its exact resource-zero return value.
                attempt { supervisor.shutdownView() }
                factory.session?.let { platform ->
                    attempt { platform.waitEventReader() }
                    attempt {
                        assertEquals(
                            SupervisorTestSession.resourceNames.map { ForeignResourceCount(it, 0uL) },
                            platform.resourceSnapshot(),
                        )
                    }
                }
                for (release in releases.asReversed()) attempt { release() }
                factory.session?.let { platform -> attempt { platform.release() } }
                attempt { supervisor.destroy() }
                attempt {
                    assertEquals(1, backend.disposeCount.get())
                    assertNull(backend.ingress)
                    assertEquals(EventIngressStatus.Closed, backend.disposeIngressStatus)
                    assertTrue(backend.deliveryStatuses.all { it == EventIngressStatus.Delivered })
                }
                val failure = primary ?: cleanupErrors.firstOrNull()
                cleanupErrors.filter { it !== failure }.forEach { failure?.addSuppressed(it) }
                if (primary == null && failure != null) throw failure
            }
        }
    }
}

private class BlengTestBackend(
    val snapshot: AdapterSnapshot = AdapterSnapshot(
        AdapterState.POWERED_ON, AuthorizationState.AUTHORIZED, AuthorizationState.AUTHORIZED, "ART synthetic backend",
    ),
    private val completeInitialization: Boolean = true,
    private val completeReads: Boolean = true,
    private val completeConnection: Boolean = true,
    private val gateCancellation: Boolean = false,
    private val rejectCancellation: Boolean = false,
    private val completeScanStart: Boolean = true,
    private val completeScanStop: Boolean = true,
    private val advertiseBeforeScanStart: Boolean = false,
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
    val stopping = CompletableDeferred<StopScanCommand>()
    val connecting = CompletableDeferred<ConnectCommand>()
    val disconnecting = CompletableDeferred<DisconnectCommand>()
    val discovering = CompletableDeferred<DiscoverGattCommand>()
    val readingRssi = CompletableDeferred<ReadRssiCommand>()
    val reading = Channel<ReadCharacteristicCommand>(2)
    val writing = CompletableDeferred<WriteCharacteristicCommand>()
    val notifications = Channel<SetNotificationsCommand>(2)
    val cancelling = CompletableDeferred<CancelOperationRequest>()
    val releaseCancellation = CompletableDeferred<Unit>()
    val cancelCount = AtomicInteger()

    @Volatile
    var database: GattDatabase? = null

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
        if (advertiseBeforeScanStart) {
            supervisedScanAdvertisements(command.scanSessionId).forEach { push(BleEvent.AdvertisementObserved(it)) }
        }
        if (completeScanStart) completeStartScan(command)
        scanning.complete(command)
    }

    fun completeStartScan(command: StartScanCommand) {
        push(BleEvent.ScanStarted(command.commandId, command.scanSessionId))
        push(BleEvent.CommandCompleted(command.commandId, null))
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

    override fun stopScan(command: StopScanCommand) {
        if (completeScanStop) completeStopScan(command)
        stopping.complete(command)
    }

    fun completeStopScan(command: StopScanCommand) {
        push(
            BleEvent.ScanStopped(
                command.scanSessionId,
                ScanStopResult.Ok(ScanStopReason.Requested(command.commandId))
            )
        )
        push(BleEvent.CommandCompleted(command.commandId, null))
    }

    override fun connect(command: ConnectCommand) {
        if (completeConnection) completeConnect(command)
        connecting.complete(command)
    }

    fun completeConnect(command: ConnectCommand) {
        push(
            BleEvent.ConnectionStateChanged(
                command.commandId, command.connectionId, ConnectionStateResult.Ok(ConnectionState.CONNECTED),
            )
        )
        push(BleEvent.CommandCompleted(command.commandId, null))
    }

    override fun disconnect(command: DisconnectCommand) {
        push(
            BleEvent.ConnectionStateChanged(
                command.commandId, command.connectionId, ConnectionStateResult.Ok(ConnectionState.DISCONNECTED),
            )
        )
        push(BleEvent.CommandCompleted(command.commandId, null))
        disconnecting.complete(command)
    }

    override fun discoverGatt(command: DiscoverGattCommand) {
        val discovered = GattDatabase(
            command.connectionId,
            command.gattSessionId,
            listOf(
                GattService(
                    GattServiceId(501uL),
                    BluetoothUuid("0000180d-0000-1000-8000-00805f9b34fb"),
                    true,
                    listOf(
                        GattCharacteristic(
                            CHARACTERISTIC,
                            BluetoothUuid("00002a37-0000-1000-8000-00805f9b34fb"),
                            CharacteristicProperties(true, true, true, true, true),
                            emptyList(),
                        )
                    ),
                )
            ),
        )
        database = discovered
        push(BleEvent.GattDiscovered(command.commandId, discovered))
        push(BleEvent.CommandCompleted(command.commandId, null))
        discovering.complete(command)
    }

    override fun readCharacteristic(command: ReadCharacteristicCommand) {
        if (completeReads) completeRead(command)
        check(reading.trySend(command).isSuccess)
    }

    fun completeRead(command: ReadCharacteristicCommand) {
        push(
            BleEvent.CharacteristicRead(
                command.commandId,
                command.connectionId,
                command.gattSessionId,
                command.characteristicId,
                READ_VALUE.copyOf(),
            )
        )
        push(BleEvent.CommandCompleted(command.commandId, null))
    }

    override fun writeCharacteristic(command: WriteCharacteristicCommand) {
        push(
            BleEvent.CharacteristicWritten(
                command.commandId, command.connectionId, command.gattSessionId, command.characteristicId,
            )
        )
        push(BleEvent.CommandCompleted(command.commandId, null))
        writing.complete(command)
    }

    override fun setNotifications(command: SetNotificationsCommand) {
        val state = when (command.action) {
            is NotificationAction.Enable -> NotificationState.Enabled(NotificationMode.INDICATION)
            NotificationAction.Disable -> NotificationState.Disabled
        }
        push(
            BleEvent.NotificationStateChanged(
                command.commandId, command.connectionId, command.gattSessionId, command.subscriptionId,
                command.characteristicId, state,
            )
        )
        push(BleEvent.CommandCompleted(command.commandId, null))
        check(notifications.trySend(command).isSuccess)
    }

    override fun readRssi(command: ReadRssiCommand) {
        push(BleEvent.RssiRead(command.commandId, command.connectionId, -61))
        push(BleEvent.CommandCompleted(command.commandId, null))
        readingRssi.complete(command)
    }

    // The published backend must answer a bounded MTU query/request like any other command.
    override fun mtu(command: MtuCommand) {
        push(
            BleEvent.MtuRead(
                command.commandId,
                command.connectionId,
                MtuInfo(mtu = 185u, maximumWriteWithResponse = 182u, maximumWriteWithoutResponse = 185u),
            )
        )
        push(BleEvent.CommandCompleted(command.commandId, null))
    }

    override fun setConnectionPriority(command: SetConnectionPriorityCommand) {
        push(BleEvent.CommandCompleted(command.commandId, null))
    }

    override fun createBond(command: CreateBondCommand) {
        push(
            BleEvent.BondStateChanged(
                command.commandId,
                command.connectionId,
                BondState.BONDED,
            ),
        )
        push(BleEvent.CommandCompleted(command.commandId, null))
    }

    override fun removeBond(command: RemoveBondCommand) {
        push(
            BleEvent.BondStateChanged(
                command.commandId,
                command.connectionId,
                BondState.NONE,
            ),
        )
        push(BleEvent.CommandCompleted(command.commandId, null))
    }

    override fun setAdapterEnabled(command: SetAdapterEnabledCommand) {
        push(BleEvent.CommandCompleted(command.commandId, null))
    }

    override fun queryDevices(command: QueryDevicesCommand) {
        push(BleEvent.DevicesListed(command.commandId, emptyList()))
        push(BleEvent.CommandCompleted(command.commandId, null))
    }

    override fun associateCompanionDevice(command: AssociateCompanionDeviceCommand) {
        push(BleEvent.CommandCompleted(command.commandId, null))
    }

    override fun refreshGattCache(command: RefreshGattCacheCommand) {
        push(BleEvent.CommandCompleted(command.commandId, null))
    }

    override fun readDescriptor(command: ReadDescriptorCommand) {
        push(
            BleEvent.DescriptorRead(
                command.commandId,
                command.connectionId,
                command.gattSessionId,
                command.characteristicId,
                command.descriptorId,
                byteArrayOf(0x01, 0x00),
            )
        )
        push(BleEvent.CommandCompleted(command.commandId, null))
    }

    override fun writeDescriptor(command: WriteDescriptorCommand) {
        push(
            BleEvent.DescriptorWritten(
                command.commandId,
                command.connectionId,
                command.gattSessionId,
                command.characteristicId,
                command.descriptorId,
            )
        )
        push(BleEvent.CommandCompleted(command.commandId, null))
    }

    override fun cancelOperation(request: CancelOperationRequest) {
        cancelCount.incrementAndGet()
        cancelling.complete(request)
        if (gateCancellation) {
            // This synchronous foreign callback runs on the native dispatch lane, not the test coroutine.
            runBlocking { withTimeout(DEADLINE_MS) { releaseCancellation.await() } }
        }
        if (rejectCancellation) {
            throw CommandDispatchException.OperationNotCancellable(request.targetCommandId)
        }
    }
}
