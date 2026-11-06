package dev.blehub.android

import android.Manifest
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothGatt

import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor

import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothProfile
import android.bluetooth.le.BluetoothLeScanner
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanResult
import android.bluetooth.le.ScanSettings
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.util.Log
import dev.blehub.AdapterSnapshot
import dev.blehub.AdapterState
import dev.blehub.AdvertisementObservation
import dev.blehub.AuthorizationState
import dev.blehub.BackendCapabilities
import dev.blehub.BackendEventIngressHandle
import dev.blehub.BleBackend
import dev.blehub.BleErrorCategory
import dev.blehub.BleEvent
import dev.blehub.BleEventError
import dev.blehub.BleHubException
import dev.blehub.BluetoothUuid
import dev.blehub.blehubContractVersion
import dev.blehub.CancelOperationRequest
import dev.blehub.CommandDispatchException
import dev.blehub.CommandId
import dev.blehub.ConnectCommand
import dev.blehub.ConnectionId
import dev.blehub.ConnectionState
import dev.blehub.ConnectionStateResult

import dev.blehub.DisconnectCommand
import dev.blehub.DiscoverGattCommand
import dev.blehub.EventIngressStatus
import dev.blehub.GattCharacteristic

import dev.blehub.GattDatabase


import dev.blehub.GattSessionId
import dev.blehub.InitializeCommand
import dev.blehub.ManufacturerData
import dev.blehub.NotificationAction
import dev.blehub.NotificationMode
import dev.blehub.NotificationModeRequest

import dev.blehub.Platform

import dev.blehub.ReadCharacteristicCommand
import dev.blehub.ReadRssiCommand
import dev.blehub.ScanOptions
import dev.blehub.ScanSessionId
import dev.blehub.ScanStopReason
import dev.blehub.ScanStopResult
import dev.blehub.ServiceData
import dev.blehub.SetNotificationsCommand
import dev.blehub.StartScanCommand
import dev.blehub.SubscriptionId
import dev.blehub.StopScanCommand
import dev.blehub.WriteCharacteristicCommand
import dev.blehub.WriteType
import java.util.UUID
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

private const val DEFAULT_ACTIVE_OPERATION_WATCHDOG_MS = 10_000L

/**
 * Android Central backend for the production BleHub contract.
 *
 * All Android adapter and scanner objects, callbacks, and mutable backend state
 * are confined to [handler]. The public UniFFI methods only validate ownership
 * and enqueue work; callback frames are converted to owned contract DTOs before
 * they cross the ingress boundary.
 *
 * The backend owns adapter initialization, scanning, connection generations, and
 * serialized connection/GATT operations. Native callbacks are copied into owned
 * contract DTOs before they cross the ingress boundary.
 */
public class AndroidBleBackend(
    context: Context,
    private val handler: Handler,
    private val activeOperationWatchdogMs: Long = DEFAULT_ACTIVE_OPERATION_WATCHDOG_MS,
) : BleBackend {
    private val appContext = context.applicationContext
    private val stateLock = Any()
    private var ownsLease = false
    private val bluetoothManager =
        appContext.getSystemService(BluetoothManager::class.java)

    private var lifecycle = Lifecycle.NEW
    private var ingress: BackendEventIngressHandle? = null
    private var pendingDisposeIngress: BackendEventIngressHandle? = null
    private var pendingDisposeConnections: List<ActiveConnection> = emptyList()
    private var bluetoothAdapter: BluetoothAdapter? = null
    private var activeScan: ActiveScan? = null
    private var ingressClosed = false
    private var nextPeripheralId = 1UL

    private var nextGattObjectId = 1UL
    private val peripheralIds = mutableMapOf<String, ULong>()
    private val peripheralAddresses = mutableMapOf<ULong, String>()
    private val connections = AndroidConnectionLifecycle(
        stateLock = stateLock,
        removeWatchdog = { handler.removeCallbacks(it) },
        closeGatt = ::closeGatt,
        publish = ::push,
    )

    init {
        require(activeOperationWatchdogMs > 0) {
            "active operation watchdog duration must be positive"
        }
        check(handler.looper != Looper.getMainLooper()) {
            "AndroidBleBackend requires a caller-owned dedicated Handler/Looper"
        }
        if (!BackendLease.acquire()) {
            throw BleHubException.ResourceBusy(
                "another Android backend owns the process Bluetooth adapter lease",
            )
        }
        ownsLease = true
    }

    /**
     * Read-only private integration diagnostics, not the canonical BleBackend ABI.
     * Call before stopping the caller-owned Handler. The Handler boundary plus stateLock
     * observes adapter-confined fields and admission/teardown reservations together.
     *
     * Counts logical owned roots, not Android heap objects or OS callback quiescence:
     * connections includes connecting/disconnecting and pending-dispose routes;
     * gattAttributes counts retained characteristic/descriptor index entries (service
     * containers belong to the GATT connection root); pendingIo includes discovery and
     * the whole FIFO. callbackRoutes counts connection correlation entries, registered
     * scan callbacks, active discovery/I/O correlations and subscription value routes.
     * adapterDelegates counts the retained adapter control endpoint; this implementation
     * registers no BroadcastReceiver. Peer cache counts identities, not both inverse maps.
     */
    @androidx.annotation.VisibleForTesting
    public fun snapshotRfResources(): Map<String, Int> {
        var snapshot: Map<String, Int>? = null
        runOnHandlerSync {
            synchronized(stateLock) {
                val connectionCounts = connectionRfResourceCounts(
                    connections.ownedRoutes + pendingDisposeConnections,
                )
                snapshot = (connectionCounts + linkedMapOf(
                    "activeScans" to if (activeScan != null) 1 else 0,
                    "callbackRoutes" to (connectionCounts.getValue("callbackRoutes") +
                            if (activeScan?.callback != null) 1 else 0),
                    "ingressRoots" to listOfNotNull(ingress, pendingDisposeIngress).size,
                    "adapterDelegates" to if (bluetoothAdapter != null) 1 else 0,
                    "peerCacheEntries" to (peripheralIds.values + peripheralAddresses.keys).toSet().size,
                    "backendLeases" to if (ownsLease) 1 else 0,
                )).also { counts -> check(counts.values.all { it >= 0 }) }
            }
        }
        return checkNotNull(snapshot)
    }

    override fun capabilities() = BackendCapabilities(
        platform = Platform.ANDROID,
        contractVersion = blehubContractVersion(),
        supportsDuplicateScanResults = true,
        supportsConnectableFlag = Build.VERSION.SDK_INT >= Build.VERSION_CODES.O,
        supportsReadRssi = true,
        supportsRequiredNotification = true,
        supportsRequiredIndication = true,
        reportsActualNotificationMode = true,
    )

    override fun bindEventIngress(ingress: BackendEventIngressHandle) {
        synchronized(stateLock) {
            check(lifecycle != Lifecycle.CLOSED && lifecycle != Lifecycle.CLOSING) {
                "Android backend is closed"
            }
            check(this.ingress == null) { "Android backend ingress is already bound" }
            ingressClosed = false
            this.ingress = ingress
        }
    }

    override fun initialize(command: InitializeCommand) {
        synchronized(stateLock) {
            when (lifecycle) {
                Lifecycle.NEW -> lifecycle = Lifecycle.INITIALIZING
                Lifecycle.INITIALIZING ->
                    throw CommandDispatchException.InvalidState(
                        "Android adapter initialization is already pending",
                    )

                Lifecycle.READY ->
                    throw CommandDispatchException.InvalidState(
                        "Android adapter is already initialized",
                    )

                Lifecycle.CLOSING, Lifecycle.CLOSED -> throw CommandDispatchException.Closed()
            }
            requireIngress()
        }
        if (!handler.post { initializeOnHandler(command) }) {
            synchronized(stateLock) {
                if (lifecycle == Lifecycle.INITIALIZING) lifecycle = Lifecycle.NEW
            }
            throw CommandDispatchException.BackendUnavailable(
                "Android backend handler rejected initialization",
                platformDetails(null, "Handler", "post returned false"),
            )
        }
    }

    override fun startScan(command: StartScanCommand) {
        val reservation = ActiveScan(command.scanSessionId, command.options)
        synchronized(stateLock) {
            requireOpenAndReady()
            if (activeScan != null) {
                throw CommandDispatchException.InvalidState("an Android scan is already active")
            }
            activeScan = reservation
        }
        if (!handler.post { startScanOnHandler(command, reservation) }) {
            synchronized(stateLock) {
                if (activeScan === reservation) activeScan = null
            }
            throw CommandDispatchException.BackendUnavailable(
                "Android backend handler rejected scan start",
                platformDetails(null, "Handler", "post returned false"),
            )
        }
    }

    override fun stopScan(command: StopScanCommand) {
        val reservation: ActiveScan
        synchronized(stateLock) {
            requireOpenAndReady()
            reservation = activeScan ?: throw CommandDispatchException.InvalidState(
                "the requested Android scan session is not active",
            )
            if (reservation.session.value != command.scanSessionId.value) {
                throw CommandDispatchException.InvalidState(
                    "the requested Android scan session is not active",
                )
            }
        }
        if (!handler.post { stopScanOnHandler(command, reservation) }) {
            throw CommandDispatchException.BackendUnavailable(
                "Android backend handler rejected scan stop",
                platformDetails(null, "Handler", "post returned false"),
            )
        }
    }

    override fun connect(command: ConnectCommand) {
        val route: ActiveConnection
        synchronized(stateLock) {
            requireOpenAndReady()
            val address = peripheralAddresses[command.peripheralId.value]
                ?: throw CommandDispatchException.InvalidRequest(
                    "the peripheral ID was not discovered by this Android backend",
                )
            route = connections.reserve(command, address)
        }
        if (!handler.post { connectOnHandler(route) }) {
            removeConnection(route)
            throw CommandDispatchException.BackendUnavailable(
                "Android backend handler rejected connection start",
                platformDetails(null, "Handler", "post returned false"),
            )
        }
    }

    override fun disconnect(command: DisconnectCommand) {
        val route: ActiveConnection
        synchronized(stateLock) {
            requireOpenAndReady()
            route = connections.find(command.connectionId.value)
                ?: throw CommandDispatchException.InvalidState(
                    "connection lifecycle is not active",
                )
            if (route.phase == ConnectionPhase.DISCONNECTING) {
                throw CommandDispatchException.InvalidState(
                    "connection lifecycle is already disconnecting",
                )
            }
            route.phase = ConnectionPhase.DISCONNECTING
            route.disconnectCommandId = command.commandId
        }
        if (!handler.post { disconnectOnHandler(route) }) {
            synchronized(stateLock) {
                if (connections.find(command.connectionId.value) === route) {
                    route.phase = if (route.connectCompleted) {
                        ConnectionPhase.CONNECTED
                    } else {
                        ConnectionPhase.CONNECTING
                    }
                    route.disconnectCommandId = null
                }
            }
            throw CommandDispatchException.BackendUnavailable(
                "Android backend handler rejected connection teardown",
                platformDetails(null, "Handler", "post returned false"),
            )
        }
    }

    override fun discoverGatt(command: DiscoverGattCommand) {
        val route: ActiveConnection
        synchronized(stateLock) {
            requireOpenAndReady()
            route = connections.find(command.connectionId.value)
                ?: throw CommandDispatchException.InvalidState(
                    "connection lifecycle is not active",
                )
            if (route.phase != ConnectionPhase.CONNECTED || route.gatt == null) {
                throw CommandDispatchException.InvalidState(
                    "GATT discovery requires a connected Android peripheral",
                )
            }
            if (route.pendingDiscovery != null) {
                throw CommandDispatchException.InvalidState(
                    "GATT discovery is already pending for this Android connection",
                )
            }
            if (!route.pendingIo.isEmpty) {
                throw CommandDispatchException.InvalidState(
                    "GATT discovery cannot replace a database while Android GATT I/O is pending",
                )
            }
            if (route.subscriptionsById.isNotEmpty()) {
                throw CommandDispatchException.InvalidState(
                    "disable Android notification subscriptions before rediscovering GATT",
                )
            }
            route.pendingDiscovery = PendingDiscovery(command)
        }
        if (!handler.post { discoverGattOnHandler(route) }) {
            synchronized(stateLock) {
                if (isActive(route)) route.pendingDiscovery = null
            }
            throw CommandDispatchException.BackendUnavailable(
                "Android backend handler rejected GATT discovery",
                platformDetails(null, "Handler", "post returned false"),
            )
        }
    }

    override fun readCharacteristic(command: ReadCharacteristicCommand) {
        val route: ActiveConnection
        val characteristic: BluetoothGattCharacteristic
        synchronized(stateLock) {
            requireOpenAndReady()
            route = requireConnectedRoute(command.connectionId.value)
            requireGattSession(route, command.gattSessionId.value)
            characteristic = route.characteristicsById[command.characteristicId.value]
                ?: throw CommandDispatchException.InvalidRequest(
                    "the characteristic ID is not present in the active Android GATT database",
                )
            enqueuePendingIoLocked(route, PendingIo.Read(command, characteristic))
        }
        if (!handler.post { driveIoQueue(route) }) {
            synchronized(stateLock) {
                if (isActive(route)) route.pendingIo.removeQueued(command.commandId)
            }
            throw CommandDispatchException.BackendUnavailable(
                "Android backend handler rejected characteristic read",
                platformDetails(null, "Handler", "post returned false"),
            )
        }
    }

    override fun writeCharacteristic(command: WriteCharacteristicCommand) {
        val route: ActiveConnection
        val characteristic: BluetoothGattCharacteristic
        synchronized(stateLock) {
            requireOpenAndReady()
            route = requireConnectedRoute(command.connectionId.value)
            requireGattSession(route, command.gattSessionId.value)
            characteristic = route.characteristicsById[command.characteristicId.value]
                ?: throw CommandDispatchException.InvalidRequest(
                    "the characteristic ID is not present in the active Android GATT database",
                )
            enqueuePendingIoLocked(route, PendingIo.Write(command, characteristic))
        }
        if (!handler.post { driveIoQueue(route) }) {
            synchronized(stateLock) {
                if (isActive(route)) route.pendingIo.removeQueued(command.commandId)
            }
            throw CommandDispatchException.BackendUnavailable(
                "Android backend handler rejected characteristic write",
                platformDetails(null, "Handler", "post returned false"),
            )
        }
    }

    override fun setNotifications(command: SetNotificationsCommand) {
        val route: ActiveConnection
        val characteristic: BluetoothGattCharacteristic
        val descriptor: BluetoothGattDescriptor
        val actualMode: NotificationMode
        val enabling: Boolean
        var strictIndicationRejected = false
        synchronized(stateLock) {
            requireOpenAndReady()
            route = requireConnectedRoute(command.connectionId.value)
            requireGattSession(route, command.gattSessionId.value)
            characteristic = route.characteristicsById[command.characteristicId.value]
                ?: throw CommandDispatchException.InvalidRequest(
                    "the characteristic ID is not present in the active Android GATT database",
                )
            if (route.pendingIo.any {
                    it is PendingIo.Notification<*, *> && it.characteristic === characteristic
                }) {
                throw CommandDispatchException.InvalidState(
                    "notification configuration for the characteristic is already queued",
                )
            }
            when (val action = command.action) {
                is NotificationAction.Enable -> {
                    if (route.subscriptionsById.containsKey(command.subscriptionId.value) ||
                        route.subscriptionsById.values.any { it.characteristic === characteristic }
                    ) {
                        throw CommandDispatchException.InvalidState(
                            "the characteristic already has an Android notification subscription",
                        )
                    }
                    if (action.mode == NotificationModeRequest.REQUIRE_INDICATION &&
                        characteristic.properties and BluetoothGattCharacteristic.PROPERTY_NOTIFY != 0
                    ) {
                        // Android can write the Indication CCCD bit on a dual-property
                        // characteristic, but that would silently choose a mode that the
                        // cross-platform contract treats as PlatformDefault/Notification.
                        actualMode = NotificationMode.INDICATION
                        strictIndicationRejected = true
                    } else {
                        actualMode = selectNotificationMode(action.mode, characteristic)
                    }
                    enabling = true
                }

                NotificationAction.Disable -> {
                    val subscription = route.subscriptionsById[command.subscriptionId.value]
                        ?: throw CommandDispatchException.InvalidRequest(
                            "the Android notification subscription is not active",
                        )
                    if (subscription.characteristic !== characteristic) {
                        throw CommandDispatchException.InvalidRequest(
                            "the Android notification subscription does not target the characteristic",
                        )
                    }
                    actualMode = subscription.mode
                    enabling = false
                }
            }
            descriptor = characteristic.getDescriptor(CLIENT_CONFIGURATION_UUID)
                ?: throw CommandDispatchException.InvalidRequest(
                    "the characteristic has no Client Characteristic Configuration descriptor",
                )
            if (!strictIndicationRejected) {
                enqueuePendingIoLocked(
                    route,
                    PendingIo.Notification(
                        command = command,
                        characteristic = characteristic,
                        descriptor = descriptor,
                        mode = actualMode,
                        enabling = enabling,
                    ),
                )
            }
        }
        if (strictIndicationRejected) {
            if (!handler.post {
                    push(
                        BleEvent.CommandCompleted(
                            command.commandId,
                            notSupportedError(
                                "strict Indication is unsupported on a dual-property Android characteristic",
                            ),
                        ),
                    )
                }
            ) {
                throw CommandDispatchException.BackendUnavailable(
                    "Android backend handler rejected strict Indication policy completion",
                    platformDetails(null, "Handler", "post returned false"),
                )
            }
            return
        }
        if (!handler.post { driveIoQueue(route) }) {
            synchronized(stateLock) {
                if (isActive(route)) route.pendingIo.removeQueued(command.commandId)
            }
            throw CommandDispatchException.BackendUnavailable(
                "Android backend handler rejected notification configuration",
                platformDetails(null, "Handler", "post returned false"),
            )
        }
    }

    override fun readRssi(command: ReadRssiCommand) {
        val route: ActiveConnection
        synchronized(stateLock) {
            requireOpenAndReady()
            route = requireConnectedRoute(command.connectionId.value)
            enqueuePendingIoLocked(route, PendingIo.Rssi(command))
        }
        if (!handler.post { driveIoQueue(route) }) {
            synchronized(stateLock) {
                if (isActive(route)) route.pendingIo.removeQueued(command.commandId)
            }
            throw CommandDispatchException.BackendUnavailable(
                "Android backend handler rejected RSSI read",
                platformDetails(null, "Handler", "post returned false"),
            )
        }
    }

    override fun cancelOperation(request: CancelOperationRequest) {
        synchronized(stateLock) {
            requireOpenAndReady()
            connections.cancelOperation(request.targetCommandId)
        }
        val event = BleEvent.CommandCompleted(
            request.targetCommandId,
            BleEventError(
                category = BleErrorCategory.CANCELLED,
                message = "Android GATT operation was cancelled before native dispatch",
                platformDetails = platformDetails(null, "AndroidBleBackend", null),
            ),
        )
        if (!handler.post { push(event) }) {
            // The operation is already retired from the native queue. Publishing
            // directly is safer than returning success without a terminal event.
            push(event)
        }
    }

    override fun dispose() {
        val connectionsToClose: List<ActiveConnection>
        synchronized(stateLock) {
            when (lifecycle) {
                Lifecycle.CLOSED -> return
                Lifecycle.NEW, Lifecycle.INITIALIZING, Lifecycle.READY -> {
                    lifecycle = Lifecycle.CLOSING
                    pendingDisposeIngress = ingress
                    ingress = null
                    ingressClosed = true
                    pendingDisposeConnections = connections.drain()
                }

                Lifecycle.CLOSING -> Unit
            }
            connectionsToClose = pendingDisposeConnections
        }

        // Keep the cleanup snapshot until the owner Handler confirms completion. If
        // posting or waiting fails, a later dispose call can retry without losing
        // native handles or the backend lease.
        runOnHandlerSync {
            synchronized(stateLock) {
                val error = operationAbortedError(
                    "Android backend disposal retired the connection operation",
                )
                connectionsToClose.forEach { route ->
                    // Disposal closes the ingress before native teardown; callers
                    // own the local AbandonedByDispose projection and no terminal
                    // event is fabricated for these retired commands.
                    retireGattStateLocked(route, error)
                }
            }
            connectionsToClose.forEach { route ->
                route.gatt?.let { gatt ->
                    runCatching { gatt.disconnect() }
                    runCatching { gatt.close() }
                }
            }
            activeScan?.let { scan ->
                val scanner = scan.scanner
                val callback = scan.callback
                if (scanner != null && callback != null) {
                    runCatching { scanner.stopScan(callback) }
                }
            }
            synchronized(stateLock) { activeScan = null }
            bluetoothAdapter = null
        }

        val ingressToDestroy: BackendEventIngressHandle?
        val releaseLease: Boolean
        synchronized(stateLock) {
            pendingDisposeConnections = emptyList()
            ingressToDestroy = pendingDisposeIngress
            pendingDisposeIngress = null
            peripheralIds.clear()
            peripheralAddresses.clear()
            lifecycle = Lifecycle.CLOSED
            releaseLease = ownsLease
            ownsLease = false
        }
        ingressToDestroy?.destroy()
        if (releaseLease) BackendLease.release()
    }

    private fun runOnHandlerSync(action: () -> Unit) {
        if (Looper.myLooper() == handler.looper) {
            action()
            return
        }
        val finished = CountDownLatch(1)
        var failure: RuntimeException? = null
        if (!handler.post {
                try {
                    action()
                } catch (error: RuntimeException) {
                    failure = error
                } finally {
                    finished.countDown()
                }
            }) {
            throw BleHubException.BackendCleanup(
                "Android backend handler rejected disposal",
                platformDetails(null, "Handler", "post returned false"),
            )
        }
        if (!finished.await(DISPOSE_TIMEOUT_MS, TimeUnit.MILLISECONDS)) {
            throw BleHubException.BackendCleanup(
                "Android backend disposal did not finish on its owner Handler",
                platformDetails(null, "Handler", "cleanup timeout"),
            )
        }
        failure?.let { throw it }
    }

    private fun connectOnHandler(route: ActiveConnection) {
        synchronized(stateLock) {
            if (!isActive(route) || lifecycle == Lifecycle.CLOSED || lifecycle == Lifecycle.CLOSING) return
        }
        push(
            BleEvent.ConnectionStateChanged(
                commandId = route.command.commandId,
                connectionId = route.command.connectionId,
                result = ConnectionStateResult.Ok(ConnectionState.CONNECTING),
            ),
        )
        val adapter = bluetoothAdapter
        if (adapter == null) {
            finishConnectionFailure(
                route,
                connectionError("Bluetooth adapter is unavailable while connecting"),
            )
            return
        }
        val device = try {
            adapter.getRemoteDevice(route.address)
        } catch (error: SecurityException) {
            finishConnectionFailure(route, permissionError(error.message))
            return
        } catch (error: IllegalArgumentException) {
            finishConnectionFailure(
                route,
                connectionError(error.message ?: "Android rejected the Bluetooth device address"),
            )
            return
        }
        val callback = AndroidGattCallback(
            handler = handler,
            route = route,
            onConnectionStateChange = ::handleConnectionStateChange,
            onServicesDiscovered = ::handleServicesDiscovered,
            onServiceChanged = ::handleServicesChanged,
            onCharacteristicRead = ::handleCharacteristicRead,
            onCharacteristicWrite = ::handleCharacteristicWrite,
            onReadRemoteRssi = ::handleReadRemoteRssi,
            onDescriptorWrite = ::handleDescriptorWrite,
            onCharacteristicChanged = ::handleCharacteristicChanged,
        )
        val gatt = try {
            device.connectGatt(appContext, false, callback, android.bluetooth.BluetoothDevice.TRANSPORT_LE)
        } catch (error: SecurityException) {
            finishConnectionFailure(route, permissionError(error.message))
            return
        } catch (error: IllegalStateException) {
            finishConnectionFailure(
                route,
                adapterUnavailable(error.message ?: "Android Bluetooth adapter is not enabled"),
            )
            return
        } catch (error: RuntimeException) {
            finishConnectionFailure(
                route,
                connectionError(error.message ?: "Android failed to start the GATT connection"),
            )
            return
        }
        if (gatt == null) {
            finishConnectionFailure(
                route,
                connectionError("Android did not create a Bluetooth GATT connection"),
            )
            return
        }
        val disconnectImmediately = synchronized(stateLock) {
            if (!isActive(route)) {
                false
            } else {
                route.gatt = gatt
                route.phase == ConnectionPhase.DISCONNECTING
            }
        }
        val retained = synchronized(stateLock) { isActive(route) }
        if (!retained) {
            runCatching { gatt.close() }
            return
        }
        if (disconnectImmediately) {
            runCatching { gatt.disconnect() }
                .onFailure {
                    finishDisconnectFailure(
                        route,
                        connectionError(it.message ?: "Android failed to continue a pending disconnect"),
                    )
                }
        }
    }

    private fun discoverGattOnHandler(route: ActiveConnection) {
        val pending: PendingDiscovery
        val gatt: BluetoothGatt?
        synchronized(stateLock) {
            if (!isActive(route) || route.phase != ConnectionPhase.CONNECTED) return
            pending = route.pendingDiscovery ?: return
            gatt = route.gatt
        }
        if (gatt == null) {
            synchronized(stateLock) {
                if (isActive(route) && route.pendingDiscovery === pending) {
                    route.pendingDiscovery = null
                } else {
                    return
                }
            }
            emitCommandError(
                pending.command.commandId,
                gattError("Android GATT object is unavailable during discovery"),
            )
            return
        }
        armWatchdog(
            route,
            gatt,
            ActiveNativeOperation.Discovery(pending),
            "Android GATT service discovery",
        )
        try {
            if (!gatt.discoverServices()) {
                finishGattDiscovery(
                    route,
                    gatt,
                    gattError("Android rejected the GATT service discovery request"),
                )
            }
        } catch (error: SecurityException) {
            finishGattDiscovery(route, gatt, permissionError(error.message))
        } catch (error: IllegalStateException) {
            finishGattDiscovery(
                route,
                gatt,
                gattError(error.message ?: "Android GATT discovery is not available"),
            )
        }
    }

    private fun handleServicesChanged(route: ActiveConnection, gatt: BluetoothGatt) {
        val invalidationError = gattError(
            "Android GATT services changed; reconnect before rediscovering",
        )
        val retiredSubscriptions: List<BluetoothGattCharacteristic>
        val pendingEvents: List<BleEvent>
        synchronized(stateLock) {
            if (!isActive(route) || route.phase != ConnectionPhase.CONNECTED || route.gatt !== gatt) return
            retiredSubscriptions =
                route.subscriptionsById.values.map { it.characteristic as BluetoothGattCharacteristic }
            pendingEvents = retireGattStateLocked(route, invalidationError)
            route.phase = ConnectionPhase.DISCONNECTING
            route.disconnectCommandId = null
        }
        retiredSubscriptions.forEach { characteristic ->
            runCatching { gatt.setCharacteristicNotification(characteristic, false) }
        }
        pendingEvents.forEach(::push)
        runCatching { gatt.disconnect() }
            .onFailure {
                finishConnectionFailure(
                    route,
                    connectionError(it.message ?: "Android failed to close an invalidated GATT connection"),
                )
            }
    }

    private fun handleServicesDiscovered(route: ActiveConnection, gatt: BluetoothGatt, status: Int) {
        synchronized(stateLock) {
            if (!isActive(route) || route.phase != ConnectionPhase.CONNECTED ||
                route.gatt !== gatt || route.pendingDiscovery == null
            ) return
        }
        if (status != BluetoothGatt.GATT_SUCCESS) {
            finishGattDiscovery(
                route,
                gatt,
                gattError("Android GATT service discovery ended with status $status", status),
            )
            return
        }
        val draft = try {
            buildGattDraft(gatt)
        } catch (error: RuntimeException) {
            finishGattDiscovery(
                route,
                gatt,
                gattError(error.message ?: "Android returned an invalid GATT database"),
            )
            return
        }
        val command: DiscoverGattCommand
        val database: GattDatabase
        val watchdog: Runnable?
        synchronized(stateLock) {
            val pending = route.pendingDiscovery
            if (!isActive(route) || route.phase != ConnectionPhase.CONNECTED ||
                route.gatt !== gatt || pending == null
            ) return
            command = pending.command
            database = GattDatabase(
                connectionId = command.connectionId,
                gattSessionId = command.gattSessionId,
                services = draft.services,
            )
            route.pendingDiscovery = null
            watchdog = route.activeWatchdog.clear()
            route.gattSessionId = command.gattSessionId
            route.characteristicsById = draft.characteristics
            route.descriptorsById = draft.descriptors
        }
        watchdog?.let(handler::removeCallbacks)
        push(BleEvent.GattDiscovered(command.commandId, database))
        push(BleEvent.CommandCompleted(command.commandId, null))
    }

    private fun buildGattDraft(gatt: BluetoothGatt): GattDraft =
        buildAndroidGattDraft(gatt, ::allocateGattObjectId)

    private fun allocateGattObjectId(): ULong = synchronized(stateLock) {
        check(nextGattObjectId != 0UL) { "Android GATT object ID space is exhausted" }
            .let {
                val id = nextGattObjectId
                nextGattObjectId++
                id
            }
    }

    private fun finishGattDiscovery(
        route: ActiveConnection,
        gatt: BluetoothGatt,
        error: BleEventError,
    ) {
        val commandId: CommandId
        val watchdog: Runnable?
        synchronized(stateLock) {
            if (!isActive(route) || route.phase != ConnectionPhase.CONNECTED ||
                route.gatt !== gatt
            ) return
            commandId = route.pendingDiscovery?.command?.commandId ?: return
            route.pendingDiscovery = null
            watchdog = route.activeWatchdog.clear()
        }
        watchdog?.let(handler::removeCallbacks)
        push(BleEvent.CommandCompleted(commandId, error))
    }

    private fun driveIoQueue(route: ActiveConnection) {
        val (gatt, operation) = synchronized(stateLock) {
            if (!isActive(route) || route.phase != ConnectionPhase.CONNECTED) return
            val gatt = route.gatt ?: return
            val operation = route.pendingIo.begin() ?: return
            gatt to operation
        }
        armWatchdog(
            route,
            gatt,
            ActiveNativeOperation.Io(operation),
            "Android GATT I/O operation ${operation.commandId.value}",
        )
        when (operation) {
            is PendingIo.Read -> readCharacteristicOnHandler(route, gatt, operation)
            is PendingIo.Write<*> -> writeCharacteristicOnHandler(route, gatt, operation)
            is PendingIo.Notification<*, *> -> configureNotificationsOnHandler(route, gatt, operation)
            is PendingIo.Rssi -> readRssiOnHandler(route, gatt, operation)
        }
    }

    private fun armWatchdog(
        route: ActiveConnection,
        gatt: BluetoothGatt,
        operation: ActiveNativeOperation,
        context: String,
    ) {
        lateinit var watchdog: Runnable
        watchdog = Runnable { handleWatchdog(route, gatt, operation, context, watchdog) }
        val (armed, replaced) = synchronized(stateLock) {
            if (!isActiveOperationLocked(route, gatt, operation)) {
                false to null
            } else {
                true to route.activeWatchdog.install(watchdog)
            }
        }
        if (!armed) return
        replaced?.let(handler::removeCallbacks)
        if (!handler.postDelayed(watchdog, activeOperationWatchdogMs)) {
            // A rejected delayed post means the owner Handler is already stopping;
            // fail closed instead of leaving the active native slot unbounded.
            handleWatchdog(route, gatt, operation, context, watchdog)
        }
    }

    private fun isActiveIo(
        route: ActiveConnection,
        gatt: BluetoothGatt,
        operation: PendingIo,
    ): Boolean = synchronized(stateLock) {
        isActiveIoLocked(route, gatt, operation)
    }

    private fun isActiveIoLocked(
        route: ActiveConnection,
        gatt: BluetoothGatt,
        operation: PendingIo,
    ): Boolean = isActive(route) && route.phase == ConnectionPhase.CONNECTED &&
            route.gatt === gatt && route.pendingIo.isActive(operation)

    private fun completeIo(
        route: ActiveConnection,
        gatt: BluetoothGatt,
        operation: PendingIo,
    ): Boolean {
        val watchdog: Runnable?
        val completed: Boolean
        synchronized(stateLock) {
            completed = isActiveIoLocked(route, gatt, operation) &&
                    route.pendingIo.complete(operation)
            watchdog = if (completed) route.activeWatchdog.clear() else null
        }
        watchdog?.let(handler::removeCallbacks)
        return completed
    }

    private fun isActiveOperationLocked(
        route: ActiveConnection,
        gatt: BluetoothGatt,
        operation: ActiveNativeOperation,
    ): Boolean = when (operation) {
        is ActiveNativeOperation.Io -> isActiveIoLocked(route, gatt, operation.operation)
        is ActiveNativeOperation.Discovery ->
            isActive(route) && route.phase == ConnectionPhase.CONNECTED &&
                    route.gatt === gatt && route.pendingDiscovery === operation.pending
    }

    private fun handleWatchdog(
        route: ActiveConnection,
        gatt: BluetoothGatt,
        operation: ActiveNativeOperation,
        context: String,
        watchdog: Runnable,
    ) {
        val pendingEvents: List<BleEvent>
        synchronized(stateLock) {
            if (!isActiveOperationLocked(route, gatt, operation) ||
                !route.activeWatchdog.takeIfCurrent(watchdog)
            ) return
            val timeout = operationTimeoutError(
                "$context exceeded the native callback watchdog",
            )
            val aborted = operationAbortedError(
                "Android GATT connection recovery retired a pending operation",
            )
            pendingEvents = when (operation) {
                is ActiveNativeOperation.Io -> retireGattStateLocked(
                    route,
                    error = aborted,
                    activeOperationError = timeout,
                    queuedOperationError = aborted,
                )

                is ActiveNativeOperation.Discovery -> retireGattStateLocked(
                    route,
                    error = timeout,
                    activeOperationError = aborted,
                    queuedOperationError = aborted,
                )
            }
            removeConnectionLocked(route)
        }

        // The old route is already retired before native teardown. Any callback
        // arriving after this point fails the exact route/gatt/operation checks.
        runCatching { gatt.disconnect() }
        runCatching { gatt.close() }
        pendingEvents.forEach(::push)
        push(
            BleEvent.ConnectionStateChanged(
                commandId = null,
                connectionId = route.command.connectionId,
                result = ConnectionStateResult.Err(
                    connectionError("Android GATT operation watchdog retired the connection"),
                ),
            ),
        )
    }

    private fun readRssiOnHandler(
        route: ActiveConnection,
        gatt: BluetoothGatt,
        pending: PendingIo.Rssi,
    ) {
        if (!isActiveIo(route, gatt, pending)) return
        try {
            if (!gatt.readRemoteRssi()) {
                finishRssi(route, gatt, pending, 0, gattError("Android rejected the RSSI read request"))
            }
        } catch (error: SecurityException) {
            finishRssi(route, gatt, pending, 0, permissionError(error.message))
        } catch (error: IllegalStateException) {
            finishRssi(
                route,
                gatt,
                pending,
                0,
                gattError(error.message ?: "Android RSSI read is not available"),
            )
        }
    }

    private fun handleReadRemoteRssi(
        route: ActiveConnection,
        gatt: BluetoothGatt,
        rssi: Int,
        status: Int,
    ) {
        val pending = synchronized(stateLock) {
            if (!isActive(route) || route.gatt !== gatt) return
            route.pendingIo.active() as? PendingIo.Rssi
        } ?: return
        finishRssi(
            route,
            gatt,
            pending,
            rssi,
            if (status == BluetoothGatt.GATT_SUCCESS) {
                null
            } else {
                gattError("Android RSSI read ended with status $status", status)
            },
        )
    }

    private fun finishRssi(
        route: ActiveConnection,
        gatt: BluetoothGatt,
        pending: PendingIo.Rssi,
        rssi: Int,
        error: BleEventError?,
    ) {
        if (!completeIo(route, gatt, pending)) return
        if (error != null) {
            push(BleEvent.CommandCompleted(pending.command.commandId, error))
        } else {
            push(
                BleEvent.RssiRead(
                    commandId = pending.command.commandId,
                    connectionId = pending.command.connectionId,
                    rssi = rssi.coerceIn(Short.MIN_VALUE.toInt(), Short.MAX_VALUE.toInt()).toShort(),
                ),
            )
            push(BleEvent.CommandCompleted(pending.command.commandId, null))
        }
        driveIoQueue(route)
    }

    private fun configureNotificationsOnHandler(
        route: ActiveConnection,
        gatt: BluetoothGatt,
        pending: PendingIo.Notification<*, *>,
    ) {
        notificationLifecycle(route, gatt).submit(pending)
    }

    private fun notificationLifecycle(route: ActiveConnection, gatt: BluetoothGatt) =
        NotificationLifecycle(
            stateLock = stateLock,
            route = route,
            ownsRoute = {
                lifecycle != Lifecycle.CLOSING && lifecycle != Lifecycle.CLOSED &&
                        isActive(route) && route.gatt === gatt
            },
            setLocalRegistration = { pending, enabled ->
                gatt.setCharacteristicNotification(pending.characteristic as BluetoothGattCharacteristic, enabled)
            },
            submitDescriptor = { pending ->
                val descriptor = pending.descriptor as BluetoothGattDescriptor
                val value = when {
                    !pending.enabling -> BluetoothGattDescriptor.DISABLE_NOTIFICATION_VALUE
                    pending.mode == NotificationMode.INDICATION -> BluetoothGattDescriptor.ENABLE_INDICATION_VALUE
                    else -> BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE
                }
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                    gatt.writeDescriptor(descriptor, value.clone()) == 0
                } else {
                    @Suppress("DEPRECATION")
                    run {
                        descriptor.value = value.clone()
                        gatt.writeDescriptor(descriptor)
                    }
                }
            },
            reportCompensationFailure = { commandId, error ->
                Log.w(
                    TAG,
                    "Android best-effort local notification compensation failed for command ${commandId.value}",
                    error
                )
            },
            removeWatchdog = { handler.removeCallbacks(it) },
            publish = ::push,
            driveQueue = { driveIoQueue(route) },
        )

    private fun handleDescriptorWrite(
        route: ActiveConnection,
        gatt: BluetoothGatt,
        descriptor: BluetoothGattDescriptor,
        status: Int,
    ) {
        notificationLifecycle(route, gatt).onCallback(descriptor, status)
    }

    private fun handleCharacteristicChanged(
        route: ActiveConnection,
        gatt: BluetoothGatt,
        characteristic: BluetoothGattCharacteristic,
        value: ByteArray,
    ) {
        val subscription: ActiveSubscription<*>
        synchronized(stateLock) {
            subscription = resolveCharacteristicNotificationRoute(
                active = isActive(route) && route.phase == ConnectionPhase.CONNECTED,
                expectedGatt = route.gatt,
                actualGatt = gatt,
                actualCharacteristic = characteristic,
                subscriptions = route.subscriptionsById.values,
                characteristicOf = { it.characteristic },
            ) ?: return
        }
        push(
            BleEvent.NotificationReceived(
                connectionId = route.command.connectionId,
                gattSessionId = route.gattSessionId ?: return,
                subscriptionId = subscription.subscriptionId,
                characteristicId = subscription.characteristicId,
                value = value,
            ),
        )
    }

    private fun selectNotificationMode(
        request: NotificationModeRequest,
        characteristic: BluetoothGattCharacteristic,
    ): NotificationMode = when (request) {
        NotificationModeRequest.PLATFORM_DEFAULT -> when {
            characteristic.properties and BluetoothGattCharacteristic.PROPERTY_NOTIFY != 0 ->
                NotificationMode.NOTIFICATION

            characteristic.properties and BluetoothGattCharacteristic.PROPERTY_INDICATE != 0 ->
                NotificationMode.INDICATION

            else -> throw CommandDispatchException.InvalidRequest(
                "characteristic advertises neither Notify nor Indicate",
            )
        }

        NotificationModeRequest.REQUIRE_NOTIFICATION -> {
            if (characteristic.properties and BluetoothGattCharacteristic.PROPERTY_NOTIFY == 0) {
                throw CommandDispatchException.InvalidRequest(
                    "characteristic does not support required Notification mode",
                )
            }
            NotificationMode.NOTIFICATION
        }

        NotificationModeRequest.REQUIRE_INDICATION -> {
            if (characteristic.properties and BluetoothGattCharacteristic.PROPERTY_INDICATE == 0) {
                throw CommandDispatchException.InvalidRequest(
                    "characteristic does not support required Indication mode",
                )
            }
            NotificationMode.INDICATION
        }
    }

    private fun readCharacteristicOnHandler(
        route: ActiveConnection,
        gatt: BluetoothGatt,
        pending: PendingIo.Read,
    ) {
        if (!isActiveIo(route, gatt, pending)) return
        val characteristic = pending.characteristic
        try {
            if (!gatt.readCharacteristic(characteristic)) {
                finishCharacteristicRead(
                    route,
                    gatt,
                    pending,
                    gattError("Android rejected the characteristic read request"),
                )
            }
        } catch (error: SecurityException) {
            finishCharacteristicRead(route, gatt, pending, permissionError(error.message))
        } catch (error: IllegalStateException) {
            finishCharacteristicRead(
                route,
                gatt,
                pending,
                gattError(error.message ?: "Android characteristic read is not available"),
            )
        }
    }

    private fun writeCharacteristicOnHandler(
        route: ActiveConnection,
        gatt: BluetoothGatt,
        pending: PendingIo.Write<*>,
    ) {
        writeLifecycle(route, gatt).submit(pending) {
            // Production admission always supplies the Framework characteristic.
            val characteristic = pending.characteristic as BluetoothGattCharacteristic
            val command = pending.command
            val writeType = when (command.writeType) {
                WriteType.WITH_RESPONSE -> BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT
                WriteType.WITHOUT_RESPONSE -> BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                gatt.writeCharacteristic(characteristic, command.value.clone(), writeType) == 0
            } else {
                @Suppress("DEPRECATION")
                run {
                    characteristic.writeType = writeType
                    characteristic.value = command.value.clone()
                    gatt.writeCharacteristic(characteristic)
                }
            }
        }
    }

    private fun handleCharacteristicRead(
        route: ActiveConnection,
        gatt: BluetoothGatt,
        characteristic: BluetoothGattCharacteristic,
        value: ByteArray,
        status: Int,
    ) {
        val pending = synchronized(stateLock) {
            if (!isActive(route) || route.gatt !== gatt || route.phase != ConnectionPhase.CONNECTED) {
                null
            } else {
                (route.pendingIo.active() as? PendingIo.Read)
                    ?.takeIf { it.characteristic === characteristic }
            }
        } ?: return
        if (status != BluetoothGatt.GATT_SUCCESS) {
            finishCharacteristicRead(
                route,
                gatt,
                pending,
                gattError("Android characteristic read ended with status $status", status),
            )
            return
        }
        if (!completeIo(route, gatt, pending)) return
        push(
            BleEvent.CharacteristicRead(
                commandId = pending.command.commandId,
                connectionId = pending.command.connectionId,
                gattSessionId = pending.command.gattSessionId,
                characteristicId = pending.command.characteristicId,
                value = value,
            ),
        )
        push(BleEvent.CommandCompleted(pending.command.commandId, null))
        driveIoQueue(route)
    }

    private fun handleCharacteristicWrite(
        route: ActiveConnection,
        gatt: BluetoothGatt,
        characteristic: BluetoothGattCharacteristic,
        status: Int,
    ) {
        writeLifecycle(route, gatt).onCallback(characteristic, status)
    }

    private fun finishCharacteristicRead(
        route: ActiveConnection,
        gatt: BluetoothGatt,
        pending: PendingIo.Read,
        error: BleEventError,
    ) {
        if (!completeIo(route, gatt, pending)) return
        push(BleEvent.CommandCompleted(pending.command.commandId, error))
        driveIoQueue(route)
    }

    private fun writeLifecycle(route: ActiveConnection, gatt: BluetoothGatt) =
        CharacteristicWriteLifecycle(
            stateLock = stateLock,
            route = route,
            ownsRoute = {
                lifecycle != Lifecycle.CLOSING && lifecycle != Lifecycle.CLOSED &&
                        isActive(route) && route.gatt === gatt
            },
            removeWatchdog = { handler.removeCallbacks(it) },
            publish = ::push,
            driveQueue = { driveIoQueue(route) },
        )

    /** Called only while stateLock is held; admission never invokes native code. */
    private fun enqueuePendingIoLocked(route: ActiveConnection, operation: PendingIo) {
        if (route.pendingIo.contains(operation.commandId)) {
            throw CommandDispatchException.InvalidRequest(
                "the command ID is already pending for this Android connection",
            )
        }
        if (!route.pendingIo.enqueue(operation)) {
            throw CommandDispatchException.ResourceExhausted(
                "Android connection GATT operation queue is full",
            )
        }
    }

    private fun requireConnectedRoute(connectionId: ULong): ActiveConnection {
        val route = connections.find(connectionId)
            ?: throw CommandDispatchException.InvalidState(
                "connection lifecycle is not active",
            )
        if (route.phase != ConnectionPhase.CONNECTED) {
            throw CommandDispatchException.InvalidState(
                "connection lifecycle is not connected",
            )
        }
        if (route.pendingDiscovery != null) {
            throw CommandDispatchException.InvalidState(
                "GATT discovery is pending on this Android connection",
            )
        }
        return route
    }

    private fun requireGattSession(route: ActiveConnection, sessionId: ULong) {
        if (route.gattSessionId?.value != sessionId) {
            throw CommandDispatchException.InvalidRequest(
                "the GATT session ID does not match the active Android database",
            )
        }
    }

    private fun disconnectOnHandler(route: ActiveConnection) {
        val retiredSubscriptions: List<BluetoothGattCharacteristic>
        val pendingEvents: List<BleEvent>
        synchronized(stateLock) {
            if (!isActive(route) || lifecycle == Lifecycle.CLOSED || lifecycle == Lifecycle.CLOSING) return
            retiredSubscriptions =
                route.subscriptionsById.values.map { it.characteristic as BluetoothGattCharacteristic }
            pendingEvents = retireGattStateLocked(
                route,
                connectionError("Android GATT activity was retired by disconnect"),
            )
        }
        retiredSubscriptions.forEach { characteristic ->
            runCatching { route.gatt?.setCharacteristicNotification(characteristic, false) }
        }
        pendingEvents.forEach(::push)
        push(
            BleEvent.ConnectionStateChanged(
                commandId = route.disconnectCommandId,
                connectionId = route.command.connectionId,
                result = ConnectionStateResult.Ok(ConnectionState.DISCONNECTING),
            ),
        )
        route.gatt?.let { gatt ->
            try {
                gatt.disconnect()
            } catch (error: RuntimeException) {
                finishDisconnectFailure(
                    route,
                    connectionError(error.message ?: "Android failed to request GATT disconnect"),
                )
            }
        }
    }

    private fun handleConnectionStateChange(
        route: ActiveConnection,
        gatt: BluetoothGatt,
        status: Int,
        newState: Int,
    ) {
        synchronized(stateLock) {
            if (!isActive(route) || route.gatt !== gatt ||
                lifecycle == Lifecycle.CLOSED || lifecycle == Lifecycle.CLOSING
            ) return
        }
        if (newState == BluetoothProfile.STATE_CONNECTED && status == BluetoothGatt.GATT_SUCCESS) {
            val continueDisconnect = synchronized(stateLock) {
                if (!isActive(route) || route.gatt !== gatt) return
                if (route.phase == ConnectionPhase.DISCONNECTING) {
                    true
                } else if (route.connectCompleted) {
                    return
                } else {
                    route.phase = ConnectionPhase.CONNECTED
                    route.connectCompleted = true
                    false
                }
            }
            if (continueDisconnect) {
                runCatching { gatt.disconnect() }
                    .onFailure {
                        finishDisconnectFailure(
                            route,
                            connectionError(it.message ?: "Android failed to continue a pending disconnect"),
                        )
                    }
                return
            }
            push(
                BleEvent.ConnectionStateChanged(
                    commandId = route.command.commandId,
                    connectionId = route.command.connectionId,
                    result = ConnectionStateResult.Ok(ConnectionState.CONNECTED),
                ),
            )
            push(BleEvent.CommandCompleted(route.command.commandId, null))
            synchronized(stateLock) {
                if (isActive(route) && route.disconnectCommandId != null) {
                    runCatching { gatt.disconnect() }
                }
            }
            return
        }
        if (newState == BluetoothProfile.STATE_DISCONNECTED) {
            finishConnectionDisconnected(route, status)
        }
    }

    private fun finishConnectionFailure(route: ActiveConnection, error: BleEventError) {
        val disconnectCommandId: CommandId?
        val connectCompleted: Boolean
        val pendingEvents: List<BleEvent>
        synchronized(stateLock) {
            if (!removeConnectionLocked(route)) return
            pendingEvents = retireGattStateLocked(route, error)
            disconnectCommandId = route.disconnectCommandId
            connectCompleted = route.connectCompleted
        }
        closeGatt(route)
        pendingEvents.forEach(::push)
        if (!connectCompleted) {
            push(
                BleEvent.ConnectionStateChanged(
                    commandId = route.command.commandId,
                    connectionId = route.command.connectionId,
                    result = ConnectionStateResult.Err(error),
                ),
            )
            push(BleEvent.CommandCompleted(route.command.commandId, error))
        }
        if (connectCompleted && disconnectCommandId == null) {
            push(
                BleEvent.ConnectionStateChanged(
                    commandId = null,
                    connectionId = route.command.connectionId,
                    result = ConnectionStateResult.Err(error),
                ),
            )
        }
        disconnectCommandId?.let { commandId ->
            push(
                BleEvent.ConnectionStateChanged(
                    commandId = commandId,
                    connectionId = route.command.connectionId,
                    result = ConnectionStateResult.Ok(ConnectionState.DISCONNECTED),
                ),
            )
            push(BleEvent.CommandCompleted(commandId, null))
        }
    }

    private fun finishConnectionDisconnected(route: ActiveConnection, status: Int) {
        connections.disconnected(route, status)
    }

    private fun finishDisconnectFailure(route: ActiveConnection, error: BleEventError) {
        val commandId: CommandId
        val pendingEvents: List<BleEvent>
        synchronized(stateLock) {
            if (!removeConnectionLocked(route)) return
            commandId = route.disconnectCommandId ?: return
            pendingEvents = retireGattStateLocked(route, error)
        }
        closeGatt(route)
        pendingEvents.forEach(::push)
        push(
            BleEvent.ConnectionStateChanged(
                commandId = commandId,
                connectionId = route.command.connectionId,
                result = ConnectionStateResult.Err(error),
            ),
        )
        push(BleEvent.CommandCompleted(commandId, error))
    }

    private fun closeGatt(route: ActiveConnection) {
        route.gatt?.let { gatt ->
            runCatching { gatt.close() }
        }
    }

    private fun retireGattStateLocked(
        route: ActiveConnection,
        error: BleEventError,
        activeOperationError: BleEventError = error,
        queuedOperationError: BleEventError = error,
    ): List<BleEvent> {
        return connections.retireGattState(route, error, activeOperationError, queuedOperationError)
    }

    private fun isActive(route: ActiveConnection): Boolean = connections.isActive(route)

    private fun removeConnection(route: ActiveConnection) {
        synchronized(stateLock) {
            removeConnectionLocked(route)
        }
    }

    private fun removeConnectionLocked(route: ActiveConnection): Boolean = connections.remove(route)

    private fun initializeOnHandler(command: InitializeCommand) {
        synchronized(stateLock) {
            if (lifecycle == Lifecycle.CLOSED || lifecycle == Lifecycle.CLOSING) return
        }
        val adapter = bluetoothManager?.adapter
        bluetoothAdapter = adapter
        val snapshot = adapterSnapshot(adapter)
        synchronized(stateLock) {
            if (lifecycle == Lifecycle.INITIALIZING) lifecycle = Lifecycle.READY
        }
        push(BleEvent.AdapterSnapshotChanged(command.commandId, snapshot))
        push(BleEvent.CommandCompleted(command.commandId, null))
    }

    private fun startScanOnHandler(command: StartScanCommand, reservation: ActiveScan) {
        synchronized(stateLock) {
            if ((lifecycle == Lifecycle.CLOSED || lifecycle == Lifecycle.CLOSING) ||
                activeScan !== reservation
            ) return
        }
        val adapter = bluetoothAdapter ?: run {
            retireScanReservation(reservation)
            emitCommandError(command.commandId, adapterUnavailable("Bluetooth adapter is unavailable"))
            return
        }
        val scanner = try {
            adapter.bluetoothLeScanner
        } catch (error: SecurityException) {
            retireScanReservation(reservation)
            emitCommandError(command.commandId, permissionError(error.message))
            return
        }
        if (scanner == null) {
            retireScanReservation(reservation)
            emitCommandError(command.commandId, adapterUnavailable("Bluetooth LE scanner is unavailable"))
            return
        }

        val callback = object : ScanCallback() {
            override fun onScanResult(callbackType: Int, result: ScanResult) {
                handler.post { handleScanResult(command, reservation, result) }
            }

            override fun onBatchScanResults(results: MutableList<ScanResult>) {
                handler.post {
                    results.forEach { result -> handleScanResult(command, reservation, result) }
                }
            }

            override fun onScanFailed(errorCode: Int) {
                handler.post { handleScanFailed(command, reservation, errorCode) }
            }
        }
        // Do not rely on vendor-specific controller filter offload. In particular,
        // some Android stacks fail to report a single non-canonical service filter
        // while reporting the same peripheral when multiple filters are present.
        // Local filtering below preserves the contract without that variation.
        val settings = ScanSettings.Builder()
            .setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY)
            .setCallbackType(
                ScanSettings.CALLBACK_TYPE_ALL_MATCHES,
            )
            .build()
        synchronized(stateLock) {
            if ((lifecycle == Lifecycle.CLOSED || lifecycle == Lifecycle.CLOSING) ||
                activeScan !== reservation
            ) return
            reservation.scanner = scanner
            reservation.callback = callback
        }
        try {
            scanner.startScan(emptyList(), settings, callback)
        } catch (error: SecurityException) {
            retireScanReservation(reservation)
            emitCommandError(command.commandId, permissionError(error.message))
            return
        } catch (error: IllegalStateException) {
            retireScanReservation(reservation)
            emitCommandError(
                command.commandId,
                adapterUnavailable(error.message ?: "Android adapter is not enabled"),
            )
            return
        }
        push(BleEvent.ScanStarted(command.commandId, command.scanSessionId))
        push(BleEvent.CommandCompleted(command.commandId, null))
    }

    private fun stopScanOnHandler(command: StopScanCommand, reservation: ActiveScan) {
        synchronized(stateLock) {
            if (activeScan !== reservation) return
            activeScan = null
        }
        val scanner = reservation.scanner
        val callback = reservation.callback
        if (scanner == null || callback == null) {
            val error = BleEventError(
                BleErrorCategory.INVALID_STATE,
                "Android scan start had not registered a native callback",
                platformDetails(null, "ScanCallback", null),
            )
            push(BleEvent.ScanStopped(command.scanSessionId, ScanStopResult.Err(error)))
            push(BleEvent.CommandCompleted(command.commandId, error))
            return
        }
        try {
            scanner.stopScan(callback)
        } catch (error: SecurityException) {
            push(
                BleEvent.ScanStopped(
                    command.scanSessionId,
                    ScanStopResult.Err(permissionError(error.message)),
                ),
            )
            push(BleEvent.CommandCompleted(command.commandId, permissionError(error.message)))
            return
        }
        push(
            BleEvent.ScanStopped(
                command.scanSessionId,
                ScanStopResult.Ok(ScanStopReason.Requested(command.commandId)),
            ),
        )
        push(BleEvent.CommandCompleted(command.commandId, null))
    }

    private fun handleScanResult(
        command: StartScanCommand,
        reservation: ActiveScan,
        result: ScanResult,
    ) {
        synchronized(stateLock) {
            if (activeScan !== reservation || lifecycle == Lifecycle.CLOSED ||
                lifecycle == Lifecycle.CLOSING
            ) return
        }
        val record = result.scanRecord ?: return
        val advertisedServiceUuids = record.serviceUuids.orEmpty().map { it.uuid.toString() }
        val requestedServiceUuids = command.options.serviceUuids.map { it.value }
        if (requestedServiceUuids.isNotEmpty() &&
            !matchesRequestedServiceUuids(requestedServiceUuids, advertisedServiceUuids)
        ) {
            return
        }
        val localName = record.deviceName ?: safeDeviceName(result)
        if (command.options.namePrefix != null &&
            !(localName?.startsWith(command.options.namePrefix!!) ?: false)
        ) {
            return
        }
        if (command.options.companyId != null &&
            record.manufacturerSpecificData.get(command.options.companyId!!.toInt()) == null
        ) {
            return
        }
        val address = result.device.address
        synchronized(stateLock) {
            if (!command.options.allowDuplicates && !reservation.reportedAddresses.add(address)) {
                return
            }
        }
        val peripheralId = peripheralIds.getOrPut(address) { nextPeripheralId++ }
        peripheralAddresses[peripheralId] = address
        val services = advertisedServiceUuids.map(::BluetoothUuid)
        val manufacturers = buildList {
            for (index in 0 until record.manufacturerSpecificData.size()) {
                val companyId = record.manufacturerSpecificData.keyAt(index)
                val payload = record.manufacturerSpecificData.valueAt(index)?.clone() ?: ByteArray(0)
                add(ManufacturerData(companyId.toUShort(), payload))
            }
        }
        val serviceData = record.serviceData?.map { (uuid, payload) ->
            ServiceData(BluetoothUuid(uuid.uuid.toString()), payload.clone())
        }.orEmpty()
        val connectable = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            result.isConnectable
        } else {
            null
        }
        push(
            BleEvent.AdvertisementObserved(
                AdvertisementObservation(
                    scanSessionId = command.scanSessionId,
                    peripheralId = dev.blehub.PeripheralId(peripheralId),
                    localName = localName,
                    advertisedServices = services,
                    manufacturerData = manufacturers,
                    serviceData = serviceData,
                    rssi = result.rssi.toShort(),
                    connectable = connectable,
                ),
            ),
        )
    }

    private fun handleScanFailed(
        command: StartScanCommand,
        reservation: ActiveScan,
        errorCode: Int,
    ) {
        synchronized(stateLock) {
            if (activeScan !== reservation || lifecycle == Lifecycle.CLOSED ||
                lifecycle == Lifecycle.CLOSING
            ) return
            activeScan = null
        }
        val error = BleEventError(
            category = BleErrorCategory.BACKEND,
            message = "Android LE scan failed with code $errorCode",
            platformDetails = platformDetails("$errorCode", "ScanCallback", null),
        )
        // The start command already received its single terminal when native
        // startScan returned. This is a passive scan failure, so only retire
        // the scan route and publish ScanStopped.
        push(BleEvent.ScanStopped(command.scanSessionId, ScanStopResult.Err(error)))
    }

    private fun adapterSnapshot(adapter: BluetoothAdapter?): AdapterSnapshot {
        val scanAuthorization = authorization(scanPermission())
        val connectionAuthorization = authorization(connectionPermission())
        val state = when {
            adapter == null -> AdapterState.UNAVAILABLE
            else -> try {
                if (adapter.isEnabled) AdapterState.POWERED_ON else AdapterState.POWERED_OFF
            } catch (_: SecurityException) {
                AdapterState.UNKNOWN
            }
        }
        val name = if (connectionAuthorization == AuthorizationState.AUTHORIZED) {
            try {
                adapter?.name
            } catch (_: SecurityException) {
                null
            }
        } else {
            null
        }
        return AdapterSnapshot(state, scanAuthorization, connectionAuthorization, name)
    }

    private fun safeDeviceName(result: ScanResult): String? = try {
        result.device.name
    } catch (_: SecurityException) {
        null
    }

    private fun requireIngress() {
        check(ingress != null) { "Android backend event ingress is not bound" }
    }

    private fun requireOpenAndReady() {
        when (lifecycle) {
            Lifecycle.READY -> requireIngress()
            Lifecycle.CLOSING, Lifecycle.CLOSED -> throw CommandDispatchException.Closed()
            Lifecycle.NEW, Lifecycle.INITIALIZING ->
                throw CommandDispatchException.InvalidState("Android adapter is not initialized")
        }
    }


    private fun emitCommandError(commandId: CommandId, error: BleEventError) {
        push(BleEvent.CommandCompleted(commandId, error))
    }

    private fun retireScanReservation(reservation: ActiveScan) {
        synchronized(stateLock) {
            if (activeScan === reservation) activeScan = null
        }
    }

    private fun push(event: BleEvent) {
        val target = synchronized(stateLock) {
            if (ingressClosed) return
            ingress
        } ?: return
        try {
            when (val status = target.push(event)) {
                EventIngressStatus.Delivered -> Unit
                EventIngressStatus.Closed -> synchronized(stateLock) {
                    ingressClosed = true
                }

                is EventIngressStatus.InvalidEvent ->
                    Log.w(TAG, "Android backend ingress rejected invalid event: ${status.message}")

                is EventIngressStatus.DownstreamFailed ->
                    Log.w(TAG, "Android backend downstream rejected event: ${status.message}")
            }
        } catch (error: RuntimeException) {
            Log.w(TAG, "Android backend ingress callback failed", error)
        }
    }

    private fun hasPermission(permission: String): Boolean =
        Build.VERSION.SDK_INT < Build.VERSION_CODES.M ||
                appContext.checkSelfPermission(permission) == PackageManager.PERMISSION_GRANTED

    private fun authorization(permission: String): AuthorizationState = when {
        hasPermission(permission) -> AuthorizationState.AUTHORIZED
        else -> AuthorizationState.DENIED
    }

    private fun scanPermission(): String = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
        Manifest.permission.BLUETOOTH_SCAN
    } else {
        Manifest.permission.ACCESS_FINE_LOCATION
    }

    private fun connectionPermission(): String = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
        Manifest.permission.BLUETOOTH_CONNECT
    } else {
        Manifest.permission.BLUETOOTH
    }


    private companion object {
        const val TAG = "AndroidBleBackend"
        const val DISPOSE_TIMEOUT_MS = 2_000L
        val CLIENT_CONFIGURATION_UUID: UUID =
            UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")
    }
}
