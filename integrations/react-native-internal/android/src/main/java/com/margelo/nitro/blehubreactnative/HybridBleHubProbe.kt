package com.margelo.nitro.blehubreactnative

import android.os.Handler
import android.os.HandlerThread
import android.util.Log
import com.facebook.react.bridge.ReactApplicationContext
import com.margelo.nitro.core.ArrayBuffer
import com.margelo.nitro.NitroModules
import com.margelo.nitro.core.Promise
import dev.blehub.BackendCapabilities
import dev.blehub.BackendEventIngressHandle
import dev.blehub.BleBackend
import dev.blehub.BleErrorCategory
import dev.blehub.BleEvent
import dev.blehub.BleEventError

import dev.blehub.BluetoothUuid
import dev.blehub.CancelOperationRequest

import dev.blehub.CommandId

import dev.blehub.ConnectionState
import dev.blehub.ConnectionStateResult


import dev.blehub.GattCharacteristicId
import dev.blehub.GattSessionId
import dev.blehub.InitializeCommand

import dev.blehub.NotificationMode
import dev.blehub.NotificationModeRequest
import dev.blehub.NotificationState
import dev.blehub.ReadCharacteristicCommand

import dev.blehub.ScanOptions
import dev.blehub.ScanSessionId
import dev.blehub.ScanStopReason
import dev.blehub.ScanStopResult



import dev.blehub.SubscriptionId

import dev.blehub.WriteType
import dev.blehub.Platform as RustPlatform
import dev.blehub.PlatformErrorDetails as RustPlatformErrorDetails
import dev.blehub.blehubContractVersion
import dev.blehub.UniffiWithHandle
import dev.blehub.uniffiEnsureInitialized
import com.blehub.reactnative.bleng.AndroidPlatformSessionFactory
import com.blehub.reactnative.bleng.AndroidRustSupervisorOwner
import dev.bleng.Bleng

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Deferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.async
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking

import java.util.UUID
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import kotlin.concurrent.thread

/**
 * Internal Nitro probe backed by the production UniFFI BLeng object and Android backend.
 *
 * This remains a private integration probe rather than the future public RN API. Its
 * backend lifecycle and RF command projection are backed by the production Android backend;
 * this remains a private integration probe until the public RN API is designed.
 */
/**
 * Selects the owner construction path for this private probe.
 *
 * Production/default probe instances always use the Rust Supervisor. The direct BLeng path exists
 * only for deterministic disposal/resource tests and must never become a runtime feature switch.
 */
private enum class ProbeLifecycleMode {
    RustSupervisor,
    DirectBlengForTesting,
}

class HybridBleHubProbe private constructor(
    private val deterministicDisposeGate: CountDownLatch?,
    engineContext: ReactApplicationContext?,
    private val lateSubmitForTesting: ((AndroidEventHandoff<BleEvent>, BleEvent) -> AndroidEventHandoff.SubmitResult)? = null,
    private val disposeFailureForTesting: Throwable? = null,
    private val snapshotBackendReadForTesting: (() -> Unit)? = null,
    private val lifecycleMode: ProbeLifecycleMode = ProbeLifecycleMode.RustSupervisor,
) : HybridBleHubProbeSpec() {
    constructor() : this(null, NitroModules.applicationContext)


    private val lock = Any()
    private var completionListener: ((CompletionEvent) -> Unit)? = null
    private var rfScanListener: ((RfScanEvent) -> Unit)? = null
    private var isClosed = false
    private var owner: Bleng? = null
    private var ownerReady = false
    private var assembling = false
    private var assemblyStarted = false
    private var assemblyJob: Deferred<Unit>? = null
    private var operationAdapter: AndroidBlengOperationAdapter? = null
    private var supervisorOwner: AndroidRustSupervisorOwner? = null
    private val operationScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var eventReader: Deferred<Result<Unit>>? = null

    // The exact backend supplied to this BLeng owner, retained through post-dispose accounting.
    private var androidBackend: dev.blehub.android.AndroidBleBackend? = null
    private var deterministicBackend: DeterministicBackend? = null
    private var closedResourceCounts: Map<String, Int>? = null
    private var closeFailure: Throwable? = null
    private var backendThread: HandlerThread? = null
    private var deliveryThread: HandlerThread? = null
    private var eventHandoff: AndroidEventHandoff<BleEvent>? = null
    private var closePromise: Promise<Unit>? = null
    private val probeId = UUID.randomUUID().toString()
    private var probeSession: AndroidBleProbeSession? = null
    private var pendingRead: ReadCharacteristicCommand? = null
    private var closeStartedByEngine = false
    private var engineBoundaryFailure: Throwable? = null
    private val lifecycleOwner = BleHubProbeLifecycleOwners.register(engineContext)

    init {
        lifecycleOwner.activate { closeInternal(engineInvalidation = true) }
    }

    override fun awaitPendingReloadReady(runId: String, generation: Double): Promise<Unit> {
        val promise = Promise<Unit>()
        val config = lifecycleOwner.pendingConfig
        if (config == null) {
            promise.reject(IllegalStateException("probe has no native pending-read engine configuration"))
        } else {
            config.session.awaitReady(config, runId, generation).whenComplete { _, failure ->
                if (failure == null) promise.resolve(Unit) else promise.reject(failure)
            }
        }
        return promise
    }

    override fun initialize(commandId: String): DispatchError? {
        val externalId = parseCanonicalU64(commandId)
            ?: return invalidRequest("commandId must be a canonical unsigned decimal u64")

        if (lifecycleMode == ProbeLifecycleMode.RustSupervisor) {
            return initializeThroughRustSupervisor(externalId)
        }

        val currentOwner = synchronized(lock) {
            if (isClosed) return@synchronized null
            owner ?: createOwnerLocked()
        } ?: return DispatchError(DispatchErrorCategory.CLOSED, "BleHub probe is closed", null)

        val adapter = synchronized(lock) { operationAdapter }
            ?: return backendUnavailable("BLeng operation adapter is unavailable")
        if (!synchronized(lock) { ownerReady }) {
            synchronized(lock) {
                if (assembling) {
                    return backendUnavailable("BLeng backend assembly is still in progress")
                }
                val backend = androidBackend ?: deterministicBackend
                    ?: return backendUnavailable("BLE backend is unavailable")
                assembling = true
                assemblyJob = operationScope.async {
                    synchronized(lock) { assemblyStarted = true }
                    try {
                        currentOwner.assembleBackend(backend)
                        synchronized(lock) { ownerReady = true }
                        adapter.initialize(externalId)?.let { handleAsyncDispatchFailure(it) }
                    } catch (error: Throwable) {
                        handleAsyncDispatchFailure(error)
                        throw error
                    } finally {
                        synchronized(lock) { assembling = false }
                    }
                }
            }
            return null
        }
        return adapter.initialize(externalId)
    }

    private fun initializeThroughRustSupervisor(externalId: ULong): DispatchError? {
        val supervisor = synchronized(lock) {
            if (isClosed) return@synchronized null
            supervisorOwner ?: createSupervisorOwnerLocked()
        } ?: return DispatchError(DispatchErrorCategory.CLOSED, "BleHub probe is closed", null)
        val adapter = synchronized(lock) { operationAdapter }
            ?: return backendUnavailable("BLeng operation adapter is unavailable")
        if (!synchronized(lock) { ownerReady }) {
            synchronized(lock) {
                if (assembling) {
                    return backendUnavailable("Rust Supervisor startup is still in progress")
                }
                assembling = true
                assemblyJob = operationScope.async {
                    adapter.markInitializationPending(externalId)
                    try {
                        val start = supervisor.start()
                        adapter.completeInitialization(CommandId(start.initializationCommandId))
                        synchronized(lock) { ownerReady = true }
                    } catch (error: Throwable) {
                        handleAsyncDispatchFailure(error)
                        throw error
                    } finally {
                        synchronized(lock) { assembling = false }
                    }
                }
            }
            return null
        }
        return null
    }

    private fun createSupervisorOwnerLocked(): AndroidRustSupervisorOwner {
        uniffiEnsureInitialized()
        val context = lifecycleOwner.context
            ?: error("probe's captured ReactApplicationContext is unavailable")
        val deliveryThread = HandlerThread("BleHubProbe-supervisor-delivery")
        deliveryThread.start()
        this.deliveryThread = deliveryThread
        val handoff = AndroidEventHandoff(
            handler = Handler(deliveryThread.looper),
            capacity = EVENT_HANDOFF_CAPACITY,
            deliver = ::deliver,
            onFailure = ::handleEventHandoffFailure,
        )
        eventHandoff = handoff
        val adapter = AndroidBlengOperationAdapter(
            owner = null,
            scope = operationScope,
            onObservationFailure = ::handleAsyncDispatchFailure,
        )
        operationAdapter = adapter
        val factory = AndroidProbePlatformSessionFactory(
            context = context,
            queueLabel = "BleHubProbe",
            eventSink = { event -> handoff.submit(event) },
            ownerSink = { attached ->
                synchronized(lock) {
                    owner = attached
                    adapter.attach(attached)
                }
            },
        )
        return AndroidRustSupervisorOwner(
            context = context,
            queueLabel = "BleHubProbe",
            sessionFactory = factory,
        ).also { supervisorOwner = it }
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
        return typedDispatch { adapter ->
            adapter.startScan(
                command,
                ScanOptions(
                    serviceUuids = serviceUuids,
                    namePrefix = null,
                    companyId = null,
                    allowDuplicates = false,
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
        return typedDispatch { adapter -> adapter.stopScan(command, ScanSessionId(scan)) }
    }

    override fun connectRfPeripheral(commandId: String, connectionId: String, peripheralId: String): DispatchError? {
        val command = parseIds(commandId, connectionId, peripheralId)
            ?: return invalidRequest("commandId, connectionId and peripheralId must be canonical unsigned decimal u64 values")
        return typedDispatch { adapter ->
            adapter.connect(command[0], dev.blehub.PeripheralId(command[2]))
        }
    }

    override fun disconnectRfPeripheral(commandId: String, connectionId: String): DispatchError? {
        val command = parseIds(commandId, connectionId)
            ?: return invalidRequest("commandId and connectionId must be canonical unsigned decimal u64 values")
        return typedDispatch { adapter ->
            adapter.disconnect(command[0], dev.blehub.ConnectionId(command[1]))
        }
    }

    override fun readRfRssi(commandId: String, connectionId: String): DispatchError? {
        val command = parseIds(commandId, connectionId)
            ?: return invalidRequest("commandId and connectionId must be canonical unsigned decimal u64 values")
        return typedDispatch { adapter ->
            adapter.readRssi(command[0], dev.blehub.ConnectionId(command[1]))
        }
    }

    override fun discoverRfGatt(commandId: String, connectionId: String, gattSessionId: String): DispatchError? {
        val command = parseIds(commandId, connectionId, gattSessionId)
            ?: return invalidRequest("commandId, connectionId and gattSessionId must be canonical unsigned decimal u64 values")
        return typedDispatch { adapter ->
            adapter.discoverGatt(command[0], dev.blehub.ConnectionId(command[1]))
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
        // Linearize acceptance/diagnostic capture with engine close. The marker is emitted
        // only after the typed Rust operation was accepted, never at native entry.
        return synchronized(lock) {
            val error = typedDispatch { adapter ->
                adapter.readCharacteristic(
                    command[0],
                    dev.blehub.ConnectionId(command[1]),
                    GattSessionId(command[2]),
                    GattCharacteristicId(command[3]),
                )
            }
            if (error == null && lifecycleOwner.pendingConfig?.generation == 1 &&
                command[0] == 12008uL && command[1] == 12004uL && command[2] == 12006uL
            ) {
                check(pendingRead == null) { "pending diagnostic read was already accepted" }
                val read = ReadCharacteristicCommand(
                    commandId = CommandId(command[0]),
                    connectionId = dev.blehub.ConnectionId(command[1]),
                    gattSessionId = GattSessionId(command[2]),
                    characteristicId = GattCharacteristicId(command[3]),
                )
                pendingRead = read
                pendingMarker("read-accepted", read)
            }
            error
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
        return typedDispatch { adapter ->
            adapter.writeCharacteristic(
                command[0],
                dev.blehub.ConnectionId(command[1]),
                GattSessionId(command[2]),
                GattCharacteristicId(command[3]),
                value.toByteArray(),
                when (writeType) {
                    RfWriteType.WITH_RESPONSE -> WriteType.WITH_RESPONSE
                    RfWriteType.WITHOUT_RESPONSE -> WriteType.WITHOUT_RESPONSE
                },
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
        return typedDispatch { adapter ->
            adapter.setNotifications(
                command[0],
                dev.blehub.ConnectionId(command[1]),
                GattSessionId(command[2]),
                SubscriptionId(command[3]),
                GattCharacteristicId(command[4]),
                enabled,
                when (mode) {
                    RfNotificationMode.PLATFORM_DEFAULT -> NotificationModeRequest.PLATFORM_DEFAULT
                    RfNotificationMode.REQUIRE_NOTIFICATION -> NotificationModeRequest.REQUIRE_NOTIFICATION
                    RfNotificationMode.REQUIRE_INDICATION -> NotificationModeRequest.REQUIRE_INDICATION
                },
            )
        }
    }

    override fun cancelOperation(targetCommandId: String): DispatchError? {
        val command = parseCanonicalU64(targetCommandId)
            ?: return invalidRequest("targetCommandId must be a canonical unsigned decimal u64")
        val adapter = synchronized(lock) { operationAdapter }
            ?: return backendUnavailable("BLeng operation adapter is unavailable")
        return adapter.cancel(command)
    }

    override fun roundTrip(sample: TransportSample): TransportSample {
        return TransportSample(sample.id, sample.bytes.copyBuffer(), sample.error)
    }

    /** Logical native roots, not a JS queue-drain guarantee; temporary diagnostic/close workers are excluded. */
    override fun snapshotRfResources(): Promise<RfResourceSnapshot> {
        val promise = Promise<RfResourceSnapshot>()
        val immediate = runCatching {
            synchronized(lock) {
                if (isClosed) {
                    return@synchronized closedResourceCounts ?: throw (
                            closeFailure ?: IllegalStateException("probe close has not completed successfully")
                            )
                }
                if (supervisorOwner != null) {
                    throw IllegalStateException(
                        "live resource inspection is unavailable until Rust Supervisor shutdown"
                    )
                }
                val currentThread = backendThread
                if (currentThread == null) {
                    // Inspection must not lazily create a BLeng owner or an owned worker.
                    return@synchronized snapshotCountsLocked(emptyBackendCounts())
                }
                val deliveryHandler = Handler(checkNotNull(deliveryThread).looper)
                check(Handler(currentThread.looper).post {
                    try {
                        // Already on the backend owner: its snapshot cannot synchronously wait
                        // for this queue while holding the probe lock.
                        val native = synchronized(lock) {
                            check(!isClosed) { "probe closed before resource inspection" }
                            androidBackend?.snapshotRfResources()
                                ?: deterministicBackend?.snapshotRfResources()
                                ?: error("backend thread has no retained backend")
                        }
                        snapshotBackendReadForTesting?.invoke()
                        // Always enqueue, even for delivery re-entry. Earlier native deliveries
                        // must return through handoff's finally decrement before probe accounting.
                        // Later ingress may still contribute real nonzero pending counts.
                        check(deliveryHandler.post {
                            val counts = runCatching {
                                synchronized(lock) {
                                    check(!isClosed) { "probe closed before delivery resource inspection" }
                                    snapshotCountsLocked(native)
                                }
                            }
                            settleRfResourceSnapshot(promise, counts)
                        }) { "delivery handler rejected resource inspection" }
                    } catch (failure: Throwable) {
                        settleRfResourceSnapshot(promise, Result.failure(failure))
                    }
                }) { "backend handler rejected resource inspection" }
                null
            }
        }
        immediate.fold(
            onSuccess = { counts ->
                if (counts != null) settleRfResourceSnapshot(promise, Result.success(counts))
            },
            onFailure = { settleRfResourceSnapshot(promise, Result.failure(it)) },
        )
        return promise
    }

    private fun settleRfResourceSnapshot(
        promise: Promise<RfResourceSnapshot>,
        counts: Result<Map<String, Int>>,
    ) {
        // Native Promise listeners can synchronously re-enter and wait for close. Never
        // settle under the probe lock or on either worker that close must drain/join.
        thread(name = "BleHubProbe-snapshot") {
            runCatching { counts.getOrThrow().toRfResourceSnapshot() }.fold(
                onSuccess = { promise.resolve(it) },
                onFailure = { promise.reject(it) },
            )
        }
    }

    private fun snapshotCountsLocked(native: Map<String, Int>): Map<String, Int> = native + linkedMapOf(
        "hubRoots" to if (owner != null) 1 else 0,
        "handoffRoots" to if (eventHandoff != null) 1 else 0,
        "handoffPending" to (eventHandoff?.pendingCount() ?: 0),
        "listeners" to listOfNotNull(completionListener, rfScanListener).size,
        "workerThreads" to listOfNotNull(backendThread, deliveryThread).count { it.isAlive },
        "lifecycleOwners" to BleHubProbeLifecycleOwners.membershipCount(lifecycleOwner),
    )

    override fun close(): Promise<Unit> = closeInternal(engineInvalidation = false)

    private fun closeInternal(engineInvalidation: Boolean): Promise<Unit> {
        val (promise, currentOwner, currentEventReader, currentAssemblyJob, currentSupervisor, currentBackendThread,
            currentDeliveryThread, oldHandoff, read, currentAndroidBackend, currentDeterministicBackend) =
            synchronized(lock) {
                closePromise?.let {
                    if (engineInvalidation && lifecycleOwner.pendingConfig?.generation == 1 && !closeStartedByEngine) {
                        engineBoundaryFailure =
                            IllegalStateException("old owner close did not originate in beforeDestroy")
                        pendingFailure(engineBoundaryFailure!!)
                    }
                    return it
                }
                closeStartedByEngine = engineInvalidation
                if (!engineInvalidation && lifecycleOwner.pendingConfig?.generation == 1) {
                    engineBoundaryFailure = IllegalStateException("old owner close did not originate in beforeDestroy")
                }
                if (engineInvalidation && lifecycleOwner.pendingConfig?.generation == 1) {
                    if (pendingRead == null) {
                        engineBoundaryFailure = IllegalStateException("beforeDestroy had no accepted pending read")
                        pendingFailure(engineBoundaryFailure!!)
                    } else {
                        pendingMarker("engine-invalidation-close-started", pendingRead!!)
                    }
                }
                val promise = Promise<Unit>()
                closePromise = promise
                isClosed = true
                completionListener = null
                rfScanListener = null
                val handoff = eventHandoff
                CloseState(
                    promise = promise,
                    owner = owner,
                    eventReader = eventReader,
                    assemblyJob = assemblyJob,
                    supervisor = supervisorOwner,
                    backendThread = backendThread,
                    deliveryThread = deliveryThread,
                    oldHandoff = handoff,
                    read = pendingRead,
                    androidBackend = androidBackend,
                    deterministicBackend = deterministicBackend,
                )
            }

        thread(name = "BleHubProbe-close") {
            var failure: Throwable? = null
            var finalBackendCounts: Map<String, Int>? = null
            fun cleanup(action: () -> Unit) {
                try {
                    action()
                } catch (error: Throwable) {
                    if (failure == null) failure = error
                    else if (failure !== error) failure!!.addSuppressed(error)
                }
            }
            // Every cleanup stage is attempted even if disposal or synthetic verification fails.
            if (currentSupervisor != null) {
                cleanup {
                    runBlocking { currentAssemblyJob?.await() }
                }
                cleanup {
                    oldHandoff?.close()
                    val resources = runBlocking { currentSupervisor.close() }
                    finalBackendCounts = resources.associate { it.name to it.count.toInt() }
                    if (read != null) pendingMarker("dispose-returned", read)
                }
                cleanup { currentDeliveryThread?.quitSafely() }
                cleanup {
                    currentDeliveryThread?.join(5_000L)
                    check(currentDeliveryThread?.isAlive != true) {
                        "supervisor delivery thread did not stop"
                    }
                }
            } else if (probeSession != null) {
                cleanup {
                    finalBackendCounts = runBlocking { checkNotNull(probeSession).retire() }
                    if (read != null) pendingMarker("dispose-returned", read)
                }
            } else if (currentOwner != null) {
                // Account for an owner assembled before the production session was activated.
                cleanup {
                    runBlocking { currentOwner.shutdown() }
                    if (read != null) pendingMarker("dispose-returned", read)
                }
                cleanup {
                    finalBackendCounts = currentAndroidBackend?.snapshotRfResources()
                        ?: currentDeterministicBackend?.snapshotRfResources()
                                ?: error("BLeng owner has no retained backend for final accounting")
                }
                cleanup {
                    runBlocking { currentEventReader?.await() }
                }
            } else {
                // Also account for a backend allocated before Hub construction failed.
                cleanup {
                    currentAndroidBackend?.dispose()
                    currentDeterministicBackend?.dispose()
                    finalBackendCounts = currentAndroidBackend?.snapshotRfResources()
                        ?: currentDeterministicBackend?.snapshotRfResources()
                                ?: emptyBackendCounts()
                }
            }
            synchronized(lock) {
                engineBoundaryFailure?.let { error ->
                    if (failure == null) failure = error
                    else if (failure !== error) failure!!.addSuppressed(error)
                }
            }
            if (failure == null && read != null && closeStartedByEngine) {
                pendingMarker("engine-close-complete", read)
            } else if (failure != null) {
                pendingFailure(failure!!)
            }
            val threadsStopped = currentBackendThread?.isAlive != true && currentDeliveryThread?.isAlive != true
            cleanup {
                check(threadsStopped) { "owned worker threads are still alive" }
                check(oldHandoff?.pendingCount() == null || oldHandoff.pendingCount() == 0) {
                    "closed handoff still owns accepted delivery jobs"
                }
                val native = checkNotNull(finalBackendCounts) { "final backend accounting was not captured" }
                check(native.values.all { it == 0 }) { "disposed backend still owns resources: $native" }
            }
            operationAdapter?.close()
            operationScope.cancel()
            lifecycleOwner.complete(failure, releaseContext = threadsStopped)
            if (failure == null) cleanup {
                synchronized(lock) {
                    check(BleHubProbeLifecycleOwners.membershipCount(lifecycleOwner) == 0) {
                        "closed probe remains registered with its React context"
                    }
                    // These roots have now completed disposal/close/drain/join, not merely
                    // received a close request. Failed close retains roots and rejects inspection.
                    owner = null
                    ownerReady = false
                    assembling = false
                    assemblyStarted = false
                    assemblyJob = null
                    operationAdapter = null
                    supervisorOwner = null
                    eventReader = null
                    probeSession = null
                    androidBackend = null
                    deterministicBackend = null
                    backendThread = null
                    deliveryThread = null
                    eventHandoff = null
                    val counts = snapshotCountsLocked(checkNotNull(finalBackendCounts))
                    counts.toRfResourceSnapshot() // Validate the complete numeric projection before caching.
                    check(counts.values.all { it == 0 }) { "closed probe still owns resources: $counts" }
                    closedResourceCounts = counts.toMap()
                }
            }
            synchronized(lock) { closeFailure = failure }
            if (failure == null) promise.resolve(Unit) else promise.reject(failure!!)
            if (!threadsStopped) {
                // The bounded gate has failed, not passed. Retain the old context on this
                // native cleanup worker until residual callbacks really stop; never wait on UI.
                currentBackendThread?.join()
                currentDeliveryThread?.join()
                BleHubProbeLifecycleOwners.unregister(lifecycleOwner)
            }
        }
        return promise
    }

    private fun pendingMarker(event: String, read: ReadCharacteristicCommand, suffix: String = "") {
        val config = lifecycleOwner.pendingConfig ?: return
        Log.i(
            TAG, "BLEHUB_NITRO_RUNTIME rf-pending-read-reload:native:$event:" +
                    "runId=${config.session.runId}:probeId=$probeId:commandId=${read.commandId.value}:" +
                    "connectionId=${read.connectionId.value}:gattSessionId=${read.gattSessionId.value}:" +
                    "characteristicId=${read.characteristicId.value}$suffix"
        )
    }

    private fun pendingFailure(failure: Throwable) {
        val config = lifecycleOwner.pendingConfig ?: return
        val read = pendingRead
        if (read != null) pendingMarker("native-boundary-failed", read)
        else Log.e(
            TAG, "BLEHUB_NITRO_RUNTIME rf-pending-read-reload:native:native-boundary-failed:" +
                    "runId=${config.session.runId}:probeId=$probeId"
        )
        Log.e(TAG, "pending read native boundary failed", failure)
    }

    private fun createOwnerLocked(): Bleng {
        uniffiEnsureInitialized()
        val backendThread = HandlerThread("BleHubProbe-android")
        val deliveryThread = HandlerThread("BleHubProbe-delivery")
        this.backendThread = backendThread
        this.deliveryThread = deliveryThread
        backendThread.start()
        deliveryThread.start()
        val handoff = AndroidEventHandoff(
            handler = Handler(deliveryThread.looper),
            capacity = EVENT_HANDOFF_CAPACITY,
            deliver = ::deliver,
            onFailure = ::handleEventHandoffFailure,
        )
        this.eventHandoff = handoff
        return try {
            val backend = deterministicDisposeGate?.let { gate ->
                DeterministicBackend(Handler(backendThread.looper), gate, disposeFailureForTesting)
                    .also { deterministicBackend = it }
            } ?: run {
                val context = lifecycleOwner.context
                    ?: error("probe's captured ReactApplicationContext is unavailable")
                dev.blehub.android.AndroidBleBackend(context, Handler(backendThread.looper))
                    .also { androidBackend = it }
            }
            val createdOwner = Bleng()
            val adapter = AndroidBlengOperationAdapter(
                owner = createdOwner,
                scope = operationScope,
                onObservationFailure = ::handleAsyncDispatchFailure,
            )
            owner = createdOwner
            operationAdapter = adapter
            eventReader = operationScope.async {
                try {
                    while (true) {
                        val event = createdOwner.nextEvent(300_000uL) ?: break
                        val currentHandoff = synchronized(lock) { eventHandoff } ?: break
                        if (currentHandoff.submit(event) == AndroidEventHandoff.SubmitResult.CLOSED) break
                    }
                    Result.success(Unit)
                } catch (error: Throwable) {
                    Result.failure(error)
                }
            }
            val session = AndroidBleProbeSession(
                eventReader = eventReader,
                backendThread = backendThread,
                deliveryThread = deliveryThread,
                handoff = handoff,
                awaitAssembly = {
                    synchronized(lock) { assemblyJob }?.await()
                },
                assemblyStarted = { synchronized(lock) { assemblyStarted } },
                shutdownOwner = { createdOwner.shutdown() },
                disposeUnboundBackend = {
                    androidBackend?.dispose()
                    deterministicBackend?.dispose()
                },
                snapshotBackend = {
                    androidBackend?.snapshotRfResources()
                        ?: deterministicBackend?.snapshotRfResources()
                        ?: emptyBackendCounts()
                },
                lateRetirementCheck = ::verifyLateRetirement,
            )
            probeSession = session
            createdOwner
        } catch (error: Throwable) {
            handoff.close()
            runCatching { androidBackend?.dispose() }
            runCatching { deterministicBackend?.dispose() }
            backendThread.quitSafely()
            deliveryThread.quitSafely()
            throw error
        }
    }

    private fun handleEventHandoffFailure() {
        Log.e(TAG, "Android Nitro event handoff retired; closing probe")
        close()
    }

    private fun verifyLateRetirement() {
        val read = pendingRead ?: return
        val oldHandoff = checkNotNull(eventHandoff) { "accepted read had no saved event handoff" }
        val synthetic = BleEvent.CharacteristicRead(
            read.commandId,
            read.connectionId,
            read.gattSessionId,
            read.characteristicId,
            byteArrayOf(0),
        )
        val result = lateSubmitForTesting?.invoke(oldHandoff, synthetic) ?: oldHandoff.submit(synthetic)
        check(result == AndroidEventHandoff.SubmitResult.CLOSED) {
            "saved old handoff accepted synthetic late read: $result"
        }
        pendingMarker("late-submit-rejected", read, ":source=synthetic:result=closed")
    }

    private fun deliver(event: BleEvent) {
        Log.d(TAG, "Received production event ${event::class.java.simpleName}")
        val completion = (event as? BleEvent.CommandCompleted)?.let {
            CompletionEvent(
                commandIdText(it.commandId),
                it.error?.toNitroError(),
            )
        }
        val rfEvent = try {
            projectRfEvent(event)
        } catch (error: Throwable) {
            Log.e(TAG, "Unable to project production BleEvent to Nitro", error)
            null
        }
        operationAdapter?.let { adapter ->
            (event as? BleEvent.CommandCompleted)?.let { adapter.complete(it.commandId) }
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
            commandId = event.commandId?.let(::commandIdText),
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
            commandId = commandIdText(event.commandId),
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
                commandId = event.commandId?.let(::commandIdText),
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
            commandId = commandIdText(event.commandId),
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
            commandId = commandIdText(event.commandId),
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
            commandId = commandIdText(event.commandId),
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
                commandId = commandIdText(event.commandId),
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
            commandId = commandIdText(event.commandId),
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
                    ?.commandId?.let(::commandIdText)

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
            commandId = commandIdText(event.commandId),
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

    private fun ArrayBuffer.copyBuffer(): ArrayBuffer {
        val source = getBuffer(false).duplicate()
        return ArrayBuffer.allocate(source.remaining()).also {
            it.getBuffer(false).put(source)
        }
    }

    private fun ArrayBuffer.toByteArray(): ByteArray {
        val buffer = getBuffer(false)
        return ByteArray(buffer.remaining()).also(buffer::get)
    }

    private fun typedDispatch(
        action: (AndroidBlengOperationAdapter) -> DispatchError?,
    ): DispatchError? {
        val adapter = synchronized(lock) {
            if (isClosed) {
                return DispatchError(DispatchErrorCategory.CLOSED, "BleHub probe is closed", null)
            }
            if (!ownerReady) {
                return backendUnavailable("BLeng backend is not assembled")
            }
            operationAdapter
        } ?: return backendUnavailable("BLeng operation adapter is unavailable")
        return try {
            action(adapter)
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

    private fun backendUnavailable(detail: String): DispatchError =
        DispatchError(DispatchErrorCategory.BACKEND_UNAVAILABLE, detail, null)

    private fun handleAsyncDispatchFailure(error: DispatchError) {
        Log.e(TAG, "BLeng typed operation was rejected asynchronously: ${error.detail}")
    }

    private fun handleAsyncDispatchFailure(error: Throwable) {
        Log.e(TAG, "BLeng owner assembly or operation observation failed", error)
    }

    private fun commandIdText(commandId: CommandId): String =
        operationAdapter?.externalId(commandId) ?: commandId.value.toString()

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
        val owner: Bleng?,
        val eventReader: Deferred<Result<Unit>>?,
        val assemblyJob: Deferred<Unit>?,
        val supervisor: AndroidRustSupervisorOwner?,
        val backendThread: HandlerThread?,
        val deliveryThread: HandlerThread?,
        val oldHandoff: AndroidEventHandoff<BleEvent>?,
        val read: ReadCharacteristicCommand?,
        val androidBackend: dev.blehub.android.AndroidBleBackend?,
        val deterministicBackend: DeterministicBackend?,
    )

    companion object {
        /** Private adapter test seam; it is not part of the Nitro/JS contract. */
        @JvmStatic
        @androidx.annotation.VisibleForTesting
        fun forDisposeWaitTest(gate: CountDownLatch): HybridBleHubProbe {
            return HybridBleHubProbe(gate, null, lifecycleMode = ProbeLifecycleMode.DirectBlengForTesting)
        }

        @androidx.annotation.VisibleForTesting
        fun forEngineDisposeTest(
            gate: CountDownLatch,
            context: ReactApplicationContext,
            lateSubmit: ((List<String>, AndroidEventHandoff.SubmitResult) -> AndroidEventHandoff.SubmitResult)? = null,
        ): HybridBleHubProbe = HybridBleHubProbe(gate, context, lateSubmit?.let { verify ->
            { handoff, event ->
                val read = event as BleEvent.CharacteristicRead
                val result = handoff.submit(read)
                verify(
                    listOf(
                        read.commandId.value.toString(), read.connectionId.value.toString(),
                        read.gattSessionId.value.toString(), read.characteristicId.value.toString(),
                    ), result
                )
            }
        }, lifecycleMode = ProbeLifecycleMode.DirectBlengForTesting)

        /** Ingress-only fake: tests lifecycle accounting, never BLE/RF behavior. */
        @androidx.annotation.VisibleForTesting
        fun forResourceAccountingTest(
            context: ReactApplicationContext,
            disposeFailure: Throwable? = null,
        ): HybridBleHubProbe = HybridBleHubProbe(
            CountDownLatch(0), context, null, disposeFailure, null,
            ProbeLifecycleMode.DirectBlengForTesting
        )

        /** Production backend with an explicit test context; never starts a React/JS engine. */
        @androidx.annotation.VisibleForTesting
        fun forAndroidResourceAccountingTest(
            context: ReactApplicationContext,
            snapshotBackendRead: (() -> Unit)? = null,
        ): HybridBleHubProbe = HybridBleHubProbe(
            null, context, null, null, snapshotBackendRead,
            ProbeLifecycleMode.DirectBlengForTesting
        )

        private const val BACKEND_THREAD_JOIN_TIMEOUT_MS = 5_000L
        const val DELIVERY_THREAD_JOIN_TIMEOUT_MS = 5_000L
        const val EVENT_HANDOFF_CAPACITY = 256
        const val TAG = "BleHubNitroProbe"
    }
}

// Structural absence before backend creation (or in the ingress-only deterministic fake),
// never a replacement for observing a production backend after disposal.
private fun emptyBackendCounts(): Map<String, Int> =
    AndroidPlatformSessionFactory.REQUIRED_ZERO_RESOURCE_NAMES.associateWith { 0 }

private fun Map<String, Int>.toRfResourceSnapshot(): RfResourceSnapshot {
    check(size == 17 && values.all { it >= 0 }) { "invalid RF resource counts: $this" }
    // Nonnegative Int values are integral and exactly representable by Nitro/JS numbers (< 2^53).
    return RfResourceSnapshot(
        activeScans = getValue("activeScans").toDouble(),
        connections = getValue("connections").toDouble(),
        gattAttributes = getValue("gattAttributes").toDouble(),
        subscriptions = getValue("subscriptions").toDouble(),
        pendingIo = getValue("pendingIo").toDouble(),
        watchdogs = getValue("watchdogs").toDouble(),
        callbackRoutes = getValue("callbackRoutes").toDouble(),
        ingressRoots = getValue("ingressRoots").toDouble(),
        adapterDelegates = getValue("adapterDelegates").toDouble(),
        peerCacheEntries = getValue("peerCacheEntries").toDouble(),
        backendLeases = getValue("backendLeases").toDouble(),
        hubRoots = getValue("hubRoots").toDouble(),
        handoffRoots = getValue("handoffRoots").toDouble(),
        handoffPending = getValue("handoffPending").toDouble(),
        listeners = getValue("listeners").toDouble(),
        workerThreads = getValue("workerThreads").toDouble(),
        lifecycleOwners = getValue("lifecycleOwners").toDouble(),
    )
}


/**
 * Deterministic backend used by the internal integration. It exercises the production foreign
 * backend and ingress interfaces without claiming platform or RF behavior.
 */
private class DeterministicBackend(
    private val handler: Handler,
    private val disposeGate: CountDownLatch,
    private val disposeFailure: Throwable? = null,
) : BleBackend {
    private val lock = Any()
    private var ingress: BackendEventIngressHandle? = null
    private var isDisposed = false

    /** This fake owns only an ingress binding, and never claims native BLE/RF resources. */
    fun snapshotRfResources(): Map<String, Int> = synchronized(lock) {
        emptyBackendCounts() + ("ingressRoots" to if (ingress != null) 1 else 0)
    }

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

    override fun cancelOperation(request: CancelOperationRequest) = Unit

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
        // Release the fake's resources even when deliberately exercising a failed close.
        disposeFailure?.let { throw it }
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



private fun BleEventError.toNitroError(): EventError = EventError(
    category = when (category) {
        BleErrorCategory.PERMISSION -> EventErrorCategory.PERMISSION
        BleErrorCategory.ADAPTER_UNAVAILABLE -> EventErrorCategory.ADAPTER_UNAVAILABLE
        BleErrorCategory.INVALID_STATE -> EventErrorCategory.INVALID_STATE
        BleErrorCategory.NOT_FOUND -> EventErrorCategory.NOT_FOUND
        BleErrorCategory.TIMEOUT -> EventErrorCategory.TIMEOUT
        BleErrorCategory.CANCELLED -> EventErrorCategory.CANCELLED
        BleErrorCategory.OPERATION_ABORTED -> EventErrorCategory.OPERATION_ABORTED
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
