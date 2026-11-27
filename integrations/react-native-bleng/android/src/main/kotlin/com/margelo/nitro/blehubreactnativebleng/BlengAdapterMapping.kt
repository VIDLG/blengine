package com.margelo.nitro.blehubreactnativebleng

import dev.blehub.DeviceQueryKind
import dev.bleng_supervisor.ForeignAdapterControlBeginResult
import dev.bleng_supervisor.ForeignCompanionAssociationBeginResult
import dev.bleng_supervisor.ForeignCompanionPresenceBeginResult
import dev.bleng_supervisor.ForeignCompanionPresenceEndReason
import dev.bleng_supervisor.ForeignCompanionPresenceObservation
import dev.bleng_supervisor.ForeignAdapterEndReason
import dev.bleng_supervisor.ForeignAdapterObservation
import dev.bleng_supervisor.ForeignAdapterObservationBeginResult
import dev.bleng_supervisor.ForeignDeviceListResult
import dev.bleng_supervisor.ForeignDeviceQueryBeginResult
import dev.bleng_supervisor.ForeignOperationFailure
import dev.bleng_supervisor.ForeignPeripheralAdoptResult

/** Mechanical projection of Rust's adapter DTOs. No radio outcome is inferred here. */
internal object BlengAdapterMapping {
    fun submissionUnavailable() = BlengFailureInfo(
        domain = BlengFailureDomain.LIFECYCLE,
        code = "submissionUnavailable",
        message = "No started session is available for adapter observation.",
        platform = null,
    )

    fun bridgeFailure() = BlengFailureMapping.bridgeFailure(
        BlengFailureDomain.DELIVERY,
        "Native adapter bridge failed.",
    )

    private fun failure(value: ForeignOperationFailure) = BlengFailureMapping.failure(value)

    fun submission(value: ForeignAdapterObservationBeginResult): BlengAdapterObservationSubmission {
        val observation = value.adapterObservation
        val error = value.failure
        return when {
            observation != null && error == null -> BlengAdapterObservationSubmission.First(
                BlengAdapterObservationAccepted(HybridBlengAdapterObservation(observation)),
            )

            observation == null && error != null ->
                BlengAdapterObservationSubmission.Second(BlengFailure(failure(error)))

            else -> BlengAdapterObservationSubmission.Second(BlengFailure(bridgeFailure()))
        }
    }

    fun controlSubmission(value: ForeignAdapterControlBeginResult): BlengAdapterControlSubmission {
        val control = value.control
        val error = value.failure
        return when {
            control != null && error == null -> BlengAdapterControlSubmission.First(
                BlengAdapterControlAccepted(HybridBlengAdapterControl(control)),
            )

            control == null && error != null ->
                BlengAdapterControlSubmission.Second(BlengFailure(failure(error)))

            else -> BlengAdapterControlSubmission.Second(BlengFailure(bridgeFailure()))
        }
    }

    fun companionAssociationSubmission(
        value: ForeignCompanionAssociationBeginResult,
    ): BlengCompanionAssociationSubmission {
        val association = value.association
        val error = value.failure
        return when {
            association != null && error == null -> BlengCompanionAssociationSubmission.First(
                BlengCompanionAssociationAccepted(HybridBlengCompanionAssociation(association)),
            )

            association == null && error != null ->
                BlengCompanionAssociationSubmission.Second(BlengFailure(failure(error)))

            else -> BlengCompanionAssociationSubmission.Second(BlengFailure(bridgeFailure()))
        }
    }

    fun companionPresenceSubmission(
        value: ForeignCompanionPresenceBeginResult,
    ): BlengCompanionPresenceSubmission {
        val observation = value.presenceObservation
        val error = value.failure
        return when {
            observation != null && error == null -> BlengCompanionPresenceSubmission.First(
                BlengCompanionPresenceAccepted(HybridBlengCompanionPresence(observation)),
            )

            observation == null && error != null ->
                BlengCompanionPresenceSubmission.Second(BlengFailure(failure(error)))

            else -> BlengCompanionPresenceSubmission.Second(BlengFailure(bridgeFailure()))
        }
    }

    fun companionPresenceNext(
        value: ForeignCompanionPresenceObservation,
    ): BlengCompanionPresenceNext {
        val presence = value.presence
        val ended = value.ended
        val error = value.failure
        return when {
            presence != null && ended == null && error == null -> BlengCompanionPresenceNext.First(
                BlengCompanionPresenceEvent(
                    present = presence.present,
                    key = presence.key,
                    localName = presence.localName,
                ),
            )

            presence == null && ended != null && error == null -> BlengCompanionPresenceNext.Second(
                BlengCompanionPresenceEndedEvent(
                    BlengEnd(
                        reason = when (ended.reason) {
                            ForeignCompanionPresenceEndReason.OWNER_CLOSED ->
                                BlengEndReason.OWNERCLOSED

                            ForeignCompanionPresenceEndReason.DETACHED ->
                                BlengEndReason.DETACHED
                        },
                        failure = null,
                    ),
                ),
            )

            presence == null && ended == null && error != null ->
                BlengCompanionPresenceNext.Third(BlengFailure(failure(error)))

            else -> BlengCompanionPresenceNext.Third(BlengFailure(bridgeFailure()))
        }
    }

    fun deviceQueryKind(value: BlengDeviceQueryKind): DeviceQueryKind = when (value) {
        BlengDeviceQueryKind.CONNECTED -> DeviceQueryKind.CONNECTED
        BlengDeviceQueryKind.BONDED -> DeviceQueryKind.BONDED
        BlengDeviceQueryKind.COMPANION -> DeviceQueryKind.COMPANION
    }

    fun querySubmission(
        value: ForeignDeviceQueryBeginResult,
    ): BlengDeviceQuerySubmission {
        val query = value.query
        val error = value.failure
        return when {
            query != null && error == null -> BlengDeviceQuerySubmission.First(
                BlengDeviceQueryAccepted(HybridBlengDeviceQuery(query)),
            )

            query == null && error != null ->
                BlengDeviceQuerySubmission.Second(BlengFailure(failure(error)))

            else -> BlengDeviceQuerySubmission.Second(BlengFailure(bridgeFailure()))
        }
    }

    fun adoptSubmission(
        value: ForeignPeripheralAdoptResult,
    ): BlengPeripheralAdoptSubmission {
        val peripheral = value.peripheral
        val error = value.failure
        return when {
            peripheral != null && error == null -> BlengPeripheralAdoptSubmission.First(
                BlengPeripheralAdoptAccepted(HybridBlengPeripheral(peripheral)),
            )

            peripheral == null && error != null ->
                BlengPeripheralAdoptSubmission.Second(BlengFailure(failure(error)))

            else -> BlengPeripheralAdoptSubmission.Second(BlengFailure(bridgeFailure()))
        }
    }

    fun deviceList(value: ForeignDeviceListResult): BlengDeviceQueryResult {
        val devices = value.devices
        val error = value.failure
        return when {
            devices != null && error == null -> BlengDeviceQueryResult.First(
                BlengDeviceQueryOutcome(
                    devices.map { device ->
                        BlengDeviceInfo(
                            key = device.key,
                            localName = device.localName,
                            connected = device.connected,
                            bonded = device.bonded,
                        )
                    }.toTypedArray(),
                ),
            )

            devices == null && error != null ->
                BlengDeviceQueryResult.Second(BlengFailure(failure(error)))

            else -> BlengDeviceQueryResult.Second(BlengFailure(bridgeFailure()))
        }
    }

    fun observation(value: ForeignAdapterObservation): BlengAdapterNext {
        val snapshot = value.snapshot
        val ended = value.ended
        val error = value.failure
        return when {
            snapshot != null && ended == null && error == null -> BlengAdapterNext.First(
                BlengAdapterSnapshotEvent(snapshot.toBlengAdapterSnapshot()),
            )

            snapshot == null && ended != null && error == null -> BlengAdapterNext.Second(
                BlengAdapterEndedEvent(
                    BlengEnd(
                        reason = when (ended.reason) {
                            ForeignAdapterEndReason.OWNER_CLOSED -> BlengEndReason.OWNERCLOSED
                            ForeignAdapterEndReason.DETACHED -> BlengEndReason.DETACHED
                        },
                        failure = null,
                    ),
                ),
            )

            snapshot == null && ended == null && error != null ->
                BlengAdapterNext.Third(BlengFailure(failure(error)))

            else -> BlengAdapterNext.Third(BlengFailure(bridgeFailure()))
        }
    }
}
