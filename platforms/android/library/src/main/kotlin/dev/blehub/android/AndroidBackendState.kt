package dev.blehub.android

import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.le.BluetoothLeScanner
import android.bluetooth.le.ScanCallback
import dev.blehub.CommandId
import dev.blehub.ConnectCommand
import dev.blehub.DiscoverGattCommand
import dev.blehub.GattCharacteristicId
import dev.blehub.GattService
import dev.blehub.GattSessionId
import dev.blehub.NotificationMode
import dev.blehub.ScanOptions
import dev.blehub.ScanSessionId
import dev.blehub.SetNotificationsCommand
import dev.blehub.SubscriptionId
import dev.blehub.WriteCharacteristicCommand

/** Internal lifecycle state shared by the Android backend's serialized handlers. */
internal enum class Lifecycle {
    NEW,
    INITIALIZING,
    READY,
    CLOSING,
    CLOSED,
}

internal enum class ConnectionPhase {
    CONNECTING,
    CONNECTED,
    DISCONNECTING,
}

internal data class ActiveConnection(
    val generation: Long,
    val command: ConnectCommand,
    val address: String,
    var phase: ConnectionPhase = ConnectionPhase.CONNECTING,
    var gatt: BluetoothGatt? = null,
    var disconnectCommandId: CommandId? = null,
    var connectCompleted: Boolean = false,
    var pendingDiscovery: PendingDiscovery? = null,
    var gattSessionId: GattSessionId? = null,
    var characteristicsById: Map<ULong, BluetoothGattCharacteristic> = emptyMap(),
    var descriptorsById: Map<ULong, BluetoothGattDescriptor> = emptyMap(),
    var pendingIo: PendingIo? = null,
    val subscriptionsById: MutableMap<ULong, ActiveSubscription> = LinkedHashMap(),
)

internal data class ActiveSubscription(
    val subscriptionId: SubscriptionId,
    val characteristicId: GattCharacteristicId,
    val characteristic: BluetoothGattCharacteristic,
    val mode: NotificationMode,
)

internal sealed class PendingIo {
    data class Read(
        val command: dev.blehub.ReadCharacteristicCommand,
        val characteristic: BluetoothGattCharacteristic,
    ) : PendingIo()

    data class Write(
        val command: WriteCharacteristicCommand,
        val characteristic: BluetoothGattCharacteristic,
    ) : PendingIo()

    data class Notification(
        val command: SetNotificationsCommand,
        val characteristic: BluetoothGattCharacteristic,
        val descriptor: BluetoothGattDescriptor,
        val mode: NotificationMode,
        val enabling: Boolean,
    ) : PendingIo()

    data class Rssi(
        val command: dev.blehub.ReadRssiCommand,
    ) : PendingIo()
}

internal data class PendingDiscovery(
    val command: DiscoverGattCommand,
)

internal data class GattDraft(
    val services: List<GattService>,
    val characteristics: Map<ULong, BluetoothGattCharacteristic>,
    val descriptors: Map<ULong, BluetoothGattDescriptor>,
)

internal data class ActiveScan(
    val session: ScanSessionId,
    val options: ScanOptions,
    var scanner: BluetoothLeScanner? = null,
    var callback: ScanCallback? = null,
    val reportedAddresses: MutableSet<String> = LinkedHashSet(),
)

internal object BackendLease {
    private val held = java.util.concurrent.atomic.AtomicBoolean(false)

    fun acquire(): Boolean = held.compareAndSet(false, true)

    fun release() {
        check(held.compareAndSet(true, false)) { "Android backend lease released twice" }
    }
}
