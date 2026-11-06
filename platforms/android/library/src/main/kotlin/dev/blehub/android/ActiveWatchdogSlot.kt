package dev.blehub.android

/**
 * Owner-handler-confined slot for the one native watchdog of a connection.
 *
 * The backend performs all calls while holding its state lock. This type only
 * linearizes replacement, cancellation, and stale callback rejection; it does
 * not schedule or remove callbacks from the Handler.
 */
internal class ActiveWatchdogSlot {
    private var current: Runnable? = null

    /** Read under the backend state lock, like installation and retirement. */
    val isArmed: Boolean
        get() = current != null

    /** Installs [next] and returns the replaced callback, if any. */
    fun install(next: Runnable): Runnable? {
        val previous = current
        current = next
        return previous
    }

    /** Removes and returns the current callback. */
    fun clear(): Runnable? = current.also { current = null }

    /** Removes [candidate] only when it is still the current callback. */
    fun takeIfCurrent(candidate: Runnable): Boolean {
        if (current !== candidate) return false
        current = null
        return true
    }
}
