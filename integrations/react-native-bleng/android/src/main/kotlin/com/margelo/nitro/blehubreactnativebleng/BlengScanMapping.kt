package com.margelo.nitro.blehubreactnativebleng

import com.margelo.nitro.core.ArrayBuffer
import dev.blehub.Platform
import dev.blehub.PlatformErrorDetails
import dev.bleng_supervisor.ForeignScanAdvertisement
import dev.bleng_supervisor.ForeignScanBeginResult
import dev.bleng_supervisor.ForeignScanCommandResult
import dev.bleng_supervisor.ForeignScanEndReason
import dev.bleng_supervisor.ForeignScanFailure
import dev.bleng_supervisor.ForeignScanFailureDomain
import dev.bleng_supervisor.ForeignScanObservation
import dev.bleng_supervisor.ForeignScanStopResult

/** Mechanical projection only: Rust owns scan validation, errors and lifecycle facts. */
internal object BlengScanMapping {
    fun submissionUnavailable() = BlengScanFailureInfo(
        domain = BlengScanFailureDomain.LIFECYCLE,
        code = "submissionUnavailable",
        message = "No started session is available for scan submission.",
        platform = null,
    )

    fun bridgeFailure() = BlengScanFailureInfo(
        domain = BlengScanFailureDomain.DELIVERY,
        code = "bridgeFailure",
        message = "Native scan bridge failed.",
        platform = null,
    )

    fun failure(value: ForeignScanFailure) = BlengScanFailureInfo(
        domain = when (value.domain) {
            ForeignScanFailureDomain.INPUT -> BlengScanFailureDomain.INPUT
            ForeignScanFailureDomain.SUBMISSION -> BlengScanFailureDomain.SUBMISSION
            ForeignScanFailureDomain.OPERATION -> BlengScanFailureDomain.OPERATION
            ForeignScanFailureDomain.OBSERVATION -> BlengScanFailureDomain.OBSERVATION
            ForeignScanFailureDomain.DELIVERY -> BlengScanFailureDomain.DELIVERY
            ForeignScanFailureDomain.LIFECYCLE -> BlengScanFailureDomain.LIFECYCLE
        },
        code = value.code,
        message = value.message,
        platform = value.platformDetails?.let(::platformDetails),
    )

    private fun platform(value: Platform): String = when (value) {
        Platform.APPLE -> "apple"
        Platform.ANDROID -> "android"
        Platform.WINDOWS -> "windows"
        Platform.LINUX -> "linux"
        Platform.TEST -> "test"
    }

    private fun platformDetails(value: PlatformErrorDetails) = BlengScanPlatformDetails(
        platform = platform(value.platform),
        code = value.code,
        name = value.name,
        message = value.message,
    )

    fun command(value: ForeignScanCommandResult) = BlengScanCommandResult(value.failure?.let(::failure))

    fun submission(value: ForeignScanBeginResult): BlengScanSubmission {
        val scan = value.scan
        val error = value.failure
        return when {
            scan != null && error == null -> BlengScanSubmission.First(BlengScanAccepted(HybridBlengScan(scan)))
            scan == null && error != null -> BlengScanSubmission.Second(BlengScanFailure(failure(error)))
            else -> BlengScanSubmission.Second(BlengScanFailure(bridgeFailure()))
        }
    }

    fun stopSubmission(value: ForeignScanStopResult): BlengScanStopSubmission {
        val operation = value.operation
        val error = value.failure
        return when {
            operation != null && error == null -> BlengScanStopSubmission.First(
                BlengScanStopAccepted(HybridBlengScanStop(operation)),
            )
            operation == null && error != null -> BlengScanStopSubmission.Second(BlengScanFailure(failure(error)))
            else -> BlengScanStopSubmission.Second(BlengScanFailure(bridgeFailure()))
        }
    }

    fun observation(value: ForeignScanObservation): BlengScanNext {
        val observed = value.advertisement
        val ended = value.ended
        val error = value.failure
        return when {
            observed != null && ended == null && error == null -> BlengScanNext.First(
                BlengScanAdvertisementEvent(advertisement(observed)),
            )
            observed == null && ended != null && error == null -> BlengScanNext.Second(
                BlengScanEndedEvent(BlengScanEnd(
                    reason = when (ended.reason) {
                        ForeignScanEndReason.REQUESTED -> BlengScanEndReason.REQUESTED
                        ForeignScanEndReason.SYSTEM_SUSPENDED -> BlengScanEndReason.SYSTEMSUSPENDED
                        ForeignScanEndReason.FAILED -> BlengScanEndReason.FAILED
                        ForeignScanEndReason.OWNER_CLOSED -> BlengScanEndReason.OWNERCLOSED
                    },
                    failure = ended.failure?.let(::failure),
                )),
            )
            observed == null && ended == null && error != null -> BlengScanNext.Third(BlengScanFailure(failure(error)))
            else -> BlengScanNext.Third(BlengScanFailure(bridgeFailure()))
        }
    }

    private fun advertisement(value: ForeignScanAdvertisement) = BlengScanAdvertisement(
        peripheral = HybridBlengPeripheral(value.peripheral),
        localName = value.localName,
        advertisedServices = value.advertisedServices.toTypedArray(),
        manufacturerData = value.manufacturerData.map {
            BlengManufacturerData(it.companyId.toDouble(), ArrayBuffer.copy(it.payload))
        }.toTypedArray(),
        serviceData = value.serviceData.map {
            BlengServiceData(it.serviceUuid.value, ArrayBuffer.copy(it.payload))
        }.toTypedArray(),
        rssi = value.rssi?.toDouble(),
        connectable = value.connectable,
    )
}
