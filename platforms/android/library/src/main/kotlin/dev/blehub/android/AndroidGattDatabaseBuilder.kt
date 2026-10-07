package dev.blehub.android

import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattService
import dev.blehub.BluetoothUuid
import dev.blehub.CharacteristicProperties
import dev.blehub.GattCharacteristic
import dev.blehub.GattDescriptor
import dev.blehub.GattService
import dev.blehub.GattCharacteristicId
import dev.blehub.GattDescriptorId
import dev.blehub.GattServiceId

/** Builds the public database and native-object maps from one Android GATT snapshot. */
internal fun buildAndroidGattDraft(
    gatt: BluetoothGatt,
    allocateObjectId: () -> ULong,
): GattDraft {
    val services = mutableListOf<GattService>()
    val characteristics = mutableMapOf<ULong, BluetoothGattCharacteristic>()
    val descriptors = mutableMapOf<ULong, android.bluetooth.BluetoothGattDescriptor>()
    gatt.services.forEach { service ->
        val serviceId = GattServiceId(allocateObjectId())
        val gattCharacteristics = service.characteristics.map { characteristic ->
            val characteristicId = GattCharacteristicId(allocateObjectId())
            characteristics[characteristicId.value] = characteristic
            val gattDescriptors = characteristic.descriptors.map { descriptor ->
                val descriptorId = GattDescriptorId(allocateObjectId())
                descriptors[descriptorId.value] = descriptor
                GattDescriptor(
                    id = descriptorId,
                    uuid = BluetoothUuid(descriptor.uuid.toString()),
                )
            }
            GattCharacteristic(
                id = characteristicId,
                uuid = BluetoothUuid(characteristic.uuid.toString()),
                properties = CharacteristicProperties(
                    readable = characteristic.properties and BluetoothGattCharacteristic.PROPERTY_READ != 0,
                    writableWithResponse = characteristic.properties and BluetoothGattCharacteristic.PROPERTY_WRITE != 0,
                    writableWithoutResponse = characteristic.properties and BluetoothGattCharacteristic.PROPERTY_WRITE_NO_RESPONSE != 0,
                    notifiable = characteristic.properties and BluetoothGattCharacteristic.PROPERTY_NOTIFY != 0,
                    indicatable = characteristic.properties and BluetoothGattCharacteristic.PROPERTY_INDICATE != 0,
                ),
                descriptors = gattDescriptors,
            )
        }
        services += GattService(
            id = serviceId,
            uuid = BluetoothUuid(service.uuid.toString()),
            primary = when (service.type) {
                BluetoothGattService.SERVICE_TYPE_PRIMARY -> true
                BluetoothGattService.SERVICE_TYPE_SECONDARY -> false
                else -> null
            },
            characteristics = gattCharacteristics,
        )
    }
    return GattDraft(services, characteristics, descriptors)
}
