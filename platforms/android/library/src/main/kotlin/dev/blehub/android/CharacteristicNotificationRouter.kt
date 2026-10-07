package dev.blehub.android

/**
 * Resolves a native characteristic callback to the subscription that owns it.
 *
 * Android may deliver callbacks after a route has been retired or through a
 * different BluetoothGatt instance. Callers must provide the current route
 * state and use identity, rather than UUID equality, for both native objects.
 */
internal fun <T> resolveCharacteristicNotificationRoute(
    active: Boolean,
    expectedGatt: Any?,
    actualGatt: Any,
    actualCharacteristic: Any,
    subscriptions: Iterable<T>,
    characteristicOf: (T) -> Any,
): T? {
    if (!active || expectedGatt !== actualGatt) return null
    return subscriptions.firstOrNull { characteristicOf(it) === actualCharacteristic }
}
