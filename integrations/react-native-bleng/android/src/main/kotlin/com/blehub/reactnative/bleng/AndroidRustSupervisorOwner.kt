package com.blehub.reactnative.bleng

import android.content.Context
import android.os.SystemClock
import android.util.Log
import dev.blehub.BluetoothUuid
import dev.blehub.NotificationModeRequest
import dev.blehub.ScanOptions
import dev.bleng_supervisor.ForeignOwnerClose
import dev.bleng_supervisor.ForeignOwnerContext
import dev.bleng_supervisor.ForeignOwnerStart
import dev.bleng_supervisor.ForeignPlatformOwnerSessionFactory
import dev.bleng_supervisor.ForeignRecoveryLink
import dev.bleng_supervisor.ForeignRecoveryPolicyConfig
import dev.bleng_supervisor.ForeignRecoveryTarget
import dev.bleng_supervisor.ForeignResourceCount
import dev.bleng_supervisor.ForeignScanEventEnvelope
import dev.bleng_supervisor.ForeignScanBeginResult
import dev.bleng_supervisor.ForeignScanOptions
import dev.bleng_supervisor.ForeignSupervisorConfig
import dev.bleng_supervisor.Supervisor
import java.util.UUID

/** Fixed lifecycle phases only; successful Start is a positive control for this gated Close log channel. */
internal fun traceClosePhase(phase: String) {
    if (!Log.isLoggable("BleHubBlengClose", Log.DEBUG)) return
    val thread = Thread.currentThread()
    Log.d(
        "BleHubBlengClose",
        "phase=$phase elapsedRealtimeNanos=${SystemClock.elapsedRealtimeNanos()} " +
            "threadId=${thread.id} threadName=${thread.name}",
    )
}

/**
 * Kotlin owner seam for one Rust-supervised Android session lifetime.
 * A queued scan consumer must check the full envelope sourceContext at consumption time;
 * retirement does not retract envelopes already dispatched to scanEventSink.
 */
class AndroidRustSupervisorOwner(
    context: Context,
    queueLabel: String,
    recoveryTarget: ForeignRecoveryTarget? = null,
    oldLink: ForeignRecoveryLink? = null,
    expectedOldOperationIds: List<String> = emptyList(),
    allowCentralRecreation: Boolean = false,
    allowFreshOwner: Boolean = true,
    sessionFactory: ForeignPlatformOwnerSessionFactory? = null,
    scanEventSink: ((ForeignScanEventEnvelope) -> Unit)? = null,
) {
    private val supervisor: Supervisor

    init {
        val initialContext = ForeignOwnerContext(
            ownerId = UUID.randomUUID().toString(),
            backendId = UUID.randomUUID().toString(),
            ownerGeneration = 1uL,
            recoveryAttempt = 1uL,
        )
        val config = ForeignSupervisorConfig(
            recovery = ForeignRecoveryPolicyConfig(
                initialContext = initialContext,
                oldLink = oldLink,
                expectedOldOperationIds = expectedOldOperationIds,
                allowCentralRecreation = allowCentralRecreation,
                allowFreshOwner = allowFreshOwner,
            ),
            recoveryTarget = recoveryTarget,
        )
        supervisor = Supervisor(
            factory = sessionFactory ?: AndroidPlatformSessionFactory(context, queueLabel, scanEventSink),
            config = config,
        )
    }

    suspend fun start(): ForeignOwnerStart {
        traceClosePhase("probe.rustStart.enter")
        try {
            val started = supervisor.start()
            traceClosePhase("probe.rustStart.return")
            return started
        } catch (error: Throwable) {
            traceClosePhase("probe.rustStart.fail")
            throw error
        }
    }

    suspend fun close(): List<ForeignResourceCount> = supervisor.shutdown()

    suspend fun closeResult(): ForeignOwnerClose {
        traceClosePhase("rust.closeResult.enter")
        try {
            val receipt = supervisor.shutdownView()
            traceClosePhase("rust.closeResult.return")
            return receipt
        } catch (error: Throwable) {
            traceClosePhase("rust.closeResult.fail")
            throw error
        }
    }

    fun beginScanView(context: ForeignOwnerContext, options: ForeignScanOptions): ForeignScanBeginResult =
        supervisor.beginScanView(context, options)

    companion object {
        /** Maps host/provider facts into the validated Rust recovery target. */
        fun recoveryTarget(
            serviceUuid: String,
            characteristicUuid: String,
            expectedValue: ByteArray,
            scanNamePrefix: String? = null,
        ): ForeignRecoveryTarget = ForeignRecoveryTarget(
            scanOptions = ScanOptions(
                serviceUuids = listOf(BluetoothUuid(serviceUuid)),
                namePrefix = scanNamePrefix,
                companyId = null,
                allowDuplicates = false,
            ),
            serviceUuid = BluetoothUuid(serviceUuid),
            characteristicUuid = BluetoothUuid(characteristicUuid),
            notificationMode = NotificationModeRequest.PLATFORM_DEFAULT,
            expectedValue = expectedValue,
        )
    }
}
