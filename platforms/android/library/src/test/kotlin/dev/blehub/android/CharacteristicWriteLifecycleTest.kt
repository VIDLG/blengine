package dev.blehub.android

import dev.blehub.BleEvent
import dev.blehub.BleEventError
import dev.blehub.CommandId
import dev.blehub.ConnectCommand
import dev.blehub.ConnectionId
import dev.blehub.GattCharacteristicId
import dev.blehub.GattSessionId
import dev.blehub.PeripheralId
import dev.blehub.WriteCharacteristicCommand
import dev.blehub.WriteType
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNull
import kotlin.test.assertSame
import kotlin.test.assertTrue

/** Real write lifecycle/queue/watchdog transitions with injected native I/O, not a Framework/Handler test. */
class CharacteristicWriteLifecycleTest {
    @Test
    fun acceptanceRetainsHeadAndWatchdogUntilPostedCallbackBeforeSameCharacteristicReplacement() {
        for (firstStatus in listOf(0, 133)) {
            val fixture = Fixture()
            val first = fixture.enqueue(1u, WriteType.WITHOUT_RESPONSE)
            val next = fixture.enqueue(2u, WriteType.WITH_RESPONSE)
            fixture.submitNative = { pending, callback ->
                // Model AndroidGattCallback posting even if native completion occurs before return.
                fixture.callbacks.addLast { callback(if (pending === first) firstStatus else 0) }
                if (pending === first) {
                    assertTrue(fixture.events.isEmpty())
                    assertSame(first, fixture.route.pendingIo.active())
                    assertTrue(fixture.route.activeWatchdog.isArmed)
                }
                true
            }

            fixture.drive()
            assertEquals(listOf(written(first), completed(first)), fixture.events)
            assertEquals(listOf(first.commandId), fixture.submitted)
            val firstWatchdog = fixture.installedWatchdogs.single()
            assertSame(first, fixture.route.pendingIo.active())
            assertEquals(resources(pending = 2, watchdogs = 1, callbackRoutes = 2), fixture.snapshot())
            assertNull(fixture.route.pendingIo.removeQueued(first.commandId))
            fixture.drive() // Additional posted drive requests cannot bypass the retained head.
            fixture.lifecycleFor().onCallback(Any(), 0)
            fixture.lifecycleFor(gatt = Any()).onCallback(fixture.characteristic, 0)
            assertEquals(listOf(first.commandId), fixture.submitted)
            assertTrue(fixture.removedWatchdogs.isEmpty())

            fixture.deliverCallback()
            assertEquals(listOf(written(first), completed(first)), fixture.events) // Success or failure is drain-only.
            assertEquals(listOf(first.commandId, next.commandId), fixture.submitted)
            assertSame(next, fixture.route.pendingIo.active())
            assertEquals(listOf(firstWatchdog), fixture.removedWatchdogs)
            assertEquals(resources(pending = 1, watchdogs = 1, callbackRoutes = 2), fixture.snapshot())
            synchronized(fixture.lock) {
                assertFalse(fixture.route.activeWatchdog.takeIfCurrent(firstWatchdog))
            }
            assertTrue(fixture.route.activeWatchdog.isArmed)

            fixture.deliverCallback()
            assertEquals(listOf(written(first), completed(first), written(next), completed(next)), fixture.events)
            fixture.lifecycleFor().onCallback(fixture.characteristic, 0)
            fixture.lifecycleFor().onCallback(fixture.characteristic, 133)
            assertEquals(listOf(written(first), completed(first), written(next), completed(next)), fixture.events)
            assertEquals(fixture.installedWatchdogs, fixture.removedWatchdogs)
            assertEquals(resources(pending = 0, watchdogs = 0, callbackRoutes = 1), fixture.snapshot())
        }
    }

    @Test
    fun callbackBeforeSubmissionReturnsWinsExactlyOnceForBothModesAndOutcomes() {
        val returns: List<() -> Boolean> = listOf(
            { true }, { false }, { throw SecurityException("late denial") },
            { throw IllegalStateException("late rejection") },
        )
        for (writeType in WriteType.entries) {
            for (status in listOf(0, 133)) {
                for (submitResult in returns) {
                    val fixture = Fixture()
                    val pending = fixture.enqueue(1u, writeType)
                    val next = fixture.enqueue(2u, WriteType.WITH_RESPONSE)
                    fixture.submitNative = { submitted, callback ->
                        if (submitted === pending) {
                            callback(status)
                            submitResult()
                        } else {
                            fixture.callbacks.addLast { callback(0) }
                            true
                        }
                    }
                    fixture.drive()
                    val expected = if (status == 0) listOf(written(pending), completed(pending))
                    else listOf(
                        completed(
                            pending,
                            gattError("Android characteristic write ended with status 133", 133)
                        )
                    )
                    assertEquals(expected, fixture.events)
                    assertEquals(listOf(pending.commandId, next.commandId), fixture.submitted)
                    assertSame(next, fixture.route.pendingIo.active())
                    assertEquals(listOf(fixture.installedWatchdogs.first()), fixture.removedWatchdogs)
                    assertEquals(resources(pending = 1, watchdogs = 1, callbackRoutes = 2), fixture.snapshot())
                    fixture.deliverCallback()
                    val finished = expected + listOf(written(next), completed(next))
                    assertEquals(finished, fixture.events)
                    assertEquals(fixture.installedWatchdogs, fixture.removedWatchdogs)
                    assertEquals(resources(pending = 0, watchdogs = 0, callbackRoutes = 1), fixture.snapshot())
                    fixture.lifecycleFor().onCallback(fixture.characteristic, 0)
                    fixture.lifecycleFor().onCallback(fixture.characteristic, 133)
                    fixture.drive()
                    assertEquals(finished, fixture.events)
                    assertEquals(listOf(pending.commandId, next.commandId), fixture.submitted)
                }
            }
        }
    }

    @Test
    fun rejectionAndSubmissionExceptionsDrainOnceWithoutPublishingWriteSuccess() {
        val outcomes: List<Pair<() -> Boolean, BleEventError>> = listOf(
            { false } to gattError("Android rejected the characteristic write request"),
            { throw SecurityException("denied") } to permissionError("denied"),
            { throw IllegalStateException("unavailable") } to gattError("unavailable"),
        )
        for (writeType in WriteType.entries) {
            for ((submit, error) in outcomes) {
                val fixture = Fixture()
                val pending = fixture.enqueue(1u, writeType)
                fixture.submitNative = { _, _ -> submit() }
                fixture.drive()
                assertEquals(listOf(completed(pending, error)), fixture.events)
                assertEquals(listOf(pending.commandId), fixture.submitted)
                assertEquals(fixture.installedWatchdogs, fixture.removedWatchdogs)
                assertEquals(resources(pending = 0, watchdogs = 0, callbackRoutes = 1), fixture.snapshot())
                fixture.lifecycleFor().onCallback(fixture.characteristic, 0)
                assertEquals(listOf(completed(pending, error)), fixture.events)
            }
        }
    }

    @Test
    fun permissionFailureAdvancesAcceptedSiblingWithoutRetiringConnectionOwner() {
        for (siblingDenied in listOf(true, false)) {
            val fixture = Fixture()
            val owner = fixture.route
            val gatt = fixture.currentGatt
            val first = fixture.enqueue(1u, WriteType.WITH_RESPONSE)
            val sibling = fixture.enqueue(2u, WriteType.WITH_RESPONSE)
            fixture.submitNative = { pending, callback ->
                if (pending === first || siblingDenied) {
                    throw SecurityException("denied ${pending.commandId.value}")
                }
                fixture.callbacks.addLast { callback(0) }
                true
            }

            fixture.drive()
            val expected = mutableListOf(completed(first, permissionError("denied 1")))
            assertEquals(listOf(first.commandId, sibling.commandId), fixture.submitted)
            if (siblingDenied) {
                expected.add(completed(sibling, permissionError("denied 2")))
            } else {
                assertEquals(expected, fixture.events)
                assertSame(sibling, fixture.route.pendingIo.active())
                assertTrue(fixture.route.activeWatchdog.isArmed)
                fixture.deliverCallback()
                expected.addAll(listOf(written(sibling), completed(sibling)))
            }

            assertEquals(expected, fixture.events)
            assertEquals(fixture.installedWatchdogs, fixture.removedWatchdogs)
            assertEquals(resources(pending = 0, watchdogs = 0, callbackRoutes = 1), fixture.snapshot())
            assertSame(owner, fixture.currentRoute)
            assertSame(gatt, fixture.currentGatt)
            assertEquals(ConnectionPhase.CONNECTED, owner.phase)
            fixture.lifecycleFor().onCallback(fixture.characteristic, 0)
            fixture.lifecycleFor().onCallback(fixture.characteristic, 133)
            fixture.drive()
            assertEquals(expected, fixture.events)
            assertEquals(listOf(first.commandId, sibling.commandId), fixture.submitted)
        }
    }

    @Test
    fun responseWriteWaitsForCallbackAndPublishesExactFailureWithoutWrittenEvent() {
        val fixture = Fixture()
        val pending = fixture.enqueue(1u, WriteType.WITH_RESPONSE)
        fixture.submitNative = { _, callback ->
            fixture.callbacks.addLast { callback(13) }
            true
        }
        fixture.drive()
        assertTrue(fixture.events.isEmpty())
        assertEquals(resources(pending = 1, watchdogs = 1, callbackRoutes = 2), fixture.snapshot())
        fixture.deliverCallback()
        assertEquals(
            listOf(completed(pending, gattError("Android characteristic write ended with status 13", 13))),
            fixture.events,
        )
        assertEquals(fixture.installedWatchdogs, fixture.removedWatchdogs)
        assertTrue(fixture.route.pendingIo.isEmpty)
    }

    @Test
    fun disconnectDuringSubmissionKeepsAcceptedTerminalButLeavesNativeDrainToRetirement() {
        val fixture = Fixture()
        val first = fixture.enqueue(1u, WriteType.WITHOUT_RESPONSE)
        val queued = fixture.enqueue(2u, WriteType.WITH_RESPONSE)
        fixture.submitNative = { _, callback ->
            synchronized(fixture.lock) { fixture.route.phase = ConnectionPhase.DISCONNECTING }
            fixture.callbacks.addLast { callback(0) }
            true
        }
        fixture.drive()
        assertEquals(listOf(written(first), completed(first)), fixture.events)
        fixture.deliverCallback()
        assertSame(first, fixture.route.pendingIo.active())
        assertTrue(fixture.removedWatchdogs.isEmpty())
        assertEquals(listOf(first.commandId), fixture.submitted)

        val error = connectionError("disconnect")
        fixture.retire(error, error)
        assertEquals(listOf(written(first), completed(first), completed(queued, error)), fixture.events)
        assertEquals(fixture.installedWatchdogs, fixture.removedWatchdogs)
        assertTrue(fixture.route.pendingIo.isEmpty)
    }

    @Test
    fun disposalWinningDuringSubmissionSuppressesAcceptanceAndCallbackPublication() {
        val fixture = Fixture()
        val first = fixture.enqueue(1u, WriteType.WITHOUT_RESPONSE)
        val queued = fixture.enqueue(2u, WriteType.WITH_RESPONSE)
        fixture.submitNative = { _, callback ->
            synchronized(fixture.lock) {
                fixture.lifecycle = Lifecycle.CLOSING
                fixture.currentRoute = null
            }
            fixture.callbacks.addLast { callback(0) }
            true
        }
        fixture.drive()
        assertTrue(fixture.events.isEmpty())
        assertSame(first, fixture.route.pendingIo.active())
        assertTrue(fixture.route.activeWatchdog.isArmed)
        fixture.deliverCallback()
        assertTrue(fixture.events.isEmpty())
        assertTrue(fixture.removedWatchdogs.isEmpty())

        val error = operationAbortedError("disposal")
        assertEquals(
            listOf(completed(first, error), completed(queued, error)),
            fixture.retire(error, error, publish = false),
        )
        assertTrue(fixture.events.isEmpty()) // Backend disposal discards the projection.
        assertEquals(fixture.installedWatchdogs, fixture.removedWatchdogs)
        assertTrue(fixture.route.pendingIo.isEmpty)
        fixture.drive()
        assertEquals(listOf(first.commandId), fixture.submitted)
    }

    @Test
    fun retirementSuppressesLocalTerminalAndOldRouteCallbackCannotCompleteReusedWrite() {
        val aborted = operationAbortedError("recovery")
        val connection = connectionError("disconnect")
        for ((activeError, queuedError, publishRetirement) in listOf(
            Triple(operationTimeoutError("watchdog"), aborted, true),
            Triple(connection, connection, true),
            Triple(aborted, aborted, false),
        )) {
            val fixture = Fixture()
            val first = fixture.enqueue(1u, WriteType.WITHOUT_RESPONSE)
            val second = fixture.enqueue(2u, WriteType.WITH_RESPONSE)
            val third = fixture.enqueue(3u, WriteType.WITHOUT_RESPONSE)
            fixture.drive()
            assertEquals(
                listOf(completed(second, queuedError), completed(third, queuedError)),
                fixture.retire(activeError, queuedError, publishRetirement),
            )
            val expected = mutableListOf<BleEvent>(written(first), completed(first))
            if (publishRetirement) {
                expected.add(completed(second, queuedError))
                expected.add(completed(third, queuedError))
            }
            assertEquals(expected, fixture.events)
            assertFalse(fixture.route.activeWatchdog.isArmed)

            fixture.replaceRoute()
            val replacement = fixture.enqueue(1u, WriteType.WITH_RESPONSE)
            fixture.drive()
            val removedBeforeStaleCallback = fixture.removedWatchdogs.toList()
            fixture.deliverCallback() // Old route/GATT, same native characteristic and command ID.
            assertEquals(expected, fixture.events)
            assertSame(replacement, fixture.route.pendingIo.active())
            assertTrue(fixture.route.activeWatchdog.isArmed)
            assertEquals(removedBeforeStaleCallback, fixture.removedWatchdogs)
            fixture.deliverCallback()
            assertEquals(expected + listOf(written(replacement), completed(replacement)), fixture.events)
            assertEquals(fixture.installedWatchdogs, fixture.removedWatchdogs)
        }
    }

    private class Fixture {
        val lock = Any()
        var route = route(1)
        var currentRoute: ActiveConnection? = route
        var currentGatt = Any()
        var lifecycle = Lifecycle.READY
        val characteristic = Any()
        val events = mutableListOf<BleEvent>()
        val submitted = mutableListOf<CommandId>()
        val callbacks = ArrayDeque<() -> Unit>()
        val installedWatchdogs = mutableListOf<Runnable>()
        val removedWatchdogs = mutableListOf<Runnable>()
        var submitNative: (PendingIo.Write<*>, (Int) -> Unit) -> Boolean = { _, callback ->
            callbacks.addLast { callback(0) }
            true
        }

        fun enqueue(id: ULong, type: WriteType): PendingIo.Write<Any> {
            val pending = PendingIo.Write(
                WriteCharacteristicCommand(
                    commandId = CommandId(id),
                    connectionId = route.command.connectionId,
                    gattSessionId = GattSessionId(9u),
                    characteristicId = GattCharacteristicId(10u),
                    value = byteArrayOf(1, 2),
                    writeType = type,
                ),
                characteristic,
            )
            synchronized(lock) { assertTrue(route.pendingIo.enqueue(pending)) }
            return pending
        }

        // Only queue admission/Handler scheduling is supplied by this fixture, not write outcomes.
        fun drive() {
            assertFalse(Thread.holdsLock(lock))
            val owner = route
            val write = synchronized(lock) {
                if (lifecycle != Lifecycle.READY || currentRoute !== owner ||
                    owner.phase != ConnectionPhase.CONNECTED
                ) return
                val pending = owner.pendingIo.begin() as? PendingIo.Write<*> ?: return
                val watchdog = object : Runnable {
                    override fun run() = Unit
                }
                assertNull(owner.activeWatchdog.install(watchdog))
                installedWatchdogs.add(watchdog)
                pending
            }
            val writes = lifecycleFor(owner, currentGatt)
            writes.submit(write) {
                assertFalse(Thread.holdsLock(lock))
                submitted.add(write.commandId)
                submitNative(write) { status -> writes.onCallback(write.characteristic, status) }
            }
        }

        fun lifecycleFor(owner: ActiveConnection = route, gatt: Any = currentGatt) =
            CharacteristicWriteLifecycle(
                stateLock = lock,
                route = owner,
                ownsRoute = {
                    assertTrue(Thread.holdsLock(lock))
                    lifecycle != Lifecycle.CLOSING && lifecycle != Lifecycle.CLOSED &&
                            currentRoute === owner && currentGatt === gatt
                },
                removeWatchdog = {
                    assertFalse(Thread.holdsLock(lock))
                    removedWatchdogs.add(it)
                },
                publish = {
                    assertFalse(Thread.holdsLock(lock))
                    events.add(it)
                },
                driveQueue = ::drive,
            )

        fun deliverCallback() = callbacks.removeFirst().invoke()

        fun snapshot() = synchronized(lock) { connectionRfResourceCounts(listOf(route)) }

        // Exercise the actual retirement projection; native disconnect/close is outside this test.
        fun retire(active: BleEventError, queued: BleEventError, publish: Boolean = true): List<BleEvent> {
            val (terminals, watchdog) = synchronized(lock) {
                retirePendingIo(route.pendingIo, active, queued) to route.activeWatchdog.clear()
            }
            watchdog?.let(removedWatchdogs::add)
            if (publish) events.addAll(terminals)
            return terminals
        }

        fun replaceRoute() = synchronized(lock) {
            route = route(route.generation + 1)
            currentRoute = route
            currentGatt = Any()
        }

        private fun route(generation: Long) = ActiveConnection(
            generation = generation,
            command = ConnectCommand(CommandId(100u), ConnectionId(7u), PeripheralId(8u), false),
            address = "synthetic-write-peer",
            phase = ConnectionPhase.CONNECTED,
            connectCompleted = true,
        )
    }

    private fun completed(pending: PendingIo.Write<*>, error: BleEventError? = null): BleEvent =
        BleEvent.CommandCompleted(pending.commandId, error)

    private fun written(pending: PendingIo.Write<*>): BleEvent = BleEvent.CharacteristicWritten(
        commandId = pending.commandId,
        connectionId = pending.command.connectionId,
        gattSessionId = pending.command.gattSessionId,
        characteristicId = pending.command.characteristicId,
    )

    private fun resources(pending: Int, watchdogs: Int, callbackRoutes: Int) = mapOf(
        "connections" to 1,
        "gattAttributes" to 0,
        "subscriptions" to 0,
        "pendingIo" to pending,
        "watchdogs" to watchdogs,
        "callbackRoutes" to callbackRoutes,
    )
}
