package com.blehub.reactnative.bleng

import dev.blehub.BleEvent
import dev.bleng.BlengException
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive

/** Drain the one owned event stream; the caller owns projection and final retirement. */
internal suspend fun drainBlengEvents(
    nextEvent: suspend () -> BleEvent?,
    onEvent: (BleEvent) -> Unit,
) {
    while (true) {
        currentCoroutineContext().ensureActive()
        val event = try {
            nextEvent()
        } catch (_: BlengException.Timeout) {
            // Only the read observation expired; no BLE command or delivery is retried.
            continue
        }
        if (event == null) return
        onEvent(event)
    }
}
