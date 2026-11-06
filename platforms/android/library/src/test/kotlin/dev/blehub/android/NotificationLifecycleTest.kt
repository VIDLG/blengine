package dev.blehub.android

import dev.blehub.BleEvent
import dev.blehub.BleEventError
import dev.blehub.CommandId
import dev.blehub.ConnectCommand
import dev.blehub.ConnectionId
import dev.blehub.GattCharacteristicId
import dev.blehub.GattSessionId
import dev.blehub.NotificationAction
import dev.blehub.NotificationMode
import dev.blehub.NotificationModeRequest
import dev.blehub.NotificationState
import dev.blehub.PeripheralId
import dev.blehub.SetNotificationsCommand
import dev.blehub.SubscriptionId
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNull
import kotlin.test.assertSame
import kotlin.test.assertTrue

/** Production notification lifecycle with controlled native ports; no Framework/Handler or RF execution. */
class NotificationLifecycleTest {
    @Test
    fun enableAndDisableCommitOnlyAfterPositiveDescriptorCallbackWithoutCompensation() {
        for (enabling in listOf(true, false)) {
            for (mode in NotificationMode.entries) {
                val fixture = Fixture(enabling, mode)
                val pending = fixture.enqueue()
                fixture.drive()
                assertEquals(submissionTrace(enabling), fixture.trace)
                assertTrue(fixture.events.isEmpty())
                assertSame(pending, fixture.route.pendingIo.active())
                assertTrue(fixture.route.activeWatchdog.isArmed)
                assertSame(fixture.originalSubscription, fixture.valueRoute())
                fixture.notifications().onCallback(NativeToken(2), 133) // Equal descriptor, wrong object.
                fixture.notifications(gatt = NativeToken(3)).onCallback(fixture.descriptor, 133)
                assertEquals(submissionTrace(enabling), fixture.trace)

                fixture.deliver()
                val nativeTrace = if (enabling) submissionTrace(true) else listOf("descriptor", "local:false")
                assertEquals(nativeTrace + listOf("unwatch", "state", "terminal", "drive"), fixture.trace)
                assertEquals(listOf(stateChanged(pending), completed(pending)), fixture.events)
                if (enabling) {
                    val subscription = fixture.valueRoute()
                    assertEquals(mode, subscription?.mode)
                    assertSame(fixture.characteristic, subscription?.characteristic)
                } else {
                    assertNull(fixture.valueRoute())
                }
                assertEquals(fixture.installedWatchdogs, fixture.removedWatchdogs)
                assertTrue(fixture.route.pendingIo.isEmpty)
                fixture.notifications().onCallback(fixture.descriptor, 133)
                assertTrue(fixture.compensationFailures.isEmpty())
                assertEquals(2, fixture.events.size)
            }
        }
    }

    @Test
    fun localRejectionOrExceptionDoesNotSubmitDescriptorOrCompensate() {
        val outcomes: List<Pair<() -> Boolean, BleEventError>> = listOf(
            { false } to gattError("Android rejected the local notification configuration"),
            { throw SecurityException("local denied") } to permissionError("local denied"),
            { throw IllegalStateException("local unavailable") } to gattError("local unavailable"),
        )
        for ((submit, primary) in outcomes) {
            val fixture = Fixture(enabling = true)
            val pending = fixture.enqueue()
            fixture.localSubmit = { submit() }
            fixture.drive()
            assertEquals(listOf("local:true", "unwatch", "terminal", "drive"), fixture.trace)
            assertEquals(listOf(completed(pending, primary)), fixture.events)
            assertSame(fixture.originalSubscription, fixture.valueRoute())
            assertTrue(fixture.compensationFailures.isEmpty())
            assertEquals(fixture.installedWatchdogs, fixture.removedWatchdogs)
        }
    }

    @Test
    fun descriptorRejectionAndExceptionsCompensateOnlyEnableAndPreserveDisableRouting() {
        val outcomes: List<Pair<() -> Boolean, BleEventError>> = listOf(
            { false } to gattError("Android rejected the Client Characteristic Configuration write"),
            { throw SecurityException("descriptor denied") } to permissionError("descriptor denied"),
            { throw IllegalStateException("descriptor unavailable") } to gattError("descriptor unavailable"),
            { throw RuntimeException() } to gattError("Android CCCD configuration is not available"),
        )
        for (enabling in listOf(true, false)) {
            for ((submit, primary) in outcomes) {
                val fixture = Fixture(enabling)
                val pending = fixture.enqueue()
                fixture.descriptorSubmit = { _, _ -> submit() }
                fixture.drive()
                assertEquals(failureTrace(enabling), fixture.trace)
                assertEquals(listOf(completed(pending, primary)), fixture.events)
                assertSame(fixture.originalSubscription, fixture.valueRoute())
                if (!enabling) {
                    assertTrue(fixture.localRegistered)
                    assertEquals(fixture.originalSubscription?.subscriptionId, fixture.receiveValue())
                }
                assertTrue(fixture.compensationFailures.isEmpty())
                assertTrue(fixture.route.pendingIo.isEmpty)
                assertEquals(fixture.installedWatchdogs, fixture.removedWatchdogs)
                fixture.notifications().onCallback(fixture.descriptor, 133)
                assertEquals(failureTrace(enabling), fixture.trace)
            }
        }
    }

    @Test
    fun failedDescriptorCallbackCompensatesOnlyEnableWithoutChangingLogicalValueRouteOrAuthorization() {
        for (enabling in listOf(true, false)) {
            for (status in listOf(5, 14, 133)) {
                val fixture = Fixture(enabling)
                val pending = fixture.enqueue()
                fixture.descriptorSubmit = { _, callback ->
                    fixture.callbacks.addLast { callback(status) }
                    true
                }
                fixture.drive()
                assertSame(fixture.originalSubscription, fixture.valueRoute())
                if (!enabling) {
                    assertTrue(fixture.localRegistered)
                    assertEquals(fixture.originalSubscription?.subscriptionId, fixture.receiveValue())
                }
                assertTrue(fixture.route.activeWatchdog.isArmed)
                fixture.deliver()
                assertEquals(failureTrace(enabling), fixture.trace)
                assertEquals(
                    listOf(completed(pending, gattError("Android CCCD write ended with status $status", status))),
                    fixture.events,
                )
                assertSame(fixture.originalSubscription, fixture.valueRoute())
                if (!enabling) {
                    assertTrue(fixture.localRegistered)
                    assertEquals(fixture.originalSubscription?.subscriptionId, fixture.receiveValue())
                }
                assertTrue(fixture.compensationFailures.isEmpty())
                assertEquals(ConnectionPhase.CONNECTED, fixture.route.phase)
                assertSame(fixture.route, fixture.currentRoute)
                assertEquals(fixture.installedWatchdogs, fixture.removedWatchdogs)
            }
        }
    }

    @Test
    fun compensationFalseOrExceptionIsDiagnosticOnlyAndDoesNotReplacePrimaryOrDisconnect() {
        val failures = listOf(null, SecurityException("compensation denied"), IllegalStateException("compensation unavailable"), RuntimeException("compensation failed"))
        for (callbackFailure in listOf(true, false)) {
            for (failure in failures) {
                val fixture = Fixture(enabling = true)
                val pending = fixture.enqueue()
                fixture.localSubmit = { enabled ->
                    if (enabled) true else {
                        if (failure != null) throw failure
                        false
                    }
                }
                fixture.descriptorSubmit = { _, callback ->
                    if (callbackFailure) fixture.callbacks.addLast { callback(133) }
                    callbackFailure
                }
                fixture.drive()
                if (callbackFailure) fixture.deliver()
                val primary = if (callbackFailure) gattError("Android CCCD write ended with status 133", 133)
                else gattError("Android rejected the Client Characteristic Configuration write")
                assertEquals(listOf(completed(pending, primary)), fixture.events)
                assertEquals(listOf("local:true", "descriptor", "local:false", "diagnostic", "unwatch", "terminal", "drive"), fixture.trace)
                assertEquals(1, fixture.compensationFailures.size)
                assertEquals(pending.commandId, fixture.compensationFailures.single().first)
                assertSame(failure, fixture.compensationFailures.single().second)
                assertSame(fixture.originalSubscription, fixture.valueRoute())
                assertEquals(ConnectionPhase.CONNECTED, fixture.route.phase)
                assertSame(fixture.route, fixture.currentRoute)
                assertTrue(fixture.route.pendingIo.isEmpty)
                assertEquals(fixture.installedWatchdogs, fixture.removedWatchdogs)
            }
        }
    }

    @Test
    fun ownerLossDuringEnableLocalSubmissionPreventsDescriptorAndCompensation() {
        for (loss in OwnerLoss.entries) {
            val fixture = Fixture(enabling = true)
            val pending = fixture.enqueue()
            fixture.localSubmit = {
                fixture.loseOwner(loss)
                true
            }
            fixture.drive()
            assertEquals(listOf("local:true"), fixture.trace)
            assertTrue(fixture.events.isEmpty())
            assertSame(pending, fixture.route.pendingIo.active())
            assertTrue(fixture.route.activeWatchdog.isArmed)
            fixture.retire(loss)
            assertEquals(listOf("local:true", "unwatch"), fixture.trace)
            assertTrue(fixture.compensationFailures.isEmpty())
        }
    }

    @Test
    fun ownerLossDuringDescriptorSubmissionOrBeforeCallbackLeavesCompensationToNobodyAndRetirementToOwner() {
        for (enabling in listOf(true, false)) {
            for (loss in OwnerLoss.entries) {
                for (failureAtSubmit in listOf(true, false)) {
                    val fixture = Fixture(enabling)
                    val pending = fixture.enqueue()
                    fixture.descriptorSubmit = { _, callback ->
                        if (!failureAtSubmit) fixture.callbacks.addLast { callback(133) }
                        fixture.loseOwner(loss)
                        !failureAtSubmit
                    }
                    fixture.drive()
                    if (!failureAtSubmit) fixture.deliver()
                    assertEquals(submissionTrace(enabling), fixture.trace)
                    assertTrue(fixture.events.isEmpty())
                    assertSame(pending, fixture.route.pendingIo.active())
                    assertTrue(fixture.route.activeWatchdog.isArmed)
                    fixture.retire(loss)
                    assertEquals(submissionTrace(enabling) + "unwatch", fixture.trace)
                    assertTrue(fixture.compensationFailures.isEmpty())
                }
            }
        }
    }

    @Test
    fun ownerLossInsideEnableCompensationDoesNotPublishPrimaryOrStartMoreNativeWork() {
        for (loss in OwnerLoss.entries) {
            val fixture = Fixture(enabling = true)
            val pending = fixture.enqueue()
            fixture.descriptorSubmit = { _, _ -> false }
            fixture.localSubmit = { enabled ->
                if (!enabled) fixture.loseOwner(loss)
                true
            }
            fixture.drive()
            assertEquals(listOf("local:true", "descriptor", "local:false"), fixture.trace)
            assertTrue(fixture.events.isEmpty())
            assertSame(pending, fixture.route.pendingIo.active())
            assertTrue(fixture.route.activeWatchdog.isArmed)
            fixture.retire(loss)
            assertEquals(listOf("local:true", "descriptor", "local:false", "unwatch"), fixture.trace)
            assertTrue(fixture.compensationFailures.isEmpty())
        }
    }

    @Test
    fun oldRouteCallbackCannotCompensateOrCompleteReplacementWithReusedNativeIdentities() {
        for (enabling in listOf(true, false)) {
            val fixture = Fixture(enabling)
            fixture.enqueue()
            fixture.descriptorSubmit = { _, callback ->
                fixture.callbacks.addLast { callback(133) }
                true
            }
            fixture.drive()
            fixture.loseOwner(OwnerLoss.REPLACED)
            fixture.retire(OwnerLoss.REPLACED)
            fixture.replaceRoute()
            fixture.descriptorSubmit = { _, callback ->
                fixture.callbacks.addLast { callback(0) }
                true
            }
            val replacement = fixture.enqueue()
            fixture.drive()
            val beforeStale = fixture.trace.toList()
            val beforeEvents = fixture.events.toList()
            fixture.deliver()
            assertEquals(beforeStale, fixture.trace)
            assertEquals(beforeEvents, fixture.events)
            assertSame(replacement, fixture.route.pendingIo.active())
            assertTrue(fixture.route.activeWatchdog.isArmed)
            fixture.deliver()
            assertEquals(beforeEvents + listOf(stateChanged(replacement), completed(replacement)), fixture.events)
            assertTrue(fixture.compensationFailures.isEmpty())
            assertEquals(fixture.installedWatchdogs, fixture.removedWatchdogs)
        }
    }

    @Test
    fun disableNeverUnregistersBeforeRemoteSuccessAndContinuesDeliveringWhilePending() {
        for (mode in NotificationMode.entries) {
            val fixture = Fixture(enabling = false, mode)
            val pending = fixture.enqueue()
            var remoteSucceeded = false
            fixture.localSubmit = { enabled ->
                assertFalse(enabled)
                assertTrue(remoteSucceeded, "local unregister must follow remote CCCD success")
                assertSame(pending, fixture.route.pendingIo.active())
                assertTrue(fixture.route.activeWatchdog.isArmed)
                assertTrue(fixture.events.isEmpty())
                true
            }
            fixture.drive()
            fixture.notifications().submit(pending) // Repeated dispatch cannot submit twice.
            assertEquals(listOf("descriptor"), fixture.trace)
            assertTrue(fixture.localRegistered)
            assertSame(fixture.originalSubscription, fixture.valueRoute())
            assertEquals(pending.command.subscriptionId, fixture.receiveValue())
            remoteSucceeded = true
            fixture.deliver()
            assertFalse(fixture.localRegistered)
            assertNull(fixture.receiveValue())
            assertNull(fixture.valueRoute())
            assertEquals(listOf(stateChanged(pending), completed(pending)), fixture.events)
            assertEquals(listOf("descriptor", "local:false", "unwatch", "state", "terminal", "drive"), fixture.trace)
        }
    }

    @Test
    fun remoteDisabledButLocalUnregisterFailureRetiresSubscriptionAndReportsOnlyPartialFailure() {
        val outcomes: List<Pair<() -> Boolean, BleEventError>> = listOf(
            { false } to gattError("Android rejected the local notification configuration"),
            { throw SecurityException("unregister denied") } to permissionError("unregister denied"),
            { throw IllegalStateException("unregister unavailable") } to gattError("unregister unavailable"),
            { throw RuntimeException() } to gattError("Android notification configuration is not available"),
        )
        for ((unregister, primary) in outcomes) {
            val fixture = Fixture(enabling = false)
            val pending = fixture.enqueue()
            fixture.localSubmit = { enabled ->
                assertFalse(enabled)
                // Conflicting duplicates and a reentrant dispatch cannot take over completion.
                fixture.notifications().onCallback(fixture.descriptor, 0)
                fixture.notifications().onCallback(fixture.descriptor, 14)
                fixture.notifications().submit(pending)
                unregister()
            }
            fixture.drive()
            assertEquals(listOf("descriptor"), fixture.trace)
            fixture.deliver()
            val terminal = fixture.events.single() as BleEvent.CommandCompleted
            assertEquals(pending.commandId, terminal.commandId)
            assertEquals(primary.category, terminal.error?.category)
            assertEquals(primary.platformDetails, terminal.error?.platformDetails)
            assertEquals(
                "Android notification disable partial failure: remote CCCD already disabled; ${primary.message}",
                terminal.error?.message,
            )
            assertNull(fixture.valueRoute())
            assertNull(fixture.receiveValue())
            assertTrue(fixture.route.pendingIo.isEmpty)
            assertSame(fixture.route, fixture.currentRoute)
            assertEquals(ConnectionPhase.CONNECTED, fixture.route.phase)
            assertTrue(fixture.compensationFailures.isEmpty())
            assertEquals(fixture.installedWatchdogs, fixture.removedWatchdogs)
            fixture.notifications().onCallback(fixture.descriptor, 0)
            fixture.notifications().onCallback(fixture.descriptor, 14)
            assertEquals(listOf("descriptor", "local:false", "unwatch", "terminal", "drive"), fixture.trace)
            assertEquals(1, fixture.events.size)
        }
    }

    @Test
    fun reentrantLocalHooksAndDescriptorCallbacksCannotRepeatCompletionOrCompensation() {
        for (enabling in listOf(true, false)) {
            for (status in listOf(0, 14)) {
                val fixture = Fixture(enabling)
                val pending = fixture.enqueue()
                fixture.localSubmit = {
                    fixture.notifications().onCallback(fixture.descriptor, 0)
                    fixture.notifications().onCallback(fixture.descriptor, 14)
                    fixture.notifications().submit(pending)
                    true
                }
                fixture.descriptorSubmit = { _, callback ->
                    callback(status)
                    callback(0)
                    callback(14)
                    false // A reentrant completion must not be overwritten by the return path.
                }
                fixture.drive()
                val expected = if (status == 0) listOf(stateChanged(pending), completed(pending))
                else listOf(completed(pending, gattError("Android CCCD write ended with status 14", 14)))
                assertEquals(expected, fixture.events)
                val expectedNative = if (enabling) {
                    if (status == 0) submissionTrace(true) else submissionTrace(true) + "local:false"
                } else {
                    if (status == 0) listOf("descriptor", "local:false") else listOf("descriptor")
                }
                val expectedEvents = if (status == 0) listOf("state", "terminal") else listOf("terminal")
                assertEquals(expectedNative + "unwatch" + expectedEvents + "drive", fixture.trace)
                assertEquals(fixture.installedWatchdogs, fixture.removedWatchdogs)
                assertTrue(fixture.route.pendingIo.isEmpty)
            }
        }
    }

    @Test
    fun ownerLossDuringPostRemoteUnregisterLeavesWatchdogAndTerminalToNativeOwnerRetirement() {
        for (loss in OwnerLoss.entries) {
            for (outcome in listOf("accepted", "rejected", "exception")) {
                val fixture = Fixture(enabling = false)
                val pending = fixture.enqueue()
                fixture.localSubmit = {
                    fixture.loseOwner(loss)
                    when (outcome) {
                        "accepted" -> true
                        "rejected" -> false
                        else -> throw SecurityException("owner lost")
                    }
                }
                fixture.drive()
                fixture.deliver()
                assertEquals(listOf("descriptor", "local:false"), fixture.trace)
                assertTrue(fixture.events.isEmpty())
                assertSame(pending, fixture.route.pendingIo.active())
                assertTrue(fixture.route.activeWatchdog.isArmed)
                fixture.retire(loss)
                fixture.notifications().onCallback(fixture.descriptor, 0)
                assertEquals(listOf("descriptor", "local:false", "unwatch"), fixture.trace)
                val expected = if (loss == OwnerLoss.DISPOSING) emptyList()
                else listOf(completed(pending, connectionError("retired")))
                assertEquals(expected, fixture.events)
                assertTrue(fixture.route.pendingIo.isEmpty)
                assertNull(fixture.valueRoute())
                assertEquals(fixture.installedWatchdogs, fixture.removedWatchdogs)
            }
        }
    }

    @Test
    fun retirementInsideUnregisterCannotRetireReplacementSubscriptionOrClearItsWatchdog() {
        val fixture = Fixture(enabling = false)
        val oldPending = fixture.enqueue()
        val oldNotifications = fixture.notifications()
        lateinit var replacement: PendingIo.Notification<Any, Any>
        fixture.localSubmit = {
            fixture.loseOwner(OwnerLoss.REPLACED)
            fixture.retire(OwnerLoss.REPLACED)
            fixture.replaceRoute()
            fixture.localSubmit = { true }
            replacement = fixture.enqueue()
            fixture.drive()
            throw IllegalStateException("old native owner lost")
        }
        fixture.drive()
        fixture.deliver()
        assertSame(replacement, fixture.route.pendingIo.active())
        assertSame(fixture.originalSubscription, fixture.valueRoute())
        assertTrue(fixture.route.activeWatchdog.isArmed)
        assertEquals(listOf(completed(oldPending, connectionError("retired"))), fixture.events)
        assertEquals(1, fixture.removedWatchdogs.size)
        oldNotifications.onCallback(fixture.descriptor, 0)
        fixture.deliver()
        assertEquals(
            listOf(completed(oldPending, connectionError("retired")), stateChanged(replacement), completed(replacement)),
            fixture.events,
        )
        assertNull(fixture.valueRoute())
        assertEquals(fixture.installedWatchdogs, fixture.removedWatchdogs)
    }

    private enum class OwnerLoss { DISCONNECTING, DISPOSING, REPLACED }
    private data class NativeToken(val id: Int)

    private class Fixture(val enabling: Boolean, val mode: NotificationMode = NotificationMode.NOTIFICATION) {
        val lock = Any()
        var route = newRoute(1)
        var currentRoute: ActiveConnection? = route
        var currentGatt: Any = NativeToken(3)
        var lifecycle = Lifecycle.READY
        val characteristic: Any = NativeToken(1)
        val descriptor: Any = NativeToken(2)
        val originalSubscription: ActiveSubscription<Any>? = if (enabling) null else ActiveSubscription(
            SubscriptionId(11u), GattCharacteristicId(10u), characteristic, mode,
        )
        val events = mutableListOf<BleEvent>()
        val trace = mutableListOf<String>()
        val callbacks = ArrayDeque<() -> Unit>()
        val compensationFailures = mutableListOf<Pair<CommandId, RuntimeException?>>()
        val installedWatchdogs = mutableListOf<Runnable>()
        val removedWatchdogs = mutableListOf<Runnable>()
        var localRegistered = !enabling
        var localSubmit: (Boolean) -> Boolean = { true }
        var descriptorSubmit: (PendingIo.Notification<*, *>, (Int) -> Unit) -> Boolean = { _, callback ->
            callbacks.addLast { callback(0) }
            true
        }

        init { originalSubscription?.let { route.subscriptionsById[it.subscriptionId.value] = it } }

        fun enqueue(): PendingIo.Notification<Any, Any> {
            val pending = PendingIo.Notification(
                command = SetNotificationsCommand(
                    commandId = CommandId(1u), connectionId = route.command.connectionId,
                    gattSessionId = GattSessionId(9u), subscriptionId = SubscriptionId(11u),
                    characteristicId = GattCharacteristicId(10u),
                    action = if (enabling) NotificationAction.Enable(NotificationModeRequest.PLATFORM_DEFAULT) else NotificationAction.Disable,
                ),
                characteristic = characteristic, descriptor = descriptor, mode = mode, enabling = enabling,
            )
            synchronized(lock) { assertTrue(route.pendingIo.enqueue(pending)) }
            return pending
        }

        // The fixture supplies Handler admission/scheduling, not notification lifecycle decisions.
        fun drive() {
            assertFalse(Thread.holdsLock(lock))
            val owner = route
            val pending = synchronized(lock) {
                if (currentRoute !== owner || lifecycle != Lifecycle.READY || owner.phase != ConnectionPhase.CONNECTED) return
                val active = owner.pendingIo.begin() as? PendingIo.Notification<*, *> ?: return
                val watchdog = object : Runnable { override fun run() = Unit }
                assertNull(owner.activeWatchdog.install(watchdog))
                installedWatchdogs.add(watchdog)
                active
            }
            notifications(owner, currentGatt).submit(pending)
        }

        fun notifications(owner: ActiveConnection = route, gatt: Any = currentGatt): NotificationLifecycle = NotificationLifecycle(
            stateLock = lock, route = owner,
            ownsRoute = {
                assertTrue(Thread.holdsLock(lock))
                currentRoute === owner && currentGatt === gatt && lifecycle != Lifecycle.CLOSING && lifecycle != Lifecycle.CLOSED
            },
            setLocalRegistration = { _, enabled ->
                assertFalse(Thread.holdsLock(lock))
                synchronized(lock) {
                    assertSame(owner, currentRoute)
                    assertSame(gatt, currentGatt)
                    assertEquals(ConnectionPhase.CONNECTED, owner.phase)
                    assertEquals(Lifecycle.READY, lifecycle)
                }
                trace.add("local:$enabled")
                localSubmit(enabled).also { if (it) localRegistered = enabled }
            },
            submitDescriptor = { pending ->
                assertFalse(Thread.holdsLock(lock))
                trace.add("descriptor")
                descriptorSubmit(pending) { status -> notifications(owner, gatt).onCallback(pending.descriptor, status) }
            },
            reportCompensationFailure = { id, error ->
                assertFalse(Thread.holdsLock(lock))
                trace.add("diagnostic")
                compensationFailures.add(id to error)
            },
            removeWatchdog = ::removeWatchdog,
            publish = {
                assertFalse(Thread.holdsLock(lock))
                trace.add(if (it is BleEvent.NotificationStateChanged) "state" else "terminal")
                events.add(it)
            },
            driveQueue = { trace.add("drive"); drive() },
        )

        fun deliver() = callbacks.removeFirst().invoke()

        fun valueRoute(): ActiveSubscription<*>? = synchronized(lock) {
            resolveCharacteristicNotificationRoute(
                active = currentRoute === route && route.phase == ConnectionPhase.CONNECTED,
                expectedGatt = currentGatt, actualGatt = currentGatt, actualCharacteristic = characteristic,
                subscriptions = route.subscriptionsById.values, characteristicOf = { it.characteristic },
            )
        }

        // Simulated native value ingress uses the production identity/logical-route resolver.
        fun receiveValue(): SubscriptionId? = if (localRegistered) valueRoute()?.subscriptionId else null

        fun loseOwner(loss: OwnerLoss) = synchronized(lock) {
            when (loss) {
                OwnerLoss.DISCONNECTING -> route.phase = ConnectionPhase.DISCONNECTING
                OwnerLoss.DISPOSING -> { lifecycle = Lifecycle.CLOSING; currentRoute = null }
                OwnerLoss.REPLACED -> currentRoute = newRoute(route.generation + 1)
            }
        }

        // Actual production retirement projection; no claim of native disconnect/close execution.
        fun retire(loss: OwnerLoss) {
            val error = if (loss == OwnerLoss.DISPOSING) operationAbortedError("dispose") else connectionError("retired")
            val (terminals, watchdog) = synchronized(lock) {
                val terminals = retirePendingIo(route.pendingIo, error, error)
                route.subscriptionsById.clear()
                terminals to route.activeWatchdog.clear()
            }
            watchdog?.let(::removeWatchdog)
            if (loss != OwnerLoss.DISPOSING) events.addAll(terminals)
        }

        fun replaceRoute() = synchronized(lock) {
            route = newRoute(route.generation + 1)
            currentRoute = route
            currentGatt = NativeToken(3)
            originalSubscription?.let { route.subscriptionsById[it.subscriptionId.value] = it }
        }

        private fun removeWatchdog(watchdog: Runnable) {
            assertFalse(Thread.holdsLock(lock))
            trace.add("unwatch")
            removedWatchdogs.add(watchdog)
        }

        private fun newRoute(generation: Long) = ActiveConnection(
            generation = generation,
            command = ConnectCommand(CommandId(100u), ConnectionId(7u), PeripheralId(8u)),
            address = "synthetic-notification-peer", phase = ConnectionPhase.CONNECTED, connectCompleted = true,
        )
    }

    private fun completed(pending: PendingIo.Notification<*, *>, error: BleEventError? = null): BleEvent =
        BleEvent.CommandCompleted(pending.commandId, error)

    private fun stateChanged(pending: PendingIo.Notification<*, *>): BleEvent = BleEvent.NotificationStateChanged(
        commandId = pending.commandId, connectionId = pending.command.connectionId,
        gattSessionId = pending.command.gattSessionId, subscriptionId = pending.command.subscriptionId,
        characteristicId = pending.command.characteristicId,
        state = if (pending.enabling) NotificationState.Enabled(pending.mode) else NotificationState.Disabled,
    )

    private fun submissionTrace(enabling: Boolean) = if (enabling) listOf("local:true", "descriptor") else listOf("descriptor")

    private fun failureTrace(enabling: Boolean) = submissionTrace(enabling) +
            (if (enabling) listOf("local:false") else emptyList()) + listOf("unwatch", "terminal", "drive")
}
