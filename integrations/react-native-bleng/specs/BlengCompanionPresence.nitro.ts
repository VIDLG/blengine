import type { HybridObject } from 'react-native-nitro-modules'
import type { BlengEnd } from './BlengEnd.nitro'
import type { BlengFailure } from './BlengFailure.nitro'

/**
 * One presence transition for one associated peer.
 *
 * Presence is an association fact, not a link: `present` never means connected. `key` is the
 * owner-scoped inventory key, comparable with `peripheral.key()` for the same peer; `localName` is
 * the platform's association label when it has one. Transitions are edge-triggered while the
 * observation is open, so a late observer does not receive a replayed current state.
 */
export interface BlengCompanionPresenceEvent {
  present: boolean
  key: string
  localName?: string
}

export interface BlengCompanionPresenceEndedEvent {
  /** Reports `ownerClosed` or `detached`. */
  ended: BlengEnd
}

// Distinct required keys are representable by the pinned Nitrogen record union.
export type BlengCompanionPresenceNext =
  | BlengCompanionPresenceEvent
  | BlengCompanionPresenceEndedEvent
  | BlengFailure

/**
 * Live presence transitions for an associated peer.
 *
 * This is observation only. It cannot associate, connect or change the adapter, and whether anyone
 * pulls has no effect on the association or the platform's presence subscription. Presence needs an
 * association created with `notifyOnDeviceNearby`; without it the platform reports nothing.
 */
export interface BlengCompanionPresence
  extends HybridObject<{ ios: 'swift'; android: 'kotlin' }> {
  /** At most one pending pull; a timeout is the caller's bound and does not end observation. */
  nextEvent(timeoutMs: number): Promise<BlengCompanionPresenceNext>
}

export interface BlengCompanionPresenceAccepted {
  companionPresence: BlengCompanionPresence
}

/** Absence of failure is a bounded observation, not a claim about the peer. */
export type BlengCompanionPresenceSubmission =
  | BlengCompanionPresenceAccepted
  | BlengFailure
