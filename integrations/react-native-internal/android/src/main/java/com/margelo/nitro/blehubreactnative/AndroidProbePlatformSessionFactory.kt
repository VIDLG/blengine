package com.margelo.nitro.blehubreactnative

import android.content.Context
import dev.bleng.Bleng
import dev.bleng_supervisor.ForeignOwnerContext
import dev.bleng_supervisor.ForeignPlatformOwnerSession
import dev.bleng_supervisor.ForeignPlatformOwnerSessionFactory
import dev.blehub.BleEvent
import com.blehub.reactnative.bleng.AndroidPlatformSession
import com.blehub.reactnative.bleng.AndroidPlatformSessionFactory

/**
 * Internal-only factory bridge for the Rust SessionSupervisor migration.
 *
 * The formal Android platform session remains the owner of Android BLE/backend resources. These
 * callbacks only project the already-owned Rust event stream into the private probe handoff and
 * retain the owner for the probe's typed-operation adapter. No callback is part of the formal
 * Android/RN package surface.
 */
internal class AndroidProbePlatformSessionFactory(
    private val context: Context,
    private val queueLabel: String,
    private val eventSink: (BleEvent) -> Unit,
    private val ownerSink: (Bleng) -> Unit,
) : ForeignPlatformOwnerSessionFactory {
    override fun requiredZeroResourceNames(): List<String> =
        AndroidPlatformSessionFactory.REQUIRED_ZERO_RESOURCE_NAMES

    override fun create(context: ForeignOwnerContext): ForeignPlatformOwnerSession {
        return AndroidPlatformSession.createWithCallbacks(
            context = this.context,
            requested = context,
            queueLabel = queueLabel,
            eventSink = eventSink,
            ownerSink = ownerSink,
        )
    }
}
