package dev.blehub.android

import kotlin.test.Test
import kotlin.test.assertFalse
import kotlin.test.assertNull
import kotlin.test.assertSame
import kotlin.test.assertTrue

class ActiveWatchdogSlotTest {
    @Test
    fun staleCallbackCannotRetireReplacement() {
        val slot = ActiveWatchdogSlot()
        val first = Runnable {}
        val second = Runnable {}

        assertFalse(slot.isArmed)
        assertNull(slot.install(first))
        assertTrue(slot.isArmed)
        assertSame(first, slot.install(second))
        assertFalse(slot.takeIfCurrent(first))
        assertTrue(slot.isArmed)
        assertTrue(slot.takeIfCurrent(second))
        assertFalse(slot.isArmed)
        assertFalse(slot.takeIfCurrent(second))
    }

    @Test
    fun clearReturnsOnlyTheCurrentCallback() {
        val slot = ActiveWatchdogSlot()
        val callback = Runnable {}

        slot.install(callback)
        assertSame(callback, slot.clear())
        assertFalse(slot.isArmed)
        assertNull(slot.clear())
    }
}
