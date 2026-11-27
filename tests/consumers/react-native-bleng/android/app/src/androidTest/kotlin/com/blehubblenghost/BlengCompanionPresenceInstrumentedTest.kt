package com.blehubblenghost

import android.annotation.SuppressLint
import android.companion.AssociationInfo
import android.companion.CompanionDeviceManager
import android.companion.ObservingDevicePresenceRequest
import android.os.Build
import android.os.ParcelFileDescriptor
import androidx.annotation.RequiresApi
import androidx.test.platform.app.InstrumentationRegistry
import com.blehub.reactnative.bleng.AndroidRustSupervisorOwner
import dev.bleng_supervisor.ForeignCompanionPresence
import dev.bleng_supervisor.SupervisedCompanionPresenceObservation
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test

/**
 * Companion presence through the real backend, the library's own `CompanionDeviceService` and the
 * shipped presence lane.
 *
 * This uses CDM's documented debug hooks (`cmd companiondevice associate` and
 * `simulate-device-appeared/disappeared`) instead of a chooser and a physical peer: the assertion
 * is that the platform's presence callback reaches the App with an owner-scoped key, not that a
 * radio observed a device. The synthetic association is created and removed by the test itself.
 */
@RequiresApi(Build.VERSION_CODES.TIRAMISU)
class BlengCompanionPresenceInstrumentedTest {
    @Test
    fun simulatedPresenceReachesTheLaneWithAnOwnerScopedKey() = runBlocking {
        assumeTrue(
            "the CDM presence hooks need Android 13 or newer",
            Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU,
        )
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        val context = instrumentation.targetContext
        val manager = requireNotNull(
            context.getSystemService(CompanionDeviceManager::class.java),
        ) { "CompanionDeviceManager is unavailable" }

        shell("cmd companiondevice disassociate 0 ${context.packageName} $MAC")
        shell("cmd companiondevice associate 0 ${context.packageName} $MAC")
        val association = awaitAssociation(manager)

        val owner = AndroidRustSupervisorOwner(context, "bleng-companion-presence")
        val started = try {
            withTimeout(START_TIMEOUT_MS) { owner.start() }
        } catch (error: Throwable) {
            closeQuietly(owner)
            throw error
        }
        try {
            val submission = owner.beginObserveCompanionPresenceView(started.context)
            assertNull("presence observation must be accepted", submission.failure)
            val observation = requireNotNull(submission.presenceObservation) {
                "presence observation produced no lane"
            }

            startObserving(manager, association)
            shell("cmd companiondevice simulate-device-appeared ${association.id}")
            val present = awaitPresenceState(observation, expected = true)
            assertTrue("presence keys are never empty", present.key.isNotEmpty())

            shell("cmd companiondevice simulate-device-disappeared ${association.id}")
            val absent = awaitPresenceState(observation, expected = false)
            assertEquals(
                "the same peer keeps one owner-scoped key",
                present.key,
                absent.key,
            )
        } finally {
            stopObserving(manager, association.id)
            shell("cmd companiondevice disassociate 0 ${context.packageName} $MAC")
            withTimeout(START_TIMEOUT_MS) { owner.closeResult() }
        }
    }

    /**
     * Pulls until the requested state arrives.
     *
     * One CDM transition can surface more than one event flavour (for example BLE appeared *and*
     * BT connected are both "present"), so the lane legitimately holds several `true` transitions
     * before a disappearance. A test that assumed "the next pull is the transition I just asked
     * for" would read its own earlier event and fail for the wrong reason.
     */
    private suspend fun awaitPresenceState(
        observation: SupervisedCompanionPresenceObservation,
        expected: Boolean,
    ): ForeignCompanionPresence {
        var last: ForeignCompanionPresence? = null
        repeat(MAX_PRESENCE_PULLS) {
            val next = withTimeout(READ_TIMEOUT_MS) { observation.nextEvent(PULL_TIMEOUT_MS) }
            assertNull("presence observation must not fail", next.failure)
            val presence = next.presence
            if (presence != null && presence.present == expected) return presence
            if (presence == null) {
                throw AssertionError("presence observation ended without the expected state")
            }
            last = presence
        }
        throw AssertionError(
            "presence lane never reported present=$expected (last=${last?.present})",
        )
    }

    private suspend fun awaitAssociation(
        manager: CompanionDeviceManager,
    ): AssociationInfo {
        repeat(ASSOCIATION_ATTEMPTS) {
            manager.myAssociations
                .firstOrNull {
                    it.deviceMacAddress?.toString()?.equals(MAC, ignoreCase = true) == true
                }
                ?.let { return it }
            delay(ASSOCIATION_POLL_MS)
        }
        throw AssertionError("the synthetic companion association was not created")
    }

    @SuppressLint("UnflaggedApi", "MissingPermission")
    private fun startObserving(
        manager: CompanionDeviceManager,
        association: AssociationInfo,
    ) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.BAKLAVA) {
            manager.startObservingDevicePresence(
                ObservingDevicePresenceRequest.Builder()
                    .setAssociationId(association.id)
                    .build(),
            )
        } else {
            @Suppress("DEPRECATION")
            manager.startObservingDevicePresence(association.deviceMacAddress.toString())
        }
    }

    private fun stopObserving(manager: CompanionDeviceManager, associationId: Int) {
        runCatching {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.BAKLAVA) {
                manager.stopObservingDevicePresence(
                    ObservingDevicePresenceRequest.Builder()
                        .setAssociationId(associationId)
                        .build(),
                )
            }
        }
    }

    private fun shell(command: String): String =
        ParcelFileDescriptor.AutoCloseInputStream(
            InstrumentationRegistry.getInstrumentation().uiAutomation
                .executeShellCommand(command),
        ).bufferedReader().use { reader -> reader.readText() }

    private suspend fun closeQuietly(owner: AndroidRustSupervisorOwner) {
        runCatching { withTimeout(START_TIMEOUT_MS) { owner.closeResult() } }
    }

    private companion object {
        const val MAC = "AA:BB:CC:DD:EE:02"
        const val START_TIMEOUT_MS = 60_000L
        const val READ_TIMEOUT_MS = 60_000L
        const val PULL_TIMEOUT_MS = 30_000.0
        const val ASSOCIATION_ATTEMPTS = 50
        const val ASSOCIATION_POLL_MS = 100L
        const val MAX_PRESENCE_PULLS = 8
    }
}
