package dev.blehub.candidate.consumer

import androidx.test.ext.junit.runners.AndroidJUnit4
import dev.blehub.candidate.blehub.BackendCapabilities
import dev.blehub.candidate.blehub.BackendEventIngressHandle
import dev.blehub.candidate.blehub.BleBackend
import dev.blehub.candidate.blehub.BleEvent
import dev.blehub.candidate.blehub.BleEventPayload
import dev.blehub.candidate.blehub.CommandId
import dev.blehub.candidate.blehub.ConnectCommand
import dev.blehub.candidate.blehub.EventIngressStatus
import dev.blehub.candidate.blehub.Platform
import dev.blehub.candidate.blehub.ScanSessionId
import dev.blehub.candidate.blehub.StartScanCommand
import dev.blehub.candidate.fixture.ProductionCandidateFixture
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
        val command = StartScanCommand(CommandId(1uL), ScanSessionId(2uL))

        fixture.startScan(command)

        assertEquals(EventIngressStatus.Delivered, backend.lastStatus.get())
        assertEquals(
            BleEventPayload.ScanStarted(command.commandId, command.scanSessionId),
            fixture.snapshot().events.single().payload,
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

        override fun capabilities() = BackendCapabilities(Platform.TEST, 3u)

        override fun bindEventIngress(ingress: BackendEventIngressHandle) {
            check(this.ingress.compareAndSet(null, ingress))
        }

        override fun startScan(command: StartScanCommand) {
            lastStatus.set(
                checkNotNull(ingress.get()).push(
                    BleEvent(
                        BleEventPayload.ScanStarted(
                            command.commandId,
                            command.scanSessionId,
                        ),
                    ),
                ),
            )
        }

        override fun connect(command: ConnectCommand) = Unit

        override fun dispose() {
            disposeCount.incrementAndGet()
            ingress.getAndSet(null)?.destroy()
        }

        fun hasIngress() = ingress.get() != null
    }
}
