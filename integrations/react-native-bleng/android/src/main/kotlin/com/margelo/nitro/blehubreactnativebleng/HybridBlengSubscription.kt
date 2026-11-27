package com.margelo.nitro.blehubreactnativebleng

import com.margelo.nitro.core.ArrayBuffer
import com.margelo.nitro.core.Promise
import dev.blehub.NotificationMode
import dev.blehub.NotificationModeRequest
import dev.bleng_supervisor.ForeignNotificationEndReason
import dev.bleng_supervisor.ForeignNotificationObservation
import dev.bleng_supervisor.ForeignNotificationSubscribeBeginResult
import dev.bleng_supervisor.ForeignNotificationSubscribeResult
import dev.bleng_supervisor.SupervisedNotificationDisable
import dev.bleng_supervisor.SupervisedNotifications
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch

private val notificationBridgeScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

private fun <T> notificationPromise(bridgeFailure: () -> T, action: suspend () -> T): Promise<T> {
    val promise = Promise<T>()
    notificationBridgeScope.launch {
        val result = try {
            action()
        } catch (_: Throwable) {
            bridgeFailure()
        }
        promise.resolve(result)
    }
    return promise
}

/** Mechanical projection only: Rust owns admission, the lane and its end reasons. */
internal object BlengNotificationMapping {
    fun bridgeFailure() = BlengFailureMapping.bridgeFailure(
        BlengFailureDomain.DELIVERY,
        "Native notification bridge failed.",
    )

    fun request(value: BlengNotificationModeRequest): NotificationModeRequest = when (value) {
        BlengNotificationModeRequest.PLATFORMDEFAULT -> NotificationModeRequest.PLATFORM_DEFAULT
        BlengNotificationModeRequest.REQUIRENOTIFICATION ->
            NotificationModeRequest.REQUIRE_NOTIFICATION

        BlengNotificationModeRequest.REQUIREINDICATION ->
            NotificationModeRequest.REQUIRE_INDICATION
    }

    /** An unreported mode stays absent; it is never guessed from the requested mode. */
    fun mode(value: NotificationMode?): BlengNotificationMode? = when (value) {
        NotificationMode.NOTIFICATION -> BlengNotificationMode.NOTIFICATION
        NotificationMode.INDICATION -> BlengNotificationMode.INDICATION
        null -> null
    }

    fun submission(value: ForeignNotificationSubscribeBeginResult): BlengSubscriptionSubmission {
        val notifications = value.notifications
        val error = value.failure
        return when {
            notifications != null && error == null -> BlengSubscriptionSubmission.First(
                BlengSubscriptionAccepted(HybridBlengSubscription(notifications)),
            )

            notifications == null && error != null ->
                BlengSubscriptionSubmission.Second(
                    BlengFailure(BlengFailureMapping.failure(error)),
                )

            else -> BlengSubscriptionSubmission.Second(BlengFailure(bridgeFailure()))
        }
    }

    fun subscribeResult(value: ForeignNotificationSubscribeResult): BlengSubscriptionResult {
        val error = value.failure
        return if (error == null) {
            BlengSubscriptionResult.First(
                BlengSubscriptionOutcome(mode(value.actualMode)),
            )
        } else {
            BlengSubscriptionResult.Second(BlengFailure(BlengFailureMapping.failure(error)))
        }
    }

    fun observation(value: ForeignNotificationObservation): BlengSubscriptionNext {
        val bytes = value.value
        val ended = value.ended
        val error = value.failure
        return when {
            // An empty value is a real notification; only its absence is a failure.
            bytes != null && ended == null && error == null ->
                BlengSubscriptionNext.First(
                    BlengSubscriptionValueEvent(ArrayBuffer.copy(bytes)),
                )

            bytes == null && ended != null && error == null -> BlengSubscriptionNext.Second(
                BlengSubscriptionEndedEvent(
                    BlengEnd(
                        reason = when (ended.reason) {
                            ForeignNotificationEndReason.DISABLED -> BlengEndReason.REQUESTED
                            ForeignNotificationEndReason.CONNECTION_CLOSED ->
                                BlengEndReason.CONNECTIONCLOSED

                            ForeignNotificationEndReason.OWNER_CLOSED -> BlengEndReason.OWNERCLOSED
                            ForeignNotificationEndReason.OVERFLOW -> BlengEndReason.OVERFLOWED
                        },
                        failure = null,
                    ),
                ),
            )

            bytes == null && ended == null && error != null ->
                BlengSubscriptionNext.Third(BlengFailure(BlengFailureMapping.failure(error)))

            else -> BlengSubscriptionNext.Third(BlengFailure(bridgeFailure()))
        }
    }

    fun disableSubmission(
        value: dev.bleng_supervisor.ForeignNotificationDisableResult,
    ): BlengSubscriptionDisableSubmission {
        val operation = value.operation
        val error = value.failure
        return when {
            operation != null && error == null -> BlengSubscriptionDisableSubmission.First(
                BlengSubscriptionDisableAccepted(HybridBlengSubscriptionDisable(operation)),
            )

            operation == null && error != null -> BlengSubscriptionDisableSubmission.Second(
                BlengFailure(BlengFailureMapping.failure(error)),
            )

            else -> BlengSubscriptionDisableSubmission.Second(BlengFailure(bridgeFailure()))
        }
    }
}

/** Holds only the Rust lane endpoint; never the subscription identity. */
class HybridBlengSubscription(
    private val notifications: SupervisedNotifications,
) : HybridBlengSubscriptionSpec() {
    override fun waitUntilEnabled(timeoutMs: Double): Promise<BlengSubscriptionResult> =
        notificationPromise(
            { BlengSubscriptionResult.Second(BlengFailure(BlengNotificationMapping.bridgeFailure())) },
        ) { BlengNotificationMapping.subscribeResult(notifications.waitResult(timeoutMs)) }

    override fun nextEvent(timeoutMs: Double): Promise<BlengSubscriptionNext> = notificationPromise(
        { BlengSubscriptionNext.Third(BlengFailure(BlengNotificationMapping.bridgeFailure())) },
    ) { BlengNotificationMapping.observation(notifications.nextObservation(timeoutMs)) }

    override fun beginDisable(): BlengSubscriptionDisableSubmission = try {
        BlengNotificationMapping.disableSubmission(notifications.beginDisableView())
    } catch (_: Throwable) {
        BlengSubscriptionDisableSubmission.Second(
            BlengFailure(BlengNotificationMapping.bridgeFailure()),
        )
    }

    override fun detach() {
        runCatching { notifications.detach() }
    }
}

class HybridBlengSubscriptionDisable(
    private val operation: SupervisedNotificationDisable,
) : HybridBlengSubscriptionDisableSpec() {
    override fun wait(timeoutMs: Double): Promise<BlengReceipt> = notificationPromise(
        { BlengReceipt(BlengNotificationMapping.bridgeFailure()) },
    ) { BlengFailureMapping.command(operation.waitResult(timeoutMs)) }

    override fun cancel(): Promise<BlengReceipt> = notificationPromise(
        { BlengReceipt(BlengNotificationMapping.bridgeFailure()) },
    ) { BlengFailureMapping.command(operation.cancelResult()) }
}
