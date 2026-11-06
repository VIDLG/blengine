package dev.blehub.android

import dev.blehub.BleEvent
import dev.blehub.BleEventError
import dev.blehub.WriteType

/**
 * Write-only submission and callback boundary, called on the backend owner Handler.
 * The caller supplies Framework I/O and route/GATT ownership; queue, watchdog and
 * command-terminal transitions stay here under the backend's existing state lock.
 */
internal class CharacteristicWriteLifecycle(
    private val stateLock: Any,
    private val route: ActiveConnection,
    // Called only under stateLock; includes open lifecycle and exact route/GATT identity.
    private val ownsRoute: () -> Boolean,
    private val removeWatchdog: (Runnable) -> Unit,
    private val publish: (BleEvent) -> Unit,
    private val driveQueue: () -> Unit,
) {
    fun submit(pending: PendingIo.Write<*>, submitNative: () -> Boolean) {
        synchronized(stateLock) {
            if (!isActive(pending)) return
        }
        val accepted = try {
            submitNative()
        } catch (error: SecurityException) {
            finish(pending, permissionError(error.message))
            return
        } catch (error: IllegalStateException) {
            finish(pending, gattError(error.message ?: "Android characteristic write is not available"))
            return
        }
        if (!accepted) {
            finish(pending, gattError("Android rejected the characteristic write request"))
        } else if (pending.command.writeType == WriteType.WITHOUT_RESPONSE) {
            val publishTerminal = synchronized(stateLock) {
                // Disconnect may change the phase during native submission. Acceptance
                // still completes an owned command, but disposal wins by closing ownership.
                ownsRoute() && route.pendingIo.isActive(pending) && pending.claimCommandTerminal()
            }
            // Both events describe local acceptance, not remote acknowledgement.
            if (publishTerminal) publishSuccess(pending)
            // Do not finish: Framework still owns the native slot and its watchdog.
        }
    }

    fun onCallback(characteristic: Any, status: Int) {
        val pending = synchronized(stateLock) {
            if (!ownsRoute() || route.phase != ConnectionPhase.CONNECTED) return
            (route.pendingIo.active() as? PendingIo.Write<*>)
                ?.takeIf { it.characteristic === characteristic }
        } ?: return
        finish(
            pending,
            if (status == 0) null else gattError("Android characteristic write ended with status $status", status),
        )
    }

    private fun isActive(pending: PendingIo.Write<*>): Boolean =
        ownsRoute() && route.phase == ConnectionPhase.CONNECTED && route.pendingIo.isActive(pending)

    private fun finish(pending: PendingIo.Write<*>, error: BleEventError?) {
        val watchdog: Runnable?
        val publishTerminal: Boolean
        synchronized(stateLock) {
            if (!isActive(pending) || !route.pendingIo.complete(pending)) return
            watchdog = route.activeWatchdog.clear()
            publishTerminal = pending.claimCommandTerminal()
        }
        watchdog?.let(removeWatchdog)
        if (publishTerminal) {
            if (error == null) {
                publishSuccess(pending)
            } else {
                publish(BleEvent.CommandCompleted(pending.commandId, error))
            }
        }
        driveQueue()
    }

    /** Only the terminal claimant publishes this ordered pair; native slot ownership is separate. */
    private fun publishSuccess(pending: PendingIo.Write<*>) {
        val command = pending.command
        publish(
            BleEvent.CharacteristicWritten(
                commandId = command.commandId,
                connectionId = command.connectionId,
                gattSessionId = command.gattSessionId,
                characteristicId = command.characteristicId,
            ),
        )
        publish(BleEvent.CommandCompleted(command.commandId, null))
    }
}
