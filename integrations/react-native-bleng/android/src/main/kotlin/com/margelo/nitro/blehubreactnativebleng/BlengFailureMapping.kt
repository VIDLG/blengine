package com.margelo.nitro.blehubreactnativebleng

import dev.blehub.Platform
import dev.blehub.PlatformErrorDetails
import dev.bleng_supervisor.ForeignOperationCommandResult
import dev.bleng_supervisor.ForeignOperationFailure
import dev.bleng_supervisor.ForeignOperationFailureDomain
import dev.bleng_supervisor.SupervisorFfiException

/**
 * The one native projection of Rust's operation failure. Scan and connection views classify the
 * same failure with this code instead of keeping a second mapping.
 */
internal object BlengFailureMapping {
    fun bridgeFailure(domain: BlengFailureDomain, message: String) = BlengFailureInfo(
        domain = domain,
        code = "bridgeFailure",
        message = message,
        platform = null,
    )

    /**
     * The one classification of a supervisor boundary error. [Timeout] keeps its own stable code so
     * callers can distinguish a bounded owner-assembly wait from a genuine platform failure.
     */
    fun supervisorFailure(value: SupervisorFfiException) = when (value) {
        is SupervisorFfiException.InvalidContext -> BlengFailureInfo(
            domain = BlengFailureDomain.INPUT,
            code = "bleng.invalid_request.context",
            message = value.detail,
            platform = null,
        )

        is SupervisorFfiException.InvalidConfiguration -> BlengFailureInfo(
            domain = BlengFailureDomain.INPUT,
            code = "bleng.invalid_request.configuration",
            message = value.detail,
            platform = null,
        )

        is SupervisorFfiException.NoActiveContext -> BlengFailureInfo(
            domain = BlengFailureDomain.LIFECYCLE,
            code = "submissionUnavailable",
            message = "Supervisor has no active owner context.",
            platform = null,
        )

        is SupervisorFfiException.Timeout -> BlengFailureInfo(
            domain = BlengFailureDomain.LIFECYCLE,
            code = "bleng.timeout",
            message = value.detail,
            platform = null,
        )

        is SupervisorFfiException.Busy -> BlengFailureInfo(
            domain = BlengFailureDomain.LIFECYCLE,
            code = "supervisor.busy",
            message = "Supervisor operation is already in progress.",
            platform = null,
        )

        is SupervisorFfiException.Platform -> BlengFailureInfo(
            domain = BlengFailureDomain.LIFECYCLE,
            code = "bridgeFailure",
            message = value.detail,
            platform = null,
        )
    }

    fun failure(value: ForeignOperationFailure) = BlengFailureInfo(
        domain = when (value.domain) {
            ForeignOperationFailureDomain.INPUT -> BlengFailureDomain.INPUT
            ForeignOperationFailureDomain.SUBMISSION -> BlengFailureDomain.SUBMISSION
            ForeignOperationFailureDomain.OPERATION -> BlengFailureDomain.OPERATION
            ForeignOperationFailureDomain.OBSERVATION -> BlengFailureDomain.OBSERVATION
            ForeignOperationFailureDomain.DELIVERY -> BlengFailureDomain.DELIVERY
            ForeignOperationFailureDomain.LIFECYCLE -> BlengFailureDomain.LIFECYCLE
        },
        code = value.code,
        message = value.message,
        platform = value.platformDetails?.let(::platformDetails),
    )

    fun command(value: ForeignOperationCommandResult) = BlengReceipt(
        value.failure?.let(::failure),
    )

    private fun platform(value: Platform): String = when (value) {
        Platform.APPLE -> "apple"
        Platform.ANDROID -> "android"
        Platform.WINDOWS -> "windows"
        Platform.LINUX -> "linux"
        Platform.TEST -> "test"
    }

    private fun platformDetails(value: PlatformErrorDetails) = BlengPlatformDetails(
        platform = platform(value.platform),
        code = value.code,
        name = value.name,
        message = value.message,
        status = value.status?.toDouble(),
        attCode = value.attCode?.toDouble(),
    )
}
