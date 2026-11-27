package com.blehubblenghost

import android.os.SystemClock
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
import com.margelo.nitro.blehubreactnativebleng.BlengAdapterSnapshot
import com.margelo.nitro.blehubreactnativebleng.BlengAdapterState
import com.margelo.nitro.blehubreactnativebleng.BlengAuthorizationState
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
import java.io.File
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Before
import org.junit.Test

/**
 * Permission transition, driven from inside the formal App's own process.
 *
 * The host owns every OS action: it revokes both permissions, grants them back, revokes
 * BLUETOOTH_CONNECT while a read is parked inside the fixture, and proves the OS ended that
 * process. Each method below is one phase in its own process, so "a fresh process recovers"
 * is a real process fact and not a new owner inside the same runtime.
 *
 * Product facts asserted here:
 * - denied: startup reports the real authorization (never a usable adapter) and a scan ends
 *   with a permission failure instead of starting;
 * - active: a read stays parked in the fixture until the OS ends this process, so no clean
 *   read terminal and no clean close can exist;
 * - restored: a new process scans, links, reads the provisioned value and disconnects.
 *
 * Markers are diagnostics for host routing (and carry this process's identity); they are
 * never a result.
 */
class BlengPermissionTransitionInstrumentedTest {
    private companion object {
        const val TAG = "BLEHUB_RN_PERMISSION"
        const val SETTLE_TIMEOUT_SECONDS = 60L
        const val OPERATION_TIMEOUT_MS = 10_000.0
        const val HELD_READ_TIMEOUT_MS = 50_000.0

        // Host setup barrier, not a product claim: the host arms the fixture's read hold after it
        // observes `active-connected`. If the arm did not happen, the host fails the lane at
        // `require_hold_held` instead of passing it.
        const val HOST_SETUP_SETTLE_MILLIS = 1_500L

        fun marker(name: String) {
            Log.i(TAG, name)
        }

        fun argument(name: String): String = requireNotNull(
            InstrumentationRegistry.getArguments().getString(name),
        ) { "$name instrumentation argument is required" }

        fun hex(value: ArrayBuffer): String =
            value.toByteArray().joinToString("") { "%02x".format(it) }

        /**
         * This process's identity for the host's death gate.
         *
         * `start_ticks` is /proc/self/stat field 22 (after the parenthesised comm); the
         * wall-clock start is the same instant expressed in the wall clock, so a revoke
         * that happens later always compares newer than this value.
         */
        fun bootstrap(): String {
            val rest = File("/proc/self/stat").readText().substringAfterLast(')').trim()
            val startTicks = rest.split(' ')[19]
            val startedUnixMs = System.currentTimeMillis() -
                    (SystemClock.elapsedRealtime() - android.os.Process.getStartElapsedRealtime())
            val packageName = InstrumentationRegistry.getInstrumentation().targetContext.packageName
            return "{\"pid\":${android.os.Process.myPid()},\"uid\":${android.os.Process.myUid()}," +
                    "\"user_id\":0,\"process_name\":\"$packageName\",\"start_ticks\":$startTicks," +
                    "\"native_started_unix_ms\":$startedUnixMs}"
        }

        fun scanOptions(serviceUuid: String) = BlengScanOptions(
            serviceUuids = arrayOf(serviceUuid),
            namePrefix = null,
            exactName = null,
            companyId = null,
            minRssi = null,
            manufacturerData = null,
            android = null,
            allowDuplicates = false,
        )
    }

    private lateinit var central: HybridBlengCentral
    private var previousReactContext: ReactApplicationContext? = null

    @Before
    fun initializeNitroRuntime() {
        // Opt-in only: a run that does not declare the scene must not touch permissions.
        assumeTrue(
            "requires the leased permission-transition host",
            InstrumentationRegistry.getArguments().getString("blehubPermissionTransition") ==
                    "permission-transition",
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
    fun deniedStartupReportsPermissionAndScanDoesNotStart() {
        marker("bootstrap:" + bootstrap())
        val startup = awaitSettlement(central.initialize()).getOrThrow().snapshotOrThrow()
        assertEquals(
            "a start without BLUETOOTH_SCAN/CONNECT must report the denied authorization",
            BlengAuthorizationState.DENIED,
            startup.scanAuthorization,
        )
        marker("denied-start")

        val serviceUuid = argument("serviceUuid").lowercase()
        val scan = when (val submission = central.beginScan(scanOptions(serviceUuid))) {
            is BlengScanSubmission.First -> submission.value.scan
            is BlengScanSubmission.Second -> error("scan submission failed: ${submission.value}")
        }
        val refusal = awaitSettlement(scan.waitUntilStarted(OPERATION_TIMEOUT_MS))
            .getOrThrow()
            .failure
        checkNotNull(refusal) { "a scan started without the scan permission" }
        assertEquals(
            "a scan without the permission must end with a permission failure",
            "bleng.operation.permission",
            refusal.code,
        )
        marker("denied-scan")
    }

    @Test
    fun activeReadDiesWithTheProcess() {
        marker("bootstrap:" + bootstrap())
        val startup = awaitSettlement(central.initialize()).getOrThrow().snapshotOrThrow()
        assertEquals(
            "the active phase requires granted scan authorization",
            BlengAuthorizationState.AUTHORIZED,
            startup.scanAuthorization,
        )
        assertEquals(
            "the active phase requires granted connection authorization",
            BlengAuthorizationState.AUTHORIZED,
            startup.connectionAuthorization,
        )
        marker("active-start")

        val serviceUuid = argument("serviceUuid").lowercase()
        val localName = argument("localName")
        val characteristicUuid = argument("readUuid").lowercase()
        val scan = when (val submission = central.beginScan(scanOptions(serviceUuid))) {
            is BlengScanSubmission.First -> submission.value.scan
            is BlengScanSubmission.Second -> error("scan submission failed: ${submission.value}")
        }
        val started = scan.waitUntilStarted(OPERATION_TIMEOUT_MS).await("scan start")
        check(started.failure == null) { "scan start failed: ${started.failure}" }
        marker("active-scan")

        val peripheral = observe(scan, serviceUuid, localName)
        when (val stop = scan.beginStop()) {
            is BlengScanStopSubmission.First ->
                stop.value.stop.wait(OPERATION_TIMEOUT_MS).await("scan stop")

            is BlengScanStopSubmission.Second ->
                error("scan stop submission failed: ${stop.value}")
        }
        val connection = connect(peripheral)
        // The host arms the fixture's read hold here; arming needs a live link. Wait for that
        // host-side setup before dispatching the read, so the read is the one that gets parked.
        marker("active-connected")
        Thread.sleep(HOST_SETUP_SETTLE_MILLIS)
        val session = discover(connection)
        marker("active-gatt")

        val read = when (val submission = characteristic(session, characteristicUuid).beginRead()) {
            is BlengReadSubmission.First -> submission.value.read
            is BlengReadSubmission.Second -> error("read submission failed: ${submission.value}")
        }
        marker("armed")
        // The fixture parks this read. The host then revokes BLUETOOTH_CONNECT and the OS
        // ends this process: neither a read terminal nor a close may be produced. If this
        // returns, the death scene is wrong and the host's death gate fails closed.
        read.wait(HELD_READ_TIMEOUT_MS).await("held read")
        marker("unexpected-held-read-terminal")
    }

    @Test
    fun restoredProcessDeliversTheValue() {
        marker("bootstrap:" + bootstrap())
        val startup = awaitSettlement(central.initialize()).getOrThrow().snapshotOrThrow()
        assertEquals(
            "the restored phase requires granted scan authorization",
            BlengAuthorizationState.AUTHORIZED,
            startup.scanAuthorization,
        )
        assertEquals(
            "the restored phase requires granted connection authorization",
            BlengAuthorizationState.AUTHORIZED,
            startup.connectionAuthorization,
        )
        marker("restored-start")

        val serviceUuid = argument("serviceUuid").lowercase()
        val localName = argument("localName")
        val characteristicUuid = argument("readUuid").lowercase()
        val expected = argument("readHex").lowercase()
        val scan = when (val submission = central.beginScan(scanOptions(serviceUuid))) {
            is BlengScanSubmission.First -> submission.value.scan
            is BlengScanSubmission.Second -> error("scan submission failed: ${submission.value}")
        }
        val started = scan.waitUntilStarted(OPERATION_TIMEOUT_MS).await("scan start")
        check(started.failure == null) { "scan start failed: ${started.failure}" }
        marker("restored-scan")

        val peripheral = observe(scan, serviceUuid, localName)
        when (val stop = scan.beginStop()) {
            is BlengScanStopSubmission.First ->
                stop.value.stop.wait(OPERATION_TIMEOUT_MS).await("scan stop")

            is BlengScanStopSubmission.Second ->
                error("scan stop submission failed: ${stop.value}")
        }
        val connection = connect(peripheral)
        marker("restored-connected")
        val session = discover(connection)
        marker("restored-gatt")
        assertEquals(
            "the restored process read back a value the fixture did not provision",
            expected,
            read(characteristic(session, characteristicUuid)),
        )
        marker("restored-io")

        val disconnect = when (val submission = connection.beginDisconnect()) {
            is BlengDisconnectSubmission.First -> submission.value.disconnect
            is BlengDisconnectSubmission.Second ->
                error("disconnect submission failed: ${submission.value}")
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
        val connect = when (val submission = peripheral.beginConnect()) {
            is BlengConnectSubmission.First -> submission.value.connect
            is BlengConnectSubmission.Second -> error("connect submission failed: ${submission.value}")
        }
        return when (val settled = connect.wait(OPERATION_TIMEOUT_MS).await("connect")) {
            is BlengConnectResult.First -> settled.value.connection
            is BlengConnectResult.Second -> error("connect failed: ${settled.value.failure}")
        }
    }

    private fun discover(connection: HybridBlengConnectionSpec): HybridBlengGattSessionSpec {
        val discovery = when (val submission = connection.beginDiscoverGatt()) {
            is BlengGattDiscoverySubmission.First -> submission.value.discovery
            is BlengGattDiscoverySubmission.Second ->
                error("discovery submission failed: ${submission.value}")
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
        val read = when (val submission = characteristic.beginRead()) {
            is BlengReadSubmission.First -> submission.value.read
            is BlengReadSubmission.Second -> error("read submission failed: ${submission.value}")
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
