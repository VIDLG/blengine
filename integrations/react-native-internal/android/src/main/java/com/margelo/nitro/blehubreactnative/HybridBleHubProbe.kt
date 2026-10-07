package com.margelo.nitro.blehubreactnative

import android.os.Handler
import android.os.HandlerThread
import android.util.Log
import com.margelo.nitro.core.ArrayBuffer
import com.margelo.nitro.NitroModules
import com.margelo.nitro.core.Promise
import dev.blehub.BackendCapabilities
import dev.blehub.BackendEventIngressHandle
import dev.blehub.BleBackend
import dev.blehub.BleErrorCategory
import dev.blehub.BleEvent
import dev.blehub.BleEventError
import dev.blehub.BleEventSink
import dev.blehub.BleHub
import dev.blehub.BleHubException
import dev.blehub.BluetoothUuid
import dev.blehub.CommandDispatchException
import dev.blehub.CommandId
import dev.blehub.ConnectCommand
import dev.blehub.ConnectionState
import dev.blehub.ConnectionStateResult
import dev.blehub.DisconnectCommand
import dev.blehub.DiscoverGattCommand
import dev.blehub.GattCharacteristicId
import dev.blehub.GattSessionId
import dev.blehub.InitializeCommand
import dev.blehub.NotificationAction
import dev.blehub.NotificationMode
import dev.blehub.NotificationModeRequest
import dev.blehub.NotificationState
import dev.blehub.ReadCharacteristicCommand
import dev.blehub.ReadRssiCommand
import dev.blehub.ScanOptions
import dev.blehub.ScanSessionId
import dev.blehub.ScanStopReason
import dev.blehub.ScanStopResult
import dev.blehub.SetNotificationsCommand
import dev.blehub.StartScanCommand
import dev.blehub.StopScanCommand
import dev.blehub.SubscriptionId
import dev.blehub.WriteCharacteristicCommand
import dev.blehub.WriteType
import dev.blehub.Platform as RustPlatform
import dev.blehub.PlatformErrorDetails as RustPlatformErrorDetails
import dev.blehub.blehubContractVersion
import dev.blehub.UniffiWithHandle
import dev.blehub.uniffiEnsureInitialized
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import kotlin.concurrent.thread

/**
 * Internal Nitro probe backed by the production UniFFI BleHub object and Android backend.
 *
 * This remains a private integration probe rather than the future public RN API. Its
 * backend lifecycle and RF command projection are backed by the production Android backend;
 * this remains a private integration probe until the public RN API is designed.
 */
class HybridBleHubProbe private constructor(
    private val deterministicDisposeGate: CountDownLatch?,
) : HybridBleHubProbeSpec() {
    constructor() : this(null)


    private val lock = Any()
    private var completionListener: ((CompletionEvent) -> Unit)? = null
    private var rfScanListener: ((RfScanEvent) -> Unit)? = null
    private var isClosed = false
    private var hub: BleHub? = null
    private var backendThread: HandlerThread? = null
    private var deliveryThread: HandlerThread? = null
    private var eventHandoff: AndroidEventHandoff<BleEvent>? = null
    private var closePromise: Promise<Unit>? = null

    override fun initialize(commandId: String): DispatchError? {
        val value = parseCanonicalU64(commandId)
            ?: return DispatchError(
                DispatchErrorCategory.INVALID_REQUEST,
                "commandId must be a canonical unsigned decimal u64",
                null,
            )

        return try {
            val currentHub = synchronized(lock) {
                if (isClosed) {
                    return@synchronized null
                }
                hub ?: createHubLocked()
            } ?: return DispatchError(
                DispatchErrorCategory.CLOSED,
                "BleHub probe is closed",
                null,
            )

            currentHub.initialize(InitializeCommand(CommandId(value)))
            null
        } catch (error: CommandDispatchException) {
            error.toNitroError()
        } catch (error: BleHubException) {
            DispatchError(
                DispatchErrorCategory.FFI_CONTRACT,
                error.detailText(),
                null,
            )
        } catch (error: Throwable) {
            DispatchError(
                DispatchErrorCategory.FFI_CONTRACT,
                error.message ?: error::class.java.simpleName,
                null,
            )
        }
    }

    override fun setCompletionListener(listener: (CompletionEvent) -> Unit) {
        synchronized(lock) {
            if (!isClosed) completionListener = listener
        }
    }

    override fun removeCompletionListener() {
        synchronized(lock) {
            completionListener = null
        }
    }

    override fun setRfScanListener(listener: (RfScanEvent) -> Unit) {
        synchronized(lock) {
            if (!isClosed) rfScanListener = listener
        }
    }

    override fun removeRfScanListener() {
        synchronized(lock) {
            rfScanListener = null
        }
    }

    override fun startRfScan(commandId: String, scanSessionId: String, serviceUuid: String): DispatchError? {
        val command = parseCanonicalU64(commandId)
            ?: return invalidRequest("commandId must be a canonical unsigned decimal u64")
        val scan = parseCanonicalU64(scanSessionId)
            ?: return invalidRequest("scanSessionId must be a canonical unsigned decimal u64")
        val serviceUuids = parseServiceUuids(serviceUuid)
            ?: return invalidRequest("serviceUuid must contain one or more canonical UUIDs")
        return dispatch { hub ->
            hub.startScan(
                StartScanCommand(
                    commandId = CommandId(command),
                    scanSessionId = ScanSessionId(scan),
                    options = ScanOptions(
                        serviceUuids = serviceUuids,
                        namePrefix = null,
                        companyId = null,
                        allowDuplicates = false,
                    ),
                ),
            )
        }
    }

    private fun parseServiceUuids(value: String): List<BluetoothUuid>? {
        val values = value.split(',').map(String::trim).filter(String::isNotEmpty)
        if (values.isEmpty()) return null
        val uuids = values.mapNotNull { candidate ->
            runCatching { BluetoothUuid(candidate) }.getOrNull()
        }
        return uuids.takeIf { it.size == values.size }
    }

    override fun stopRfScan(commandId: String, scanSessionId: String): DispatchError? {
        val command = parseCanonicalU64(commandId)
            ?: return invalidRequest("commandId must be a canonical unsigned decimal u64")
        val scan = parseCanonicalU64(scanSessionId)
            ?: return invalidRequest("scanSessionId must be a canonical unsigned decimal u64")
        return dispatch { hub ->
            hub.stopScan(
                StopScanCommand(
                    commandId = CommandId(command),
                    scanSessionId = ScanSessionId(scan),
                ),
            )
        }
    }

    override fun connectRfPeripheral(commandId: String, connectionId: String, peripheralId: String): DispatchError? {
        val command = parseIds(commandId, connectionId, peripheralId)
            ?: return invalidRequest("commandId, connectionId and peripheralId must be canonical unsigned decimal u64 values")
        return dispatch { hub ->
            hub.connect(
                ConnectCommand(
                    commandId = CommandId(command[0]),
                    connectionId = dev.blehub.ConnectionId(command[1]),
                    peripheralId = dev.blehub.PeripheralId(command[2]),
                ),
            )
        }
    }

    override fun disconnectRfPeripheral(commandId: String, connectionId: String): DispatchError? {
        val command = parseIds(commandId, connectionId)
            ?: return invalidRequest("commandId and connectionId must be canonical unsigned decimal u64 values")
        return dispatch { hub ->
            hub.disconnect(
                DisconnectCommand(
                    commandId = CommandId(command[0]),
                    connectionId = dev.blehub.ConnectionId(command[1]),
                ),
            )
        }
    }

    override fun readRfRssi(commandId: String, connectionId: String): DispatchError? {
        val command = parseIds(commandId, connectionId)
            ?: return invalidRequest("commandId and connectionId must be canonical unsigned decimal u64 values")
        return dispatch { hub ->
            hub.readRssi(
                ReadRssiCommand(
                    commandId = CommandId(command[0]),
                    connectionId = dev.blehub.ConnectionId(command[1]),
                ),
            )
        }
    }

    override fun discoverRfGatt(commandId: String, connectionId: String, gattSessionId: String): DispatchError? {
        val command = parseIds(commandId, connectionId, gattSessionId)
            ?: return invalidRequest("commandId, connectionId and gattSessionId must be canonical unsigned decimal u64 values")
        return dispatch { hub ->
            hub.discoverGatt(
                DiscoverGattCommand(
                    commandId = CommandId(command[0]),
                    connectionId = dev.blehub.ConnectionId(command[1]),
                    gattSessionId = GattSessionId(command[2]),
                ),
            )
        }
    }

    override fun readRfCharacteristic(
        commandId: String,
        connectionId: String,
        gattSessionId: String,
        characteristicId: String,
    ): DispatchError? {
        val command = parseIds(commandId, connectionId, gattSessionId, characteristicId)
            ?: return invalidRequest("commandId, connectionId, gattSessionId and characteristicId must be canonical unsigned decimal u64 values")
        return dispatch { hub ->
            hub.readCharacteristic(
                ReadCharacteristicCommand(
                    commandId = CommandId(command[0]),
                    connectionId = dev.blehub.ConnectionId(command[1]),
                    gattSessionId = GattSessionId(command[2]),
                    characteristicId = GattCharacteristicId(command[3]),
                ),
            )
        }
    }

    override fun writeRfCharacteristic(
        commandId: String,
        connectionId: String,
        gattSessionId: String,
        characteristicId: String,
        value: ArrayBuffer,
        writeType: RfWriteType,
    ): DispatchError? {
        val command = parseIds(commandId, connectionId, gattSessionId, characteristicId)
            ?: return invalidRequest("commandId, connectionId, gattSessionId and characteristicId must be canonical unsigned decimal u64 values")
        return dispatch { hub ->
            hub.writeCharacteristic(
                WriteCharacteristicCommand(
                    commandId = CommandId(command[0]),
                    connectionId = dev.blehub.ConnectionId(command[1]),
                    gattSessionId = GattSessionId(command[2]),
                    characteristicId = GattCharacteristicId(command[3]),
                    value = value.toByteArray(),
                    writeType = when (writeType) {
                        RfWriteType.WITH_RESPONSE -> WriteType.WITH_RESPONSE
                        RfWriteType.WITHOUT_RESPONSE -> WriteType.WITHOUT_RESPONSE
                    },
                ),
            )
        }
    }

    override fun setRfNotifications(
        commandId: String,
        connectionId: String,
        gattSessionId: String,
        subscriptionId: String,
        characteristicId: String,
        enabled: Boolean,
        mode: RfNotificationMode,
    ): DispatchError? {
        val command = parseIds(commandId, connectionId, gattSessionId, subscriptionId, characteristicId)
            ?: return invalidRequest("all RF notification IDs must be canonical unsigned decimal u64 values")
        return dispatch { hub ->
            hub.setNotifications(
                SetNotificationsCommand(
                    commandId = CommandId(command[0]),
                    connectionId = dev.blehub.ConnectionId(command[1]),
                    gattSessionId = GattSessionId(command[2]),
                    subscriptionId = SubscriptionId(command[3]),
                    characteristicId = GattCharacteristicId(command[4]),
                    action = if (!enabled) {
                        NotificationAction.Disable
                    } else {
                        NotificationAction.Enable(
                            when (mode) {
                                RfNotificationMode.PLATFORM_DEFAULT -> NotificationModeRequest.PLATFORM_DEFAULT
                                RfNotificationMode.REQUIRE_NOTIFICATION -> NotificationModeRequest.REQUIRE_NOTIFICATION
                                RfNotificationMode.REQUIRE_INDICATION -> NotificationModeRequest.REQUIRE_INDICATION
                            },
                        )
                    },
                ),
            )
        }
    }

    override fun roundTrip(sample: TransportSample): TransportSample {
        return TransportSample(sample.id, ArrayBuffer.copy(sample.bytes), sample.error)
    }

    override fun close(): Promise<Unit> {
        val (promise, currentHub, currentBackendThread, currentDeliveryThread) =
            synchronized(lock) {
                closePromise?.let { return it }
                val promise = Promise<Unit>()
                closePromise = promise
                isClosed = true
                completionListener = null
                rfScanListener = null
                val handoff = eventHandoff.also { eventHandoff = null }
                handoff?.close()
                CloseState(
                    promise = promise,
                    hub = hub.also { hub = null },
                    backendThread = backendThread.also { backendThread = null },
                    deliveryThread = deliveryThread.also { deliveryThread = null },
                )
            }

        thread(name = "BleHubProbe-close") {
            val failure = try {
                // Dispose is the Rust/backend lifecycle operation. The Android backend
                // drains its owner Handler before the thread is stopped.
                if (currentHub != null) {
                    try {
                        currentHub.dispose()
                    } finally {
                        currentHub.close()
                    }
                }
                currentBackendThread?.quitSafely()
                currentBackendThread?.join(BACKEND_THREAD_JOIN_TIMEOUT_MS)
                currentDeliveryThread?.quitSafely()
                currentDeliveryThread?.join(DELIVERY_THREAD_JOIN_TIMEOUT_MS)
                null
            } catch (error: Throwable) {
                currentBackendThread?.quitSafely()
                currentDeliveryThread?.quitSafely()
                error
            }
            if (failure == null) promise.resolve(Unit) else promise.reject(failure)
        }
        return promise
    }

    private fun createHubLocked(): BleHub {
        uniffiEnsureInitialized()
        val backendThread = HandlerThread("BleHubProbe-android")
        val deliveryThread = HandlerThread("BleHubProbe-delivery")
        backendThread.start()
        deliveryThread.start()
        val handoff = AndroidEventHandoff(
            handler = Handler(deliveryThread.looper),
            capacity = EVENT_HANDOFF_CAPACITY,
            deliver = ::deliver,
            onFailure = ::handleEventHandoffFailure,
        )
        return try {
            val backend = deterministicDisposeGate?.let { gate ->
                DeterministicBackend(Handler(backendThread.looper), gate)
            } ?: run {
                val context = NitroModules.applicationContext
                    ?: error("NitroModules.applicationContext is unavailable")
                dev.blehub.android.AndroidBleBackend(context, Handler(backendThread.looper))
            }
            val downstream = CompletionSink(handoff)
            BleHub(backend, downstream).also {
                this.backendThread = backendThread
                this.deliveryThread = deliveryThread
                this.eventHandoff = handoff
                hub = it
            }
        } catch (error: Throwable) {
            handoff.close()
            backendThread.quitSafely()
            deliveryThread.quitSafely()
            throw error
        }
    }

    private fun handleEventHandoffFailure() {
        Log.e(TAG, "Android Nitro event handoff retired; closing probe")
        close()
    }

    private fun deliver(event: BleEvent) {
        Log.d(TAG, "Received production event ${event::class.java.simpleName}")
        val completion = (event as? BleEvent.CommandCompleted)?.let {
            CompletionEvent(
                it.commandId.value.toString(),
                it.error?.toNitroError(),
            )
        }
        val rfEvent = try {
            projectRfEvent(event)
        } catch (error: Throwable) {
            Log.e(TAG, "Unable to project production BleEvent to Nitro", error)
            null
        }
        val completionCallback: ((CompletionEvent) -> Unit)?
        val rfCallback: ((RfScanEvent) -> Unit)?
        synchronized(lock) {
            // Linearize listener selection with close, but never hold the probe lock while
            // entering Nitro. Nitro callbacks may re-enter the hybrid object during dispatch.
            if (isClosed) return
            completionCallback = completionListener
            rfCallback = rfScanListener
        }
        try {
            if (completion != null) completionCallback?.invoke(completion)
        } catch (error: Throwable) {
            Log.e(TAG, "Unable to deliver production completion to Nitro", error)
        }
        try {
            if (rfEvent != null) {
                Log.d(
                    TAG,
                    "Projected RF event kind=${rfEvent.kind} " +
                            "adapterState=${rfEvent.adapterState} " +
                            "scanAuthorization=${rfEvent.scanAuthorization}",
                )
                rfCallback?.invoke(rfEvent)
            }
        } catch (error: Throwable) {
            Log.e(TAG, "Unable to deliver production RF event to Nitro", error)
        }
    }

    private fun projectRfEvent(event: BleEvent): RfScanEvent? = when (event) {
        is BleEvent.AdapterSnapshotChanged -> RfScanEvent(
            kind = "adapterSnapshotChanged",
            commandId = event.commandId?.value?.toString(),
            scanSessionId = null,
            peripheralId = null,
            connectionId = null,
            connectionState = null,
            gattSessionId = null,
            characteristicId = null,
            subscriptionId = null,
            gattServices = null,
            value = null,
            notificationState = null,
            actualNotificationMode = null,
            rssi = null,
            localName = null,
            advertisedServices = null,
            adapterState = adapterStateText(event.snapshot.state),
            scanAuthorization = authorizationText(event.snapshot.scanAuthorization),
            error = null,
        )

        is BleEvent.ScanStarted -> RfScanEvent(
            kind = "scanStarted",
            commandId = event.commandId.value.toString(),
            scanSessionId = event.scanId.value.toString(),
            peripheralId = null,
            connectionId = null,
            connectionState = null,
            gattSessionId = null,
            characteristicId = null,
            subscriptionId = null,
            gattServices = null,
            value = null,
            notificationState = null,
            actualNotificationMode = null,
            rssi = null,
            localName = null,
            advertisedServices = null,
            adapterState = null,
            scanAuthorization = null,
            error = null,
        )

        is BleEvent.AdvertisementObserved -> RfScanEvent(
            kind = "advertisementObserved",
            commandId = null,
            scanSessionId = event.advertisement.scanSessionId.value.toString(),
            peripheralId = event.advertisement.peripheralId.value.toString(),
            connectionId = null,
            connectionState = null,
            gattSessionId = null,
            characteristicId = null,
            subscriptionId = null,
            gattServices = null,
            value = null,
            notificationState = null,
            actualNotificationMode = null,
            rssi = event.advertisement.rssi?.toDouble(),
            localName = event.advertisement.localName,
            advertisedServices = event.advertisement.advertisedServices
                .map { it.value }
                .toTypedArray(),
            adapterState = null,
            scanAuthorization = null,
            error = null,
        )

        is BleEvent.ConnectionStateChanged -> {
            val state = (event.result as? ConnectionStateResult.Ok)?.state
            val error = (event.result as? ConnectionStateResult.Err)?.error?.toNitroError()
            RfScanEvent(
                kind = "connectionStateChanged",
                commandId = event.commandId?.value?.toString(),
                scanSessionId = null,
                peripheralId = null,
                connectionId = event.connectionId.value.toString(),
                connectionState = state?.let(::connectionStateText),
                gattSessionId = null,
                characteristicId = null,
                subscriptionId = null,
                gattServices = null,
                value = null,
                notificationState = null,
                actualNotificationMode = null,
                rssi = null,
                localName = null,
                advertisedServices = null,
                adapterState = null,
                scanAuthorization = null,
                error = error,
            )
        }

        is BleEvent.GattDiscovered -> RfScanEvent(
            kind = "gattDiscovered",
            commandId = event.commandId.value.toString(),
            scanSessionId = null,
            peripheralId = null,
            connectionId = event.database.connectionId.value.toString(),
            connectionState = null,
            gattSessionId = event.database.gattSessionId.value.toString(),
            characteristicId = null,
            subscriptionId = null,
            gattServices = event.database.services.map(::gattService).toTypedArray(),
            value = null,
            notificationState = null,
            actualNotificationMode = null,
            rssi = null,
            localName = null,
            advertisedServices = null,
            adapterState = null,
            scanAuthorization = null,
            error = null,
        )

        is BleEvent.CharacteristicRead -> RfScanEvent(
            kind = "characteristicRead",
            commandId = event.commandId.value.toString(),
            scanSessionId = null,
            peripheralId = null,
            connectionId = event.connectionId.value.toString(),
            connectionState = null,
            gattSessionId = event.gattSessionId.value.toString(),
            characteristicId = event.characteristicId.value.toString(),
            subscriptionId = null,
            gattServices = null,
            value = event.value.toArrayBuffer(),
            notificationState = null,
            actualNotificationMode = null,
            rssi = null,
            localName = null,
            advertisedServices = null,
            adapterState = null,
            scanAuthorization = null,
            error = null,
        )

        is BleEvent.CharacteristicWritten -> RfScanEvent(
            kind = "characteristicWritten",
            commandId = event.commandId.value.toString(),
            scanSessionId = null,
            peripheralId = null,
            connectionId = event.connectionId.value.toString(),
            connectionState = null,
            gattSessionId = event.gattSessionId.value.toString(),
            characteristicId = event.characteristicId.value.toString(),
            subscriptionId = null,
            gattServices = null,
            value = null,
            notificationState = null,
            actualNotificationMode = null,
            rssi = null,
            localName = null,
            advertisedServices = null,
            adapterState = null,
            scanAuthorization = null,
            error = null,
        )

        is BleEvent.NotificationStateChanged -> {
            val notificationState = when (event.state) {
                is NotificationState.Enabled -> "Enabled"
                NotificationState.Disabled -> "Disabled"
            }
            val actualMode = (event.state as? NotificationState.Enabled)?.actualMode
                ?.let(::notificationModeText)
            RfScanEvent(
                kind = "notificationStateChanged",
                commandId = event.commandId.value.toString(),
                scanSessionId = null,
                peripheralId = null,
                connectionId = event.connectionId.value.toString(),
                connectionState = null,
                gattSessionId = event.gattSessionId.value.toString(),
                characteristicId = event.characteristicId.value.toString(),
                subscriptionId = event.subscriptionId.value.toString(),
                gattServices = null,
                value = null,
                notificationState = notificationState,
                actualNotificationMode = actualMode,
                rssi = null,
                localName = null,
                advertisedServices = null,
                adapterState = null,
                scanAuthorization = null,
                error = null,
            )
        }

        is BleEvent.NotificationReceived -> RfScanEvent(
            kind = "notificationReceived",
            commandId = null,
            scanSessionId = null,
            peripheralId = null,
            connectionId = event.connectionId.value.toString(),
            connectionState = null,
            gattSessionId = event.gattSessionId.value.toString(),
            characteristicId = event.characteristicId.value.toString(),
            subscriptionId = event.subscriptionId.value.toString(),
            gattServices = null,
            value = event.value.toArrayBuffer(),
            notificationState = null,
            actualNotificationMode = null,
            rssi = null,
            localName = null,
            advertisedServices = null,
            adapterState = null,
            scanAuthorization = null,
            error = null,
        )

        is BleEvent.RssiRead -> RfScanEvent(
            kind = "rssiRead",
            commandId = event.commandId.value.toString(),
            scanSessionId = null,
            peripheralId = null,
            connectionId = event.connectionId.value.toString(),
            connectionState = null,
            gattSessionId = null,
            characteristicId = null,
            subscriptionId = null,
            gattServices = null,
            value = null,
            notificationState = null,
            actualNotificationMode = null,
            rssi = event.rssi.toDouble(),
            localName = null,
            advertisedServices = null,
            adapterState = null,
            scanAuthorization = null,
            error = null,
        )

        is BleEvent.ScanStopped -> {
            val commandId = when (val result = event.result) {
                is ScanStopResult.Ok -> (result.reason as? ScanStopReason.Requested)
                    ?.commandId?.value?.toString()

                is ScanStopResult.Err -> null
            }
            val error = (event.result as? ScanStopResult.Err)?.error?.toNitroError()
            RfScanEvent(
                kind = "scanStopped",
                commandId = commandId,
                scanSessionId = event.scanId.value.toString(),
                peripheralId = null,
                connectionId = null,
                connectionState = null,
                gattSessionId = null,
                characteristicId = null,
                subscriptionId = null,
                gattServices = null,
                value = null,
                notificationState = null,
                actualNotificationMode = null,
                rssi = null,
                localName = null,
                advertisedServices = null,
                adapterState = null,
                scanAuthorization = null,
                error = error,
            )
        }

        is BleEvent.CommandCompleted -> RfScanEvent(
            kind = "commandCompleted",
            commandId = event.commandId.value.toString(),
            scanSessionId = null,
            peripheralId = null,
            connectionId = null,
            connectionState = null,
            gattSessionId = null,
            characteristicId = null,
            subscriptionId = null,
            gattServices = null,
            value = null,
            notificationState = null,
            actualNotificationMode = null,
            rssi = null,
            localName = null,
            advertisedServices = null,
            adapterState = null,
            scanAuthorization = null,
            error = event.error?.toNitroError(),
        )

        is BleEvent.BackendError -> RfScanEvent(
            kind = "backendError",
            commandId = null,
            scanSessionId = null,
            peripheralId = null,
            connectionId = null,
            connectionState = null,
            gattSessionId = null,
            characteristicId = null,
            subscriptionId = null,
            gattServices = null,
            value = null,
            notificationState = null,
            actualNotificationMode = null,
            rssi = null,
            localName = null,
            advertisedServices = null,
            adapterState = null,
            scanAuthorization = null,
            error = event.error.toNitroError(),
        )

        else -> null
    }

    private fun gattService(service: dev.blehub.GattService): RfGattService = RfGattService(
        id = service.id.value.toString(),
        uuid = service.uuid.value,
        primary = service.primary,
        characteristics = service.characteristics.map(::gattCharacteristic).toTypedArray(),
    )

    private fun gattCharacteristic(
        characteristic: dev.blehub.GattCharacteristic,
    ): RfGattCharacteristic = RfGattCharacteristic(
        id = characteristic.id.value.toString(),
        uuid = characteristic.uuid.value,
        readable = characteristic.properties.readable,
        writableWithResponse = characteristic.properties.writableWithResponse,
        writableWithoutResponse = characteristic.properties.writableWithoutResponse,
        notifiable = characteristic.properties.notifiable,
        indicatable = characteristic.properties.indicatable,
        descriptors = characteristic.descriptors.map { descriptor ->
            RfGattDescriptor(descriptor.id.value.toString(), descriptor.uuid.value)
        }.toTypedArray(),
    )

    private fun connectionStateText(state: ConnectionState): String = when (state) {
        ConnectionState.CONNECTING -> "Connecting"
        ConnectionState.CONNECTED -> "Connected"
        ConnectionState.DISCONNECTING -> "Disconnecting"
        ConnectionState.DISCONNECTED -> "Disconnected"
    }

    private fun notificationModeText(mode: NotificationMode): String = when (mode) {
        NotificationMode.NOTIFICATION -> "Notification"
        NotificationMode.INDICATION -> "Indication"
    }

    private fun parseIds(vararg values: String): ULongArray? = values.map {
        parseCanonicalU64(it) ?: return null
    }.toULongArray()

    private fun ByteArray.toArrayBuffer(): ArrayBuffer = ArrayBuffer.allocate(size).also {
        it.getBuffer(false).put(this)
    }

    private fun ArrayBuffer.toByteArray(): ByteArray {
        val buffer = getBuffer(false)
        return ByteArray(buffer.remaining()).also(buffer::get)
    }

    private fun dispatch(action: (BleHub) -> Unit): DispatchError? {
        val currentHub = synchronized(lock) {
            if (isClosed) {
                return DispatchError(DispatchErrorCategory.CLOSED, "BleHub probe is closed", null)
            }
            hub ?: return DispatchError(
                DispatchErrorCategory.INVALID_STATE,
                "BleHub probe is not initialized",
                null,
            )
        }
        return try {
            action(currentHub)
            null
        } catch (error: CommandDispatchException) {
            error.toNitroError()
        } catch (error: BleHubException) {
            DispatchError(DispatchErrorCategory.FFI_CONTRACT, error.detailText(), null)
        } catch (error: Throwable) {
            DispatchError(
                DispatchErrorCategory.FFI_CONTRACT,
                error.message ?: error::class.java.simpleName,
                null,
            )
        }
    }

    private fun invalidRequest(detail: String): DispatchError =
        DispatchError(DispatchErrorCategory.INVALID_REQUEST, detail, null)

    private fun adapterStateText(state: dev.blehub.AdapterState): String = when (state) {
        dev.blehub.AdapterState.UNKNOWN -> "Unknown"
        dev.blehub.AdapterState.UNAVAILABLE -> "Unavailable"
        dev.blehub.AdapterState.POWERED_OFF -> "PoweredOff"
        dev.blehub.AdapterState.POWERED_ON -> "PoweredOn"
    }

    private fun authorizationText(state: dev.blehub.AuthorizationState): String = when (state) {
        dev.blehub.AuthorizationState.NOT_REQUIRED -> "NotRequired"
        dev.blehub.AuthorizationState.NOT_DETERMINED -> "NotDetermined"
        dev.blehub.AuthorizationState.DENIED -> "Denied"
        dev.blehub.AuthorizationState.RESTRICTED -> "Restricted"
        dev.blehub.AuthorizationState.AUTHORIZED -> "Authorized"
    }

    private fun parseCanonicalU64(text: String): ULong? {
        if (text == "0") return 0u
        if (text.isEmpty() || text[0] == '0' || text.any { it !in '0'..'9' }) return null
        return text.toULongOrNull()
    }

    private data class CloseState(
        val promise: Promise<Unit>,
        val hub: BleHub?,
        val backendThread: HandlerThread?,
        val deliveryThread: HandlerThread?,
    )

    companion object {
        /** Private adapter test seam; it is not part of the Nitro/JS contract. */
        @JvmStatic
        @androidx.annotation.VisibleForTesting
        fun forDisposeWaitTest(gate: CountDownLatch): HybridBleHubProbe {
            return HybridBleHubProbe(gate)
        }

        private const val BACKEND_THREAD_JOIN_TIMEOUT_MS = 5_000L
        const val DELIVERY_THREAD_JOIN_TIMEOUT_MS = 5_000L
        const val EVENT_HANDOFF_CAPACITY = 256
        const val TAG = "BleHubNitroProbe"
    }
}

/** A foreign sink whose only purpose is to project the production event back to Nitro. */
private class CompletionSink(
    private val handoff: AndroidEventHandoff<BleEvent>,
) : BleEventSink {
    override fun onEvent(event: BleEvent) {
        handoff.submit(event)
    }
}

/**
 * Deterministic backend used by the internal integration. It exercises the production foreign
 * backend and ingress interfaces without claiming platform or RF behavior.
 */
private class DeterministicBackend(
    private val handler: Handler,
    private val disposeGate: CountDownLatch,
) : BleBackend {
    private val lock = Any()
    private var ingress: BackendEventIngressHandle? = null
    private var isDisposed = false

    override fun capabilities(): BackendCapabilities = BackendCapabilities(
        platform = RustPlatform.TEST,
        contractVersion = blehubContractVersion(),
        supportsDuplicateScanResults = false,
        supportsConnectableFlag = false,
        supportsReadRssi = false,
        supportsRequiredNotification = false,
        supportsRequiredIndication = false,
        reportsActualNotificationMode = false,
    )

    override fun bindEventIngress(ingress: BackendEventIngressHandle) {
        synchronized(lock) {
            check(!isDisposed) { "deterministic backend is disposed" }
            check(this.ingress == null) { "event ingress was already bound" }
            this.ingress = ingress
        }
    }

    override fun initialize(command: InitializeCommand) {
        postCompletion(command.commandId)
    }

    override fun startScan(command: dev.blehub.StartScanCommand) = Unit

    override fun stopScan(command: dev.blehub.StopScanCommand) = Unit

    override fun connect(command: dev.blehub.ConnectCommand) = Unit

    override fun disconnect(command: dev.blehub.DisconnectCommand) = Unit

    override fun discoverGatt(command: dev.blehub.DiscoverGattCommand) = Unit

    override fun readCharacteristic(command: dev.blehub.ReadCharacteristicCommand) = Unit

    override fun writeCharacteristic(command: dev.blehub.WriteCharacteristicCommand) = Unit

    override fun setNotifications(command: dev.blehub.SetNotificationsCommand) = Unit

    override fun readRssi(command: dev.blehub.ReadRssiCommand) = Unit

    override fun dispose() {
        check(disposeGate.await(5, TimeUnit.SECONDS)) {
            "deterministic dispose gate was not released"
        }
        val root = synchronized(lock) {
            if (isDisposed) {
                null
            } else {
                isDisposed = true
                ingress.also { ingress = null }
            }
        }
        root?.destroy()
    }

    private fun postCompletion(commandId: CommandId) {
        val callbackIngress = synchronized(lock) {
            if (isDisposed) {
                null
            } else {
                ingress?.let { root ->
                    BackendEventIngressHandle(UniffiWithHandle, root.uniffiCloneHandle())
                }
            }
        } ?: return

        val posted = handler.post {
            try {
                val shouldDeliver = synchronized(lock) { !isDisposed }
                if (shouldDeliver) {
                    callbackIngress.push(BleEvent.CommandCompleted(commandId, null))
                }
            } finally {
                callbackIngress.destroy()
            }
        }
        if (!posted) callbackIngress.destroy()
    }
}

private fun CommandDispatchException.toNitroError(): DispatchError = when (this) {
    is CommandDispatchException.Closed -> DispatchError(
        DispatchErrorCategory.CLOSED,
        "BleHub is closed",
        null,
    )

    is CommandDispatchException.InvalidRequest -> DispatchError(
        DispatchErrorCategory.INVALID_REQUEST,
        detail,
        null,
    )

    is CommandDispatchException.InvalidState -> DispatchError(
        DispatchErrorCategory.INVALID_STATE,
        detail,
        null,
    )

    is CommandDispatchException.BackendUnavailable -> DispatchError(
        DispatchErrorCategory.BACKEND_UNAVAILABLE,
        detail,
        platformDetails?.toNitroDetails(),
    )

    is CommandDispatchException.FfiContract -> DispatchError(
        DispatchErrorCategory.FFI_CONTRACT,
        detail,
        null,
    )
}

private fun BleHubException.detailText(): String = when (this) {
    is BleHubException.BackendAssembly -> detail
    is BleHubException.BackendCleanup -> detail
    is BleHubException.ResourceBusy -> detail
    is BleHubException.InvalidConfiguration -> "$field: $detail"
    is BleHubException.FfiContract -> detail
}

private fun BleEventError.toNitroError(): EventError = EventError(
    category = when (category) {
        BleErrorCategory.PERMISSION -> EventErrorCategory.PERMISSION
        BleErrorCategory.ADAPTER_UNAVAILABLE -> EventErrorCategory.ADAPTER_UNAVAILABLE
        BleErrorCategory.INVALID_STATE -> EventErrorCategory.INVALID_STATE
        BleErrorCategory.NOT_FOUND -> EventErrorCategory.NOT_FOUND
        BleErrorCategory.TIMEOUT -> EventErrorCategory.TIMEOUT
        BleErrorCategory.CANCELLED -> EventErrorCategory.CANCELLED
        BleErrorCategory.CONNECTION -> EventErrorCategory.CONNECTION
        BleErrorCategory.GATT -> EventErrorCategory.GATT
        BleErrorCategory.PROTOCOL -> EventErrorCategory.PROTOCOL
        BleErrorCategory.NOT_SUPPORTED -> EventErrorCategory.NOT_SUPPORTED
        BleErrorCategory.BACKEND -> EventErrorCategory.BACKEND
        BleErrorCategory.INTERNAL -> EventErrorCategory.INTERNAL
    },
    message = message,
    platformDetails = platformDetails?.toNitroDetails(),
)

private fun RustPlatformErrorDetails.toNitroDetails(): NitroPlatformErrorDetails = NitroPlatformErrorDetails(
    platform = when (platform) {
        RustPlatform.WINDOWS -> NitroPlatform.PLATFORM_WINDOWS
        RustPlatform.ANDROID -> NitroPlatform.PLATFORM_ANDROID
        RustPlatform.APPLE -> NitroPlatform.PLATFORM_APPLE
        RustPlatform.LINUX -> NitroPlatform.PLATFORM_LINUX
        RustPlatform.TEST -> NitroPlatform.PLATFORM_TEST
    },
    code = code,
    name = name,
    message = message,
)
