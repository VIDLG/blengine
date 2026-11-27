package com.blehubblenghost

import androidx.test.platform.app.InstrumentationRegistry
import com.blehub.reactnative.bleng.AndroidRustSupervisorOwner
import dev.blehub.DeviceQueryKind
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Companion inventory through the real Android backend, without BLeng, the RN runtime or RF work.
 *
 * `start()` only reads the adapter snapshot: no radio command, no scan and no connection. The
 * companion query then calls `CompanionDeviceManager` through the production backend, which is the
 * part a fake backend cannot reach. An empty list is a valid answer: this asserts the engine
 * contract and that a system association is never reported as a link.
 */
class BlengCompanionInventoryInstrumentedTest {
    @Test
    fun companionInventoryIsReadThroughTheRealBackend() = runBlocking {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val owner = AndroidRustSupervisorOwner(context, "bleng-companion-inventory")
        val started = try {
            withTimeout(START_TIMEOUT_MS) { owner.start() }
        } catch (error: Throwable) {
            closeQuietly(owner)
            throw error
        }
        try {
            val submission = owner.beginQueryDevicesView(
                started.context,
                DeviceQueryKind.COMPANION,
                emptyList(),
            )
            assertNull("companion inventory must be accepted", submission.failure)
            val query = requireNotNull(submission.query) { "companion inventory produced no query" }
            val result = withTimeout(READ_TIMEOUT_MS) { query.waitResult(10_000.0) }
            assertNull("companion inventory must not fail", result.failure)
            val devices = requireNotNull(result.devices) { "companion inventory produced no list" }
            devices.forEach { record ->
                assertTrue("an inventory record always carries a key", record.key.isNotEmpty())
                assertFalse(
                    "a companion association is not a live link",
                    record.connected,
                )
                assertFalse(
                    "a companion association is not a bond report",
                    record.bonded,
                )
            }
        } finally {
            withTimeout(START_TIMEOUT_MS) { owner.closeResult() }
        }
    }

    private suspend fun closeQuietly(owner: AndroidRustSupervisorOwner) {
        runCatching { withTimeout(START_TIMEOUT_MS) { owner.closeResult() } }
    }

    private companion object {
        const val START_TIMEOUT_MS = 60_000L
        const val READ_TIMEOUT_MS = 60_000L
    }
}
