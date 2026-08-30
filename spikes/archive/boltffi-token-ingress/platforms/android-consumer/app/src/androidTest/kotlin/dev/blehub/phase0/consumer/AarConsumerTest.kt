package dev.blehub.phase0.consumer

import androidx.test.ext.junit.runners.AndroidJUnit4
import dev.blehub.phase0.BackendCapabilities
import dev.blehub.phase0.BackendEventIngressToken
import dev.blehub.phase0.BleBackend
import dev.blehub.phase0.BleEvent
import dev.blehub.phase0.BleEventPayload
import dev.blehub.phase0.CommandId
import dev.blehub.phase0.ConnectCommand
import dev.blehub.phase0.EventIngressStatus
import dev.blehub.phase0.Phase0Fixture
import dev.blehub.phase0.Platform
import dev.blehub.phase0.ScanSessionId
import dev.blehub.phase0.StartScanCommand
import dev.blehub.phase0.fixtureContractVersion
import dev.blehub.phase0.pushBackendEvent
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicReference
import org.junit.Assert.assertEquals
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class AarConsumerTest {
    @Test
    fun cleanConsumerLoadsKotlinAndNativeCodeFromTheAar() {
        val backend = TestBackend()
        val fixture = Phase0Fixture(backend)
        val command = StartScanCommand(CommandId(1uL), ScanSessionId(2uL))

        fixture.startScan(command)

        assertEquals(EventIngressStatus.Delivered, backend.lastStatus.get())
        assertEquals(1uL, fixture.snapshot().eventCount)
        assertEquals(1, backend.capabilitiesCount.get())
        assertEquals(1, backend.bindCount.get())
        fixture.dispose()
        fixture.close()
        assertEquals(1, backend.disposeCount.get())
    }

    private class TestBackend : BleBackend {
        private val token = AtomicReference<BackendEventIngressToken>()
        val lastStatus = AtomicReference<EventIngressStatus>()
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
            lastStatus.set(
                pushBackendEvent(
                    checkNotNull(token.get()),
                    BleEvent(
                        BleEventPayload.ScanStarted(command.commandId, command.scanSessionId),
                    ),
                ),
            )
        }

        override fun connect(command: ConnectCommand) = Unit

        override fun dispose() {
            disposeCount.incrementAndGet()
        }
    }
}
