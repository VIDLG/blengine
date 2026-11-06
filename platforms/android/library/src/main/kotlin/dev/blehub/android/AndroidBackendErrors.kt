package dev.blehub.android

import dev.blehub.BleErrorCategory
import dev.blehub.BleEventError
import dev.blehub.Platform
import dev.blehub.PlatformErrorDetails

internal fun platformDetails(code: String?, name: String?, message: String?) =
    PlatformErrorDetails(Platform.ANDROID, code, name, message)

internal fun adapterUnavailable(message: String) = BleEventError(
    category = BleErrorCategory.ADAPTER_UNAVAILABLE,
    message = message,
    platformDetails = platformDetails(null, "BluetoothAdapter", null),
)

internal fun connectionError(message: String, status: Int? = null) = BleEventError(
    category = BleErrorCategory.CONNECTION,
    message = message,
    platformDetails = platformDetails(status?.toString(), "BluetoothGatt", null),
)

internal fun operationTimeoutError(message: String) = BleEventError(
    category = BleErrorCategory.TIMEOUT,
    message = message,
    platformDetails = platformDetails(null, "AndroidBleBackend", null),
)

internal fun operationAbortedError(message: String) = BleEventError(
    category = BleErrorCategory.OPERATION_ABORTED,
    message = message,
    platformDetails = platformDetails(null, "AndroidBleBackend", null),
)


internal fun gattError(message: String, status: Int? = null) = BleEventError(
    category = BleErrorCategory.GATT,
    message = message,
    platformDetails = platformDetails(status?.toString(), "BluetoothGatt", null),
)

internal fun permissionError(message: String?) = BleEventError(
    category = BleErrorCategory.PERMISSION,
    message = message ?: "Android Bluetooth permission was denied",
    platformDetails = platformDetails(null, "SecurityException", message),
)

internal fun notSupportedError(message: String) = BleEventError(
    category = BleErrorCategory.NOT_SUPPORTED,
    message = message,
    platformDetails = platformDetails(null, "AndroidBleBackend", null),
)
