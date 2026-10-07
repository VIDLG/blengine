package dev.blehub.ffi.runtime.consumer

import android.os.Handler
import android.os.HandlerThread
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry

import dev.blehub.BackendCapabilities
import dev.blehub.BackendEventIngressHandle
import dev.blehub.BleBackend
import dev.blehub.BleEvent
import dev.blehub.BleEventSink
import dev.blehub.BleHub
import dev.blehub.Platform
import dev.blehub.android.AndroidBleBackend
import dev.blehub.CommandId
import dev.blehub.ConnectCommand
import dev.blehub.DisconnectCommand
import dev.blehub.DiscoverGattCommand
import dev.blehub.EventIngressStatus
import dev.blehub.InitializeCommand
import dev.blehub.ReadCharacteristicCommand
import dev.blehub.ReadRssiCommand
import dev.blehub.ScanOptions
import dev.blehub.ScanSessionId
import dev.blehub.SetNotificationsCommand
import dev.blehub.StartScanCommand
import dev.blehub.StopScanCommand
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import dev.blehub.WriteCharacteristicCommand
import dev.blehub.blehubContractVersion
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicReference
import java.util.concurrent.locks.ReentrantLock
import kotlin.concurrent.withLock
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class PublishedArtifactConsumerTest {
    @Test
    fun publishedAarLoadsTheProductionComponentAndObjectGraph() {
        val backend = ConsumerBackend()
        val sink = ConsumerSink()
        val hub = BleHub(backend, sink)
        val command = StartScanCommand(
            commandId = CommandId(1uL),
            scanSessionId = ScanSessionId(2uL),
            options = ScanOptions(
                serviceUuids = emptyList(),
                namePrefix = null,
                companyId = null,
                allowDuplicates = false,
            ),
        )

        hub.startScan(command)

        assertEquals(EventIngressStatus.Delivered, backend.lastStatus.get())
        assertEquals(
            BleEvent.ScanStarted(command.commandId, command.scanSessionId),
            sink.snapshot().single(),
        )
        hub.dispose()
        hub.destroy()
        assertEquals(1, backend.disposeCount.get())
        assertFalse(backend.hasIngress())
    }

    @Test
    fun publishedBackendAarInitializesAndDeliversAdapterSnapshot() {
        val worker = HandlerThread("android-backend-initialization-consumer").also { it.start() }
        val backend = AndroidBleBackend(
            InstrumentationRegistry.getInstrumentation().targetContext,
            Handler(worker.looper),
        )
        val sink = FirstEventSink()
        val hub = BleHub(backend, sink)
        try {
            hub.initialize(InitializeCommand(CommandId(3uL)))
            assertTrue(sink.await())
            assertTrue(sink.event.get() is BleEvent.AdapterSnapshotChanged)
        } finally {
            hub.dispose()
            hub.destroy()
            worker.quitSafely()
        }
    }

    @Test
    fun publishedBackendAarLoadsAndReleasesItsCallerOwnedHandler() {
        val worker = HandlerThread("android-backend-consumer").also { it.start() }
        try {
            val backend = AndroidBleBackend(
                InstrumentationRegistry.getInstrumentation().targetContext,
                Handler(worker.looper),
            )
            assertEquals(Platform.ANDROID, backend.capabilities().platform)
            assertEquals(5u, backend.capabilities().contractVersion)
            assertTrue(backend.capabilities().supportsReadRssi)
            backend.dispose()
        } finally {
            worker.quitSafely()
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

        override fun setNotifications(command: SetNotificationsCommand) = Unit

        override fun readRssi(command: ReadRssiCommand) = Unit

        override fun dispose() {
            disposeCount.incrementAndGet()
            ingress.getAndSet(null)?.destroy()
        }

        fun hasIngress() = ingress.get() != null
    }
}
