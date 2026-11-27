import type { HybridObject } from 'react-native-nitro-modules'
import type { BlengEnd } from './BlengEnd.nitro'
import type { BlengReceipt, BlengFailure } from './BlengFailure.nitro'

/** What the caller asks for. The platform decides which mode is actually used. */
export type BlengNotificationModeRequest =
  | 'platformDefault'
  | 'requireNotification'
  | 'requireIndication'

export type BlengNotificationMode = 'notification' | 'indication'

/**
 * One subscription's values.
 *
 * This is a stream, not a receipt: a slow consumer fails closed instead of losing a value, so
 * `nextEvent` must be pulled to keep the subscription readable.
 */
export interface BlengSubscription extends HybridObject<{ ios: 'swift'; android: 'kotlin' }> {
  /** Consumes the original enable result. `mode` is absent when the platform did not report it. */
  waitUntilEnabled(timeoutMs: number): Promise<BlengSubscriptionResult>
  /** At most one pending pull; a timeout does not stop the subscription. */
  nextEvent(timeoutMs: number): Promise<BlengSubscriptionNext>
  beginDisable(): BlengSubscriptionDisableSubmission
  /** Ends observation only; the native subscription and an explicit disable stay independent. */
  detach(): void
}

export interface BlengSubscriptionDisable extends HybridObject<{ ios: 'swift'; android: 'kotlin' }> {
  wait(timeoutMs: number): Promise<BlengReceipt>
  cancel(): Promise<BlengReceipt>
}

export interface BlengSubscriptionValueEvent {
  value: ArrayBuffer
}

export interface BlengSubscriptionEndedEvent {
  /** Reports `requested`, `connectionClosed`, `ownerClosed` or `overflowed`. */
  ended: BlengEnd
}

export type BlengSubscriptionNext =
  | BlengSubscriptionValueEvent
  | BlengSubscriptionEndedEvent
  | BlengFailure

export interface BlengSubscriptionAccepted {
  notifications: BlengSubscription
}
export interface BlengSubscriptionOutcome {
  /** Absent when the platform did not report which mode it selected. */
  mode?: BlengNotificationMode
}
export interface BlengSubscriptionDisableAccepted {
  disable: BlengSubscriptionDisable
}

/** Absence of failure is a bounded submission, not an active subscription. */
export type BlengSubscriptionSubmission = BlengSubscriptionAccepted | BlengFailure
export type BlengSubscriptionResult = BlengSubscriptionOutcome | BlengFailure
export type BlengSubscriptionDisableSubmission = BlengSubscriptionDisableAccepted | BlengFailure
