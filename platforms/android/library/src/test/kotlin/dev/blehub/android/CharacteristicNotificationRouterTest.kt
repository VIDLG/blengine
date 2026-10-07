package dev.blehub.android

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNull

class CharacteristicNotificationRouterTest {
    private class NativeObject

    private data class Subscription(
        val id: Int,
        val characteristic: NativeObject,
    )

    @Test
    fun interleavedRoutesRemainBoundToTheirNativeGattAndCharacteristic() {
        val gattA = NativeObject()
        val gattB = NativeObject()
        val characteristicA = NativeObject()
        val characteristicB = NativeObject()
        val subscriptionA = Subscription(1, characteristicA)
        val subscriptionB = Subscription(2, characteristicB)
        val subscriptions = listOf(subscriptionA, subscriptionB)

        assertEquals(
            subscriptionA,
            resolveCharacteristicNotificationRoute(
                active = true,
                expectedGatt = gattA,
                actualGatt = gattA,
                actualCharacteristic = characteristicA,
                subscriptions = subscriptions,
                characteristicOf = { it.characteristic },
            ),
        )
        assertEquals(
            subscriptionB,
            resolveCharacteristicNotificationRoute(
                active = true,
                expectedGatt = gattB,
                actualGatt = gattB,
                actualCharacteristic = characteristicB,
                subscriptions = subscriptions,
                characteristicOf = { it.characteristic },
            ),
        )
    }

    @Test
    fun staleOrMismatchedCallbacksAreIgnored() {
        val expectedGatt = NativeObject()
        val staleGatt = NativeObject()
        val characteristic = NativeObject()
        val subscription = Subscription(1, characteristic)

        fun resolve(
            active: Boolean = true,
            actualGatt: NativeObject = expectedGatt,
            actualCharacteristic: NativeObject = characteristic,
        ) = resolveCharacteristicNotificationRoute(
            active = active,
            expectedGatt = expectedGatt,
            actualGatt = actualGatt,
            actualCharacteristic = actualCharacteristic,
            subscriptions = listOf(subscription),
            characteristicOf = { it.characteristic },
        )

        assertNull(resolve(active = false))
        assertNull(resolve(actualGatt = staleGatt))
        assertNull(resolve(actualCharacteristic = NativeObject()))
    }
}
