package dev.blehub.uniffi.consumer

import androidx.test.ext.junit.runners.AndroidJUnit4
import dev.blehub.uniffi.BackendEventIngressHandle
import dev.blehub.uniffi.BleBackend
import dev.blehub.uniffi.BleEvent
import dev.blehub.uniffi.EventIngressStatus
import dev.blehub.uniffi.Phase0Fixture
import dev.blehub.uniffi.StartScanCommand
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicReference
import org.junit.Assert.assertEquals
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class PublishedArtifactConsumerTest {
    @Test
    fun publishedAarAndTransitiveJnaLoadWithoutGeneratedSources() {
        val backend = TestBackend()
        val fixture = Phase0Fixture(backend)

        fixture.startScan(StartScanCommand(commandId = 1u, scanSessionId = 2u))

        assertEquals(EventIngressStatus.DELIVERED, backend.lastStatus.get())
        assertEquals(1, fixture.events().size)
        fixture.dispose()
        fixture.destroy()
        backend.releaseIngress()
        assertEquals(1, backend.disposeCount.get())
    }

    private class TestBackend : BleBackend {
        private val ingress = AtomicReference<BackendEventIngressHandle>()
        val lastStatus = AtomicReference<EventIngressStatus>()
        val disposeCount = AtomicInteger()

        override fun bindEventIngress(ingress: BackendEventIngressHandle) {
            check(this.ingress.compareAndSet(null, ingress))
        }

        override fun startScan(command: StartScanCommand) {
            lastStatus.set(
                checkNotNull(ingress.get()).push(
                    BleEvent(
                        sequence = 1u,
                        commandId = command.commandId,
                        payload = byteArrayOf(1, 2, 3),
                    ),
                ),
            )
        }

        override fun dispose() {
            disposeCount.incrementAndGet()
        }

        fun releaseIngress() {
            ingress.getAndSet(null)?.destroy()
        }
    }
}
