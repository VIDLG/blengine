package dev.blehub.android

import dev.blehub.CommandId
import dev.blehub.ConnectCommand
import dev.blehub.ConnectionId
import dev.blehub.DiscoverGattCommand
import dev.blehub.GattSessionId
import dev.blehub.PeripheralId
import dev.blehub.ReadRssiCommand
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNotNull
import kotlin.test.assertTrue

/** Synthetic route ownership only: no native Android calls and no RF claim. */
class ConnectionRfResourceCountsTest {
    @Test
    fun overlappingIndexesCountOneRouteAndRetirementDoesNotHideTheRemainingConnection() {
        val ioRoute = route(1u)
        val discoveryRoute = route(2u)
        ioRoute.pendingIo.enqueue(PendingIo.Rssi(ReadRssiCommand(CommandId(3u), ConnectionId(1u))))
        ioRoute.pendingIo.enqueue(PendingIo.Rssi(ReadRssiCommand(CommandId(4u), ConnectionId(1u))))
        val activeIo = assertNotNull(ioRoute.pendingIo.begin())
        ioRoute.activeWatchdog.install(Runnable {})
        discoveryRoute.pendingDiscovery = PendingDiscovery(
            DiscoverGattCommand(CommandId(5u), ConnectionId(2u), GattSessionId(6u)),
        )
        discoveryRoute.activeWatchdog.install(Runnable {})
        // The first route occurs in both indexes and the retained disposal snapshot.
        val owned = listOf(ioRoute, discoveryRoute, ioRoute, ioRoute)
        val active = connectionRfResourceCounts(owned)
        assertEquals(2, active.getValue("connections"))
        assertEquals(3, active.getValue("pendingIo")) // Native head + queued I/O + discovery.
        assertEquals(2, active.getValue("watchdogs"))
        assertEquals(4, active.getValue("callbackRoutes")) // Connections + two active operations.

        // The common terminal claim models local acceptance, not a Framework submission.
        assertTrue(activeIo.claimCommandTerminal())
        assertEquals(active, connectionRfResourceCounts(owned))

        assertEquals(2, ioRoute.pendingIo.retire().size)
        ioRoute.activeWatchdog.clear()
        discoveryRoute.pendingDiscovery = null
        discoveryRoute.activeWatchdog.clear()
        val retiredOperations = connectionRfResourceCounts(owned)
        assertEquals(0, retiredOperations.getValue("pendingIo"))
        assertEquals(0, retiredOperations.getValue("watchdogs"))
        assertEquals(2, retiredOperations.getValue("connections"))
        assertEquals(2, retiredOperations.getValue("callbackRoutes"))
        assertTrue(connectionRfResourceCounts(emptyList()).values.all { it == 0 })
    }

    private fun route(id: ULong) = ActiveConnection(
        generation = id.toLong(),
        command = ConnectCommand(CommandId(id), ConnectionId(id), PeripheralId(id)),
        address = "synthetic-peer-$id",
        phase = ConnectionPhase.CONNECTED,
        connectCompleted = true,
    )
}
