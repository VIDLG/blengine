package com.margelo.nitro.blehubreactnativebleng

import com.margelo.nitro.core.ArrayBuffer
import com.margelo.nitro.core.Promise
import dev.blehub.CharacteristicProperties
import dev.blehub.MtuInfo
import dev.blehub.WriteType
import dev.bleng_supervisor.ForeignDescriptorReadBeginResult
import dev.bleng_supervisor.ForeignDescriptorReadValueResult
import dev.bleng_supervisor.ForeignDescriptorWriteBeginResult
import dev.bleng_supervisor.ForeignGattDiscoveryBeginResult
import dev.bleng_supervisor.ForeignGattDiscoveryResult
import dev.bleng_supervisor.ForeignMtuBeginResult
import dev.bleng_supervisor.ForeignMtuValueResult
import dev.bleng_supervisor.ForeignReadBeginResult
import dev.bleng_supervisor.ForeignReadValueResult
import dev.bleng_supervisor.ForeignRssiBeginResult
import dev.bleng_supervisor.ForeignRssiValueResult
import dev.bleng_supervisor.ForeignWriteBeginResult
import dev.bleng_supervisor.SupervisedDescriptorRead
import dev.bleng_supervisor.SupervisedDescriptorWrite
import dev.bleng_supervisor.SupervisedGattCharacteristic
import dev.bleng_supervisor.SupervisedGattDescriptor
import dev.bleng_supervisor.SupervisedGattDiscovery
import dev.bleng_supervisor.SupervisedGattService
import dev.bleng_supervisor.SupervisedGattSession
import dev.bleng_supervisor.SupervisedMtu
import dev.bleng_supervisor.SupervisedRead
import dev.bleng_supervisor.SupervisedRssi
import dev.bleng_supervisor.SupervisedWrite
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch

private val gattBridgeScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

private fun <T> gattPromise(bridgeFailure: () -> T, action: suspend () -> T): Promise<T> {
    val promise = Promise<T>()
    gattBridgeScope.launch {
        val result = try {
            action()
        } catch (_: Throwable) {
            bridgeFailure()
        }
        promise.resolve(result)
    }
    return promise
}

/** Mechanical projection only: Rust owns validation, classification and topology identity. */
internal object BlengGattMapping {
    fun bridgeFailure() = BlengFailureMapping.bridgeFailure(
        BlengFailureDomain.DELIVERY,
        "Native GATT bridge failed.",
    )

    fun properties(value: CharacteristicProperties) = BlengCharacteristicProperties(
        readable = value.readable,
        writableWithResponse = value.writableWithResponse,
        writableWithoutResponse = value.writableWithoutResponse,
        notifiable = value.notifiable,
        indicatable = value.indicatable,
    )

    /** Absence of a platform report is its own state, not "secondary". */
    fun kind(primary: Boolean?): BlengServiceKind = when (primary) {
        true -> BlengServiceKind.PRIMARY
        false -> BlengServiceKind.SECONDARY
        null -> BlengServiceKind.UNKNOWN
    }

    fun writeType(value: BlengWriteType): WriteType = when (value) {
        BlengWriteType.WITHRESPONSE -> WriteType.WITH_RESPONSE
        BlengWriteType.WITHOUTRESPONSE -> WriteType.WITHOUT_RESPONSE
    }

    fun discoverySubmission(value: ForeignGattDiscoveryBeginResult): BlengGattDiscoverySubmission {
        val discovery = value.discovery
        val error = value.failure
        return when {
            discovery != null && error == null -> BlengGattDiscoverySubmission.First(
                BlengGattDiscoveryAccepted(HybridBlengGattDiscovery(discovery)),
            )

            discovery == null && error != null ->
                BlengGattDiscoverySubmission.Second(BlengFailure(BlengFailureMapping.failure(error)))

            else -> BlengGattDiscoverySubmission.Second(BlengFailure(bridgeFailure()))
        }
    }

    fun discoveryResult(value: ForeignGattDiscoveryResult): BlengGattDiscoveryResult {
        val session = value.session
        val error = value.failure
        return when {
            session != null && error == null -> BlengGattDiscoveryResult.First(
                BlengGattSessionOutcome(HybridBlengGattSession(session)),
            )

            session == null && error != null ->
                BlengGattDiscoveryResult.Second(BlengFailure(BlengFailureMapping.failure(error)))

            else -> BlengGattDiscoveryResult.Second(BlengFailure(bridgeFailure()))
        }
    }

    fun readSubmission(value: ForeignReadBeginResult): BlengReadSubmission {
        val read = value.read
        val error = value.failure
        return when {
            read != null && error == null ->
                BlengReadSubmission.First(BlengReadAccepted(HybridBlengRead(read)))

            read == null && error != null ->
                BlengReadSubmission.Second(BlengFailure(BlengFailureMapping.failure(error)))

            else -> BlengReadSubmission.Second(BlengFailure(bridgeFailure()))
        }
    }

    fun readResult(value: ForeignReadValueResult): BlengReadResult {
        val bytes = value.value
        val error = value.failure
        return when {
            // A zero-length value is a real read; only the absent case is a failure.
            bytes != null && error == null ->
                BlengReadResult.First(BlengReadOutcome(ArrayBuffer.copy(bytes)))

            bytes == null && error != null ->
                BlengReadResult.Second(BlengFailure(BlengFailureMapping.failure(error)))

            else -> BlengReadResult.Second(BlengFailure(bridgeFailure()))
        }
    }

    fun writeSubmission(value: ForeignWriteBeginResult): BlengWriteSubmission {
        val write = value.write
        val error = value.failure
        return when {
            write != null && error == null ->
                BlengWriteSubmission.First(BlengWriteAccepted(HybridBlengWrite(write)))

            write == null && error != null ->
                BlengWriteSubmission.Second(BlengFailure(BlengFailureMapping.failure(error)))

            else -> BlengWriteSubmission.Second(BlengFailure(bridgeFailure()))
        }
    }

    fun rssiSubmission(value: ForeignRssiBeginResult): BlengRssiSubmission {
        val rssi = value.rssi
        val error = value.failure
        return when {
            rssi != null && error == null -> BlengRssiSubmission.First(BlengRssiAccepted(HybridBlengRssi(rssi)))
            rssi == null && error != null ->
                BlengRssiSubmission.Second(BlengFailure(BlengFailureMapping.failure(error)))

            else -> BlengRssiSubmission.Second(BlengFailure(bridgeFailure()))
        }
    }

    fun descriptorReadSubmission(value: ForeignDescriptorReadBeginResult): BlengDescriptorReadSubmission {
        val read = value.read
        val error = value.failure
        return when {
            read != null && error == null -> BlengDescriptorReadSubmission.First(
                BlengDescriptorReadAccepted(HybridBlengDescriptorRead(read)),
            )

            read == null && error != null ->
                BlengDescriptorReadSubmission.Second(BlengFailure(BlengFailureMapping.failure(error)))

            else -> BlengDescriptorReadSubmission.Second(BlengFailure(bridgeFailure()))
        }
    }

    fun descriptorReadResult(value: ForeignDescriptorReadValueResult): BlengDescriptorReadResult {
        val bytes = value.value
        val error = value.failure
        return when {
            bytes != null && error == null -> BlengDescriptorReadResult.First(
                BlengDescriptorReadOutcome(ArrayBuffer.copy(bytes)),
            )

            bytes == null && error != null ->
                BlengDescriptorReadResult.Second(BlengFailure(BlengFailureMapping.failure(error)))

            else -> BlengDescriptorReadResult.Second(BlengFailure(bridgeFailure()))
        }
    }

    fun descriptorWriteSubmission(
        value: ForeignDescriptorWriteBeginResult,
    ): BlengDescriptorWriteSubmission {
        val write = value.write
        val error = value.failure
        return when {
            write != null && error == null -> BlengDescriptorWriteSubmission.First(
                BlengDescriptorWriteAccepted(HybridBlengDescriptorWrite(write)),
            )

            write == null && error != null ->
                BlengDescriptorWriteSubmission.Second(BlengFailure(BlengFailureMapping.failure(error)))

            else -> BlengDescriptorWriteSubmission.Second(BlengFailure(bridgeFailure()))
        }
    }

    fun invalidMtu(message: String) = BlengFailureInfo(
        domain = BlengFailureDomain.INPUT,
        code = "bleng.invalid_request.mtu",
        message = message,
        platform = null,
    )

    fun mtuSubmission(value: ForeignMtuBeginResult): BlengMtuSubmission {
        val mtu = value.mtu
        val error = value.failure
        return when {
            mtu != null && error == null -> BlengMtuSubmission.First(BlengMtuAccepted(HybridBlengMtu(mtu)))
            mtu == null && error != null ->
                BlengMtuSubmission.Second(BlengFailure(BlengFailureMapping.failure(error)))

            else -> BlengMtuSubmission.Second(BlengFailure(bridgeFailure()))
        }
    }

    fun mtuInfo(value: MtuInfo) = BlengMtuInfo(
        mtu = value.mtu?.toDouble(),
        maximumWriteWithResponse = value.maximumWriteWithResponse?.toDouble(),
        maximumWriteWithoutResponse = value.maximumWriteWithoutResponse?.toDouble(),
    )

    fun mtuResult(value: ForeignMtuValueResult): BlengMtuResult {
        val info = value.info
        val error = value.failure
        return when {
            info != null && error == null ->
                BlengMtuResult.First(BlengMtuOutcome(mtuInfo(info)))

            info == null && error != null ->
                BlengMtuResult.Second(BlengFailure(BlengFailureMapping.failure(error)))

            else -> BlengMtuResult.Second(BlengFailure(bridgeFailure()))
        }
    }

    fun rssiResult(value: ForeignRssiValueResult): BlengRssiResult {
        val rssi = value.rssi
        val error = value.failure
        return when {
            // Zero and negative readings are real values; only absence is a failure.
            rssi != null && error == null -> BlengRssiResult.First(
                BlengRssiOutcome(rssi.toDouble()),
            )

            rssi == null && error != null ->
                BlengRssiResult.Second(BlengFailure(BlengFailureMapping.failure(error)))

            else -> BlengRssiResult.Second(BlengFailure(bridgeFailure()))
        }
    }
}

/** Holds only the original Rust discovery proxy. */
class HybridBlengGattDiscovery(
    private val discovery: SupervisedGattDiscovery,
) : HybridBlengGattDiscoverySpec() {
    override fun wait(timeoutMs: Double): Promise<BlengGattDiscoveryResult> = gattPromise(
        { BlengGattDiscoveryResult.Second(BlengFailure(BlengGattMapping.bridgeFailure())) },
    ) { BlengGattMapping.discoveryResult(discovery.waitResult(timeoutMs)) }

    override fun cancel(): Promise<BlengReceipt> = gattPromise(
        { BlengReceipt(BlengGattMapping.bridgeFailure()) },
    ) { BlengFailureMapping.command(discovery.cancelResult()) }
}

/** One discovered topology. Services are created per call from the Rust snapshot. */
class HybridBlengGattSession(
    private val session: SupervisedGattSession,
) : HybridBlengGattSessionSpec() {
    override fun services(): Array<HybridBlengGattServiceSpec> =
        session.services().map(::HybridBlengGattService).toTypedArray()
}

class HybridBlengGattService(
    private val service: SupervisedGattService,
) : HybridBlengGattServiceSpec() {
    override fun uuid(): String = service.uuid().value

    override fun kind(): BlengServiceKind = BlengGattMapping.kind(service.`primary`())

    override fun characteristics(): Array<HybridBlengGattCharacteristicSpec> =
        service.characteristics().map(::HybridBlengGattCharacteristic).toTypedArray()
}

class HybridBlengGattCharacteristic(
    private val characteristic: SupervisedGattCharacteristic,
) : HybridBlengGattCharacteristicSpec() {
    override fun uuid(): String = characteristic.uuid().value

    override fun properties(): BlengCharacteristicProperties =
        BlengGattMapping.properties(characteristic.properties())

    /** Descriptors are capabilities: each one keeps its native handle inside the bridge. */
    override fun descriptors(): Array<HybridBlengGattDescriptorSpec> =
        characteristic.descriptors()
            .map { HybridBlengGattDescriptor(it) }
            .toTypedArray()

    override fun beginRead(): BlengReadSubmission = try {
        BlengGattMapping.readSubmission(characteristic.beginRead())
    } catch (_: Throwable) {
        BlengReadSubmission.Second(BlengFailure(BlengGattMapping.bridgeFailure()))
    }

    /**
     * Copies the payload here because this Nitro method runs synchronously on the JS thread, where
     * a non-owning `ArrayBuffer` is still accessible. Length validation belongs to Rust; an
     * over-long value comes back as a typed failure instead of being re-checked here.
     */
    override fun beginWrite(
        value: ArrayBuffer,
        writeType: BlengWriteType,
    ): BlengWriteSubmission = try {
        BlengGattMapping.writeSubmission(
            characteristic.beginWrite(
                value.toByteArray(),
                BlengGattMapping.writeType(writeType),
            ),
        )
    } catch (_: Throwable) {
        BlengWriteSubmission.Second(BlengFailure(BlengGattMapping.bridgeFailure()))
    }

    override fun beginSubscription(
        mode: BlengNotificationModeRequest,
    ): BlengSubscriptionSubmission = try {
        BlengNotificationMapping.submission(
            characteristic.beginEnableNotifications(BlengNotificationMapping.request(mode)),
        )
    } catch (_: Throwable) {
        BlengSubscriptionSubmission.Second(
            BlengFailure(BlengNotificationMapping.bridgeFailure()),
        )
    }
}

class HybridBlengRead(private val read: SupervisedRead) : HybridBlengReadSpec() {
    override fun wait(timeoutMs: Double): Promise<BlengReadResult> = gattPromise(
        { BlengReadResult.Second(BlengFailure(BlengGattMapping.bridgeFailure())) },
    ) { BlengGattMapping.readResult(read.waitResult(timeoutMs)) }

    override fun cancel(): Promise<BlengReceipt> = gattPromise(
        { BlengReceipt(BlengGattMapping.bridgeFailure()) },
    ) { BlengFailureMapping.command(read.cancelResult()) }
}

class HybridBlengWrite(private val write: SupervisedWrite) : HybridBlengWriteSpec() {
    override fun wait(timeoutMs: Double): Promise<BlengReceipt> = gattPromise(
        { BlengReceipt(BlengGattMapping.bridgeFailure()) },
    ) { BlengFailureMapping.command(write.waitResult(timeoutMs)) }

    override fun cancel(): Promise<BlengReceipt> = gattPromise(
        { BlengReceipt(BlengGattMapping.bridgeFailure()) },
    ) { BlengFailureMapping.command(write.cancelResult()) }
}

class HybridBlengRssi(private val rssi: SupervisedRssi) : HybridBlengRssiSpec() {
    override fun wait(timeoutMs: Double): Promise<BlengRssiResult> = gattPromise(
        { BlengRssiResult.Second(BlengFailure(BlengGattMapping.bridgeFailure())) },
    ) { BlengGattMapping.rssiResult(rssi.waitResult(timeoutMs)) }

    override fun cancel(): Promise<BlengReceipt> = gattPromise(
        { BlengReceipt(BlengGattMapping.bridgeFailure()) },
    ) { BlengFailureMapping.command(rssi.cancelResult()) }
}

class HybridBlengGattDescriptor(
    private val descriptor: SupervisedGattDescriptor,
) : HybridBlengGattDescriptorSpec() {
    override fun uuid(): String = descriptor.uuid().value

    override fun beginRead(): BlengDescriptorReadSubmission = try {
        BlengGattMapping.descriptorReadSubmission(descriptor.beginRead())
    } catch (_: Throwable) {
        BlengDescriptorReadSubmission.Second(BlengFailure(BlengGattMapping.bridgeFailure()))
    }

    override fun beginWrite(value: ArrayBuffer): BlengDescriptorWriteSubmission = try {
        BlengGattMapping.descriptorWriteSubmission(descriptor.beginWrite(value.toByteArray()))
    } catch (_: Throwable) {
        BlengDescriptorWriteSubmission.Second(BlengFailure(BlengGattMapping.bridgeFailure()))
    }
}

class HybridBlengDescriptorRead(
    private val read: SupervisedDescriptorRead,
) : HybridBlengDescriptorReadSpec() {
    override fun wait(timeoutMs: Double): Promise<BlengDescriptorReadResult> = gattPromise(
        { BlengDescriptorReadResult.Second(BlengFailure(BlengGattMapping.bridgeFailure())) },
    ) { BlengGattMapping.descriptorReadResult(read.waitResult(timeoutMs)) }

    override fun cancel(): Promise<BlengReceipt> = gattPromise(
        { BlengReceipt(BlengGattMapping.bridgeFailure()) },
    ) { BlengFailureMapping.command(read.cancelResult()) }
}

class HybridBlengDescriptorWrite(
    private val write: SupervisedDescriptorWrite,
) : HybridBlengDescriptorWriteSpec() {
    override fun wait(timeoutMs: Double): Promise<BlengReceipt> = gattPromise(
        { BlengReceipt(BlengGattMapping.bridgeFailure()) },
    ) { BlengFailureMapping.command(write.waitResult(timeoutMs)) }

    override fun cancel(): Promise<BlengReceipt> = gattPromise(
        { BlengReceipt(BlengGattMapping.bridgeFailure()) },
    ) { BlengFailureMapping.command(write.cancelResult()) }
}

class HybridBlengMtu(private val mtu: SupervisedMtu) : HybridBlengMtuSpec() {
    override fun wait(timeoutMs: Double): Promise<BlengMtuResult> = gattPromise(
        { BlengMtuResult.Second(BlengFailure(BlengGattMapping.bridgeFailure())) },
    ) { BlengGattMapping.mtuResult(mtu.waitResult(timeoutMs)) }

    override fun cancel(): Promise<BlengReceipt> = gattPromise(
        { BlengReceipt(BlengGattMapping.bridgeFailure()) },
    ) { BlengFailureMapping.command(mtu.cancelResult()) }
}
