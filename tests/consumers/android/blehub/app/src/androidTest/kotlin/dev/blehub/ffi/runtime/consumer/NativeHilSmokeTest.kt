package dev.blehub.ffi.runtime.consumer

import android.os.Bundle
import android.os.Handler
import android.os.HandlerThread
import android.os.Process
import android.os.SystemClock
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import dev.blehub.*
import dev.blehub.android.AndroidBleBackend
import java.io.File
import java.util.UUID
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import org.junit.Assert.*
import org.junit.Assume.assumeTrue
import org.junit.Test
import org.junit.runner.RunWith

/** Opt-in only: the leased host owner supplies the canonical profile and triggers one value. */
@RunWith(AndroidJUnit4::class)
class NativeHilSmokeTest {
    @Test(timeout = 110_000)
    fun productionHubSmoke() {
        val args = InstrumentationRegistry.getArguments()
        assumeTrue("requires the leased both-write native HIL host", args.getString("nativeHil") == "writes-v2")
        val runId = required(args, "runId")
        require(UUID.fromString(runId).toString() == runId)
        val serviceUuid = required(args, "serviceUuid")
        val namePrefix = required(args, "namePrefix")
        val rwUuid = required(args, "readWriteUuid")
        val updatesUuid = required(args, "updatesUuid")
        val initial = hex(required(args, "initialHex"))
        val notification = hex(required(args, "notificationHex"))
        val written = hex(required(args, "writeHex"))
        val withoutResponse = hex(required(args, "writeWithoutResponseHex"))
        require(written.size == initial.size && !written.contentEquals(initial)) {
            "smoke write must preserve the profile value length and change its bytes"
        }
        require(
            withoutResponse.size == initial.size &&
                    !withoutResponse.contentEquals(initial) && !withoutResponse.contentEquals(written)
        ) {
            "no-response readback needs a distinct, profile-sized value"
        }
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        fun phase(value: String) {
            instrumentation.sendStatus(2, Bundle().apply {
                putString("native_hil", "$runId:$value")
            })
        }

        val stat = File("/proc/self/stat").readText()
        val startTicks = stat.substring(stat.lastIndexOf(')') + 2).trim().split(Regex("\\s+"))[19]
        phase("started:${Process.myPid()}:$startTicks")
        val worker = HandlerThread("blehub-native-hil").also { it.start() }
        AutoCloseable {
            worker.quitSafely()
            worker.join(5_000)
            assertFalse("caller Handler did not terminate", worker.isAlive)
        }.use {
            val backend = AndroidBleBackend(instrumentation.targetContext, Handler(worker.looper))
            AutoCloseable { backend.dispose() }.use {
                val sink = DirectSink()
                val hub = BleHub(backend, sink)
                AutoCloseable { hub.destroy() }.use {
                    AutoCloseable { hub.dispose() }.use {
                        val init = CommandId(1uL)
                        hub.initialize(InitializeCommand(init))
                        val adapter = sink.await<BleEvent.AdapterSnapshotChanged> { it.commandId == init }
                        assertEquals(AdapterState.POWERED_ON, adapter.snapshot.state)
                        assertTrue(adapter.snapshot.scanAuthorization in allowedAuthorization)
                        assertTrue(adapter.snapshot.connectionAuthorization in allowedAuthorization)
                        sink.completed(init)
                        phase("initialized")

                        val scan = ScanSessionId(2uL)
                        val start = CommandId(3uL)
                        hub.startScan(
                            StartScanCommand(
                                start, scan,
                                ScanOptions(
                                    serviceUuids = listOf(BluetoothUuid(serviceUuid)),
                                    namePrefix = namePrefix,
                                    exactName = null,
                                    companyId = null,
                                    minRssi = null,
                                    manufacturerData = null,
                                    android = null,
                                    allowDuplicates = false,
                                )
                            )
                        )
                        sink.await<BleEvent.ScanStarted> { it.commandId == start && it.scanId == scan }
                        sink.completed(start)
                        val observed = sink.await<BleEvent.AdvertisementObserved> {
                            it.advertisement.scanSessionId == scan &&
                                    it.advertisement.advertisedServices.any { uuid -> uuid.value == serviceUuid }
                        }.advertisement
                        val stop = CommandId(4uL)
                        hub.stopScan(StopScanCommand(stop, scan))
                        assertTrue(sink.await<BleEvent.ScanStopped> { it.scanId == scan }.result is ScanStopResult.Ok)
                        sink.completed(stop)
                        phase("scanned")

                        val connection = ConnectionId(5uL)
                        val connect = CommandId(6uL)
                        hub.connect(ConnectCommand(connect, connection, observed.peripheralId, false))
                        sink.await<BleEvent.ConnectionStateChanged> {
                            it.commandId == connect && it.connectionId == connection &&
                                    it.result == ConnectionStateResult.Ok(ConnectionState.CONNECTED)
                        }
                        sink.completed(connect)
                        phase("connected")

                        val gatt = GattSessionId(7uL)
                        val discover = CommandId(8uL)
                        hub.discoverGatt(DiscoverGattCommand(discover, connection, gatt))
                        val database = sink.await<BleEvent.GattDiscovered> { it.commandId == discover }.database
                        assertEquals(connection, database.connectionId)
                        assertEquals(gatt, database.gattSessionId)
                        sink.completed(discover)
                        val service = database.services.single { it.uuid.value == serviceUuid }
                        val rw = service.characteristics.single { it.uuid.value == rwUuid }.id
                        val updates = service.characteristics.single { it.uuid.value == updatesUuid }.id
                        phase("discovered")

                        fun read(id: ULong, expected: ByteArray) {
                            val command = CommandId(id)
                            hub.readCharacteristic(ReadCharacteristicCommand(command, connection, gatt, rw))
                            val value = sink.await<BleEvent.CharacteristicRead> { it.commandId == command }
                            assertEquals(connection, value.connectionId)
                            assertEquals(gatt, value.gattSessionId)
                            assertEquals(rw, value.characteristicId)
                            assertArrayEquals(expected, value.value)
                            sink.completed(command)
                        }
                        read(9uL, initial)
                        val write = CommandId(10uL)
                        hub.writeCharacteristic(
                            WriteCharacteristicCommand(
                                write, connection, gatt, rw,
                                written, WriteType.WITH_RESPONSE
                            )
                        )
                        sink.await<BleEvent.CharacteristicWritten> {
                            it.commandId == write && it.connectionId == connection &&
                                    it.gattSessionId == gatt && it.characteristicId == rw
                        }
                        sink.completed(write)
                        read(11uL, written)
                        val localWrite = CommandId(16uL)
                        hub.writeCharacteristic(
                            WriteCharacteristicCommand(
                                localWrite, connection, gatt, rw,
                                withoutResponse, WriteType.WITHOUT_RESPONSE
                            )
                        )
                        sink.await<BleEvent.CharacteristicWritten> {
                            it.commandId == localWrite && it.connectionId == connection &&
                                    it.gattSessionId == gatt && it.characteristicId == rw
                        }
                        sink.completed(localWrite)
                        // Local submission is not delivery; this separate read proves the value.
                        read(17uL, withoutResponse)
                        phase("readback")

                        val subscription = SubscriptionId(12uL)
                        val enable = CommandId(13uL)
                        hub.setNotifications(
                            SetNotificationsCommand(
                                enable, connection, gatt, subscription,
                                updates, NotificationAction.Enable(NotificationModeRequest.REQUIRE_NOTIFICATION)
                            )
                        )
                        val enabled = sink.await<BleEvent.NotificationStateChanged> {
                            it.commandId == enable && it.connectionId == connection && it.gattSessionId == gatt &&
                                    it.subscriptionId == subscription && it.characteristicId == updates
                        }
                        assertEquals(NotificationState.Enabled(NotificationMode.NOTIFICATION), enabled.state)
                        sink.completed(enable)
                        phase("subscribed")
                        val value = sink.await<BleEvent.NotificationReceived> {
                            it.connectionId == connection && it.gattSessionId == gatt &&
                                    it.subscriptionId == subscription && it.characteristicId == updates
                        }
                        assertArrayEquals(notification, value.value)
                        phase("notified")

                        val disable = CommandId(14uL)
                        hub.setNotifications(
                            SetNotificationsCommand(
                                disable, connection, gatt, subscription,
                                updates, NotificationAction.Disable
                            )
                        )
                        assertEquals(
                            NotificationState.Disabled,
                            sink.await<BleEvent.NotificationStateChanged> {
                                it.commandId == disable && it.connectionId == connection && it.gattSessionId == gatt &&
                                        it.subscriptionId == subscription && it.characteristicId == updates
                            }.state
                        )
                        sink.completed(disable)
                        val disconnect = CommandId(15uL)
                        hub.disconnect(DisconnectCommand(disconnect, connection))
                        sink.await<BleEvent.ConnectionStateChanged> {
                            it.commandId == disconnect && it.connectionId == connection &&
                                    it.result == ConnectionStateResult.Ok(ConnectionState.DISCONNECTED)
                        }
                        sink.completed(disconnect)
                        phase("disconnected")
                    }
                }
            }
        }
        // This is emitted only after dispose, destroy and the caller Handler join succeed.
        phase("closed")
    }

    private class DirectSink : BleEventSink {
        private val events = ArrayBlockingQueue<BleEvent>(256)
        private val overflow = AtomicBoolean(false)
        private val pending = mutableListOf<BleEvent>()
        private val deadline = SystemClock.elapsedRealtime() + 90_000

        override fun onEvent(event: BleEvent) {
            if (!events.offer(event)) overflow.set(true)
        }

        inline fun <reified T : BleEvent> await(noinline matches: (T) -> Boolean): T {
            while (true) {
                check(!overflow.get()) { "direct sink overflow" }
                val found = pending.indexOfFirst { it is T && matches(it) }
                if (found >= 0) return pending.removeAt(found) as T
                val remaining = minOf(20_000, deadline - SystemClock.elapsedRealtime())
                check(remaining > 0) { "native HIL runtime deadline" }
                val event = events.poll(remaining, TimeUnit.MILLISECONDS)
                    ?: error("native HIL event timeout: ${T::class.java.simpleName}")
                when (event) {
                    is BleEvent.BackendError -> error("backend: ${event.error}")
                    is BleEvent.CommandCompleted -> check(event.error == null) { "command failed: $event" }
                    is BleEvent.ConnectionStateChanged -> check(event.result !is ConnectionStateResult.Err) {
                        "connection failed: $event"
                    }

                    else -> Unit
                }
                check(pending.size < 256) { "unconsumed direct sink events" }
                pending.add(event)
            }
        }

        fun completed(id: CommandId) {
            await<BleEvent.CommandCompleted> { it.commandId == id }
        }
    }

    companion object {
        private val allowedAuthorization = setOf(AuthorizationState.AUTHORIZED, AuthorizationState.NOT_REQUIRED)
        private fun required(args: Bundle, key: String): String =
            requireNotNull(args.getString(key)?.takeIf { it.isNotEmpty() }) { "missing $key" }

        private fun hex(value: String): ByteArray {
            require(value.isNotEmpty() && value.length % 2 == 0 && value.matches(Regex("[0-9a-f]+")))
            return value.chunked(2).map { it.toInt(16).toByte() }.toByteArray()
        }
    }
}
