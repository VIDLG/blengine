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
import com.margelo.nitro.blehubreactnativebleng.BlengEndReason
import com.margelo.nitro.blehubreactnativebleng.BlengConnectionNext
import com.margelo.nitro.blehubreactnativebleng.BlengDisconnectSubmission
import com.margelo.nitro.blehubreactnativebleng.BlengGattDiscoveryResult
import com.margelo.nitro.blehubreactnativebleng.BlengFailureInfo
import com.margelo.nitro.blehubreactnativebleng.BlengGattDiscoverySubmission
import com.margelo.nitro.blehubreactnativebleng.BlengNotificationMode
import com.margelo.nitro.blehubreactnativebleng.BlengNotificationModeRequest
import com.margelo.nitro.blehubreactnativebleng.BlengSubscriptionNext
import com.margelo.nitro.blehubreactnativebleng.BlengSubscriptionResult
import com.margelo.nitro.blehubreactnativebleng.BlengSubscriptionSubmission
import com.margelo.nitro.blehubreactnativebleng.BlengReadResult
import com.margelo.nitro.blehubreactnativebleng.BlengReadSubmission
import com.margelo.nitro.blehubreactnativebleng.BlengScanNext
import com.margelo.nitro.blehubreactnativebleng.BlengScanOptions
import com.margelo.nitro.blehubreactnativebleng.BlengScanStopSubmission
import com.margelo.nitro.blehubreactnativebleng.BlengScanSubmission
import com.margelo.nitro.blehubreactnativebleng.BlengWriteSubmission
import com.margelo.nitro.blehubreactnativebleng.BlengWriteType
import com.margelo.nitro.blehubreactnativebleng.HybridBlengConnectionSpec
import com.margelo.nitro.blehubreactnativebleng.HybridBlengGattCharacteristicSpec
import com.margelo.nitro.blehubreactnativebleng.HybridBlengGattSessionSpec
import com.margelo.nitro.blehubreactnativebleng.HybridBlengSubscriptionSpec
import com.margelo.nitro.blehubreactnativebleng.HybridBlengPeripheralSpec
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
import org.junit.Before
import org.junit.Test
import org.junit.Assume.assumeTrue

/**
 * Two links of one owner, on real hardware, through the formal App's own native surface.
 *
 * What this closes is *isolation*: two independently advertised peripherals are connected at the
 * same time, interleaved I/O is routed per link, and when one peer ends its own link the other
 * keeps delivering a value the host applied afterwards. Nothing here is inferred from a single
 * link's silence, and the host owns both the peers and the timing: it applies values and ends the
 * failing link's peer, then asserts this test observed the consequences.
 *
 * The peers are described by the host as a list (index → service UUID, local name, read/write and
 * updates characteristics, expected value, mode); there is no fixed count in this test beyond the
 * contract's own "at least two".
 *
 * Stage markers go through logcat so the host can advance its own fixtures. Markers are diagnostics,
 * never a result: every claim below is an assertion on the public surface.
 */
class BlengMultiLinkInstrumentedTest {
    private companion object {
        const val TAG = "BLEHUB_RN_MULTI"
        const val SETTLE_TIMEOUT_SECONDS = 60L
        const val VALUE_TIMEOUT_MS = 20_000.0
        const val OPERATION_TIMEOUT_MS = 10_000.0
        const val END_TIMEOUT_MS = 30_000.0

        fun marker(name: String) {
            Log.i(TAG, name)
        }

        fun argument(name: String): String = requireNotNull(
            InstrumentationRegistry.getArguments().getString(name),
        ) { "$name instrumentation argument is required" }

        fun hex(value: ArrayBuffer): String =
            value.toByteArray().joinToString("") { "%02x".format(it) }
    }

    /** One peer as the host declared it, plus the capability this run observed for it. */
    private data class Peer(
        val index: Int,
        val serviceUuid: String,
        val localName: String,
        val readWriteUuid: String,
        val updatesUuid: String,
        val valueHex: String,
        val mode: BlengNotificationMode,
    )

    private lateinit var central: HybridBlengCentral
    private var previousReactContext: com.facebook.react.bridge.ReactApplicationContext? = null

    @Before
    fun initializeNitroRuntime() {
        // Opt-in only: a run that does not declare peers must not touch the radio.
        assumeTrue(
            "requires the leased multi-fixture host",
            InstrumentationRegistry.getArguments().getString("blehubMultiLink") == "multi-link",
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
    fun twoLinksKeepTheirOwnStateWhenOnePeerEnds() {
        val peers = declaredPeers()
        val snapshot = central.initialize().await("start").snapshotOrThrow()
        assertNotNull("a started owner must report its adapter snapshot", snapshot)
        marker("started")

        val peripherals = observe(peers)
        marker("scan-stopped")

        // Two connections on one owner: the product surface allows it, so the engine must.
        val connections = peers.mapIndexed { index, peer ->
            connect(peripherals[index], index)
        }
        val sessions = peers.indices.map { index -> discover(connections[index], index) }

        // Interleaved I/O: each write must be read back from the link it was sent on. A declared
        // fault changes only its own link: that write is refused by the host, and the attribute
        // still holds what it held before the attempt.
        val faultPeer = declaredFault(peers)
        peers.forEachIndexed { index, peer ->
            val characteristic = characteristic(sessions[index], peer.readWriteUuid)
            if (index == faultPeer) {
                val before = read(characteristic, index)
                val refused = write(characteristic, writeMark(index), index)
                check(refused != null) {
                    "link $index reported a refused write as accepted"
                }
                marker("write-rejected:$index")
                assertEquals(
                    "link $index changed a value whose write was refused",
                    before,
                    read(characteristic, index),
                )
                marker("unchanged:$index")
                return@forEachIndexed
            }
            val written = writeMark(index)
            write(characteristic, written, index)?.let { error("link $index write failed: $it") }
            marker("written:$index")
            val readBack = read(characteristic, index)
            assertEquals(
                "link $index read back another link's bytes",
                written.joinToString("") { "%02x".format(it) },
                readBack,
            )
            marker("io:$index")
        }

        val subscriptions = peers.indices.map { index ->
            subscribe(sessions[index], peers[index], index)
        }

        // Both links deliver before either is disturbed: the baseline the isolation claim needs.
        peers.indices.forEach { index ->
            val value = pull(subscriptions[index], index)
            assertEquals(
                "link $index delivered a value the host did not apply",
                peers[index].valueHex,
                value,
            )
            marker("value:$index")
        }

        // The host ends only the first peer's link. Nothing on this side asks for it.
        marker("drop-requested:0")
        val ending = when (
            val next = connections[0].awaitEnd(END_TIMEOUT_MS).await("link end")
        ) {
            is BlengConnectionNext.First -> next.value.ended
            is BlengConnectionNext.Second -> error("link end failed: ${next.value.failure}")
        }
        assertEquals(
            "a peer-initiated end must not be attributed to this side",
            BlengEndReason.UNREQUESTED,
            ending.reason,
        )
        marker("dropped:0")

        // Every surviving link is unaffected: each still receives a value the host applies
        // *after* the other peer ended. Contiguity is the isolation claim, so this is not
        // a single-peer special case.
        for (index in 1 until peers.size) {
            marker("survivor-requested:$index")
            val survivor = pull(subscriptions[index], index)
            assertEquals(
                "link $index lost a value applied after peer 0 ended",
                peers[index].valueHex,
                survivor,
            )
            marker("survivor:$index")
        }

        // And each survivor's own end is still its own fact, reported the same way.
        for (index in 1 until peers.size) {
            disconnect(connections[index], index)
        }
        marker("done")
    }

    /** The host-declared fault as a peer index; absent and `none` are the same declared absence. */
    private fun declaredFault(peers: List<Peer>): Int? {
        val text = InstrumentationRegistry.getArguments().getString("blehubFault").orEmpty()
        if (text.isEmpty() || text == "none") return null
        val parts = text.split(":", limit = 2)
        require(parts.size == 2 && parts[0] == "write-reject") {
            "unsupported declared fault: $text"
        }
        val index = parts[1].toIntOrNull()
        require(index != null && index in peers.indices) {
            "unsupported declared fault: $text"
        }
        return index
    }

    private fun declaredPeers(): List<Peer> {
        val count = argument("blehubPeerCount").toInt()
        require(count >= 2) { "a multi-link scene needs at least two declared peers" }
        return (0 until count).map { index ->
            val mode = when (argument("peer${index}Mode")) {
                "notification" -> BlengNotificationMode.NOTIFICATION
                "indication" -> BlengNotificationMode.INDICATION
                else -> error("peer${index}Mode must be notification or indication")
            }
            Peer(
                index = index,
                serviceUuid = argument("peer${index}ServiceUuid").lowercase(),
                localName = argument("peer${index}LocalName"),
                readWriteUuid = argument("peer${index}ReadWriteUuid").lowercase(),
                updatesUuid = argument("peer${index}UpdatesUuid").lowercase(),
                valueHex = argument("peer${index}ValueHex").lowercase(),
                mode = mode,
            )
        }
    }

    /** Scan once and return one observed capability per declared peer, in declaration order. */
    private fun observe(peers: List<Peer>): List<HybridBlengPeripheralSpec> {
        val submission = central.beginScan(
            BlengScanOptions(
                serviceUuids = peers.map { it.serviceUuid }.toTypedArray(),
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

        val observed = arrayOfNulls<HybridBlengPeripheralSpec>(peers.size)
        var remaining = peers.size
        val deadline = System.currentTimeMillis() + SETTLE_TIMEOUT_SECONDS * 1000
        while (remaining > 0) {
            check(System.currentTimeMillis() < deadline) {
                "only ${peers.size - remaining}/${peers.size} declared peers advertised"
            }
            when (val next = scan.nextEvent(VALUE_TIMEOUT_MS).await("scan observation")) {
                is BlengScanNext.First -> {
                    val advertisement = next.value.advertisement
                    // Resolve by what the peer advertised, never by arrival order.
                    val index = peers.indexOfFirst { peer ->
                        peer.serviceUuid in advertisement.advertisedServices.map { it.lowercase() }
                    }
                    if (index >= 0 && advertisement.localName == peers[index].localName &&
                        observed[index] == null
                    ) {
                        observed[index] = advertisement.peripheral
                        remaining -= 1
                    }
                }

                is BlengScanNext.Second -> error("scan ended: ${next.value.ended.reason}")
                is BlengScanNext.Third -> error("scan observation failed: ${next.value.failure}")
            }
        }
        val stop = when (val result = scan.beginStop()) {
            is BlengScanStopSubmission.First -> result.value.stop
            is BlengScanStopSubmission.Second -> error("stop submission failed: ${result.value}")
        }
        stop.wait(OPERATION_TIMEOUT_MS).await("scan stop")
        // Markers in declaration order: arrival order is the radio's, not a fact about which
        // peer was observed, so it must not leak into the stage contract.
        observed.forEachIndexed { index, spec ->
            requireNotNull(spec) { "peer $index was never observed" }
            marker("advertisement:$index")
        }
        return observed.map { requireNotNull(it) }
    }

    private fun connect(peripheral: HybridBlengPeripheralSpec, index: Int): HybridBlengConnectionSpec {
        val connect = when (val result = peripheral.beginConnect()) {
            is BlengConnectSubmission.First -> result.value.connect
            is BlengConnectSubmission.Second ->
                error("link $index connect submission failed: ${result.value}")
        }
        val connection = when (val result = connect.wait(OPERATION_TIMEOUT_MS).await("connect")) {
            is BlengConnectResult.First -> result.value.connection
            is BlengConnectResult.Second -> error("link $index connect failed: ${result.value.failure}")
        }
        marker("connected:$index")
        return connection
    }

    private fun discover(connection: HybridBlengConnectionSpec, index: Int): HybridBlengGattSessionSpec {
        val discovery = when (val result = connection.beginDiscoverGatt()) {
            is BlengGattDiscoverySubmission.First -> result.value.discovery
            is BlengGattDiscoverySubmission.Second ->
                error("link $index discovery submission failed: ${result.value}")
        }
        val session = when (val result = discovery.wait(OPERATION_TIMEOUT_MS).await("discovery")) {
            is BlengGattDiscoveryResult.First -> result.value.session
            is BlengGattDiscoveryResult.Second ->
                error("link $index discovery failed: ${result.value.failure}")
        }
        marker("gatt:$index")
        return session
    }

    private fun subscribe(
        session: HybridBlengGattSessionSpec,
        peer: Peer,
        index: Int,
    ): HybridBlengSubscriptionSpec {
        val entry = characteristic(session, peer.updatesUuid)
        val properties: BlengCharacteristicProperties = entry.properties()
        val request = if (properties.notifiable) {
            BlengNotificationModeRequest.REQUIRENOTIFICATION
        } else {
            require(properties.indicatable) { "${peer.updatesUuid} is neither notify nor indicate" }
            BlengNotificationModeRequest.REQUIREINDICATION
        }
        val notifications = when (val result = entry.beginSubscription(request)) {
            is BlengSubscriptionSubmission.First -> result.value.notifications
            is BlengSubscriptionSubmission.Second ->
                error("link $index enable submission failed: ${result.value}")
        }
        val receipt = when (
            val settled = notifications.waitUntilEnabled(OPERATION_TIMEOUT_MS).await("enable")
        ) {
            is BlengSubscriptionResult.First -> settled.value
            is BlengSubscriptionResult.Second ->
                error("link $index enable failed: ${settled.value.failure}")
        }
        assertEquals(
            "link $index selected a mode this peer does not advertise",
            peer.mode,
            receipt.mode,
        )
        marker("subscribed:$index")
        return notifications
    }

    /** One value from this subscription; a timeout is not a delivery and never satisfies a scene. */
    private fun pull(notifications: HybridBlengSubscriptionSpec, index: Int): String {
        val deadline = System.currentTimeMillis() + SETTLE_TIMEOUT_SECONDS * 1000
        while (System.currentTimeMillis() < deadline) {
            when (val next = notifications.nextEvent(VALUE_TIMEOUT_MS).await("observation")) {
                is BlengSubscriptionNext.First -> return hex(next.value.value)
                is BlengSubscriptionNext.Second ->
                    error("link $index stream ended: ${next.value.ended.reason}")

                is BlengSubscriptionNext.Third -> {
                    // A caller deadline is not a fact about the link; re-arm like the App does.
                    val failure = next.value.failure
                    check(failure.domain.toString().contains("OBSERVATION", true)) {
                        "link $index observation failed: $failure"
                    }
                }
            }
        }
        error("link $index delivered nothing the host applied")
    }

    /** A distinct profile-sized payload per link, so a crossed write cannot look correct. */
    private fun writeMark(index: Int): ByteArray =
        argument("peer${index}WriteHex").lowercase()
            .chunked(2)
            .map { it.toInt(16).toByte() }
            .toByteArray()

    private fun write(
        characteristic: HybridBlengGattCharacteristicSpec,
        bytes: ByteArray,
        index: Int,
    ): BlengFailureInfo? {
        val write = when (
            val result = characteristic.beginWrite(ArrayBuffer.copy(bytes), BlengWriteType.WITHRESPONSE)
        ) {
            is BlengWriteSubmission.First -> result.value.write
            is BlengWriteSubmission.Second ->
                error("link $index write submission failed: ${result.value}")
        }
        return write.wait(OPERATION_TIMEOUT_MS).await("write").failure
    }

    private fun read(characteristic: HybridBlengGattCharacteristicSpec, index: Int): String {
        val read = when (val result = characteristic.beginRead()) {
            is BlengReadSubmission.First -> result.value.read
            is BlengReadSubmission.Second ->
                error("link $index read submission failed: ${result.value}")
        }
        return when (val settled = read.wait(OPERATION_TIMEOUT_MS).await("read")) {
            is BlengReadResult.First -> hex(settled.value.value)
            is BlengReadResult.Second -> error("link $index read failed: ${settled.value.failure}")
        }
    }

    private fun disconnect(connection: HybridBlengConnectionSpec, index: Int) {
        val disconnect = when (val result = connection.beginDisconnect()) {
            is BlengDisconnectSubmission.First -> result.value.disconnect
            is BlengDisconnectSubmission.Second ->
                error("link $index disconnect submission failed: ${result.value}")
        }
        disconnect.wait(OPERATION_TIMEOUT_MS).await("disconnect")
        marker("disconnected:$index")
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
