package dev.blehub.phase0

import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicLong
import java.util.concurrent.atomic.AtomicReference
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertFalse
import kotlin.test.assertNotEquals
import kotlin.test.assertTrue

class TokenIngressFixtureTest {
    @Test
    fun capabilitiesFailureDisposesTheForeignBackendWithoutBinding() {
        val expected = BleHubError.BackendAssembly("capabilities unavailable")
        val backend = TestBackend(
            capabilitiesProvider = { throw expected },
        )

        val actual = assertFailsWith<BleHubError.BackendAssembly> {
            Phase0Fixture(backend)
        }

        assertEquals(expected, actual)
        assertEquals(1, backend.capabilitiesCount.get())
        assertEquals(0, backend.bindCount.get())
        assertEquals(1, backend.disposeCount.get())
        assertEquals(null, backend.token.get())
    }

    @Test
    fun invalidContractVersionDisposesBeforeBinding() {
        val backend = TestBackend(
            capabilitiesProvider = { BackendCapabilities(Platform.TEST, 2u) },
        )

        assertFailsWith<BleHubError.FfiContract> {
            Phase0Fixture(backend)
        }

        assertEquals(1, backend.disposeCount.get())
        assertEquals(0, backend.bindCount.get())
        assertEquals(null, backend.token.get())
    }

    @Test
    fun bindFailureDoesNotRetainTheTokenAndRollsBackOnce() {
        val expected = BleHubError.BackendAssembly("bind rejected")
        val backend = TestBackend(
            bindAction = { throw expected },
        )

        val actual = assertFailsWith<BleHubError.BackendAssembly> {
            Phase0Fixture(backend)
        }

        assertEquals(expected, actual)
        assertEquals(1, backend.bindCount.get())
        assertEquals(1, backend.disposeCount.get())
        assertEquals(null, backend.token.get())
    }

    @Test
    fun pushDuringBindIsClosedBecauseRegistrationHasNotHappened() {
        val bindStatus = AtomicReference<EventIngressStatus>()
        val backend = TestBackend(
            bindAction = { token ->
                bindStatus.set(
                    pushBackendEvent(
                        token,
                        BleEvent(
                            BleEventPayload.ScanStarted(CommandId(1uL), ScanSessionId(2uL)),
                        ),
                    ),
                )
            },
        )
        val fixture = Phase0Fixture(backend)

        assertEquals(EventIngressStatus.Closed, bindStatus.get())
        assertEquals(0uL, fixture.snapshot().eventCount)
        fixture.dispose()
        fixture.close()
    }

    @Test
    fun uncheckedCapabilitiesExceptionBecomesFfiContractAndRollsBack() {
        val backend = TestBackend(
            capabilitiesProvider = { error("unchecked capabilities failure") },
        )

        assertFailsWith<BleHubError.FfiContract> {
            Phase0Fixture(backend)
        }

        assertEquals(1, backend.disposeCount.get())
        assertEquals(0, backend.bindCount.get())
    }

    @Test
    fun uncheckedBindExceptionBecomesFfiContractAndRollsBack() {
        val backend = TestBackend(
            bindAction = { error("unchecked bind failure") },
        )

        assertFailsWith<BleHubError.FfiContract> {
            Phase0Fixture(backend)
        }

        assertEquals(1, backend.bindCount.get())
        assertEquals(1, backend.disposeCount.get())
        assertEquals(null, backend.token.get())
    }

    @Test
    fun synchronousPushBeforeCommandReturnUsesTheBoundToken() {
        val commandReturned = AtomicReference(false)
        val backend = TestBackend { token, command ->
            assertEquals(
                EventIngressStatus.Delivered,
                pushBackendEvent(token, scanStarted(command)),
            )
            assertEquals(false, commandReturned.get())
        }
        val fixture = Phase0Fixture(backend)

        fixture.startScan(scanCommand(1uL))
        commandReturned.set(true)

        assertEquals(1uL, fixture.snapshot().eventCount)
        assertEquals(1, backend.capabilitiesCount.get())
        assertEquals(1, backend.bindCount.get())
        assertEquals(Platform.TEST, fixture.capabilities().platform)
        fixture.dispose()
        fixture.close()
        assertEquals(1, backend.disposeCount.get())
    }

    @Test
    fun asynchronousJvmWorkerPushReachesTheRustSink() {
        val executor = Executors.newSingleThreadExecutor()
        val backend = TestBackend { token, command ->
            executor.submit {
                assertEquals(
                    EventIngressStatus.Delivered,
                    pushBackendEvent(token, scanStarted(command)),
                )
            }.get(5, TimeUnit.SECONDS)
        }
        val fixture = Phase0Fixture(backend)

        fixture.startScan(scanCommand(2uL))

        assertEquals(1uL, fixture.snapshot().eventCount)
        fixture.dispose()
        fixture.close()
        executor.shutdownNow()
    }

    @Test
    fun rustCreatedCallerThreadAttachesAndCanReenterRustThroughPush() {
        val jvmCallerThread = Thread.currentThread().id
        val backendThread = AtomicLong(jvmCallerThread)
        val backend = TestBackend { token, command ->
            backendThread.set(Thread.currentThread().id)
            assertEquals(
                EventIngressStatus.Delivered,
                pushBackendEvent(token, scanStarted(command)),
            )
        }
        val fixture = Phase0Fixture(backend)

        fixture.startScanFromRustThread(scanCommand(3uL))

        assertNotEquals(jvmCallerThread, backendThread.get())
        assertEquals(1uL, fixture.snapshot().eventCount)
        fixture.dispose()
        fixture.close()
    }

    @Test
    fun uncheckedCommandExceptionBecomesFfiContractWithoutCrossingTheAbi() {
        val backend = TestBackend { _, _ -> error("unchecked command failure") }
        val fixture = Phase0Fixture(backend)

        assertFailsWith<CommandDispatchError.FfiContract> {
            fixture.startScan(scanCommand(30uL))
        }

        assertEquals(0uL, fixture.snapshot().eventCount)
        fixture.dispose()
        fixture.close()
    }

    @Test
    fun typedDispatchRejectionCrossesTheForeignTraitBoundaryWithoutEvents() {
        val expected = CommandDispatchError.BackendUnavailable(
            CommandKind.START_SCAN,
            "scripted rejection",
            null,
        )
        val backend = TestBackend { _, _ -> throw expected }
        val fixture = Phase0Fixture(backend)

        val actual = assertFailsWith<CommandDispatchError.BackendUnavailable> {
            fixture.startScan(scanCommand(4uL))
        }

        assertEquals(expected, actual)
        assertEquals(0uL, fixture.snapshot().eventCount)
        fixture.dispose()
        fixture.close()
    }

    @Test
    fun downstreamRejectionReturnsStructuredFailureAndDoesNotRecordTheEvent() {
        val status = AtomicReference<EventIngressStatus>()
        val backend = TestBackend { token, command ->
            status.set(pushBackendEvent(token, scanStarted(command)))
        }
        val fixture = Phase0Fixture(backend)
        fixture.setSinkBehavior(FixtureSinkBehavior.REJECT)

        fixture.startScan(scanCommand(5uL))

        assertTrue(status.get() is EventIngressStatus.DownstreamFailed)
        assertEquals(0uL, fixture.snapshot().eventCount)
        fixture.dispose()
        fixture.close()
    }

    @Test
    fun invalidEventIsRejectedBeforeTheRustSink() {
        val status = AtomicReference<EventIngressStatus>()
        val backend = TestBackend { token, command ->
            status.set(
                pushBackendEvent(
                    token,
                    BleEvent(
                        BleEventPayload.ScanStarted(CommandId(0uL), command.scanSessionId),
                    ),
                ),
            )
        }
        val fixture = Phase0Fixture(backend)

        fixture.startScan(scanCommand(50uL))

        assertTrue(status.get() is EventIngressStatus.InvalidEvent)
        assertEquals(0uL, fixture.snapshot().eventCount)
        fixture.dispose()
        fixture.close()
    }

    @Test
    fun rustSinkPanicIsContainedAtTheExportedIngressBoundary() {
        val status = AtomicReference<EventIngressStatus>()
        val backend = TestBackend { token, command ->
            status.set(pushBackendEvent(token, scanStarted(command)))
        }
        val fixture = Phase0Fixture(backend)
        fixture.setSinkBehavior(FixtureSinkBehavior.PANIC)

        fixture.startScan(scanCommand(51uL))

        assertTrue(status.get() is EventIngressStatus.DownstreamFailed)
        assertEquals(0uL, fixture.snapshot().activeCallbacks)
        fixture.dispose()
        fixture.close()
    }

    @Test
    fun disposeWaitsForAnAdmittedRustSinkCallbackAndThenRejectsLatePush() {
        val backend = TestBackend()
        val fixture = Phase0Fixture(backend)
        val token = checkNotNull(backend.token.get())
        fixture.setSinkBehavior(FixtureSinkBehavior.BLOCK)
        val pushExecutor = Executors.newSingleThreadExecutor()
        val disposeExecutor = Executors.newSingleThreadExecutor()
        val push = pushExecutor.submit<EventIngressStatus> {
            pushBackendEvent(token, scanStarted(scanCommand(52uL)))
        }
        assertTrue(fixture.waitForBlockedCallback(5_000uL))

        val dispose = disposeExecutor.submit<Unit> { fixture.dispose() }
        Thread.sleep(100)
        assertFalse(dispose.isDone)
        fixture.releaseBlockedCallback()

        assertEquals(EventIngressStatus.Delivered, push.get(5, TimeUnit.SECONDS))
        dispose.get(5, TimeUnit.SECONDS)
        assertEquals(0uL, fixture.snapshot().activeCallbacks)
        assertEquals(
            EventIngressStatus.Closed,
            pushBackendEvent(token, scanStarted(scanCommand(53uL))),
        )
        fixture.close()
        pushExecutor.shutdownNow()
        disposeExecutor.shutdownNow()
    }

    @Test
    fun disposeWaitsForACommandHoldingTheBackendReadGate() {
        val commandEntered = CountDownLatch(1)
        val releaseCommand = CountDownLatch(1)
        val backend = TestBackend { _, _ ->
            commandEntered.countDown()
            assertTrue(releaseCommand.await(5, TimeUnit.SECONDS))
        }
        val fixture = Phase0Fixture(backend)
        val commandExecutor = Executors.newSingleThreadExecutor()
        val disposeExecutor = Executors.newSingleThreadExecutor()
        val command = commandExecutor.submit<Unit> {
            fixture.startScan(scanCommand(54uL))
        }
        assertTrue(commandEntered.await(5, TimeUnit.SECONDS))

        val dispose = disposeExecutor.submit<Unit> { fixture.dispose() }
        Thread.sleep(100)
        assertFalse(dispose.isDone)
        assertEquals(0, backend.disposeCount.get())
        releaseCommand.countDown()

        command.get(5, TimeUnit.SECONDS)
        dispose.get(5, TimeUnit.SECONDS)
        assertEquals(1, backend.disposeCount.get())
        fixture.close()
        commandExecutor.shutdownNow()
        disposeExecutor.shutdownNow()
    }

    @Test
    fun concurrentJvmPushesAreSerializedByTheRustIngressState() {
        val backend = TestBackend { token, command ->
            val executor = Executors.newFixedThreadPool(16)
            repeat(64) {
                executor.execute {
                    assertEquals(
                        EventIngressStatus.Delivered,
                        pushBackendEvent(token, scanStarted(command)),
                    )
                }
            }
            executor.shutdown()
            assertTrue(executor.awaitTermination(10, TimeUnit.SECONDS))
        }
        val fixture = Phase0Fixture(backend)

        fixture.startScan(scanCommand(6uL))

        val snapshot = fixture.snapshot()
        assertEquals(64uL, snapshot.eventCount)
        assertEquals(1uL, snapshot.maximumActiveCallbacks)
        fixture.dispose()
        fixture.close()
    }

    @Test
    fun typedDisposeFailureStillClosesAndReleasesTheBackend() {
        val expected = BleHubError.BackendCleanup("scripted dispose failure", null)
        val backend = TestBackend(
            onDispose = { throw expected },
        )
        val fixture = Phase0Fixture(backend)
        val token = checkNotNull(backend.token.get())

        val actual = assertFailsWith<BleHubError.BackendCleanup> {
            fixture.dispose()
        }

        assertEquals(expected, actual)
        assertEquals(1, backend.disposeCount.get())
        assertEquals(
            EventIngressStatus.Closed,
            pushBackendEvent(token, scanStarted(scanCommand(59uL))),
        )
        fixture.dispose()
        fixture.close()
    }

    @Test
    fun staleTokenCannotReachANewHub() {
        val oldBackend = TestBackend { _, _ -> Unit }
        val oldFixture = Phase0Fixture(oldBackend)
        val oldToken = checkNotNull(oldBackend.token.get())
        oldFixture.dispose()
        oldFixture.close()

        val newBackend = TestBackend { _, _ -> Unit }
        val newFixture = Phase0Fixture(newBackend)

        assertEquals(
            EventIngressStatus.Closed,
            pushBackendEvent(oldToken, scanStarted(scanCommand(60uL))),
        )
        assertEquals(0uL, newFixture.snapshot().eventCount)
        assertNotEquals(oldToken, newBackend.token.get())
        newFixture.dispose()
        newFixture.close()
    }

    @Test
    fun tenThousandLifecycleTokensAreUniqueAndCleanupRunsOncePerBackend() {
        val tokens = ConcurrentHashMap.newKeySet<ULong>()
        val disposeCount = AtomicInteger()

        repeat(10_000) { sequence ->
            val backend = TestBackend(
                onDispose = { disposeCount.incrementAndGet() },
            )
            val fixture = Phase0Fixture(backend)
            assertTrue(tokens.add(checkNotNull(backend.token.get()).value))
            fixture.startScan(scanCommand(sequence.toULong() + 1uL))
            fixture.dispose()
            fixture.close()
        }

        assertEquals(10_000, tokens.size)
        assertEquals(10_000, disposeCount.get())
    }

    @Test
    fun disposeIsIdempotentAndTheRetainedTokenBecomesClosed() {
        val backend = TestBackend { _, _ -> Unit }
        val fixture = Phase0Fixture(backend)
        val token = checkNotNull(backend.token.get())
        val sinkDropsBefore = fixture.snapshot().sinkDropCount

        fixture.dispose()
        fixture.dispose()

        assertEquals(1, backend.disposeCount.get())
        assertEquals(
            EventIngressStatus.Closed,
            pushBackendEvent(token, scanStarted(scanCommand(7uL))),
        )
        assertEquals(sinkDropsBefore + 1uL, fixture.snapshot().sinkDropCount)
        fixture.close()
    }

    private class TestBackend(
        private val capabilitiesProvider: () -> BackendCapabilities = {
            BackendCapabilities(Platform.TEST, fixtureContractVersion)
        },
        private val bindAction: ((BackendEventIngressToken) -> Unit)? = null,
        private val onDispose: () -> Unit = {},
        private val onStartScan: (BackendEventIngressToken, StartScanCommand) -> Unit = { _, _ -> },
    ) : BleBackend {
        val token = AtomicReference<BackendEventIngressToken>()
        val capabilitiesCount = AtomicInteger()
        val bindCount = AtomicInteger()
        val disposeCount = AtomicInteger()

        override fun capabilities(): BackendCapabilities {
            capabilitiesCount.incrementAndGet()
            return capabilitiesProvider()
        }

        override fun bindEventIngress(token: BackendEventIngressToken) {
            bindCount.incrementAndGet()
            bindAction?.invoke(token)
            check(this.token.compareAndSet(null, token))
        }

        override fun startScan(command: StartScanCommand) {
            onStartScan(checkNotNull(token.get()), command)
        }

        override fun connect(command: ConnectCommand) = Unit

        override fun dispose() {
            disposeCount.incrementAndGet()
            onDispose()
        }
    }

    companion object {
        private fun scanCommand(id: ULong) = StartScanCommand(
            commandId = CommandId(id),
            scanSessionId = ScanSessionId(id + 100uL),
        )

        private fun scanStarted(command: StartScanCommand) = BleEvent(
            BleEventPayload.ScanStarted(command.commandId, command.scanSessionId),
        )
    }
}
