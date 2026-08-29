package dev.blehub.consumer

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
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertTrue

class ArtifactConsumerTest {
    @Test
    fun consumesOnlyTheBindingsJarAndNativeLibraries() {
        val bindingsLocation = Phase0Fixture::class.java.protectionDomain.codeSource.location
        assertTrue(bindingsLocation.path.endsWith(".jar"))

        val disposeCount = AtomicInteger()
        var boundToken: BackendEventIngressToken? = null
        val backend = object : BleBackend {
            override fun capabilities() =
                BackendCapabilities(Platform.TEST, fixtureContractVersion)

            override fun bindEventIngress(token: BackendEventIngressToken) {
                check(token.value != 0uL)
                boundToken = token
            }

            override fun startScan(command: StartScanCommand) {
                assertEquals(
                    EventIngressStatus.Delivered,
                    pushBackendEvent(
                        checkNotNull(boundToken),
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
            }
        }
        val fixture = Phase0Fixture(backend)

        fixture.startScan(
            StartScanCommand(
                commandId = CommandId(1uL),
                scanSessionId = ScanSessionId(2uL),
            ),
        )

        assertEquals(1uL, fixture.snapshot().eventCount)
        fixture.dispose()
        fixture.close()
        assertEquals(1, disposeCount.get())
    }
}
