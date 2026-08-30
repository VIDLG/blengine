package dev.blehub.candidate.consumer

import androidx.test.ext.junit.runners.AndroidJUnit4
import dev.blehub.candidate.blehub.BackendCapabilities
import dev.blehub.candidate.blehub.BackendEventIngressHandle
import dev.blehub.candidate.blehub.BleBackend
import dev.blehub.candidate.blehub.BleEvent
import dev.blehub.candidate.blehub.CommandId
import dev.blehub.candidate.blehub.ConnectCommand
import dev.blehub.candidate.blehub.DisconnectCommand
import dev.blehub.candidate.blehub.DiscoverGattCommand
import dev.blehub.candidate.blehub.EventIngressStatus
import dev.blehub.candidate.blehub.InitializeCommand
import dev.blehub.candidate.blehub.Platform
import dev.blehub.candidate.blehub.ReadCharacteristicCommand
import dev.blehub.candidate.blehub.ReadRssiCommand
import dev.blehub.candidate.blehub.ScanOptions
import dev.blehub.candidate.blehub.ScanSessionId
import dev.blehub.candidate.blehub.SetNotificationsCommand
import dev.blehub.candidate.blehub.StartScanCommand
import dev.blehub.candidate.blehub.StopScanCommand
import dev.blehub.candidate.blehub.WriteCharacteristicCommand
import dev.blehub.candidate.fixture.ProductionCandidateFixture
import dev.blehub.candidate.fixture.fixtureContractVersion
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicReference
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class PublishedArtifactConsumerTest {
    @Test
    fun publishedAarLoadsBothComponentsAndTheProductionObjectGraph() {
        val backend = ConsumerBackend()
        val fixture = ProductionCandidateFixture(backend)
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

        fixture.startScan(command)

        assertEquals(EventIngressStatus.Delivered, backend.lastStatus.get())
        assertEquals(
            BleEvent.ScanStarted(command.commandId, command.scanSessionId),
            fixture.snapshot().events.single(),
        )
        fixture.dispose()
        fixture.destroy()
        assertEquals(1, backend.disposeCount.get())
        assertFalse(backend.hasIngress())
    }

    private class ConsumerBackend : BleBackend {
        private val ingress = AtomicReference<BackendEventIngressHandle>()
        val lastStatus = AtomicReference<EventIngressStatus>()
        val disposeCount = AtomicInteger()

        override fun capabilities() = BackendCapabilities(
            platform = Platform.TEST,
            contractVersion = fixtureContractVersion(),
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
