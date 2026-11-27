package com.blehub.reactnative.bleng

/** Retires an existing session-owned worker without taking ownership of backend disposal. */
internal class OwnedBackendThread(
    private val thread: Thread,
    private val requestQuit: () -> Unit,
    private val joinMillis: Long = 5_000L,
) {
    private var stopResult: Result<Unit>? = null

    init {
        require(joinMillis > 0) { "Backend thread join must be bounded" }
    }

    @Synchronized
    fun stop() {
        val result = stopResult ?: runCatching {
            check(Thread.currentThread() !== thread) { "Android backend thread cannot join itself" }
            requestQuit()
            try {
                thread.join(joinMillis)
            } catch (error: InterruptedException) {
                Thread.currentThread().interrupt()
                throw error
            }
            check(!thread.isAlive) { "Android backend thread did not stop" }
        }.also { stopResult = it }
        result.getOrThrow()
    }

    fun <T> snapshotAndStop(snapshot: () -> T): T {
        var failure: Throwable? = null
        try {
            return snapshot()
        } catch (error: Throwable) {
            failure = error
            throw error
        } finally {
            try {
                stop()
            } catch (error: Throwable) {
                val primary = failure
                if (primary == null) throw error
                if (primary !== error) primary.addSuppressed(error)
            }
        }
    }
}
