package dev.blehub.android

import dev.blehub.BleErrorCategory
import dev.blehub.BleEventError
import dev.blehub.Platform
import dev.blehub.PlatformErrorDetails

internal fun platformDetails(
    code: String?,
    name: String?,
    message: String?,
    status: Int? = null,
    attCode: Int? = null,
) = PlatformErrorDetails(Platform.ANDROID, code, name, message, status, attCode)

internal fun adapterUnavailable(message: String) = BleEventError(
    category = BleErrorCategory.ADAPTER_UNAVAILABLE,
    message = message,
    platformDetails = platformDetails(null, "BluetoothAdapter", null),
)

internal fun connectionError(message: String, status: Int? = null) = BleEventError(
    category = BleErrorCategory.CONNECTION,
    message = message,
    platformDetails = platformDetails(status?.toString(), "BluetoothGatt", null, status),
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
    platformDetails = platformDetails(status?.toString(), "BluetoothGatt", null, status),
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

/**
 * Maps one companion-association outcome.
 *
 * A chooser the user declined or dismissed is a normal cancellation, not a backend fault; the
 * platform reports those only as reason strings, so the known ones are mapped and everything else
 * keeps its text as a backend failure instead of being guessed into success.
 */
internal fun companionAssociationError(detail: String?) = BleEventError(
    category = if (
        detail != null &&
        (detail.contains("user_rejected") || detail.contains("canceled"))
    ) {
        BleErrorCategory.CANCELLED
    } else {
        BleErrorCategory.BACKEND
    },
    message = detail ?: "Android did not report a companion association",
    platformDetails = platformDetails(detail, "CompanionDeviceManager", detail),
)

/**
 * Reports a presence subscription failure that happened *after* an association was created.
 *
 * The association is a fact and its command stays successful; this is the separate fact that the
 * presence lane will stay silent, so it travels as an uncorrelated backend error.
 */
internal fun companionPresenceError(detail: String?) = BleEventError(
    category = BleErrorCategory.BACKEND,
    message = detail ?: "Android could not start companion presence observation",
    platformDetails = platformDetails(detail, "CompanionDeviceManager", detail),
)
