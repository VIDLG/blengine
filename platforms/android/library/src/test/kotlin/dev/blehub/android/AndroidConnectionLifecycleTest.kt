package dev.blehub.android

import dev.blehub.BleEvent
import dev.blehub.CommandDispatchException
import dev.blehub.CommandId
import dev.blehub.ConnectCommand
import dev.blehub.ConnectionId
import dev.blehub.ConnectionState
import dev.blehub.ConnectionStateResult
import dev.blehub.DiscoverGattCommand
import dev.blehub.GattCharacteristicId
import dev.blehub.GattSessionId
import dev.blehub.NotificationAction
import dev.blehub.NotificationMode
import dev.blehub.NotificationModeRequest
import dev.blehub.PeripheralId
import dev.blehub.SetNotificationsCommand
import dev.blehub.SubscriptionId
import dev.blehub.WriteCharacteristicCommand
import dev.blehub.WriteType
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertFalse
import kotlin.test.assertNull
import kotlin.test.assertSame
import kotlin.test.assertTrue

/** Uses the production admission/index/retirement owner; native calls and Handler scheduling are ports. */
class AndroidConnectionLifecycleTest {
    private enum class Head { RESPONSE_WRITE, NO_RESPONSE_WRITE, CCC }

    @Test
    fun lossRetiresOnlyItsOwnerAndExplicitAdmissionReusesPeerWithoutOldCallbackContamination() {
        for (status in listOf(0, 133)) {
            for (head in Head.entries) {
                val fixture = Fixture()
                val old = fixture.reserve(1u, 10u)
                val other = fixture.reserve(2u, 20u)
                synchronized(fixture.lock) {
                    assertFailsWith<CommandDispatchException.InvalidState> { fixture.reserve(3u, 10u) }
                    assertFailsWith<CommandDispatchException.InvalidRequest> { fixture.reserve(1u, 30u) }
                }
                fixture.connected(old, 100u)
                fixture.connected(other, 200u)
                val write = fixture.write(
                    old,
                    if (head == Head.NO_RESPONSE_WRITE) WriteType.WITHOUT_RESPONSE else WriteType.WITH_RESPONSE
                )
                val ccc = fixture.ccc(old)
                val ordered = if (head == Head.CCC) listOf(ccc, write) else listOf(write, ccc)
                ordered.forEach { assertTrue(old.pendingIo.enqueue(it)) }
                val otherWrite = fixture.write(other)
                assertTrue(other.pendingIo.enqueue(otherWrite))
                fixture.drive(old)
                fixture.drive(other)
                val oldWatchdog = fixture.watchdogs.getValue(old)
                val otherWatchdog = fixture.watchdogs.getValue(other)
                val beforeLoss = fixture.events.toList()
                assertEquals(if (head == Head.NO_RESPONSE_WRITE) 2 else 0, beforeLoss.size)
                assertTrue(old.activeWatchdog.isArmed)

                fixture.connections.disconnected(old, status)
                val activityError =
                    if (status == 0) connectionError("Android GATT connection ended during native activity")
                    else connectionError("Android GATT connection ended during native activity", status)
                val lost = BleEvent.ConnectionStateChanged(
                    null, old.command.connectionId,
                    if (status == 0) ConnectionStateResult.Ok(ConnectionState.DISCONNECTED)
                    else ConnectionStateResult.Err(
                        connectionError(
                            "Android GATT connection ended with status $status",
                            status
                        )
                    ),
                )
                val interrupted = ordered.filterNot { head == Head.NO_RESPONSE_WRITE && it === write }
                    .map { BleEvent.CommandCompleted(it.commandId, activityError) }
                val expected = beforeLoss + interrupted + lost
                assertEquals(expected, fixture.events)
                assertEquals(listOf(old), fixture.closed)
                assertEquals(listOf(oldWatchdog), fixture.removedWatchdogs)
                assertTrue(old.pendingIo.isEmpty)
                assertFalse(old.activeWatchdog.isArmed)
                assertNull(old.gattSessionId)
                assertTrue(old.subscriptionsById.isEmpty())
                assertEquals(listOf(other), fixture.connections.activeRoutes.toList())
                assertEquals(listOf(other, other), fixture.connections.ownedRoutes)
                assertSame(otherWrite, other.pendingIo.active())
                assertTrue(other.activeWatchdog.isArmed)
                assertFalse(otherWatchdog in fixture.removedWatchdogs)

                // Same backend owner, real admission into both indexes, no fixture replaceRoute().
                val fresh = fixture.reserve(3u, 10u)
                assertTrue(fresh.generation > old.generation)
                fixture.connected(fresh, 300u)
                val freshWrite = fixture.write(fresh, commandId = write.commandId)
                assertTrue(fresh.pendingIo.enqueue(freshWrite))
                fixture.drive(fresh)
                val freshWatchdog = fixture.watchdogs.getValue(fresh)
                fixture.callbacks.getValue(old).invoke()
                fixture.connections.disconnected(old, status)
                synchronized(fixture.lock) { assertFalse(fixture.connections.remove(old)) }
                assertEquals(expected, fixture.events)
                assertEquals(listOf(old), fixture.closed)
                assertSame(fresh, fixture.connections.find(3u))
                assertSame(freshWrite, fresh.pendingIo.active())
                assertTrue(fresh.activeWatchdog.isArmed)
                assertFalse(freshWatchdog in fixture.removedWatchdogs)
                assertFailsWith<CommandDispatchException.InvalidState> { fixture.reserve(4u, 10u) }

                fixture.callbacks.getValue(fresh).invoke()
                assertEquals(expected + fixture.writeSuccess(freshWrite), fixture.events)
                fixture.callbacks.getValue(other).invoke()
                assertEquals(
                    expected + fixture.writeSuccess(freshWrite) + fixture.writeSuccess(otherWrite),
                    fixture.events
                )
                assertTrue(fresh.pendingIo.isEmpty)
                assertTrue(other.pendingIo.isEmpty)
                assertEquals(3, fixture.removedWatchdogs.size)
                assertEquals(3, fixture.removedWatchdogs.toSet().size)
                synchronized(fixture.lock) {
                    assertTrue(fixture.connections.remove(fresh))
                    // Even caller-reused connection IDs cannot let an old route remove a new owner.
                    val reused = fixture.reserve(1u, 10u)
                    assertTrue(reused.generation > fresh.generation)
                    assertFalse(fixture.connections.remove(old))
                    assertSame(reused, fixture.connections.find(1u))
                }
            }
        }
    }

    @Test
    fun disconnectCallbackKeepsConnectAndExplicitDisconnectOutcomesDistinct() {
        for (status in listOf(0, 133)) {
            for (connected in listOf(false, true)) {
                val fixture = Fixture()
                val route = fixture.reserve(1u, 10u)
                if (connected) fixture.connected(route, 100u)
                route.disconnectCommandId = CommandId(99u)
                fixture.connections.disconnected(route, status)
                val error = if (status == 0) null else connectionError(
                    "Android GATT connection ended with status $status",
                    status
                )
                val expected = mutableListOf<BleEvent>()
                if (!connected) {
                    val connectError =
                        error ?: connectionError("Android GATT connection ended before reaching the connected state")
                    expected.add(
                        BleEvent.ConnectionStateChanged(
                            route.command.commandId,
                            route.command.connectionId,
                            ConnectionStateResult.Err(connectError)
                        )
                    )
                    expected.add(BleEvent.CommandCompleted(route.command.commandId, connectError))
                }
                expected.add(
                    BleEvent.ConnectionStateChanged(
                        CommandId(99u), route.command.connectionId,
                        if (error == null) ConnectionStateResult.Ok(ConnectionState.DISCONNECTED) else ConnectionStateResult.Err(
                            error
                        )
                    )
                )
                expected.add(BleEvent.CommandCompleted(CommandId(99u), error))
                assertEquals(expected, fixture.events)
                assertTrue(fixture.connections.ownedRoutes.isEmpty())
                fixture.connections.disconnected(route, status)
                assertEquals(expected, fixture.events)
                assertEquals(listOf(route), fixture.closed)
            }
        }
    }

    @Test
    fun liveConnectCancellationIsRejectedUntilCompletionOrRetirementWithoutHistory() {
        for (completes in listOf(false, true)) {
            val fixture = Fixture()
            val route = fixture.reserve(1u, 10u)
            synchronized(fixture.lock) {
                repeat(2) {
                    val error = assertFailsWith<CommandDispatchException.OperationNotCancellable> {
                        fixture.connections.cancelOperation(route.command.commandId)
                    }
                    assertEquals(route.command.commandId, error.targetCommandId)
                }
                assertSame(route, fixture.connections.find(1u))
                assertFalse(route.connectCompleted)
                if (completes) {
                    fixture.connected(route, 100u)
                    assertFailsWith<CommandDispatchException.OperationNotFound> {
                        fixture.connections.cancelOperation(route.command.commandId)
                    }
                }
                route.phase = ConnectionPhase.DISCONNECTING
                route.disconnectCommandId = CommandId(99u)
                if (!completes) {
                    assertFailsWith<CommandDispatchException.OperationNotCancellable> {
                        fixture.connections.cancelOperation(route.command.commandId)
                    }
                }
            }
            fixture.connections.disconnected(route, 0)
            synchronized(fixture.lock) {
                val error = assertFailsWith<CommandDispatchException.OperationNotFound> {
                    fixture.connections.cancelOperation(route.command.commandId)
                }
                assertEquals(route.command.commandId, error.targetCommandId)
                assertTrue(fixture.connections.ownedRoutes.isEmpty())
            }
        }
    }

    @Test
    fun liveDiscoveryCancellationIsRejectedWithoutTouchingItsWatchdogOrSibling() {
        val fixture = Fixture()
        val route = fixture.reserve(1u, 10u)
        val other = fixture.reserve(2u, 20u)
        fixture.connected(route, 100u)
        fixture.connected(other, 200u)
        val pending = PendingDiscovery(
            DiscoverGattCommand(CommandId(300u), route.command.connectionId, GattSessionId(301u))
        )
        route.pendingDiscovery = pending
        val watchdog = Runnable {}
        route.activeWatchdog.install(watchdog)
        val sibling = fixture.write(other)
        assertTrue(other.pendingIo.enqueue(sibling))
        fixture.drive(other)
        synchronized(fixture.lock) {
            repeat(2) {
                val error = assertFailsWith<CommandDispatchException.OperationNotCancellable> {
                    fixture.connections.cancelOperation(pending.command.commandId)
                }
                assertEquals(pending.command.commandId, error.targetCommandId)
            }
            val unknown = assertFailsWith<CommandDispatchException.OperationNotFound> {
                fixture.connections.cancelOperation(CommandId(999u))
            }
            assertEquals(CommandId(999u), unknown.targetCommandId)
            assertSame(pending, route.pendingDiscovery)
            assertTrue(route.activeWatchdog.isArmed)
            assertSame(sibling, other.pendingIo.active())
            assertTrue(fixture.removedWatchdogs.isEmpty())
            assertTrue(fixture.events.isEmpty())
            // Production retirement clears the discovery owner and watchdog together.
            fixture.connections.retireGattState(route, connectionError("discovery ended"))
            assertFailsWith<CommandDispatchException.OperationNotFound> {
                fixture.connections.cancelOperation(pending.command.commandId)
            }
            assertNull(route.pendingDiscovery)
            assertEquals(listOf(watchdog), fixture.removedWatchdogs)
            assertSame(sibling, other.pendingIo.active())
            assertTrue(other.activeWatchdog.isArmed)
        }
    }

    @Test
    fun cancellationStillRemovesOnlyQueuedIoAndNeverTheActiveHead() {
        val fixture = Fixture()
        val route = fixture.reserve(1u, 10u)
        fixture.connected(route, 100u)
        val active = fixture.write(route)
        val queued = fixture.write(route, commandId = CommandId(112u))
        assertTrue(route.pendingIo.enqueue(active))
        assertTrue(route.pendingIo.enqueue(queued))
        fixture.drive(route)
        synchronized(fixture.lock) {
            assertFailsWith<CommandDispatchException.OperationNotCancellable> {
                fixture.connections.cancelOperation(active.commandId)
            }
            assertEquals(2, route.pendingIo.size)
            fixture.connections.cancelOperation(queued.commandId)
            assertEquals(1, route.pendingIo.size)
            assertSame(active, route.pendingIo.active())
            assertFailsWith<CommandDispatchException.OperationNotFound> {
                fixture.connections.cancelOperation(queued.commandId)
            }
            assertTrue(route.activeWatchdog.isArmed)
            assertTrue(fixture.removedWatchdogs.isEmpty())
            assertTrue(fixture.events.isEmpty())
        }
    }

    private class Fixture {
        val lock = Any()
        val events = mutableListOf<BleEvent>()
        val closed = mutableListOf<ActiveConnection>()
        val removedWatchdogs = mutableListOf<Runnable>()

        // Route is mutable; identity maps must not use its data-class hashCode.
        val watchdogs = java.util.IdentityHashMap<ActiveConnection, Runnable>()
        val callbacks = java.util.IdentityHashMap<ActiveConnection, () -> Unit>()
        private val characteristic = Any()
        private val descriptor = Any()
        val connections: AndroidConnectionLifecycle = AndroidConnectionLifecycle(
            stateLock = lock,
            removeWatchdog = { removedWatchdogs.add(it) },
            closeGatt = {
                assertFalse(Thread.holdsLock(lock))
                assertFalse(connectionsActive(it))
                assertTrue(it.pendingIo.isEmpty)
                assertNull(it.gattSessionId)
                closed.add(it)
            },
            publish = {
                assertFalse(Thread.holdsLock(lock))
                events.add(it)
            },
        )

        private fun connectionsActive(route: ActiveConnection): Boolean =
            synchronized(lock) { connections.isActive(route) }

        fun reserve(id: ULong, peer: ULong): ActiveConnection = synchronized(lock) {
            connections.reserve(
                ConnectCommand(CommandId(100u + id), ConnectionId(id), PeripheralId(peer), false),
                "synthetic-peer-$peer"
            )
        }

        // A connected platform observation, not a simulated connectGatt result.
        fun connected(route: ActiveConnection, session: ULong) = synchronized(lock) {
            route.phase = ConnectionPhase.CONNECTED
            route.connectCompleted = true
            route.gattSessionId = GattSessionId(session)
            route.subscriptionsById[50u] =
                ActiveSubscription(SubscriptionId(50u), GattCharacteristicId(51u), Any(), NotificationMode.NOTIFICATION)
        }

        fun write(
            route: ActiveConnection,
            type: WriteType = WriteType.WITH_RESPONSE,
            commandId: CommandId = CommandId(route.command.connectionId.value * 100u + 11u),
        ) = PendingIo.Write(
            WriteCharacteristicCommand(
                commandId,
                route.command.connectionId,
                checkNotNull(route.gattSessionId),
                GattCharacteristicId(8u),
                byteArrayOf(1, 2),
                type
            ),
            characteristic,
        )

        fun ccc(route: ActiveConnection) = PendingIo.Notification(
            SetNotificationsCommand(
                CommandId(12u),
                route.command.connectionId,
                checkNotNull(route.gattSessionId),
                SubscriptionId(9u),
                GattCharacteristicId(8u),
                NotificationAction.Enable(NotificationModeRequest.PLATFORM_DEFAULT)
            ),
            characteristic, descriptor, NotificationMode.NOTIFICATION, true,
        )

        fun drive(route: ActiveConnection) {
            val operation = synchronized(lock) {
                val next = route.pendingIo.begin() ?: return
                val watchdog = object : Runnable {
                    override fun run() = Unit
                }
                assertNull(route.activeWatchdog.install(watchdog))
                watchdogs[route] = watchdog
                next
            }
            when (operation) {
                is PendingIo.Write<*> -> {
                    val writes = CharacteristicWriteLifecycle(
                        lock, route, { connectionsActive(route) },
                        { removedWatchdogs.add(it) }, { events.add(it) }, { drive(route) })
                    writes.submit(operation) {
                        callbacks[route] = { writes.onCallback(characteristic, 0) }
                        true
                    }
                }

                is PendingIo.Notification<*, *> -> {
                    val notifications = NotificationLifecycle(
                        lock, route, { connectionsActive(route) },
                        { _, _ -> true }, { true }, { _, _ -> error("unexpected compensation failure") },
                        { removedWatchdogs.add(it) }, { events.add(it) }, { drive(route) })
                    notifications.submit(operation)
                    callbacks[route] = { notifications.onCallback(descriptor, 0) }
                }

                else -> error("unexpected native operation")
            }
        }

        fun writeSuccess(write: PendingIo.Write<*>) = listOf(
            BleEvent.CharacteristicWritten(
                write.commandId,
                write.command.connectionId,
                write.command.gattSessionId,
                write.command.characteristicId
            ),
            BleEvent.CommandCompleted(write.commandId, null),
        )
    }
}
