package com.blehub.reactnative.bleng

import dev.blehub.BleEvent
import dev.blehub.CommandId
import dev.bleng.BlengException
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitCancellation
import kotlinx.coroutines.cancel
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.supervisorScope
import kotlinx.coroutines.withTimeout
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test

/** Observation windows are not stream termination or permission to repeat BLE commands. */
class BlengEventReaderTest {
    @Test
    fun observationTimeoutsDoNotEndTheReaderOrDuplicateDeliveredEvents() = runBlocking {
        val first = BleEvent.CommandCompleted(CommandId(1uL), null)
        val second = BleEvent.CommandCompleted(CommandId(2uL), null)
        val delivered = mutableListOf<BleEvent>()
        var reads = 0
        withTimeout(2_000L) {
            drainBlengEvents(
                nextEvent = {
                    when (++reads) {
                        1, 2, 4 -> throw BlengException.Timeout()
                        3 -> first
                        5 -> second
                        6 -> null
                        else -> error("read continued after EOF")
                    }
                },
                onEvent = { delivered += it },
            )
        }
        assertEquals(6, reads)
        assertEquals(listOf(first, second), delivered)
    }

    @Test
    fun eofEndsTheReaderWithoutAnAdditionalRead() = runBlocking {
        var reads = 0
        drainBlengEvents(
            nextEvent = { reads++; null },
            onEvent = { error("EOF must not deliver an event") },
        )
        assertEquals(1, reads)
    }

    @Test
    fun nonObservationReadFailuresPropagateWithoutAnotherRead() = runBlocking {
        for (primary in listOf(
            BlengException.TransportFault(),
            BlengException.WorkerStopped(),
            BlengException.InvalidTimeout(),
            CancellationException("reader cancelled"),
            IllegalStateException("unexpected reader failure"),
        )) {
            var reads = 0
            val failure = runCatching {
                drainBlengEvents(
                    nextEvent = { reads++; throw primary },
                    onEvent = { error("failed reads cannot deliver") },
                )
            }.exceptionOrNull()
            assertSame(primary, failure)
            assertEquals(1, reads)
        }
    }

    @Test
    fun aDeliveryTimeoutIsNotMistakenForAnIdleRead() = runBlocking {
        val primary = BlengException.Timeout()
        var reads = 0
        var deliveries = 0
        val failure = runCatching {
            drainBlengEvents(
                nextEvent = {
                    reads++
                    check(reads == 1) { "delivery failure was retried" }
                    BleEvent.CommandCompleted(CommandId(1uL), null)
                },
                onEvent = { deliveries++; throw primary },
            )
        }.exceptionOrNull()
        assertSame(primary, failure)
        assertEquals(1, reads)
        assertEquals(1, deliveries)
    }

    @Test
    fun cancellationWhileReadingDoesNotRestartTheReader() = runBlocking {
        val entered = CompletableDeferred<Unit>()
        var reads = 0
        var retirements = 0
        val reader = launch {
            try {
                drainBlengEvents(
                    nextEvent = { reads++; entered.complete(Unit); awaitCancellation() },
                    onEvent = { error("cancelled reader must not deliver") },
                )
            } finally {
                retirements++
            }
        }
        withTimeout(2_000L) {
            entered.await()
            reader.cancel()
            reader.join()
        }
        assertTrue(reader.isCancelled)
        assertFalse(reader.isActive)
        assertEquals(1, reads)
        assertEquals(1, retirements)
    }

    @Test
    fun cancellationAtAnObservationTimeoutStopsBeforeTheNextRead() = runBlocking {
        var reads = 0
        withTimeout(2_000L) {
            supervisorScope {
                val reader = async {
                    drainBlengEvents(
                        nextEvent = {
                            reads++
                            currentCoroutineContext().cancel()
                            throw BlengException.Timeout()
                        },
                        onEvent = { error("cancelled reader must not deliver") },
                    )
                }
                assertTrue(runCatching { reader.await() }.exceptionOrNull() is CancellationException)
            }
        }
        assertEquals(1, reads)
    }
}
