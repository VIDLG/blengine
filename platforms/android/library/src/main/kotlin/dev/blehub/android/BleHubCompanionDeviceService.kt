package dev.blehub.android

import android.annotation.SuppressLint
import android.companion.AssociationInfo
import android.companion.CompanionDeviceManager
import android.companion.CompanionDeviceService
import android.companion.DevicePresenceEvent
import android.os.Build
import androidx.annotation.RequiresApi

/**
 * Platform-bound companion-device service for presence delivery.
 *
 * The system binds this service (declared in this library's manifest) only when the app has asked
 * to observe an associated device's presence, and it may bind it while the app is otherwise not
 * running. The service does one thing: translate one platform presence callback into one address +
 * label + present fact for [CompanionPresenceBroker]. It declares no permission of its own; the
 * host app declares `REQUEST_OBSERVE_COMPANION_DEVICE_PRESENCE` when it opts in with
 * `notifyOnDeviceNearby`.
 *
 * API 31-35 deliver `onDeviceAppeared`/`onDeviceDisappeared`. API 36 (flag-gated) may deliver
 * `onDevicePresenceEvent`, which carries only an association id; that path resolves the address from
 * this app's own associations before publishing.
 */
@RequiresApi(Build.VERSION_CODES.S)
public class BleHubCompanionDeviceService : CompanionDeviceService() {
    @Suppress("OVERRIDE_DEPRECATION")
    override fun onDeviceAppeared(associationInfo: AssociationInfo) {
        publish(associationInfo, present = true)
    }

    @Suppress("OVERRIDE_DEPRECATION")
    override fun onDeviceDisappeared(associationInfo: AssociationInfo) {
        publish(associationInfo, present = false)
    }

    @RequiresApi(Build.VERSION_CODES.BAKLAVA)
    @SuppressLint("UnflaggedApi")
    override fun onDevicePresenceEvent(event: DevicePresenceEvent) {
        val present = when (event.event) {
            DevicePresenceEvent.EVENT_BLE_APPEARED,
            DevicePresenceEvent.EVENT_BT_CONNECTED -> true

            DevicePresenceEvent.EVENT_BLE_DISAPPEARED,
            DevicePresenceEvent.EVENT_BT_DISCONNECTED -> false

            else -> return
        }
        val resolved = resolveAssociation(event.associationId) ?: return
        CompanionPresenceBroker.publish(resolved.first, resolved.second, present)
    }

    private fun publish(associationInfo: AssociationInfo, present: Boolean) {
        val address = associationInfo.normalizedAddress() ?: return
        CompanionPresenceBroker.publish(address, associationInfo.displayName?.toString(), present)
    }

    @RequiresApi(Build.VERSION_CODES.BAKLAVA)
    @SuppressLint("MissingPermission", "UnflaggedApi")
    private fun resolveAssociation(associationId: Int): Pair<String, String?>? {
        if (associationId == DevicePresenceEvent.NO_ASSOCIATION) return null
        val manager = getSystemService(CompanionDeviceManager::class.java) ?: return null
        val association = manager.myAssociations.firstOrNull { it.id == associationId } ?: return null
        val address = association.normalizedAddress() ?: return null
        return address to association.displayName?.toString()
    }

    private fun AssociationInfo.normalizedAddress(): String? =
        deviceMacAddress?.toString()?.trim()?.uppercase()?.takeIf { it.isNotEmpty() }
}
