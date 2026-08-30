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

    private val updateProfile = profile.services
        .flatMap(ServiceProfile::characteristics)
        .single { it.role == "updates" }

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
            synchronized(lock) {
                if (generation != lifecycleGeneration) return
                if (status == BluetoothGatt.GATT_SUCCESS && newState == BluetoothProfile.STATE_CONNECTED) {
                    connectedDevices[key] = device
                } else {
                    connectedDevices.remove(key)
                    subscriptions.remove(key)
                    inFlightUpdates.remove(key)
                }
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
            if (status == BluetoothGatt.GATT_SUCCESS) {
                synchronized(lock) {
                    if (generation != lifecycleGeneration) return
                    values[characteristic.uuid] = value.copyOf()
                }
            } else if (!isCurrent(generation)) {
                return
            }
            if (responseNeeded) {
                serverFor(generation)?.sendResponse(device, requestId, status, offset, null)
            }
            publish(
                "write device=${device.address} characteristic=${characteristic.uuid} " +
                    "status=$status value=${value.toHex()}",
            )
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
            if (status == BluetoothGatt.GATT_SUCCESS) {
                synchronized(lock) {
                    if (generation != lifecycleGeneration) return
                    if (mode == SubscriptionMode.DISABLED) {
                        subscriptions.remove(device.address)
                    } else {
                        subscriptions[device.address] = checkNotNull(mode)
                    }
                }
            }
            if (responseNeeded) {
                serverFor(generation)?.sendResponse(device, requestId, status, offset, null)
            }
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
            val accepted = synchronized(lock) {
                generation == lifecycleGeneration && inFlightUpdates.remove(device.address)
            }
            if (accepted) {
                publish("notification completion device=${device.address} status=$status")
            }
        }

        override fun onMtuChanged(device: BluetoothDevice, mtu: Int) {
            if (!isCurrent(generation)) return
            publish("MTU changed device=${device.address} mtu=$mtu")
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
        val resources = synchronized(lock) {
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
            captured
        }
        resources.first?.let { selectedAdvertiser ->
            resources.second?.let(selectedAdvertiser::stopAdvertising)
        }
        resources.third?.clearServices()
        resources.third?.close()
        publish("fixture stopped")
    }

    fun disconnectAll() {
        val server = synchronized(lock) { gattServer }
        val devices = synchronized(lock) { connectedDevices.values.toList() }
        devices.forEach { server?.cancelConnection(it) }
        publish("disconnect requested for ${devices.size} device(s)")
    }

    fun sendNotification(value: ByteArray) = sendUpdate(value, SubscriptionMode.NOTIFICATION)

    fun sendIndication(value: ByteArray) = sendUpdate(value, SubscriptionMode.INDICATION)

    fun reportStatus() = publish("status requested")

    fun requireAdvertising() {
        check(synchronized(lock) { phase == PeripheralPhase.ADVERTISING }) {
            "fixture is not advertising: $phase"
        }
        publish("advertising readiness confirmed")
    }

    override fun close() = stop()

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

    private fun findCharacteristic(uuid: UUID): CharacteristicProfile? =
        profile.services.asSequence()
            .flatMap { it.characteristics.asSequence() }
            .firstOrNull { it.uuid == uuid }

    private fun fail(generation: Long, message: String) {
        val resources = synchronized(lock) {
            if (generation != lifecycleGeneration) return
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
            captured
        }
        resources.first?.let { selectedAdvertiser ->
            resources.second?.let(selectedAdvertiser::stopAdvertising)
        }
        resources.third?.clearServices()
        resources.third?.close()
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
        private val CCCD_UUID = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")
        private val CCCD_DISABLED = byteArrayOf(0x00, 0x00)
        private val CCCD_NOTIFICATION = byteArrayOf(0x01, 0x00)
        private val CCCD_INDICATION = byteArrayOf(0x02, 0x00)
    }
}
