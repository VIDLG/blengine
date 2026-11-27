package com.blehubblenghost

import android.annotation.SuppressLint
import android.bluetooth.BluetoothManager
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanResult
import android.companion.AssociationInfo
import android.companion.CompanionDeviceManager
import android.os.Build
import android.os.ParcelFileDescriptor
import android.util.Log
import androidx.annotation.RequiresApi
import androidx.test.platform.app.InstrumentationRegistry
import com.blehub.reactnative.bleng.AndroidRustSupervisorOwner
import dev.bleng_supervisor.ForeignCompanionPresence
import dev.bleng_supervisor.ForeignOwnerContext
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
 * The real `CompanionDeviceManager` chooser, through the library's own association command.
 *
 * This is the one companion path that cannot be simulated: the system chooser is platform UI. The
 * advertising peer is the project's nRF52840 DK_A (`BleHubMultiRfV2DKA`, service `fd600000-...`),
 * started by the host. Because the association is created with `notifyOnDeviceNearby = true`, the
 * library arms presence itself, so the first `present = true` is a real system observation of the
 * advertising DK.
 *
 * Two tests share the chooser setup:
 * - [chooserAssociationArmsPresenceAndReportsTheAdvertisingPeer] completes the disappearance with
 *   CDM's own hook, so it is bounded and needs no one at the device.
 * - [realDisappearanceAfterThePeerLeaves] waits for a **real** disappearance (the host stops the
 *   fixture's advertising, or a human carries it away). It logs `WALK_AWAY_NOW` after the appear.
 */
@RequiresApi(Build.VERSION_CODES.TIRAMISU)
class BlengCompanionAssociationInstrumentedTest {
    @Test
    fun chooserAssociationArmsPresenceAndReportsTheAdvertisingPeer() = runBlocking {
        assumeTrue(
            "the CDM chooser and presence path need Android 13 or newer",
            Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU,
        )
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val manager = requireNotNull(
            context.getSystemService(CompanionDeviceManager::class.java),
        ) { "CompanionDeviceManager is unavailable" }
        assertFixtureVisible(context, managerAdapter(context))
        val session = startOwner(context)
        var associatedMac: String? = null
        try {
            val association = associateThroughChooser(context, manager, session)
            associatedMac = association.deviceMacAddress?.toString()
            val observation = openPresenceLane(session)

            // A real appear is opportunistic: the platform's own presence scanner may not deliver
            // it promptly on every OEM build. The lane itself is proven deterministically with the
            // platform's presence hook below.
            val realAppear = runCatching {
                awaitPresenceState(observation, expected = true, REAL_APPEAR_WINDOW_MS)
            }.getOrNull()
            Log.i("BleHubPresence", "realAppear=${realAppear?.key ?: "none"}")

            shell("cmd companiondevice simulate-device-appeared ${association.id}")
            val present = awaitPresenceState(observation, expected = true, PRESENCE_TIMEOUT_MS)
            assertTrue("presence keys are never empty", present.key.isNotEmpty())

            shell("cmd companiondevice simulate-device-disappeared ${association.id}")
            val absent = awaitPresenceState(observation, expected = false, PRESENCE_TIMEOUT_MS)
            assertEquals("the same peer keeps one owner-scoped key", present.key, absent.key)
        } finally {
            cleanup(context, session.owner, associatedMac)
        }
    }

    @Test
    fun realDisappearanceAfterThePeerLeaves() = runBlocking {
        assumeTrue(
            "the CDM chooser and presence path need Android 13 or newer",
            Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU,
        )
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val manager = requireNotNull(
            context.getSystemService(CompanionDeviceManager::class.java),
        ) { "CompanionDeviceManager is unavailable" }
        assertFixtureVisible(context, managerAdapter(context))
        val session = startOwner(context)
        var associatedMac: String? = null
        try {
            val association = associateThroughChooser(context, manager, session)
            associatedMac = association.deviceMacAddress?.toString()
            val observation = openPresenceLane(session)

            val present = awaitPresenceState(observation, expected = true, REAL_APPEAR_TIMEOUT_MS)
            assertTrue("presence keys are never empty", present.key.isNotEmpty())
            // The host (or a human) now removes the peer: stop the fixture's advertising or carry
            // the DK out of range. Anything else keeps this test waiting and failing loudly.
            Log.i("BleHubPresence", "WALK_AWAY_NOW key=${present.key}")

            val absent = awaitPresenceState(observation, expected = false, WALK_AWAY_TIMEOUT_MS)
            assertEquals("the same peer keeps one owner-scoped key", present.key, absent.key)
        } finally {
            cleanup(context, session.owner, associatedMac)
        }
    }

    private data class OwnerSession(
        val owner: AndroidRustSupervisorOwner,
        val context: ForeignOwnerContext,
    )

    private suspend fun associateThroughChooser(
        context: android.content.Context,
        manager: CompanionDeviceManager,
        session: OwnerSession,
    ): AssociationInfo {
        val before = manager.myAssociations.map { it.id }.toSet()
        // ColorOS cancels the chooser when the requesting App has no foreground Activity
        // (background-activity-start policy). This test does not drive the UI; it only needs the
        // App resumed so the system may show its own chooser.
        shell("wm dismiss-keyguard")
        shell("am start -n ${context.packageName}/.MainActivity")
        delay(FOREGROUND_SETTLE_MS)

        val submission = session.owner.beginAssociateCompanionView(
            session.context,
            listOf(FIXTURE_SERVICE_UUID),
            "BleHub DK A",
            null,
            true,
        )
        assertNull("association submission must be accepted", submission.failure)
        val receipt = requireNotNull(submission.association) {
            "association submission produced no handle"
        }
        // The chooser is system UI and, on this build, exposes no accessibility nodes. Select the
        // single filtered row with key events: DPAD_DOWN focuses the list, DPAD_CENTER opens.
        awaitChooser()
        // The window appears before its filtered list is populated; keys sent too early land on
        // the default action. Settle first, then focus and open the single row.
        delay(CHOOSER_SETTLE_MS)
        shell("input keyevent 20")
        delay(CHOOSER_FOCUS_MS)
        shell("input keyevent 23")
        val outcome = withTimeout(ASSOCIATION_TIMEOUT_MS) {
            receipt.waitResult(CHOOSER_WAIT_MS)
        }
        assertNull("the chooser must create the association", outcome.failure)

        val association = awaitNewAssociation(manager, before)
        Log.i(
            "BleHubPresence",
            "association id=${association.id} mac=${association.deviceMacAddress}",
        )
        return association
    }

    private fun openPresenceLane(
        session: OwnerSession,
    ): SupervisedCompanionPresenceObservation {
        val lane = session.owner.beginObserveCompanionPresenceView(session.context)
        assertNull("presence observation must be accepted", lane.failure)
        return requireNotNull(lane.presenceObservation) { "presence observation produced no lane" }
    }

    private suspend fun startOwner(
        context: android.content.Context,
    ): OwnerSession {
        val owner = AndroidRustSupervisorOwner(context, "bleng-companion-association")
        val started = try {
            withTimeout(START_TIMEOUT_MS) { owner.start() }
        } catch (error: Throwable) {
            closeQuietly(owner)
            throw error
        }
        return OwnerSession(owner, started.context)
    }

    private suspend fun cleanup(
        context: android.content.Context,
        owner: AndroidRustSupervisorOwner,
        associatedMac: String?,
    ) {
        associatedMac?.let { mac ->
            shell("cmd companiondevice disassociate 0 ${context.packageName} $mac")
        }
        withTimeout(START_TIMEOUT_MS) { owner.closeResult() }
    }

    private suspend fun awaitChooser() {
        repeat(CHOOSER_APPEAR_ATTEMPTS) {
            if (shell("dumpsys window").contains("CompanionAssociationActivity")) return
            delay(CHOOSER_POLL_MS)
        }
        throw AssertionError("the system chooser did not appear")
    }

    private suspend fun awaitPresenceState(
        observation: SupervisedCompanionPresenceObservation,
        expected: Boolean,
        timeoutMs: Long,
    ): ForeignCompanionPresence = withTimeout(timeoutMs) {
        var last: ForeignCompanionPresence? = null
        repeat(MAX_PRESENCE_PULLS) {
            val next = observation.nextEvent(PULL_TIMEOUT_MS)
            val failure = next.failure
            if (failure != null) {
                // A pull deadline is the caller's bound, not a failure of the observation: keep
                // waiting under the outer timeout so a long real transition can still land.
                if (failure.code == "bleng.timeout") return@repeat
                throw AssertionError("presence observation failed: ${failure.code}")
            }
            val presence = next.presence
                ?: throw AssertionError("presence observation ended before present=$expected")
            if (presence.present == expected) return@withTimeout presence
            last = presence
        }
        throw AssertionError("presence lane never reported present=$expected (last=${last?.present})")
    }

    private suspend fun awaitNewAssociation(
        manager: CompanionDeviceManager,
        before: Set<Int>,
    ): AssociationInfo {
        repeat(ASSOCIATION_ATTEMPTS) {
            manager.myAssociations
                .firstOrNull { it.id !in before && it.deviceMacAddress != null }
                ?.let { return it }
            delay(ASSOCIATION_POLL_MS)
        }
        throw AssertionError("the chooser did not create an association with an address")
    }

    private fun shell(command: String): String =
        ParcelFileDescriptor.AutoCloseInputStream(
            InstrumentationRegistry.getInstrumentation().uiAutomation
                .executeShellCommand(command),
        ).bufferedReader().use { reader -> reader.readText() }

    private fun managerAdapter(context: android.content.Context) =
        requireNotNull(context.getSystemService(BluetoothManager::class.java)) {
            "BluetoothManager is unavailable"
        }

    @SuppressLint("MissingPermission")
    private fun assertFixtureVisible(
        context: android.content.Context,
        manager: BluetoothManager,
    ) {
        val adapter = requireNotNull(manager.adapter) { "BluetoothAdapter is unavailable" }
        val scanner = requireNotNull(adapter.bluetoothLeScanner) { "LE scanner is unavailable" }
        val seen = java.util.concurrent.ConcurrentHashMap<String, String?>()
        val callback = object : ScanCallback() {
            override fun onScanResult(callbackType: Int, result: ScanResult) {
                val uuids = result.scanRecord?.serviceUuids?.map { it.uuid.toString() }.orEmpty()
                if (uuids.any { it.equals(FIXTURE_SERVICE_UUID, ignoreCase = true) }) {
                    seen[result.device.address] = result.scanRecord?.deviceName
                }
            }
        }
        scanner.startScan(callback)
        try {
            Thread.sleep(FIXTURE_SCAN_MS)
        } finally {
            scanner.stopScan(callback)
        }
        assertTrue(
            "the phone cannot see the advertising fixture $FIXTURE_SERVICE_UUID; seen=$seen",
            seen.isNotEmpty(),
        )
        Log.i("BleHubPresence", "fixtureVisible=$seen")
    }

    private suspend fun closeQuietly(owner: AndroidRustSupervisorOwner) {
        runCatching { withTimeout(START_TIMEOUT_MS) { owner.closeResult() } }
    }

    private companion object {
        const val FIXTURE_SERVICE_UUID = "fd600000-0000-4000-8000-000000000000"
        const val START_TIMEOUT_MS = 60_000L
        const val ASSOCIATION_TIMEOUT_MS = 200_000L
        const val CHOOSER_WAIT_MS = 180_000.0
        const val PRESENCE_TIMEOUT_MS = 60_000L
        const val PULL_TIMEOUT_MS = 30_000.0
        const val ASSOCIATION_ATTEMPTS = 50
        const val ASSOCIATION_POLL_MS = 100L
        const val MAX_PRESENCE_PULLS = 16
        const val FOREGROUND_SETTLE_MS = 3_000L
        const val FIXTURE_SCAN_MS = 5_000L
        const val CHOOSER_APPEAR_ATTEMPTS = 60
        const val CHOOSER_POLL_MS = 250L
        const val CHOOSER_SETTLE_MS = 6_000L
        const val CHOOSER_FOCUS_MS = 800L
        const val REAL_APPEAR_WINDOW_MS = 8_000L
        const val REAL_APPEAR_TIMEOUT_MS = 30_000L
        const val WALK_AWAY_TIMEOUT_MS = 180_000L
    }
}
