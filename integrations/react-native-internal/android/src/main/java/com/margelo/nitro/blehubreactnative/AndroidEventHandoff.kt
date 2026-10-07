package com.margelo.nitro.blehubreactnative

import android.os.Handler

/**
 * Finite handoff from a synchronous native event source to a serialized Android
 * delivery handler. It never owns a JS queue and permanently retires when its
 * capacity is exceeded, rather than silently dropping an accepted event.
 */
class AndroidEventHandoff<Event>(
    private val handler: Handler,
    capacity: Int,
    private val deliver: (Event) -> Unit,
    private val onFailure: () -> Unit,
) {
    private val lock = Any()
    private val capacity = capacity.also { require(it > 0) }
    private var accepting = true
    private var discardPending = false
    private var pending = 0

    fun submit(event: Event): SubmitResult {
        val result = synchronized(lock) {
            when {
                !accepting -> SubmitResult.CLOSED
                pending >= capacity -> {
                    accepting = false
                    SubmitResult.OVERFLOW
                }

                else -> {
                    pending += 1
                    SubmitResult.ACCEPTED
                }
            }
        }

        when (result) {
            SubmitResult.ACCEPTED -> Unit
            SubmitResult.OVERFLOW -> {
                onFailure()
                return result
            }

            SubmitResult.CLOSED, SubmitResult.DELIVERY_UNAVAILABLE -> return result
        }

        if (handler.post {
                val shouldDeliver = synchronized(lock) { !discardPending }
                try {
                    if (shouldDeliver) deliver(event)
                } finally {
                    synchronized(lock) { pending -= 1 }
                }
            }) {
            return SubmitResult.ACCEPTED
        }

        val shouldNotify = synchronized(lock) {
            pending -= 1
            if (accepting) {
                accepting = false
                discardPending = true
                true
            } else {
                false
            }
        }
        if (shouldNotify) onFailure()
        return SubmitResult.DELIVERY_UNAVAILABLE
    }

    fun close() {
        synchronized(lock) {
            accepting = false
            discardPending = true
        }
    }

    enum class SubmitResult {
        ACCEPTED,
        CLOSED,
        OVERFLOW,
        DELIVERY_UNAVAILABLE,
    }
}
