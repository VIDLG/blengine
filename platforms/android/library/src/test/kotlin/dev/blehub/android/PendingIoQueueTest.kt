package dev.blehub.android

import dev.blehub.BleEvent
import dev.blehub.CommandId
import dev.blehub.ConnectionId
import dev.blehub.ReadRssiCommand
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNull
import kotlin.test.assertSame
import kotlin.test.assertTrue

/** Production queue/terminal/retirement helpers only; no Android Framework callback execution. */
class PendingIoQueueTest {
    @Test
    fun onlyTheHeadCanBecomeActiveAndCompletionAdvancesFifo() {
        val queue = PendingIoQueue()
        val first = rssi(1u)
        val second = rssi(2u)

        assertTrue(queue.enqueue(first))
        assertTrue(queue.enqueue(second))
        assertEquals(2, queue.size)
        assertSame(first, queue.begin())
        assertEquals(2, queue.size) // Starting native work must not hide the active head.
        assertSame(first, queue.active())
        assertNull(queue.begin())
        assertFalse(queue.complete(second))
        assertTrue(queue.complete(first))
        assertEquals(1, queue.size)
        assertSame(second, queue.begin())
    }

    @Test
    fun queuedRemovalCannotTouchTheActiveHead() {
        val queue = PendingIoQueue()
        val active = rssi(1u)
        val queued = rssi(2u)

        queue.enqueue(active)
        queue.enqueue(queued)
        queue.begin()

        assertNull(queue.removeQueued(active.commandId))
        assertSame(queued, queue.removeQueued(queued.commandId))
        assertSame(active, queue.active())
        assertTrue(queue.complete(active))
        assertTrue(queue.isEmpty)
    }

    @Test
    fun duplicateCommandsAndCapacityAreRejectedWithoutChangingTheLane() {
        val queue = PendingIoQueue(capacity = 1)
        val operation = rssi(1u)

        assertTrue(queue.enqueue(operation))
        assertFalse(queue.enqueue(operation))
        assertFalse(queue.enqueue(rssi(2u)))
        assertNull(queue.active())
        assertSame(operation, queue.begin())
        assertFalse(queue.complete(rssi(1u)))
        assertTrue(queue.complete(operation))
    }

    @Test
    fun retirementReturnsEveryEntryAndInvalidatesTheActiveClaim() {
        val queue = PendingIoQueue()
        val first = rssi(1u)
        val second = rssi(2u)
        val third = rssi(3u)

        queue.enqueue(first)
        queue.enqueue(second)
        queue.enqueue(third)
        queue.begin()

        assertEquals(3, queue.size)
        assertEquals(listOf(first, second, third), queue.retire())
        assertEquals(0, queue.size)
        assertTrue(queue.isEmpty)
        assertNull(queue.active())
        assertFalse(queue.complete(first))

        val replacement = rssi(1u)
        val following = rssi(2u)
        // Reused IDs and equal DTOs must not restore a retired operation's ownership.
        assertEquals(first, replacement)
        assertEquals(second, following)
        assertTrue(queue.enqueue(replacement))
        assertTrue(queue.enqueue(following))
        assertFalse(queue.complete(first))
        assertSame(replacement, queue.begin())
        assertFalse(queue.isActive(first))
        assertTrue(queue.isActive(replacement))
        assertFalse(queue.complete(first))
        assertSame(replacement, queue.active())
        assertEquals(2, queue.size)

        assertTrue(queue.complete(replacement))
        assertSame(following, queue.begin())
        assertFalse(queue.complete(second))
        assertSame(following, queue.active())
        assertEquals(1, queue.size)
        assertTrue(queue.complete(following))
        assertTrue(queue.isEmpty)
        assertEquals(emptyList(), queue.retire())
    }

    @Test
    fun claimedCommandTerminalKeepsNativeHeadUntilDrainBeforeReplacement() {
        val queue = PendingIoQueue(capacity = 2)
        val accepted = rssi(1u)
        val replacement = rssi(2u)
        assertTrue(queue.enqueue(accepted))
        assertTrue(queue.enqueue(replacement))
        assertSame(accepted, queue.begin())

        // Exercise the same terminal claim used for accepted no-response writes.
        assertTrue(accepted.claimCommandTerminal())
        assertFalse(accepted.claimCommandTerminal())
        assertEquals(2, queue.size)
        assertSame(accepted, queue.active())
        assertTrue(queue.isActive(accepted.commandId))
        assertTrue(queue.contains(accepted.commandId))
        assertNull(queue.begin())
        assertNull(queue.removeQueued(accepted.commandId))
        assertFalse(queue.enqueue(rssi(1u)))
        assertFalse(queue.enqueue(rssi(3u)))
        assertFalse(queue.complete(replacement))

        assertTrue(queue.complete(accepted))
        assertFalse(accepted.claimCommandTerminal()) // Native drain owes no second terminal.
        assertEquals(1, queue.size)
        assertSame(replacement, queue.begin())
        assertFalse(queue.complete(accepted))
        assertSame(replacement, queue.active())
        assertTrue(queue.complete(replacement))
        assertTrue(replacement.claimCommandTerminal())
        assertTrue(queue.isEmpty)
    }

    @Test
    fun retirementClassifiesOnlyTheNativeActiveOwnerAsTimeoutAndPreservesFifo() {
        val queue = PendingIoQueue()
        val active = rssi(1u)
        val second = rssi(2u)
        val third = rssi(3u)
        listOf(active, second, third).forEach { assertTrue(queue.enqueue(it)) }
        assertSame(active, queue.begin())
        val timeout = operationTimeoutError("active native callback missing")
        val aborted = operationAbortedError("connection recovery")

        assertEquals(
            listOf(
                BleEvent.CommandCompleted(active.commandId, timeout),
                BleEvent.CommandCompleted(second.commandId, aborted),
                BleEvent.CommandCompleted(third.commandId, aborted),
            ),
            retirePendingIo(queue, timeout, aborted),
        )
        assertTrue(queue.isEmpty)
        assertNull(queue.active())
        assertFalse(queue.complete(active))
        assertFalse(active.claimCommandTerminal())
        assertFalse(second.claimCommandTerminal())
        assertFalse(third.claimCommandTerminal())
        assertEquals(emptyList(), retirePendingIo(queue, timeout, aborted))
    }

    @Test
    fun retirementSuppressesClaimedHeadForWatchdogDisconnectAndDisposalProjection() {
        val aborted = operationAbortedError("connection recovery or disposal")
        val connection = connectionError("disconnect")
        val errors = listOf(
            operationTimeoutError("native callback missing") to aborted,
            connection to connection,
            aborted to aborted,
        )
        for ((activeError, queuedError) in errors) {
            val queue = PendingIoQueue()
            val accepted = rssi(1u)
            val second = rssi(2u)
            val third = rssi(3u)
            listOf(accepted, second, third).forEach { assertTrue(queue.enqueue(it)) }
            assertSame(accepted, queue.begin())
            assertTrue(accepted.claimCommandTerminal())

            // Disposal discards this projection in the backend; it does not publish it.
            assertEquals(
                listOf(
                    BleEvent.CommandCompleted(second.commandId, queuedError),
                    BleEvent.CommandCompleted(third.commandId, queuedError),
                ),
                retirePendingIo(queue, activeError, queuedError),
            )
            assertEquals(0, queue.size)
            assertNull(queue.active())
            assertFalse(queue.complete(accepted))
            assertEquals(emptyList(), retirePendingIo(queue, activeError, queuedError))

            val reused = rssi(1u)
            assertEquals(accepted, reused) // Terminal state must not become DTO identity.
            assertTrue(queue.enqueue(reused))
            assertSame(reused, queue.begin())
            assertFalse(queue.complete(accepted))
            assertEquals(
                listOf(BleEvent.CommandCompleted(reused.commandId, activeError)),
                retirePendingIo(queue, activeError, queuedError),
            )
        }
    }

    private fun rssi(commandId: ULong): PendingIo.Rssi =
        PendingIo.Rssi(
            ReadRssiCommand(
                commandId = CommandId(commandId),
                connectionId = ConnectionId(7u),
            ),
        )
}
