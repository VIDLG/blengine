package dev.blehub.spike

import java.util.Collections
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicLong
import kotlin.test.Test
import kotlin.test.assertContentEquals
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertFalse
import kotlin.test.assertTrue

class LifecycleSpikeTest {
    @Test
    fun retainsKotlinBackendAndSinkAfterBuilderCloses() {
        val events = Collections.synchronizedList(mutableListOf<BleEvent>())
        val disposeCount = AtomicInteger()
        val hub = createRetainedHub(events, disposeCount)

        hub.startScan(StartScanCommand(41u, 7u))

        assertEquals(1, events.size)
        assertEquals(41u, events.single().commandId)
        assertContentEquals(byteArrayOf(1, 2, 3), events.single().payload)
        hub.dispose()
        hub.close()
        assertEquals(1, disposeCount.get())
    }

    @Test
    fun synchronousPushCompletesBeforeCommandReturns() {
        val commandReturned = AtomicBoolean(false)
        val callbackSeen = AtomicBoolean(false)
        val builder = BleHubBuilder(BleEventSink {
            assertFalse(commandReturned.get())
            callbackSeen.set(true)
        })
        val ingress = builder.backendEventSink()
        val backend = object : BleBackend {
            override fun startScan(command: StartScanCommand) {
                assertEquals(
                    EventIngressStatus.DELIVERED,
                    ingress.push(BleEvent(1u, command.commandId, byteArrayOf(9))),
                )
            }

            override fun dispose() = Unit
        }
        val hub = builder.finish(backend)

        hub.startScan(StartScanCommand(1u, 1u))
        commandReturned.set(true)

        assertTrue(callbackSeen.get())
        hub.dispose()
        ingress.close()
        hub.close()
        builder.close()
    }

    @Test
    fun asyncPushFromJvmWorkerThreadReachesSink() {
        val callback = CountDownLatch(1)
        val executor = Executors.newSingleThreadExecutor()
        val builder = BleHubBuilder(BleEventSink { callback.countDown() })
        val ingress = builder.backendEventSink()
        val backend = object : BleBackend {
            override fun startScan(command: StartScanCommand) {
                executor.execute {
                    ingress.push(BleEvent(1u, command.commandId, byteArrayOf(5)))
                }
            }

            override fun dispose() = Unit
        }
        val hub = builder.finish(backend)

        hub.startScan(StartScanCommand(2u, 1u))

        assertTrue(callback.await(5, TimeUnit.SECONDS))
        hub.dispose()
        ingress.close()
        hub.close()
        builder.close()
        executor.shutdownNow()
    }

    @Test
    fun declaredDispatchErrorCrossesKotlinRustBoundary() {
        val builder = BleHubBuilder(BleEventSink { })
        val ingress = builder.backendEventSink()
        val backend = object : BleBackend {
            override fun startScan(command: StartScanCommand) {
                throw CommandDispatchError.Rejected
            }

            override fun dispose() = Unit
        }
        val hub = builder.finish(backend)

        assertFailsWith<CommandDispatchError.Rejected> {
            hub.startScan(StartScanCommand(3u, 1u))
        }

        hub.dispose()
        ingress.close()
        hub.close()
        builder.close()
    }

    @Test
    fun declaredSinkErrorMapsToDownstreamFailed() {
        val builder = BleHubBuilder(BleEventSink {
            throw EventSinkError.Rejected
        })
        val ingress = builder.backendEventSink()
        val backend = object : BleBackend {
            override fun startScan(command: StartScanCommand) = Unit

            override fun dispose() = Unit
        }
        val hub = builder.finish(backend)

        assertEquals(
            EventIngressStatus.DOWNSTREAM_FAILED,
            ingress.push(BleEvent(1u, null, byteArrayOf(1))),
        )

        hub.dispose()
        ingress.close()
        hub.close()
        builder.close()
    }

    @Test
    fun catchAllSinkAdapterCanMapUncheckedExceptionToDeclaredError() {
        val unsafeSink = BleEventSink {
            error("undeclared sink failure")
        }
        val guardedSink = BleEventSink { event ->
            try {
                unsafeSink.onEvent(event)
            } catch (_: Throwable) {
                throw EventSinkError.UnexpectedCallback
            }
        }
        val builder = BleHubBuilder(guardedSink)
        val ingress = builder.backendEventSink()
        val backend = object : BleBackend {
            override fun startScan(command: StartScanCommand) = Unit

            override fun dispose() = Unit
        }
        val hub = builder.finish(backend)

        assertEquals(
            EventIngressStatus.DOWNSTREAM_FAILED,
            ingress.push(BleEvent(1u, null, byteArrayOf(1))),
        )

        hub.dispose()
        ingress.close()
        hub.close()
        builder.close()
    }

    @Test
    fun latePushReturnsClosedAndDisposeIsExactlyOnce() {
        val disposeCount = AtomicInteger()
        val builder = BleHubBuilder(BleEventSink { })
        val ingress = builder.backendEventSink()
        val backend = object : BleBackend {
            override fun startScan(command: StartScanCommand) = Unit

            override fun dispose() {
                disposeCount.incrementAndGet()
            }
        }
        val hub = builder.finish(backend)

        hub.dispose()
        hub.dispose()

        assertEquals(1, disposeCount.get())
        assertEquals(
            EventIngressStatus.CLOSED,
            ingress.push(BleEvent(1u, null, byteArrayOf())),
        )
        ingress.close()
        ingress.close()
        hub.close()
        hub.close()
        builder.close()
        builder.close()
    }

    @Test
    fun concurrentPushesAreSerializedBeforeCallingSink() {
        val activeCallbacks = AtomicInteger()
        val maximumActiveCallbacks = AtomicInteger()
        val delivered = AtomicInteger()
        val builder = BleHubBuilder(BleEventSink {
            val active = activeCallbacks.incrementAndGet()
            maximumActiveCallbacks.accumulateAndGet(active, ::maxOf)
            Thread.sleep(1)
            delivered.incrementAndGet()
            activeCallbacks.decrementAndGet()
        })
        val ingress = builder.backendEventSink()
        val backend = object : BleBackend {
            override fun startScan(command: StartScanCommand) {
                val executor = Executors.newFixedThreadPool(8)
                repeat(64) { sequence ->
                    executor.execute {
                        ingress.push(BleEvent(sequence.toULong(), command.commandId, byteArrayOf(1)))
                    }
                }
                executor.shutdown()
                assertTrue(executor.awaitTermination(10, TimeUnit.SECONDS))
            }

            override fun dispose() = Unit
        }
        val hub = builder.finish(backend)

        hub.startScan(StartScanCommand(4u, 1u))

        assertEquals(64, delivered.get())
        assertEquals(1, maximumActiveCallbacks.get())
        hub.dispose()
        ingress.close()
        hub.close()
        builder.close()
    }

    @Test
    fun rustCreatedThreadAttachesToJvmAndCallsSink() {
        val callerThread = Thread.currentThread().id
        val callbackThread = AtomicLong(callerThread)
        val builder = BleHubBuilder(BleEventSink {
            callbackThread.set(Thread.currentThread().id)
        })
        val ingress = builder.backendEventSink()
        val backend = object : BleBackend {
            override fun startScan(command: StartScanCommand) = Unit

            override fun dispose() = Unit
        }
        val hub = builder.finish(backend)

        assertEquals(
            EventIngressStatus.DELIVERED,
            ingress.pushFromRustThread(BleEvent(1u, null, byteArrayOf(1))),
        )
        assertTrue(callbackThread.get() != callerThread)
        hub.dispose()
        ingress.close()
        hub.close()
        builder.close()
    }

    @Test
    fun unexpectedBackendExceptionMapsToContractError() {
        val callbackInvoked = AtomicBoolean(false)
        val builder = BleHubBuilder(BleEventSink { })
        val ingress = builder.backendEventSink()
        val backend = object : BleBackend {
            override fun startScan(command: StartScanCommand) {
                callbackInvoked.set(true)
                error("undeclared backend failure")
            }

            override fun dispose() = Unit
        }
        val hub = builder.finish(backend)

        assertFailsWith<CommandDispatchError.UnexpectedCallback> {
            hub.startScan(StartScanCommand(5u, 1u))
        }

        assertTrue(callbackInvoked.get())
        hub.dispose()
        ingress.close()
        hub.close()
        builder.close()
    }

    @Test
    fun catchAllBackendAdapterCanMapUnexpectedExceptionToDeclaredError() {
        val builder = BleHubBuilder(BleEventSink { })
        val ingress = builder.backendEventSink()
        val backend = object : BleBackend {
            override fun startScan(command: StartScanCommand) {
                try {
                    error("undeclared backend failure")
                } catch (_: Throwable) {
                    throw CommandDispatchError.UnexpectedCallback
                }
            }

            override fun dispose() = Unit
        }
        val hub = builder.finish(backend)

        assertFailsWith<CommandDispatchError.UnexpectedCallback> {
            hub.startScan(StartScanCommand(6u, 1u))
        }

        hub.dispose()
        ingress.close()
        hub.close()
        builder.close()
    }

    @Test
    fun disposeWaitsForInFlightSinkAndRejectsLatePush() {
        val callbackEntered = CountDownLatch(1)
        val releaseCallback = CountDownLatch(1)
        val disposeReturned = CountDownLatch(1)
        val builder = BleHubBuilder(BleEventSink {
            callbackEntered.countDown()
            assertTrue(releaseCallback.await(5, TimeUnit.SECONDS))
        })
        val ingress = builder.backendEventSink()
        val backend = object : BleBackend {
            override fun startScan(command: StartScanCommand) = Unit

            override fun dispose() = Unit
        }
        val hub = builder.finish(backend)
        val pushExecutor = Executors.newSingleThreadExecutor()
        val disposeExecutor = Executors.newSingleThreadExecutor()
        val pushResult = pushExecutor.submit<EventIngressStatus> {
            ingress.push(BleEvent(1u, null, byteArrayOf(1)))
        }
        assertTrue(callbackEntered.await(5, TimeUnit.SECONDS))

        disposeExecutor.execute {
            hub.dispose()
            disposeReturned.countDown()
        }

        assertFalse(disposeReturned.await(100, TimeUnit.MILLISECONDS))
        releaseCallback.countDown()
        assertTrue(disposeReturned.await(5, TimeUnit.SECONDS))
        assertEquals(EventIngressStatus.DELIVERED, pushResult.get(5, TimeUnit.SECONDS))
        assertEquals(
            EventIngressStatus.CLOSED,
            ingress.push(BleEvent(2u, null, byteArrayOf(2))),
        )
        pushExecutor.shutdownNow()
        disposeExecutor.shutdownNow()
        ingress.close()
        hub.close()
        builder.close()
    }

    @Test
    fun representativePayloadsSurviveSustainedPushes() {
        val eventCount = AtomicInteger()
        val byteCount = AtomicInteger()
        val builder = BleHubBuilder(BleEventSink { event ->
            eventCount.incrementAndGet()
            byteCount.addAndGet(event.payload.size)
        })
        val ingress = builder.backendEventSink()
        val backend = object : BleBackend {
            override fun startScan(command: StartScanCommand) = Unit

            override fun dispose() = Unit
        }
        val hub = builder.finish(backend)

        for (size in listOf(20, 244, 1024)) {
            repeat(1_000) { sequence ->
                assertEquals(
                    EventIngressStatus.DELIVERED,
                    ingress.push(BleEvent(sequence.toULong(), null, ByteArray(size))),
                )
            }
        }

        assertEquals(3_000, eventCount.get())
        assertEquals(1_288_000, byteCount.get())
        hub.dispose()
        ingress.close()
        hub.close()
        builder.close()
    }

    @Test
    fun tenThousandAssemblyAndDisposeCyclesComplete() {
        val disposeCount = AtomicInteger()
        repeat(10_000) {
            val builder = BleHubBuilder(BleEventSink { })
            val ingress = builder.backendEventSink()
            val backend = object : BleBackend {
                override fun startScan(command: StartScanCommand) = Unit

                override fun dispose() {
                    disposeCount.incrementAndGet()
                }
            }
            val hub = builder.finish(backend)
            hub.dispose()
            ingress.close()
            hub.close()
            builder.close()
        }

        assertEquals(10_000, disposeCount.get())
    }

    private fun createRetainedHub(
        events: MutableList<BleEvent>,
        disposeCount: AtomicInteger,
    ): BleHub {
        val builder = BleHubBuilder(BleEventSink(events::add))
        val ingress = builder.backendEventSink()
        val backend = object : BleBackend {
            override fun startScan(command: StartScanCommand) {
                ingress.push(BleEvent(1u, command.commandId, byteArrayOf(1, 2, 3)))
            }

            override fun dispose() {
                disposeCount.incrementAndGet()
                ingress.close()
            }
        }
        val hub = builder.finish(backend)
        builder.close()
        return hub
    }
}
