package com.blehub.reactnative.bleng

import android.content.Context
import dev.blehub.android.AndroidBleSessionSupervisor
import dev.blehub.android.AndroidBleSessionSupervisorPhase
import dev.blehub.android.AndroidBleResourceSnapshot
import dev.blehub.BleEvent

/**
 * Formal production owner for the Android BLeng package line.
 *
 * Nitro is intentionally not referenced here. The lifecycle-only RN bridge delegates to this
 * owner, while operation/event projection and recovery outcomes remain separate native contracts.
 */
class AndroidBlengSessionOwner(
    context: Context,
    private val queueLabel: String,
    eventSink: ((BleEvent) -> Unit)? = null,
) {
    private val supervisor = AndroidBleSessionSupervisor(
        sessionFactory = { generation ->
            AndroidBlengSession.create(
                context = context,
                queueLabel = "$queueLabel-g$generation",
                eventSink = eventSink,
            )
        },
        requiredZeroResources = AndroidBleSessionSupervisor.DEFAULT_REQUIRED_ZERO_RESOURCES,
    )

    val phase: AndroidBleSessionSupervisorPhase
        get() = supervisor.phase

    suspend fun start(): AndroidBlengSession = supervisor.start() as AndroidBlengSession

    suspend fun replace(): AndroidBlengSession = supervisor.replace() as AndroidBlengSession

    suspend fun close() {
        supervisor.close()
    }

    suspend fun activeSession(): AndroidBlengSession = supervisor.activeSession() as AndroidBlengSession

    fun lastRetiredResources(): AndroidBleResourceSnapshot = supervisor.lastRetiredResources
}
