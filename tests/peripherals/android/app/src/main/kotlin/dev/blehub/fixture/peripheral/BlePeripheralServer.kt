package dev.blehub.fixture.peripheral

import android.annotation.SuppressLint
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.BluetoothGattServer
import android.bluetooth.BluetoothGattServerCallback
import android.bluetooth.BluetoothGattService
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothProfile
import android.bluetooth.BluetoothStatusCodes
import android.bluetooth.le.AdvertiseCallback
import android.bluetooth.le.AdvertiseData
import android.bluetooth.le.AdvertiseSettings
import android.bluetooth.le.BluetoothLeAdvertiser
import android.content.Context
import android.os.Build
import android.os.ParcelUuid
import android.os.SystemClock
import android.util.Log
import java.io.Closeable
import java.util.UUID

internal enum class PeripheralPhase {
    STOPPED,
    STARTING,
    ADVERTISING,
    ERROR,
}

internal enum class SubscriptionMode {
    DISABLED,
    NOTIFICATION,
    INDICATION,
}

internal data class PeripheralSnapshot(
    val phase: PeripheralPhase,
    val connectedDevices: List<String>,
    val subscriptions: Map<String, SubscriptionMode>,
    val updateValue: ByteArray,
    val lastEvent: String,
)

internal data class NotificationBurstCompletion(
    val mode: SubscriptionMode,
    val count: Int,
    val payloadBytes: Int,
    val localElapsedNanos: Long,
    val error: String?,
)

internal data class WriteBatchSnapshot(
    val batchId: ByteArray,
    val expectedCount: Int,
    val receivedCount: Int,
    val uniqueCount: Int,
    val missingCount: Int,
    val duplicateCount: Int,
    val outOfOrderCount: Int,
    val outOfRangeCount: Int,
    val foreignBatchCount: Int,
    val malformedCount: Int,
    val callbackSpanNanos: Long,
)

@SuppressLint("MissingPermission")
@Suppress("DEPRECATION")
internal class BlePeripheralServer(
    context: Context,
    private val profile: FixtureProfile,
    private val onSnapshot: (PeripheralSnapshot) -> Unit,
) : Closeable {
    private val appContext = context.applicationContext
    private val manager = appContext.getSystemService(BluetoothManager::class.java)
    private val adapter: BluetoothAdapter = manager.adapter
    private val lock = Any()
    private val values = mutableMapOf<UUID, ByteArray>()
    private val characteristics = mutableMapOf<UUID, BluetoothGattCharacteristic>()
    private val connectedDevices = mutableMapOf<String, BluetoothDevice>()
    private val subscriptions = mutableMapOf<String, SubscriptionMode>()
    private val inFlightUpdates = mutableSetOf<String>()
    private val pendingServices = mutableSetOf<UUID>()

    private var phase = PeripheralPhase.STOPPED
    private var lastEvent = "fixture created"
    private var lifecycleGeneration = 0L
    private var gattServer: BluetoothGattServer? = null
    private var advertiser: BluetoothLeAdvertiser? = null
    private var advertiseCallback: AdvertiseCallback? = null
    private var notificationBurst: ActiveNotificationBurst? = null
    private var writeBatch: ActiveWriteBatch? = null

    private val updateProfile = profile.services
        .flatMap(ServiceProfile::characteristics)
        .single { it.role == "updates" }

    private class ActiveNotificationBurst(
        val generation: Long,
        val device: BluetoothDevice,
        val mode: SubscriptionMode,
        val burstId: ByteArray,
        val count: Int,
        val payloadBytes: Int,
        val startedAtNanos: Long,
        val onComplete: (NotificationBurstCompletion) -> Unit,
        var nextSequence: Int = 0,
        var completedCount: Int = 0,
    )

    private data class NotificationBurstDispatch(
        val generation: Long,
        val device: BluetoothDevice,
        val mode: SubscriptionMode,
        val server: BluetoothGattServer,
        val characteristic: BluetoothGattCharacteristic,
        val sequence: Int,
        val payload: ByteArray,
    )

    private class ActiveWriteBatch(
        val batchId: ByteArray,
        val expectedCount: Int,
        val seenSequences: MutableSet<Long> = mutableSetOf(),
        var nextSequence: Long = 0,
        var receivedCount: Int = 0,
        var duplicateCount: Int = 0,
        var outOfOrderCount: Int = 0,
        var outOfRangeCount: Int = 0,
        var foreignBatchCount: Int = 0,
        var malformedCount: Int = 0,
        var firstCallbackNanos: Long? = null,
        var lastCallbackNanos: Long? = null,
    )

    private inner class GattCallback(
        private val generation: Long,
    ) : BluetoothGattServerCallback() {
        override fun onServiceAdded(status: Int, service: BluetoothGattService) {
            if (!isCurrent(generation)) return
            if (status != BluetoothGatt.GATT_SUCCESS) {
                fail(generation, "service ${service.uuid} add failed: status=$status")
                return
            }
            val ready = synchronized(lock) {
                if (generation != lifecycleGeneration) return
                pendingServices.remove(service.uuid)
                pendingServices.isEmpty() && phase == PeripheralPhase.STARTING
            }
            publish("service ${service.uuid} added")
            if (ready) startAdvertising(generation)
        }

        override fun onConnectionStateChange(device: BluetoothDevice, status: Int, newState: Int) {
            val key = device.address
            val burstCompletion = synchronized(lock) {
                if (generation != lifecycleGeneration) return
                if (status == BluetoothGatt.GATT_SUCCESS && newState == BluetoothProfile.STATE_CONNECTED) {
                    connectedDevices[key] = device
                    null
                } else {
                    connectedDevices.remove(key)
                    subscriptions.remove(key)
                    inFlightUpdates.remove(key)
                    if (notificationBurst?.device?.address == key) {
                        finishNotificationBurstLocked(
                            "${notificationBurst?.mode?.name?.lowercase()} burst device disconnected: " +
                                "status=$status state=$newState",
                        )
                    } else {
                        null
                    }
                }
            }
            burstCompletion?.invoke()
            if (status == BluetoothGatt.GATT_SUCCESS && newState == BluetoothProfile.STATE_CONNECTED) {
                serverFor(generation)?.setPreferredPhy(
                    device,
                    BluetoothDevice.PHY_LE_2M_MASK,
                    BluetoothDevice.PHY_LE_2M_MASK,
                    BluetoothDevice.PHY_OPTION_NO_PREFERRED,
                )
                serverFor(generation)?.readPhy(device)
            }
            publish("connection device=$key status=$status state=$newState")
        }

        override fun onCharacteristicReadRequest(
            device: BluetoothDevice,
            requestId: Int,
            offset: Int,
            characteristic: BluetoothGattCharacteristic,
        ) {
            val characteristicProfile = findCharacteristic(characteristic.uuid)
            val value = synchronized(lock) {
                if (generation != lifecycleGeneration) return
                values[characteristic.uuid]?.copyOf()
            }
            val (status, response) = when {
                characteristicProfile == null || value == null -> BluetoothGatt.GATT_FAILURE to null
                CharacteristicProperty.READ !in characteristicProfile.properties ->
                    BluetoothGatt.GATT_READ_NOT_PERMITTED to null
                offset !in 0..value.size -> BluetoothGatt.GATT_INVALID_OFFSET to null
                else -> BluetoothGatt.GATT_SUCCESS to value.copyOfRange(offset, value.size)
            }
            serverFor(generation)?.sendResponse(device, requestId, status, offset, response)
            publish("read device=${device.address} characteristic=${characteristic.uuid} status=$status")
        }

        override fun onCharacteristicWriteRequest(
            device: BluetoothDevice,
            requestId: Int,
            characteristic: BluetoothGattCharacteristic,
            preparedWrite: Boolean,
            responseNeeded: Boolean,
            offset: Int,
            value: ByteArray,
        ) {
            if (!isCurrent(generation)) return
            val characteristicProfile = findCharacteristic(characteristic.uuid)
            val writable = characteristicProfile?.properties?.any {
                it == CharacteristicProperty.WRITE ||
                    it == CharacteristicProperty.WRITE_WITHOUT_RESPONSE
            } == true
            val status = when {
                preparedWrite -> BluetoothGatt.GATT_REQUEST_NOT_SUPPORTED
                !writable -> BluetoothGatt.GATT_WRITE_NOT_PERMITTED
                offset != 0 -> BluetoothGatt.GATT_INVALID_OFFSET
                else -> BluetoothGatt.GATT_SUCCESS
            }
            val benchmarkWrite = if (status == BluetoothGatt.GATT_SUCCESS) {
                synchronized(lock) {
                    if (generation != lifecycleGeneration) return
                    values[characteristic.uuid] = value.copyOf()
                    recordWriteBatchLocked(value)
                }
            } else {
                if (!isCurrent(generation)) return
                false
            }
            if (responseNeeded) {
                serverFor(generation)?.sendResponse(device, requestId, status, offset, null)
            }
            if (!benchmarkWrite) {
                publish(
                    "write device=${device.address} characteristic=${characteristic.uuid} " +
                        "status=$status value=${value.toHex()}",
                )
            }
        }

        override fun onDescriptorReadRequest(
            device: BluetoothDevice,
            requestId: Int,
            offset: Int,
            descriptor: BluetoothGattDescriptor,
        ) {
            val mode = synchronized(lock) {
                if (generation != lifecycleGeneration) return
                subscriptions[device.address] ?: SubscriptionMode.DISABLED
            }
            val value = if (descriptor.uuid == CCCD_UUID) mode.cccdValue() else null
            val status = when {
                value == null -> BluetoothGatt.GATT_READ_NOT_PERMITTED
                offset !in 0..value.size -> BluetoothGatt.GATT_INVALID_OFFSET
                else -> BluetoothGatt.GATT_SUCCESS
            }
            val response = if (status == BluetoothGatt.GATT_SUCCESS) {
                value!!.copyOfRange(offset, value.size)
            } else {
                null
            }
            serverFor(generation)?.sendResponse(device, requestId, status, offset, response)
            publish("descriptor read device=${device.address} status=$status mode=$mode")
        }

        override fun onDescriptorWriteRequest(
            device: BluetoothDevice,
            requestId: Int,
            descriptor: BluetoothGattDescriptor,
            preparedWrite: Boolean,
            responseNeeded: Boolean,
            offset: Int,
            value: ByteArray,
        ) {
            if (!isCurrent(generation)) return
            val mode = value.subscriptionModeOrNull()
            val status = when {
                descriptor.uuid != CCCD_UUID -> BluetoothGatt.GATT_WRITE_NOT_PERMITTED
                preparedWrite -> BluetoothGatt.GATT_REQUEST_NOT_SUPPORTED
                offset != 0 -> BluetoothGatt.GATT_INVALID_OFFSET
                mode == null -> BluetoothGatt.GATT_FAILURE
                else -> BluetoothGatt.GATT_SUCCESS
            }
            val burstCompletion = if (status == BluetoothGatt.GATT_SUCCESS) {
                synchronized(lock) {
                    if (generation != lifecycleGeneration) return
                    if (mode == SubscriptionMode.DISABLED) {
                        subscriptions.remove(device.address)
                    } else {
                        subscriptions[device.address] = checkNotNull(mode)
                    }
                    if (!notificationSubscriberMatchesBurstLocked()) {
                        finishNotificationBurstLocked(
                            "subscription mode or subscribers changed during burst",
                        )
                    } else {
                        null
                    }
                }
            } else {
                null
            }
            if (responseNeeded) {
                serverFor(generation)?.sendResponse(device, requestId, status, offset, null)
            }
            burstCompletion?.invoke()
            publish(
                "CCCD write device=${device.address} status=$status " +
                    "mode=${mode ?: "invalid"} value=${value.toHex()}",
            )
        }

        override fun onExecuteWrite(device: BluetoothDevice, requestId: Int, execute: Boolean) {
            if (!isCurrent(generation)) return
            serverFor(generation)?.sendResponse(
                device,
                requestId,
                BluetoothGatt.GATT_REQUEST_NOT_SUPPORTED,
                0,
                null,
            )
        }

        override fun onNotificationSent(device: BluetoothDevice, status: Int) {
            var burstCompletion: (() -> Unit)? = null
            var dispatchNext = false
            val ordinaryUpdate = synchronized(lock) {
                if (generation != lifecycleGeneration || !inFlightUpdates.remove(device.address)) {
                    return
                }
                val burst = notificationBurst
                if (burst == null || burst.device.address != device.address) {
                    true
                } else {
                    if (status != BluetoothGatt.GATT_SUCCESS) {
                        burstCompletion = finishNotificationBurstLocked(
                            "${burst.mode.name.lowercase()} burst callback failed at sequence=" +
                                "${burst.completedCount}: status=$status",
                        )
                    } else {
                        burst.completedCount += 1
                        if (burst.completedCount == burst.count) {
                            burstCompletion = finishNotificationBurstLocked(null)
                        } else {
                            dispatchNext = true
                        }
                    }
                    false
                }
            }
            burstCompletion?.invoke()
            if (dispatchNext) dispatchNextNotificationBurst()
            if (ordinaryUpdate) {
                publish("notification completion device=${device.address} status=$status")
            }
        }

        override fun onMtuChanged(device: BluetoothDevice, mtu: Int) {
            if (!isCurrent(generation)) return
            publish("MTU changed device=${device.address} mtu=$mtu")
        }

        override fun onPhyUpdate(device: BluetoothDevice, txPhy: Int, rxPhy: Int, status: Int) {
            if (!isCurrent(generation)) return
            publish(
                "PHY updated device=${device.address} tx=${txPhy.phyName()} " +
                    "rx=${rxPhy.phyName()} status=$status",
            )
        }

        override fun onPhyRead(device: BluetoothDevice, txPhy: Int, rxPhy: Int, status: Int) {
            if (!isCurrent(generation)) return
            publish(
                "PHY read device=${device.address} tx=${txPhy.phyName()} " +
                    "rx=${rxPhy.phyName()} status=$status",
            )
        }
    }

    private inner class LifecycleAdvertiseCallback(
        private val generation: Long,
    ) : AdvertiseCallback() {
        override fun onStartSuccess(settingsInEffect: AdvertiseSettings) {
            val accepted = synchronized(lock) {
                if (generation != lifecycleGeneration || phase != PeripheralPhase.STARTING) {
                    false
                } else {
                    phase = PeripheralPhase.ADVERTISING
                    true
                }
            }
            if (accepted) publish("advertising started profile=${profile.profileId}")
        }

        override fun onStartFailure(errorCode: Int) {
            fail(generation, "advertising failed: error=$errorCode")
        }
    }

    fun start() {
        synchronized(lock) {
            if (phase == PeripheralPhase.STARTING || phase == PeripheralPhase.ADVERTISING) {
                publish("start ignored: already $phase")
                return
            }
        }
        check(adapter.isEnabled) { "Bluetooth is disabled" }
        check(adapter.isMultipleAdvertisementSupported) {
            "device does not support BLE peripheral advertising"
        }
        val selectedAdvertiser = checkNotNull(adapter.bluetoothLeAdvertiser) {
            "Bluetooth LE advertiser is unavailable"
        }
        val generation = synchronized(lock) {
            lifecycleGeneration += 1
            lifecycleGeneration
        }
        val selectedAdvertiseCallback = LifecycleAdvertiseCallback(generation)
        val selectedServer = checkNotNull(
            manager.openGattServer(appContext, GattCallback(generation)),
        ) { "Bluetooth GATT server could not be opened" }

        synchronized(lock) {
            phase = PeripheralPhase.STARTING
            advertiser = selectedAdvertiser
            advertiseCallback = selectedAdvertiseCallback
            gattServer = selectedServer
            values.clear()
            characteristics.clear()
            connectedDevices.clear()
            subscriptions.clear()
            inFlightUpdates.clear()
            pendingServices.clear()
            writeBatch = null
        }

        try {
            val services = profile.services.map(::buildService)
            synchronized(lock) { pendingServices += services.map(BluetoothGattService::getUuid) }
            services.forEach { service ->
                check(selectedServer.addService(service)) {
                    "service ${service.uuid} was rejected before registration"
                }
            }
            publish("GATT server starting profile=${profile.profileId}")
        } catch (error: Exception) {
            fail(generation, "GATT server start failed: ${error.message}")
            throw error
        }
    }

    fun stop() {
        val (resources, burstCompletion) = synchronized(lock) {
            val completion = finishNotificationBurstLocked("subscription burst stopped")
            lifecycleGeneration += 1
            val captured = Triple(advertiser, advertiseCallback, gattServer)
            advertiser = null
            advertiseCallback = null
            gattServer = null
            phase = PeripheralPhase.STOPPED
            pendingServices.clear()
            connectedDevices.clear()
            subscriptions.clear()
            inFlightUpdates.clear()
            writeBatch = null
            captured to completion
        }
        resources.first?.let { selectedAdvertiser ->
            resources.second?.let(selectedAdvertiser::stopAdvertising)
        }
        resources.third?.clearServices()
        resources.third?.close()
        burstCompletion?.invoke()
        publish("fixture stopped")
    }

    fun disconnectAll() {
        val (server, devices, burstCompletion) = synchronized(lock) {
            Triple(
                gattServer,
                connectedDevices.values.toList(),
                finishNotificationBurstLocked("subscription burst disconnect requested"),
            )
        }
        burstCompletion?.invoke()
        devices.forEach { server?.cancelConnection(it) }
        publish("disconnect requested for ${devices.size} device(s)")
    }

    fun sendNotification(value: ByteArray) = sendUpdate(value, SubscriptionMode.NOTIFICATION)

    fun sendIndication(value: ByteArray) = sendUpdate(value, SubscriptionMode.INDICATION)

    fun sendNotificationBurst(
        burstId: ByteArray,
        count: Int,
        payloadBytes: Int,
        onComplete: (NotificationBurstCompletion) -> Unit,
    ) = sendBurst(
        SubscriptionMode.NOTIFICATION,
        burstId,
        count,
        payloadBytes,
        onComplete,
    )

    fun sendIndicationBurst(
        burstId: ByteArray,
        count: Int,
        payloadBytes: Int,
        onComplete: (NotificationBurstCompletion) -> Unit,
    ) = sendBurst(
        SubscriptionMode.INDICATION,
        burstId,
        count,
        payloadBytes,
        onComplete,
    )

    private fun sendBurst(
        mode: SubscriptionMode,
        burstId: ByteArray,
        count: Int,
        payloadBytes: Int,
        onComplete: (NotificationBurstCompletion) -> Unit,
    ) {
        check(mode != SubscriptionMode.DISABLED) { "a burst requires an active subscription mode" }
        val modeName = mode.name.lowercase()
        require(burstId.size == BURST_ID_BYTES) { "burst_id must be exactly 8 bytes" }
        require(count in MIN_BURST_COUNT..MAX_BURST_COUNT) {
            "$modeName burst count must be between $MIN_BURST_COUNT and $MAX_BURST_COUNT"
        }
        require(payloadBytes in BURST_HEADER_BYTES..MAX_BURST_PAYLOAD_BYTES) {
            "$modeName burst payload must be between $BURST_HEADER_BYTES and " +
                "$MAX_BURST_PAYLOAD_BYTES bytes"
        }
        synchronized(lock) {
            check(notificationBurst == null) { "a subscription burst is already active" }
            check(inFlightUpdates.isEmpty()) {
                "a notification or indication is already in flight"
            }
            checkNotNull(gattServer) { "GATT server is not running" }
            checkNotNull(characteristics[updateProfile.uuid]) {
                "updates characteristic is unavailable"
            }
            val targets = connectedDevices
                .filterKeys { subscriptions[it] == mode }
                .values
                .toList()
            check(targets.size == 1) {
                "$modeName burst requires exactly one $mode subscriber; found ${targets.size}"
            }
            notificationBurst = ActiveNotificationBurst(
                generation = lifecycleGeneration,
                device = targets.single(),
                mode = mode,
                burstId = burstId.copyOf(),
                count = count,
                payloadBytes = payloadBytes,
                startedAtNanos = SystemClock.elapsedRealtimeNanos(),
                onComplete = onComplete,
            )
        }
        dispatchNextNotificationBurst()
    }

    fun startWriteBatch(batchId: ByteArray, expectedCount: Int) {
        require(batchId.size == WRITE_BATCH_ID_BYTES) {
            "write batch ID must be exactly $WRITE_BATCH_ID_BYTES bytes"
        }
        require(expectedCount in MIN_BURST_COUNT..MAX_BURST_COUNT) {
            "write batch count must be between $MIN_BURST_COUNT and $MAX_BURST_COUNT"
        }
        synchronized(lock) {
            checkNotNull(gattServer) { "GATT server is not running" }
            writeBatch = ActiveWriteBatch(batchId.copyOf(), expectedCount)
        }
    }

    fun writeBatchSnapshot(batchId: ByteArray): WriteBatchSnapshot = synchronized(lock) {
        require(batchId.size == WRITE_BATCH_ID_BYTES) {
            "write batch ID must be exactly $WRITE_BATCH_ID_BYTES bytes"
        }
        val batch = checkNotNull(writeBatch) { "no write batch is active" }
        check(batch.batchId.contentEquals(batchId)) { "write batch ID does not match" }
        val first = batch.firstCallbackNanos
        val last = batch.lastCallbackNanos
        WriteBatchSnapshot(
            batchId = batch.batchId.copyOf(),
            expectedCount = batch.expectedCount,
            receivedCount = batch.receivedCount,
            uniqueCount = batch.seenSequences.size,
            missingCount = batch.expectedCount - batch.seenSequences.size,
            duplicateCount = batch.duplicateCount,
            outOfOrderCount = batch.outOfOrderCount,
            outOfRangeCount = batch.outOfRangeCount,
            foreignBatchCount = batch.foreignBatchCount,
            malformedCount = batch.malformedCount,
            callbackSpanNanos = if (first == null || last == null) 0 else last - first,
        )
    }

    fun reportStatus() = publish("status requested")

    fun requireAdvertising() {
        check(synchronized(lock) { phase == PeripheralPhase.ADVERTISING }) {
            "fixture is not advertising: $phase"
        }
        publish("advertising readiness confirmed")
    }

    override fun close() = stop()

    private fun recordWriteBatchLocked(value: ByteArray): Boolean {
        if (value.size < WRITE_BATCH_MAGIC.size ||
            !value.copyOfRange(0, WRITE_BATCH_MAGIC.size).contentEquals(WRITE_BATCH_MAGIC)
        ) {
            return false
        }
        val batch = writeBatch ?: return true
        if (value.size !in WRITE_BATCH_HEADER_BYTES..MAX_WRITE_BATCH_PAYLOAD_BYTES) {
            batch.malformedCount += 1
            return true
        }
        val batchId = value.copyOfRange(
            WRITE_BATCH_MAGIC.size,
            WRITE_BATCH_MAGIC.size + WRITE_BATCH_ID_BYTES,
        )
        if (!batch.batchId.contentEquals(batchId)) {
            batch.foreignBatchCount += 1
            return true
        }

        val now = SystemClock.elapsedRealtimeNanos()
        batch.firstCallbackNanos = batch.firstCallbackNanos ?: now
        batch.lastCallbackNanos = now
        batch.receivedCount += 1
        val sequence = value.readLittleEndianLong(WRITE_BATCH_MAGIC.size + WRITE_BATCH_ID_BYTES)
        if (sequence < 0 || sequence >= batch.expectedCount.toLong()) {
            batch.outOfRangeCount += 1
        } else if (!batch.seenSequences.add(sequence)) {
            batch.duplicateCount += 1
        } else {
            if (sequence != batch.nextSequence) batch.outOfOrderCount += 1
            while (batch.seenSequences.contains(batch.nextSequence)) batch.nextSequence += 1
        }
        return true
    }

    private fun Int.phyName(): String = when (this) {
        BluetoothDevice.PHY_LE_1M -> "1M"
        BluetoothDevice.PHY_LE_2M -> "2M"
        BluetoothDevice.PHY_LE_CODED -> "CODED"
        else -> "UNKNOWN($this)"
    }

    private fun ByteArray.readLittleEndianLong(offset: Int): Long {
        var result = 0L
        repeat(Long.SIZE_BYTES) { index ->
            result = result or ((this[offset + index].toLong() and 0xffL) shl (index * 8))
        }
        return result
    }

    private fun buildService(serviceProfile: ServiceProfile): BluetoothGattService {
        val type = if (serviceProfile.primary) {
            BluetoothGattService.SERVICE_TYPE_PRIMARY
        } else {
            BluetoothGattService.SERVICE_TYPE_SECONDARY
        }
        val service = BluetoothGattService(serviceProfile.uuid, type)
        serviceProfile.characteristics.forEach { characteristicProfile ->
            val characteristic = BluetoothGattCharacteristic(
                characteristicProfile.uuid,
                characteristicProfile.androidProperties(),
                characteristicProfile.androidPermissions(),
            )
            if (characteristicProfile.properties.any {
                    it == CharacteristicProperty.NOTIFY || it == CharacteristicProperty.INDICATE
                }
            ) {
                check(
                    characteristic.addDescriptor(
                        BluetoothGattDescriptor(
                            CCCD_UUID,
                            BluetoothGattDescriptor.PERMISSION_READ or
                                BluetoothGattDescriptor.PERMISSION_WRITE,
                        ),
                    ),
                ) { "could not add CCCD to ${characteristic.uuid}" }
            }
            check(service.addCharacteristic(characteristic)) {
                "could not add characteristic ${characteristic.uuid}"
            }
            synchronized(lock) {
                characteristics[characteristic.uuid] = characteristic
                values[characteristic.uuid] = characteristicProfile.initialValue?.copyOf()
                    ?: byteArrayOf()
            }
        }
        return service
    }

    private fun startAdvertising(generation: Long) {
        val settings = AdvertiseSettings.Builder()
            .setAdvertiseMode(AdvertiseSettings.ADVERTISE_MODE_LOW_LATENCY)
            .setTxPowerLevel(AdvertiseSettings.ADVERTISE_TX_POWER_MEDIUM)
            .setConnectable(true)
            .setTimeout(0)
            .build()
        val dataBuilder = AdvertiseData.Builder().setIncludeDeviceName(false)
        profile.advertising.serviceUuids.forEach { dataBuilder.addServiceUuid(ParcelUuid(it)) }
        val data = dataBuilder.build()
        synchronized(lock) {
            if (generation != lifecycleGeneration || phase != PeripheralPhase.STARTING) return
            val selectedAdvertiser = advertiser ?: return
            val selectedCallback = advertiseCallback ?: return
            selectedAdvertiser.startAdvertising(settings, data, selectedCallback)
        }
    }

    private fun sendUpdate(value: ByteArray, mode: SubscriptionMode) {
        val (server, characteristic, targets) = synchronized(lock) {
            check(notificationBurst == null) {
                "a notification or indication burst is active"
            }
            values[updateProfile.uuid] = value.copyOf()
            val selectedServer = checkNotNull(gattServer) { "GATT server is not running" }
            val selectedCharacteristic = checkNotNull(characteristics[updateProfile.uuid]) {
                "updates characteristic is unavailable"
            }
            val selectedTargets = connectedDevices
                .filterKeys { subscriptions[it] == mode }
                .values
                .toList()
            check(selectedTargets.isNotEmpty()) { "no connected device subscribed for $mode" }
            check(selectedTargets.none { it.address in inFlightUpdates }) {
                "a notification or indication is already in flight"
            }
            Triple(selectedServer, selectedCharacteristic, selectedTargets)
        }

        var accepted = 0
        targets.forEach { device ->
            synchronized(lock) {
                check(inFlightUpdates.add(device.address)) {
                    "a notification or indication is already in flight"
                }
            }
            val dispatched = try {
                if (Build.VERSION.SDK_INT >= 33) {
                    server.notifyCharacteristicChanged(
                        device,
                        characteristic,
                        mode == SubscriptionMode.INDICATION,
                        value,
                    ) == BluetoothStatusCodes.SUCCESS
                } else {
                    characteristic.value = value
                    server.notifyCharacteristicChanged(
                        device,
                        characteristic,
                        mode == SubscriptionMode.INDICATION,
                    )
                }
            } catch (error: Exception) {
                synchronized(lock) { inFlightUpdates.remove(device.address) }
                throw error
            }
            if (dispatched) {
                accepted += 1
            } else {
                synchronized(lock) { inFlightUpdates.remove(device.address) }
            }
            publish(
                "$mode dispatch device=${device.address} accepted=$dispatched " +
                    "value=${value.toHex()}",
            )
        }
        check(accepted == targets.size) {
            "$mode dispatch accepted by $accepted of ${targets.size} target(s)"
        }
    }

    private fun dispatchNextNotificationBurst() {
        var failure: String? = null
        val dispatch = synchronized(lock) {
            val burst = notificationBurst ?: return
            val server = gattServer
            val characteristic = characteristics[updateProfile.uuid]
            when {
                burst.generation != lifecycleGeneration -> {
                    failure = "subscription burst fixture generation changed"
                    null
                }
                server == null -> {
                    failure = "GATT server is not running"
                    null
                }
                characteristic == null -> {
                    failure = "updates characteristic is unavailable"
                    null
                }
                !notificationSubscriberMatchesBurstLocked() -> {
                    failure = "${burst.mode.name.lowercase()} burst requires exactly one unchanged " +
                        "${burst.mode} subscriber"
                    null
                }
                burst.device.address in inFlightUpdates -> {
                    failure = "a notification or indication is already in flight for the burst device"
                    null
                }
                burst.nextSequence >= burst.count -> {
                    failure = "subscription burst has no sequence left to dispatch"
                    null
                }
                else -> {
                    val sequence = burst.nextSequence
                    val payload = notificationBurstPayload(
                        burst.burstId,
                        sequence,
                        burst.payloadBytes,
                    )
                    burst.nextSequence += 1
                    values[updateProfile.uuid] = payload.copyOf()
                    inFlightUpdates.add(burst.device.address)
                    NotificationBurstDispatch(
                        generation = burst.generation,
                        device = burst.device,
                        mode = burst.mode,
                        server = server,
                        characteristic = characteristic,
                        sequence = sequence,
                        payload = payload,
                    )
                }
            }
        }
        if (dispatch == null) {
            failNotificationBurst(checkNotNull(failure))
            return
        }

        val rejection = try {
            val accepted = if (Build.VERSION.SDK_INT >= 33) {
                dispatch.server.notifyCharacteristicChanged(
                    dispatch.device,
                    dispatch.characteristic,
                    dispatch.mode == SubscriptionMode.INDICATION,
                    dispatch.payload,
                ) == BluetoothStatusCodes.SUCCESS
            } else {
                dispatch.characteristic.value = dispatch.payload
                dispatch.server.notifyCharacteristicChanged(
                    dispatch.device,
                    dispatch.characteristic,
                    dispatch.mode == SubscriptionMode.INDICATION,
                )
            }
            if (accepted) null else {
                "${dispatch.mode.name.lowercase()} burst dispatch rejected at " +
                    "sequence=${dispatch.sequence}"
            }
        } catch (error: Exception) {
            "${dispatch.mode.name.lowercase()} burst dispatch failed at sequence=${dispatch.sequence}: " +
                (error.message ?: error::class.java.simpleName)
        }
        if (rejection != null) {
            val completion = synchronized(lock) {
                if (notificationBurst?.generation != dispatch.generation) {
                    null
                } else {
                    inFlightUpdates.remove(dispatch.device.address)
                    finishNotificationBurstLocked(rejection)
                }
            }
            completion?.invoke()
        }
    }

    private fun notificationSubscriberMatchesBurstLocked(): Boolean {
        val burst = notificationBurst ?: return true
        val subscriberAddresses = connectedDevices.keys
            .filter { subscriptions[it] == burst.mode }
        return subscriberAddresses.size == 1 && subscriberAddresses.single() == burst.device.address
    }

    private fun failNotificationBurst(message: String) {
        val completion = synchronized(lock) { finishNotificationBurstLocked(message) }
        completion?.invoke()
    }

    private fun finishNotificationBurstLocked(error: String?): (() -> Unit)? {
        val burst = notificationBurst ?: return null
        notificationBurst = null
        inFlightUpdates.remove(burst.device.address)
        val completion = NotificationBurstCompletion(
            mode = burst.mode,
            count = burst.count,
            payloadBytes = burst.payloadBytes,
            localElapsedNanos = SystemClock.elapsedRealtimeNanos() - burst.startedAtNanos,
            error = error,
        )
        return { burst.onComplete(completion) }
    }

    private fun notificationBurstPayload(
        burstId: ByteArray,
        sequence: Int,
        payloadBytes: Int,
    ): ByteArray {
        val payload = ByteArray(payloadBytes)
        BURST_MAGIC.copyInto(payload, destinationOffset = 0)
        burstId.copyInto(payload, destinationOffset = BURST_MAGIC.size)
        val sequenceOffset = BURST_MAGIC.size + BURST_ID_BYTES
        val sequenceValue = sequence.toLong()
        repeat(Long.SIZE_BYTES) { index ->
            payload[sequenceOffset + index] = (sequenceValue ushr (index * Byte.SIZE_BITS)).toByte()
        }
        for (index in BURST_HEADER_BYTES until payload.size) {
            payload[index] = (burstId[index % burstId.size].toInt() xor (index * 17)).toByte()
        }
        return payload
    }

    private fun findCharacteristic(uuid: UUID): CharacteristicProfile? =
        profile.services.asSequence()
            .flatMap { it.characteristics.asSequence() }
            .firstOrNull { it.uuid == uuid }

    private fun fail(generation: Long, message: String) {
        val (resources, burstCompletion) = synchronized(lock) {
            if (generation != lifecycleGeneration) return
            val completion = finishNotificationBurstLocked(message)
            lifecycleGeneration += 1
            val captured = Triple(advertiser, advertiseCallback, gattServer)
            advertiser = null
            advertiseCallback = null
            gattServer = null
            phase = PeripheralPhase.ERROR
            pendingServices.clear()
            connectedDevices.clear()
            subscriptions.clear()
            inFlightUpdates.clear()
            captured to completion
        }
        resources.first?.let { selectedAdvertiser ->
            resources.second?.let(selectedAdvertiser::stopAdvertising)
        }
        resources.third?.clearServices()
        resources.third?.close()
        burstCompletion?.invoke()
        publish(message)
    }

    private fun isCurrent(generation: Long): Boolean = synchronized(lock) {
        generation == lifecycleGeneration
    }

    private fun serverFor(generation: Long): BluetoothGattServer? = synchronized(lock) {
        gattServer.takeIf { generation == lifecycleGeneration }
    }

    private fun publish(message: String) {
        val snapshot = synchronized(lock) {
            lastEvent = message
            PeripheralSnapshot(
                phase = phase,
                connectedDevices = connectedDevices.keys.sorted(),
                subscriptions = subscriptions.toSortedMap(),
                updateValue = values[updateProfile.uuid]?.copyOf() ?: byteArrayOf(),
                lastEvent = lastEvent,
            )
        }
        Log.i(LOG_TAG, message)
        onSnapshot(snapshot)
    }

    private fun CharacteristicProfile.androidProperties(): Int = properties.fold(0) { mask, value ->
        mask or when (value) {
            CharacteristicProperty.READ -> BluetoothGattCharacteristic.PROPERTY_READ
            CharacteristicProperty.WRITE -> BluetoothGattCharacteristic.PROPERTY_WRITE
            CharacteristicProperty.WRITE_WITHOUT_RESPONSE ->
                BluetoothGattCharacteristic.PROPERTY_WRITE_NO_RESPONSE
            CharacteristicProperty.NOTIFY -> BluetoothGattCharacteristic.PROPERTY_NOTIFY
            CharacteristicProperty.INDICATE -> BluetoothGattCharacteristic.PROPERTY_INDICATE
        }
    }

    private fun CharacteristicProfile.androidPermissions(): Int {
        var result = 0
        if (CharacteristicProperty.READ in properties) {
            result = result or BluetoothGattCharacteristic.PERMISSION_READ
        }
        if (properties.any {
                it == CharacteristicProperty.WRITE ||
                    it == CharacteristicProperty.WRITE_WITHOUT_RESPONSE
            }
        ) {
            result = result or BluetoothGattCharacteristic.PERMISSION_WRITE
        }
        return result
    }

    private fun SubscriptionMode.cccdValue(): ByteArray = when (this) {
        SubscriptionMode.DISABLED -> CCCD_DISABLED
        SubscriptionMode.NOTIFICATION -> CCCD_NOTIFICATION
        SubscriptionMode.INDICATION -> CCCD_INDICATION
    }.copyOf()

    private fun ByteArray.subscriptionModeOrNull(): SubscriptionMode? = when {
        contentEquals(CCCD_DISABLED) -> SubscriptionMode.DISABLED
        contentEquals(CCCD_NOTIFICATION) -> SubscriptionMode.NOTIFICATION
        contentEquals(CCCD_INDICATION) -> SubscriptionMode.INDICATION
        else -> null
    }

    companion object {
        private const val LOG_TAG = "BleHubPeripheral"
        private const val MIN_BURST_COUNT = 1
        private const val MAX_BURST_COUNT = 10_000
        private const val BURST_ID_BYTES = 8
        private const val BURST_HEADER_BYTES = 20
        private const val MAX_BURST_PAYLOAD_BYTES = 512
        private const val WRITE_BATCH_ID_BYTES = 8
        private const val WRITE_BATCH_HEADER_BYTES = 20
        private const val MAX_WRITE_BATCH_PAYLOAD_BYTES = 512
        private val BURST_MAGIC = byteArrayOf('B'.code.toByte(), 'H'.code.toByte(), 'N'.code.toByte(), '1'.code.toByte())
        private val WRITE_BATCH_MAGIC = byteArrayOf('B'.code.toByte(), 'H'.code.toByte(), 'W'.code.toByte(), '1'.code.toByte())
        private val CCCD_UUID = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")
        private val CCCD_DISABLED = byteArrayOf(0x00, 0x00)
        private val CCCD_NOTIFICATION = byteArrayOf(0x01, 0x00)
        private val CCCD_INDICATION = byteArrayOf(0x02, 0x00)
    }
}
