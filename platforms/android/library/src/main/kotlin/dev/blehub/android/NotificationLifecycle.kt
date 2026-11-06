package dev.blehub.android

import dev.blehub.BleEvent
import dev.blehub.BleEventError
import dev.blehub.CommandId
import dev.blehub.NotificationState

/** Notification-only local registration, CCCD submission and callback boundary on the owner Handler. */
internal class NotificationLifecycle(
    private val stateLock: Any,
    private val route: ActiveConnection,
    // Called under stateLock; checks open lifecycle and exact route/GATT identity.
    private val ownsRoute: () -> Boolean,
    private val setLocalRegistration: (PendingIo.Notification<*, *>, Boolean) -> Boolean,
    private val submitDescriptor: (PendingIo.Notification<*, *>) -> Boolean,
    private val reportCompensationFailure: (CommandId, RuntimeException?) -> Unit,
    private val removeWatchdog: (Runnable) -> Unit,
    private val publish: (BleEvent) -> Unit,
    private val driveQueue: () -> Unit,
) {
    fun submit(pending: PendingIo.Notification<*, *>) {
        synchronized(stateLock) {
            if (!isActive(pending) || pending.stage != PendingIo.NotificationStage.QUEUED) return
            pending.stage = if (pending.enabling) PendingIo.NotificationStage.REGISTERING_LOCAL
            else PendingIo.NotificationStage.WRITING_DESCRIPTOR
        }
        if (pending.enabling) {
            val localAccepted = try {
                setLocalRegistration(pending, true)
            } catch (error: SecurityException) {
                finish(pending, permissionError(error.message))
                return
            } catch (error: RuntimeException) {
                finish(pending, gattError(error.message ?: "Android notification configuration is not available"))
                return
            }
            if (!localAccepted) {
                finish(pending, gattError("Android rejected the local notification configuration"))
                return
            }
            synchronized(stateLock) {
                // Retirement owns a route lost during local registration.
                if (!isActive(pending)) return
                pending.localCompensationPending = true
                pending.stage = PendingIo.NotificationStage.WRITING_DESCRIPTOR
            }
        }
        // Disable must leave both local and logical routing intact until remote success.
        val accepted = try {
            submitDescriptor(pending)
        } catch (error: SecurityException) {
            finish(pending, permissionError(error.message))
            return
        } catch (error: RuntimeException) {
            finish(pending, gattError(error.message ?: "Android CCCD configuration is not available"))
            return
        }
        if (!accepted) {
            finish(pending, gattError("Android rejected the Client Characteristic Configuration write"))
        }
    }

    fun onCallback(descriptor: Any, status: Int) {
        val pending = synchronized(stateLock) {
            if (!ownsRoute() || route.phase != ConnectionPhase.CONNECTED) return
            (route.pendingIo.active() as? PendingIo.Notification<*, *>)
                ?.takeIf { it.descriptor === descriptor && it.stage == PendingIo.NotificationStage.WRITING_DESCRIPTOR }
        } ?: return
        finish(pending, if (status == 0) null else gattError("Android CCCD write ended with status $status", status))
    }

    private fun isActive(pending: PendingIo.Notification<*, *>): Boolean =
        ownsRoute() && route.phase == ConnectionPhase.CONNECTED && route.pendingIo.isActive(pending)

    private fun finish(pending: PendingIo.Notification<*, *>, error: BleEventError?) {
        val compensate = synchronized(stateLock) {
            if (!isActive(pending) || pending.stage == PendingIo.NotificationStage.COMPLETING) return
            // Claim before calling native hooks: duplicate/reentrant callbacks cannot
            // unregister or compensate twice. The native slot/watchdog stay owned.
            pending.stage = PendingIo.NotificationStage.COMPLETING
            (error != null && pending.enabling && pending.localCompensationPending).also {
                if (it) pending.localCompensationPending = false
            }
        }
        if (compensate) {
            // The ownership check above admits one best-effort LOCAL request only.
            // No descriptor retry or remote-state guarantee follows from this call.
            var compensationError: RuntimeException? = null
            val restored = try {
                setLocalRegistration(pending, false)
            } catch (failure: RuntimeException) {
                compensationError = failure
                false
            }
            if (!restored) reportCompensationFailure(pending.commandId, compensationError)
        }

        val remoteDisabled = !pending.enabling && error == null
        val terminalError = if (remoteDisabled) {
            val localError = try {
                if (setLocalRegistration(pending, false)) null
                else gattError("Android rejected the local notification configuration")
            } catch (failure: SecurityException) {
                permissionError(failure.message)
            } catch (failure: RuntimeException) {
                gattError(failure.message ?: "Android notification configuration is not available")
            }
            localError?.let {
                BleEventError(
                    category = it.category,
                    message = "Android notification disable partial failure: remote CCCD already disabled; ${it.message}",
                    platformDetails = it.platformDetails,
                )
            }
        } else error

        val watchdog: Runnable?
        val publishTerminal: Boolean
        synchronized(stateLock) {
            // Ownership can change during either native call, including compensation.
            // Leave queue/watchdog/terminal cleanup to disconnect or disposal in that case.
            if (!isActive(pending) || !route.pendingIo.complete(pending)) return
            watchdog = route.activeWatchdog.clear()
            if (remoteDisabled) {
                // A local failure cannot restore a remotely disabled subscription.
                route.subscriptionsById.remove(pending.command.subscriptionId.value)
            } else if (pending.enabling && terminalError == null) {
                route.subscriptionsById[pending.command.subscriptionId.value] = ActiveSubscription(
                    subscriptionId = pending.command.subscriptionId,
                    characteristicId = pending.command.characteristicId,
                    characteristic = pending.characteristic,
                    mode = pending.mode,
                )
            }
            publishTerminal = pending.claimCommandTerminal()
        }
        watchdog?.let(removeWatchdog)
        if (publishTerminal && terminalError == null) {
            publish(
                BleEvent.NotificationStateChanged(
                    commandId = pending.commandId,
                    connectionId = pending.command.connectionId,
                    gattSessionId = pending.command.gattSessionId,
                    subscriptionId = pending.command.subscriptionId,
                    characteristicId = pending.command.characteristicId,
                    state = if (pending.enabling) NotificationState.Enabled(pending.mode) else NotificationState.Disabled,
                ),
            )
        }
        if (publishTerminal) publish(BleEvent.CommandCompleted(pending.commandId, terminalError))
        driveQueue()
    }
}
