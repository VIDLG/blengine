package com.blehubblenghost

import android.util.Log
import androidx.test.platform.app.InstrumentationRegistry
import com.facebook.react.bridge.BridgeReactContext
import com.facebook.soloader.nativeloader.NativeLoader
import com.facebook.soloader.nativeloader.SystemDelegate
import com.margelo.nitro.JNIOnLoad
import com.margelo.nitro.NitroModules
import com.margelo.nitro.blehubreactnativebleng.BleHubBlengEngineLifecycleModule
import com.margelo.nitro.blehubreactnativebleng.BleHubReactNativeBlengPackage
import com.margelo.nitro.blehubreactnativebleng.BlengCharacteristicProperties
import com.margelo.nitro.blehubreactnativebleng.BlengConnectResult
import com.margelo.nitro.blehubreactnativebleng.BlengConnectSubmission
import com.margelo.nitro.blehubreactnativebleng.BlengFailure
import com.margelo.nitro.blehubreactnativebleng.BlengGattDiscoveryResult
import com.margelo.nitro.blehubreactnativebleng.BlengGattDiscoverySubmission
import com.margelo.nitro.blehubreactnativebleng.BlengSubscriptionDisableSubmission
import com.margelo.nitro.blehubreactnativebleng.BlengEndReason
import com.margelo.nitro.blehubreactnativebleng.BlengNotificationMode
import com.margelo.nitro.blehubreactnativebleng.BlengNotificationModeRequest
import com.margelo.nitro.blehubreactnativebleng.BlengSubscriptionNext
import com.margelo.nitro.blehubreactnativebleng.BlengSubscriptionResult
import com.margelo.nitro.blehubreactnativebleng.BlengSubscriptionSubmission
import com.margelo.nitro.blehubreactnativebleng.BlengScanNext
import com.margelo.nitro.blehubreactnativebleng.BlengScanOptions
import com.margelo.nitro.blehubreactnativebleng.BlengScanStopSubmission
import com.margelo.nitro.blehubreactnativebleng.BlengScanSubmission
import com.margelo.nitro.blehubreactnativebleng.HybridBlengGattSessionSpec
import com.margelo.nitro.blehubreactnativebleng.HybridBlengSubscription
import com.margelo.nitro.blehubreactnativebleng.HybridBlengCentral
import com.margelo.nitro.core.Promise
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * Real-device notification RF evidence through the formal App's own native surface.
 *
 * The observable this closes is delivery: a value the peripheral actually transmitted must appear
 * in the subscription the App's public capability opened, and disabling that subscription must end
 * the stream with `disabled` rather than leaving it to a link or owner loss.
 *
 * The host owns the peripheral and the timing: it waits for the `subscribed` marker below, makes the
 * fixture transmit, and asserts the applied bytes against what this test observed. Stage markers are
 * emitted through logcat so the host can drive its own coordination; the markers are diagnostics,
 * never a result. Nothing here installs a driver, edits settings, or bypasses the public API.
 */
class BlengNotificationInstrumentedTest {
    private companion object {
        const val TAG = "BLEHUB_RN_NOTIFY"
        const val SETTLE_TIMEOUT_SECONDS = 30L
        const val VALUE_TIMEOUT_MS = 20_000.0
        const val OPERATION_TIMEOUT_MS = 10_000.0

        fun marker(name: String, detail: String = ""): Unit {
            Log.i(TAG, if (detail.isEmpty()) name else "$name:$detail")
        }

        fun argument(name: String): String = requireNotNull(
            InstrumentationRegistry.getArguments().getString(name),
        ) { "$name instrumentation argument is required" }
    }

    private lateinit var central: HybridBlengCentral
    private var previousReactContext: com.facebook.react.bridge.ReactApplicationContext? = null

    @Before
    fun initializeNitroRuntime() {
        NativeLoader.initIfUninitialized(SystemDelegate())
        JNIOnLoad.initializeNativeNitro()
        previousReactContext = NitroModules.applicationContext
        val context = BridgeReactContext(InstrumentationRegistry.getInstrumentation().targetContext)
        NitroModules.applicationContext = context
        // Same production path as the App: register the engine lifecycle module for this context.
        BleHubReactNativeBlengPackage().getModule(
            BleHubBlengEngineLifecycleModule.NAME,
            context,
        )
        central = HybridBlengCentral()
    }

    @After
    fun closeOwner() {
        try {
            if (this::central.isInitialized) central.close().await("owner close")
        } finally {
            NitroModules.applicationContext = previousReactContext
        }
    }

    /** One opened subscription plus the facts the host declared for it. */
    private data class Subscription(
        val notifications: com.margelo.nitro.blehubreactnativebleng.HybridBlengSubscriptionSpec,
        val connection: com.margelo.nitro.blehubreactnativebleng.HybridBlengConnectionSpec,
        val mode: BlengNotificationMode,
        val value: String,
    )

    @Test
    fun realPeripheralValueReachesTheSubscriptionAndTheStreamEndsOnDisable() {
        val subscription = openSubscription()
        val notifications = subscription.notifications
        val value = readValues(notifications, 1).single()
        assertEquals(
            "the pulled value is not the value the peripheral applied",
            subscription.value,
            value,
        )
        marker("value-matched", value)
        finishSubscription(subscription)
    }

    /**
     * Repeats of the peripheral's own value are delivered once each, in send order.
     *
     * The value length is fixed per characteristic by the firmware, so this scene keeps the
     * profile's payload and varies only the count: a lost or duplicated admission shows up
     * as a count mismatch, and a reordered backlog shows up as a mismatch at its index.
     */
    @Test
    fun repeatedValuesAreNeitherLostNorDuplicated() {
        val count = argument("blehubValueCount").toInt()
        val subscription = openSubscription()
        val notifications = subscription.notifications
        marker("values-requested", count.toString())
        val values = readValues(notifications, count)
        val expected = List(count) { subscription.value }
        assertEquals("values were lost, duplicated or reordered", expected, values)
        marker("values-matched", values.size.toString())
        finishSubscription(subscription)
    }

    /**
     * A consumer that never drains fails closed instead of losing values silently.
     *
     * The host sends more values than the observation budget while this test deliberately reads
     * nothing. Overflow is a **terminal of this lane**, not an operation failure: the supervisor's
     * own rule is "a consumer that cannot keep up ends its own observation lane; the native
     * subscription and every other operation keep running — this is not a runtime-wide fault", and
     * it is reported as `ended(reason = overflowed)` with no failure (the scan lane has the same
     * shape). This test previously demanded `failure.code = delivery.value_overflow`, which that
     * contract deliberately stopped producing; the assertion now names the reason, and the other
     * two answers (a value, or a failure) are refused by name.
     */
    @Test
    fun aSlowConsumerFailsClosedAtTheValueBudget() {
        val settleSeconds = argument("blehubDrainDelaySeconds").toLong()
        val subscription = openSubscription()
        val notifications = subscription.notifications
        marker("values-requested", "overflow")
        // Bounded idle: the host needs this window to admit the whole overflow sequence.
        Thread.sleep(settleSeconds * 1000)
        val reason = when (
            val next = notifications.nextEvent(OPERATION_TIMEOUT_MS).await("overflow observation")
        ) {
            is BlengSubscriptionNext.First ->
                error("a slow consumer received a value instead of failing closed")

            is BlengSubscriptionNext.Second -> {
                val ended = next.value.ended
                if (ended.failure != null) {
                    error(
                        "the overflow end carried a failure (${ended.failure!!.code}); " +
                            "overflow is this lane's terminal, not an operation fault"
                    )
                }
                ended.reason
            }

            is BlengSubscriptionNext.Third ->
                error(
                    "a slow consumer saw an operation failure (${next.value.failure.code}) " +
                        "instead of this lane's overflow terminal"
                )
        }
        assertEquals(
            "the overflow must end this lane as unsupported-by-budget, not as a fault",
            BlengEndReason.OVERFLOWED,
            reason,
        )
        marker("overflow-observed", reason.name.lowercase())
        notifications.detach()
        disconnect(subscription.connection)
        marker("done")
    }

    private fun openSubscription(): Subscription {
        val serviceUuid = argument("blehubServiceUuid")
        val characteristicUuid = argument("blehubCharacteristicUuid").lowercase()
        val expectedValue = argument("blehubValueHex").lowercase()
        val expectedMode = when (argument("blehubMode")) {
            "notification" -> BlengNotificationMode.NOTIFICATION
            "indication" -> BlengNotificationMode.INDICATION
            else -> error("blehubMode must be notification or indication")
        }

        val snapshot = central.initialize().await("start").snapshotOrThrow()
        assertNotNull("a started owner must report its adapter snapshot", snapshot)
        marker(
            "started", "state=${snapshot.state.name.lowercase()}" +
                    ":scan=${snapshot.scanAuthorization.name.lowercase()}"
        )

        val submission = central.beginScan(
            BlengScanOptions(
                serviceUuids = arrayOf(serviceUuid),
                namePrefix = null,
                exactName = null,
                companyId = null,
                minRssi = null,
                manufacturerData = null,
                android = null,
                allowDuplicates = false,
            ),
        )
        val scan = when (submission) {
            is BlengScanSubmission.First -> submission.value.scan
            is BlengScanSubmission.Second -> error("scan submission failed: ${submission.value}")
        }
        marker("scan-submitted")
        scan.waitUntilStarted(OPERATION_TIMEOUT_MS).await("scan start")
        marker("scan-started")

        // Only a capability that this run observed may be connected; never a remembered device.
        var peripheral: com.margelo.nitro.blehubreactnativebleng.HybridBlengPeripheralSpec? = null
        val advertiseDeadline = System.currentTimeMillis() + SETTLE_TIMEOUT_SECONDS * 1000
        var observed = 0
        while (peripheral == null) {
            check(System.currentTimeMillis() < advertiseDeadline) {
                "no advertisement for $serviceUuid within the fixture window (observed=$observed)"
            }
            when (val next = scan.nextEvent(VALUE_TIMEOUT_MS).await("scan observation")) {
                is BlengScanNext.First -> {
                    observed += 1
                    val advert = next.value.advertisement
                    // Diagnostics for a filter or platform miss; the marker is not a result.
                    if (observed <= 4) {
                        marker(
                            "advertisement",
                            (advert.localName ?: "not-reported") + "#" +
                                    advert.advertisedServices.joinToString("|"),
                        )
                    }
                    if (advert.advertisedServices.any {
                            it.lowercase() == serviceUuid.lowercase()
                        }
                    ) {
                        peripheral = advert.peripheral
                    }
                }

                is BlengScanNext.Second -> error("scan ended: ${next.value.ended.reason}")
                is BlengScanNext.Third -> error("scan observation failed: ${next.value.failure}")
            }
        }
        marker("advertisement-matched")

        val stop = when (val result = scan.beginStop()) {
            is BlengScanStopSubmission.First -> result.value.stop
            is BlengScanStopSubmission.Second -> error("stop submission failed: ${result.value}")
        }
        stop.wait(OPERATION_TIMEOUT_MS).await("scan stop")
        marker("scan-stopped")

        val connect = when (val result = peripheral.beginConnect()) {
            is BlengConnectSubmission.First -> result.value.connect
            is BlengConnectSubmission.Second -> error("connect submission failed: ${result.value}")
        }
        val connection = when (val result = connect.wait(OPERATION_TIMEOUT_MS).await("connect")) {
            is BlengConnectResult.First -> result.value.connection
            is BlengConnectResult.Second -> error("connect failed: ${result.value.failure}")
        }
        marker("connected")

        val discovery = when (val result = connection.beginDiscoverGatt()) {
            is BlengGattDiscoverySubmission.First -> result.value.discovery
            is BlengGattDiscoverySubmission.Second ->
                error("discovery submission failed: ${result.value}")
        }
        val session = when (
            val result = discovery.wait(OPERATION_TIMEOUT_MS).await("discovery")
        ) {
            is BlengGattDiscoveryResult.First -> result.value.session
            is BlengGattDiscoveryResult.Second -> error("discovery failed: ${result.value.failure}")
        }
        marker("gatt-ready")

        val entry = characteristic(session, characteristicUuid)
        val properties: BlengCharacteristicProperties = entry.properties()
        // Ask for exactly what the characteristic advertises, as the App does.
        val request = if (properties.notifiable) {
            BlengNotificationModeRequest.REQUIRENOTIFICATION
        } else {
            require(properties.indicatable) { "$characteristicUuid is neither notify nor indicate" }
            BlengNotificationModeRequest.REQUIREINDICATION
        }
        val notifications = when (val result = entry.beginSubscription(request)) {
            is BlengSubscriptionSubmission.First -> result.value.notifications
            is BlengSubscriptionSubmission.Second ->
                error("enable submission failed: ${result.value}")
        }
        val receipt = when (
            val settled = notifications.waitUntilEnabled(OPERATION_TIMEOUT_MS).await("enable")
        ) {
            is BlengSubscriptionResult.First -> settled.value
            is BlengSubscriptionResult.Second ->
                error("enable failed: ${settled.value.failure}")
        }
        assertEquals("the platform must report the mode it selected", expectedMode, receipt.mode)
        marker("subscribed", expectedMode.name.lowercase())
        return Subscription(
            notifications = notifications,
            connection = connection,
            mode = expectedMode,
            value = expectedValue,
        )
    }

    /** Disable, then require the stream's own terminal; a receipt alone is not silence. */
    private fun finishSubscription(subscription: Subscription) {
        val notifications = subscription.notifications
        val disable = when (val result = notifications.beginDisable()) {
            is BlengSubscriptionDisableSubmission.First -> result.value.disable
            is BlengSubscriptionDisableSubmission.Second ->
                error("disable submission failed: ${result.value}")
        }
        disable.wait(OPERATION_TIMEOUT_MS).await("disable")
        marker("disabled")

        when (val next = notifications.nextEvent(VALUE_TIMEOUT_MS).await("stream end")) {
            is BlengSubscriptionNext.Second -> assertEquals(
                "the stream ended for a reason other than its own disable",
                BlengEndReason.REQUESTED,
                next.value.ended.reason,
            )

            is BlengSubscriptionNext.First -> error("a value arrived after the disable receipt")
            is BlengSubscriptionNext.Third -> error("stream observation failed: ${next.value.failure}")
        }
        notifications.detach()
        marker("terminal")
        disconnect(subscription.connection)
        marker("done")
    }

    /** Release the peer before the host closes the fixture: a passive close refuses a peer. */
    private fun disconnect(
        connection: com.margelo.nitro.blehubreactnativebleng.HybridBlengConnectionSpec,
    ) {
        val disconnected = when (val result = connection.beginDisconnect()) {
            is com.margelo.nitro.blehubreactnativebleng.BlengDisconnectSubmission.First ->
                result.value.disconnect

            is com.margelo.nitro.blehubreactnativebleng.BlengDisconnectSubmission.Second ->
                error("disconnect submission failed: ${result.value}")
        }
        disconnected.wait(OPERATION_TIMEOUT_MS).await("disconnect")
        marker("disconnected")
    }

    private fun readValues(
        notifications: com.margelo.nitro.blehubreactnativebleng.HybridBlengSubscriptionSpec,
        count: Int,
    ): List<String> {
        val deadline = System.currentTimeMillis() + SETTLE_TIMEOUT_SECONDS * 1000
        val values = mutableListOf<String>()
        while (values.size < count) {
            check(System.currentTimeMillis() < deadline) {
                "only ${values.size}/$count values reached the subscription"
            }
            when (val next = notifications.nextEvent(VALUE_TIMEOUT_MS).await("notification observation")) {
                is BlengSubscriptionNext.First ->
                    values.add(next.value.value.toByteArray().joinToString("") { "%02x".format(it) })

                is BlengSubscriptionNext.Second -> error("stream ended: ${next.value.ended.reason}")
                is BlengSubscriptionNext.Third -> {
                    // A caller-side observation timeout is not a dead subscription.
                    val failure: BlengFailure = next.value
                    check(failure.failure.domain.toString().contains("OBSERVATION", true)) {
                        "notification observation failed: ${failure.failure}"
                    }
                }
            }
        }
        return values
    }

    private fun characteristic(
        session: HybridBlengGattSessionSpec,
        uuid: String,
    ): com.margelo.nitro.blehubreactnativebleng.HybridBlengGattCharacteristicSpec {
        val found = session.services()
            .flatMap { it.characteristics().asList() }
            .filter { it.uuid().lowercase() == uuid }
        require(found.size == 1) {
            "the discovered topology does not expose exactly one $uuid characteristic: " +
                    found.size
        }
        return found.single()
    }

    private fun <T> Promise<T>.await(label: String): T = awaitSettlement(label).getOrThrow()

    private fun <T> Promise<T>.awaitSettlement(label: String = "promise"): Result<T> {
        val settled = CountDownLatch(1)
        val result = AtomicReference<Result<T>>()
        then {
            result.set(Result.success(it))
            settled.countDown()
        }.catch {
            result.set(Result.failure(it))
            settled.countDown()
        }
        assertTrue(
            "$label did not settle within ${SETTLE_TIMEOUT_SECONDS}s",
            settled.await(SETTLE_TIMEOUT_SECONDS, TimeUnit.SECONDS),
        )
        return requireNotNull(result.get())
    }
}
