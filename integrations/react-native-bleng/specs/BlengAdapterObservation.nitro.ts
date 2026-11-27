import type { HybridObject } from 'react-native-nitro-modules'
import type { BlengAdapterSnapshot } from './BlengCentral.nitro'
import type { BlengEnd } from './BlengEnd.nitro'
import type { BlengFailure } from './BlengFailure.nitro'

export interface BlengAdapterSnapshotEvent {
  snapshot: BlengAdapterSnapshot
}

export interface BlengAdapterEndedEvent {
  /** Reports `ownerClosed` or `detached`. */
  ended: BlengEnd
}

// Distinct required keys are representable by the pinned Nitrogen record union.
export type BlengAdapterNext = BlengAdapterSnapshotEvent | BlengAdapterEndedEvent | BlengFailure

/**
 * The adapter's own reported state, as a stream.
 *
 * This is the only surface that reports a radio change *after* `start()`: the snapshot here is what
 * the platform said, never what the App assumed. It is an observation and nothing else — the
 * engine's recovery, the adapter and every operation stay independent of whether anyone is pulling.
 */
export interface BlengAdapterObservation
  extends HybridObject<{ ios: 'swift'; android: 'kotlin' }> {
  /** At most one pending pull; a timeout is the caller's bound and does not end observation. */
  nextEvent(timeoutMs: number): Promise<BlengAdapterNext>
}

export interface BlengAdapterObservationAccepted {
  adapterObservation: BlengAdapterObservation
}

/** Absence of failure is a bounded observation, not a claim about the radio. */
export type BlengAdapterObservationSubmission =
  | BlengAdapterObservationAccepted
  | BlengFailure
