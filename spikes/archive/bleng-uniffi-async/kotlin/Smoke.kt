import kotlinx.coroutines.async
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import uniffi.blehub.*
import uniffi.bleng.BlengForeignProbe
import uniffi.bleng.ForeignProbeException
import uniffi.blehub_bleng_uniffi_async_spike.AsyncProbe

private class ProbeBackend(private val completeInitialization: Boolean = true) : BleBackend {
    private var ingress: BackendEventIngressHandle? = null
    private var initializationCount = 0
    private var disposeCount = 0

    @Synchronized
    fun capturedIngress(): BackendEventIngressHandle = checkNotNull(ingress)

    @Synchronized
    fun initializations(): Int = initializationCount

    @Synchronized
    fun disposals(): Int = disposeCount

    override fun capabilities(): BackendCapabilities {
        check(!Thread.currentThread().name.contains("main"))
        return BackendCapabilities(Platform.TEST, blehubContractVersion(), false, false, false, false, false, false)
    }

    @Synchronized
    override fun bindEventIngress(ingress: BackendEventIngressHandle) {
        this.ingress = ingress
    }

    override fun initialize(command: InitializeCommand) {
        synchronized(this) { initializationCount++ }
        if (!completeInitialization) return
        val root = capturedIngress()
        val snapshot =
            AdapterSnapshot(AdapterState.POWERED_ON, AuthorizationState.AUTHORIZED, AuthorizationState.AUTHORIZED, null)
        check(root.push(BleEvent.AdapterSnapshotChanged(command.commandId, snapshot)) == EventIngressStatus.Delivered)
        check(root.push(BleEvent.CommandCompleted(command.commandId, null)) == EventIngressStatus.Delivered)
    }

    override fun startScan(command: StartScanCommand) {
        val root = capturedIngress()
        check(root.push(BleEvent.ScanStarted(command.commandId, command.scanSessionId)) == EventIngressStatus.Delivered)
        check(root.push(BleEvent.CommandCompleted(command.commandId, null)) == EventIngressStatus.Delivered)
    }

    override fun stopScan(command: StopScanCommand): Unit = error("unused")
    override fun connect(command: ConnectCommand): Unit = error("unused")
    override fun disconnect(command: DisconnectCommand): Unit = error("unused")
    override fun discoverGatt(command: DiscoverGattCommand): Unit = error("unused")
    override fun readCharacteristic(command: ReadCharacteristicCommand): Unit = error("unused")
    override fun writeCharacteristic(command: WriteCharacteristicCommand): Unit = error("unused")
    override fun setNotifications(command: SetNotificationsCommand): Unit = error("unused")
    override fun readRssi(command: ReadRssiCommand): Unit = error("unused")

    @Synchronized
    override fun dispose() {
        disposeCount++
        ingress = null
    }
}

private suspend fun awaitCondition(condition: () -> Boolean) {
    withTimeout(2_000) {
        while (!condition()) delay(10)
    }
}

fun main() = runBlocking {
    val library = java.nio.file.Path.of(".work/cargo-target/debug/libblehub_bleng_uniffi_async_spike.dylib")
        .toAbsolutePath().toString()
    for (component in listOf("blehub", "bleng", "blehub_bleng_uniffi_async_spike")) {
        System.setProperty("uniffi.component.$component.libraryOverride", library)
    }
    withTimeout(15_000) {
        check(blehubContractVersion() == 5u)
        AsyncProbe().use { toy ->
            check(toy.roundTrip(ULong.MAX_VALUE) == ULong.MAX_VALUE)
            val pending = async { toy.waitForRelease() }
            awaitCondition { toy.isWaiting() }
            check(!toy.wasWaitingFutureDropped())
            pending.cancelAndJoin()
            check(pending.isCancelled)
            // Coroutine cancellation frees/drops the Rust future without releasing it.
            awaitCondition { toy.wasWaitingFutureDropped() }
            toy.releaseWaiter()
            check(toy.roundTrip(17u) == 17uL)
            toy.shutdown()
        }

        val backend = ProbeBackend()
        BlengForeignProbe().use { owner ->
            owner.assembleBackend(backend)
            val lateIngress = backend.capturedIngress()
            check(owner.initialize(2_000u).state == AdapterState.POWERED_ON)
            check(owner.startScan(2_000u).value != 0uL)
            owner.shutdown()
            owner.shutdown()
            check(backend.disposals() == 1)
            check(lateIngress.push(BleEvent.CommandCompleted(CommandId(99u), null)) == EventIngressStatus.Closed)
            try {
                owner.initialize(20u)
                error("closed owner accepted initialize")
            } catch (_: ForeignProbeException.Closed) {
                // Explicit shutdown rejects subsequent calls.
            }
        }

        val pendingBackend = ProbeBackend(completeInitialization = false)
        BlengForeignProbe().use { owner ->
            owner.assembleBackend(pendingBackend)
            val pending = async { owner.initialize(5_000u) }
            awaitCondition { pendingBackend.initializations() == 1 }
            pending.cancelAndJoin()
            check(pending.isCancelled)
            owner.shutdown()
            check(pendingBackend.disposals() == 1)
        }
        println("bleng_uniffi_async_spike_kotlin_jvm=passed")
    }
}
