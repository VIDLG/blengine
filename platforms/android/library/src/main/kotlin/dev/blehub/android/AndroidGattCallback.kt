package dev.blehub.android

import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCallback
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.os.Handler
import android.util.Log

/**
 * Posts every platform callback back to the backend's owner Handler.
 *
 * The callback object owns no BLE state; the backend keeps route correlation and
 * lifecycle decisions on its serialized handler.
 */
@Suppress("DEPRECATION")
internal class AndroidGattCallback(
    private val handler: Handler,
    private val route: ActiveConnection,
    private val onConnectionStateChange: (ActiveConnection, BluetoothGatt, Int, Int) -> Unit,
    private val onServicesDiscovered: (ActiveConnection, BluetoothGatt, Int) -> Unit,
    private val onServiceChanged: (ActiveConnection, BluetoothGatt) -> Unit,
    private val onCharacteristicRead: (
        ActiveConnection,
        BluetoothGatt,
        BluetoothGattCharacteristic,
        ByteArray,
        Int,
    ) -> Unit,
    private val onCharacteristicWrite: (
        ActiveConnection,
        BluetoothGatt,
        BluetoothGattCharacteristic,
        Int,
    ) -> Unit,
    private val onReadRemoteRssi: (ActiveConnection, BluetoothGatt, Int, Int) -> Unit,
    private val onDescriptorWrite: (
        ActiveConnection,
        BluetoothGatt,
        BluetoothGattDescriptor,
        Int,
    ) -> Unit,
    private val onCharacteristicChanged: (
        ActiveConnection,
        BluetoothGatt,
        BluetoothGattCharacteristic,
        ByteArray,
    ) -> Unit,
) : BluetoothGattCallback() {
    override fun onConnectionStateChange(gatt: BluetoothGatt, status: Int, newState: Int) {
        post("a connection") { onConnectionStateChange(route, gatt, status, newState) }
    }

    override fun onServicesDiscovered(gatt: BluetoothGatt, status: Int) {
        post("a GATT discovery") { onServicesDiscovered(route, gatt, status) }
    }

    override fun onServiceChanged(gatt: BluetoothGatt) {
        post("a GATT service-change") { onServiceChanged(route, gatt) }
    }

    @Deprecated("Android legacy callback overload")
    override fun onCharacteristicRead(
        gatt: BluetoothGatt,
        characteristic: BluetoothGattCharacteristic,
        status: Int,
    ) {
        post("a characteristic read") {
            onCharacteristicRead(
                route,
                gatt,
                characteristic,
                characteristic.value?.clone() ?: ByteArray(0),
                status,
            )
        }
    }

    override fun onCharacteristicRead(
        gatt: BluetoothGatt,
        characteristic: BluetoothGattCharacteristic,
        value: ByteArray,
        status: Int,
    ) {
        post("a characteristic read") {
            onCharacteristicRead(route, gatt, characteristic, value.clone(), status)
        }
    }

    override fun onCharacteristicWrite(
        gatt: BluetoothGatt,
        characteristic: BluetoothGattCharacteristic,
        status: Int,
    ) {
        post("a characteristic write") {
            onCharacteristicWrite(route, gatt, characteristic, status)
        }
    }

    override fun onReadRemoteRssi(gatt: BluetoothGatt, rssi: Int, status: Int) {
        post("an RSSI") { onReadRemoteRssi(route, gatt, rssi, status) }
    }

    override fun onDescriptorWrite(
        gatt: BluetoothGatt,
        descriptor: BluetoothGattDescriptor,
        status: Int,
    ) {
        post("a descriptor write") { onDescriptorWrite(route, gatt, descriptor, status) }
    }

    @Deprecated("Android legacy callback overload")
    override fun onCharacteristicChanged(
        gatt: BluetoothGatt,
        characteristic: BluetoothGattCharacteristic,
    ) {
        post("a notification") {
            onCharacteristicChanged(
                route,
                gatt,
                characteristic,
                characteristic.value?.clone() ?: ByteArray(0),
            )
        }
    }

    override fun onCharacteristicChanged(
        gatt: BluetoothGatt,
        characteristic: BluetoothGattCharacteristic,
        value: ByteArray,
    ) {
        post("a notification") {
            onCharacteristicChanged(route, gatt, characteristic, value.clone())
        }
    }

    private fun post(kind: String, action: () -> Unit) {
        if (!handler.post(action)) {
            Log.w(TAG, "Android backend handler rejected $kind callback")
        }
    }

    private companion object {
        const val TAG = "AndroidGattCallback"
    }
}
