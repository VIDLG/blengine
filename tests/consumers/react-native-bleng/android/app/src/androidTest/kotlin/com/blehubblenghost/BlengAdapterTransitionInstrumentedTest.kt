package com.blehubblenghost

import android.util.Log
import androidx.test.platform.app.InstrumentationRegistry
import com.facebook.react.bridge.BridgeReactContext
import com.facebook.react.bridge.ReactApplicationContext
import com.facebook.soloader.nativeloader.NativeLoader
import com.facebook.soloader.nativeloader.SystemDelegate
import com.margelo.nitro.JNIOnLoad
import com.margelo.nitro.NitroModules
import com.margelo.nitro.blehubreactnativebleng.BleHubBlengEngineLifecycleModule
import com.margelo.nitro.blehubreactnativebleng.BleHubReactNativeBlengPackage
import com.margelo.nitro.blehubreactnativebleng.BlengAdapterNext
import com.margelo.nitro.blehubreactnativebleng.BlengAdapterObservationSubmission
import com.margelo.nitro.blehubreactnativebleng.BlengAdapterSnapshot
import com.margelo.nitro.blehubreactnativebleng.BlengAdapterState
import com.margelo.nitro.blehubreactnativebleng.BlengConnectResult
import com.margelo.nitro.blehubreactnativebleng.BlengConnectSubmission
import com.margelo.nitro.blehubreactnativebleng.BlengDisconnectSubmission
import com.margelo.nitro.blehubreactnativebleng.BlengGattDiscoveryResult
import com.margelo.nitro.blehubreactnativebleng.BlengGattDiscoverySubmission
import com.margelo.nitro.blehubreactnativebleng.BlengReadResult
import com.margelo.nitro.blehubreactnativebleng.BlengReadSubmission
import com.margelo.nitro.blehubreactnativebleng.BlengScanNext
import com.margelo.nitro.blehubreactnativebleng.BlengScanOptions
import com.margelo.nitro.blehubreactnativebleng.BlengScanStopSubmission
import com.margelo.nitro.blehubreactnativebleng.BlengScanSubmission
import com.margelo.nitro.blehubreactnativebleng.HybridBlengConnectionSpec
import com.margelo.nitro.blehubreactnativebleng.HybridBlengGattCharacteristicSpec
import com.margelo.nitro.blehubreactnativebleng.HybridBlengGattSessionSpec
import com.margelo.nitro.blehubreactnativebleng.HybridBlengPeripheralSpec
import com.margelo.nitro.blehubreactnativebleng.HybridBlengScanSpec
import com.margelo.nitro.blehubreactnativebleng.HybridBlengCentral
import com.margelo.nitro.core.ArrayBuffer
import com.margelo.nitro.core.Promise
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Before
import org.junit.Test

/**
 * Adapter OFF -> ON recovery, driven from inside the formal App's own process.
 *
 * The host owns the radio: it turns the phone's Bluetooth off before this scene starts and back on
 * when the App reports the refused scan. This test owns only the product surface: startup must
 * still succeed and *report* the real adapter state (never claim power), a scan in that state must
 * not start, and the App's own retry must produce a working owner, link and read once the adapter
 * returns.
 *
 * Markers go through logcat so the host can act on them; they are diagnostics, never a result.
 */
class BlengAdapterTransitionInstrumentedTest {
    private companion object {
        const val TAG = "BLEHUB_RN_ADAPTER"
        const val SETTLE_TIMEOUT_SECONDS = 60L
        const val OPERATION_TIMEOUT_MS = 10_000.0
        const val RESTORE_TIMEOUT_SECONDS = 60L
        const val RETRY_GAP_MILLIS = 250L

        fun marker(name: String) {
            Log.i(TAG, name)
        }

        fun argument(name: String): String = requireNotNull(
            InstrumentationRegistry.getArguments().getString(name),
        ) { "$name instrumentation argument is required" }

        fun hex(value: ArrayBuffer): String =
            value.toByteArray().joinToString("") { "%02x".format(it) }
    }

    private lateinit var central: HybridBlengCentral
    private var previousReactContext: ReactApplicationContext? = null

    @Before
    fun initializeNitroRuntime() {
        // Opt-in only: a run that does not declare the scene must not touch the radio.
        assumeTrue(
            "requires the leased adapter-transition host",
            InstrumentationRegistry.getArguments().getString("blehubAdapterTransition") ==
                    "adapter-transition",
        )
        NativeLoader.initIfUninitialized(SystemDelegate())
        JNIOnLoad.initializeNativeNitro()
        previousReactContext = NitroModules.applicationContext
        val context = BridgeReactContext(InstrumentationRegistry.getInstrumentation().targetContext)
        NitroModules.applicationContext = context
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

    @Test
    fun refusedScanRecoversThroughRetryOnceTheAdapterIsBack() {
        // The host turned the adapter off before this process started the scene. Startup still
        // succeeds: the product truth is that it *reports* the real adapter state instead of
        // pretending to be powered on.
        val startup = awaitSettlement(central.initialize()).getOrThrow().snapshotOrThrow()
        assertEquals(
            "a start while the radio is off must report the real adapter state",
            BlengAdapterState.POWEREDOFF,
            startup.state,
        )

        val serviceUuid = argument("serviceUuid").lowercase()
        val localName = argument("localName")
        val characteristicUuid = argument("readUuid").lowercase()
        val expected = argument("readHex").lowercase()
        val options = BlengScanOptions(
            serviceUuids = arrayOf(serviceUuid),
            namePrefix = null,
            exactName = null,
            companyId = null,
            minRssi = null,
            manufacturerData = null,
            android = null,
            allowDuplicates = false,
        )

        // The user-visible refusal: with the radio off a scan must not become a started scan.
        // The operation may be refused at submission or end with a terminal; either way it must
        // report adapter_unavailable, never a started scan that cannot observe anything.
        val refusal = when (val submission = central.beginScan(options)) {
            is BlengScanSubmission.First -> awaitSettlement(
                submission.value.scan.waitUntilStarted(OPERATION_TIMEOUT_MS),
            ).getOrThrow().failure

            is BlengScanSubmission.Second -> submission.value.failure
        }
        checkNotNull(refusal) { "a scan started while the adapter was reported off" }
        assertEquals(
            "a scan while the radio is off must end adapter_unavailable",
            "bleng.operation.adapter_unavailable",
            refusal.code,
        )
        // The adapter lane is the product surface for "the radio came back". Park one pull before
        // the host flips the radio, so the snapshot can only come from a real platform change.
        val stream = when (val submission = central.beginAdapterObservation()) {
            is BlengAdapterObservationSubmission.First -> submission.value.adapterObservation
            is BlengAdapterObservationSubmission.Second ->
                error("adapter observation was refused: ${submission.value.failure.code}")
        }
        val observed = stream.nextEvent(RESTORE_TIMEOUT_SECONDS * 1000.0)
        marker("adapter-off")
        val streamed = awaitSettlement(observed, "adapter stream").getOrThrow()
        when (streamed) {
            is BlengAdapterNext.First ->
                assertEquals(
                    "the adapter lane must report the radio the platform restored",
                    BlengAdapterState.POWEREDON,
                    streamed.value.snapshot.state,
                )

            is BlengAdapterNext.Second -> error("adapter observation ended before the radio returned")
            is BlengAdapterNext.Third -> error("adapter observation failed: ${streamed.value.failure.code}")
        }
        marker("adapter-stream-on")
        // A refused scan does not fault the owner, but recovery is still its own new attempt.
        central.close().await("off owner close")

        // The host turns the adapter back on when it reads the marker above. The App's recovery is
        // its own retry loop, and only a *powered-on* report counts as recovered: a successful
        // start that still reports POWEREDOFF is not recovery.
        var recovered: HybridBlengCentral? = null
        var snapshot: BlengAdapterSnapshot? = null
        val deadline = System.currentTimeMillis() + RESTORE_TIMEOUT_SECONDS * 1000
        while (System.currentTimeMillis() < deadline) {
            val candidate = HybridBlengCentral()
            val result = awaitSettlement(candidate.initialize())
            val candidateSnapshot = result.getOrNull()?.asFirstOrNull()?.snapshot
            if (candidateSnapshot?.state == BlengAdapterState.POWEREDON) {
                recovered = candidate
                snapshot = candidateSnapshot
                break
            }
            candidate.close().await("refused retry close")
            Thread.sleep(RETRY_GAP_MILLIS)
        }
        checkNotNull(recovered) { "the adapter never returned within ${RESTORE_TIMEOUT_SECONDS}s" }
        central = recovered
        assertNotNull("a started owner must report its adapter snapshot", snapshot)
        marker("adapter-restored")

        val scan = when (val result = central.beginScan(
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
        )) {
            is BlengScanSubmission.First -> result.value.scan
            is BlengScanSubmission.Second -> error("scan submission failed: ${result.value}")
        }
        marker("scan-submitted")
        scan.waitUntilStarted(OPERATION_TIMEOUT_MS).await("scan start")
        marker("scan-started")

        val peripheral = observe(scan, serviceUuid, localName)
        marker("advertisement")
        when (val stop = scan.beginStop()) {
            is BlengScanStopSubmission.First ->
                stop.value.stop.wait(OPERATION_TIMEOUT_MS).await("scan stop")

            is BlengScanStopSubmission.Second ->
                error("scan stop submission failed: ${stop.value}")
        }
        marker("scan-stopped")

        val connection = connect(peripheral)
        marker("connected")
        val session = discover(connection)
        marker("gatt")
        val value = read(characteristic(session, characteristicUuid))
        assertEquals(
            "the recovered link read back a value the fixture did not provision",
            expected,
            value,
        )
        marker("io")

        val disconnect = when (val result = connection.beginDisconnect()) {
            is BlengDisconnectSubmission.First -> result.value.disconnect
            is BlengDisconnectSubmission.Second ->
                error("disconnect submission failed: ${result.value}")
        }
        disconnect.wait(OPERATION_TIMEOUT_MS).await("disconnect")
        marker("done")
    }

    private fun observe(
        scan: HybridBlengScanSpec,
        serviceUuid: String,
        localName: String,
    ): HybridBlengPeripheralSpec {
        val deadline = System.currentTimeMillis() + SETTLE_TIMEOUT_SECONDS * 1000
        while (System.currentTimeMillis() < deadline) {
            when (val next = scan.nextEvent(OPERATION_TIMEOUT_MS).await("scan observation")) {
                is BlengScanNext.First -> {
                    val advertisement = next.value.advertisement
                    if (
                        advertisement.localName == localName &&
                        serviceUuid in advertisement.advertisedServices.map { it.lowercase() }
                    ) {
                        return advertisement.peripheral
                    }
                }

                is BlengScanNext.Second -> error("scan ended: ${next.value.ended}")
                is BlengScanNext.Third -> {
                    val failure = next.value.failure
                    check(failure.domain.toString().contains("OBSERVATION", true)) {
                        "scan observation failed: $failure"
                    }
                }
            }
        }
        error("the declared peer never advertised")
    }

    private fun connect(peripheral: HybridBlengPeripheralSpec): HybridBlengConnectionSpec {
        val connect = when (val result = peripheral.beginConnect()) {
            is BlengConnectSubmission.First -> result.value.connect
            is BlengConnectSubmission.Second -> error("connect submission failed: ${result.value}")
        }
        return when (val settled = connect.wait(OPERATION_TIMEOUT_MS).await("connect")) {
            is BlengConnectResult.First -> settled.value.connection
            is BlengConnectResult.Second -> error("connect failed: ${settled.value.failure}")
        }
    }

    private fun discover(connection: HybridBlengConnectionSpec): HybridBlengGattSessionSpec {
        val discovery = when (val result = connection.beginDiscoverGatt()) {
            is BlengGattDiscoverySubmission.First -> result.value.discovery
            is BlengGattDiscoverySubmission.Second ->
                error("discovery submission failed: ${result.value}")
        }
        return when (val settled = discovery.wait(OPERATION_TIMEOUT_MS).await("discovery")) {
            is BlengGattDiscoveryResult.First -> settled.value.session
            is BlengGattDiscoveryResult.Second -> error("discovery failed: ${settled.value.failure}")
        }
    }

    private fun characteristic(
        session: HybridBlengGattSessionSpec,
        uuid: String,
    ): HybridBlengGattCharacteristicSpec {
        val found = session.services()
            .flatMap { it.characteristics().asList() }
            .filter { it.uuid().lowercase() == uuid }
        require(found.size == 1) {
            "the discovered topology does not expose exactly one $uuid characteristic: ${found.size}"
        }
        return found.single()
    }

    private fun read(characteristic: HybridBlengGattCharacteristicSpec): String {
        val read = when (val result = characteristic.beginRead()) {
            is BlengReadSubmission.First -> result.value.read
            is BlengReadSubmission.Second -> error("read submission failed: ${result.value}")
        }
        return when (val settled = read.wait(OPERATION_TIMEOUT_MS).await("read")) {
            is BlengReadResult.First -> hex(settled.value.value)
            is BlengReadResult.Second -> error("read failed: ${settled.value.failure}")
        }
    }

    private fun <T> Promise<T>.await(label: String): T = awaitSettlement(this, label).getOrThrow()

    private fun <T> awaitSettlement(
        promise: Promise<T>,
        label: String = "promise",
    ): Result<T> {
        val settled = CountDownLatch(1)
        val result = AtomicReference<Result<T>>()
        promise.then {
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
        return result.get()
    }
}
