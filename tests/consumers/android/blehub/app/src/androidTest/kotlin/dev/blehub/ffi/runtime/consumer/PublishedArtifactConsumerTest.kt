package dev.blehub.ffi.runtime.consumer

import android.os.Handler
import android.os.HandlerThread
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry

import dev.blehub.AssociateCompanionDeviceCommand
import dev.blehub.BackendCapabilities
import dev.blehub.BackendEventIngressHandle
import dev.blehub.BleBackend
import dev.blehub.CancelOperationRequest
import dev.blehub.BleEvent
import dev.blehub.BleEventSink
import dev.blehub.BleHub
import dev.blehub.Platform
import dev.blehub.android.AndroidBleBackend
import dev.blehub.CommandId
import dev.blehub.ConnectCommand
import dev.blehub.CreateBondCommand
import dev.blehub.DisconnectCommand
import dev.blehub.DiscoverGattCommand
import dev.blehub.EventIngressStatus
import dev.blehub.InitializeCommand
import dev.blehub.MtuCommand
import dev.blehub.QueryDevicesCommand
import dev.blehub.ReadCharacteristicCommand
import dev.blehub.ReadDescriptorCommand
import dev.blehub.ReadRssiCommand
import dev.blehub.RefreshGattCacheCommand
import dev.blehub.RemoveBondCommand
import dev.blehub.ScanOptions
import dev.blehub.ScanSessionId
import dev.blehub.SetAdapterEnabledCommand
import dev.blehub.SetConnectionPriorityCommand
import dev.blehub.SetNotificationsCommand
import dev.blehub.StartScanCommand
import dev.blehub.StopScanCommand
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import dev.blehub.WriteCharacteristicCommand
import dev.blehub.WriteDescriptorCommand
import dev.blehub.blehubContractVersion
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicReference
import java.util.concurrent.locks.ReentrantLock
import kotlin.concurrent.withLock
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class PublishedArtifactConsumerTest {
    @Test
    fun publishedAarLoadsTheProductionComponentAndObjectGraph() {
        val backend = ConsumerBackend()
        val sink = ConsumerSink()
        val command = StartScanCommand(
            commandId = CommandId(1uL),
            scanSessionId = ScanSessionId(2uL),
            options = ScanOptions(
                serviceUuids = emptyList(),
                namePrefix = null,
                exactName = null,
                companyId = null,
                minRssi = null,
                manufacturerData = null,
                allowDuplicates = false, android = null
            ),
        )

        withConsumerHub(backend, sink) { hub ->
            hub.startScan(command)
            assertEquals(EventIngressStatus.Delivered, backend.lastStatus.get())
            assertEquals(
                BleEvent.ScanStarted(command.commandId, command.scanSessionId),
                sink.snapshot().single(),
            )
        }
        assertEquals(1, backend.disposeCount.get())
        assertFalse(backend.hasIngress())
    }

    @Test
    fun publishedBackendAarInitializesAndDeliversAdapterSnapshot() {
        withPublishedBackend("android-backend-initialization-consumer") { backend ->
            val sink = FirstEventSink()
            withConsumerHub(backend, sink) { hub ->
                hub.initialize(InitializeCommand(CommandId(3uL)))
                assertTrue(sink.await())
                assertTrue(sink.event.get() is BleEvent.AdapterSnapshotChanged)
            }
        }
    }

    @Test
    fun publishedBackendAarLoadsAndReleasesItsCallerOwnedHandler() {
        withPublishedBackend("android-backend-consumer") { backend ->
            assertEquals(Platform.ANDROID, backend.capabilities().platform)
            assertEquals(blehubContractVersion(), backend.capabilities().contractVersion)
            assertTrue(backend.capabilities().supportsReadRssi)
        }
    }

    @Test
    fun failedConsumerAssertionReleasesBackendLeaseForTheNextOwner() {
        val failure = AssertionError("intentional consumer assertion before disposal")
        val observed = runCatching {
            withPublishedBackend("android-backend-failing-consumer") { throw failure }
        }.exceptionOrNull()
        assertSame(failure, observed)
        assertEquals(0, failure.suppressed.size)
        withPublishedBackend("android-backend-replacement-consumer") { backend ->
            assertEquals(blehubContractVersion(), backend.capabilities().contractVersion)
        }
    }

    @Test
    fun realBackendIngressAndLeaseAreObservedBeforeDroppingTheDisposedInstance() {
        // No initialization, scan or connection: this verifies production ownership, not BLE.
        repeat(10) {
            withPublishedBackend("BleHubTest-resources") { backend ->
                withConsumerHub(backend, object : BleEventSink {
                    override fun onEvent(event: BleEvent) = Unit
                }) { hub ->
                    val active = backend.snapshotRfResources()
                    assertEquals(1, active.getValue("backendLeases"))
                    assertEquals(1, active.getValue("ingressRoots"))
                    hub.dispose()
                    // Observe the SAME backend after dispose while its Handler still runs.
                    val disposed = backend.snapshotRfResources()
                    // The inventory is an owner diagnostic, not frozen ABI: pin its shape and its
                    // zeroed values, never a counter count that grows with each new owned root.
                    assertEquals(active.keys, disposed.keys)
                    assertTrue("disposed native roots: $disposed", disposed.values.all { count -> count == 0 })
                }
            }
        } // Each next constructor also proves the actual process lease was released.
    }

    private fun <T> withPublishedBackend(name: String, action: (AndroidBleBackend) -> T): T {
        val worker = HandlerThread(name).also { it.start() }
        return AutoCloseable {
            worker.quitSafely()
            worker.join(5_000)
            assertFalse("consumer-owned HandlerThread did not stop", worker.isAlive)
        }.use {
            val backend = AndroidBleBackend(
                InstrumentationRegistry.getInstrumentation().targetContext,
                Handler(worker.looper),
            )
            AutoCloseable { backend.dispose() }.use { action(backend) }
        }
    }

    private fun <T> withConsumerHub(backend: BleBackend, sink: BleEventSink, action: (BleHub) -> T): T {
        val hub = BleHub(backend, sink)
        // Nested use preserves the primary assertion and suppresses cleanup failures.
        return AutoCloseable { hub.destroy() }.use {
            AutoCloseable { hub.dispose() }.use { action(hub) }
        }
    }

    private class FirstEventSink : BleEventSink {
        val event = AtomicReference<BleEvent?>()
        private val signal = CountDownLatch(1)

        override fun onEvent(event: BleEvent) {
            this.event.set(event)
            signal.countDown()
        }

        fun await(): Boolean = signal.await(5, TimeUnit.SECONDS)
    }

    private class ConsumerSink : BleEventSink {
        private val lock = ReentrantLock()
        private val events = mutableListOf<BleEvent>()

        override fun onEvent(event: BleEvent) {
            lock.withLock { events += event }
        }

        fun snapshot(): List<BleEvent> = lock.withLock { events.toList() }
    }

    private class ConsumerBackend : BleBackend {
        private val ingress = AtomicReference<BackendEventIngressHandle>()
        val lastStatus = AtomicReference<EventIngressStatus>()
        val disposeCount = AtomicInteger()

        override fun capabilities() = BackendCapabilities(
            platform = Platform.TEST,
            contractVersion = blehubContractVersion(),
            supportsDuplicateScanResults = false,
            supportsConnectableFlag = false,
            supportsReadRssi = false,
            supportsRequiredNotification = false,
            supportsRequiredIndication = false,
            reportsActualNotificationMode = false,
        )

        override fun bindEventIngress(ingress: BackendEventIngressHandle) {
            check(this.ingress.compareAndSet(null, ingress))
        }

        override fun initialize(command: InitializeCommand) = Unit

        override fun setAdapterEnabled(command: SetAdapterEnabledCommand) = Unit

        override fun queryDevices(command: QueryDevicesCommand) = Unit

        // Android-only platform capability; the consumer compiles the shape, it does not pair.
        override fun associateCompanionDevice(command: AssociateCompanionDeviceCommand) = Unit

        override fun startScan(command: StartScanCommand) {
            lastStatus.set(
                checkNotNull(ingress.get()).push(
                    BleEvent.ScanStarted(
                        command.commandId,
                        command.scanSessionId,
                    ),
                ),
            )
        }

        override fun stopScan(command: StopScanCommand) = Unit

        override fun connect(command: ConnectCommand) = Unit

        override fun disconnect(command: DisconnectCommand) = Unit

        override fun discoverGatt(command: DiscoverGattCommand) = Unit

        override fun readCharacteristic(command: ReadCharacteristicCommand) = Unit

        override fun writeCharacteristic(command: WriteCharacteristicCommand) = Unit

        override fun readDescriptor(command: ReadDescriptorCommand) = Unit

        override fun writeDescriptor(command: WriteDescriptorCommand) = Unit

        override fun setNotifications(command: SetNotificationsCommand) = Unit

        override fun readRssi(command: ReadRssiCommand) = Unit

        override fun mtu(command: MtuCommand) = Unit

        override fun setConnectionPriority(command: SetConnectionPriorityCommand) = Unit

        override fun createBond(command: CreateBondCommand) = Unit

        override fun removeBond(command: RemoveBondCommand) = Unit

        override fun refreshGattCache(command: RefreshGattCacheCommand) = Unit

        override fun cancelOperation(request: CancelOperationRequest) = Unit

        override fun dispose() {
            disposeCount.incrementAndGet()
            ingress.getAndSet(null)?.destroy()
        }

        fun hasIngress() = ingress.get() != null
    }
}
