package com.margelo.nitro.blehubreactnativebleng

import com.margelo.nitro.core.ArrayBuffer
import dev.bleng_supervisor.ForeignScanAdvertisement
import dev.bleng_supervisor.ForeignScanBeginResult
import dev.bleng_supervisor.ForeignScanEndReason
import dev.bleng_supervisor.ForeignScanObservation
import dev.bleng_supervisor.ForeignScanStopResult

/** Mechanical projection only: Rust owns scan validation, errors and lifecycle facts. */
internal object BlengScanMapping {
    fun submissionUnavailable() = BlengFailureInfo(
        domain = BlengFailureDomain.LIFECYCLE,
        code = "submissionUnavailable",
        message = "No started session is available for scan submission.",
        platform = null,
    )

    fun bridgeFailure() =
        BlengFailureMapping.bridgeFailure(BlengFailureDomain.DELIVERY, "Native scan bridge failed.")

    private fun failure(value: dev.bleng_supervisor.ForeignOperationFailure) =
        BlengFailureMapping.failure(value)

    fun submission(value: ForeignScanBeginResult): BlengScanSubmission {
        val scan = value.scan
        val error = value.failure
        return when {
            scan != null && error == null -> BlengScanSubmission.First(BlengScanAccepted(HybridBlengScan(scan)))
            scan == null && error != null -> BlengScanSubmission.Second(BlengFailure(failure(error)))
            else -> BlengScanSubmission.Second(BlengFailure(bridgeFailure()))
        }
    }

    fun stopSubmission(value: ForeignScanStopResult): BlengScanStopSubmission {
        val operation = value.operation
        val error = value.failure
        return when {
            operation != null && error == null -> BlengScanStopSubmission.First(
                BlengScanStopAccepted(HybridBlengScanStop(operation)),
            )

            operation == null && error != null -> BlengScanStopSubmission.Second(BlengFailure(failure(error)))
            else -> BlengScanStopSubmission.Second(BlengFailure(bridgeFailure()))
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
                BlengScanEndedEvent(
                    BlengEnd(
                        reason = when (ended.reason) {
                            ForeignScanEndReason.REQUESTED -> BlengEndReason.REQUESTED
                            ForeignScanEndReason.SYSTEM_SUSPENDED -> BlengEndReason.SYSTEMSUSPENDED
                            ForeignScanEndReason.FAILED -> BlengEndReason.FAILED
                            ForeignScanEndReason.OWNER_CLOSED -> BlengEndReason.OWNERCLOSED
                            ForeignScanEndReason.OVERFLOW -> BlengEndReason.OVERFLOWED
                        },
                        failure = ended.failure?.let(::failure),
                    )
                ),
            )

            observed == null && ended == null && error != null -> BlengScanNext.Third(BlengFailure(failure(error)))
            else -> BlengScanNext.Third(BlengFailure(bridgeFailure()))
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
